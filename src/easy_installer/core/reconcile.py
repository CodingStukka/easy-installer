"""Keep the registry true when an app changes its own file (self-updaters).

Apps such as T3 Code or Anytype download a new version and replace their AppImage themselves -
some overwrite the file, some save the new one under another name and delete the old one.
Easy Installer notices that the file no longer is what it recorded and brings the registry
entry, the launcher and the icon up to date ("reconcile"):

==============  ===================================================================================
``unchanged``   the file is what was recorded
``recorded``    nothing changed; the file's modification time was stored (entries made by 0.1)
``updated``     the app replaced its own file: launcher, icon and registry entry were refreshed
``adopted``     the recorded file is gone, exactly one other AppImage in the apps folder is this
                app: the entry now belongs to that file
``missing``     the recorded file is gone (and nothing could be adopted)
``foreign``     the file is now another app, or not an AppImage at all: nothing was changed
``needs-admin`` a system-wide app changed; only "Repair" (with the password) can refresh it
==============  ===================================================================================

Only per-user installations are ever changed here, always under the registry lock and through
the installer's normal in-place path. Nothing asks for a password and no backup is made.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..errors import EasyInstallerError, InstallError
from ..i18n import _
from . import installer, updates
from .inspector import AppImageInfo, inspect_appimage
from .integration import compare_versions
from .paths import Scope
from .registry import KIND_APPIMAGE, InstalledApp, Registry

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float | None, str], None]

CHANGE_UNCHANGED = "unchanged"
CHANGE_RECORDED = "recorded"
CHANGE_UPDATED = "updated"
CHANGE_ADOPTED = "adopted"
CHANGE_MISSING = "missing"
CHANGE_FOREIGN = "foreign"
CHANGE_NEEDS_ADMIN = "needs-admin"

#: How many unregistered AppImages in the apps folder are looked at when a file is gone.
MAX_ADOPTION_CANDIDATES = 16

#: Files already found to be something else: (scope, id) -> (size, mtime_ns). They are not read
#: again on every check (e.g. each time the window gets the focus) until they change.
_known_foreign: dict[tuple[str, str], tuple[int, int]] = {}


@dataclass
class ReconcileResult:
    app: InstalledApp            # the entry as it is now
    change: str                  # one of the CHANGE_* values
    old_version: str | None
    new_version: str | None


def went_back(result: ReconcileResult) -> bool:
    """The app replaced its own file with an OLDER version - e.g. its updater installed a
    download it had made before a newer version was installed with Easy Installer."""
    old, new = result.old_version, result.new_version
    return result.change in (CHANGE_UPDATED, CHANGE_ADOPTED) and bool(old and new) \
        and compare_versions(new, old) < 0


def _result(app: InstalledApp, change: str, old: InstalledApp | None = None) -> ReconcileResult:
    before = old if old is not None else app
    return ReconcileResult(app=app, change=change, old_version=before.version,
                           new_version=app.version)


def _noop(fraction: float | None, message: str) -> None:
    pass


def _stat_regular(path: str) -> os.stat_result | None:
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _differs(app: InstalledApp, st: os.stat_result) -> bool:
    return st.st_size != app.size or bool(app.mtime_ns and st.st_mtime_ns != app.mtime_ns)


def needs_reconcile(app: InstalledApp) -> bool:
    """One ``stat``: the app file exists and is not what was recorded (size, or modification
    time once that is known). AppImages only."""
    if app.kind != KIND_APPIMAGE:
        return False
    st = _stat_regular(app.appimage_path)
    return st is not None and _differs(app, st)


# ------------------------------------------------------------------------------------------------
# the individual outcomes
# ------------------------------------------------------------------------------------------------


def _registry(scope: Scope) -> Registry:
    return Registry(installer.layout_for(scope).registry_path)


def _file_details(app: InstalledApp, info: AppImageInfo | None) -> dict:
    """What 0.2 records about an AppImage besides its version: where its updates come from,
    where it was downloaded from, its signature and the names of its data folders. An entry
    made by 0.1 lacks all of it; its (unchanged) file is looked at once - without running it and
    without reading it in full - when its modification time is recorded."""
    try:
        if info is not None:
            return installer.file_details(info)
        with inspect_appimage(app.appimage_path, compute_hash=False) as inspected:
            if inspected.app_id != app.main_id:
                return {}
            return installer.file_details(inspected)
    except (EasyInstallerError, OSError) as exc:
        log.info("cannot read the details of %s: %s", app.appimage_path,
                 getattr(exc, "details", None) or exc)
        return {}


def _missing_details(current: InstalledApp, details: dict) -> dict:
    """The details the entry does not have yet (what is recorded is never replaced here)."""
    changes: dict = {}
    if current.update_source is None and details.get("update_source"):
        changes["update_source"] = details["update_source"]
    if current.origin_url is None and details.get("origin_url"):
        changes["origin_url"] = details["origin_url"]
    if current.signature is None and details.get("signature"):
        changes["signature"] = details["signature"]
    if not current.data_hints and details.get("data_hints"):
        changes["data_hints"] = list(details["data_hints"])
    return changes


def _record(app: InstalledApp, st: os.stat_result,
            info: AppImageInfo | None = None) -> ReconcileResult:
    """Store the file's size and modification time; the app itself did not change. For an
    entry made by 0.1 the details that 0.2 knows about a file are filled in on the way."""
    details = _file_details(app, info)   # before the lock is taken: reading takes a moment
    registry = _registry(Scope.USER)
    with registry.locked(timeout=installer.UNATTENDED_LOCK_TIMEOUT):
        current = registry.get(app.id)
        if current is None or current.appimage_path != app.appimage_path \
                or current.sha256 != app.sha256:
            return _result(current or app, CHANGE_UNCHANGED)   # changed meanwhile: next time
        updated = dataclasses.replace(current, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                      **_missing_details(current, details))
        registry.put(updated)
    log.info("recorded the modification time of %s", app.appimage_path)
    return _result(updated, CHANGE_RECORDED, app)


def _foreign(app: InstalledApp, st: os.stat_result, why: str) -> ReconcileResult:
    # (info, not warning: the caller tells the user - the CLI shows warnings as they are)
    log.info("%s is no longer %s (%s); leaving everything as it is", app.appimage_path,
             app.main_id, why)
    _known_foreign[(Scope(app.scope).value, app.id)] = (st.st_size, st.st_mtime_ns)
    return _result(app, CHANGE_FOREIGN)


def _install_in_place(info: AppImageInfo, app: InstalledApp, path: Path,
                      report: ProgressCallback, before: os.stat_result) -> InstalledApp:
    """The installer's in-place path for the file ``path`` (``info``) as the app file of ``app``."""
    info.sha256 = info.sha256 or installer._file_sha256(info.path)
    after = os.stat(info.path)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise InstallError(_("The file {name} changed after it was checked. Please try again.")
                           .format(name=installer.display_name(info.path)),
                           details=f"{info.path} changed while it was read")
    info.size = after.st_size
    plan = installer.plan_refresh(info, app, path=path, version_hint=_announced_version(info, app))
    new = installer.execute_install(plan, progress=report)
    if new.mtime_ns != before.st_mtime_ns:
        # It was written again in the meantime: what was recorded may already be outdated.
        # Remember the state that was read, so that the next check looks at the file again.
        new = dataclasses.replace(new, mtime_ns=before.st_mtime_ns)
        Registry(plan.layout.registry_path).put(new)
    return new


