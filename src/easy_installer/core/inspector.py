"""Inspect an AppImage without running it: name, version, icon, desktop entry, Electron, ...

Everything is extracted into a private temporary directory (``work_dir``, mode 0700) that
belongs to the returned :class:`AppImageInfo`; call :meth:`AppImageInfo.cleanup` or use it as a
context manager when done. Symlinks inside the payload are resolved by extracting their targets
one hop at a time - never by following them on the host file system.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import tempfile
import unicodedata
import weakref
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from ..errors import ExtractionError, NotAnAppImageError
from ..i18n import _
from .appdata import data_hints_for
from .desktop_entry import DesktopEntry, exec_program, split_exec
from .elf import (
    ELF_MAGIC,
    ElfInfo,
    is_foreign_arch,
    payload_magic,
    read_elf_info,
    read_update_info,
)
from .imageinfo import ImageInfo, probe_image
from .integration import (
    MimeTypeDef,
    derive_app_id,
    name_from_filename,
    normalize_version,
    parse_mime_package,
    parse_version_from_filename,
)
from .origin import read_origin
from .signature import SignatureInfo, read_signature
from .squashfs import (
    SQUASHFS_MAGIC,
    PayloadReader,
    damaged_file_message,
    escape_pattern,
    open_payload,
    remove_tree,
)
from .updates import UpdateSource, choose_source

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float | None, str], None]

MAX_ICON_SIZE = 10 * 1024 * 1024
MAX_DESKTOP_SIZE = 1024 * 1024
#: Top-level desktop files considered (real AppImages have one).
MAX_DESKTOP_FILES = 16
MAX_SYMLINK_HOPS = 8
HASH_CHUNK_SIZE = 1024 * 1024
ICON_PNG_SIZES = (512, 256, 1024, 192, 128, 96, 64, 48, 32)
ICON_EXTENSIONS = ("svg", "png", "xpm")
ELECTRON_MARKERS = ("chrome-sandbox", "chrome_crashpad_handler", "resources/app.asar")
MIME_PACKAGES_DIR = "usr/share/mime/packages"
MAX_MIME_PACKAGES = 8
MAX_MIME_PACKAGE_SIZE = 256 * 1024
ELECTRON_MARKER_NAMES = frozenset({"chrome-sandbox", "chrome_crashpad_handler"})
#: Markers are looked for down to e.g. ``usr/lib/<app>/resources/app.asar`` (5 path components).
ELECTRON_MAX_DEPTH = 5
#: electron-builder's update configuration (where the app itself looks for new versions)
UPDATE_CONFIG = "resources/app-update.yml"
MAX_UPDATE_CONFIG_SIZE = 64 * 1024
#: Programs named in an embedded ``Exec=`` that say nothing about the app's own name.
_GENERIC_PROGRAMS = frozenset({"apprun", "env", "sh", "bash"})
_IMAGE_SUFFIXES = (".png", ".svg", ".svgz", ".xpm", ".jpg", ".jpeg", ".ico", ".gif", ".bmp", ".webp")
_MAX_NAME_LENGTH = 200


@dataclass
class AppImageInfo:
    path: Path
    size: int
    sha256: str | None
    elf: ElfInfo
    appimage_type: int | None
    arch: str
    app_id: str
    name: str
    display_name: str
    version: str | None
    comment: str | None
    categories: list[str]
    desktop_entry: DesktopEntry | None
    desktop_filename: str | None
    icon_path: Path | None
    icon_info: ImageInfo | None
    is_electron: bool
    exec_has_no_sandbox: bool
    terminal: bool
    update_info: str | None
    work_dir: Path
    warnings: list[str] = field(default_factory=list)
    #: file types the app defines itself (usr/share/mime/packages/*.xml), for its MimeType= list
    mime_types: list[MimeTypeDef] = field(default_factory=list)
    # -- added in 0.2 ---------------------------------------------------------------------------
    #: where the app publishes new versions (``resources/app-update.yml``, else ``.upd_info``)
    update_source: UpdateSource | None = None
    #: where the file was downloaded from, if the browser noted it on the file
    origin_url: str | None = None
    #: the embedded signature; None = not signed (the normal case)
    signature: SignatureInfo | None = None
    #: names under which the app may keep its settings and data (``core.appdata``)
    data_hints: list[str] = field(default_factory=list)

    def _own_work_dir(self, owner: "_WorkDirOwner") -> None:
        """From now on ``work_dir`` lives as long as this object (or until the program ends)."""
        # Deliberately not a dataclass field: keeps asdict()/JSON output to the documented fields.
        self.__dict__["_work_dir_owner"] = owner
        self.__dict__["_finalizer"] = owner.finalizer

    def cleanup(self) -> None:
        finalizer = self.__dict__.get("_finalizer")
        if finalizer is not None:
            finalizer()  # runs remove_tree at most once
        else:
            remove_tree(self.work_dir)

    def __enter__(self) -> "AppImageInfo":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


class _WorkDirOwner:
    """Removes the work dir when it is garbage-collected - or when the program ends, also while
    the file is still being read (e.g. the window quits and the inspection thread just stops)."""

    def __init__(self, work_dir: Path):
        self.finalizer = weakref.finalize(self, remove_tree, work_dir)


# --------------------------------------------------------------------------------------------
# Files inside the payload
# --------------------------------------------------------------------------------------------

def _split(rel: str) -> list[str]:
    return [part for part in rel.split("/") if part not in ("", ".")]


class PayloadFiles:
    """On-demand extraction of payload members into ``root`` with in-payload symlink resolution.

    With ``sizes`` (from the payload listing) a regular file larger than the caller's limit is
    never extracted: repeated bytes compress about 1000:1, so a few MB of AppImage could
    otherwise fill the disk while it is only being looked at.
    """

    def __init__(self, reader: PayloadReader, root: Path, members: Sequence[str] | None,
                 sizes: dict[str, int] | None = None):
        self.reader = reader
        self.root = root
        self._root_real = os.path.realpath(root)
        self.sizes = sizes
        self.members: set[str] | None = set(members) if members is not None else None
        self.dirs: set[str] = set()
        for member in self.members or ():
            parts = member.split("/")
            for i in range(1, len(parts)):
                self.dirs.add("/".join(parts[:i]))
        self._attempted: set[str] = set()

    def listed(self, rel: str) -> bool | None:
        """True/False if the payload listing is available, None if unknown."""
        return None if self.members is None else rel in self.members

    def extract(self, rels: Iterable[str], max_size: int = MAX_ICON_SIZE) -> None:
        """Extract exact paths (not patterns) in one go, skipping known-missing/already-tried ones
        and files known to be larger than ``max_size``."""
        todo = []
        for rel in rels:
            if rel in self._attempted:
                continue
            self._attempted.add(rel)
            if self.members is not None and (rel not in self.members or rel in self.dirs):
                continue
            if self.sizes is not None and self.sizes.get(rel, 0) > max_size:
                log.debug("not extracting %s (%d bytes)", rel, self.sizes[rel])
                continue
            todo.append(rel)
        if todo:
            self.reader.extract([escape_pattern(r) for r in todo], self.root)

    def resolve(self, rel: str, *, max_hops: int = MAX_SYMLINK_HOPS,
                max_size: int = MAX_ICON_SIZE) -> Path | None:
        """Path of the regular file inside ``root`` that ``rel`` refers to, or None.

        Symlinks are followed only within the payload (absolute targets and ``..`` escapes are
        rejected); at most ``max_hops`` links are followed. Files known to be larger than
        ``max_size`` are not extracted (None).
        """
        pending = _split(rel)
        done: list[str] = []
        hops = 0
        while pending:
            part = pending.pop(0)
            if part == "..":
                if not done:
                    return None
                done.pop()
                continue
            current = "/".join([*done, part])
            local = os.path.join(self.root, *done, part)
            is_last = not pending
            if not os.path.lexists(local):
                if self.members is not None:
                    if current in self.dirs:
                        if is_last:
                            return None  # a directory, not a file
                        done.append(part)
                        continue
                    if current not in self.members:
                        return None
                    self.extract([current], max_size)
                elif is_last:
                    self.extract([current], max_size)
                else:
                    # No listing: extract the full remaining path (creates parent directories).
                    # A ".." further down cannot be handled without knowing which components are
                    # directories (extracting one would pull in its whole subtree) - give up.
                    if ".." in pending:
                        return None
                    self.extract(["/".join([*done, part, *pending])], max_size)
                if not os.path.lexists(local):
                    return None
            st = os.lstat(local)
            if stat.S_ISLNK(st.st_mode):
                hops += 1
                if hops > max_hops:
                    log.debug("too many symlink hops resolving %s", rel)
                    return None
                target = os.readlink(local)
                if not target or target.startswith("/"):
                    log.debug("refusing symlink %s -> %s (points outside the app)", current, target)
                    return None
                pending = _split(target) + pending
                continue
            if stat.S_ISDIR(st.st_mode):
                if is_last:
                    return None
                done.append(part)
                continue
            if stat.S_ISREG(st.st_mode) and is_last:
                done.append(part)
                continue
            return None
        if not done:
            return None
        final = self.root.joinpath(*done)
        real = os.path.realpath(final)
        if not real.startswith(self._root_real + os.sep) or not stat.S_ISREG(os.lstat(final).st_mode):
            return None
        return final

    def top_level_desktop_files(self) -> list[str]:
        """Extract the top-level ``*.desktop`` files and ``.DirIcon``; return the desktop names."""
        if self.members is not None:
            names = sorted(
                m for m in self.members
                if "/" not in m and m.endswith(".desktop") and m not in self.dirs
            )[:MAX_DESKTOP_FILES]
            self.extract(names, MAX_DESKTOP_SIZE)
            self.extract([".DirIcon"], MAX_ICON_SIZE)
            return names
        self.reader.extract(["*.desktop", ".DirIcon"], self.root)
        with os.scandir(self.root) as it:
            names = sorted(e.name for e in it if e.name.endswith(".desktop"))
        self._attempted.update([*names, ".DirIcon"])
        return names

    def exists(self, rel: str) -> bool:
        listed = self.listed(rel)
        if listed is not None:
            return listed
        self.extract([rel])
        return os.path.lexists(os.path.join(self.root, *_split(rel)))


def _make_readable(path: Path) -> None:
    """Payload files keep their original modes; make sure we can read our own extracted copy."""
    try:
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IRUSR:
            os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRUSR)
    except OSError:
        pass


def _read_bytes(path: Path, limit: int) -> bytes | None:
    _make_readable(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        log.debug("cannot open %s: %s", path, exc)
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        data = fh.read(limit + 1)
    return data if len(data) <= limit else None


def _read_text(path: Path, limit: int) -> str | None:
    data = _read_bytes(path, limit)
    return None if data is None else data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

def _clean_text(value: str | None, limit: int = _MAX_NAME_LENGTH) -> str | None:
    """Single-line text without control characters, at most ``limit`` characters."""
    if not value:
        return None
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip() or None


def _strip_image_suffix(name: str) -> str:
    lower = name.lower()
    for suffix in _IMAGE_SUFFIXES:
        if lower.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _dir_icon_stems(files: PayloadFiles) -> set[str]:
    stems: set[str] = set()
    link = files.root / ".DirIcon"
    if link.is_symlink():
        try:
            stems.add(_strip_image_suffix(PurePosixPath(os.readlink(link)).name))
        except OSError:
            pass
    final = files.resolve(".DirIcon")
    if final is not None and final.name != ".DirIcon":
        stems.add(_strip_image_suffix(final.name))
    stems.discard("")
    return stems


def _choose_desktop(files: PayloadFiles) -> tuple[str | None, DesktopEntry | None]:
    names = files.top_level_desktop_files()
    dir_icon_stems = _dir_icon_stems(files)
    candidates: list[tuple[str, DesktopEntry]] = []
    for name in names:
        path = files.resolve(name, max_size=MAX_DESKTOP_SIZE)
        if path is None:
            continue
        text = _read_text(path, MAX_DESKTOP_SIZE)
        if text is None:
            continue
        entry = DesktopEntry.parse(text)
        if entry.has_group(DesktopEntry.MAIN):
            candidates.append((name, entry))
    if not candidates:
        return None, None

    def preference(item: tuple[str, DesktopEntry]) -> tuple[bool, bool, str]:
        name, entry = item
        visible = (
            (entry.get("Type") or "").strip() == "Application"
            and not entry.get_bool("NoDisplay")
            and not entry.get_bool("Hidden")
        )
        return (not visible, name[: -len(".desktop")] not in dir_icon_stems, name)

    return min(candidates, key=preference)


def _icon_candidates(icon_value: str, files: PayloadFiles) -> list[str]:
    candidates: list[str] = []
    rel = icon_value.strip().lstrip("/")
    base = _strip_image_suffix(PurePosixPath(rel).name) if rel else ""
    if base and base not in (".", ".."):
        candidates.append(f"usr/share/icons/hicolor/scalable/apps/{base}.svg")
        candidates += [f"usr/share/icons/hicolor/{s}x{s}/apps/{base}.png" for s in ICON_PNG_SIZES]
        candidates += [f"{base}.{ext}" for ext in ICON_EXTENSIONS]
        if files.members is not None:
            prefix = f"usr/share/pixmaps/{base}."
            found = [m for m in files.members if m.startswith(prefix) and "/" not in m[len(prefix):]]
            rank = {f"{prefix}{ext}": i for i, ext in enumerate(ICON_EXTENSIONS)}
            candidates += sorted(found, key=lambda m: (rank.get(m, len(rank)), m))
        else:
            candidates += [f"usr/share/pixmaps/{base}.{ext}" for ext in ICON_EXTENSIONS]
        if "/" in rel:
            candidates.append(rel)  # an explicit path inside the AppImage
    candidates.append(".DirIcon")
    return list(dict.fromkeys(candidates))


def _find_icon(files: PayloadFiles, entry: DesktopEntry | None) -> tuple[Path | None, ImageInfo | None]:
    icon_value = (entry.get("Icon") or "") if entry is not None else ""
    for candidate in _icon_candidates(icon_value, files):
        path = files.resolve(candidate)
        if path is None:
            continue
        size = path.stat().st_size
        if size == 0 or size > MAX_ICON_SIZE:
            log.debug("ignoring icon candidate %s (%d bytes)", candidate, size)
            continue
        _make_readable(path)
        info = probe_image(path)
        if info.format != "unknown":
            log.debug("using icon %s (%s)", candidate, info)
            return path, info
    return None, None


def _is_electron_member(member: str) -> bool:
    parts = member.split("/")
    if len(parts) > ELECTRON_MAX_DEPTH:
        return False
    return parts[-1] in ELECTRON_MARKER_NAMES or parts[-2:] == ["resources", "app.asar"]


def _is_electron(files: PayloadFiles, entry: DesktopEntry | None = None) -> bool:
    """Chromium/Electron markers at the root or in a folder like ``opt/<App>/`` (repacked .deb)."""
    if files.members is not None:
        return any(_is_electron_member(m) for m in files.members)
    # Without a listing only probe the tiny chrome-sandbox helper (app.asar can be huge), next to
    # the root and next to the program the desktop entry starts.
    folders = [""]
    program = exec_program(entry.get("Exec") or "") if entry is not None else None
    if program and "/" in program and not program.startswith("/") and ".." not in program:
        folders.append(str(PurePosixPath(program).parent))
    return any(files.exists(f"{folder}/chrome-sandbox".lstrip("/")) for folder in folders
               if folder != ".")


def _mime_types(files: PayloadFiles, entry: DesktopEntry | None) -> list[MimeTypeDef]:
    """Definitions from ``usr/share/mime/packages/*.xml`` (only if the app opens file types)."""
    if entry is None or not entry.get_list("MimeType"):
        return []
    if files.members is not None:
        names = sorted(m for m in files.members if m.startswith(MIME_PACKAGES_DIR + "/")
                       and "/" not in m[len(MIME_PACKAGES_DIR) + 1:] and m.endswith(".xml"))
    else:
        files.reader.extract([f"{MIME_PACKAGES_DIR}/*.xml"], files.root)
        try:
            with os.scandir(files.root.joinpath(*MIME_PACKAGES_DIR.split("/"))) as it:
                names = sorted(f"{MIME_PACKAGES_DIR}/{e.name}" for e in it if e.name.endswith(".xml"))
        except OSError:
            names = []
    definitions: list[MimeTypeDef] = []
    for name in names[:MAX_MIME_PACKAGES]:
        path = files.resolve(name, max_size=MAX_MIME_PACKAGE_SIZE)
        data = _read_bytes(path, MAX_MIME_PACKAGE_SIZE) if path is not None else None
        if data:
            definitions += parse_mime_package(data)  # bytes: expat honours the declared encoding
    return definitions


def _has_no_sandbox(entry: DesktopEntry | None) -> bool:
    if entry is None:
        return False
    try:
        return "--no-sandbox" in split_exec(entry.get("Exec") or "")
    except ValueError:
        return False


def _update_config(files: PayloadFiles, is_electron: bool) -> bytes | None:
    """The content of electron-builder's ``resources/app-update.yml`` (at most 64 KiB), if any.

    With a payload listing the file is found at the root or where a repacked .deb keeps the
    app (``opt/<App>/resources/``); without one it is only tried at the root of Electron apps.
    """
    if files.members is not None:
        found = sorted((m for m in files.members
                        if (m == UPDATE_CONFIG or m.endswith("/" + UPDATE_CONFIG))
                        and m.count("/") < ELECTRON_MAX_DEPTH and m not in files.dirs),
                       key=lambda m: (m.count("/"), m))
        candidates = found[:2]
    else:
        candidates = [UPDATE_CONFIG] if is_electron else []
    for rel in candidates:
        try:
            path = files.resolve(rel, max_size=MAX_UPDATE_CONFIG_SIZE)
        except (ExtractionError, OSError) as exc:   # the update source is a nicety
            log.debug("cannot read %s: %s", rel, exc)
            continue
        data = _read_bytes(path, MAX_UPDATE_CONFIG_SIZE) if path is not None else None
        if data:
            return data
    return None


def _update_source(files: PayloadFiles, is_electron: bool, update_info: str | None) -> UpdateSource | None:
    try:
        return choose_source(update_info, _update_config(files, is_electron))
    except Exception:  # noqa: BLE001 - odd update information must never stop an installation
        log.warning("cannot work out where updates come from", exc_info=True)
        return None


def _exec_name(entry: DesktopEntry | None) -> str | None:
    """The file name of the program the embedded entry starts, unless it is a generic one."""
    program = exec_program(entry.get("Exec") or "") if entry is not None else None
    name = PurePosixPath(program).name if program else ""
    return name if name and name.casefold() not in _GENERIC_PROGRAMS else None


def _data_hints(name: str | None, app_id: str, desktop_filename: str | None,
                entry: DesktopEntry | None) -> list[str]:
    stem = desktop_filename[: -len(".desktop")] if desktop_filename else None
    try:
        return data_hints_for(
            name=name, app_id=app_id, desktop_stem=stem,
            wm_class=entry.get("StartupWMClass") if entry is not None else None,
            exec_name=_exec_name(entry))
    except Exception:  # noqa: BLE001
        log.warning("cannot work out the app's data folder names", exc_info=True)
        return []


def _sha256(path: Path, size: int, progress: ProgressCallback) -> str:
    digest = hashlib.sha256()
    done = 0
    message = _("Checking the file…")
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(HASH_CHUNK_SIZE):
                digest.update(chunk)
                done += len(chunk)
                progress(min(done / size, 1.0) if size else None, message)
    except OSError as exc:
        raise ExtractionError(_("The file could not be read."), str(exc)) from exc
    return digest.hexdigest()


def arch_label(elf: ElfInfo) -> str:
    """The architecture for messages (also for machines Easy Installer has no name for)."""
    return elf.arch if elf.arch not in ("", "unknown") else f"ELF machine {elf.machine}"


def _check_payload(path: Path, elf: ElfInfo) -> None:
    magic = payload_magic(path, elf)
    if magic == SQUASHFS_MAGIC or elf.appimage_type == 1:
        return
    if elf.appimage_type == 2 and len(magic) < 4:
        raise ExtractionError(damaged_file_message(), "the file ends before the AppImage payload")
    if elf.appimage_type == 2:
        # A real AppImage with another file system inside (e.g. DwarFS, made with uruntime). Reading
        # it would mean running its own runtime, which Easy Installer never does to inspect a file.
        raise NotAnAppImageError(
            _("This AppImage is packed in a format that Easy Installer cannot read yet."),
            f"type-2 AppImage with payload magic {magic!r} (not squashfs)")
    raise NotAnAppImageError(_("This file is not an AppImage."), f"payload magic {magic!r}")


#: Archives Easy Installer cannot open (yet): their magic bytes, and suffixes for the rest.
_OTHER_ARCHIVES = ((b"\x28\xb5\x2f\xfd", ".zst"), (b"7z\xbc\xaf\x27\x1c", ".7z"),
                   (b"Rar!\x1a\x07", ".rar"), (b"\x04\x22\x4d\x18", ".lz4"),
                   (b"LZIP", ".lz"))
_OTHER_ARCHIVE_SUFFIXES = (".tar.zst", ".tzst", ".zst", ".7z", ".rar", ".tar.lz", ".tar.lzma",
                           ".tlz", ".tar.lz4", ".lz4")


def _install_the_archive_hint() -> str:
    return _("If the app came as a .zip or .tar archive, install that archive instead.")


def _what_it_is_instead(path: Path, error: NotAnAppImageError) -> NotAnAppImageError:
    """A more useful answer than "not an AppImage" for what beginners drop on the window: an
    archive Easy Installer cannot open, a program (or a folder) unpacked from an archive."""
    if path.is_dir():
        return NotAnAppImageError(
            _("This is a folder, not an app file.") + " " + _install_the_archive_hint(),
            error.details)
    if str(error) != _("This file is not an AppImage."):
        return error   # empty, damaged, unreadable: that is the answer
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return error
    lower = path.name.lower()
    suffix = next((s for s in _OTHER_ARCHIVE_SUFFIXES if lower.endswith(s)), None) \
        or next((s for magic, s in _OTHER_ARCHIVES if head.startswith(magic)), None)
    if suffix is not None:
        return NotAnAppImageError(
            _("Easy Installer cannot open this kind of file ({kind}) yet. It can install "
              "AppImages and apps that come as .zip, .tar.gz, .tar.xz or .tar.bz2 archives. "
              "Look on the maker’s website for one of those, for example an AppImage.").format(
                  kind=suffix), error.details)
    if head.startswith((ELF_MAGIC, b"#!")):
        return NotAnAppImageError(
            _("This is a program, but not an AppImage.") + " " + _install_the_archive_hint(),
            error.details)
    return error


# --------------------------------------------------------------------------------------------

def _noop(fraction: float | None, message: str) -> None:
    pass


def inspect_appimage(path: str | os.PathLike, *, compute_hash: bool = True,
                     progress: ProgressCallback | None = None) -> AppImageInfo:
    report = progress or _noop
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError) as exc:  # e.g. symlink loops
        raise NotAnAppImageError(_("The file could not be found."), str(exc)) from exc
    report(0.0, _("Checking the file…"))
    try:
        elf = read_elf_info(path)
        size = path.stat().st_size
        _check_payload(path, elf)
    except NotAnAppImageError as exc:
        raise _what_it_is_instead(path, exc) from exc

    work_dir = Path(tempfile.mkdtemp(prefix="easy-installer-"))
    owner = _WorkDirOwner(work_dir)
    try:
        os.chmod(work_dir, 0o700)
        info = _inspect(path, size, elf, work_dir, compute_hash, report)
    except BaseException:
        owner.finalizer()
        raise
    info._own_work_dir(owner)
    return info


def _inspect(path: Path, size: int, elf: ElfInfo, work_dir: Path, compute_hash: bool,
             report: ProgressCallback) -> AppImageInfo:
    root = work_dir / "root"
    root.mkdir(mode=0o700)
    report(None, _("Reading app information…"))
    reader = open_payload(path, elf)
    files = PayloadFiles(reader, root, reader.list_members(), reader.member_sizes())

    desktop_filename, entry = _choose_desktop(files)
    report(None, _("Looking for the app icon…"))
    icon_path, icon_info = _find_icon(files, entry)
    is_electron = _is_electron(files, entry)
    mime_types = _mime_types(files, entry)

    raw_name = _clean_text(entry.get("Name")) if entry is not None else None
    name = raw_name or _clean_text(name_from_filename(path.name)) or _clean_text(path.stem) or "App"
    display_name = (_clean_text(entry.get_localized("Name")) if entry is not None else None) or name
    comment = _clean_text(entry.get_localized("Comment"), limit=500) if entry is not None else None
    embedded_version = normalize_version(
        _clean_text(entry.get("X-AppImage-Version"), limit=64) if entry is not None else None)
    version = embedded_version or parse_version_from_filename(path.name)
    categories = [c.strip() for c in entry.get_list("Categories") if c.strip()] if entry is not None else []

    warnings: list[str] = []
    if is_foreign_arch(elf):
        warnings.append(_(
            "This app is made for a different kind of computer ({arch}). It will probably not "
            "start on this one.").format(arch=arch_label(elf)))
    if entry is None:
        warnings.append(_("This app does not include a menu entry. Easy Installer will create one "
                          "from the file name."))
    if icon_path is None:
        warnings.append(_("This app does not include an icon. A standard icon will be used."))

    sha256 = _sha256(path, size, report) if compute_hash else None
    update_info = read_update_info(path, elf)
    app_id = derive_app_id(desktop_filename, raw_name, path.name)
    update_source = _update_source(files, is_electron, update_info)
    # Unsigned is the norm and costs one small read; a signed file is hashed once more for gpg.
    signed = read_signature(path, elf)
    report(1.0, _("Done"))

    return AppImageInfo(
        path=path,
        size=size,
        sha256=sha256,
        elf=elf,
        appimage_type=elf.appimage_type,
        arch=elf.arch,
        app_id=app_id,
        name=name,
        display_name=display_name,
        version=version,
        comment=comment,
        categories=categories,
        desktop_entry=entry,
        desktop_filename=desktop_filename,
        icon_path=icon_path,
        icon_info=icon_info,
        is_electron=is_electron,
        exec_has_no_sandbox=_has_no_sandbox(entry),
        terminal=entry.get_bool("Terminal") if entry is not None else False,
        update_info=update_info,
        work_dir=work_dir,
        warnings=warnings,
        mime_types=mime_types,
        update_source=update_source,
        origin_url=read_origin(path),
        signature=signed,
        data_hints=_data_hints(raw_name or name, app_id, desktop_filename, entry),
    )
