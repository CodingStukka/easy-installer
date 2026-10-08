"""The previous version of an app, kept for a while after an update (per-user file handling).

When an update replaces an app file, the old one is kept as
``<apps_dir>/.easyinstaller-backups/<id>/<SafeName>-<version|sha8>.AppImage`` and recorded in the
registry entry's ``previous``, so the user can go back ("rollback"). One backup per app.

Data safety rules of this module:

* A backup is made with a **hard link** (or, where the file system has none, a rename): the old
  version is never copied, never leaves its file system, and - with the link - never disappears
  from the path the registry knows until the new version is saved.
* Everything that is moved or deleted here lies **directly inside**
  ``backups_dir/<app id>/``; ``backups_dir`` and the app's folder must be real folders (never
  symbolic links). Symbolic links are removed as links, never followed.
* A backup may be a **file** (AppImage) or a **folder** (portable app): moving and deleting
  handle both. A folder is only deleted for good when nothing in it differs from what was
  unpacked (``core.appfolder``); a kept folder that holds files the app or the user saved goes
  to the trash instead.

System-wide apps are handled by the administrator helper (``helper/ops.py``), which has its own,
stricter implementation of the same layout.
"""

from __future__ import annotations

import calendar
import contextlib
import dataclasses
import hashlib
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import InstallError
from ..i18n import _
from . import appfolder
from .integration import ID_RE, backup_file_name, safe_file_stem, version_slug
from .paths import BACKUPS_DIR_NAME, Layout
from .registry import KIND_PORTABLE, InstalledApp, Registry, make_previous

log = logging.getLogger(__name__)

DIR_MODE = 0o755
SECONDS_PER_DAY = 24 * 3600
SAVED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
#: Pruning is housekeeping: it does not wait long for a running installation.
PRUNE_LOCK_TIMEOUT = 10
_MAX_NAME_TRIES = 1000


# ------------------------------------------------------------------------------------------------
# paths
# ------------------------------------------------------------------------------------------------


def backups_dir(layout: Layout) -> Path:
    """``layout.apps_dir / ".easyinstaller-backups"``."""
    return layout.backups_dir


def app_backup_dir(layout: Layout, app_id: str) -> Path:
    if not isinstance(app_id, str) or not ID_RE.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id!r}")
    return layout.backups_dir / app_id


def backed_up_path(app: InstalledApp) -> Path:
    """What is kept of ``app``: its AppImage, or a portable app's whole folder."""
    if app.kind == KIND_PORTABLE and app.install_dir:
        return Path(app.install_dir)
    return Path(app.appimage_path)


def kept_folder_name(name: str, version: str | None, sha256: str | None) -> str:
    """``"Blender-4.2.0"``: the name of a kept folder of a portable app."""
    return f"{safe_file_stem(name)}-{version_slug(version, sha256)}"


def planned_backup_path(layout: Layout, app: InstalledApp) -> Path:
    """Where the current version of ``app`` would be kept (the final name may get a number)."""
    if app.kind == KIND_PORTABLE:
        name = kept_folder_name(app.name, app.version, app.sha256)
    else:
        name = backup_file_name(app.name, app.version, app.sha256)
    return app_backup_dir(layout, app.id) / name


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _lexists(path: Path | str) -> bool:
    return os.path.lexists(path)


def is_backup_path(layout: Layout, app_id: str, path: object) -> bool:
    """``path`` names something directly inside ``backups_dir/<app_id>/`` (both real folders).

    Purely about the location: the entry itself may be missing. Paths from the registry are
    only ever moved or deleted when this holds.
    """
    if not isinstance(path, (str, os.PathLike)) or not isinstance(app_id, str) \
            or not ID_RE.fullmatch(app_id):
        return False
    text = os.fspath(path)
    folder = app_backup_dir(layout, app_id)
    # Exactly "<folder>/<name>", spelled as this layout spells it (that is how it was recorded):
    # no "..", no further folder level, nothing that merely resolves to the same place.
    prefix = os.fspath(folder).rstrip("/") + "/"
    if not isinstance(text, str) or not os.path.isabs(text) or not text.startswith(prefix):
        return False
    name = text[len(prefix):]
    if not name or "/" in name or name in (".", "..") or "\0" in name:
        return False
    return _is_real_dir(layout.backups_dir) and _is_real_dir(folder)


