"""Registry of installed apps: a small JSON file, written atomically under a file lock."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import InstallError
from ..i18n import _
from .paths import Scope

log = logging.getLogger(__name__)

REGISTRY_FORMAT = 1
FILE_MODE = 0o644
DIR_MODE = 0o755
#: Only the owner may open the lock file: flock() works on any open descriptor, so a readable
#: lock file would let every local user block the (system) registry.
LOCK_MODE = 0o600
LOCK_POLL_INTERVAL = 0.2

STATUS_OK = "ok"
STATUS_MISSING_APPIMAGE = "missing-appimage"
STATUS_MISSING_LAUNCHER = "missing-launcher"
#: The app file differs from what was recorded (e.g. the app updated itself). Never returned by
#: :meth:`InstalledApp.status`; reported by ``core.reconcile``.
STATUS_CHANGED = "changed"

KIND_APPIMAGE = "appimage"
KIND_PORTABLE = "portable"


def utc_now() -> str:
    """Current time as ISO-8601 UTC with second precision, e.g. "2026-09-29T15:04:05Z"."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class InstalledApp:
    id: str
    name: str
    version: str | None
    scope: Scope
    appimage_path: str
    desktop_path: str
    icon_paths: list[str] = field(default_factory=list)
    icon_name: str = ""
    apparmor_profile: str | None = None
    sandbox_fix: str = "none"
    extract_and_run: bool = False
    #: the start mode was chosen explicitly (``install --extract-and-run yes|no``), not from
    #: whether FUSE works: it is never switched automatically, and Repair keeps it
    extract_and_run_explicit: bool = False
    sha256: str | None = None
    size: int = 0
    arch: str = ""
    update_info: str | None = None
    original_filename: str = ""
    comment: str | None = None
    installed_at: str = ""
    updated_at: str = ""
    installer_version: str = __version__
    #: shared-mime-info package with the app's own file types (``.../mime/packages/*.xml``)
    mime_package: str | None = None
    # -- added in 0.2 (all optional: entries written by 0.1 simply lack them) -------------------
    #: "appimage" | "portable" (an unpacked archive; ``appimage_path`` is then its executable)
    kind: str = KIND_APPIMAGE
    #: portable only: the app's folder inside the apps folder
    install_dir: str | None = None
    #: ``st_mtime_ns`` of ``appimage_path`` when it was last recorded (0 = unknown)
    mtime_ns: int = 0
    #: a copy kept next to another version ("keep both"): the id of the main app
    base_id: str | None = None
    #: never offered updates (set for "keep both" copies)
    pinned: bool = False
    #: the replaced version, kept for a while:
    #: ``{"version": str|None, "path": str, "sha256": str|None, "size": int, "saved_at": iso}``
    previous: dict | None = None
    #: where updates come from (``UpdateSource.to_dict()``)
    update_source: dict | None = None
    #: where the file was downloaded from, if known
    origin_url: str | None = None
    #: ``SignatureInfo.to_dict()``; None = not signed
    signature: dict | None = None
    #: names used to find the app's settings and data folders
    data_hints: list[str] = field(default_factory=list)
    #: keys of the entry that this version does not know (written by a newer version): kept
    #: as they are, so that rewriting the registry never loses what a newer version recorded
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        unknown = data.pop("extra")
        data["scope"] = Scope(self.scope).value
        data["icon_paths"] = [str(p) for p in self.icon_paths]
        return {**{k: v for k, v in unknown.items() if k not in data}, **data}

    @property
    def exec_path(self) -> str:
        """What the launcher starts: the AppImage, or a portable app's executable."""
        return self.appimage_path

    @property
    def main_id(self) -> str:
        """The id of the app this entry is a version of (its own id unless it is a kept copy)."""
        return self.base_id or self.id

    @classmethod
    def from_dict(cls, d: dict) -> InstalledApp:
        """Build from a dict; unknown keys are kept as they are (``extra``), missing optional
        keys get defaults.

        Raises ValueError if the mandatory data (id, name, paths, scope) is unusable.
        """
        if not isinstance(d, dict):
            raise ValueError("app entry is not an object")
        app_id = d.get("id")
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("app entry without id")
        try:
            scope = Scope(d.get("scope") or Scope.USER.value)
        except ValueError as exc:
            raise ValueError(f"invalid scope for {app_id!r}: {d.get('scope')!r}") from exc
        icon_paths = d.get("icon_paths") or []
        if not isinstance(icon_paths, list):
            icon_paths = [icon_paths]
        return cls(
            id=app_id,
            name=_str(d.get("name")) or app_id,
            version=_opt_str(d.get("version")),
            scope=scope,
            appimage_path=_str(d.get("appimage_path")),
            desktop_path=_str(d.get("desktop_path")),
            icon_paths=[str(p) for p in icon_paths if p],
            icon_name=_str(d.get("icon_name")),
            apparmor_profile=_opt_str(d.get("apparmor_profile")),
            sandbox_fix=_str(d.get("sandbox_fix")) or "none",
            extract_and_run=bool(d.get("extract_and_run", False)),
            extract_and_run_explicit=bool(d.get("extract_and_run_explicit", False)),
            sha256=_opt_str(d.get("sha256")),
            size=_int(d.get("size")),
            arch=_str(d.get("arch")),
            update_info=_opt_str(d.get("update_info")),
            original_filename=_str(d.get("original_filename")),
            comment=_opt_str(d.get("comment")),
            installed_at=_str(d.get("installed_at")),
            updated_at=_str(d.get("updated_at")) or _str(d.get("installed_at")),
            installer_version=_str(d.get("installer_version")) or __version__,
            mime_package=_opt_str(d.get("mime_package")),
            kind=_kind(d.get("kind")),
            install_dir=_opt_str(d.get("install_dir")),
            mtime_ns=max(_int(d.get("mtime_ns")), 0),
            base_id=_opt_str(d.get("base_id")),
            pinned=d.get("pinned") is True,
            previous=_previous(d.get("previous")),
            update_source=_opt_dict(d.get("update_source")),
            origin_url=_opt_str(d.get("origin_url")),
            signature=_opt_dict(d.get("signature")),
            data_hints=_str_list(d.get("data_hints")),
            extra={key: value for key, value in d.items()
                   if isinstance(key, str) and key not in _FIELDS},
        )

    def status(self) -> str:
        if not self.appimage_path or not os.path.isfile(self.appimage_path):
            return STATUS_MISSING_APPIMAGE
        if not self.desktop_path or not os.path.isfile(self.desktop_path):
            return STATUS_MISSING_LAUNCHER
        return STATUS_OK

    @property
    def icon_path(self) -> str | None:
        for path in self.icon_paths:
            if path and os.path.isfile(path):
                return path
        return None


