"""Root-side operations of the privileged helper (see DESIGN.md §16).

Threat model: the request ``m`` and every file it points to are controlled by the caller - a local
user who authenticated as administrator, or any process of that user that reuses the
``auth_admin_keep`` window. Therefore:

* every value is type- and range-checked; unknown keys are ignored;
* source files are opened with the *caller's* privileges (``O_NOFOLLOW``), so the helper can never
  be used to read a file the caller could not read;
* destinations are computed here from :class:`Layout`, never taken from the request; directories
  are walked component by component without following symlinks, files are created as temporary
  files next to their destination and moved into place with ``rename`` (which replaces, but never
  writes through, a symlink or hard link planted at the destination);
* files recorded in the (possibly tampered) registry are only deleted inside the layout's folders
  and only if their names look exactly like the ones Easy Installer creates;
* the replaced version of an app is kept in ``<apps_dir>/.easyinstaller-backups/<id>/`` (a path
  computed here, root-owned): it gets there by a hard link or a rename inside the apps folder's
  file system, never by a copy, and it is only used ("consume_backup") or deleted when it is
  exactly the ``previous`` recorded for that app id and lies in that app's backup folder;
* values the helper only stores for the client (update source, origin, signature, data hints)
  are small, flat and free of control characters.

The operations work without root against ``system_layout(tmp)`` with ``run_commands=False``: the
privilege switch and ``fchown`` are skipped when the process is not root.
"""

from __future__ import annotations

import calendar
import contextlib
import dataclasses
import errno
import hashlib
import logging
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from .. import __version__
from ..core.desktop_entry import INVALID_CHARS_RE, DesktopEntry, exec_env, exec_program
from ..core.elf import ELF_MAGIC
from ..core.imageinfo import ImageInfo, probe_image
from ..core.integration import (
    ACTION_GROUP_PREFIX,
    FALLBACK_ICON,
    ID_RE,
    UNINSTALL_ACTION,
    MAX_MIME_TYPES,
    DesktopRenderSpec,
    MimeTypeDef,
    apparmor_profile_name,
    appimage_target,
    backup_file_name,
    desktop_file_name,
    exec_env_allowed,
    host_mime_database,
    icon_name,
    icon_target,
    mime_package_name,
    render_desktop_entry,
    render_mime_package,
    select_mime_types,
)
from ..core.paths import BACKUPS_DIR_NAME, Layout, Scope
from ..core.registry import InstalledApp, Registry, make_previous, utc_now
from ..core.sandbox import NO_SANDBOX_ARG, SandboxFix, render_apparmor_profile
from ..core.system_checks import LIBFUSE2, LIBFUSE2_T64, SystemStatus
from ..errors import HelperError, InstallError, NotInstalledError
from ..i18n import _

log = logging.getLogger(__name__)

T = TypeVar("T")

# -- limits and fixed values -------------------------------------------------------------------

SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
COMMAND_ENV = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
TOOL_TIMEOUT = 120
APT_TIMEOUT = 480
APT_LOCK_TIMEOUT = 120
#: How long an operation waits for another one to finish with the system registry.
REGISTRY_LOCK_TIMEOUT = 120

DIR_MODE = 0o755
FILE_MODE = 0o644
EXEC_MODE = 0o755
TEMP_MODE = 0o600

CHUNK_SIZE = 1 << 20
MAX_APPIMAGE_SIZE = 64 << 30
MAX_ICON_SIZE = 10 * 1024 * 1024
MAX_PROFILE_SIZE = 64 * 1024
MAX_DESKTOP_TEXT = 512 * 1024
MAX_NAME = 200
MAX_PATH = 4096
MAX_FILENAME = 255
MAX_VERSION = 128
MAX_COMMENT = 2000
MAX_UPDATE_INFO = 1024
MAX_ARCH = 32
FREE_SPACE_MARGIN = 16 * 1024 * 1024
OUTPUT_TAIL = 4000
#: Values the helper only stores in the registry for the client (never uses itself).
MAX_URL = 2048
MAX_STORED_KEYS = 16
MAX_STORED_VALUE = 4096
MAX_STORED_INT = 2 ** 53
MAX_DATA_HINTS = 32
MAX_DATA_HINT = 128
#: How long a replaced version may be kept (``backup_max_age_days`` is clamped to this).
MIN_BACKUP_AGE_DAYS = 0   # 0: every kept version goes (backups are switched off)
MAX_BACKUP_AGE_DAYS = 90
SECONDS_PER_DAY = 24 * 3600
SAVED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MAX_NAME_TRIES = 1000

LAUNCHER_NAME = "easy-installer"
UNINSTALL_FLAG = "--uninstall"
ALLOWED_EXTRA_ARGS = frozenset({NO_SANDBOX_ARG})
FUSE_PACKAGES = frozenset({LIBFUSE2, LIBFUSE2_T64})
PROFILE_MARKER = "# Managed by Easy Installer"
#: Owners accepted for the uninstall launcher and its folders (the launcher runs for every user).
TRUSTED_UIDS = frozenset({0})

_SOURCE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

# Control characters (C0, DEL, C1), unpaired surrogates and Unicode line/paragraph separators.
_BAD_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
_BAD_CHARS_MULTILINE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
#: The embedded desktop file is only parsed: render_desktop_entry() drops lines with control
#: characters (as for per-user installs) and verify_desktop() checks the result. Lone surrogates
#: could not even be written.
_SURROGATES = re.compile("[\ud800-\udfff]")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_ARCH_RE = re.compile(r"[A-Za-z0-9_.-]{0,%d}" % MAX_ARCH)
_APPIMAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,240}\.AppImage")
_STORED_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_URL_RE = re.compile(r"https?://[^\s/]", re.IGNORECASE)
_ICON_SIZE_DIR_RE = re.compile(r"scalable|([1-9][0-9]{0,3})x\1")
_ICON_EXTENSIONS = ("png", "svg", "xpm")
_PROFILE_ATTACH_RE = re.compile(r'^profile\s+\S+\s+"((?:[^"\\\n]|\\.)*)"', re.MULTILINE)


def _no_op() -> None:
    pass


#: Called once an operation has saved its result (the registry): from then on it is finished in
#: any case. helper.main uses it to ignore Ctrl+C, which reaches the root helper from the
#: terminal, too - an interrupt during the cache refresh must not turn an installed app into a
#: "failed" answer. Tests leave it alone.
on_committed: Callable[[], None] = _no_op


# -- errors -------------------------------------------------------------------------------------


class RequestError(HelperError):
    """The request itself is invalid (wrong types, forbidden values). Nothing was changed."""

    def __init__(self, details: str):
        super().__init__(_("Something went wrong, so nothing was changed. Please try again."),
                         details=details)


class UnsafePathError(InstallError):
    """A folder or file on the way to a destination is a symlink, has an unexpected owner, ..."""

    def __init__(self, path: Path | str, reason: str):
        super().__init__(
            _("Easy Installer stopped because a folder on this computer is set up in an "
              "unexpected way: {path}").format(path=path),
            details=f"{path}: {reason}",
        )


class ProfileConflictError(InstallError):
    """The AppArmor profile file exists and belongs to something else."""

    def __init__(self, details: str):
        super().__init__(
            _("Another installation of this app already has this special permission, so it "
              "was not changed."),
            details=details,
        )


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


# -- request validation ---------------------------------------------------------------------------


def _require_mapping(m: Any) -> Mapping[str, Any]:
    if not isinstance(m, dict):
        raise RequestError(f"request must be a JSON object, got {type(m).__name__}")
    return m


def _opt_text(m: Mapping[str, Any], key: str, *, max_len: int,
              pattern: re.Pattern[str] = _BAD_CHARS) -> str | None:
    value = m.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(f"{key} must be a string, got {type(value).__name__}")
    if len(value) > max_len:
        raise RequestError(f"{key} is too long ({len(value)} > {max_len} characters)")
    if pattern.search(value):
        raise RequestError(f"{key} contains control characters")
    return value


def _text(m: Mapping[str, Any], key: str, *, max_len: int) -> str:
    value = _opt_text(m, key, max_len=max_len)
    if value is None or not value.strip():
        raise RequestError(f"{key} is missing or empty")
    return value


def _flag(m: Mapping[str, Any], key: str) -> bool:
    value = m.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be true or false, got {type(value).__name__}")
    return value


def _app_id(m: Mapping[str, Any]) -> str:
    value = m.get("app_id")
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise RequestError(f"invalid app_id {_short(value)}")
    return value


def _is_clean_abs_path(value: str) -> bool:
    return (value.startswith("/") and not value.startswith("//")
            and os.path.normpath(value) == value and not _BAD_CHARS.search(value))


def _opt_abs_path(m: Mapping[str, Any], key: str) -> str | None:
    value = _opt_text(m, key, max_len=MAX_PATH)
    if value is not None and not _is_clean_abs_path(value):
        raise RequestError(f"{key} must be an absolute, normalised path: {_short(value)}")
    return value


def _abs_path(m: Mapping[str, Any], key: str) -> str:
    value = _opt_abs_path(m, key)
    if value is None:
        raise RequestError(f"{key} is missing")
    return value


def _file_name(m: Mapping[str, Any], key: str) -> str | None:
    value = _opt_text(m, key, max_len=MAX_FILENAME)
    if value is None:
        return None
    if "/" in value or value in ("", ".", ".."):
        raise RequestError(f"{key} must be a plain file name: {_short(value)}")
    return value


def _opt_id(m: Mapping[str, Any], key: str) -> str | None:
    value = m.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise RequestError(f"invalid {key} {_short(value)}")
    return value


