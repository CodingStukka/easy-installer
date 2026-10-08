"""The folder of a portable app: what Easy Installer unpacked there, and what the app or the
user saved there since.

Many apps that come as an archive keep their settings, profiles or saved games next to their
program: Tor Browser's profile, the ``data`` folder of VS Code in portable mode, a game's
``saves``. Easy Installer replaces and removes such a folder as a whole, so it must know what
in there is its own - and never throw away the rest:

* :func:`write_manifest` records every entry of a freshly unpacked folder in
  ``.easy-installer-files.json`` (the marker file keeps the record's sha256, so an archive can
  never bring its own record);
* :func:`differences` compares a folder with that record: entries the archive did not contain
  ("added") and entries that changed since ("changed");
* :func:`carry_over` moves what was added into the folder of the version that takes the old
  one's place (update, reinstall, going back), so the app finds it again;
* :func:`dispose` gets rid of a folder that is no longer needed: it is deleted only when
  nothing in it differs from what was unpacked - otherwise it goes to the trash, and if even
  that is impossible it stays where it is.

Folders unpacked by Easy Installer 0.2.0 have no record: what is in them is unknown, so they
are never deleted permanently (see :func:`is_pristine`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import appdata
from .portable import MANIFEST_NAME, MARKER_NAME

log = logging.getLogger(__name__)

MANIFEST_FORMAT = 1
MAX_MARKER_SIZE = 1024 * 1024
MAX_MANIFEST_SIZE = 64 * 1024 * 1024
#: Entries looked at in one folder before giving up ("cannot tell" - the careful answer).
MAX_WALK_ENTRIES = 1_000_000
#: The app's icon, kept next to the marker (see ``installer._marker_icon``).
ICON_STEM = ".easy-installer-icon"
_ICON_NAME_RE = re.compile(re.escape(ICON_STEM) + r"\.(?:png|svg|xpm)\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DIR_MODE = 0o755

#: What :func:`dispose` did with a folder.
DELETED = "deleted"     # nothing in it was the user's: deleted
TRASHED = "trashed"     # it is in the trash now
KEPT = "kept"           # it could be neither deleted nor trashed: it stays (see the path)

# entry kinds in the record
_DIR, _FILE, _LINK, _OTHER = "d", "f", "l", "o"


def is_own_name(name: str) -> bool:
    """A name at the top of an app folder that is Easy Installer's, not the app's."""
    return name in (MARKER_NAME, MANIFEST_NAME) or bool(_ICON_NAME_RE.match(name))


def read_marker(folder: Path | str) -> dict | None:
    """The content of ``<folder>/.easy-installer.json`` (written when a portable app is
    installed), or None if it is missing, a link, too large or not a JSON object."""
    data = _read_small_file(os.path.join(folder, MARKER_NAME), MAX_MARKER_SIZE)
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _read_small_file(path: str, limit: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                return None
            data = fh.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


# ------------------------------------------------------------------------------------------------
# the record of what was unpacked
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """Relative path -> ``("d",)`` folder | ``("f", size, mtime_ns)`` file |
    ``("l", target)`` symbolic link | ``("o",)`` anything else."""

    entries: dict[str, tuple]


@dataclass(frozen=True)
class Difference:
    relpath: str
    #: True: the archive did not contain it; False: it is there but changed
    added: bool


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return _DIR
    if stat.S_ISREG(mode):
        return _FILE
    if stat.S_ISLNK(mode):
        return _LINK
    return _OTHER


def _describe(path: str, st: os.stat_result) -> tuple:
    kind = _kind(st.st_mode)
    if kind == _FILE:
        return (_FILE, st.st_size, st.st_mtime_ns)
    if kind == _LINK:
        return (_LINK, os.readlink(path))
    return (kind,)


def _join(rel_dir: str, name: str) -> str:
    return f"{rel_dir}/{name}" if rel_dir else name


def _listing(folder: str, rel_dir: str) -> list[os.DirEntry]:
    with os.scandir(os.path.join(folder, rel_dir) if rel_dir else folder) as entries:
        return sorted(entries, key=lambda entry: entry.name)


def scan(folder: Path | str) -> Manifest:
    """Every entry of ``folder`` (symbolic links are recorded, never followed), except Easy
    Installer's own files at the top. Raises OSError."""
    root = os.fspath(folder)
    entries: dict[str, tuple] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        for entry in _listing(root, rel_dir):
            if not rel_dir and is_own_name(entry.name):
                continue
            rel = _join(rel_dir, entry.name)
            described = _describe(entry.path, entry.stat(follow_symlinks=False))
            entries[rel] = described
            if described[0] == _DIR:
                stack.append(rel)
            if len(entries) > MAX_WALK_ENTRIES:
                raise OSError(f"{root} has more than {MAX_WALK_ENTRIES} entries")
    return Manifest(entries)


