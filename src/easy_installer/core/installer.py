"""Install, update and uninstall AppImages (user scope directly, system scope via the helper)
and portable apps from archives (user scope only)."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .. import __version__
from ..errors import (
    AuthorizationError,
    EasyInstallerError,
    HelperError,
    InstallError,
    NotInstalledError,
    UnsupportedArchitectureError,
)
from ..i18n import _
# What the helper accepts. Imported at start, never on first use: a program that keeps
# running while a newer version of Easy Installer is installed must not load new files later.
from ..helper import ops as helper_rules
from . import appdata, appfolder, backups, downloads, privileged, variants
from .appdata import DataLocation
from .desktop_entry import DesktopEntry, exec_program, rewrite_exec, split_exec
from .elf import host_arch, is_foreign_arch, read_elf_info
from .imageinfo import extension_for, probe_image
from .inspector import AppImageInfo, arch_label, inspect_appimage
from .integration import (
    ACTION_GROUP_PREFIX,
    ID_RE,
    UNINSTALL_ACTION,
    DesktopRenderSpec,
    MimeTypeDef,
    apparmor_profile_name,
    appimage_target,
    compare_versions,
    desktop_file_name,
    host_mime_database,
    icon_target,
    mime_package_name,
    render_desktop_entry,
    render_mime_package,
    safe_file_stem,
    select_mime_types,
    variant_id,
    variant_name,
    version_label,
    with_name_suffix,
)
from .integration import icon_name as make_icon_name
from .paths import (  # backups_dir: re-exported, it is part of this module's interface
    BACKUPS_DIR_NAME,
    Layout,
    Scope,
    backups_dir,
    cache_home,
    home_dir,
    layout_for,
)
from .origin import clean_url
from .portable import (
    MARKER_NAME,
    ExecutableCandidate,
    PortableInfo,
    adopt_work_dir,
    desktop_exec_matches,
    extract_portable,
)
from .registry import KIND_APPIMAGE, KIND_PORTABLE, STATUS_OK, InstalledApp, Registry, utc_now
from .sandbox import (
    NO_SANDBOX_ARG,
    SandboxFix,
    default_sandbox_fix,
    needs_sandbox_fix,
    render_apparmor_profile,
)
from .settings import load_settings
from .signature import STATUS_INVALID, STATUS_VALID, SignatureInfo
from .squashfs import remove_tree
from .system_checks import SystemStatus, get_system_status
from .updates import UpdateCache

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float | None, str], None]

CHUNK_SIZE = 4 * 1024 * 1024
MAX_ICON_SIZE = 10 * 1024 * 1024
DIR_MODE = 0o755
EXEC_MODE = 0o755
FILE_MODE = 0o644
FALLBACK_ICON = "application-x-executable"
LAUNCHER_NAME = "easy-installer"
TOOL_TIMEOUT = 60
#: How long an installation waits for another one (e.g. at its password prompt) to finish.
USER_LOCK_TIMEOUT = 600
#: Work done in the background (picking up an app that updated itself) only waits this long.
UNATTENDED_LOCK_TIMEOUT = 5

_PERMISSION_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS)

ACTION_INSTALL = "install"
ACTION_UPDATE = "update"
ACTION_REINSTALL = "reinstall"
ACTION_DOWNGRADE = "downgrade"
#: Going back to the kept previous version (only planned by :func:`plan_rollback`).
ACTION_ROLLBACK = "rollback"

#: What :func:`portable_dir_state` says about the folder of a portable app.
PORTABLE_OK = "ok"              # it is there and verifiably the app's own folder
PORTABLE_MISSING = "missing"    # nothing is there (any more)
PORTABLE_UNSAFE = "unsafe"      # something is there that must not be touched
#: Format of the marker file ``<install_dir>/.easy-installer.json``.
MARKER_FORMAT = 1
MAX_MARKER_SIZE = appfolder.MAX_MARKER_SIZE
#: The app's icon, kept in its folder so that a kept version can get its launcher back.
MARKER_ICON_STEM = appfolder.ICON_STEM
_MARKER_ICON_EXTENSIONS = ("png", "svg", "xpm")
_MAX_MARKER_DESKTOP = 256 * 1024

#: The helper operation that deletes the kept previous version of a system-wide app.
DROP_BACKUP_OP = "drop-backup"

#: "Look it up" marker for the uninstall command of a plan.
_LOOK_UP = object()


@dataclass
class InstallOptions:
    scope: Scope = Scope.USER
    #: True: the file stays where it is and a copy is installed; False: it is moved (an archive:
    #: deleted once it is unpacked); None = the usual for the kind of file: an AppImage is
    #: moved, an archive is kept (it is not the app itself). Always True/False in a plan.
    keep_original: bool | None = None
    sandbox_fix: SandboxFix | None = None
    extract_and_run: bool | None = None
    add_uninstall_action: bool = True
    allow_foreign_arch: bool = False
    #: install next to the installed version (as a separate, pinned app) instead of replacing it
    keep_both: bool = False
    #: keep the replaced version for a while; None = as set in the settings (backup_days > 0)
    keep_backup: bool | None = None


@dataclass
class InstallPlan:
    #: what is installed: an :class:`AppImageInfo`, or for ``kind == "portable"`` the
    #: :class:`PortableInfo` (also available, typed, as ``portable``)
    info: AppImageInfo
    options: InstallOptions
    layout: Layout
    app_id: str
    name: str
    target_appimage: Path
    desktop_path: Path
    icon_name: str
    icon_target: Path | None
    desktop_text: str
    action: str
    existing: InstalledApp | None
    existing_other_scope: InstalledApp | None
    sandbox_fix: SandboxFix
    extract_and_run: bool
    uninstall_command: tuple[str, ...] | None
    apparmor_profile_text: str | None
    requires_root: bool
    in_place: bool
    warnings: list[str] = field(default_factory=list)
    #: The version that will be recorded: info.version, or the installed one when the very same
    #: file is installed again and its name no longer tells the version (e.g. "FreeCAD.AppImage").
    version: str | None = None
    #: The file is the registered app file of another installation (e.g. the copy installed "only
    #: for me" when installing for everyone): it is never moved or deleted, a copy is installed.
    source_is_installed: bool = False
    #: file types the app defines that this computer does not know yet (registered on install)
    mime_types: list[MimeTypeDef] = field(default_factory=list)
    #: "appimage" | "portable"
    kind: str = KIND_APPIMAGE
    #: This file could also be installed next to the installed version ("keep both"): the app
    #: is installed in this scope and the file is not one of its installed files.
    keep_both_available: bool = False
    #: where the replaced version will be kept (None: nothing is replaced, or it is not kept)
    backup_target: Path | None = None
    #: the replaced version is wanted as a backup (resolved ``options.keep_backup``)
    keep_backup: bool = False
    #: a kept copy ("keep both"): the id of the main app, else None
    base_id: str | None = None
    pinned: bool = False
    #: what a kept copy's names end in (its version), e.g. "1.0.2"
    name_suffix: str | None = None
    #: the installation with the app's own id in this scope (what "keep both" keeps)
    main_installed: InstalledApp | None = None
    #: rollback: the file is ``existing.previous`` and is used up by the installation
    consumes_backup: bool = False
    #: made in the background (an app updated itself): never asks for the administrator
    #: password and does not wait long for another installation
    unattended: bool = False
    #: the installed version carries a valid signature and this file is unsigned, invalid or
    #: signed by someone else (``warnings`` starts with a note about it)
    signer_changed: bool = False
    #: portable apps: the inspected archive (``info`` is the same object) ...
    portable: PortableInfo | None = None
    #: ... the folder the app is unpacked to (``target_appimage`` is its program in there) ...
    install_dir: Path | None = None
    #: ... and, when going back or repairing, the complete app folder that is used as it is
    #: instead of unpacking an archive
    source_dir: Path | None = None

    @property
    def scope(self) -> Scope:
        return self.layout.scope

    @property
    def mime_package(self) -> Path | None:
        if not self.mime_types:
            return None
        return self.layout.mime_dir / "packages" / mime_package_name(self.app_id)


# ------------------------------------------------------------------------------------------------
# small helpers
# ------------------------------------------------------------------------------------------------


def _noop_progress(fraction: float | None, message: str) -> None:
    pass


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def display_text(text: str) -> str:
    """``text`` (e.g. a path) printable anywhere: bytes of a file name that are not UTF-8 (e.g.
    from an old Windows zip) arrive as lone surrogates, which GTK, JSON and terminals refuse;
    they become U+FFFD."""
    try:
        data = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "surrogatepass")
    return data.decode("utf-8", "replace")


def display_name(path: Path) -> str:
    """The file name as text (see :func:`display_text`)."""
    return display_text(path.name)


def _is_utf8_path(path: Path) -> bool:
    try:
        str(path).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _same_file_path(a: Path | str, b: Path | str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _is_within(path: Path | str, root: Path | str, *, resolve: bool = True) -> bool:
    norm = os.path.realpath if resolve else os.path.abspath
    try:
        Path(norm(path)).relative_to(norm(root))
    except ValueError:
        return False
    return True


def _registry(layout: Layout) -> Registry:
    return Registry(layout.registry_path)


def _other_scope(scope: Scope) -> Scope:
    return Scope.SYSTEM if scope is Scope.USER else Scope.USER


def _os_error(exc: OSError) -> InstallError:
    """Translate a low-level file error into a friendly InstallError."""
    details = f"{type(exc).__name__}: {exc}"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return InstallError(
            _("There is not enough free disk space. Please free up some space and try again."),
            details=details,
        )
    if exc.errno in _PERMISSION_ERRNOS:
        where = exc.filename or ""
        if where:
            message = _("Easy Installer is not allowed to change this location: {path}").format(
                path=where)
        else:
            message = _("Easy Installer is not allowed to change the files it needs to.")
        return InstallError(message, details=details)
    return InstallError(
        _("The app could not be installed because a file could not be copied or saved."),
        details=details,
    )


# ------------------------------------------------------------------------------------------------
# uninstall launcher discovery
# ------------------------------------------------------------------------------------------------


def _launcher_candidates() -> list[Path]:
    candidates: list[Path] = []
    found = shutil.which(LAUNCHER_NAME)
    if found:
        candidates.append(Path(found))
    candidates += [
        Path("/usr/bin") / LAUNCHER_NAME,
        Path("/usr/local/bin") / LAUNCHER_NAME,
        home_dir() / ".local" / "bin" / LAUNCHER_NAME,
    ]
    return candidates


def _transient_roots() -> list[Path]:
    """Places where an `easy-installer` executable is not a real installation."""
    roots = [Path(tempfile.gettempdir())]
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        roots.append(Path(sys.prefix))  # virtualenv (e.g. the development .venv)
    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "easy_installer").is_dir():
        roots.append(checkout)
    return roots


def find_uninstall_launcher(scope: Scope = Scope.USER) -> str | None:
    """The installed ``easy-installer`` for a launcher's "Uninstall…" action (None if there is none).

    A system-wide launcher is used by every user, so for ``Scope.SYSTEM`` only a launcher the
    helper trusts (root-owned, in root-owned folders) qualifies, and /usr/bin and /usr/local/bin
    are preferred over a PATH hit such as ~/.local/bin (which the helper would drop).
    """
    transient = _transient_roots()
    candidates = _launcher_candidates()
    if Scope(scope) is Scope.SYSTEM:
        preferred = (Path("/usr/bin"), Path("/usr/local/bin"))
        candidates = sorted(candidates, key=lambda c: c.parent not in preferred)  # stable
        candidates = [c for c in candidates      # (the helper's own check)
                      if helper_rules.is_trusted_executable(str(c), name=LAUNCHER_NAME)]
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
        except OSError:
            continue
        if any(_is_within(candidate, root, resolve=False) or _is_within(candidate, root)
               for root in transient):
            log.debug("ignoring launcher %s (source checkout, venv or temp dir)", candidate)
            continue
        return str(candidate)
    return None


# ------------------------------------------------------------------------------------------------
# planning
# ------------------------------------------------------------------------------------------------


def _resolve_action(version: str | None, existing: InstalledApp | None, same_file: bool) -> str:
    """``same_file``: the file is the installed one (same path or sha256)."""
    if existing is None:
        return ACTION_INSTALL
    if version is None or existing.version is None:
        # Without both versions nothing is known about which one is newer: never call it a
        # downgrade; a different file (another build) simply replaces the installed one.
        return ACTION_REINSTALL if version == existing.version and same_file else ACTION_UPDATE
    cmp = compare_versions(version, existing.version)
    if cmp > 0:
        return ACTION_UPDATE
    if cmp < 0:
        return ACTION_DOWNGRADE
    return ACTION_REINSTALL


def _is_same_file(info: AppImageInfo, app: InstalledApp | None) -> bool:
    """``info`` is the very file ``app`` was installed from (or its installed copy). The same
    path with other content (the app replaced its own file) is another file."""
    if app is None:
        return False
    known = bool(info.sha256 and app.sha256)
    if app.appimage_path and _same_file_path(info.path, app.appimage_path):
        return not known or info.sha256 == app.sha256
    return known and info.sha256 == app.sha256


def _source_owner(info: AppImageInfo, app_id: str, scope: Scope,
                  registries: Iterable[Registry]) -> InstalledApp | None:
    """The other installation whose registered app file is the file being installed."""
    for registry in registries:
        owner = registry.owner_of(info.path)
        if owner is None:
            continue
        app = registry.get(owner)
        if app is not None and not (app.scope is scope and app.id == app_id):
            return app
    return None


def _resolve_target(layout: Layout, info: AppImageInfo, app_id: str, name: str,
                    registry: Registry, *, source_may_stay: bool = True) -> Path:
    """``source_may_stay``: the file being installed may become the target itself (in place)."""
    source = info.path if source_may_stay else None
    target = appimage_target(layout, app_id, name, registry.owner_of, source=source)
    # Never overwrite a file that belongs to someone else, even if the suffixed name is taken too.
    n = 2
    base = target
    while (target.exists() or target.is_symlink()) \
            and not (source is not None and _same_file_path(target, source)) \
            and registry.owner_of(target) != app_id:
        target = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    return target


def _apparmor_note() -> str:
    return _("This app needs a special permission to start on this computer. You will be asked "
             "for your password once to allow it.")


def _resolve_sandbox(info: AppImageInfo, options: InstallOptions, status: SystemStatus,
                     warnings: list[str]) -> SandboxFix:
    if not needs_sandbox_fix(info, status):
        return SandboxFix.NONE
    fix = SandboxFix(options.sandbox_fix) if options.sandbox_fix is not None \
        else default_sandbox_fix(status)
    if fix is SandboxFix.APPARMOR and default_sandbox_fix(status) is not SandboxFix.APPARMOR:
        warnings.append(_(
            "This computer cannot give the app the special permission it needs, so it will be "
            "started without its built-in security sandbox."))
        return SandboxFix.NO_SANDBOX
    if fix is SandboxFix.APPARMOR:
        warnings.append(_apparmor_note())
    elif fix is SandboxFix.NO_SANDBOX:
        warnings.append(_(
            "This app will be started without its built-in security sandbox, because this "
            "computer blocks it otherwise."))
    else:
        warnings.append(_(
            "This app may not start, because this computer blocks the security sandbox it needs."))
    return fix


def _apparmor_profile_current(layout: Layout, app_id: str, profile_text: str) -> bool:
    path = layout.apparmor_dir / apparmor_profile_name(app_id)
    try:
        return path.read_text(encoding="utf-8") == profile_text
    except (OSError, UnicodeDecodeError):
        return False


def _embedded_stem(info: AppImageInfo) -> str | None:
    if not info.desktop_filename:
        return None
    return info.desktop_filename.removesuffix(".desktop") or None


def _embedded_entry(info: AppImageInfo, name_suffix: str | None) -> DesktopEntry | None:
    """The app's own desktop entry; for a kept copy with the version added to every name."""
    if not name_suffix:
        return info.desktop_entry
    return with_name_suffix(info.desktop_entry, name_suffix)