def usable_previous(layout: Layout, app: InstalledApp | None) -> dict | None:
    """``app.previous`` if its backup is really there (in this app's backup folder), else None."""
    previous = app.previous if app is not None else None
    if not previous:
        return None
    path = previous.get("path")
    if not is_backup_path(layout, app.id, path):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        return None
    return previous


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def find_unrecorded(layout: Layout, app: InstalledApp) -> dict | None:
    """A kept copy of exactly this version of ``app`` that its registry entry does not know.

    An update or rollback that was interrupted (power loss) after keeping the old version but
    before saving the registry leaves such a file. When the entry is later brought in line with
    the new app file (reconcile), this is its ``previous`` - the interrupted operation ends up
    complete. Only a regular file with the recorded size and sha256 qualifies.
    """
    if not app.sha256 or not isinstance(app.id, str) or not ID_RE.fullmatch(app.id):
        return None
    folder = app_backup_dir(layout, app.id)
    if not _is_real_dir(layout.backups_dir) or not _is_real_dir(folder):
        return None
    recorded = app.previous.get("path") if app.previous else None
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        path = folder / name
        if os.fspath(path) == recorded:
            continue
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size != app.size \
                    or _sha256(path) != app.sha256:
                continue
        except OSError:
            continue
        log.info("found the kept version %s of %s again: %s", app.version, app.id, path)
        return make_previous(version=app.version, path=path, sha256=app.sha256, size=st.st_size,
                             original_filename=app.original_filename)
    return None


# ------------------------------------------------------------------------------------------------
# keeping the old version
# ------------------------------------------------------------------------------------------------


def _ensure_real_dir(path: Path) -> None:
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        pass
    if not _is_real_dir(path):
        raise InstallError(
            _("A folder is in the way where the previous version should be kept: {path}").format(
                path=path),
            details=f"{path} is not a real folder")


def ensure_app_backup_dir(layout: Layout, app_id: str) -> Path:
    """Create ``backups_dir/<app_id>`` (the apps folder must exist); refuses symbolic links."""
    folder = app_backup_dir(layout, app_id)
    _ensure_real_dir(layout.backups_dir)
    _ensure_real_dir(folder)
    return folder


def matches_entry(app: InstalledApp, st: os.stat_result) -> bool:
    """The app file that ``st`` describes is the one the registry entry ``app`` records: the
    same size, and the same modification time once that is known. An app that replaced its own
    file (a self-updater) is something else until Easy Installer has looked at it again."""
    return st.st_size == app.size and not (app.mtime_ns and st.st_mtime_ns != app.mtime_ns)


class FileChangedError(InstallError):
    """The app file is no longer what its registry entry describes (it updated itself in the
    meantime): it must not be kept under the version the entry names."""


def file_changed_error(app: InstalledApp) -> FileChangedError:
    return FileChangedError(
        _("{name} has changed its own file in the meantime (it may have updated itself). "
          "Please try again.").format(name=app.name),
        details=f"{app.appimage_path} is no longer the file recorded for {app.id} "
                f"(size {app.size}, mtime_ns {app.mtime_ns})")


def _free_name(wanted: Path) -> Path:
    if not _lexists(wanted):
        return wanted
    stem, suffix = (wanted.stem, wanted.suffix) if wanted.suffix == ".AppImage" else (wanted.name, "")
    for n in range(2, _MAX_NAME_TRIES):
        candidate = wanted.with_name(f"{stem}-{n}{suffix}")
        if not _lexists(candidate):
            return candidate
    raise InstallError(_("The previous version could not be kept."),
                       details=f"no free name for {wanted}")


def _link(source: Path, target: Path) -> None:
    os.link(source, target, follow_symlinks=False)


def _move(source: Path, target: Path) -> None:
    os.rename(source, target)


@dataclass
class Stash:
    """The old version of an app, kept in its backup folder while the new one is installed."""

    source: Path           # where the old version was (and, if ``linked``, still is)
    path: Path             # where it is kept
    record: dict           # the registry's ``previous`` for the new entry
    linked: bool           # hard link (the source is untouched) or moved

    def undo(self) -> None:
        """Take the backup back (the installation failed): the old version is at ``source``."""
        if self.linked:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
        elif _lexists(self.path):
            # Whatever a failed installation left at the old place is replaced by the old
            # version itself.
            os.replace(self.path, self.source)
        _remove_empty_dirs(self.path.parent)