def _announced_version(info: AppImageInfo, app: InstalledApp) -> str | None:
    """The version of a new file that does not tell its own (FreeCAD): the update Easy
    Installer found for the app, if this file is that update (same checksum, or - without
    one - same size). Else it stays unknown: never the old version's."""
    if info.version:
        return None
    try:
        update = updates.UpdateCache().get(Scope(app.scope).value, app.id)
    except Exception:  # noqa: BLE001 - the cache is a nicety
        return None
    if update is None or not update.version:
        return None
    if update.sha256:
        return update.version if update.sha256 == info.sha256 else None
    return update.version if update.size and update.size == info.size else None


def _refresh(app: InstalledApp, st: os.stat_result, report: ProgressCallback) -> ReconcileResult:
    """The file changed: the app updated itself - or the file is something else now."""
    key = (Scope(app.scope).value, app.id)
    if _known_foreign.get(key) == (st.st_size, st.st_mtime_ns):
        return _result(app, CHANGE_FOREIGN)
    _known_foreign.pop(key, None)
    try:
        info = inspect_appimage(app.appimage_path, compute_hash=False)
    except EasyInstallerError as exc:
        return _foreign(app, st, exc.details or str(exc))
    with info:
        if info.app_id != app.main_id:
            return _foreign(app, st, f"it is {info.app_id!r}")
        info.sha256 = installer._file_sha256(info.path)
        if app.sha256 and info.sha256 == app.sha256:
            return _record(app, st, info)   # only touched: the content is what was recorded
        new = _install_in_place(info, app, Path(app.appimage_path), report, st)
    log.info("%s updated itself: %s -> %s", app.id, app.version, new.version)
    return _result(new, CHANGE_UPDATED, app)