def write_manifest(folder: Path) -> tuple[Manifest, str]:
    """Record what is in the freshly unpacked ``folder`` (before the marker is written) in
    ``<folder>/.easy-installer-files.json``. Returns the record and the sha256 of the file,
    which the marker keeps. Raises OSError."""
    manifest = scan(folder)
    payload = {"format": MANIFEST_FORMAT,
               "entries": {rel: list(value) for rel, value in manifest.entries.items()}}
    # ensure_ascii: names that are not UTF-8 (surrogate escapes) round-trip as \\u escapes
    data = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    path = os.path.join(folder, MANIFEST_NAME)
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        os.fchmod(fh.fileno(), 0o644)
    return manifest, hashlib.sha256(data).hexdigest()


def _entry(value: object) -> tuple | None:
    if not isinstance(value, list) or not value:
        return None
    kind = value[0]
    if kind in (_DIR, _OTHER) and len(value) == 1:
        return (kind,)
    if kind == _FILE and len(value) == 3 and all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in value[1:]):
        return (_FILE, value[1], value[2])
    if kind == _LINK and len(value) == 2 and isinstance(value[1], str):
        return (_LINK, value[1])
    return None


def read_manifest(folder: Path | str, marker: dict | None = None) -> Manifest | None:
    """The record of what was unpacked into ``folder`` - only if the folder's marker vouches for
    it (its ``files_sha256``). None: there is none (e.g. unpacked by Easy Installer 0.2.0), or
    it is damaged."""
    marker = read_marker(folder) if marker is None else marker
    expected = marker.get("files_sha256") if isinstance(marker, dict) else None
    if not isinstance(expected, str) or not _SHA256_RE.match(expected):
        return None
    data = _read_small_file(os.path.join(folder, MANIFEST_NAME), MAX_MANIFEST_SIZE)
    if data is None or hashlib.sha256(data).hexdigest() != expected:
        return None
    try:
        payload = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    raw = payload.get("entries") if isinstance(payload, dict) else None
    if payload.get("format") != MANIFEST_FORMAT or not isinstance(raw, dict):
        return None
    entries: dict[str, tuple] = {}
    for rel, value in raw.items():
        described = _entry(value)
        if described is None or not isinstance(rel, str) or not rel:
            return None
        entries[rel] = described
    return Manifest(entries)


# ------------------------------------------------------------------------------------------------
# comparing
# ------------------------------------------------------------------------------------------------


def differences(folder: Path | str, manifest: Manifest, *,
                content: bool = True) -> list[Difference] | None:
    """What in ``folder`` is not as ``manifest`` recorded it: entries that were added (a
    folder that was added is one difference - its content is not looked at) and, with
    ``content``, files and links that changed (size, modification time, link target) or
    changed their kind. Entries that are gone are no difference. Easy Installer's own files at
    the top are left out. None: the folder cannot be read completely (so nobody can tell)."""
    root = os.fspath(folder)
    found: list[Difference] = []
    seen = 0
    stack = [""]
    try:
        while stack:
            rel_dir = stack.pop()
            for entry in _listing(root, rel_dir):
                seen += 1
                if seen > MAX_WALK_ENTRIES:
                    log.warning("%s has too many entries to compare", root)
                    return None
                if not rel_dir and is_own_name(entry.name):
                    continue
                rel = _join(rel_dir, entry.name)
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                recorded = manifest.entries.get(rel)
                kind = _kind(st.st_mode)
                if recorded is None:
                    found.append(Difference(rel, True))
                elif recorded[0] != kind:
                    found.append(Difference(rel, False))
                elif kind == _DIR:
                    stack.append(rel)
                elif content and _describe(entry.path, st) != recorded:
                    found.append(Difference(rel, False))
    except OSError as exc:
        log.info("cannot compare %s with what was unpacked: %s", root, exc)
        return None
    return sorted(found, key=lambda difference: difference.relpath)


def is_pristine(folder: Path | str) -> bool:
    """Nothing in ``folder`` was added or changed since Easy Installer unpacked it - known for
    sure, from its record. False when that cannot be told."""
    manifest = read_manifest(folder)
    return manifest is not None and differences(folder, manifest) == []


# ------------------------------------------------------------------------------------------------
# taking over what the app saved
# ------------------------------------------------------------------------------------------------