def _stored_dict(m: Mapping[str, Any], key: str) -> dict | None:
    """A small flat JSON object the helper only stores (update source, signature).

    Keys are plain identifiers; values are null, true/false, whole numbers or text without
    control characters (tab and line feed are allowed: nothing of it ever reaches a launcher).
    """
    value = m.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RequestError(f"{key} must be an object, got {type(value).__name__}")
    if len(value) > MAX_STORED_KEYS:
        raise RequestError(f"{key} has too many entries ({len(value)} > {MAX_STORED_KEYS})")
    result: dict[str, Any] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not _STORED_KEY_RE.fullmatch(name):
            raise RequestError(f"{key} has an invalid key {_short(name)}")
        if item is None or isinstance(item, bool):
            pass
        elif isinstance(item, int):
            if abs(item) > MAX_STORED_INT:
                raise RequestError(f"{key}.{name} is out of range")
        elif isinstance(item, str):
            if len(item) > MAX_STORED_VALUE:
                raise RequestError(f"{key}.{name} is too long ({len(item)} > {MAX_STORED_VALUE} "
                                   "characters)")
            if _BAD_CHARS_MULTILINE.search(item):
                raise RequestError(f"{key}.{name} contains control characters")
        else:
            raise RequestError(f"{key}.{name} must be text, a whole number, true/false or null, "
                               f"got {type(item).__name__}")
        result[name] = item
    return result or None


def _opt_url(m: Mapping[str, Any], key: str) -> str | None:
    value = _opt_text(m, key, max_len=MAX_URL)
    if value is None or value == "":
        return None
    if not _URL_RE.match(value):
        raise RequestError(f"{key} must be an http(s) address: {_short(value)}")
    return value


def _data_hints(m: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = m.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_DATA_HINTS:
        raise RequestError(f"{key} must be a list of at most {MAX_DATA_HINTS} names")
    for item in value:
        if not _valid_hint(item):
            raise RequestError(f"{key} contains an invalid name {_short(item)}")
    return tuple(dict.fromkeys(value))


def _valid_hint(item: Any) -> bool:
    return (isinstance(item, str) and bool(item.strip()) and len(item) <= MAX_DATA_HINT
            and not _BAD_CHARS.search(item) and "/" not in item and item not in (".", ".."))


def storable_dict(value: Any) -> dict | None:
    """``value`` reduced to what :func:`_stored_dict` accepts (client side, before sending).

    The helper rejects a request with odd values rather than repairing it; the client therefore
    drops what cannot be stored (nested data, odd keys) and cleans text, so that e.g. an
    unusual signature note never stops an installation.
    """
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for name, item in value.items():
        if len(result) >= MAX_STORED_KEYS:
            break
        if not isinstance(name, str) or not _STORED_KEY_RE.fullmatch(name):
            continue
        if isinstance(item, str):
            item = _BAD_CHARS_MULTILINE.sub("", item)[:MAX_STORED_VALUE]
        elif item is None or isinstance(item, bool):
            pass
        elif not isinstance(item, int) or abs(item) > MAX_STORED_INT:
            continue
        result[name] = item
    return result or None


def storable_url(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= MAX_URL and not _BAD_CHARS.search(value) \
            and _URL_RE.match(value):
        return value
    return None


def storable_hints(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(item for item in value if _valid_hint(item)))[:MAX_DATA_HINTS]


def _backup_age(m: Mapping[str, Any], key: str) -> int | None:
    value = m.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"{key} must be a whole number of days, got {_short(value)}")
    return max(MIN_BACKUP_AGE_DAYS, min(MAX_BACKUP_AGE_DAYS, value))


@dataclass(frozen=True)
class InstallRequest:
    app_id: str
    name: str
    version: str | None
    comment: str | None
    source_appimage: str
    sha256: str
    embedded_desktop: str | None
    embedded_desktop_filename: str | None
    icon_source: str | None
    extra_args: tuple[str, ...]
    extract_and_run: bool
    apparmor: bool
    uninstall_command: Any
    arch: str
    size: int | None
    update_info: str | None
    original_filename: str | None
    is_electron: bool
    mime_types: tuple[MimeTypeDef, ...] = ()
    extract_and_run_explicit: bool = False
    #: keep the version this installation replaces in the app's backup folder
    keep_backup: bool = False
    #: the source is this app's recorded previous version (going back): it is used up
    consume_backup: bool = False
    #: prune system backups older than this many days (None: do not prune)
    backup_max_age_days: int | None = None
    # -- only stored in the registry for the client --
    base_id: str | None = None
    pinned: bool = False
    update_source: dict | None = None
    origin_url: str | None = None
    signature: dict | None = None
    data_hints: tuple[str, ...] = ()

    @property
    def embedded_stem(self) -> str | None:
        if not self.embedded_desktop_filename:
            return None
        return self.embedded_desktop_filename.removesuffix(".desktop") or None

    @classmethod
    def from_manifest(cls, m: Any) -> InstallRequest:
        m = _require_mapping(m)
        sha = m.get("sha256")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise RequestError(f"sha256 must be 64 hex digits, got {_short(sha)}")
        extra = m.get("extra_args") or []
        if not isinstance(extra, list) or len(extra) > 8 \
                or not all(isinstance(a, str) and a in ALLOWED_EXTRA_ARGS for a in extra):
            raise RequestError(f"extra_args may only contain {sorted(ALLOWED_EXTRA_ARGS)}: {_short(extra)}")
        size = m.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)
                                 or not 0 <= size <= MAX_APPIMAGE_SIZE):
            raise RequestError(f"size must be a non-negative integer, got {_short(size)}")
        arch = _opt_text(m, "arch", max_len=MAX_ARCH) or ""
        if not _ARCH_RE.fullmatch(arch):
            raise RequestError(f"invalid arch {_short(arch)}")
        mime = m.get("mime_types") or []
        if not isinstance(mime, list) or len(mime) > MAX_MIME_TYPES:
            raise RequestError(f"mime_types must be a list of at most {MAX_MIME_TYPES} entries")
        try:
            mime_types = tuple(MimeTypeDef.from_dict(entry) for entry in mime)
        except ValueError as exc:
            raise RequestError(f"invalid mime_types: {exc}") from None
        app_id = _app_id(m)
        base_id = _opt_id(m, "base_id")
        if base_id is not None and base_id == app_id:
            raise RequestError("base_id must be the id of another app")
        return cls(
            app_id=app_id,
            name=_text(m, "name", max_len=MAX_NAME),
            version=_opt_text(m, "version", max_len=MAX_VERSION) or None,
            comment=_opt_text(m, "comment", max_len=MAX_COMMENT, pattern=_BAD_CHARS_MULTILINE) or None,
            source_appimage=_abs_path(m, "source_appimage"),
            sha256=sha.lower(),
            embedded_desktop=_opt_text(m, "embedded_desktop", max_len=MAX_DESKTOP_TEXT,
                                       pattern=_SURROGATES),
            embedded_desktop_filename=_file_name(m, "embedded_desktop_filename"),
            icon_source=_opt_abs_path(m, "icon_source"),
            extra_args=tuple(dict.fromkeys(extra)),
            extract_and_run=_flag(m, "extract_and_run"),
            apparmor=_flag(m, "apparmor"),
            uninstall_command=m.get("uninstall_command"),
            arch=arch,
            size=size,
            update_info=_opt_text(m, "update_info", max_len=MAX_UPDATE_INFO) or None,
            original_filename=_file_name(m, "original_filename"),
            is_electron=_flag(m, "is_electron"),
            mime_types=mime_types,
            extract_and_run_explicit=_flag(m, "extract_and_run_explicit"),
            keep_backup=_flag(m, "keep_backup"),
            consume_backup=_flag(m, "consume_backup"),
            backup_max_age_days=_backup_age(m, "backup_max_age_days"),
            base_id=base_id,
            pinned=_flag(m, "pinned"),
            update_source=_stored_dict(m, "update_source"),
            origin_url=_opt_url(m, "origin_url"),
            signature=_stored_dict(m, "signature"),
            data_hints=_data_hints(m, "data_hints"),
        )


# -- privileges -------------------------------------------------------------------------------------


def running_as_root() -> bool:
    return os.geteuid() == 0


def _local_owner_uids() -> frozenset[int]:
    """Owners accepted for folders we write into: root, or ourselves when not running as root."""
    euid = os.geteuid()
    return frozenset({0}) if euid == 0 else frozenset({0, euid})


def primary_gid(uid: int) -> int:
    try:
        return pwd.getpwuid(uid).pw_gid
    except KeyError as exc:
        raise RequestError(f"unknown user id {uid}") from exc


def caller_groups(uid: int, gid: int) -> list[int]:
    """Supplementary groups of ``uid`` (so root's own groups never grant access)."""
    try:
        name = pwd.getpwuid(uid).pw_name
        groups = os.getgrouplist(name, gid)
    except (KeyError, OSError):
        groups = []
    return list(dict.fromkeys([gid, *groups]))


def _switch(what: str, fn: Callable[[Any], None], value: Any) -> None:
    """A privilege change; failing is fatal and never mistaken for a file access error."""
    try:
        fn(value)
    except OSError as exc:
        raise HelperError(_("The administrator task failed unexpectedly."),
                          details=f"{what}({value!r}) failed: {exc}") from None


@contextlib.contextmanager
def caller_privileges(uid: int, gid: int) -> Iterator[None]:
    """Temporarily act with the caller's uid, gid and groups (only when running as root).

    Restoring is done in ``finally`` blocks, so root privileges come back even when the body
    raises. If restoring itself fails a HelperError propagates and the helper aborts.
    """
    if not running_as_root():
        yield
        return
    saved_euid, saved_egid, saved_groups = os.geteuid(), os.getegid(), os.getgroups()
    _switch("setgroups", os.setgroups, caller_groups(uid, gid))
    try:
        _switch("setegid", os.setegid, gid)
        try:
            _switch("seteuid", os.seteuid, uid)
            try:
                yield
            finally:
                _switch("seteuid", os.seteuid, saved_euid)
        finally:
            _switch("setegid", os.setegid, saved_egid)
    finally:
        _switch("setgroups", os.setgroups, saved_groups)