def _render_desktop(info: AppImageInfo, *, app_id: str, name: str, target: Path,
                    icon: str | None, scope: Scope, extract_and_run: bool,
                    uninstall_command: tuple[str, ...] | None, sandbox_fix: SandboxFix,
                    version: str | None, name_suffix: str | None = None) -> str:
    spec = DesktopRenderSpec(
        app_id=app_id,
        name=name,
        appimage_path=target,
        icon_name=icon,
        embedded=_embedded_entry(info, name_suffix),
        embedded_stem=_embedded_stem(info),
        comment=info.comment,
        version=version,
        scope=scope,
        extra_args=(NO_SANDBOX_ARG,) if sandbox_fix is SandboxFix.NO_SANDBOX else (),
        extract_and_run=extract_and_run,
        uninstall_command=uninstall_command,
    )
    return render_desktop_entry(spec)


def render_plan_desktop(plan: InstallPlan, sandbox_fix: SandboxFix | None = None) -> str:
    """Desktop entry text for ``plan``, optionally with a different sandbox fix."""
    return _render_desktop(
        plan.info, app_id=plan.app_id, name=plan.name, target=plan.target_appimage,
        icon=plan.icon_name if plan.icon_target is not None else None, scope=plan.layout.scope,
        extract_and_run=plan.extract_and_run, uninstall_command=plan.uninstall_command,
        sandbox_fix=plan.sandbox_fix if sandbox_fix is None else sandbox_fix, version=plan.version,
        name_suffix=plan.name_suffix,
    )


def options_from_app(app: InstalledApp, *, keep_original: bool = False,
                     keep_backup: bool | None = None) -> InstallOptions:
    """Options that keep the choices made when ``app`` was installed (update, rollback, repair).

    The scope, the sandbox fix and an explicitly chosen start mode are taken over; everything
    that was decided automatically is decided again.
    """
    sandbox = SandboxFix(app.sandbox_fix) if app.sandbox_fix in (
        SandboxFix.APPARMOR.value, SandboxFix.NO_SANDBOX.value) else None
    return InstallOptions(
        scope=Scope(app.scope), keep_original=keep_original, sandbox_fix=sandbox,
        extract_and_run=app.extract_and_run if app.extract_and_run_explicit else None,
        keep_backup=keep_backup)


def installed_uninstall_command(app: InstalledApp) -> tuple[str, ...] | None | object:
    """The "Uninstall…" command in the app's current launcher: the command, None if the
    launcher has no such action, or ``_LOOK_UP`` if the launcher cannot be read."""
    try:
        entry = DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return _LOOK_UP
    value = entry.get("Exec", ACTION_GROUP_PREFIX + UNINSTALL_ACTION)
    if not value:
        return None
    try:
        command = tuple(split_exec(value))
    except ValueError:
        return _LOOK_UP
    if len(command) == 3 and os.path.isabs(command[0]) and command[1] == "--uninstall" \
            and command[2] == app.id:
        return command
    return _LOOK_UP


def _keep_backup_wanted(options: InstallOptions) -> bool:
    if options.keep_backup is not None:
        return bool(options.keep_backup)
    return load_settings().backup_days > 0


def _planned_backup(layout: Layout, registry: Registry, existing: InstalledApp | None,
                    info: AppImageInfo, *, keep_backup: bool, in_place: bool) -> Path | None:
    """Where the version that ``info`` replaces will be kept, or None if nothing is kept:
    nothing is replaced, the very same file is installed again, or the old file is gone."""
    if existing is None or not keep_backup or in_place or existing.kind != KIND_APPIMAGE \
            or _is_same_file(info, existing) or not ID_RE.fullmatch(existing.id):
        return None
    old = existing.appimage_path
    if not _safe_to_delete(old, kind="appimage", app=existing):
        return None
    try:
        st = os.lstat(old)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or not backups.matches_entry(existing, st):
        return None   # (a file that changed itself is not the version the entry names)
    if os.path.realpath(old) in _used_by_others(registry, existing.id):
        return None   # another entry starts this file, too: it stays where it is
    return backups.planned_backup_path(layout, existing)


def _changed_itself(app: InstalledApp | None) -> bool:
    """The app file of ``app`` is there but no longer the one its entry records: the app
    replaced it itself (a self-updater) and nobody has looked at it since (``core.reconcile``)."""
    if app is None or app.kind != KIND_APPIMAGE or not app.appimage_path:
        return False
    try:
        st = os.lstat(app.appimage_path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not backups.matches_entry(app, st)


def _signature_of(value: object) -> SignatureInfo | None:
    """A signature as the inspector reports it or as the registry stores it."""
    if isinstance(value, SignatureInfo):
        return value
    to_dict = getattr(value, "to_dict", None)
    return SignatureInfo.from_dict(to_dict() if callable(to_dict) else value)


def signer_changed(installed: InstalledApp | None, info: object) -> bool:
    """Signature continuity (DESIGN section 25): the installed version carries a valid
    signature, and the file ``info`` describes is unsigned, not valid or signed with another key.

    The key travels inside the file, so a signer's *name* proves nothing; what counts is that
    the fingerprint stays the same from one version to the next. False when the installed app
    is not (validly) signed, or when nothing is known about the file's signature.
    """
    recorded = _signature_of(installed.signature) if installed is not None else None
    if recorded is None or recorded.status != STATUS_VALID or not recorded.fingerprint:
        return False
    if not hasattr(info, "signature"):
        return False
    sha = getattr(info, "sha256", None)
    if sha and installed.sha256 and sha == installed.sha256:
        return False   # the very same file: nothing changed (even if gpg is gone meanwhile)
    new = _signature_of(getattr(info, "signature"))
    return new is None or new.status != STATUS_VALID or new.fingerprint != recorded.fingerprint


def _signer_changed_note(installed: InstalledApp) -> str:
    return _("Careful: the installed version of {name} was signed by its maker, but this file "
             "is not signed by the same maker. It may come from someone else. Only go on if "
             "you trust where you got it.").format(name=installed.name)


def invalid_signature_note(info: object) -> str | None:
    """A file whose own signature does not fit its contents was changed or is damaged."""
    found = _signature_of(getattr(info, "signature", None))
    if found is None or found.status != STATUS_INVALID:
        return None
    return _("The signature of this file does not match: it was changed after its maker "
             "signed it, or it is damaged. Better download it again from the maker's website.")


def _kind_conflict(installed: InstalledApp) -> InstallError:
    """An AppImage and an unpacked archive of the same app cannot replace each other."""
    return InstallError(
        _("{name} is already installed from a different kind of file. Please uninstall it "
          "first, then install this file.").format(name=installed.name),
        details=f"{installed.id} is installed as {installed.kind!r}")


def plan_install(info: AppImageInfo | PortableInfo, options: InstallOptions | None = None, *,
                 status: SystemStatus | None = None) -> InstallPlan:
    """What installing ``info`` would do: an inspected AppImage, or (per-user only) an inspected
    portable archive (``plan.kind == "portable"``)."""
    if isinstance(info, PortableInfo):
        return _make_portable_plan(info, options, status=status)
    return _make_plan(info, options, status=status)


def plan_update(info: AppImageInfo, app: InstalledApp, *, keep_backup: bool | None = None,
                version_hint: str | None = None,
                status: SystemStatus | None = None) -> InstallPlan:
    """The plan that replaces the installed ``app`` by the file ``info`` (a downloaded update).

    Unlike :func:`plan_install` the file is not matched against the registry: it replaces
    exactly this entry, with the choices made when the app was installed (scope, sandbox fix,
    start mode). The file is moved into place; the replaced version is kept as the settings
    say (``keep_backup``: None). ``version_hint``: the version the update source announced,
    used if the file itself does not tell its version. An app that was installed although it
    is made for another kind of computer may be updated by a file for that same kind.
    """
    options = options_from_app(app, keep_original=False, keep_backup=keep_backup)
    options.allow_foreign_arch = bool(app.arch) and info.arch == app.arch
    return _make_plan(info, options, status=status, entry=app, version_hint=version_hint)


def _make_plan(info: AppImageInfo, options: InstallOptions | None, *,
               status: SystemStatus | None, entry: InstalledApp | None = None,
               stay_at: Path | None = None, action: str | None = None,
               version_hint: str | None = None, unattended: bool = False,
               uninstall_command: object = _LOOK_UP) -> InstallPlan:
    """:func:`plan_install`, plus what rollback and reconcile need.

    ``entry``: plan for exactly this installed entry (instead of matching the file against
    the registry); ``stay_at``: the file stays where it is - at this path, as the registry
    shall record it - and becomes the entry's app file;
    ``action``: replaces the install/update/downgrade wording; ``version_hint``: the version if
    the file does not tell it; ``unattended``: see :attr:`InstallPlan.unattended`;
    ``uninstall_command``: a fixed command (or None for no action) instead of looking it up.
    """
    options = dataclasses.replace(options or InstallOptions())
    options.scope = Scope(options.scope)
    options.keep_original = bool(options.keep_original)   # None: an AppImage is moved
    status = status or get_system_status()
    warnings: list[str] = list(info.warnings)

    if is_foreign_arch(info.elf):
        arch, host = arch_label(info.elf), host_arch()
        if not options.allow_foreign_arch:
            raise UnsupportedArchitectureError(
                _("This app is made for a different kind of computer ({arch}) and cannot run "
                  "on this one ({host}).").format(arch=arch, host=host))
        warnings.append(_(
            "This app is made for a different kind of computer ({arch}). It will probably not "
            "start on this one.").format(arch=arch))

    main_id = info.app_id
    if not ID_RE.fullmatch(main_id or ""):
        raise InstallError(_("This app has an unusual name that Easy Installer cannot handle."),
                           details=f"invalid app id {main_id!r}")

    layout = layout_for(options.scope)
    registry = _registry(layout)
    other_registry = _registry(layout_for(_other_scope(options.scope)))
    installed = registry.all()
    match = variants.match_installed(installed, main_id, info.path, info.sha256)
    current = entry if entry is not None else match.entry
    for other in (current, match.main):
        if other is not None and other.kind != KIND_APPIMAGE:
            raise _kind_conflict(other)
    base_name = info.name or info.display_name or main_id
    version = info.version or version_hint

    # Keep both: the new file becomes a separate, pinned app next to the installed version.
    keep_both_available = (entry is None and match.main is not None and current is match.main
                           and not match.is_installed_file)
    options.keep_both = bool(options.keep_both) and keep_both_available
    base_id: str | None = None
    pinned = False
    name_suffix: str | None = None
    if options.keep_both:
        sha = info.sha256
        if not version and not sha:
            try:
                sha = info.sha256 = _file_sha256(info.path)   # what tells the copies apart
            except OSError as exc:
                raise _os_error(exc) from exc
        app_id = variant_id(main_id, version, sha)
        name_suffix = version_label(version, sha)
        existing = registry.get(app_id)   # an older copy with the same version is replaced
        base_id, pinned = main_id, True
    elif current is not None and current.base_id:
        app_id, existing = current.id, current
        base_id, pinned = current.base_id, current.pinned
        name_suffix = version_label(version, info.sha256 or current.sha256)
    else:
        app_id, existing = (current.id, current) if current is not None else (main_id, None)
    if not ID_RE.fullmatch(app_id or ""):
        raise InstallError(_("This app has an unusual name that Easy Installer cannot handle."),
                           details=f"invalid app id {app_id!r}")
    name = variant_name(base_name, name_suffix) if name_suffix else base_name
    existing_other = other_registry.get(app_id) or other_registry.get(main_id)

    source_owner: InstalledApp | None = None
    if existing is not None and options.scope is Scope.USER and existing.appimage_path \
            and _same_file_path(info.path, existing.appimage_path):
        # Installing (repairing) the installed file itself: it stays where it is.
        target = Path(existing.appimage_path)
    elif stay_at is not None:
        target = Path(stay_at)
    else:
        # The registered app file of another installation (the other scope, or another id) must
        # stay where it is: moving, adopting or later deleting it would break that one.
        source_owner = _source_owner(info, app_id, options.scope, (registry, other_registry))
        target = _resolve_target(layout, info, app_id, name, registry,
                                 source_may_stay=source_owner is None)
    in_place = options.scope is Scope.USER and _same_file_path(target, info.path)
    desktop_path = layout.desktop_dir / desktop_file_name(app_id)
    has_icon = info.icon_path is not None and info.icon_info is not None
    icon_tgt = icon_target(layout, app_id, info.icon_info) if has_icon else None
    icon = make_icon_name(app_id) if has_icon else FALLBACK_ICON

    if version is None:
        # The version often only comes from the download's name, which is gone once installed.
        version = next((app.version for app in (existing, existing_other)
                        if app is not None and app.version and _is_same_file(info, app)), None)
    resolved_action = action or _resolve_action(version, existing, _is_same_file(info, existing))
    if resolved_action == ACTION_DOWNGRADE:
        warnings.append(_(
            "A newer version ({installed}) is already installed. This file contains the older "
            "version {new}.").format(installed=existing.version, new=version))
    if existing_other is not None:
        if existing_other.scope is Scope.SYSTEM:
            warnings.append(_("This app is also installed for everyone on this computer."))
        else:
            warnings.append(_("This app is also installed just for you."))
    if existing is None:
        namesake = next((app for app in installed if app.id != app_id and app.main_id != main_id
                         and app.name.casefold() == name.casefold()), None)
        if namesake is not None:
            warnings.append(_("Another app called {name} is already installed. This one is added "
                              "next to it and does not replace it.").format(name=namesake.name))

    if source_owner is not None and not options.keep_original:
        options.keep_original = True
        warnings.append(_("{file} is the app file of an app that is already installed, so it "
                          "stays where it is and a copy is installed.").format(
                              file=display_name(info.path)))

    keep_backup = _keep_backup_wanted(options)
    backup_target = _planned_backup(layout, registry, existing, info, keep_backup=keep_backup,
                                    in_place=in_place)
    if backup_target is None and keep_backup and not in_place and not unattended \
            and not _is_same_file(info, existing) and _changed_itself(existing):
        warnings.append(_("{name} has changed its own file since Easy Installer last looked at "
                          "it, so the version that is there now is not kept.").format(
                              name=existing.name))

    # Trust: a file whose signature is broken, and an app whose signer is no longer the same.
    # (Not when the app updated itself in the background or goes back to a version that was
    # installed before: nobody is asked there.)
    changed_signer = False
    if not unattended and action != ACTION_ROLLBACK:
        note = invalid_signature_note(info)
        if note:
            warnings.insert(0, note)
        reference = existing if existing is not None else match.main
        changed_signer = signer_changed(reference, info)
        if changed_signer:
            warnings.insert(0, _signer_changed_note(reference))

    sandbox_fix = _resolve_sandbox(info, options, status, warnings)
    profile_text = None
    needs_root_for_profile = False
    if sandbox_fix is SandboxFix.APPARMOR:
        # The helper attaches the profile to the resolved path (AppArmor matches those), e.g.
        # /data/home/u/... when /home/u is a symlink; compare with exactly that text.
        profile_text = render_apparmor_profile(app_id, os.path.realpath(target))
        needs_root_for_profile = not _apparmor_profile_current(layout, app_id, profile_text)
        if unattended and needs_root_for_profile:
            # Nobody is there to type a password: the app keeps working without its sandbox.
            sandbox_fix, profile_text, needs_root_for_profile = SandboxFix.NO_SANDBOX, None, False
            with contextlib.suppress(ValueError):
                warnings.remove(_apparmor_note())

    if options.extract_and_run is None:
        extract_and_run = not status.can_run_appimage(info.elf)
        if extract_and_run:
            warnings.append(_(
                "A system component (FUSE) is missing, so the app will start a little slower "
                "than usual."))
    else:
        extract_and_run = bool(options.extract_and_run)

    command: tuple[str, ...] | None = None
    if uninstall_command is not _LOOK_UP:
        command = tuple(uninstall_command) if uninstall_command else None  # type: ignore[arg-type]
    elif options.add_uninstall_action:
        launcher = find_uninstall_launcher() if options.scope is Scope.USER \
            else find_uninstall_launcher(Scope.SYSTEM)
        if launcher:
            command = (launcher, "--uninstall", app_id)

    desktop_text = _render_desktop(
        info, app_id=app_id, name=name, target=target, icon=icon if has_icon else None,
        scope=options.scope, extract_and_run=extract_and_run,
        uninstall_command=command, sandbox_fix=sandbox_fix, version=version,
        name_suffix=name_suffix,
    )

    return InstallPlan(
        info=info, options=options, layout=layout, app_id=app_id, name=name,
        target_appimage=target, desktop_path=desktop_path, icon_name=icon, icon_target=icon_tgt,
        desktop_text=desktop_text, action=resolved_action, existing=existing,
        existing_other_scope=existing_other, sandbox_fix=sandbox_fix,
        extract_and_run=extract_and_run, uninstall_command=command,
        apparmor_profile_text=profile_text,
        requires_root=options.scope is Scope.SYSTEM or needs_root_for_profile,
        in_place=in_place, warnings=_dedupe(warnings), version=version,
        source_is_installed=source_owner is not None,
        mime_types=select_mime_types(
            info.mime_types,
            info.desktop_entry.get_list("MimeType") if info.desktop_entry is not None else (),
            host_mime_database()) if info.mime_types else [],
        keep_both_available=keep_both_available, backup_target=backup_target,
        keep_backup=keep_backup, base_id=base_id, pinned=pinned, name_suffix=name_suffix,
        main_installed=match.main, unattended=unattended, signer_changed=changed_signer,
    )


def plan_refresh(info: AppImageInfo, app: InstalledApp, *, path: Path | None = None,
                 status: SystemStatus | None = None,
                 version_hint: str | None = None) -> InstallPlan:
    """Plan for the file of an installed per-user app that changed on its own (the app updated
    itself) or that the entry is moved to (its updater renamed the file): see ``core.reconcile``.

    The file stays where it is (recorded as ``path``, default ``info.path``); the choices of
    the installation (sandbox fix, start mode, "Uninstall…" action) are kept; no backup is
    made; nothing asks for a password.
    """
    options = options_from_app(app, keep_original=True, keep_backup=False)
    return _make_plan(info, options, status=status, entry=app, stay_at=path or info.path,
                      unattended=True, uninstall_command=installed_uninstall_command(app),
                      version_hint=version_hint)


# ------------------------------------------------------------------------------------------------
# portable apps (unpacked archives): planning
# ------------------------------------------------------------------------------------------------


def read_portable_marker(folder: Path | str) -> dict | None:
    """The content of ``<folder>/.easy-installer.json`` (written when a portable app is
    installed), or None if it is missing, a link, too large or not a JSON object."""
    return appfolder.read_marker(folder)


def portable_dir_state(layout: Layout, app: InstalledApp,
                       registry: Registry | None = None) -> tuple[str, str]:
    """May the folder of the portable app ``app`` be replaced or removed?

    Returns ``(state, technical details)``. ``"ok"`` only if ``app.install_dir``

    * is spelled as a folder directly inside the apps folder (no ``..``, not hidden, not the
      apps folder itself - so never the home folder or the folder with the kept versions),
    * is a real folder of this user - not a symbolic link, not a mount point,
    * carries the marker file ``.easy-installer.json`` with this app's id, and
    * contains nothing another installed app uses (with ``registry``).

    ``"missing"``: there is nothing at that (plausible) place. ``"unsafe"``: anything else -
    the folder must be left alone.
    """
    raw = app.install_dir
    if app.kind != KIND_PORTABLE or not isinstance(raw, str) or not raw or "\0" in raw \
            or not os.path.isabs(raw) or not ID_RE.fullmatch(app.id or ""):
        return PORTABLE_UNSAFE, f"no usable folder is recorded for {app.id!r}: {raw!r}"
    parent, name = os.path.split(raw)
    if not name or name in (".", "..") or name.startswith(".") or os.path.normpath(raw) != raw:
        return PORTABLE_UNSAFE, f"{raw!r} is not a plain folder name"
    if os.path.realpath(parent) != os.path.realpath(layout.apps_dir):
        return PORTABLE_UNSAFE, f"{raw!r} is not directly inside {str(layout.apps_dir)!r}"
    try:
        st = os.lstat(raw)
    except FileNotFoundError:
        return PORTABLE_MISSING, f"{raw!r} does not exist"
    except OSError as exc:
        return PORTABLE_UNSAFE, f"{raw!r}: {exc}"
    if not stat.S_ISDIR(st.st_mode):
        return PORTABLE_UNSAFE, f"{raw!r} is a link or a file, not a real folder"
    if st.st_uid != os.geteuid():
        return PORTABLE_UNSAFE, f"{raw!r} belongs to another user"
    if os.path.ismount(raw):
        return PORTABLE_UNSAFE, f"{raw!r} is a mount point"
    marker = read_portable_marker(raw)
    if marker is None or marker.get("id") != app.id:
        found = marker.get("id") if marker is not None else None
        return PORTABLE_UNSAFE, f"{raw!r} carries no marker of {app.id!r} (found: {found!r})"
    if registry is not None:
        for other in registry.all():
            if other.id == app.id:
                continue
            for path in (other.install_dir, other.appimage_path):
                if path and (_is_within(path, raw) or _is_within(raw, path)):
                    return PORTABLE_UNSAFE, f"{raw!r} is also used by {other.id!r}"
    return PORTABLE_OK, ""


def installed_program(info: PortableInfo) -> str | None:
    """The program (relative path) that an installed version of this portable app starts, if
    the archive ``info`` has it among its candidates: a newer archive keeps the choice made for
    the installed version (the GUI and the CLI preselect it)."""
    candidates = {candidate.relpath for candidate in info.executables}
    for app in find_installed(info.app_id):
        if app.kind != KIND_PORTABLE or not app.install_dir or not app.appimage_path:
            continue
        try:
            relpath = PurePosixPath(os.path.relpath(app.appimage_path, app.install_dir))
        except ValueError:
            continue
        if relpath.as_posix() in candidates and ".." not in relpath.parts:
            return relpath.as_posix()
    return None


def _portable_relpath(value: object, info: PortableInfo | None = None) -> str:
    """``info.executable`` as a plain relative path inside the app folder - one that is in the
    archive ``info`` (as far as that is known) and that a menu entry can name (UTF-8)."""
    text = value if isinstance(value, str) else ""
    parts = text.split("/")
    if not text or text.startswith("/") or len(text) > 1024 \
            or any(part in ("", ".", "..") for part in parts) \
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text) \
            or (info is not None and info.has_member(text) is False):
        raise InstallError(_("The program that starts this app was not found in the archive."),
                           details=f"unusable program path {value!r}")
    try:
        text.encode("utf-8")
    except UnicodeError as exc:
        raise InstallError(
            _("The name of the program that starts this app uses letters that the app menu "
              "cannot show, so it cannot be started from there."),
            details=f"program path {value!r} is not UTF-8") from exc
    return text