def _ensure_parents(old: str, new: str, relpath: str) -> bool:
    """The folders leading to ``relpath`` in ``new`` exist as real folders (created like their
    counterparts in ``old`` where they are missing). False if something else is in the way."""
    parts = relpath.split("/")[:-1]
    done = ""
    for part in parts:
        done = _join(done, part)
        target = os.path.join(new, done)
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            try:
                mode = stat.S_IMODE(os.lstat(os.path.join(old, done)).st_mode) | 0o700
            except OSError:
                mode = DIR_MODE
            os.mkdir(target, DIR_MODE)
            os.chmod(target, mode & 0o755 | 0o700)
            continue
        if not stat.S_ISDIR(st.st_mode):
            return False
    return True


def carry_over(old: Path | str, new: Path | str, reference: Manifest) -> list[tuple[str, str]]:
    """Move what the app or the user added to ``old`` - every entry that is not in
    ``reference`` (what was unpacked there) - to the same place in ``new``, where nothing is
    in its way. What is in the way (the new version brings something of that name) stays in
    ``old``. Returns the moves ``(from, to)`` that were made (see :func:`undo_carry_over`)."""
    old_root, new_root = os.fspath(old), os.fspath(new)
    found = differences(old_root, reference, content=False)
    moves: list[tuple[str, str]] = []
    for difference in found or ():
        if not difference.added:
            continue
        source = os.path.join(old_root, difference.relpath)
        target = os.path.join(new_root, difference.relpath)
        try:
            if not _ensure_parents(old_root, new_root, difference.relpath) \
                    or os.path.lexists(target):
                log.info("not taking over %s: the new version has something there", source)
                continue
            os.rename(source, target)
        except OSError as exc:
            log.warning("could not take over %s: %s", source, exc)
            continue
        moves.append((source, target))
    if moves:
        log.info("took over %d entries saved in %s", len(moves), old_root)
    return moves


def undo_carry_over(moves: list[tuple[str, str]]) -> list[str]:
    """Put back what :func:`carry_over` moved. Returns what could not be put back (it is still
    in the new folder)."""
    stuck = []
    for source, target in reversed(moves):
        try:
            os.rename(target, source)
        except OSError as exc:
            log.warning("could not put %s back: %s", source, exc)
            stuck.append(target)
    return stuck


# ------------------------------------------------------------------------------------------------
# getting rid of a folder
# ------------------------------------------------------------------------------------------------


def _make_removable(folder: str | os.PathLike) -> None:
    try:
        st = os.lstat(folder)
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
            os.chmod(folder, stat.S_IMODE(st.st_mode) | 0o700)
    except OSError:
        pass   # whatever really cannot be removed is reported by rmtree


def make_accessible(path: Path | str) -> None:
    """Give the folders of a tree that is about to go the owner's rwx (an app folder may contain
    folders made read-only - by the archive, the app or the user). Links are not entered."""
    _make_removable(path)
    for current, dirnames, _files in os.walk(path):   # top-down; links are not entered
        for name in dirnames:
            _make_removable(os.path.join(current, name))


def delete_tree(path: Path | str) -> None:
    """Delete a folder for good (never following links). Raises OSError if something stays."""
    make_accessible(path)
    shutil.rmtree(path)


def free_name(wanted: Path) -> Path:
    """``wanted``, or ``wanted-2``, ``wanted-3``, ... - the first name that is not taken."""
    if not os.path.lexists(wanted):
        return wanted
    for n in range(2, 1000):
        candidate = wanted.with_name(f"{wanted.name}-{n}")
        if not os.path.lexists(candidate):
            return candidate
    raise OSError(f"no free name for {wanted}")


def dispose(folder: Path, *, visible: Path) -> tuple[str, Path | None]:
    """``folder`` (an app folder, verified by the caller) is no longer needed.

    It is deleted only when :func:`is_pristine` says nothing in it is the user's. Else it is
    first renamed to ``visible`` (or a free variant of it: a name people can find, so that
    restoring it from the trash brings it back somewhere they can see it) and moved to the
    trash. If the trash cannot take it, it stays there. Returns ``(DELETED, None)``,
    ``(TRASHED, the name it had)`` or ``(KEPT, where it is)``. Raises OSError if a folder
    that was to be deleted could not be deleted completely.
    """
    make_accessible(folder)
    if is_pristine(folder):
        delete_tree(folder)
        log.info("removed %s", folder)
        return DELETED, None
    place = Path(folder)
    with contextlib.suppress(OSError):
        target = free_name(visible)
        if os.path.normpath(target) != os.path.normpath(place):
            os.rename(place, target)
            place = target
    error = appdata.trash_folder(place)
    if error is None:
        log.info("moved %s (with files saved after it was installed) to the trash", place)
        return TRASHED, place
    log.warning("keeping %s: it cannot be moved to the trash (%s)", place, error)
    return KEPT, place