def stash(layout: Layout, app: InstalledApp) -> Stash:
    """Keep the current version of ``app`` in its backup folder.

    A regular file gets a second name there (hard link): it stays where it is until the
    installer replaces or removes it, so there is no moment without the app. Folders, and files
    on file systems without hard links, are moved (renamed). Nothing is ever copied: if the
    backup folder is on another file system, OSError (EXDEV) is raised and nothing has changed.

    Raises InstallError/OSError if the old version cannot be kept, and
    :class:`FileChangedError` (nothing has changed) if the app file is no longer the one the
    entry describes (see :func:`matches_entry`).
    """
    source = backed_up_path(app)
    st = os.lstat(source)
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        raise InstallError(_("The previous version could not be kept."),
                           details=f"{source} is neither a regular file nor a folder")
    ensure_app_backup_dir(layout, app.id)
    target = _free_name(planned_backup_path(layout, app))
    linked = False
    if stat.S_ISREG(st.st_mode):
        try:
            _link(source, target)
            linked = True
        except OSError as exc:
            log.info("cannot hard-link %s to %s (%s); moving it instead", source, target, exc)
    if not linked:
        try:
            _move(source, target)
        except OSError:
            _remove_empty_dirs(target.parent)
            raise
    kept = Stash(source=source, path=target, record={}, linked=linked)
    if stat.S_ISREG(st.st_mode) and app.kind != KIND_PORTABLE:
        # What was really linked (or moved) must be the version the entry describes: an app
        # that replaced its own file meanwhile must not be kept under the old version's name.
        try:
            same = matches_entry(app, os.lstat(target))
        except OSError:
            same = False
        if not same:
            kept.undo()
            raise file_changed_error(app)
    kept.record = make_previous(version=app.version, path=target, sha256=app.sha256,
                                size=app.size or (st.st_size if stat.S_ISREG(st.st_mode) else 0),
                                original_filename=app.original_filename)
    log.info("kept version %s of %s as %s", app.version, app.id, target)
    return kept


# ------------------------------------------------------------------------------------------------
# deleting
# ------------------------------------------------------------------------------------------------


def _remove_entry(layout: Layout, path: Path) -> bool:
    """Delete a file or a symbolic link (the link itself), or dispose of a kept app folder: it
    is deleted when nothing in it is the user's, else moved to the trash - from a visible place
    in the apps folder, where restoring it brings it back (``appfolder.dispose``). It never
    follows symbolic links and copes with folders inside that were made read-only.
    False: it is not there. Raises OSError if something stays."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        result, where = appfolder.dispose(Path(path), visible=layout.apps_dir / path.name)
        if result != appfolder.DELETED:
            log.warning("the kept version %s holds files that were saved in it: %s %s",
                        path, result, where)
    else:
        os.unlink(path)
    return True


def _rmdir_if_empty(directory: Path) -> None:
    if _is_real_dir(directory):
        with contextlib.suppress(OSError):
            os.rmdir(directory)


def _remove_empty_dirs(folder: Path) -> None:
    """Remove an app's backup folder, then the backups folder itself, once they are empty."""
    root = folder.parent
    if root.name != BACKUPS_DIR_NAME:
        return
    _rmdir_if_empty(folder)
    _rmdir_if_empty(root)


def remove_backup(layout: Layout, app_id: str, path: object) -> bool:
    """Delete the backup ``path`` of ``app_id``; anything outside its backup folder is left
    alone (False). Raises OSError if it is there but cannot be deleted."""
    if not is_backup_path(layout, app_id, path):
        if path:
            log.warning("not deleting %r: it is not a backup of %s", path, app_id)
        return False
    target = Path(os.fspath(path))
    removed = _remove_entry(layout, target)
    if removed:
        log.info("removed the kept previous version %s", target)
    _remove_empty_dirs(target.parent)
    return removed