def _portable_target(layout: Layout, registry: Registry, existing: InstalledApp | None,
                     app_id: str, name: str) -> Path:
    """The folder a portable app is unpacked to: the app's present folder when it is (or was)
    verifiably its own, else a free name - nothing that is already there is ever reused."""
    if existing is not None and existing.install_dir:
        state, details = portable_dir_state(layout, existing, registry)
        if state in (PORTABLE_OK, PORTABLE_MISSING):
            return Path(existing.install_dir)
        log.warning("not reusing the folder of %s: %s", existing.id, details)
    stem = safe_file_stem(name)
    taken = {os.path.realpath(app.install_dir) for app in registry.all() if app.install_dir}
    candidates = [stem, f"{stem}-{app_id}"] + [f"{stem}-{app_id}-{n}" for n in range(2, 1000)]
    for candidate in candidates:
        path = layout.apps_dir / candidate
        if not os.path.lexists(path) and os.path.realpath(path) not in taken:
            return path
    raise InstallError(_("The app could not be installed because a file could not be copied or "
                         "saved."), details=f"no free folder name for {stem!r}")


def _portable_sandbox(info: PortableInfo, options: InstallOptions, status: SystemStatus,
                      warnings: list[str]) -> SandboxFix:
    """An unpacked Electron app can only be started without its sandbox where the computer
    blocks it: a permission (AppArmor profile) is only ever given to AppImages. The
    ``--no-sandbox`` of the archive's own menu entry only counts if that entry's command is
    kept (it starts the chosen program)."""
    has_flag = info.exec_has_no_sandbox and desktop_exec_matches(info.desktop_entry,
                                                                  info.executable)
    if not (info.is_electron and status.userns_restricted and not has_flag):
        return SandboxFix.NONE
    if options.sandbox_fix is not None and SandboxFix(options.sandbox_fix) is SandboxFix.NONE:
        warnings.append(_(
            "This app may not start, because this computer blocks the security sandbox it needs."))
        return SandboxFix.NONE
    warnings.append(_(
        "This app will be started without its built-in security sandbox, because this "
        "computer blocks it otherwise."))
    return SandboxFix.NO_SANDBOX


def _render_portable_desktop(info: PortableInfo, *, app_id: str, name: str, program: Path,
                             install_dir: Path, icon: str | None, sandbox_fix: SandboxFix,
                             uninstall_command: tuple[str, ...] | None,
                             version: str | None) -> str:
    embedded = info.desktop_entry
    if embedded is not None and not desktop_exec_matches(embedded, info.executable):
        # The app's own entry starts something else (``sh -c "..."``, another program): keep
        # its names and categories, but start the chosen program plainly.
        embedded = embedded.copy()
        for key in ("Exec", "TryExec"):
            embedded.remove(key)
    spec = DesktopRenderSpec(
        app_id=app_id, name=name, appimage_path=program, icon_name=icon, embedded=embedded,
        embedded_stem=_embedded_stem(info), comment=info.comment, version=version,
        scope=Scope.USER,
        extra_args=(NO_SANDBOX_ARG,) if sandbox_fix is SandboxFix.NO_SANDBOX else (),
        extract_and_run=False, uninstall_command=uninstall_command,
        working_dir=install_dir, startup_wm_class=info.wm_class,
    )
    return render_desktop_entry(spec)


def _make_portable_plan(info: PortableInfo, options: InstallOptions | None, *,
                        status: SystemStatus | None, entry: InstalledApp | None = None,
                        action: str | None = None, source_dir: Path | None = None,
                        in_place: bool = False,
                        uninstall_command: object = _LOOK_UP) -> InstallPlan:
    """:func:`plan_install` for a portable archive, plus what rollback and repair need.

    ``entry``: plan for exactly this installed entry; ``source_dir``: a complete app folder
    (the kept previous version) that takes the app's place instead of an unpacked archive;
    ``in_place``: the installed folder stays as it is, only the launcher, the icon and the
    registry entry are made again ("Repair").
    """
    given_keep = options.keep_original if options is not None else None
    options = dataclasses.replace(options or InstallOptions())
    options.scope = Scope(options.scope)
    # The archive is not the app: it stays unless the caller asks explicitly to delete it.
    options.keep_original = True if given_keep is None else bool(given_keep)
    if options.scope is not Scope.USER:
        raise InstallError(_("Apps that come as an archive can only be installed just for you, "
                             "not for everyone on this computer."),
                           details="portable apps are per-user only")
    if options.keep_both:
        raise InstallError(_("An app that comes as an archive can only replace the version you "
                             "have. It cannot be installed next to it."),
                           details="keep_both is not available for portable apps")
    status = status or get_system_status()
    warnings: list[str] = list(info.warnings)

    host = host_arch()
    if info.arch and info.arch != host and not options.allow_foreign_arch:
        raise UnsupportedArchitectureError(
            _("This app is made for a different kind of computer ({arch}) and cannot run "
              "on this one ({host}).").format(arch=info.arch, host=host))

    app_id = info.app_id
    if not ID_RE.fullmatch(app_id or ""):
        raise InstallError(_("This app has an unusual name that Easy Installer cannot handle."),
                           details=f"invalid app id {app_id!r}")
    layout = layout_for(Scope.USER)
    registry = _registry(layout)
    installed = registry.all()
    existing = entry if entry is not None else registry.get(app_id)
    for other in [existing, *variants.family(installed, app_id)]:
        if other is not None and other.kind != KIND_PORTABLE:
            raise _kind_conflict(other)
    existing_other = _registry(layout_for(Scope.SYSTEM)).get(app_id)
    name = info.name or info.display_name or app_id
    version = info.version
    executable = _portable_relpath(info.executable, info)

    if in_place or source_dir is not None:
        if existing is None or not existing.install_dir:
            raise InstallError(_("This app is not installed."), details=f"{app_id}: no folder")
        install_dir = Path(existing.install_dir)
    else:
        install_dir = _portable_target(layout, registry, existing, app_id, name)
    program = install_dir / executable
    desktop_path = layout.desktop_dir / desktop_file_name(app_id)
    has_icon = info.icon_path is not None and info.icon_info is not None
    icon_tgt = icon_target(layout, app_id, info.icon_info) if has_icon else None
    icon = make_icon_name(app_id) if has_icon else FALLBACK_ICON
    if not has_icon and (in_place or source_dir is not None) and existing is not None \
            and existing.icon_path and existing.icon_name:
        icon = existing.icon_name   # the folder no longer tells its icon: the installed one stays

    same_file = bool(existing is not None and info.sha256 and existing.sha256 == info.sha256)
    if version is None and same_file:
        version = existing.version
    resolved_action = action or _resolve_action(version, existing, same_file)
    if resolved_action == ACTION_DOWNGRADE:
        warnings.append(_(
            "A newer version ({installed}) is already installed. This file contains the older "
            "version {new}.").format(installed=existing.version, new=version))
    if existing_other is not None:
        warnings.append(_("This app is also installed for everyone on this computer."))
    if existing is None:
        namesake = next((app for app in installed if app.id != app_id and app.main_id != app_id
                         and app.name.casefold() == name.casefold()), None)
        if namesake is not None:
            warnings.append(_("Another app called {name} is already installed. This one is added "
                              "next to it and does not replace it.").format(name=namesake.name))

    keep_backup = _keep_backup_wanted(options)
    backup_target: Path | None = None
    # The same archive again replaces the folder without keeping it (a fresh copy of what is
    # there). Going back always keeps the current version: two folders swap, and what a kept
    # folder says about itself is never a reason to delete the installed one.
    replaces = source_dir is not None or not same_file
    if existing is not None and keep_backup and not in_place and replaces \
            and _same_file_path(install_dir, existing.install_dir or "") \
            and portable_dir_state(layout, existing, registry)[0] == PORTABLE_OK:
        backup_target = backups.planned_backup_path(layout, existing)

    sandbox_fix = _portable_sandbox(info, options, status, warnings)
    command: tuple[str, ...] | None = None
    if uninstall_command is not _LOOK_UP:
        command = tuple(uninstall_command) if uninstall_command else None  # type: ignore[arg-type]
    elif options.add_uninstall_action:
        launcher = find_uninstall_launcher()
        if launcher:
            command = (launcher, "--uninstall", app_id)

    desktop_text = _render_portable_desktop(
        info, app_id=app_id, name=name, program=program, install_dir=install_dir,
        icon=icon if icon != FALLBACK_ICON else None, sandbox_fix=sandbox_fix,
        uninstall_command=command, version=version)

    return InstallPlan(
        info=info, options=options, layout=layout, app_id=app_id, name=name,  # type: ignore[arg-type]
        target_appimage=program, desktop_path=desktop_path, icon_name=icon, icon_target=icon_tgt,
        desktop_text=desktop_text, action=resolved_action, existing=existing,
        existing_other_scope=existing_other, sandbox_fix=sandbox_fix, extract_and_run=False,
        uninstall_command=command, apparmor_profile_text=None, requires_root=False,
        in_place=in_place, warnings=_dedupe(warnings), version=version, kind=KIND_PORTABLE,
        keep_both_available=False, backup_target=backup_target, keep_backup=keep_backup,
        main_installed=existing, portable=info, install_dir=install_dir, source_dir=source_dir,
    )