def as_caller(uid: int, gid: int, fn: Callable[[], T]) -> T:
    with caller_privileges(uid, gid):
        return fn()


# -- reading client files ---------------------------------------------------------------------------


def _procfs_dev() -> int | None:
    """Device number of procfs (its files look like regular files but must never be sources)."""
    try:
        return os.stat("/proc/self").st_dev   # only exists when procfs is really mounted
    except OSError:
        return None


def _source_error(exc: OSError, path: str) -> InstallError:
    name = os.path.basename(path)
    details = f"{path}: {exc}"
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return InstallError(
            _("The file {name} is only a link to another file. Please choose the file itself.")
            .format(name=name), details=details)
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return InstallError(
            _("The file {name} is no longer there. Was it moved or deleted?").format(name=name),
            details=details)
    if exc.errno in (errno.EACCES, errno.EPERM):
        return InstallError(_("You are not allowed to read the file {name}.").format(name=name),
                            details=details)
    return InstallError(_("The file {name} could not be read.").format(name=name), details=details)


def _open_nofollow(path: str) -> int:
    return os.open(path, _SOURCE_FLAGS)


def open_source(path: str, uid: int, gid: int) -> tuple[int, os.stat_result]:
    """Open a client-supplied file with the caller's privileges; it must be a regular file."""
    try:
        fd = as_caller(uid, gid, lambda: _open_nofollow(path))
    except OSError as exc:
        raise _source_error(exc, path) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InstallError(_("{name} is not a normal file.").format(name=os.path.basename(path)),
                               details=f"{path}: mode {stat.filemode(st.st_mode)}")
        if st.st_dev == _procfs_dev():
            raise InstallError(_("{name} is not a normal file.").format(name=os.path.basename(path)),
                               details=f"{path}: files in /proc are not accepted")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _read_limited(fd: int, limit: int) -> bytes | None:
    """Everything from ``fd`` (from the current position), or None if it exceeds ``limit``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(CHUNK_SIZE, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            return None


def probe_image_bytes(data: bytes) -> ImageInfo:
    """:func:`probe_image` for data held in memory (never touches the caller's file again)."""
    try:
        fd = os.memfd_create("easyinstaller-icon", os.MFD_CLOEXEC)
    except (AttributeError, OSError):
        fd = -1
    if fd >= 0:
        try:
            _write_all(fd, data)
            view = Path(f"/proc/self/fd/{fd}")
            if view.exists():
                return probe_image(view)
        finally:
            os.close(fd)
    with tempfile.TemporaryDirectory(prefix="easyinstaller-") as tmp:
        probe = Path(tmp) / "icon"
        probe.write_bytes(data)
        return probe_image(probe)


def read_icon(path: str, uid: int, gid: int) -> tuple[bytes, ImageInfo]:
    fd, st = open_source(path, uid, gid)
    try:
        if st.st_size > MAX_ICON_SIZE:
            data = None
        else:
            try:
                data = _read_limited(fd, MAX_ICON_SIZE)
            except OSError as exc:
                raise _source_error(exc, path) from exc
    finally:
        os.close(fd)
    if data is None:
        raise InstallError(_("The app icon is too large."), details=f"{path}: > {MAX_ICON_SIZE} bytes")
    info = probe_image_bytes(data)
    if not data or info.format == "unknown":
        raise InstallError(_("The app icon could not be read."), details=f"{path}: not a PNG/SVG/XPM image")
    return data, info


# -- safe destination folders -----------------------------------------------------------------------


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _check_name(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise UnsafePathError(name, "invalid file name")


def _check_dir(fd: int, path: Path) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(path, "not a folder")
    if st.st_uid not in _local_owner_uids():
        raise UnsafePathError(path, f"owned by uid {st.st_uid}")
    if st.st_mode & stat.S_IWOTH and not st.st_mode & stat.S_ISVTX:
        raise UnsafePathError(path, "writable by everyone")


def _open_child_dir(parent_fd: int, name: str, path: Path, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(path, "is a symbolic link or not a folder") from exc
        raise
    created = True
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        created = False
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(path, "is a symbolic link or not a folder") from exc
        raise
    if created:
        try:
            os.fchmod(fd, DIR_MODE)
        except BaseException:
            os.close(fd)
            raise
        log.info("created folder %s", path)
    return fd


class SafeDir:
    """An open folder reached from ``/`` without following symlinks.

    Every folder on the way must be a real folder owned by root (or by us when not running as
    root) and not writable by everyone (unless sticky, like /tmp). All file operations are
    relative to the folder's descriptor, so the checked folder is the one that is changed.
    """

    def __init__(self, path: Path, fd: int):
        self.path = path
        self.fd = fd

    @classmethod
    def open(cls, path: Path | str, *, create: bool = False) -> SafeDir:
        text = os.fspath(path)
        if not _is_clean_abs_path(text):
            raise UnsafePathError(text, "not an absolute, normalised path")
        path = Path(text)
        fd = os.open("/", _DIR_FLAGS)
        current = Path("/")
        try:
            _check_dir(fd, current)
            for part in path.parts[1:]:
                current = current / part
                child = _open_child_dir(fd, part, current, create)
                os.close(fd)
                fd = child
                _check_dir(fd, current)
        except BaseException:
            os.close(fd)
            raise
        return cls(path, fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> SafeDir:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def lstat(self, name: str) -> os.stat_result | None:
        _check_name(name)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def create_temp(self) -> tuple[int, str]:
        for _attempt in range(32):
            name = f".easyinstaller-{secrets.token_hex(8)}.tmp"
            try:
                return os.open(name, _NEW_FILE_FLAGS, TEMP_MODE, dir_fd=self.fd), name
            except FileExistsError:
                continue
        raise InstallError(_("The app could not be installed because a file could not be saved."),
                           details=f"no free temporary name in {self.path}")

    def write_temp(self, data: bytes, mode: int) -> str:
        """A new, complete file with ``data`` under a temporary name; returns that name."""
        fd, tmp = self.create_temp()
        try:
            _write_all(fd, data)
            _finish_file(fd, mode)
        except BaseException:
            os.close(fd)
            self.unlink(tmp)
            raise
        os.close(fd)
        return tmp

    def write_file(self, name: str, data: bytes, mode: int) -> None:
        """Atomically create or replace ``name`` (a symlink at ``name`` is replaced, not followed)."""
        _check_name(name)
        st = self.lstat(name)
        if st is not None and stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(self.path / name, "a folder is in the way")
        tmp = self.write_temp(data, mode)
        try:
            self.replace(tmp, name)
        except BaseException:
            self.unlink(tmp)
            raise
        self.fsync()

    def read_regular(self, name: str, limit: int) -> bytes | None:
        """Content of the regular file ``name`` (None if missing); refuses symlinks & big files."""
        _check_name(name)
        try:
            fd = os.open(name, _SOURCE_FLAGS, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePathError(self.path / name, "is a symbolic link") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePathError(self.path / name, "not a regular file")
            data = _read_limited(fd, limit)
        finally:
            os.close(fd)
        if data is None:
            raise UnsafePathError(self.path / name, f"larger than {limit} bytes")
        return data

    def replace(self, src: str, dst: str) -> None:
        _check_name(src)
        _check_name(dst)
        os.replace(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def unlink(self, name: str) -> bool:
        _check_name(name)
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            return False
        return True

    def fsync(self) -> None:
        with contextlib.suppress(OSError):
            os.fsync(self.fd)

    def free_bytes(self) -> int:
        st = os.fstatvfs(self.fd)
        return st.f_bavail * st.f_frsize


def _finish_file(fd: int, mode: int) -> None:
    os.fsync(fd)
    if running_as_root():
        os.fchown(fd, 0, 0)
    os.fchmod(fd, mode)


class _Transaction:
    """Undo steps for a failed install; backups of replaced files are dropped on commit."""

    def __init__(self) -> None:
        self._undo: list[tuple[str, Callable[[], object]]] = []
        self._on_commit: list[tuple[str, Callable[[], object]]] = []

    def on_rollback(self, what: str, fn: Callable[[], object]) -> None:
        self._undo.append((what, fn))

    def staged(self, directory: SafeDir, tmp: str) -> None:
        self.on_rollback(f"remove {directory.path / tmp}", lambda: directory.unlink(tmp))

    def place(self, directory: SafeDir, tmp: str, name: str) -> None:
        """Move the staged file ``tmp`` to ``name``, keeping a backup of what was there."""
        st = directory.lstat(name)
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                raise InstallError(
                    _("A folder is in the way where the app should be installed: {path}")
                    .format(path=directory.path / name))
            backup = f".easyinstaller-old-{secrets.token_hex(8)}"
            os.rename(name, backup, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
            self.on_rollback(f"restore {directory.path / name}",
                             lambda: directory.replace(backup, name))
            self._on_commit.append((f"drop backup of {directory.path / name}",
                                    lambda: directory.unlink(backup)))
        directory.replace(tmp, name)
        self.on_rollback(f"remove {directory.path / name}", lambda: directory.unlink(name))
        directory.fsync()

    def rollback(self) -> None:
        for what, fn in reversed(self._undo):
            try:
                fn()
            except Exception:  # keep undoing the other steps
                log.exception("rollback step failed: %s", what)
        self._undo.clear()
        self._on_commit.clear()

    def commit(self) -> None:
        for what, fn in self._on_commit:
            try:
                fn()
            except Exception:
                log.warning("cleanup step failed: %s", what, exc_info=True)
        self._undo.clear()
        self._on_commit.clear()


# -- external commands ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None      # None: could not be started or timed out
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def find_tool(name: str) -> str | None:
    """Absolute path of ``name`` on the fixed safe PATH (the caller's PATH is never used)."""
    return shutil.which(name, path=SAFE_PATH)


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_TAIL else "[...]\n" + text[-OUTPUT_TAIL:]


def run_command(cmd: Sequence[str], *, timeout: float = TOOL_TIMEOUT,
                env: Mapping[str, str] | None = None) -> CommandResult:
    """Run an absolute command with a minimal environment; never raises for tool failures."""
    full_env = {**COMMAND_ENV, **(env or {})}
    log.info("running %s", " ".join(cmd))
    try:
        proc = subprocess.run(list(cmd), stdin=subprocess.DEVNULL, capture_output=True,
                              env=full_env, cwd="/", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(None, f"timed out after {timeout} s\n{_decode(exc.stderr)}")
    except OSError as exc:
        return CommandResult(None, f"could not start {cmd[0]}: {exc}")
    output = _decode(proc.stdout) + _decode(proc.stderr)
    return CommandResult(proc.returncode, _tail(output.strip()))


def _decode(data: bytes | str | None) -> str:
    if isinstance(data, str):
        return data
    return (data or b"").decode("utf-8", errors="replace")


def refresh_caches(layout: Layout, run_commands: bool, *, mime: bool = False) -> None:
    """update-desktop-database, the icon cache and (``mime``) the MIME database; only logged."""
    if not run_commands:
        return
    try:
        mime_tool = find_tool("update-mime-database") if mime else None
        if mime_tool and (layout.mime_dir / "packages").is_dir():
            result = run_command([mime_tool, str(layout.mime_dir)])
            if not result.ok:
                log.warning("update-mime-database failed: %s", result.output)
        udd = find_tool("update-desktop-database")
        if udd and layout.desktop_dir.is_dir():
            result = run_command([udd, str(layout.desktop_dir)])
            if not result.ok:
                log.warning("update-desktop-database failed: %s", result.output)
        icon_tool = find_tool("gtk-update-icon-cache") or find_tool("gtk4-update-icon-cache")
        if icon_tool and layout.icons_dir.is_dir():
            result = run_command([icon_tool, "-f", "-t", str(layout.icons_dir)])
            if not result.ok:
                log.warning("%s failed: %s", icon_tool, result.output)
        # Running desktops rescan an icon theme only when its folder's mtime changes.
        if layout.icons_dir.is_dir():
            os.utime(layout.icons_dir)
    except Exception:  # caches are a nicety; never fail an operation because of them
        log.warning("refreshing desktop caches failed", exc_info=True)


# -- trusted uninstall launcher ---------------------------------------------------------------------


def is_trusted_executable(path: str, *, name: str) -> bool:
    """``path`` is a root-owned, world-executable file called ``name`` in root-owned folders.

    The launcher ends up in a desktop file used by every user, so it and every folder above it
    must be owned by a trusted uid, not writable by others and reachable by everyone.
    """
    if not isinstance(path, str) or not _is_clean_abs_path(path) or os.path.basename(path) != name:
        return False
    current = Path("/")
    try:
        for part in [None, *Path(path).parts[1:-1]]:
            if part is not None:
                current = current / part
            st = os.lstat(current)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid not in TRUSTED_UIDS:
                return False
            if st.st_mode & stat.S_IWOTH or not st.st_mode & stat.S_IXOTH:
                return False
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_uid in TRUSTED_UIDS
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and bool(st.st_mode & stat.S_IXOTH))


def trusted_uninstall_command(value: Any, app_id: str) -> tuple[str, ...] | None:
    """The uninstall command if it is exactly ``[<trusted easy-installer>, "--uninstall", id]``."""
    if value is None:
        return None
    if (isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value)
            and value[1] == UNINSTALL_FLAG and value[2] == app_id
            and is_trusted_executable(value[0], name=LAUNCHER_NAME)):
        return tuple(value)
    log.warning("dropping untrusted uninstall_command %s", _short(value, 300))
    return None


# -- AppArmor profiles --------------------------------------------------------------------------------


def profile_attachment(text: str) -> str | None:
    """The attachment path of a profile written by :func:`render_apparmor_profile`."""
    match = _PROFILE_ATTACH_RE.search(text)
    if not match:
        return None
    return re.sub(r"\\(.)", r"\1", match.group(1))


def _is_within(path: str, directory: str) -> bool:
    directory = directory.rstrip("/")
    return bool(directory) and path.startswith(directory + "/")


def _dir_variants(directory: Path) -> set[str]:
    return {str(directory), os.path.realpath(directory)}


def _in_apps_dir(layout: Layout, path: str) -> bool:
    return os.path.dirname(path) in _dir_variants(layout.apps_dir)


def _read_profile(directory: SafeDir, name: str) -> str | None:
    try:
        data = directory.read_regular(name, MAX_PROFILE_SIZE)
    except UnsafePathError as exc:
        raise ProfileConflictError(exc.details or str(exc)) from exc
    return None if data is None else data.decode("utf-8", errors="replace")


def _load_profile(path: Path) -> None:
    parser = find_tool("apparmor_parser")
    if parser is None:
        raise InstallError(
            _("This computer cannot give apps this special permission, because AppArmor is "
              "not available."), details="apparmor_parser not found on " + SAFE_PATH)
    result = run_command([parser, "-r", str(path)])
    if not result.ok:
        raise InstallError(_("The special permission for the app could not be set up."),
                           details=f"apparmor_parser -r {path} exited with {result.returncode}:\n"
                                   f"{result.output}")


def _unload_profile(path: Path) -> None:
    parser = find_tool("apparmor_parser")
    if parser is None:
        return
    result = run_command([parser, "-R", str(path)])
    if not result.ok:
        log.warning("apparmor_parser -R %s failed: %s", path, result.output)


@dataclass
class ProfileChange:
    """A written profile and what was there before (for rollback)."""

    layout: Layout
    name: str
    previous: str | None
    loaded: bool = False

    @property
    def path(self) -> Path:
        return self.layout.apparmor_dir / self.name

    def revert(self, run_commands: bool) -> None:
        with SafeDir.open(self.layout.apparmor_dir) as directory:
            if self.previous is None:
                if run_commands and self.loaded:
                    _unload_profile(self.path)
                directory.unlink(self.name)
            else:
                directory.write_file(self.name, self.previous.encode("utf-8"), FILE_MODE)
                if run_commands and self.loaded:
                    _load_profile(self.path)
            directory.fsync()


def install_profile(layout: Layout, app_id: str, attach_path: str, *,
                    may_replace: Callable[[str], bool], run_commands: bool) -> ProfileChange:
    """Write ``apparmor_dir/easyinstaller-<id>`` and load it; on failure nothing is left behind."""
    try:
        text = render_apparmor_profile(app_id, attach_path)
    except ValueError as exc:
        raise RequestError(f"cannot render AppArmor profile: {exc}") from exc
    name = apparmor_profile_name(app_id)
    with SafeDir.open(layout.apparmor_dir, create=True) as directory:
        previous = _read_profile(directory, name)
        if previous is not None:
            if not previous.startswith(PROFILE_MARKER):
                raise ProfileConflictError(f"{directory.path / name} is not managed by Easy Installer")
            attached = profile_attachment(previous)
            if attached is not None and attached != attach_path and not may_replace(attached):
                raise ProfileConflictError(f"{directory.path / name} is used for {attached!r}")
        change = ProfileChange(layout, name, previous)
        directory.write_file(name, text.encode("utf-8"), FILE_MODE)
        if run_commands:
            try:
                _load_profile(change.path)
            except BaseException:
                # The kernel still has the old state; put the old file back (or remove ours).
                if previous is None:
                    directory.unlink(name)
                else:
                    directory.write_file(name, previous.encode("utf-8"), FILE_MODE)
                directory.fsync()
                raise
            change.loaded = True
    log.info("AppArmor profile %s written for %s", change.path, attach_path)
    return change


def _remove_profile_file(layout: Layout, app_id: str, *, allow: Callable[[str | None], bool],
                         run_commands: bool) -> tuple[bool, str]:
    """Unload and delete our profile for ``app_id`` if ``allow(attachment)``; (removed, reason)."""
    name = apparmor_profile_name(app_id)
    path = layout.apparmor_dir / name
    try:
        directory = SafeDir.open(layout.apparmor_dir)
    except FileNotFoundError:
        return False, "missing"
    with directory:
        try:
            text = _read_profile(directory, name)
        except ProfileConflictError as exc:
            log.warning("not removing %s: %s", path, exc.details)
            return False, "not a regular file"
        if text is None:
            return False, "missing"
        if not text.startswith(PROFILE_MARKER):
            log.warning("not removing %s: not managed by Easy Installer", path)
            return False, "not managed by Easy Installer"
        attached = profile_attachment(text)
        if not allow(attached):
            log.warning("not removing %s: it is used for %r", path, attached)
            return False, "used by another installation"
        if run_commands:
            _unload_profile(path)
        directory.unlink(name)
        directory.fsync()
    log.info("AppArmor profile %s removed", path)
    return True, "removed"


# -- registry and recorded paths ----------------------------------------------------------------------


def open_registry(layout: Layout, *, create: bool) -> Registry | None:
    """The system registry after checking its folder and file; None if it does not exist."""
    try:
        directory = SafeDir.open(layout.registry_path.parent, create=create)
    except FileNotFoundError:
        return None
    with directory:
        for name in (layout.registry_path.name, layout.registry_path.name + ".lock"):
            st = directory.lstat(name)
            if st is not None and not stat.S_ISREG(st.st_mode):
                raise UnsafePathError(directory.path / name, "not a regular file")
    return Registry(layout.registry_path)


def recorded_path_allowed(layout: Layout, path: Any, kind: str, app_id: str) -> bool:
    """Is ``path`` (from the registry) a file Easy Installer could have created for ``app_id``?"""
    if not isinstance(path, str) or not _is_clean_abs_path(path) or not ID_RE.fullmatch(app_id):
        return False
    p = Path(path)
    if kind == "appimage":
        return p.parent == layout.apps_dir and bool(_APPIMAGE_NAME_RE.fullmatch(p.name))
    if kind == "desktop":
        return p.parent == layout.desktop_dir and p.name == desktop_file_name(app_id)
    if kind == "profile":
        return p.parent == layout.apparmor_dir and p.name == apparmor_profile_name(app_id)
    if kind == "mime":
        return p.parent == layout.mime_dir / "packages" and p.name == mime_package_name(app_id)
    if kind == "backup":
        # only this app's own backup folder - never another app's, never anything outside
        return (p.parent == layout.backups_dir / app_id
                and bool(_APPIMAGE_NAME_RE.fullmatch(p.name)))
    if kind == "icon":
        try:
            parts = p.relative_to(layout.icons_dir).parts
        except ValueError:
            return False
        names = {f"{icon_name(app_id)}.{ext}" for ext in _ICON_EXTENSIONS}
        return (len(parts) == 3 and bool(_ICON_SIZE_DIR_RE.fullmatch(parts[0]))
                and parts[1] == "apps" and parts[2] in names)
    return False


def delete_recorded(layout: Layout, path: str, kind: str, app_id: str) -> str:
    """Delete a file recorded in the registry. Returns "removed", "missing" or "skipped"."""
    if not recorded_path_allowed(layout, path, kind, app_id):
        log.warning("not deleting unexpected %s path %r of %s", kind, path, app_id)
        return "skipped"
    target = Path(path)
    try:
        directory = SafeDir.open(target.parent)
    except FileNotFoundError:
        return "missing"
    except UnsafePathError as exc:
        log.warning("not deleting %s: %s", path, exc.details)
        return "skipped"
    with directory:
        st = directory.lstat(target.name)
        if st is None:
            return "missing"
        if stat.S_ISDIR(st.st_mode):
            log.warning("not deleting %s: it is a folder", path)
            return "skipped"
        directory.unlink(target.name)
        directory.fsync()
    log.info("removed %s", path)
    return "removed"


def _os_error(exc: OSError) -> InstallError:
    details = f"{type(exc).__name__}: {exc}"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return InstallError(
            _("There is not enough free disk space. Please free up some space and try again."),
            details=details)
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return InstallError(_("Easy Installer is not allowed to change the files it needs to."),
                            details=details)
    return InstallError(
        _("The app could not be installed because a file could not be copied or saved."),
        details=details)


# -- the kept previous version ("backup") ---------------------------------------------------------------


def _remove_empty_backup_dirs(layout: Layout, app_id: str) -> None:
    """Remove ``backups/<id>`` and the backups folder itself once they are empty."""
    try:
        with SafeDir.open(layout.backups_dir) as root:
            with contextlib.suppress(OSError):
                os.rmdir(app_id, dir_fd=root.fd)
        with SafeDir.open(layout.apps_dir) as apps:
            with contextlib.suppress(OSError):
                os.rmdir(BACKUPS_DIR_NAME, dir_fd=apps.fd)
    except (OSError, UnsafePathError):
        pass


def delete_backup(layout: Layout, app: InstalledApp, path: Any) -> str:
    """Delete a recorded backup of ``app``: "removed", "missing" or "skipped" (not its own)."""
    try:
        outcome = delete_recorded(layout, path, "backup", app.id)
    except OSError as exc:
        log.warning("could not delete the kept version %r of %s: %s", path, app.id, exc)
        return "skipped"
    if ID_RE.fullmatch(app.id):
        _remove_empty_backup_dirs(layout, app.id)
    return outcome


def _recorded_backup(layout: Layout, app: InstalledApp | None) -> dict | None:
    """``app.previous`` if it names a file in this app's own backup folder that is there."""
    previous = app.previous if app is not None else None
    if not previous or not recorded_path_allowed(layout, previous.get("path"), "backup", app.id):
        return None
    return previous if os.path.lexists(previous["path"]) else None


def _check_consumed_backup(layout: Layout, req: InstallRequest,
                           existing: InstalledApp | None) -> str | None:
    """``consume_backup``: the source must be exactly this app's recorded previous version."""
    if not req.consume_backup:
        return None
    previous = existing.previous if existing is not None else None
    path = previous.get("path") if previous else None
    if not path or req.source_appimage != path \
            or not recorded_path_allowed(layout, path, "backup", req.app_id):
        raise RequestError(
            f"consume_backup: {_short(req.source_appimage)} is not the kept previous version "
            f"recorded for {req.app_id}")
    return path


def _shares_app_file(registry: Registry, app: InstalledApp) -> bool:
    return any(other.id != app.id and other.appimage_path == app.appimage_path
               for other in registry.load().values())


def _free_backup_name(directory: SafeDir, wanted: str) -> str:
    if directory.lstat(wanted) is None:
        return wanted
    stem = wanted.removesuffix(".AppImage")
    for n in range(2, _MAX_NAME_TRIES):
        name = f"{stem}-{n}.AppImage"
        if directory.lstat(name) is None:
            return name
    raise InstallError(_("The previous version could not be kept, so you cannot go back to it."),
                       details=f"no free name for {directory.path / wanted}")


def keep_previous_version(layout: Layout, existing: InstalledApp, apps_dir: SafeDir,
                          stack: contextlib.ExitStack, tx: _Transaction) -> dict:
    """Give the installed app file a second name in the app's backup folder (a hard link, or a
    rename where links are not possible): it is never copied and stays on its file system.

    With the link the old version stays at its recorded path until the new one replaces it.
    Returns the registry's ``previous`` record. Raises InstallError/OSError; nothing changed then.
    """
    old = Path(existing.appimage_path)
    if not recorded_path_allowed(layout, existing.appimage_path, "appimage", existing.id):
        raise UnsafePathError(existing.appimage_path, "not an app file of this installation")
    st = apps_dir.lstat(old.name)
    if st is None or not stat.S_ISREG(st.st_mode):
        raise InstallError(_("The previous version could not be kept, so you cannot go back to it."),
                           details=f"{old} is not a regular file")
    if st.st_size != existing.size or (existing.mtime_ns and st.st_mtime_ns != existing.mtime_ns):
        # Not the version the entry names (it was changed in place, or an update was killed
        # before its registry entry was saved): never kept under that version's name.
        raise InstallError(_("The previous version could not be kept, so you cannot go back to it."),
                           details=f"{old} is not the file recorded for {existing.id}")
    # Undone last: the folders made for the backup go again if nothing is kept after all.
    tx.on_rollback(f"remove empty backup folders of {existing.id}",
                   lambda: _remove_empty_backup_dirs(layout, existing.id))
    directory = stack.enter_context(SafeDir.open(layout.backups_dir / existing.id, create=True))
    name = _free_backup_name(
        directory, backup_file_name(existing.name, existing.version, existing.sha256))
    if not _APPIMAGE_NAME_RE.fullmatch(name):
        raise UnsafePathError(directory.path / name, "unexpected backup name")
    try:
        os.link(old.name, name, src_dir_fd=apps_dir.fd, dst_dir_fd=directory.fd,
                follow_symlinks=False)
        tx.on_rollback(f"remove {directory.path / name}", lambda: directory.unlink(name))
    except OSError as exc:
        log.info("cannot hard-link %s into %s (%s); moving it instead", old, directory.path, exc)
        os.rename(old.name, name, src_dir_fd=apps_dir.fd, dst_dir_fd=directory.fd)
        tx.on_rollback(f"move {directory.path / name} back to {old}",
                       lambda: os.replace(name, old.name, src_dir_fd=directory.fd,
                                          dst_dir_fd=apps_dir.fd))
    directory.fsync()
    log.info("kept version %s of %s as %s", existing.version, existing.id, directory.path / name)
    return make_previous(version=existing.version, path=directory.path / name,
                         sha256=existing.sha256, size=st.st_size,
                         original_filename=existing.original_filename)


def _saved_time(previous: dict) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(previous.get("saved_at")),
                                                    SAVED_AT_FORMAT)))
    except (ValueError, OverflowError):
        return None


def remove_unrecorded_backups(layout: Layout, registry: Registry, app_id: str, *,
                              cutoff: float | None = None) -> list[str]:
    """Delete the files in ``backups/<app_id>/`` that no registry entry records - what an
    update that was killed after keeping the old version (before the registry was saved)
    left behind; nobody else would ever remove it. Only regular files with the names the
    helper gives (``<Name>-<version>.AppImage``), and with ``cutoff`` only those put there
    before it (ctime). Call with the registry locked. Returns the paths deleted."""
    if not ID_RE.fullmatch(app_id):
        return []
    recorded = {os.path.normpath(str(app.previous.get("path")))
                for app in registry.load().values()
                if app.previous and app.previous.get("path")}
    removed: list[str] = []
    try:
        directory = SafeDir.open(layout.backups_dir / app_id)
    except (FileNotFoundError, NotADirectoryError):
        return removed
    except (OSError, UnsafePathError) as exc:
        log.warning("not looking at the kept versions of %s: %s", app_id, exc)
        return removed
    with directory:
        for name in sorted(os.listdir(directory.fd)):
            path = directory.path / name
            if not _APPIMAGE_NAME_RE.fullmatch(name) or os.path.normpath(path) in recorded:
                continue
            st = directory.lstat(name)
            if st is None or not stat.S_ISREG(st.st_mode) \
                    or (cutoff is not None and st.st_ctime > cutoff):
                continue
            if directory.unlink(name):
                removed.append(str(path))
                log.info("removed %s: no registry entry records it", path)
        directory.fsync()
    _remove_empty_backup_dirs(layout, app_id)
    return removed


def prune_backups(layout: Layout, registry: Registry, max_age_days: int) -> list[str]:
    """Delete the recorded backups older than ``max_age_days`` (call with the registry locked),
    and the files in the backup folders that no entry records and that are as old.

    Only files named by a registry entry's ``previous`` inside that app's own backup folder
    are deleted; a record whose date cannot be read is left alone. Returns the ids pruned.
    """
    cutoff = time.time() - max_age_days * SECONDS_PER_DAY
    pruned: list[str] = []
    try:
        with SafeDir.open(layout.backups_dir) as root:
            folders = [name for name in sorted(os.listdir(root.fd)) if ID_RE.fullmatch(name)]
    except (OSError, UnsafePathError):
        folders = []
    for app_id in folders:
        remove_unrecorded_backups(layout, registry, app_id, cutoff=cutoff)
    for app in registry.load().values():
        previous = app.previous
        if not previous:
            continue
        saved = _saved_time(previous)
        if saved is None or saved > cutoff:
            continue
        try:
            delete_backup(layout, app, previous.get("path"))
            registry.put(dataclasses.replace(app, previous=None))
        except OSError as exc:
            log.warning("could not prune the kept version of %s: %s", app.id, exc)
            continue
        pruned.append(app.id)
    return pruned


# -- install ------------------------------------------------------------------------------------------


def _changed_error(source: str, details: str) -> InstallError:
    return InstallError(
        _("The file {name} changed while it was being installed. Please try again.")
        .format(name=os.path.basename(source)), details=details)


def _check_appimage_source(fd: int, st: os.stat_result, req: InstallRequest) -> None:
    if st.st_size > MAX_APPIMAGE_SIZE:
        raise InstallError(_("This file is too large to be installed."),
                           details=f"{st.st_size} bytes")
    if req.size is not None and st.st_size != req.size:
        raise _changed_error(req.source_appimage, f"size {req.size} -> {st.st_size}")
    if os.pread(fd, len(ELF_MAGIC), 0) != ELF_MAGIC:
        raise InstallError(_("This file is not an AppImage."),
                           details=f"{req.source_appimage}: no ELF header")


def _copy_appimage(src_fd: int, expected_size: int, directory: SafeDir,
                   req: InstallRequest) -> tuple[str, str, int]:
    """Copy into a new temp file (0600 until complete, then 0755); returns (tmp, sha256, size)."""
    if directory.free_bytes() < expected_size + FREE_SPACE_MARGIN:
        raise InstallError(
            _("There is not enough free disk space. Please free up some space and try again."),
            details=f"{directory.path}: {directory.free_bytes()} bytes free, "
                    f"{expected_size} needed")
    fd, tmp = directory.create_temp()
    try:
        digest = hashlib.sha256()
        total = 0
        os.lseek(src_fd, 0, os.SEEK_SET)
        while chunk := os.read(src_fd, CHUNK_SIZE):
            total += len(chunk)
            if total > expected_size:
                raise _changed_error(req.source_appimage, "the file grew while it was copied")
            digest.update(chunk)
            _write_all(fd, chunk)
        _finish_file(fd, EXEC_MODE)
    except BaseException:
        os.close(fd)
        directory.unlink(tmp)
        raise
    os.close(fd)
    return tmp, digest.hexdigest(), total


def _resolve_target(layout: Layout, req: InstallRequest, registry: Registry) -> Path:
    target = appimage_target(layout, req.app_id, req.name, registry.owner_of)
    base, n = target, 2
    # Never replace a file that belongs to something else, even if the suffixed name is taken.
    while os.path.lexists(target) and registry.owner_of(target) != req.app_id:
        target = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
        if n > 1000:
            raise InstallError(_("The app could not be installed because a file could not be saved."),
                               details=f"no free name for {base}")
    return target


def render_desktop(req: InstallRequest, *, target: Path, icon: str | None,
                   extra_args: tuple[str, ...],
                   uninstall_command: tuple[str, ...] | None) -> str:
    """Render the launcher from the embedded entry and helper-computed values, then verify it."""
    embedded = DesktopEntry.parse(req.embedded_desktop) if req.embedded_desktop is not None else None
    spec = DesktopRenderSpec(
        app_id=req.app_id, name=req.name, appimage_path=target, icon_name=icon,
        embedded=embedded, embedded_stem=req.embedded_stem, comment=req.comment,
        version=req.version, scope=Scope.SYSTEM, extra_args=extra_args,
        extract_and_run=req.extract_and_run, uninstall_command=uninstall_command,
    )
    try:
        text = render_desktop_entry(spec)
    except ValueError as exc:
        raise RequestError(f"the app's desktop entry cannot be used: {exc}") from exc
    verify_desktop(text, app_id=req.app_id, target=target, uninstall_command=uninstall_command)
    return text


def _check_exec(value: str, expected: str) -> str | None:
    """Why the Exec value does not simply run ``expected`` (None if it does)."""
    if exec_program(value) != expected:
        return "program"
    assignments = exec_env(value)
    if assignments is None or not all(exec_env_allowed(k, v) for k, v in assignments):
        return "environment"
    return None


def verify_desktop(text: str, *, app_id: str, target: Path,
                   uninstall_command: tuple[str, ...] | None) -> None:
    """Defence in depth: every command in the rendered launcher runs the installed AppImage.

    Only harmless ``env`` assignments are allowed (never LD_PRELOAD & co.), there are no
    localized Exec/TryExec/Icon keys (which some desktops prefer) and no control characters
    (other parsers read some as a line break, which could smuggle in a second Exec line).
    """
    entry = DesktopEntry.parse(text)
    problems: list[str] = []
    main = DesktopEntry.MAIN
    if INVALID_CHARS_RE.search(text):
        problems.append("control or line break characters")
    if entry.groups()[:1] != [main]:
        problems.append("[Desktop Entry] is not the first group")
    if entry.get("TryExec") != str(target):
        problems.append("TryExec")
    reason = _check_exec(entry.get("Exec") or "", str(target))
    if reason:
        problems.append(f"Exec ({reason})")
    if entry.get("X-EasyInstaller-Id") != app_id:
        problems.append("X-EasyInstaller-Id")
    uninstall_group = ACTION_GROUP_PREFIX + UNINSTALL_ACTION
    for group in entry.groups():
        for key in entry.keys(group):
            if key.startswith(("Exec[", "TryExec[", "Icon[")):
                problems.append(f"{key} in [{group}]")
        if group.startswith(ACTION_GROUP_PREFIX):
            expected = str(target)
            if group == uninstall_group and uninstall_command:
                expected = uninstall_command[0]
            reason = _check_exec(entry.get("Exec", group) or "", expected)
            if reason:
                problems.append(f"Exec of [{group}] ({reason})")
        elif group != main and not group.startswith("X-"):
            problems.append(f"unexpected group [{group}]")
    if problems:
        raise HelperError(_("The administrator task failed unexpectedly."),
                          details="rendered launcher failed verification: " + ", ".join(problems))


def _sandbox_fix(profile: ProfileChange | None, extra_args: Sequence[str]) -> str:
    if profile is not None:
        return SandboxFix.APPARMOR.value
    if NO_SANDBOX_ARG in extra_args:
        return SandboxFix.NO_SANDBOX.value
    return SandboxFix.NONE.value


def _remove_previous(layout: Layout, old: InstalledApp, new: InstalledApp, run_commands: bool,
                     registry: Registry | None = None) -> None:
    keep = {new.appimage_path, new.desktop_path, *new.icon_paths, new.mime_package}
    if registry is not None and _shares_app_file(registry, old):
        keep.add(old.appimage_path)   # another entry starts this file, too: it stays
    candidates = [("appimage", old.appimage_path), ("desktop", old.desktop_path),
                  ("mime", old.mime_package)]
    candidates += [("icon", p) for p in old.icon_paths]
    for kind, path in candidates:
        if not path or path in keep:
            continue
        try:
            delete_recorded(layout, path, kind, new.id)
        except OSError as exc:
            log.warning("could not remove old file %s: %s", path, exc)
    if old.apparmor_profile and not new.apparmor_profile:
        _remove_system_profile(layout, old, run_commands)


def _remove_system_profile(layout: Layout, app: InstalledApp, run_commands: bool) -> bool:
    if not recorded_path_allowed(layout, app.apparmor_profile, "profile", app.id):
        log.warning("not removing unexpected profile path %r of %s", app.apparmor_profile, app.id)
        return False

    def belongs_to_system_app(attached: str | None) -> bool:
        return attached is None or attached == app.appimage_path or _in_apps_dir(layout, attached)

    try:
        removed, _reason = _remove_profile_file(layout, app.id, allow=belongs_to_system_app,
                                                run_commands=run_commands)
    except OSError as exc:
        log.warning("could not remove AppArmor profile of %s: %s", app.id, exc)
        return False
    return removed


def op_install(m: dict, *, layout: Layout, caller_uid: int, caller_gid: int,
               run_commands: bool = True) -> dict:
    """Install (or update) an AppImage for all users. Result: ``{"ok": True, "app": {...}}``."""
    req = InstallRequest.from_manifest(m)
    uninstall_command = trusted_uninstall_command(req.uninstall_command, req.app_id)
    try:
        return _install(req, layout=layout, caller_uid=caller_uid, caller_gid=caller_gid,
                        uninstall_command=uninstall_command, run_commands=run_commands)
    except OSError as exc:
        raise _os_error(exc) from exc


def _install(req: InstallRequest, *, layout: Layout, caller_uid: int, caller_gid: int,
             uninstall_command: tuple[str, ...] | None, run_commands: bool) -> dict:
    warnings: list[str] = []
    with contextlib.ExitStack() as stack:
        src_fd, src_st = open_source(req.source_appimage, caller_uid, caller_gid)
        stack.callback(os.close, src_fd)
        _check_appimage_source(src_fd, src_st, req)
        icon_data, icon_info = (read_icon(req.icon_source, caller_uid, caller_gid)
                                if req.icon_source else (None, None))

        registry = open_registry(layout, create=True) or Registry(layout.registry_path)
        # Held until the end: no other installation may change the files or the registry in
        # between (and nothing is staged while waiting for it).
        stack.enter_context(registry.locked(timeout=REGISTRY_LOCK_TIMEOUT))
        existing = registry.get(req.app_id)
        consumed = _check_consumed_backup(layout, req, existing)
        apps_dir = stack.enter_context(SafeDir.open(layout.apps_dir, create=True))
        desktop_dir = stack.enter_context(SafeDir.open(layout.desktop_dir, create=True))
        target = _resolve_target(layout, req, registry)
        desktop_path = layout.desktop_dir / desktop_file_name(req.app_id)
        icon_path = icon_target(layout, req.app_id, icon_info) if icon_info is not None else None
        icon_dir = (stack.enter_context(SafeDir.open(icon_path.parent, create=True))
                    if icon_path is not None else None)
        # The app's own file types: only those its launcher opens and this computer lacks.
        embedded = DesktopEntry.parse(req.embedded_desktop) if req.embedded_desktop else None
        mime_types = select_mime_types(
            req.mime_types, embedded.get_list("MimeType") if embedded is not None else (),
            host_mime_database()) if req.mime_types else []
        mime_path = (layout.mime_dir / "packages" / mime_package_name(req.app_id)
                     if mime_types else None)
        mime_dir = (stack.enter_context(SafeDir.open(mime_path.parent, create=True))
                    if mime_path is not None else None)

        tx = _Transaction()
        profile: ProfileChange | None = None
        try:
            tmp, sha, size = _copy_appimage(src_fd, src_st.st_size, apps_dir, req)
            tx.staged(apps_dir, tmp)
            if sha != req.sha256:
                raise _changed_error(req.source_appimage, f"sha256 {req.sha256} -> {sha}")
            if req.size is not None and size != req.size:
                raise _changed_error(req.source_appimage, f"size {req.size} -> {size}")
            icon_tmp = None
            if icon_dir is not None and icon_data is not None:
                icon_tmp = icon_dir.write_temp(icon_data, FILE_MODE)
                tx.staged(icon_dir, icon_tmp)

            previous = _previous_for(layout, req, existing, registry, apps_dir, stack, tx,
                                     new_sha=sha, consumed=consumed, warnings=warnings)
            tx.place(apps_dir, tmp, target.name)
            placed = apps_dir.lstat(target.name)
            if icon_dir is not None and icon_tmp is not None and icon_path is not None:
                tx.place(icon_dir, icon_tmp, icon_path.name)

            extra_args = req.extra_args
            if req.apparmor:
                profile, extra_args = _system_profile(layout, req, target, extra_args,
                                                      run_commands, warnings)
                if profile is not None:
                    change = profile
                    tx.on_rollback(f"revert AppArmor profile {change.path}",
                                   lambda: change.revert(run_commands))

            desktop_text = render_desktop(
                req, target=target, icon=icon_name(req.app_id) if icon_path else None,
                extra_args=extra_args, uninstall_command=uninstall_command)
            desktop_tmp = desktop_dir.write_temp(desktop_text.encode("utf-8"), FILE_MODE)
            tx.staged(desktop_dir, desktop_tmp)
            tx.place(desktop_dir, desktop_tmp, desktop_path.name)
            if mime_dir is not None and mime_path is not None:
                mime_tmp = mime_dir.write_temp(
                    render_mime_package(req.app_id, mime_types).encode("utf-8"), FILE_MODE)
                tx.staged(mime_dir, mime_tmp)
                tx.place(mime_dir, mime_tmp, mime_path.name)

            now = utc_now()
            app = InstalledApp(
                id=req.app_id,
                name=req.name,
                version=req.version,
                scope=Scope.SYSTEM,
                appimage_path=str(target),
                desktop_path=str(desktop_path),
                icon_paths=[str(icon_path)] if icon_path is not None else [],
                icon_name=icon_name(req.app_id) if icon_path is not None else FALLBACK_ICON,
                apparmor_profile=str(profile.path) if profile is not None else None,
                sandbox_fix=_sandbox_fix(profile, extra_args),
                extract_and_run=req.extract_and_run,
                extract_and_run_explicit=req.extract_and_run_explicit,
                sha256=sha,
                size=size,
                arch=req.arch,
                update_info=req.update_info,
                original_filename=req.original_filename or (
                    None if req.consume_backup else os.path.basename(req.source_appimage)),
                comment=req.comment,
                installed_at=existing.installed_at if existing and existing.installed_at else now,
                updated_at=now,
                installer_version=__version__,
                mime_package=str(mime_path) if mime_path is not None else None,
                mtime_ns=placed.st_mtime_ns if placed is not None else 0,
                base_id=req.base_id,
                pinned=req.pinned,
                previous=previous,
                update_source=req.update_source,
                origin_url=req.origin_url,
                signature=req.signature,
                data_hints=list(req.data_hints),
            )
            registry.put(app)
        except BaseException:
            tx.rollback()
            raise
        on_committed()
        tx.commit()
        if existing is not None:
            _remove_previous(layout, existing, app, run_commands, registry)
            _drop_obsolete_backup(layout, existing, app)
        if req.backup_max_age_days is not None:
            try:
                prune_backups(layout, registry, req.backup_max_age_days)
            except Exception:  # housekeeping: the installation itself is done
                log.warning("pruning old backups failed", exc_info=True)

    refresh_caches(layout, run_commands, mime=bool(
        app.mime_package or (existing is not None and existing.mime_package)))
    log.info("installed %s %s to %s", app.id, app.version, app.appimage_path)
    return {"ok": True, "app": app.to_dict(),
            "action": "update" if existing is not None else "install", "warnings": warnings}


def _previous_for(layout: Layout, req: InstallRequest, existing: InstalledApp | None,
                  registry: Registry, apps_dir: SafeDir, stack: contextlib.ExitStack,
                  tx: _Transaction, *, new_sha: str, consumed: str | None,
                  warnings: list[str]) -> dict | None:
    """The new entry's ``previous``; keeps the replaced version when asked to.

    * The same file again (same sha256, or the source is the installed file itself - "Repair"
      of an app file that changed in place): nothing is replaced, the recorded backup stays.
    * A different file with ``keep_backup``: the installed file is kept (link/rename). If that
      fails, an update goes on with a warning and the older backup (if any) stays - what
      cannot be replaced by a newer backup is not thrown away; going back (``consume_backup``)
      swaps the two versions, so there it is an error and nothing is changed.
    * A different file without ``keep_backup``: no ``previous``; the old backup is deleted
      after the commit (backups are not wanted).
    """
    if existing is None:
        return None
    same_file = (existing.sha256 and existing.sha256 == new_sha) \
        or req.source_appimage == existing.appimage_path
    if same_file and consumed is None:
        return _recorded_backup(layout, existing)
    if not req.keep_backup:
        return None
    try:
        if _shares_app_file(registry, existing):
            raise InstallError(
                _("The previous version could not be kept, so you cannot go back to it."),
                details=f"{existing.appimage_path} is used by another installed app")
        return keep_previous_version(layout, existing, apps_dir, stack, tx)
    except (InstallError, OSError) as exc:
        details = exc.details if isinstance(exc, InstallError) else f"{type(exc).__name__}: {exc}"
        if consumed is not None:
            raise InstallError(_("The current version could not be kept, so nothing was changed."),
                               details=details) from exc
        _remove_empty_backup_dirs(layout, existing.id)
        log.warning("cannot keep the previous version of %s: %s", existing.id, details)
        warnings.append(_("The previous version could not be kept, so you cannot go back to it."))
        return _recorded_backup(layout, existing)


def _drop_obsolete_backup(layout: Layout, old: InstalledApp, new: InstalledApp) -> None:
    """One backup per app: the older one goes once it is no longer the entry's ``previous``
    (replaced by a newer backup, used up by going back, or its version was replaced)."""
    path = old.previous.get("path") if old.previous else None
    if not path or (new.previous and new.previous.get("path") == path):
        return
    delete_backup(layout, new, path)


def _system_profile(layout: Layout, req: InstallRequest, target: Path, extra_args: tuple[str, ...],
                    run_commands: bool, warnings: list[str]
                    ) -> tuple[ProfileChange | None, tuple[str, ...]]:
    """Install the AppArmor profile for a system app; fall back to --no-sandbox on failure."""
    def replaceable(attached: str) -> bool:
        return _in_apps_dir(layout, attached)

    try:
        return install_profile(layout, req.app_id, str(target), may_replace=replaceable,
                               run_commands=run_commands), extra_args
    except (InstallError, OSError) as exc:
        details = exc.details if isinstance(exc, InstallError) else str(exc)
        log.warning("AppArmor profile for %s not installed (%s); using --no-sandbox",
                    req.app_id, details)
        warnings.append(_("The app could not get the special permission it needs, so it will be "
                          "started without its built-in security sandbox."))
        return None, tuple(dict.fromkeys((*extra_args, NO_SANDBOX_ARG)))


# -- uninstall ----------------------------------------------------------------------------------------


def op_uninstall(m: dict, *, layout: Layout, run_commands: bool = True) -> dict:
    """Remove a system-wide app: only files recorded in the system registry, inside the layout."""
    app_id = _app_id(_require_mapping(m))
    try:
        registry = open_registry(layout, create=False)
        if registry is None:
            raise NotInstalledError(_("This app is not installed for everyone on this computer."),
                                    details=f"{app_id} (system)")
        with registry.locked(timeout=REGISTRY_LOCK_TIMEOUT):
            removed, skipped = _uninstall_locked(layout, registry, app_id, run_commands)
    except OSError as exc:
        raise _os_error(exc) from exc
    refresh_caches(layout, run_commands, mime=any(p.endswith(".xml") for p in removed))
    log.info("uninstalled %s (removed %d files, skipped %d)", app_id, len(removed), len(skipped))
    return {"ok": True, "removed": removed, "skipped": skipped}


def _uninstall_locked(layout: Layout, registry: Registry, app_id: str,
                      run_commands: bool) -> tuple[list[str], list[str]]:
    app = registry.get(app_id)
    if app is None:
        raise NotInstalledError(_("This app is not installed for everyone on this computer."),
                                details=f"{app_id} (system)")
    removed: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    targets = [("appimage", app.appimage_path), ("desktop", app.desktop_path),
               ("mime", app.mime_package or "")]
    targets += [("icon", p) for p in app.icon_paths]
    if app.previous:
        targets.append(("backup", app.previous.get("path") or ""))  # its kept previous version
    on_committed()  # nothing can be undone once the first file is gone: finish it
    shared = _shares_app_file(registry, app)
    for kind, path in targets:
        if not path:
            continue
        if kind == "appimage" and shared:
            log.warning("keeping %s: another installed app uses it, too", path)
            skipped.append(path)
            continue
        try:
            outcome = delete_recorded(layout, path, kind, app_id)
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
            failures.append(f"{path}: {exc}")
            continue
        if outcome == "removed":
            removed.append(path)
        elif outcome == "skipped":
            skipped.append(path)
    if failures:
        raise InstallError(_("Some files of {name} could not be removed.").format(name=app.name),
                           details="\n".join(failures))
    if app.apparmor_profile:
        if _remove_system_profile(layout, app, run_commands):
            removed.append(app.apparmor_profile)
        else:
            skipped.append(app.apparmor_profile)
    registry.remove(app_id)
    # (and what an update that was killed kept without recording it)
    removed += remove_unrecorded_backups(layout, registry, app_id)
    _remove_empty_backup_dirs(layout, app_id)
    return removed, skipped


def op_drop_backup(m: dict, *, layout: Layout, run_commands: bool = True) -> dict:
    """Delete the kept previous version of a system-wide app and forget it in the registry.

    Only the file recorded as this app's ``previous`` is deleted, and only if it lies in the
    app's own backup folder; a record pointing anywhere else is just forgotten ("skipped").
    """
    app_id = _app_id(_require_mapping(m))
    not_installed = NotInstalledError(
        _("This app is not installed for everyone on this computer."), details=f"{app_id} (system)")
    removed: list[str] = []
    skipped: list[str] = []
    try:
        registry = open_registry(layout, create=False)
        if registry is None:
            raise not_installed
        with registry.locked(timeout=REGISTRY_LOCK_TIMEOUT):
            app = registry.get(app_id)
            if app is None:
                raise not_installed
            path = app.previous.get("path") if app.previous else None
            if path:
                outcome = delete_recorded(layout, path, "backup", app_id)
                (removed if outcome == "removed" else skipped if outcome == "skipped" else []
                 ).append(path)
                registry.put(dataclasses.replace(app, previous=None))
                on_committed()
            removed += remove_unrecorded_backups(layout, registry, app_id)
            _remove_empty_backup_dirs(layout, app_id)
    except OSError as exc:
        raise _os_error(exc) from exc
    log.info("dropped the kept previous version of %s (%s)", app_id, removed or skipped or "none")
    return {"ok": True, "removed": removed, "skipped": skipped}


# -- AppArmor for user-scope apps -------------------------------------------------------------------


def _check_apparmor_target(path: str, *, layout: Layout, caller_uid: int, caller_gid: int,
                           caller_home: Path) -> tuple[str, bool, str]:
    """Validate the AppImage to attach a profile to. Returns (real path, in_home, home real path)."""
    def probe() -> tuple[str, str, int]:
        real = os.path.realpath(path)
        home = os.path.realpath(caller_home)
        return real, home, _open_nofollow(path)

    try:
        real, home_real, fd = as_caller(caller_uid, caller_gid, probe)
    except OSError as exc:
        raise _source_error(exc, path) from exc
    try:
        st = os.fstat(fd)
        in_home = home_real != "/" and _is_within(real, home_real)
        in_apps = _in_apps_dir(layout, real)
        if not (in_home or in_apps):
            raise InstallError(
                _("The special permission can only be given to apps in your home folder."),
                details=f"{path} (resolved {real}) is outside {home_real} and {layout.apps_dir}")
        if not stat.S_ISREG(st.st_mode) or st.st_dev == _procfs_dev():
            raise InstallError(_("{name} is not a normal file.").format(name=os.path.basename(path)),
                               details=f"{path}: mode {stat.filemode(st.st_mode)}")
        owners = {caller_uid} if in_home else _local_owner_uids()
        if st.st_uid not in owners:
            raise InstallError(_("The app file {name} does not belong to you.")
                               .format(name=os.path.basename(path)),
                               details=f"{path}: owned by uid {st.st_uid}, expected {sorted(owners)}")
        if os.pread(fd, len(ELF_MAGIC), 0) != ELF_MAGIC:
            raise InstallError(_("This file is not an AppImage."), details=f"{path}: no ELF header")
    finally:
        os.close(fd)
    return real, in_home, home_real


def op_apparmor_install(m: dict, *, layout: Layout, caller_uid: int, caller_home: Path,
                        run_commands: bool = True, caller_gid: int | None = None) -> dict:
    """Let a user's Electron AppImage use user namespaces (Ubuntu 24.04+ restriction)."""
    m = _require_mapping(m)
    app_id = _app_id(m)
    path = _abs_path(m, "appimage_path")
    gid = primary_gid(caller_uid) if caller_gid is None else caller_gid
    try:
        real, in_home, home_real = _check_apparmor_target(
            path, layout=layout, caller_uid=caller_uid, caller_gid=gid, caller_home=caller_home)

        def replaceable(attached: str) -> bool:
            # Only a profile of the same person's copy (or the same system folder) may be replaced.
            return _is_within(attached, home_real) if in_home else _in_apps_dir(layout, attached)

        change = install_profile(layout, app_id, real, may_replace=replaceable,
                                 run_commands=run_commands)
    except OSError as exc:
        raise _os_error(exc) from exc
    on_committed()
    return {"ok": True, "profile_path": str(change.path), "profile_name": change.name,
            "appimage_path": real}


def op_apparmor_remove(m: dict, *, layout: Layout, run_commands: bool = True,
                       caller_home: Path | None = None) -> dict:
    """Remove ``easyinstaller-<id>`` unless it belongs to a system app or to another person."""
    app_id = _app_id(_require_mapping(m))
    registry = open_registry(layout, create=False)
    system_app = registry.get(app_id) if registry is not None else None
    home_variants = _dir_variants(caller_home) if caller_home is not None else set()

    def allow(attached: str | None) -> bool:
        if attached is None:
            return True
        if system_app is not None and system_app.apparmor_profile \
                and attached == system_app.appimage_path:
            return False
        if caller_home is not None:
            return any(home != "/" and _is_within(attached, home) for home in home_variants)
        return True

    try:
        removed, reason = _remove_profile_file(layout, app_id, allow=allow, run_commands=run_commands)
    except OSError as exc:
        raise _os_error(exc) from exc
    return {"ok": True, "removed": removed, "reason": reason,
            "profile_path": str(layout.apparmor_dir / apparmor_profile_name(app_id))}


# -- FUSE ---------------------------------------------------------------------------------------------


def op_install_fuse(m: dict, *, status: SystemStatus, run_commands: bool = True) -> dict:
    """``apt-get install -y <libfuse2|libfuse2t64>``; the package name never comes from the client."""
    _require_mapping(m)
    no_apt = _("The missing system component cannot be installed automatically on this computer.")
    if not status.has_apt:
        raise HelperError(no_apt, details="apt-get is not available")
    package = status.libfuse2_package
    if package not in FUSE_PACKAGES:
        raise HelperError(no_apt, details=f"unexpected package name {package!r}")
    if run_commands:
        apt_get = find_tool("apt-get")
        if apt_get is None:
            raise HelperError(no_apt, details="apt-get not found on " + SAFE_PATH)
        result = run_command(
            [apt_get, "install", "-y", "-q", "-o", f"DPkg::Lock::Timeout={APT_LOCK_TIMEOUT}", package],
            timeout=APT_TIMEOUT, env={"DEBIAN_FRONTEND": "noninteractive"})
        if not result.ok:
            raise InstallError(
                _("The system component could not be installed. Please check your internet "
                  "connection and try again."),
                details=f"apt-get install {package} exited with {result.returncode}:\n{result.output}")
    log.info("installed %s", package)
    return {"ok": True, "package": package}


__all__ = [
    "InstallRequest", "ProfileConflictError", "RequestError", "SafeDir", "UnsafePathError",
    "caller_privileges", "find_tool", "is_trusted_executable", "op_apparmor_install",
    "op_apparmor_remove", "op_drop_backup", "op_install", "op_install_fuse", "op_uninstall",
    "open_source", "run_command", "trusted_uninstall_command",
]