def remove_app_backups(layout: Layout, app_id: str) -> None:
    """Delete everything kept for ``app_id`` (it is being uninstalled). Raises OSError."""
    folder = app_backup_dir(layout, app_id)
    if not _lexists(folder):
        return
    if not _is_real_dir(layout.backups_dir) or not _is_real_dir(folder):
        log.warning("not deleting %s: not a real folder", folder)
        return
    for name in sorted(os.listdir(folder)):
        _remove_entry(layout, folder / name)
    log.info("removed the kept previous versions in %s", folder)
    _remove_empty_dirs(folder)


# ------------------------------------------------------------------------------------------------
# age
# ------------------------------------------------------------------------------------------------


def saved_time(previous: dict) -> float | None:
    """``previous["saved_at"]`` as seconds since the epoch (None if it cannot be read)."""
    text = previous.get("saved_at")
    if not isinstance(text, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, SAVED_AT_FORMAT)))
    except (ValueError, OverflowError):
        return None


def age_days(previous: dict, now: float | None = None) -> float | None:
    saved = saved_time(previous)
    if saved is None:
        return None
    return ((time.time() if now is None else now) - saved) / SECONDS_PER_DAY


def is_expired(previous: dict, max_age_days: float, now: float | None = None) -> bool:
    """Older than ``max_age_days`` (0 or less: every backup is). A backup without a readable
    date is judged by when its file was put there."""
    if max_age_days <= 0:
        return True
    now = time.time() if now is None else now
    saved = saved_time(previous)
    if saved is None:
        try:
            saved = os.lstat(previous.get("path") or "").st_ctime
        except OSError:
            return False
    return now - saved > max_age_days * SECONDS_PER_DAY


def prune(layout: Layout, registry: Registry, max_age_days: float, *,
          now: float | None = None, lock_timeout: float | None = PRUNE_LOCK_TIMEOUT
          ) -> list[InstalledApp]:
    """Delete the backups older than ``max_age_days``; returns the changed registry entries.

    Also forgets backups that are no longer there, and deletes old entries in the backup
    folders that no installed app refers to (left behind by an interrupted installation, or by
    an older version of Easy Installer that rewrote the registry).
    """
    now = time.time() if now is None else now
    changed: list[InstalledApp] = []
    if not _lexists(layout.backups_dir) and not any(app.previous for app in registry.all()):
        return changed   # nothing kept: do not even create the lock file
    try:
        lock = registry.locked(timeout=lock_timeout)
        with lock:
            for app in registry.all():
                updated = _prune_app(layout, app, max_age_days, now)
                if updated is not None:
                    registry.put(updated)
                    changed.append(updated)
            _remove_orphans(layout, registry, max_age_days, now)
    except InstallError as exc:
        log.info("not pruning backups now: %s", exc.details or exc)
    return changed


def _prune_app(layout: Layout, app: InstalledApp, max_age_days: float,
               now: float) -> InstalledApp | None:
    previous = app.previous
    if not previous:
        return None
    path = previous.get("path") or ""
    ours = is_backup_path(layout, app.id, path)
    exists = _lexists(path)
    if exists and not is_expired(previous, max_age_days, now):
        return None
    if exists and ours:
        try:
            remove_backup(layout, app.id, path)
        except OSError as exc:
            log.warning("could not delete the old backup %s: %s", path, exc)
            return None
    elif exists:
        log.warning("forgetting the backup %s of %s (not in its backup folder)", path, app.id)
    return dataclasses.replace(app, previous=None)


def _remove_orphans(layout: Layout, registry: Registry, max_age_days: float, now: float) -> None:
    root = layout.backups_dir
    if not _is_real_dir(root):
        return
    recorded = {os.path.normpath(app.previous["path"]) for app in registry.all()
                if app.previous and app.previous.get("path")}
    cutoff = now - max(max_age_days, 0) * SECONDS_PER_DAY
    try:
        folders = sorted(os.listdir(root))
    except OSError:
        return
    for name in folders:
        folder = root / name
        if not ID_RE.fullmatch(name) or not _is_real_dir(folder):
            continue
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for entry in entries:
            path = folder / entry
            if os.path.normpath(path) in recorded:
                continue
            try:
                if os.lstat(path).st_ctime > cutoff:
                    continue
                _remove_entry(layout, path)
                log.info("removed the unused backup %s", path)
            except OSError as exc:
                log.warning("could not delete the unused backup %s: %s", path, exc)
        _rmdir_if_empty(folder)
    _rmdir_if_empty(root)