# ------------------------------------------------------------------------------------------------
# file operations (module-level so tests can inject failures)
# ------------------------------------------------------------------------------------------------


def _rename(src: Path, dst: Path) -> None:
    os.rename(src, dst)


def _unlink_source(path: Path) -> None:
    os.unlink(path)


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _write_desktop_file(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), FILE_MODE)


def _copy_icon(src: Path, dst: Path) -> None:
    with open(src, "rb") as fh:
        data = fh.read(MAX_ICON_SIZE + 1)
    if len(data) > MAX_ICON_SIZE:
        raise InstallError(_("The app icon is too large."), details=f"{src}: > {MAX_ICON_SIZE} bytes")
    _atomic_write_bytes(dst, data, FILE_MODE)


def _copy_with_progress(src: Path, part: Path, report: ProgressCallback, message: str,
                        start: float, end: float) -> str:
    """Copy ``src`` to the new file ``part`` (0755, fsynced) and return its sha256."""
    digest = hashlib.sha256()
    with open(src, "rb") as fin:
        total = os.fstat(fin.fileno()).st_size or 1
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fout:
                done = 0
                while True:
                    chunk = fin.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fout.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    report(start + (end - start) * min(done / total, 1.0), message)
                fout.flush()
                os.fsync(fout.fileno())
                os.fchmod(fout.fileno(), EXEC_MODE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(part)
            raise
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class _Transaction:
    """Remembers how to undo each step; commit() deletes backups of replaced files."""

    def __init__(self) -> None:
        self._undo: list[tuple[str, Callable[[], None]]] = []
        self._on_commit: list[tuple[str, Callable[[], None]]] = []

    def on_rollback(self, what: str, fn: Callable[[], None]) -> None:
        self._undo.append((what, fn))

    def on_commit(self, what: str, fn: Callable[[], None]) -> None:
        self._on_commit.append((what, fn))

    def created(self, path: Path) -> None:
        self.on_rollback(f"remove {path}", lambda: _unlink_missing_ok(path))

    def backup(self, path: Path) -> None:
        """Move an existing file aside so it can be restored on rollback."""
        if not (path.exists() or path.is_symlink()):
            return
        if path.is_dir() and not path.is_symlink():
            raise InstallError(
                _("A folder is in the way where the app should be installed: {path}").format(path=path))
        backup = path.with_name(f".{path.name}.easyinstaller-old-{secrets.token_hex(4)}")
        os.rename(path, backup)
        self.on_rollback(f"restore {path}", lambda: os.replace(backup, path))
        self.on_commit(f"drop backup of {path}", lambda: _unlink_missing_ok(backup))

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


def _unlink_missing_ok(path: Path | str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


# ------------------------------------------------------------------------------------------------
# execute: user scope
# ------------------------------------------------------------------------------------------------


def _check_source(plan: InstallPlan) -> None:
    source = plan.info.path
    try:
        st = source.stat()
    except FileNotFoundError as exc:
        raise InstallError(
            _("The file {name} is no longer there. Was it moved or deleted?").format(
                name=display_name(source)),
            details=str(exc)) from exc
    except OSError as exc:
        raise _os_error(exc) from exc
    if plan.info.size and st.st_size != plan.info.size:
        raise InstallError(
            _("The file {name} changed after it was checked. Please try again.").format(
                name=display_name(source)),
            details=f"size {plan.info.size} -> {st.st_size}")


def _place_appimage(plan: InstallPlan, tx: _Transaction,
                    report: ProgressCallback) -> tuple[str | None, Path | None]:
    """Put the AppImage at its target. Returns (sha256 if computed, source to delete on commit)."""
    source, target = plan.info.path, plan.target_appimage

    if plan.in_place:
        old_mode = source.stat().st_mode & 0o7777
        os.chmod(target, EXEC_MODE)
        tx.on_rollback(f"restore mode of {target}", lambda: os.chmod(target, old_mode))
        report(0.8, _("Preparing the app…"))
        return None, None

    if not plan.options.keep_original:
        report(0.05, _("Moving the app…"))
        old_mode = source.stat().st_mode & 0o7777
        tx.backup(target)
        try:
            _rename(source, target)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                log.info("%s and %s are on different drives; copying instead", source, target)
            elif exc.errno in _PERMISSION_ERRNOS and os.access(target.parent, os.W_OK):
                # The source folder is read-only (e.g. a USB stick): copy and try to delete later.
                log.info("cannot move %s (%s); copying instead", source, exc)
            else:
                raise
        else:
            def move_back() -> None:
                os.rename(target, source)
                os.chmod(source, old_mode)

            tx.on_rollback(f"move {target} back to {source}", move_back)
            os.chmod(target, EXEC_MODE)
            _fsync_dir(target.parent)
            report(0.8, _("Moving the app…"))
            return None, None

    message = _("Copying the app…")
    report(0.05, message)
    part = target.parent / f".{target.name}.part"
    _unlink_missing_ok(part)
    tx.on_rollback(f"remove {part}", lambda: _unlink_missing_ok(part))
    sha = _copy_with_progress(source, part, report, message, 0.05, 0.8)
    if plan.info.sha256 and sha != plan.info.sha256:
        raise InstallError(
            _("The file {name} changed while it was being installed. Please try again.").format(
                name=display_name(source)),
            details=f"sha256 {plan.info.sha256} -> {sha}")
    tx.backup(target)
    os.replace(part, target)
    tx.created(target)
    _fsync_dir(target.parent)
    # Moving across drives: the original is deleted only once everything else has worked.
    return sha, (None if plan.options.keep_original else source)


def _install_apparmor_profile(plan: InstallPlan, tx: _Transaction) -> str | None:
    """Ask the helper to install the profile. Returns the profile path; raises Helper/AuthorizationError."""
    profile_path = str(plan.layout.apparmor_dir / apparmor_profile_name(plan.app_id))
    if plan.apparmor_profile_text and _apparmor_profile_current(
            plan.layout, plan.app_id, plan.apparmor_profile_text):
        log.info("AppArmor profile for %s is already up to date", plan.app_id)
        return profile_path
    if plan.unattended:
        raise HelperError(_("The administrator task could not be started."),
                          details="not asking for the password in the background")
    result = privileged.run_helper(
        "apparmor-install",
        {"app_id": plan.app_id, "appimage_path": str(plan.target_appimage)},
    )
    path = result.get("profile_path") if isinstance(result.get("profile_path"), str) else None
    had_profile = plan.existing is not None and bool(plan.existing.apparmor_profile)
    if not had_profile:
        def remove_profile() -> None:
            try:
                privileged.run_helper("apparmor-remove", {"app_id": plan.app_id})
            except EasyInstallerError as exc:
                log.warning("could not remove AppArmor profile during rollback: %s", exc)

        tx.on_rollback(f"remove AppArmor profile for {plan.app_id}", remove_profile)
    return path or profile_path


def _used_by_others(registry: Registry, app_id: str) -> set[str]:
    """App files other entries use (two entries sharing one file must each keep it)."""
    return {os.path.realpath(app.appimage_path) for app in registry.all()
            if app.id != app_id and app.appimage_path}


def _remove_previous_files(plan: InstallPlan, new: InstalledApp, registry: Registry) -> None:
    old = plan.existing
    if old is None:
        return
    keep = {os.path.realpath(p) for p in (new.appimage_path, new.desktop_path, *new.icon_paths)}
    keep |= _used_by_others(registry, new.id)
    if plan.options.keep_original and not _same_file_path(plan.info.path, old.appimage_path):
        # The user's original file - but not the previous version's own app file, which nothing
        # would track any more (it would survive even an uninstall).
        keep.add(os.path.realpath(plan.info.path))
    keep |= {os.path.realpath(new.mime_package)} if new.mime_package else set()
    candidates = [("desktop", old.desktop_path), ("mime", old.mime_package or "")]
    if old.kind == KIND_APPIMAGE and new.kind == KIND_APPIMAGE:
        # (the program of a portable app is part of its folder, which is swapped as a whole)
        candidates.insert(0, ("appimage", old.appimage_path))
    candidates += [("icon", p) for p in old.icon_paths]
    for kind, path in candidates:
        if not path or os.path.realpath(path) in keep:
            continue
        if not _safe_to_delete(path, kind=kind, app=old):
            log.warning("not deleting unexpected %s path %s of the previous version", kind, path)
            continue
        try:
            _unlink_missing_ok(path)
            log.info("removed file of the previous version: %s", path)
        except OSError as exc:
            log.warning("could not remove old file %s: %s", path, exc)


_BACKUP_RE = re.compile(r"^\.(.+)\.easyinstaller-old-[0-9a-f]{8}\Z", re.DOTALL)
_PART_RE = re.compile(r"^\..+\.AppImage\.part\Z", re.DOTALL)
#: the folder a portable app is unpacked to before it takes its place in the apps folder
_STAGING_DIR_RE = re.compile(r"^\..+\.easyinstaller-new-[0-9a-f]{8}\Z", re.DOTALL)


def _registered_files(registry: Registry) -> set[str]:
    return {os.path.realpath(path) for app in registry.all()
            for path in (app.appimage_path, app.desktop_path, app.mime_package, *app.icon_paths)
            if path}


def _own_real_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid()


def _clean_leftover_dir(directory: Path, name: str, registry: Registry) -> None:
    """A folder that an interrupted installation of a portable app left in the apps folder.

    * ``.<name>.easyinstaller-new-<x>``: a half unpacked app - deleted;
    * ``.<name>.easyinstaller-old-<x>``: the version that was being replaced (or removed). It is
      put back if ``<name>`` is gone and still is the folder of an installed app; else it is
      deleted - but only if it carries Easy Installer's marker file.
    """
    path = directory / name
    if not _own_real_dir(path):
        return
    if _STAGING_DIR_RE.match(name):
        remove_tree(path)
        log.info("removed the partly unpacked app %s", path)
        return
    match = _BACKUP_RE.match(name)
    if match is None:
        return
    marker = read_portable_marker(path)
    if marker is None:
        log.warning("leaving %s alone: it carries no marker of Easy Installer", path)
        return
    original = directory / match.group(1)
    owner = next((app for app in registry.all() if app.kind == KIND_PORTABLE and app.install_dir
                  and os.path.normpath(app.install_dir) == os.path.normpath(original)), None)
    if owner is not None and marker.get("id") == owner.id and not os.path.lexists(original):
        os.rename(path, original)
        log.info("restored %s from an interrupted installation", original)
        return
    # (never deleted for good while it holds what the app or the user saved in it)
    result, where = appfolder.dispose(path, visible=directory / backups.kept_folder_name(
        _marker_text(marker, "name") or match.group(1), _marker_text(marker, "version", 64),
        marker.get("sha256") if isinstance(marker.get("sha256"), str) else None))
    log.info("the replaced version %s was %s%s", path, result, f" ({where})" if where else "")


def _clean_leftovers(directories: Iterable[Path], registry: Registry, *,
                     apps_dir: Path | None = None) -> None:
    """Remove what an interrupted installation (crash, logout, kill) left behind.

    Runs under the registry lock, so no other installation is using these files: partial copies
    are deleted; a backup of a replaced file is put back only if its file is missing and still
    belongs to an installed app (else it is deleted: e.g. the app was uninstalled since, and its
    old launcher must not come back). In ``apps_dir`` the same is done for the folders of
    portable apps (see :func:`_clean_leftover_dir`).
    """
    registered = _registered_files(registry)
    for directory in dict.fromkeys(directories):
        if BACKUPS_DIR_NAME in Path(directory).parts:
            continue   # kept previous versions are none of this function's business
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if name == BACKUPS_DIR_NAME:
                continue
            path = directory / name
            try:
                if apps_dir is not None and Path(directory) == Path(apps_dir) \
                        and name.startswith(".") and path.is_dir() and not path.is_symlink():
                    _clean_leftover_dir(Path(directory), name, registry)
                    continue
                if _PART_RE.match(name) and not path.is_dir():
                    _unlink_missing_ok(path)
                    log.info("removed the partial copy %s", path)
                    continue
                match = _BACKUP_RE.match(name)
                if match is None or path.is_dir():
                    continue
                original = directory / match.group(1)
                if not (original.exists() or original.is_symlink()) \
                        and os.path.realpath(original) in registered:
                    os.rename(path, original)
                    log.info("restored %s from an interrupted installation", original)
                else:
                    _unlink_missing_ok(path)
                    log.info("removed the old backup %s", path)
            except OSError as exc:
                log.warning("cannot clean up %s: %s", path, exc)


def recover_interrupted(scope: Scope = Scope.USER) -> None:
    """Put back what an installation, update or rollback that was stopped hard (power loss,
    a kill, the end of the session) had set aside, and remove its half-done copies - what the
    next installation would do anyway, but before an app is reported missing (its "Remove"
    would delete the app file that is still there under another name). Per-user apps only;
    never raises, does not wait long for a running installation."""
    if Scope(scope) is not Scope.USER:
        return
    layout = layout_for(Scope.USER)
    registry = _registry(layout)
    try:
        if not layout.registry_path.exists():
            return
        with registry.locked(timeout=UNATTENDED_LOCK_TIMEOUT):
            icon_dirs = {Path(path).parent for app in registry.all() for path in app.icon_paths}
            _clean_leftovers([layout.apps_dir, layout.desktop_dir, *sorted(icon_dirs),
                              layout.mime_dir / "packages"], registry, apps_dir=layout.apps_dir)
    except (EasyInstallerError, OSError) as exc:
        log.info("not looking for interrupted installations now: %s",
                 getattr(exc, "details", None) or exc)


def _check_plan_current(plan: InstallPlan, registry: Registry) -> None:
    """The plan was made earlier (e.g. before "Proceed? [Y/n]"): is it still right?"""
    current = registry.get(plan.app_id)
    target = plan.target_appimage
    problem = None
    if current != plan.existing:
        problem = f"registry entry of {plan.app_id} changed since planning"
    elif plan.kind == KIND_PORTABLE:
        pass   # a whole folder is swapped: _swap_in_portable checks what is in its way
    elif not plan.in_place and (target.exists() or target.is_symlink()) \
            and registry.owner_of(target) != plan.app_id:
        problem = f"{target} appeared and belongs to {registry.owner_of(target) or 'nobody'}"
    if problem:
        raise InstallError(_("Another installation changed the installed apps in the meantime. "
                             "Please try again."), details=problem)


def _stash_previous(plan: InstallPlan, tx: _Transaction,
                    warnings: list[str]) -> backups.Stash | None:
    """Keep the version that is about to be replaced (see :mod:`.backups`).

    An update goes on without a backup if the old version cannot be kept (with a note); going
    back swaps the two versions, so there it is an error and nothing is changed.
    """
    if plan.backup_target is None or plan.existing is None:
        return None
    try:
        stash = backups.stash(plan.layout, plan.existing)
    except backups.FileChangedError:
        raise   # the plan no longer fits the app: nothing was changed, the caller tries again
    except (OSError, InstallError) as exc:
        details = exc.details if isinstance(exc, InstallError) else f"{type(exc).__name__}: {exc}"
        if plan.action == ACTION_ROLLBACK:
            raise InstallError(
                _("The current version of {name} could not be kept, so nothing was changed.")
                .format(name=plan.existing.name), details=details) from exc
        log.warning("cannot keep the previous version of %s: %s", plan.app_id, details)
        warnings.append(_("The previous version could not be kept, so you cannot go back "
                          "to it."))
        return None
    tx.on_rollback(f"take back the kept version {stash.path}", stash.undo)
    return stash


def _new_previous(plan: InstallPlan, stash: backups.Stash | None,
                  sha: str | None) -> dict | None:
    """The ``previous`` of the new registry entry: the most recent version that is kept.

    * the version that was just replaced, if it was kept;
    * nothing if the app file was replaced and backups are not wanted - the older backup is
      deleted then, too (``_drop_obsolete_backup``);
    * else the backup that is already there: the same file was installed again, the file
      changed in place (the app updated itself), or the replaced version could not be kept -
      what cannot be replaced by a newer backup is not thrown away. (After an interrupted
      update the version that was already kept is found again, see ``find_unrecorded``.)
    """
    if stash is not None:
        return stash.record
    existing = plan.existing
    if existing is None:
        return None
    same_content = bool(sha and existing.sha256 and sha == existing.sha256)
    replaced = not (plan.in_place or same_content or _is_same_file(plan.info, existing))
    if replaced and not plan.keep_backup:
        return None
    if plan.unattended and not _same_content(plan):
        # The file changed under the entry. If an interrupted update or rollback had already
        # kept the version the entry still describes, that is the previous version now.
        found = backups.find_unrecorded(plan.layout, existing)
        if found is not None:
            return found
    return backups.usable_previous(plan.layout, existing)


def _drop_obsolete_backup(plan: InstallPlan, new: InstalledApp) -> None:
    """One backup per app: delete the older one once it is no longer the entry's ``previous``."""
    old = plan.existing.previous if plan.existing is not None else None
    if not old or not old.get("path"):
        return
    if new.previous and new.previous.get("path") == old["path"]:
        return
    try:
        backups.remove_backup(plan.layout, plan.existing.id, old["path"])
    except OSError as exc:
        log.warning("could not delete the older backup %s: %s", old["path"], exc)


def _info_value(info: object, name: str) -> object:
    """An optional attribute of the inspected file (``to_dict()`` form of value objects)."""
    value = getattr(info, name, None)
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


def _same_content(plan: InstallPlan) -> bool:
    """The file has the content of the installed one (not just its place: an app that updated
    itself is a different file at the same path)."""
    info, existing = plan.info, plan.existing
    if existing is None:
        return False
    if info.sha256 and existing.sha256:
        return info.sha256 == existing.sha256
    return plan.in_place and not plan.unattended


def file_details(info: object) -> dict:
    """``update_source``, ``origin_url``, ``signature`` and ``data_hints`` as an inspected file
    tells them (optional attributes of an ``AppImageInfo``/``PortableInfo``), in the form the
    registry stores. Values of an unexpected type count as "not known"."""
    update_source = _info_value(info, "update_source")
    origin_url = _info_value(info, "origin_url")
    signature = _info_value(info, "signature")
    data_hints = _info_value(info, "data_hints")
    return {
        "update_source": dict(update_source)
        if isinstance(update_source, dict) and update_source else None,
        "origin_url": origin_url if isinstance(origin_url, str) and origin_url else None,
        "signature": dict(signature) if isinstance(signature, dict) and signature else None,
        "data_hints": [hint for hint in data_hints if isinstance(hint, str) and hint]
        if isinstance(data_hints, (list, tuple)) else [],
    }


def recorded_extras(plan: InstallPlan) -> dict:
    """``update_source``, ``origin_url``, ``signature`` and ``data_hints`` for the registry.

    Whatever the inspected file says (``plan.info.<name>``, optional attributes), completed by
    what is known about the installed app: where updates come from and the data folder hints
    belong to the app; origin and signature belong to one file, so they are only taken over
    when the very same file is installed again.
    """
    info, existing = plan.info, plan.existing
    same_file = _same_content(plan)
    details = file_details(info)
    update_source, origin_url = details["update_source"], details["origin_url"]
    signed, hints = details["signature"], details["data_hints"]
    if existing is not None:
        if update_source is None:
            update_source = existing.update_source
        if not hints:
            hints = list(existing.data_hints)
        if same_file:
            if origin_url is None:
                origin_url = existing.origin_url
            if signed is None and not hasattr(info, "signature"):
                signed = existing.signature
    return {"update_source": update_source, "origin_url": origin_url, "signature": signed,
            "data_hints": hints}


def _original_filename(plan: InstallPlan) -> str:
    """The name of the downloaded file; kept when the file comes from Easy Installer's own
    folders (repair, an app that updated itself). Going back: the name the kept version was
    installed from (unknown for versions kept by 0.2.0)."""
    existing = plan.existing
    if existing is not None and plan.action == ACTION_ROLLBACK:
        recorded = (existing.previous or {}).get("original_filename") \
            or getattr(plan.info, "original_filename", None)   # (a kept folder's marker)
        return recorded if isinstance(recorded, str) and recorded else ""
    if existing is not None and existing.original_filename and plan.in_place:
        return existing.original_filename
    return display_name(plan.info.path)


def _mtime_ns(path: Path) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _execute_user(plan: InstallPlan, report: ProgressCallback) -> InstalledApp:
    _check_source(plan)
    registry = _registry(plan.layout)
    timeout = UNATTENDED_LOCK_TIMEOUT if plan.unattended else USER_LOCK_TIMEOUT
    with registry.locked(timeout=timeout, on_wait=lambda: report(
            None, _("Waiting for another installation to finish…"))):
        # First put back what an interrupted installation moved aside, then check the plan
        # against what is really there now.
        _clean_leftovers((d for d in (plan.layout.apps_dir, plan.layout.desktop_dir,
                                      plan.icon_target.parent if plan.icon_target else None,
                                      plan.mime_package.parent if plan.mime_package else None)
                          if d), registry, apps_dir=plan.layout.apps_dir)
        _check_plan_current(plan, registry)
        if plan.kind == KIND_PORTABLE:
            return _execute_portable_locked(plan, registry, report)
        return _execute_user_locked(plan, registry, report)


def _execute_user_locked(plan: InstallPlan, registry: Registry,
                         report: ProgressCallback) -> InstalledApp:
    layout = plan.layout
    info = plan.info
    tx = _Transaction()
    warnings: list[str] = []
    sandbox_fix = plan.sandbox_fix
    apparmor_profile = plan.existing.apparmor_profile if plan.existing else None

    try:
        for directory in (layout.apps_dir, layout.desktop_dir,
                          plan.icon_target.parent if plan.icon_target else None):
            if directory is not None:
                directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

        # First give the old version its second name in the backup folder (nothing moves
        # yet), then put the new one in place.
        stash = _stash_previous(plan, tx, warnings)
        sha, source_to_delete = _place_appimage(plan, tx, report)

        icon_paths: list[str] = []
        if plan.icon_target is not None and info.icon_path is not None:
            report(0.85, _("Adding the app icon…"))
            tx.backup(plan.icon_target)
            _copy_icon(info.icon_path, plan.icon_target)
            tx.created(plan.icon_target)
            icon_paths.append(str(plan.icon_target))

        report(0.88, _("Adding the app to the menu…"))
        tx.backup(plan.desktop_path)
        _write_desktop_file(plan.desktop_path, plan.desktop_text)
        tx.created(plan.desktop_path)

        mime_package = plan.mime_package
        if mime_package is not None:
            # The app's own file types (e.g. *.FCStd), so that double-clicking such files works.
            mime_package.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            tx.backup(mime_package)
            _atomic_write_bytes(mime_package, render_mime_package(
                plan.app_id, plan.mime_types).encode("utf-8"), FILE_MODE)
            tx.created(mime_package)

        if sandbox_fix is SandboxFix.APPARMOR:
            report(None, _("Asking for permission to let the app use its security sandbox…"))
            try:
                apparmor_profile = _install_apparmor_profile(plan, tx)
            except (AuthorizationError, HelperError) as exc:
                log.warning("AppArmor profile not installed (%s); falling back to --no-sandbox",
                            exc.details or exc)
                sandbox_fix = SandboxFix.NO_SANDBOX
                _write_desktop_file(plan.desktop_path, render_plan_desktop(plan, sandbox_fix))
                with contextlib.suppress(ValueError):
                    plan.warnings.remove(_apparmor_note())
                warnings.append(_(
                    "The app could not get the special permission it needs, so it will be "
                    "started without its built-in security sandbox."))

        report(0.95, _("Saving…"))
        now = utc_now()
        app = InstalledApp(
            id=plan.app_id,
            name=plan.name,
            version=plan.version,
            scope=Scope.USER,
            appimage_path=str(plan.target_appimage),
            desktop_path=str(plan.desktop_path),
            icon_paths=icon_paths,
            icon_name=plan.icon_name,
            apparmor_profile=apparmor_profile,
            sandbox_fix=sandbox_fix.value,
            extract_and_run=plan.extract_and_run,
            extract_and_run_explicit=plan.options.extract_and_run is not None,
            sha256=info.sha256 or sha,
            size=info.size,
            arch=info.arch,
            update_info=info.update_info,
            original_filename=_original_filename(plan),
            comment=info.comment,
            installed_at=plan.existing.installed_at if plan.existing and plan.existing.installed_at else now,
            updated_at=now,
            installer_version=__version__,
            mime_package=str(mime_package) if mime_package is not None else None,
            kind=plan.kind,
            mtime_ns=_mtime_ns(plan.target_appimage),
            base_id=plan.base_id,
            pinned=plan.pinned,
            previous=_new_previous(plan, stash, sha),
            **recorded_extras(plan),
        )
        registry.put(app)
    except BaseException as exc:
        tx.rollback()
        if isinstance(exc, OSError):
            raise _os_error(exc) from exc
        raise

    tx.commit()
    _remove_previous_files(plan, app, registry)
    _drop_obsolete_backup(plan, app)
    if source_to_delete is not None:
        try:
            _unlink_source(source_to_delete)
        except FileNotFoundError:
            pass   # e.g. the kept version that was just put back: already gone
        except OSError as exc:
            log.warning("could not delete the original file %s: %s", source_to_delete, exc)
            warnings.append(_(
                "The app was installed, but the original file could not be deleted. You can "
                "delete {name} yourself.").format(name=display_name(source_to_delete)))

    report(0.98, _("Updating the app menu…"))
    refresh_desktop_caches(layout, mime=bool(app.mime_package or (
        plan.existing is not None and plan.existing.mime_package)))
    _validate_desktop_file(plan.desktop_path)
    for warning in warnings:
        if warning not in plan.warnings:
            plan.warnings.append(warning)
    report(1.0, _("Done"))
    return app


# ------------------------------------------------------------------------------------------------
# execute: portable apps (unpacked archives; user scope only)
# ------------------------------------------------------------------------------------------------


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _write_new_file(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Create ``path`` anew in a freshly unpacked app folder. The name is the installer's:
    whatever the archive put there goes - a link is removed (never written through), a folder
    of that name too."""
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.lexists(path):
        remove_tree(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        os.fchmod(fh.fileno(), mode)


def _marker_icon(folder: Path, info: PortableInfo) -> str | None:
    """Keep the app's icon in its folder (repair and going back need it); its file name."""
    if info.icon_path is None or info.icon_info is None:
        return None
    extension = extension_for(info.icon_info)
    if extension not in _MARKER_ICON_EXTENSIONS:
        return None
    name = f"{MARKER_ICON_STEM}.{extension}"
    try:
        with open(info.icon_path, "rb") as fh:
            data = fh.read(MAX_ICON_SIZE + 1)
        if not data or len(data) > MAX_ICON_SIZE:
            return None
        _write_new_file(folder / name, data)
    except OSError as exc:
        log.warning("cannot keep the icon in %s: %s", folder, exc)
        return None
    return name


def _write_portable_marker(folder: Path, plan: InstallPlan) -> appfolder.Manifest:
    """Write ``.easy-installer.json`` into a freshly unpacked app folder, after the record of
    everything that was unpacked (``.easy-installer-files.json``, see ``core.appfolder``).

    The marker says which installed app the folder belongs to (nothing without it is ever
    deleted), vouches for the record (its sha256) and keeps what the archive told about the
    app, so that the launcher can be made again without the archive (repair, going back to a
    kept version). Returns the record.
    """
    info = plan.portable
    assert info is not None
    entry = info.desktop_entry
    desktop = entry.to_text() if entry is not None else None
    if desktop is not None and len(desktop.encode("utf-8", "replace")) > _MAX_MARKER_DESKTOP:
        desktop = None
    icon = _marker_icon(folder, info)
    manifest, files_sha256 = appfolder.write_manifest(folder)
    data = {
        "format": MARKER_FORMAT,
        "id": plan.app_id,
        "name": info.name,
        "version": plan.version,
        "executable": info.executable,
        "desktop": desktop,
        "desktop_filename": info.desktop_filename,
        "comment": info.comment,
        "categories": list(info.categories),
        "terminal": bool(info.terminal),
        "is_electron": bool(info.is_electron),
        "exec_has_no_sandbox": bool(info.exec_has_no_sandbox),
        "wm_class": info.wm_class,
        "arch": info.arch,
        "tree_size": int(info.tree_size),
        "file_count": int(info.file_count),
        "icon": icon,
        "sha256": info.sha256,
        "original_filename": display_name(info.path),
        "origin_url": info.origin_url,
        "installed_at": utc_now(),
        "installer_version": __version__,
        "files_sha256": files_sha256,
    }
    _write_new_file(folder / MARKER_NAME, (json.dumps(data, indent=2) + "\n").encode("ascii"))
    return manifest


def _marker_text(marker: dict, key: str, limit: int = 200) -> str | None:
    value = marker.get(key)
    if not isinstance(value, str):
        return None
    # (display_text: JSON can carry half a surrogate pair, which no file can be written with)
    return _CONTROL_RE.sub(" ", display_text(value)).strip()[:limit].strip() or None


def _marker_arch(marker: dict) -> str | None:
    value = marker.get("arch")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,16}", value) else None


def _marker_int(marker: dict, key: str) -> int:
    value = marker.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _incomplete_folder(folder: Path | str, details: str) -> InstallError:
    return InstallError(_("This app's folder is incomplete. Please install the app again."),
                        details=f"{folder}: {details}")


def _marker_icon_copy(folder: Path, marker: dict, work_dir: Path):
    """The icon kept in the app folder, copied to ``work_dir`` (the folder may move)."""
    name = marker.get("icon")
    # Only the file the installer itself put there - never a path the marker makes up.
    if not isinstance(name, str) \
            or name not in [f"{MARKER_ICON_STEM}.{ext}" for ext in _MARKER_ICON_EXTENSIONS]:
        return None, None
    source = folder / name
    try:
        st = os.lstat(source)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_ICON_SIZE:
            return None, None
        copy = work_dir / f"icon{source.suffix}"
        shutil.copyfile(source, copy, follow_symlinks=False)
        probed = probe_image(copy)
    except OSError as exc:
        log.debug("cannot use the icon kept in %s: %s", folder, exc)
        return None, None
    return (copy, probed) if probed.format != "unknown" else (None, None)


def portable_info_from_folder(folder: Path | str, app_id: str) -> PortableInfo:
    """What an unpacked portable app says about itself in its marker file - the stand-in for
    inspecting the archive when the launcher is made again (repair) or a kept version comes
    back (rollback). ``info.path`` is the folder; the caller cleans up (``info.cleanup()``).

    Raises InstallError if the folder carries no marker of ``app_id`` or its program is gone.
    Everything in the marker is treated like the content of an archive: as untrusted text.
    """
    folder = Path(folder)
    marker = read_portable_marker(folder)
    if not _own_real_dir(folder) or marker is None or marker.get("id") != app_id:
        found = marker.get("id") if marker is not None else None
        raise _incomplete_folder(folder, f"no marker of {app_id!r} (found: {found!r})")
    try:
        executable = _portable_relpath(marker.get("executable"))
    except InstallError as exc:
        raise _incomplete_folder(folder, exc.details or str(exc)) from exc
    program = folder / executable
    if not _is_within(program, folder) or not os.path.isfile(program):
        raise _incomplete_folder(folder, f"the program {executable!r} is not there")
    try:
        with open(program, "rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        raise _incomplete_folder(folder, f"{executable!r}: {exc}") from exc

    entry: DesktopEntry | None = None
    text = marker.get("desktop")
    if isinstance(text, str) and len(text) <= _MAX_MARKER_DESKTOP:
        parsed = DesktopEntry.parse(display_text(text))
        entry = parsed if parsed.has_group(DesktopEntry.MAIN) else None
    name = _marker_text(marker, "name") or app_id
    sha = marker.get("sha256")
    categories = marker.get("categories")
    desktop_filename = _marker_text(marker, "desktop_filename")
    work_dir = Path(tempfile.mkdtemp(prefix="easy-installer-"))
    try:
        os.chmod(work_dir, 0o700)
        icon_path, icon_info = _marker_icon_copy(folder, marker, work_dir)
        info = PortableInfo(
            path=folder, size=0,
            sha256=sha if isinstance(sha, str) and _SHA256_RE.match(sha) else None,
            app_id=app_id, name=name, display_name=name,
            version=_marker_text(marker, "version", 64), comment=_marker_text(marker, "comment", 500),
            categories=[display_text(c) for c in categories
                        if isinstance(c, str) and c and not _CONTROL_RE.search(c)]
            if isinstance(categories, list) else [],
            terminal=marker.get("terminal") is True, strip_prefix=None, desktop_entry=entry,
            desktop_filename=desktop_filename if desktop_filename and "/" not in desktop_filename
            and desktop_filename.endswith(".desktop") else None,
            executables=[ExecutableCandidate(
                relpath=executable, kind="elf" if head == b"\x7fELF" else "script", score=0)],
            executable=executable, icon_path=icon_path, icon_info=icon_info,
            is_electron=marker.get("is_electron") is True, arch=_marker_arch(marker),
            tree_size=_marker_int(marker, "tree_size"), file_count=_marker_int(marker, "file_count"),
            work_dir=work_dir, warnings=[],
            exec_has_no_sandbox=marker.get("exec_has_no_sandbox") is True,
            wm_class=_marker_text(marker, "wm_class", 100),
            origin_url=clean_url(marker.get("origin_url")),
        )
    except BaseException:
        remove_tree(work_dir)
        raise
    # (not a field of PortableInfo: the name of the archive the folder was unpacked from)
    info.__dict__["original_filename"] = _marker_text(marker, "original_filename", 255)
    return adopt_work_dir(info)


def _scaled(report: ProgressCallback, start: float, end: float) -> ProgressCallback:
    def scaled(fraction: float | None, message: str) -> None:
        report(None if fraction is None
               else start + (end - start) * min(max(fraction, 0.0), 1.0), message)

    return scaled


def _folder_in_the_way(path: Path, details: str) -> InstallError:
    return InstallError(
        _("A folder is in the way where the app should be installed: {path}").format(
            path=display_text(str(path))), details=details)


def _aside_name(folder: Path) -> Path:
    return folder.with_name(f".{folder.name}.easyinstaller-old-{secrets.token_hex(4)}")


def _saved_files_reference(plan: InstallPlan, old: Path,
                           new_manifest: appfolder.Manifest | None) -> appfolder.Manifest | None:
    """What was unpacked into the folder ``old`` that is being replaced - so that everything
    else in it (what the app or the user saved there) can be taken over. None: unknown."""
    marker = appfolder.read_marker(old)
    manifest = appfolder.read_manifest(old, marker)
    if manifest is not None:
        return manifest
    # Unpacked by Easy Installer 0.2.0, which kept no record. When the very same archive is
    # installed again, the new folder is exactly what was unpacked there.
    sha = plan.portable.sha256 if plan.portable is not None else None
    if new_manifest is not None and plan.source_dir is None and sha \
            and isinstance(marker, dict) and marker.get("sha256") == sha:
        return new_manifest
    return None


def _kept_folder_note(name: str, result: str, where: Path | None) -> str | None:
    if result == appfolder.TRASHED:
        return _("Some files in the folder of {name} were changed after it was installed. The "
                 "old folder is in the trash, in case you need them.").format(name=name)
    if result == appfolder.KEPT:
        return _("Some files in the folder of {name} were changed after it was installed, so "
                 "the old folder was kept: {path}").format(
                     name=name, path=display_text(str(where or "")))
    return None


def _dispose_replaced(plan: InstallPlan, folder: Path, warnings: list[str]) -> None:
    """The folder of the version that was replaced and is not kept: deleted, unless the app or
    the user saved something in it that did not go over into the new folder."""
    existing = plan.existing
    name = existing.name if existing is not None else plan.name
    visible = plan.layout.apps_dir / backups.kept_folder_name(
        name, existing.version if existing is not None else None,
        existing.sha256 if existing is not None else None)
    result, where = appfolder.dispose(folder, visible=visible)
    note = _kept_folder_note(plan.name, result, where)
    if note:
        warnings.append(note)


def _swap_in_portable(plan: InstallPlan, registry: Registry, tx: _Transaction,
                      warnings: list[str], report: ProgressCallback) -> backups.Stash | None:
    """Put the new app folder in place of the installed one.

    The new tree is complete (unpacked next to its destination, record and marker written)
    before anything that is installed moves. The installed folder is only ever touched when it
    is verifiably the app's own (:func:`portable_dir_state`); it becomes the kept previous
    version, or is set aside and disposed of once everything is saved. What the app or the
    user saved in it - everything that was not unpacked there - is taken over into the new
    folder (the new version's own files win). Returns the backup made.
    """
    layout, info, install_dir = plan.layout, plan.portable, plan.install_dir
    assert info is not None and install_dir is not None
    carried: list[tuple[str, str]] = []    # what was taken over from the old folder ...
    stuck: list[str] = []                  # ... and could not be put back after a failure
    new_manifest: appfolder.Manifest | None = None
    if plan.source_dir is not None:
        # going back: the kept version itself takes the app's place
        new_tree = Path(plan.source_dir)
        marker = read_portable_marker(new_tree)
        if not backups.is_backup_path(layout, plan.app_id, os.fspath(new_tree)) \
                or not _own_real_dir(new_tree) or marker is None \
                or marker.get("id") != plan.app_id \
                or not os.path.isfile(new_tree / info.executable):
            raise InstallError(
                _("The kept previous version of {name} cannot be used any more.").format(
                    name=plan.name), details=f"{new_tree} is not a kept version of {plan.app_id}")
    else:
        new_tree = install_dir.with_name(
            f".{install_dir.name}.easyinstaller-new-{secrets.token_hex(4)}")

        def drop_new_tree() -> None:
            if stuck:   # files the app saved are in there: never delete them
                _dispose_replaced(plan, new_tree, [])
            else:
                remove_tree(new_tree)

        tx.on_rollback(f"remove {new_tree}", drop_new_tree)
        extract_portable(info, new_tree, progress=_scaled(report, 0.02, 0.8))
        new_manifest = _write_portable_marker(new_tree, plan)
        _fsync_dir(new_tree)

    stash: backups.Stash | None = None
    old_place: Path | None = None
    if os.path.lexists(install_dir):
        existing = plan.existing
        ours = existing is not None and bool(existing.install_dir) \
            and os.path.normpath(existing.install_dir) == os.path.normpath(install_dir)
        state, details = portable_dir_state(layout, existing, registry) if ours \
            else (PORTABLE_UNSAFE, f"{install_dir} is not the folder of {plan.app_id}")
        if state != PORTABLE_OK:
            raise _folder_in_the_way(install_dir, details)
        stash = _stash_previous(plan, tx, warnings)   # the whole folder moves to the backups
        if stash is None:
            # Not kept: out of the way now, disposed of when the new version is saved.
            aside = old_place = _aside_name(install_dir)
            os.rename(install_dir, aside)
            tx.on_rollback(f"restore {install_dir}", lambda: os.rename(aside, install_dir))
        else:
            old_place = stash.path
    _rename(new_tree, install_dir)
    tx.on_rollback(f"move {install_dir} back", lambda: os.rename(install_dir, new_tree))
    _fsync_dir(install_dir.parent)
    if old_place is not None:
        # Only now, with the new folder in its place: a crash never leaves the app's files in
        # a half unpacked folder (those are deleted by the next installation).
        reference = _saved_files_reference(plan, old_place, new_manifest)
        if reference is not None:
            carried.extend(appfolder.carry_over(old_place, install_dir, reference))
            tx.on_rollback("put back what was taken over",
                           lambda: stuck.extend(appfolder.undo_carry_over(carried)))
        if stash is None:
            gone = old_place
            tx.on_commit(f"dispose of {gone}", lambda: _dispose_replaced(plan, gone, warnings))
    return stash


def _execute_portable_locked(plan: InstallPlan, registry: Registry,
                             report: ProgressCallback) -> InstalledApp:
    layout, info, install_dir = plan.layout, plan.portable, plan.install_dir
    if info is None or install_dir is None:
        raise InstallError(_("The app could not be installed because a file could not be "
                             "copied or saved."), details="portable plan without archive/folder")
    existing = plan.existing
    tx = _Transaction()
    warnings: list[str] = []
    stash: backups.Stash | None = None

    try:
        for directory in (layout.apps_dir, layout.desktop_dir,
                          plan.icon_target.parent if plan.icon_target else None):
            if directory is not None:
                directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

        if plan.in_place:
            # Repair: the folder stays exactly as it is.
            state, details = portable_dir_state(layout, existing, registry) \
                if existing is not None else (PORTABLE_UNSAFE, "not installed")
            if state != PORTABLE_OK:
                raise _incomplete_folder(install_dir, details)
        else:
            stash = _swap_in_portable(plan, registry, tx, warnings, report)
        program = plan.target_appimage
        if not _is_within(program, install_dir) or not os.path.isfile(program):
            raise InstallError(
                _("The program that starts this app was not found in the archive."),
                details=f"{program} is missing after unpacking")

        icon_paths: list[str] = []
        if plan.icon_target is not None and info.icon_path is not None:
            report(0.85, _("Adding the app icon…"))
            tx.backup(plan.icon_target)
            _copy_icon(info.icon_path, plan.icon_target)
            tx.created(plan.icon_target)
            icon_paths.append(str(plan.icon_target))
        elif existing is not None and plan.icon_name == existing.icon_name:
            icon_paths = list(existing.icon_paths)   # the installed icon stays

        report(0.88, _("Adding the app to the menu…"))
        tx.backup(plan.desktop_path)
        _write_desktop_file(plan.desktop_path, plan.desktop_text)
        tx.created(plan.desktop_path)

        report(0.95, _("Saving…"))
        now = utc_now()
        app = InstalledApp(
            id=plan.app_id,
            name=plan.name,
            version=plan.version,
            scope=Scope.USER,
            appimage_path=str(program),
            desktop_path=str(plan.desktop_path),
            icon_paths=icon_paths,
            icon_name=plan.icon_name,
            apparmor_profile=None,
            sandbox_fix=plan.sandbox_fix.value,
            extract_and_run=False,
            extract_and_run_explicit=False,
            sha256=info.sha256,
            size=info.tree_size or (existing.size if existing is not None and plan.in_place else 0),
            arch=info.arch or "",
            update_info=None,
            original_filename=_original_filename(plan),
            comment=info.comment,
            installed_at=existing.installed_at if existing and existing.installed_at else now,
            updated_at=now,
            installer_version=__version__,
            mime_package=None,
            kind=KIND_PORTABLE,
            install_dir=str(install_dir),
            mtime_ns=_mtime_ns(program),
            previous=_new_previous(plan, stash, None),
            **recorded_extras(plan),
        )
        registry.put(app)
    except BaseException as exc:
        tx.rollback()
        if isinstance(exc, OSError):
            raise _os_error(exc) from exc
        raise

    tx.commit()
    _remove_previous_files(plan, app, registry)
    _drop_obsolete_backup(plan, app)
    if not plan.options.keep_original and plan.source_dir is None and not plan.in_place:
        # Asked for explicitly: the archive is deleted now that the app is unpacked.
        try:
            _unlink_source(info.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete the original file %s: %s", info.path, exc)
            warnings.append(_(
                "The app was installed, but the original file could not be deleted. You can "
                "delete {name} yourself.").format(name=display_name(info.path)))

    report(0.98, _("Updating the app menu…"))
    refresh_desktop_caches(layout, mime=bool(existing is not None and existing.mime_package))
    _validate_desktop_file(plan.desktop_path)
    for warning in warnings:
        if warning not in plan.warnings:
            plan.warnings.append(warning)
    report(1.0, _("Done"))
    return app


# ------------------------------------------------------------------------------------------------
# execute: system scope
# ------------------------------------------------------------------------------------------------


MOUNTINFO = "/proc/self/mountinfo"
STAGING_PREFIX = "staging-"
STAGING_MAX_AGE = 24 * 3600
_MOUNTINFO_ESCAPE_RE = re.compile(rb"\\([0-7]{3})")


def mount_of(path: Path | str, mountinfo: str | None = None) -> tuple[str, str, frozenset[str]] | None:
    """(mount point, file system type, super options) of the mount holding ``path``, or None."""
    try:
        real = os.path.realpath(path)
        with open(mountinfo or MOUNTINFO, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    best: tuple[str, str, frozenset[str]] | None = None
    for line in data.splitlines():
        fields = line.split(b" ")
        try:
            sep = fields.index(b"-", 6)
        except ValueError:
            continue
        if len(fields) < sep + 4:
            continue
        point = os.fsdecode(_MOUNTINFO_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 8)]),
                                                     fields[4]))
        if not (point == "/" or real == point or real.startswith(point.rstrip("/") + "/")):
            continue
        # The longest mount point wins; of equal ones the later (it hides the earlier one).
        if best is None or len(point) >= len(best[0]):
            best = (point, os.fsdecode(fields[sep + 1]),
                    frozenset(os.fsdecode(fields[sep + 3]).split(",")))
    return best


def on_private_fuse_mount(path: Path | str) -> bool:
    """``path`` is on a FUSE file system that only its owner may enter (no ``allow_other``).

    gvfs network shares (/run/user/<uid>/gvfs), the document portal, sshfs, rclone, ... The
    kernel refuses every other process there - including the root helper, which only changes
    its effective user id to the caller's - so such files are copied to a local folder first.
    """
    mount = mount_of(path)
    if mount is None:
        return False
    _point, fstype, options = mount
    is_fuse = fstype in ("fuse", "fuseblk") or fstype.startswith("fuse.")
    return is_fuse and "allow_other" not in options


def _staging_base() -> Path:
    for base in (cache_home() / "easy-installer", Path("/var/tmp"), Path(tempfile.gettempdir())):
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(base, os.W_OK | os.X_OK) and not on_private_fuse_mount(base):
            return base
    raise InstallError(_("The app could not be installed because a file could not be copied or saved."),
                       details="no local folder for a temporary copy")


def _remove_stale_staging(base: Path) -> None:
    """Temporary copies left behind by an interrupted installation (e.g. a crash or logout)."""
    try:
        entries = list(os.scandir(base))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        try:
            if entry.name.startswith(STAGING_PREFIX) and entry.is_dir(follow_symlinks=False) \
                    and now - entry.stat(follow_symlinks=False).st_mtime > STAGING_MAX_AGE:
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


def _stage_source(plan: InstallPlan, report: ProgressCallback) -> tuple[Path, Path, str]:
    """Copy the AppImage to a private local folder; returns (folder, copy, sha256)."""
    base = _staging_base()
    if base.name == "easy-installer":
        _remove_stale_staging(base)
    try:
        folder = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=base))  # mode 0700
    except OSError as exc:
        raise _os_error(exc) from exc
    copy = folder / "source.AppImage"
    try:
        sha = _copy_with_progress(plan.info.path, copy, report, _("Copying the app…"), 0.0, 0.5)
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise _os_error(exc) from exc
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if plan.info.sha256 and sha != plan.info.sha256:
        shutil.rmtree(folder, ignore_errors=True)
        raise InstallError(
            _("The file {name} changed while it was being installed. Please try again.").format(
                name=display_name(plan.info.path)),
            details=f"sha256 {plan.info.sha256} -> {sha}")
    return folder, copy, sha