_FIELDS = frozenset(f.name for f in fields(InstalledApp))


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = _str(value)
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON's "Infinity"
        return 0


def _kind(value: Any) -> str:
    return value if isinstance(value, str) and value else KIND_APPIMAGE


def _opt_dict(value: Any) -> dict | None:
    """A JSON object (copied, so the entry never shares it with the caller) or None."""
    return copy.deepcopy(value) if isinstance(value, dict) and value else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _previous(value: Any) -> dict | None:
    """The record of the kept previous version; unusable data (no path) counts as "none"."""
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if not isinstance(path, str) or not path:
        return None
    record = {
        "version": _opt_str(value.get("version")),
        "path": path,
        "sha256": _opt_str(value.get("sha256")),
        "size": max(_int(value.get("size")), 0),
        "saved_at": _str(value.get("saved_at")),
    }
    original = _opt_str(value.get("original_filename"))
    if original:
        record["original_filename"] = original
    return record


def make_previous(*, version: str | None, path: str | os.PathLike, sha256: str | None, size: int,
                  saved_at: str | None = None, original_filename: str | None = None) -> dict:
    """The ``InstalledApp.previous`` record for a version that was just moved aside
    (``original_filename``: the name of the file it was installed from, if known)."""
    record = {"version": version or None, "path": os.fspath(path), "sha256": sha256 or None,
              "size": max(int(size), 0), "saved_at": saved_at or utc_now()}
    if original_filename:
        record["original_filename"] = original_filename
    return record


