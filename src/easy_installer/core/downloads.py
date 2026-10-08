"""The private folders that updates are downloaded into: ``$XDG_CACHE_HOME/easy-installer/
downloads/<app id>/`` (0700).

A folder is held (``flock``) by the update that uses it, from creating it until it is removed
again. A folder that nobody holds is what an update left behind when it was stopped hard (the
computer was switched off, the session ended, the program crashed): it can be several hundred
megabytes and is removed at the next start, when its app is uninstalled, and before its app
is updated again.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from pathlib import Path

from .integration import ID_RE
from .paths import cache_dir
from .squashfs import remove_tree

log = logging.getLogger(__name__)

DOWNLOADS_DIR_NAME = "downloads"
DOWNLOAD_DIR_MODE = 0o700
#: A folder that nobody holds is only a leftover once nothing in it changed for this long
#: (an update of an Easy Installer 0.2.0 that still runs does not hold its folder).
LEFTOVER_AGE = 15 * 60


class FolderInUse(Exception):
    """Another update holds the folder right now."""


def downloads_dir() -> Path:
    """``$XDG_CACHE_HOME/easy-installer/downloads``: one private folder per app while its
    update is downloaded; removed again afterwards."""
    return cache_dir() / DOWNLOADS_DIR_NAME


def app_download_dir(app_id: str) -> Path:
    """``downloads_dir()/<app_id>``; ValueError for an id that is no plain name."""
    if not isinstance(app_id, str) or not ID_RE.fullmatch(app_id):
        raise ValueError(f"unusable app id {app_id!r}")
    return downloads_dir() / app_id


def _try_lock(folder: Path) -> int | None:
    """An open, exclusively locked descriptor of ``folder``; None if another process holds it.
    Raises OSError if the folder cannot be opened (e.g. it is gone)."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    return fd


def hold(folder: Path) -> int:
    """Create ``folder`` anew and hold it; returns the descriptor for :func:`release`.

    What an interrupted update left there is removed first. FolderInUse if another update
    holds it; OSError if it cannot be created.
    """
    folder.parent.mkdir(mode=DOWNLOAD_DIR_MODE, parents=True, exist_ok=True)
    try:
        old = _try_lock(folder)
    except FileNotFoundError:
        old = -1
    if old is None:
        raise FolderInUse(str(folder))
    if old >= 0:
        try:
            remove_tree(folder)   # what an interrupted update left behind
        finally:
            os.close(old)
    folder.mkdir(mode=DOWNLOAD_DIR_MODE)
    fd = _try_lock(folder)
    if fd is None:   # somebody else was quicker
        raise FolderInUse(str(folder))
    return fd


def release(folder: Path, fd: int) -> None:
    """Remove the held ``folder`` (and the downloads folder if it is empty now), then let go."""
    try:
        remove_tree(folder)
        try:
            os.rmdir(folder.parent)   # only if no other update is being downloaded
        except OSError:
            pass
    finally:
        os.close(fd)


def _last_change(folder: Path) -> float:
    newest = os.lstat(folder).st_mtime
    for root, dirs, files in os.walk(folder):
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                continue
    return newest


def remove_leftovers(app_id: str | None = None, *, min_age: float = LEFTOVER_AGE) -> list[Path]:
    """Remove download folders that no update holds (of ``app_id``, or of every app) and in
    which nothing changed for ``min_age`` seconds. Never raises; returns what was removed."""
    removed: list[Path] = []
    root = downloads_dir()
    try:
        if app_id is not None:
            folders = [app_download_dir(app_id)]
        else:
            folders = [root / name for name in sorted(os.listdir(root))]
    except (OSError, ValueError):
        return removed
    now = time.time()
    for folder in folders:
        try:
            fd = _try_lock(folder)
        except OSError:
            continue   # gone, or not a folder of ours
        if fd is None:
            continue   # an update is running
        try:
            if now - _last_change(folder) < min_age:
                continue
            remove_tree(folder)
            removed.append(folder)
            log.info("removed the unfinished download %s", folder)
        except OSError as exc:
            log.warning("could not remove the unfinished download %s: %s", folder, exc)
        finally:
            os.close(fd)
    try:
        os.rmdir(root)
    except OSError:
        pass
    return removed