def build_manifest(plan: InstallPlan, sha256: str, source: Path | None = None) -> dict:
    """The helper's install request; ``source``: a local copy to install instead of info.path."""
    info = plan.info
    # A kept copy ("keep both") is sent with its own id and name; its launcher names already
    # carry the version, so the helper renders it like any other app.
    embedded = _embedded_entry(info, plan.name_suffix)
    backup_days = load_settings().backup_days
    extras = recorded_extras(plan)
    return {
        "app_id": plan.app_id,
        "name": plan.name,
        "version": plan.version,
        "comment": info.comment,
        "source_appimage": str(source or info.path),
        "sha256": sha256,
        "embedded_desktop": embedded.to_text() if embedded is not None else None,
        "embedded_desktop_filename": info.desktop_filename,
        "icon_source": str(info.icon_path) if info.icon_path is not None else None,
        "extra_args": [NO_SANDBOX_ARG] if plan.sandbox_fix is SandboxFix.NO_SANDBOX else [],
        "extract_and_run": bool(plan.extract_and_run),
        "extract_and_run_explicit": plan.options.extract_and_run is not None,
        "apparmor": plan.sandbox_fix is SandboxFix.APPARMOR,
        "uninstall_command": list(plan.uninstall_command) if plan.uninstall_command else None,
        "arch": info.arch,
        "size": info.size,
        "update_info": info.update_info,
        "original_filename": _original_filename(plan) or None,   # ("" is refused)
        "is_electron": bool(info.is_electron),
        "mime_types": [definition.to_dict() for definition in plan.mime_types],
        # 0.2: the replaced version is kept (the helper decides whether there is one), going
        # back uses up the kept one, old system backups are pruned on the way
        "keep_backup": bool(plan.keep_backup),
        "consume_backup": bool(plan.consumes_backup),
        # (0 = off: every kept system version goes; a 0.2.0 helper takes it as 1 day)
        "backup_max_age_days": backup_days,
        "base_id": plan.base_id,
        "pinned": bool(plan.pinned),
        "update_source": helper_rules.storable_dict(extras["update_source"]),
        "origin_url": helper_rules.storable_url(extras["origin_url"]),
        "signature": helper_rules.storable_dict(extras["signature"]),
        "data_hints": helper_rules.storable_hints(extras["data_hints"]),
    }