def _adoption_candidates(registry: Registry, apps_dir: Path) -> list[Path]:
    """Unregistered ``*.AppImage`` files directly in the apps folder, newest first."""
    registered = {os.path.realpath(entry.appimage_path) for entry in registry.all()
                  if entry.appimage_path}
    found: list[tuple[int, Path]] = []
    try:
        entries = list(os.scandir(apps_dir))
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if name.startswith(".") or not name.lower().endswith(".appimage"):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime_ns
        except OSError:
            continue
        if os.path.realpath(entry.path) in registered:
            continue
        found.append((mtime, Path(entry.path)))
    found.sort(key=lambda item: (-item[0], item[1].name))
    return [path for _mtime, path in found[:MAX_ADOPTION_CANDIDATES]]


def _adopt(app: InstalledApp, report: ProgressCallback) -> ReconcileResult | None:
    """The recorded file is gone: move the entry to the one unregistered AppImage in the apps
    folder that is this app (updaters that save the new version under a new name)."""
    layout = installer.layout_for(Scope.USER)
    matches: list[tuple[Path, AppImageInfo]] = []
    try:
        for path in _adoption_candidates(Registry(layout.registry_path), layout.apps_dir):
            try:
                info = inspect_appimage(path, compute_hash=False)
            except EasyInstallerError:
                continue
            if info.app_id == app.main_id:
                matches.append((path, info))
            else:
                info.cleanup()
        if len(matches) != 1:
            if matches:
                log.info("not adopting a file for %s: %d candidates", app.id, len(matches))
            return None
        path, info = matches[0]
        new = _install_in_place(info, app, path, report, os.stat(path))
    finally:
        for _path, info in matches:
            info.cleanup()
    log.info("%s now uses %s (was %s)", app.id, new.appimage_path, app.appimage_path)
    return _result(new, CHANGE_ADOPTED, app)


# ------------------------------------------------------------------------------------------------
# public API
# ------------------------------------------------------------------------------------------------


def _reconcile(app: InstalledApp, report: ProgressCallback) -> ReconcileResult:
    if app.kind != KIND_APPIMAGE:
        return _result(app, CHANGE_UNCHANGED)
    scope = Scope(app.scope)
    # What the registry says now (the caller's object may be older, e.g. a list in a window).
    app = _registry(scope).get(app.id) or app
    if app.kind != KIND_APPIMAGE:
        return _result(app, CHANGE_UNCHANGED)
    st = _stat_regular(app.appimage_path)
    if st is None:
        adopted = _adopt(app, report) if scope is Scope.USER else None
        return adopted or _result(app, CHANGE_MISSING)
    changed = _differs(app, st)
    if scope is Scope.SYSTEM:
        # The launcher and the registry belong to root: "Repair" refreshes them.
        return _result(app, CHANGE_NEEDS_ADMIN if changed else CHANGE_UNCHANGED)
    if changed:
        return _refresh(app, st, report)
    if not app.mtime_ns:
        return _record(app, st)
    return _result(app, CHANGE_UNCHANGED)


def reconcile_app(app: InstalledApp, *, progress: ProgressCallback | None = None) -> ReconcileResult:
    """Bring one installed app in line with its file. Problems (a busy registry, a file that is
    still being written, a full disk) leave everything as it is: ``"unchanged"``, tried again
    the next time."""
    try:
        return _reconcile(app, progress or _noop)
    except (EasyInstallerError, OSError) as exc:
        details = getattr(exc, "details", None) or exc
        log.warning("could not bring %s up to date with its file: %s", app.id, details)
        return _result(app, CHANGE_UNCHANGED)


def reconcile_all(scopes: Iterable[Scope] = (Scope.USER, Scope.SYSTEM)) -> list[ReconcileResult]:
    """:func:`reconcile_app` for every installed app. One broken app never stops the others.

    First, what an update that was stopped hard set aside is put back
    (``installer.recover_interrupted``): such an app is not missing."""
    results: list[ReconcileResult] = []
    scopes = list(scopes)
    if Scope.USER in scopes:
        installer.recover_interrupted(Scope.USER)
    for scope in scopes:
        try:
            apps = _registry(Scope(scope)).all()
        except Exception:  # noqa: BLE001
            log.warning("cannot read the %s registry", scope, exc_info=True)
            continue
        for app in apps:
            try:
                results.append(reconcile_app(app))
            except Exception:  # noqa: BLE001 - e.g. a bug triggered by one odd file
                log.warning("reconciling %s failed", app.id, exc_info=True)
                results.append(_result(app, CHANGE_UNCHANGED))
    return results