def _same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    if os.path.normpath(os.fspath(a)) == os.path.normpath(os.fspath(b)):
        return True
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_fd: int | None = None

    # -- reading -------------------------------------------------------------------------------

    def load(self) -> dict[str, InstalledApp]:
        return self._load(strict=False)

    def _load(self, *, strict: bool) -> dict[str, InstalledApp]:
        """``strict``: raise read errors instead of returning {} (writers must not lose data)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if strict:
                raise
            log.warning("cannot read registry %s: %s", self.path, exc)
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("apps", {}), dict):
                raise ValueError("unexpected registry structure")
        except (UnicodeDecodeError, ValueError) as exc:
            self._quarantine(exc)
            return {}
        fmt = data.get("format", REGISTRY_FORMAT)
        if fmt != REGISTRY_FORMAT:
            log.warning("registry %s has format %r, expected %d", self.path, fmt, REGISTRY_FORMAT)
        apps: dict[str, InstalledApp] = {}
        for key, entry in data.get("apps", {}).items():
            if isinstance(entry, dict) and "id" not in entry:
                entry = {**entry, "id": key}
            try:
                app = InstalledApp.from_dict(entry)
            except ValueError as exc:
                log.warning("skipping invalid registry entry %r: %s", key, exc)
                continue
            apps[app.id] = app
        return apps

    def all(self) -> list[InstalledApp]:
        return sorted(self.load().values(), key=lambda a: (a.name.casefold(), a.id))

    def get(self, app_id: str) -> InstalledApp | None:
        return self.load().get(app_id)

    def owner_of(self, appimage_path: Path) -> str | None:
        for app in self.load().values():
            if app.appimage_path and _same_path(app.appimage_path, appimage_path):
                return app.id
        return None

    # -- writing -------------------------------------------------------------------------------

    def put(self, app: InstalledApp) -> None:
        with self.locked():
            apps = self._load(strict=True)
            apps[app.id] = app
            self._write(apps)

    def remove(self, app_id: str) -> None:
        with self.locked():
            apps = self._load(strict=True)
            if apps.pop(app_id, None) is None:
                return
            self._write(apps)

    @contextlib.contextmanager
    def locked(self, *, timeout: float | None = None,
               on_wait: Callable[[], None] | None = None) -> Iterator[None]:
        """Hold the registry's lock, e.g. for a whole installation (re-entrant for this object).

        ``on_wait`` is called once if another installation holds the lock; after ``timeout``
        seconds an InstallError ("another installation is running") is raised.
        """
        if self._lock_fd is not None:
            yield
            return
        self._ensure_dir()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, LOCK_MODE)
        try:
            _restrict_lock_file(fd)
            self._acquire(fd, timeout, on_wait)
            self._lock_fd = fd
            try:
                yield
            finally:
                self._lock_fd = None
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- internals -----------------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

    def _acquire(self, fd: int, timeout: float | None, on_wait: Callable[[], None] | None) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        log.info("waiting for the lock %s", self.lock_path)
        if on_wait is not None:
            on_wait()
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(LOCK_POLL_INTERVAL)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InstallError(
                        _("Another installation is running right now. Please try again in a "
                          "moment."),
                        details=f"{self.lock_path} stayed locked for {timeout:g} s") from None

    def _write(self, apps: dict[str, InstalledApp]) -> None:
        self._ensure_dir()
        payload = {
            "format": REGISTRY_FORMAT,
            "apps": {app_id: apps[app_id].to_dict() for app_id in sorted(apps)},
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            # e.g. a path that is not valid UTF-8 (surrogate escapes): \u escapes round-trip
            text = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
                os.fchmod(fh.fileno(), FILE_MODE)
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        _fsync_dir(self.path.parent)

    def _quarantine(self, exc: Exception) -> None:
        n = 1
        while True:
            target = self.path.with_name(f"{self.path.name}.corrupt-{n}")
            if not target.exists():
                break
            n += 1
        try:
            os.rename(self.path, target)
        except OSError as rename_exc:
            log.warning("registry %s is corrupt (%s) and could not be moved aside: %s",
                        self.path, exc, rename_exc)
            return
        log.warning("registry %s is corrupt (%s); moved it to %s and starting fresh",
                    self.path, exc, target)


def _restrict_lock_file(fd: int) -> None:
    """Lock files made by older versions were readable by everyone; tighten them."""
    try:
        st = os.fstat(fd)
        if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) != LOCK_MODE:
            os.fchmod(fd, LOCK_MODE)
    except OSError as exc:
        log.debug("cannot restrict the lock file: %s", exc)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