def _add_helper_warnings(plan: InstallPlan, warnings: object) -> None:
    """Show the helper's notes (e.g. the AppArmor fallback) like the user-scope ones.

    The helper runs without the user's locale, so its English texts are translated here.
    """
    if not isinstance(warnings, list):
        return
    for warning in warnings:
        if not isinstance(warning, str) or not warning.strip():
            continue
        text = _(warning)
        if text == _("The app could not get the special permission it needs, so it will be "
                     "started without its built-in security sandbox."):
            with contextlib.suppress(ValueError):
                plan.warnings.remove(_apparmor_note())
        if text not in plan.warnings:
            plan.warnings.append(text)


def _execute_system(plan: InstallPlan, report: ProgressCallback) -> InstalledApp:
    _check_source(plan)
    sha = plan.info.sha256
    staging: Path | None = None
    helper_source: Path | None = None
    try:
        if on_private_fuse_mount(plan.info.path) or not _is_utf8_path(plan.info.path):
            # The helper cannot open the file there (or receive its name as text).
            log.info("installing a local copy of %s", plan.info.path)
            staging, helper_source, sha = _stage_source(plan, report)
        elif not sha:
            report(None, _("Checking the file…"))
            try:
                sha = _file_sha256(plan.info.path)
            except OSError as exc:
                raise _os_error(exc) from exc
        report(None, _("Installing for everyone on this computer…"))
        result = privileged.run_helper("install", build_manifest(plan, sha, helper_source))
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    try:
        app = InstalledApp.from_dict(result["app"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HelperError(_("The administrator task failed unexpectedly."),
                          details=f"unexpected helper result: {result!r}") from exc
    _add_helper_warnings(plan, result.get("warnings"))

    source = plan.info.path
    if not plan.options.keep_original and not _is_within(source, plan.layout.apps_dir) \
            and not _same_file_path(source, app.appimage_path):
        try:
            _unlink_source(source)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete the original file %s: %s", source, exc)
            plan.warnings.append(_(
                "The app was installed, but the original file could not be deleted. You can "
                "delete {name} yourself.").format(name=display_name(source)))
    report(1.0, _("Done"))
    return app


def execute_install(plan: InstallPlan, *, progress: ProgressCallback | None = None) -> InstalledApp:
    report = progress or _noop_progress
    # Another file than the installed one: an update that was found earlier is outdated.
    replaces = plan.existing is not None and not _same_content(plan)
    if plan.layout.scope is Scope.SYSTEM:
        app = _execute_system(plan, report)
    else:
        try:
            app = _execute_user(plan, report)
        except EasyInstallerError:
            raise
        except OSError as exc:
            raise _os_error(exc) from exc
    if replaces:
        _forget_cached_update(plan.layout.scope, app.id)
    return app


# ------------------------------------------------------------------------------------------------
# uninstall / listing
# ------------------------------------------------------------------------------------------------


def _safe_to_delete(path: str, *, kind: str, app: InstalledApp) -> bool:
    """Only delete files that look exactly like the ones Easy Installer creates."""
    if not path or not os.path.isabs(path):
        return False
    if os.path.isdir(path) and not os.path.islink(path):
        return False
    name = os.path.basename(path)
    if kind == "desktop":
        return name == desktop_file_name(app.id)
    if kind == "mime":
        return (name == mime_package_name(app.id)
                and os.path.basename(os.path.dirname(path)) == "packages")
    if kind == "icon":
        return name.startswith(make_icon_name(app.id) + ".")
    return name.endswith(".AppImage")


def _remove_portable_dir(layout: Layout, app: InstalledApp, registry: Registry,
                         notes: list[str], failures: list[str]) -> None:
    """Delete the folder of a portable app - only if it is verifiably the app's own.

    The recorded folder must lie directly in the apps folder, be a real folder of this user (no
    symbolic link, no mount point), carry the marker file with this app's id and contain
    nothing another installed app uses (:func:`portable_dir_state`). Anything else stays where
    it is, with a note for the user: a registry entry alone never gets a folder deleted.
    """
    state, details = portable_dir_state(layout, app, registry)
    if state == PORTABLE_MISSING:
        return
    left_alone = _("The folder {path} was not removed, because Easy Installer cannot be sure "
                   "that it belongs to {name}. You can delete it yourself.").format(
                       path=display_text(str(app.install_dir or "")), name=app.name)
    if state != PORTABLE_OK:
        log.warning("not deleting the folder of %s: %s", app.id, details)
        notes.append(left_alone)
        return
    folder = Path(app.install_dir)
    aside = _aside_name(folder)
    try:
        os.rename(folder, aside)
    except OSError as exc:
        log.warning("could not remove %s: %s", folder, exc)
        failures.append(f"{folder}: {exc}")
        return
    # What was checked is out of everybody's way now. Look once more, then delete it - or,
    # if the app or the user saved something in it, move it to the trash (under its own name).
    marker = read_portable_marker(aside)
    if not _own_real_dir(aside) or marker is None or marker.get("id") != app.id:
        log.warning("not deleting %s: it changed while it was being removed", folder)
        with contextlib.suppress(OSError):
            os.rename(aside, folder)
        notes.append(left_alone)
        return
    try:
        result, where = appfolder.dispose(aside, visible=folder)
    except OSError as exc:
        failures.append(f"{aside}: could not be removed completely ({exc})")
        return
    if result == appfolder.TRASHED:
        notes.append(_("The folder of {name} was moved to the trash, because files were saved "
                       "in it after it was installed. You can get them back from there.")
                     .format(name=app.name))
    elif result == appfolder.KEPT:
        notes.append(_("The folder {path} was kept, because files were saved in it after {name} "
                       "was installed. You can delete it yourself.").format(
                           path=display_text(str(where or folder)), name=app.name))
    else:
        log.info("removed the app folder %s", folder)


def _uninstall_user(app_id: str, layout: Layout, report: ProgressCallback,
                    keep_permission: bool) -> list[str]:
    registry = _registry(layout)
    with registry.locked(timeout=USER_LOCK_TIMEOUT, on_wait=lambda: report(
            None, _("Waiting for another installation to finish…"))):
        return _uninstall_user_locked(app_id, layout, registry, report, keep_permission)


def _uninstall_user_locked(app_id: str, layout: Layout, registry: Registry,
                           report: ProgressCallback, keep_permission: bool) -> list[str]:
    app = registry.get(app_id)
    if app is None:
        raise NotInstalledError(_("This app is not installed."), details=f"{app_id} ({layout.scope})")
    notes: list[str] = []

    if app.apparmor_profile and keep_permission:
        # Removing the profile needs an administrator (e.g. a standard user, or no polkit agent
        # over ssh): the app itself can still go.
        log.info("keeping AppArmor profile %s of %s as asked", app.apparmor_profile, app_id)
        notes.append(_("The special permission that {name} needed was left on this computer, "
                       "because only an administrator can remove it.").format(name=app.name))
    elif app.apparmor_profile:
        # First, while nothing is changed yet: a cancelled or refused password prompt cancels
        # the uninstall (AuthorizationError propagates; CLI and GUI then offer keep_permission).
        report(None, _("Removing the app's special permission…"))
        try:
            privileged.run_helper("apparmor-remove", {"app_id": app_id})
        except HelperError as exc:
            log.info("keeping AppArmor profile of %s: %s", app_id, exc.details or exc)
            notes.append(_("The special permission that {name} needed could not be removed.")
                         .format(name=app.name))

    report(0.1, _("Removing {name}…").format(name=app.name))
    failures: list[str] = []
    targets = [("desktop", app.desktop_path), ("mime", app.mime_package or "")]
    if app.kind == KIND_APPIMAGE:
        targets.insert(0, ("appimage", app.appimage_path))
    targets += [("icon", p) for p in app.icon_paths]
    shared = _used_by_others(registry, app_id)
    if app.kind == KIND_PORTABLE:
        # The program is part of the app's folder, which goes as a whole - or not at all.
        _remove_portable_dir(layout, app, registry, notes, failures)
    for kind, path in targets:
        if not _safe_to_delete(path, kind=kind, app=app):
            if path:
                log.warning("not deleting unexpected %s path %s", kind, path)
            continue
        if kind == "appimage" and os.path.realpath(path) in shared:
            log.warning("keeping %s: another installed app uses it, too", path)
            continue
        try:
            _unlink_missing_ok(path)
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
            failures.append(f"{path}: {exc}")
    if ID_RE.fullmatch(app_id):
        # The kept previous version goes with the app (copies kept next to it have their own).
        try:
            backups.remove_app_backups(layout, app_id)
        except OSError as exc:
            log.warning("could not delete the kept previous version of %s: %s", app_id, exc)
            failures.append(f"{backups.app_backup_dir(layout, app_id)}: {exc}")
    if failures:
        raise InstallError(_("Some files of {name} could not be removed.").format(name=app.name),
                           details="\n".join(failures))

    report(0.9, _("Updating the app menu…"))
    registry.remove(app_id)
    # Backups an interrupted update of this app left next to its files go with it.
    _clean_leftovers((Path(path).parent for kind, path in targets
                      if _safe_to_delete(path, kind=kind, app=app)), registry,
                     apps_dir=layout.apps_dir)
    _forget_cached_update(layout.scope, app_id)
    refresh_desktop_caches(layout, mime=bool(app.mime_package))
    report(1.0, _("Done"))
    return notes


def _forget_cached_update(scope: Scope, app_id: str) -> None:
    """An update that was found for an app that is gone (or replaced) must not be offered any
    more; what an interrupted download of it left behind goes, too."""
    try:
        UpdateCache().forget(Scope(scope).value, app_id)
    except Exception:  # noqa: BLE001 - the cache is a nicety
        log.debug("cannot update the update cache", exc_info=True)
    downloads.remove_leftovers(app_id)


def uninstall_needs_password(app: InstalledApp) -> bool:
    """Removing ``app`` runs the administrator helper (system app, or an AppArmor profile)."""
    return Scope(app.scope) is Scope.SYSTEM or bool(app.apparmor_profile)


def can_keep_permission(app: InstalledApp) -> bool:
    """A refused password prompt only concerns the permission: ``uninstall(keep_permission=True)``."""
    return Scope(app.scope) is Scope.USER and bool(app.apparmor_profile)


def uninstall(app_id: str, scope: Scope, *, progress: ProgressCallback | None = None,
              keep_permission: bool = False) -> list[str]:
    """Remove an app; returns translated notes (e.g. what could not be removed).

    ``keep_permission``: remove a per-user app without asking for the administrator password,
    leaving its AppArmor profile (for when that password cannot be given).
    """
    report = progress or _noop_progress
    scope = Scope(scope)
    layout = layout_for(scope)
    if scope is Scope.USER:
        return _uninstall_user(app_id, layout, report, keep_permission)
    if _registry(layout).get(app_id) is None:
        raise NotInstalledError(_("This app is not installed."), details=f"{app_id} (system)")
    report(None, _("Removing the app for everyone on this computer…"))
    privileged.run_helper("uninstall", {"app_id": app_id})
    _forget_cached_update(scope, app_id)
    report(1.0, _("Done"))
    return []


# ------------------------------------------------------------------------------------------------
# settings and data an app leaves behind
# ------------------------------------------------------------------------------------------------


_MAX_LAUNCHER_SIZE = 1024 * 1024


def _launcher_wm_class(app: InstalledApp) -> str | None:
    """``StartupWMClass`` of the app's installed launcher (what the app calls itself)."""
    try:
        with open(app.desktop_path, "rb") as fh:
            data = fh.read(_MAX_LAUNCHER_SIZE + 1)
        if len(data) > _MAX_LAUNCHER_SIZE:
            return None
        entry = DesktopEntry.parse(data.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    return entry.get("StartupWMClass") or None


def data_hints_of(app: InstalledApp) -> list[str]:
    """The names under which ``app`` may keep its settings and data: what was recorded when
    it was installed; for entries made by 0.1 they are worked out from the app's name, its id
    and the ``StartupWMClass`` of its launcher."""
    hints = [hint for hint in app.data_hints if isinstance(hint, str) and hint]
    if hints:
        return hints
    try:
        return appdata.data_hints_for(name=app.name, app_id=app.main_id, desktop_stem=None,
                                      wm_class=_launcher_wm_class(app), exec_name=None)
    except Exception:  # noqa: BLE001
        log.warning("cannot work out the data folder names of %s", app.id, exc_info=True)
        return []


def other_installations(app: InstalledApp) -> list[InstalledApp]:
    """Every other installed version of the same app: in the other scope, and copies kept
    next to it ("keep both") in either scope."""
    scope = Scope(app.scope)
    return [other for other in list_installed()
            if other.main_id == app.main_id
            and not (Scope(other.scope) is scope and other.id == app.id)]


def removable_data(app: InstalledApp) -> list[DataLocation]:
    """The settings and data folders of ``app`` that may be offered for removal when it is
    uninstalled (DESIGN section 24) - each with its kind, size and number of files.

    Nothing is offered while another installation of the same app remains (the other scope, a
    copy kept next to it): that one still uses the data. Neither is a folder that another
    installed app looks for under the same name (the same program installed from another
    kind of file has another id). Works before and after the uninstall
    (``app`` itself does not count). The folders come from ``core.appdata.find_app_data``:
    only direct children of the user's config/data/cache/state folders and ``~/.<name>``,
    never the app's own files, its kept previous version or anything of Easy Installer.
    Nothing is deleted here: pass what the user ticked to ``appdata.move_to_trash``.
    """
    try:
        if other_installations(app):
            return []
        locations = app_data(app)
        if not locations:
            return []
        # Another app (the same program installed from another kind of file, e.g. its
        # AppImage next to its archive) that looks for a folder of the same name uses it, too.
        scope = Scope(app.scope)
        used = {hint.casefold() for other in list_installed()
                if not (Scope(other.scope) is scope and other.id == app.id)
                for hint in data_hints_of(other)}
    except Exception:  # noqa: BLE001 - an offer that cannot be made is simply not made
        log.warning("cannot look for the other installations of %s", app.id, exc_info=True)
        return []
    return [location for location in locations
            if location.path.name.removeprefix(".").casefold() not in used]


def app_data(app: InstalledApp) -> list[DataLocation]:
    """The settings and data folders of ``app`` with their sizes, also while other
    installations of the same app share them (to *show* them, e.g. in its details; what may be
    offered for removal is :func:`removable_data`). Never raises."""
    try:
        hints = data_hints_of(app)
        if not hints:
            return []
        layout = layout_for(Scope(app.scope))
        user = layout_for(Scope.USER)
        exclude = [Path(path) for path in (app.install_dir, app.appimage_path, app.desktop_path,
                                           (app.previous or {}).get("path")) if path]
        exclude += [layout.apps_dir, user.apps_dir, user.backups_dir, user.registry_dir]
        return appdata.find_app_data(hints, exclude=exclude)
    except Exception:  # noqa: BLE001 - what cannot be found is simply not shown
        log.warning("cannot look for the data of %s", app.id, exc_info=True)
        return []


# ------------------------------------------------------------------------------------------------
# the kept previous version: going back, deleting, pruning
# ------------------------------------------------------------------------------------------------


def _no_previous(app: InstalledApp) -> InstallError:
    return InstallError(
        _("There is no previous version of {name} to go back to.").format(name=app.name),
        details=f"{app.id} ({Scope(app.scope).value}): previous = {app.previous!r}")


def plan_rollback(app_id: str, scope: Scope, *, status: SystemStatus | None = None) -> InstallPlan:
    """The plan that swaps an app with its kept previous version (``plan.action == "rollback"``).

    The kept file is inspected like any AppImage; the installation's choices (sandbox fix,
    start mode) are taken over, and the current version becomes the new ``previous``. The
    caller owns ``plan.info`` (call ``plan.info.cleanup()``).
    """
    scope = Scope(scope)
    layout = layout_for(scope)
    app = _registry(layout).get(app_id)
    if app is None:
        raise NotInstalledError(_("This app is not installed."), details=f"{app_id} ({scope.value})")
    previous = backups.usable_previous(layout, app)
    if previous is None:
        raise _no_previous(app)
    unusable = _("The kept previous version of {name} cannot be used any more.").format(
        name=app.name)
    if app.kind == KIND_PORTABLE:
        return _plan_portable_rollback(app, previous, unusable, status)
    if _changed_itself(app):
        # The current version is kept by going back: it must be what the entry names.
        raise backups.file_changed_error(app)
    try:
        info = inspect_appimage(previous["path"])
    except EasyInstallerError as exc:   # damaged, or not an app at all
        raise InstallError(unusable, details=f"{previous['path']}: {exc.details or exc}") from exc
    try:
        if info.app_id != app.main_id:
            raise InstallError(
                unusable, details=f"{previous['path']} is {info.app_id!r}, expected {app.main_id!r}")
        options = options_from_app(app, keep_original=False, keep_backup=True)
        options.allow_foreign_arch = True   # it was installed before: never refused now
        plan = _make_plan(info, options, status=status, entry=app, action=ACTION_ROLLBACK,
                          version_hint=previous.get("version"))
        plan.consumes_backup = True
        return plan
    except BaseException:
        info.cleanup()
        raise


def _plan_portable_rollback(app: InstalledApp, previous: dict, unusable: str,
                            status: SystemStatus | None) -> InstallPlan:
    """The kept folder of a portable app takes the place of the installed one (they swap)."""
    folder = Path(previous["path"])
    try:
        info = portable_info_from_folder(folder, app.id)
    except EasyInstallerError as exc:
        raise InstallError(unusable, details=exc.details or str(exc)) from exc
    try:
        # What the registry recorded when the version was kept counts; the marker only fills in.
        info.version = previous.get("version") or info.version
        info.sha256 = previous.get("sha256") or info.sha256
        info.tree_size = int(previous.get("size") or 0) or info.tree_size
        options = options_from_app(app, keep_original=True, keep_backup=True)
        options.allow_foreign_arch = True   # it was installed before: never refused now
        plan = _make_portable_plan(info, options, status=status, entry=app,
                                   action=ACTION_ROLLBACK, source_dir=folder)
        plan.consumes_backup = True
        return plan
    except BaseException:
        info.cleanup()
        raise


def _cannot_repair(app: InstalledApp, details: str | None) -> InstallError:
    return InstallError(
        _("{name} cannot be repaired automatically. Please install it again.").format(
            name=app.name), details=details)


def plan_repair(app_id: str, scope: Scope, *, status: SystemStatus | None = None) -> InstallPlan:
    """The plan that makes the launcher, the icon and the registry entry of an installed app
    again from the app itself ("Repair"): its AppImage is inspected anew - which also fills in
    what entries made by 0.1 lack (update source, signature, data folder names) -, a portable
    app is described by the marker file in its folder. The app's files stay where they are and
    the choices of the installation are kept. The caller owns ``plan.info``
    (``plan.info.cleanup()``).
    """
    scope = Scope(scope)
    layout = layout_for(scope)
    registry = _registry(layout)
    app = registry.get(app_id)
    if app is None:
        raise NotInstalledError(_("This app is not installed."), details=f"{app_id} ({scope.value})")
    if scope is Scope.USER:
        recover_interrupted(scope)   # an update that was stopped hard set the file aside
        app = registry.get(app_id) or app
    options = options_from_app(app, keep_original=True, keep_backup=False)
    options.allow_foreign_arch = True   # it is installed: never refuse to repair it
    if app.kind == KIND_PORTABLE:
        state, details = portable_dir_state(layout, app, registry)
        if state != PORTABLE_OK:
            raise _cannot_repair(app, details)
        try:
            portable = portable_info_from_folder(app.install_dir, app.id)
        except EasyInstallerError as exc:
            raise _cannot_repair(app, exc.details or str(exc)) from exc
        try:
            portable.version = portable.version or app.version
            return _make_portable_plan(portable, options, status=status, entry=app, in_place=True)
        except BaseException:
            portable.cleanup()
            raise
    if app.kind != KIND_APPIMAGE:
        raise _cannot_repair(app, f"unknown kind {app.kind!r}")
    info = inspect_appimage(app.appimage_path)
    try:
        if info.app_id != app.main_id:
            raise _cannot_repair(app, f"app id changed: {app.main_id!r} -> {info.app_id!r}")
        return _make_plan(info, options, status=status, entry=app)
    except BaseException:
        info.cleanup()
        raise


def repair(app_id: str, scope: Scope, *, progress: ProgressCallback | None = None) -> InstalledApp:
    """Make the launcher, icon and registry entry of an installed app again (:func:`plan_repair`)."""
    plan = plan_repair(app_id, scope)
    try:
        return execute_install(plan, progress=progress)
    finally:
        plan.info.cleanup()


def rollback(app_id: str, scope: Scope, *, progress: ProgressCallback | None = None) -> InstalledApp:
    """Go back to the kept previous version; the current one is kept in its place."""
    plan = plan_rollback(app_id, scope)
    try:
        return execute_install(plan, progress=progress)
    finally:
        plan.info.cleanup()


def drop_backup(app_id: str, scope: Scope) -> None:
    """Delete the kept previous version of an app (system-wide apps: through the helper)."""
    scope = Scope(scope)
    layout = layout_for(scope)
    registry = _registry(layout)
    app = registry.get(app_id)
    if app is None:
        raise NotInstalledError(_("This app is not installed."), details=f"{app_id} ({scope.value})")
    if not app.previous:
        return
    if scope is Scope.SYSTEM:
        privileged.run_helper(DROP_BACKUP_OP, {"app_id": app_id})
        return
    with registry.locked(timeout=USER_LOCK_TIMEOUT):
        app = registry.get(app_id)
        if app is None or not app.previous:
            return
        try:
            backups.remove_backup(layout, app.id, app.previous.get("path"))
        except OSError as exc:
            raise InstallError(
                _("The previous version of {name} could not be deleted.").format(name=app.name),
                details=f"{app.previous.get('path')}: {exc}") from exc
        registry.put(dataclasses.replace(app, previous=None))


def prune_backups(max_age_days: int, scopes: Iterable[Scope] = (Scope.USER,)) -> list[InstalledApp]:
    """Delete kept previous versions older than ``max_age_days`` (0: all of them).

    Returns the registry entries that lost their backup. Only per-user backups are deleted
    here; those of system-wide apps are pruned by the helper during its next installation.
    """
    pruned: list[InstalledApp] = []
    for scope in scopes:
        if Scope(scope) is not Scope.USER:
            continue
        layout = layout_for(Scope.USER)
        try:
            pruned += backups.prune(layout, _registry(layout), max_age_days)
        except OSError as exc:
            log.warning("could not prune the kept previous versions: %s", exc)
    return pruned


def can_start_directly(app: InstalledApp, status: SystemStatus) -> bool:
    """The app's AppImage could now start without unpacking itself (FUSE works for its runtime)."""
    try:
        return status.can_run_appimage(read_elf_info(app.appimage_path))
    except EasyInstallerError:
        return False


def switches_start_mode(app: InstalledApp) -> bool:
    """``app`` unpacks itself only because FUSE was missing (not because it was asked to)."""
    return app.extract_and_run and not app.extract_and_run_explicit


def _without_extract_and_run(name: str, value: str) -> bool:
    return name != "APPIMAGE_EXTRACT_AND_RUN"


def update_start_mode(app: InstalledApp, status: SystemStatus | None = None) -> InstalledApp | None:
    """Stop unpacking a per-user app on every start once FUSE works (e.g. right after installing it).

    Rewrites the launcher without ``APPIMAGE_EXTRACT_AND_RUN=1`` and updates the registry. Only
    for per-user apps (system launchers belong to root: they are repaired through the helper)
    whose start mode was chosen automatically, never for an explicit ``--extract-and-run yes``.
    Returns the updated entry, or None if nothing had to change.
    """
    status = status or get_system_status()
    if not switches_start_mode(app) or Scope(app.scope) is not Scope.USER \
            or app.status() != STATUS_OK or not can_start_directly(app, status):
        return None
    layout = layout_for(Scope.USER)
    registry = _registry(layout)
    with registry.locked(timeout=USER_LOCK_TIMEOUT):
        current = registry.get(app.id)
        if current is None or not switches_start_mode(current) \
                or not _safe_to_delete(current.desktop_path, kind="desktop", app=current):
            return None
        path = Path(current.desktop_path)
        try:
            entry = DesktopEntry.parse(path.read_text(encoding="utf-8"))
            for group in entry.groups():
                value = entry.get("Exec", group)
                if value and exec_program(value) == current.appimage_path:
                    entry.set("Exec", rewrite_exec(value, current.appimage_path,
                                                   keep_env=_without_extract_and_run), group)
            _write_desktop_file(path, entry.to_text())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            log.warning("cannot update the launcher %s: %s", path, exc)
            return None
        updated = dataclasses.replace(current, extract_and_run=False, updated_at=utc_now())
        registry.put(updated)
    refresh_desktop_caches(layout)
    log.info("%s now starts without unpacking itself", app.id)
    return updated


def update_start_modes(status: SystemStatus | None = None) -> list[InstalledApp]:
    """:func:`update_start_mode` for every per-user app; returns the updated ones."""
    status = status or get_system_status()
    updated = []
    for app in _registry(layout_for(Scope.USER)).all():
        try:
            new = update_start_mode(app, status)
        except EasyInstallerError as exc:
            log.warning("cannot update the start mode of %s: %s", app.id, exc)
            continue
        if new is not None:
            updated.append(new)
    return updated


def list_installed(scopes: Iterable[Scope] = (Scope.USER, Scope.SYSTEM)) -> list[InstalledApp]:
    apps: list[InstalledApp] = []
    for scope in scopes:
        apps.extend(_registry(layout_for(Scope(scope))).all())
    return sorted(apps, key=lambda a: (a.name.casefold(), a.scope.value, a.id))


def find_installed(app_id: str) -> list[InstalledApp]:
    found = []
    for scope in (Scope.USER, Scope.SYSTEM):
        app = _registry(layout_for(scope)).get(app_id)
        if app is not None:
            found.append(app)
    return found


# ------------------------------------------------------------------------------------------------
# desktop integration tools
# ------------------------------------------------------------------------------------------------


def _run_tool(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=TOOL_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", cmd[0], exc)
        return None
    if proc.returncode != 0:
        log.warning("%s exited with %s: %s", " ".join(cmd), proc.returncode, proc.stderr.strip())
    return proc


def touch_icon_theme(icons_dir: Path) -> None:
    """Bump the mtime of the hicolor folder, like ``xdg-icon-resource`` does.

    Running programs (GNOME Shell, panels, GTK apps) only rescan an icon theme when the mtime of
    its top folder changes; an icon added to an existing size folder does not change it, so the
    new app would keep a generic icon until the next login.
    """
    try:
        if icons_dir.is_dir():
            os.utime(icons_dir)
    except OSError as exc:
        log.warning("cannot update the modification time of %s: %s", icons_dir, exc)


def _mime_tool() -> str | None:
    return shutil.which("update-mime-database")


def refresh_desktop_caches(layout: Layout, *, mime: bool = False) -> None:
    """``mime``: a MIME package was added or removed, rebuild ``layout.mime_dir`` as well."""
    try:
        status = get_system_status()
        if status.update_desktop_database and layout.desktop_dir.is_dir():
            _run_tool([status.update_desktop_database, str(layout.desktop_dir)])
        if status.icon_cache_tool and (layout.icons_dir / "icon-theme.cache").is_file():
            _run_tool([status.icon_cache_tool, "-f", "-t", str(layout.icons_dir)])
        touch_icon_theme(layout.icons_dir)
        tool = _mime_tool() if mime else None
        if tool and (layout.mime_dir / "packages").is_dir():
            _run_tool([tool, str(layout.mime_dir)])
    except Exception:  # caches are a nicety; never fail an install because of them
        log.warning("refreshing desktop caches failed", exc_info=True)


def _validate_desktop_file(path: Path) -> None:
    try:
        tool = get_system_status().desktop_file_validate
        if not tool:
            return
        proc = _run_tool([tool, str(path)])
        if proc is not None and proc.stdout.strip():
            log.info("desktop-file-validate %s:\n%s", path, proc.stdout.strip())
    except Exception:
        log.debug("desktop-file-validate failed", exc_info=True)
