"""Portable app archives (``.tar.gz``, ``.tar.xz``, ``.tar.bz2``, ``.tar``, ``.zip``).

Two jobs, both without ever running anything from the archive:

* :func:`inspect_portable` reads the member list and only a handful of small files (an embedded
  ``.desktop`` file, ``product-info.json``, ``application.ini``, a ``version`` file, the first
  bytes of the programs, one icon) to find out what the app is called, which program starts it
  and which icon it has. The archive is never unpacked for that.
* :func:`extract_portable` unpacks the archive into a fresh folder. Archives come from the
  internet, so every member is treated as hostile input (see :func:`extract_portable`).

Everything that is kept from an inspection lives in a private temporary directory
(``work_dir``, mode 0700) owned by the returned :class:`PortableInfo`, exactly like
:class:`~easy_installer.core.inspector.AppImageInfo`.
"""

from __future__ import annotations

import bz2
import errno
import gzip
import hashlib
import io
import json
import logging
import lzma
import os
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import unicodedata
import weakref
import zipfile
import zlib
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence

from ..errors import ArchiveError, InstallError
from ..i18n import _
from .appdata import data_hints_for
from .desktop_entry import DesktopEntry, exec_program, split_exec
from .elf import ELF_MAGIC, arch_name, host_arch, host_machine, normalize_arch
from .imageinfo import PNG_SIGNATURE, ImageInfo, probe_image
from .inspector import ELECTRON_MARKER_NAMES, ELECTRON_MAX_DEPTH
from .integration import (
    derive_app_id,
    name_from_filename,
    normalize_version,
    parse_version_from_filename,
    safe_file_stem,
)
from .origin import read_origin
from .squashfs import damaged_file_message, remove_tree

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float | None, str], None]

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar", ".zip")

#: Written into the app folder by the installer; an archive can never bring its own.
MARKER_NAME = ".easy-installer.json"
#: ... and the record of everything that was unpacked (see ``core.appfolder``).
MANIFEST_NAME = ".easy-installer-files.json"

# ---- limits -------------------------------------------------------------------------------
MAX_TOTAL_SIZE = 8 * 1024 ** 3          # unpacked size of one app
MAX_ENTRIES = 200_000                   # members of one archive
MAX_DESKTOP_SIZE = 256 * 1024
MAX_ICON_SIZE = 10 * 1024 * 1024
MAX_METADATA_SIZE = 1024 * 1024         # product-info.json, application.ini
MAX_VERSION_FILE_SIZE = 4 * 1024
HEAD_SIZE = 64                          # bytes read of each program candidate
MAX_EXEC_DEPTH = 2                      # "program", "bin/program" (and see _PROGRAM_DIRS)
MAX_ICON_DEPTH = 8
MAX_DESKTOP_FILES = 16
MAX_EXECUTABLES = 20                    # candidates kept in PortableInfo.executables
MAX_HEAD_READS = 2000                   # program candidates looked at (archives without exec bits)
MAX_ICON_PROBES = 200                   # PNG sizes looked up
MAX_ICON_ATTEMPTS = 8                   # icon files extracted until one is usable
MAX_LINK_TARGET = 1024                  # bytes of a symlink target
MAX_LINK_HOPS = 40                      # like the kernel
MAX_LINK_STEPS = 8192                   # path components walked for one link
MAX_TOTAL_LINK_STEPS = 2_000_000        # ... and for all links of one archive
MAX_NAME_BYTES = 255                    # one path component
MAX_PATH_BYTES = 3072                   # a member's whole path (PATH_MAX minus room for the target)
CHUNK_SIZE = 1024 * 1024
HASH_CHUNK_SIZE = 1024 * 1024
FREE_SPACE_RESERVE = 16 * 1024 * 1024

_MAX_TAR_HEADER_READ = 8 * 1024 * 1024  # largest single read tarfile may do (pax/long-name headers)
_MAX_TAR_TRAILER = 64 * 1024 * 1024     # padding after the last member that is still read
_XZ_MEMORY_LIMIT = 512 * 1024 * 1024    # `xz -9` needs 65 MiB; a hostile header can ask for 4 GiB
_PNG_HEAD = 32                          # enough for the IHDR size
_MAX_ASAR_INDEX = 16 * 1024 * 1024      # the file list at the start of an Electron app.asar
_ASAR_CAPTURE = 8 * 1024 * 1024         # its start, kept while a compressed tar passes by
_MAX_ASAR_ENTRIES = 200_000
_MAX_ASAR_ICON_DEPTH = 5
_CAPTURE_BUDGET = 32 * 1024 * 1024      # bytes a tar scan keeps in memory "just in case"
_CAPTURE_IMAGE_SIZE = 2 * 1024 * 1024
_MAX_TEXT = 200

ET_EXEC, ET_DYN = 2, 3

# ---- program scoring (see score_executable) -----------------------------------------------
SCORE_DESKTOP_EXEC = 100
SCORE_PRODUCT_LAUNCHER = 90
SCORE_NAME_EXACT = 60
SCORE_NAME_PARTIAL = 30
SCORE_GENERIC_LAUNCHER = 20
SCORE_ROOT = 10
SCORE_BIN = 8
SCORE_WRAPPER = 5
SCORE_ELF = 2
PENALTY_HELPER = -60
PENALTY_FOREIGN_ARCH = -40

_IMAGE_SUFFIXES = (".png", ".svg", ".xpm")
_LIBRARY_RE = re.compile(r"\.(?:so(?:\.[0-9][0-9A-Za-z.]*)?|node|dylib|o|a|ko)\Z", re.IGNORECASE)
#: Files that are never the program to start, whatever their permission bits say (a zip made on
#: Windows or by .NET marks every file executable).
_NOT_A_PROGRAM_SUFFIXES = (
    ".desktop", ".dll", ".pdb", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".icns",
    ".json", ".xml", ".txt", ".md", ".html", ".htm", ".css", ".pak", ".dat", ".ini", ".cfg",
    ".conf", ".jar", ".zip", ".pdf", ".ttf", ".otf", ".vmoptions", ".properties",
)
_PROGRAM_SUFFIXES = (".sh", ".bash", ".run", ".bin", ".py", ".x86_64", ".x86-64", ".amd64", ".x64",
                     ".x86", ".aarch64", ".arm64", ".appimage")
_HELPER_PARTS = (
    "sandbox", "crashpad", "crashreport", "crash-report", "crash_report", "crashhandler",
    "crash_handler", "minidump", "createdump", "updater", "uninstall", "unins0", "fsnotifier",
    "restarter", "pingsender", "plugin-container", "thumbnailer", "askpass", "elevate", "helper",
    "glxtest", "vaapitest", "qtwebengineprocess",
)
_HELPER_STEMS = frozenset({"update", "install", "installer", "setup", "remove", "postinstall",
                           "post-install", "preinstall", "configure"})
_GENERIC_LAUNCHERS = frozenset({"apprun", "run", "start", "launch", "launcher", "startup"})
#: Folders whose programs are candidates although they lie deeper than MAX_EXEC_DEPTH: archives
#: laid out like a package ("usr/bin/app", "usr/share/applications/app.desktop").
_PROGRAM_DIRS = ("bin", "usr/bin", "usr/local/bin")
#: Files at the top of a source code tree (a "tarball" of a program that still has to be built).
_SOURCE_MARKERS = frozenset({
    "configure", "configure.ac", "configure.in", "autogen.sh", "Makefile.am", "Makefile.in",
    "CMakeLists.txt", "meson.build", "setup.py", "pyproject.toml", "Cargo.toml", "go.mod",
    "build.gradle", "pom.xml", "SConstruct", "BUILD.bazel"})
#: An AppImage is an ELF program with this magic at offset 8 (type 1 or 2).
_APPIMAGE_MAGICS = (b"AI\x01", b"AI\x02")
_GENERIC_FOLDERS = frozenset({"app", "application", "dist", "build", "release", "bin", "package",
                              "portable", "out", "output", "publish", "linux", "files", "program",
                              "artifact", "artifacts", "download", "downloads", "bundle",
                              "binaries", "binary", "deploy", "latest"})
_STRONG_ICON_NAMES = frozenset({"icon", "logo", "appicon", "applogo", "productlogo", "desktopicon",
                                "launchericon"})
_WEAK_ICON_NAMES = frozenset({"", "default", "app", "application", "main"})
_ICON_DIRS = frozenset({"icons", "icon", "pixmaps"})
_IGNORED_TOP = "__MACOSX"               # resource forks added by macOS' archive tool

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_VERSION_TEXT_RE = re.compile(r"[vV]?\d+(?:\.\d+)+[0-9A-Za-z.+~_-]{0,40}\Z")
_TRAILING_VERSION_RE = re.compile(r"(?:^|[._-])[vV]?(\d+(?:\.\d+){1,3})\Z")
_ICON_SIZE_RE = re.compile(r"@\d+x|\d+x\d+|\d+")

_PERMISSION_ERRNOS = (errno.EACCES, errno.EPERM, errno.EROFS)


# --------------------------------------------------------------------------------------------
# Public data
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutableCandidate:
    relpath: str        # inside the app folder, e.g. "bin/idea.sh"
    kind: str           # "elf" | "script"
    score: int          # see score_executable()


@dataclass
class PortableInfo:
    path: Path
    size: int
    sha256: str | None
    app_id: str
    name: str
    display_name: str
    version: str | None
    comment: str | None
    categories: list[str]
    terminal: bool
    strip_prefix: str | None
    desktop_entry: DesktopEntry | None
    desktop_filename: str | None
    executables: list[ExecutableCandidate]
    executable: str
    icon_path: Path | None
    icon_info: ImageInfo | None
    is_electron: bool
    arch: str | None
    tree_size: int
    file_count: int
    work_dir: Path
    warnings: list[str] = field(default_factory=list)
    #: the embedded Exec already contains --no-sandbox (same meaning as AppImageInfo's field)
    exec_has_no_sandbox: bool = False
    #: window class for StartupWMClass when known (product-info.json "startupWmClass")
    wm_class: str | None = None
    #: where the archive was downloaded from, if the browser noted it on the file
    origin_url: str | None = None

    @property
    def data_hints(self) -> list[str]:
        """Names under which the app may keep its settings and data (``core.appdata``); they
        follow the chosen ``executable``."""
        stem = self.desktop_filename[: -len(".desktop")] if self.desktop_filename else None
        entry_class = self.desktop_entry.get("StartupWMClass") if self.desktop_entry is not None else None
        return data_hints_for(name=self.name, app_id=self.app_id, desktop_stem=stem,
                              wm_class=entry_class or self.wm_class,
                              exec_name=PurePosixPath(self.executable).name)

    def has_member(self, relpath: str) -> bool | None:
        """``relpath`` is a file (or link) of the app folder; None: not known (an info that
        :func:`inspect_portable` did not make)."""
        members = self.__dict__.get("_members")
        return None if members is None else relpath in members

    def _own_work_dir(self, owner: "_WorkDirOwner") -> None:
        # Deliberately not dataclass fields: asdict()/JSON output stays the documented fields.
        self.__dict__["_work_dir_owner"] = owner
        self.__dict__["_finalizer"] = owner.finalizer

    def cleanup(self) -> None:
        finalizer = self.__dict__.get("_finalizer")
        if finalizer is not None:
            finalizer()  # runs remove_tree at most once
        else:
            remove_tree(self.work_dir)

    def __enter__(self) -> "PortableInfo":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


class _WorkDirOwner:
    """Removes the work dir when it is garbage-collected or when the program ends (also while
    the archive is still being read)."""

    def __init__(self, work_dir: Path):
        self.finalizer = weakref.finalize(self, remove_tree, work_dir)


def adopt_work_dir(info: PortableInfo) -> PortableInfo:
    """Tie ``info.work_dir`` to the life of ``info``: it is removed by ``cleanup()``, when the
    object is garbage-collected or when the program ends. For a :class:`PortableInfo` that was
    not made by :func:`inspect_portable` (the installer describes an installed folder with one)."""
    info._own_work_dir(_WorkDirOwner(info.work_dir))
    return info


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------

def _damaged(details: str | None = None) -> ArchiveError:
    return ArchiveError(damaged_file_message(), details)


def _not_an_archive(details: str | None = None) -> ArchiveError:
    return ArchiveError(_("This file is not an app archive that Easy Installer can open."), details)


def _not_an_app(details: str | None = None) -> ArchiveError:
    return ArchiveError(
        _("This archive does not look like an app. Easy Installer could not find a program to "
          "start in it."), details)


def _installer_inside(program: str, details: str | None = None) -> ArchiveError:
    return ArchiveError(
        _("This archive contains an installer ({program}), not an app that is ready to use. "
          "Unpack the archive and follow the instructions that came with it, or look for an "
          "AppImage of this app.").format(program=program), details)


def _source_code(details: str | None = None) -> ArchiveError:
    return ArchiveError(
        _("This archive contains the source code of a program, not an app that is ready to "
          "use. Look on the maker’s website for a version for Linux that is ready to use, for "
          "example an AppImage."), details)


def _appimage_inside(program: str, details: str | None = None) -> ArchiveError:
    return ArchiveError(
        _("This archive contains the AppImage “{name}”. Please unpack the archive first (in "
          "Files: right-click it and choose “Extract”), then install “{name}” with Easy "
          "Installer.").format(name=program), details)


def _unsafe(details: str | None = None) -> ArchiveError:
    return ArchiveError(
        _("This archive is built in an unsafe way, so Easy Installer will not unpack it."), details)


def _too_large(details: str | None = None) -> ArchiveError:
    return ArchiveError(_("This archive is too large for Easy Installer to unpack."), details)


def _changed(details: str | None = None) -> ArchiveError:
    return ArchiveError(_("The archive was changed after it was opened. Please try again."), details)


def _write_error(exc: OSError) -> InstallError:
    """A friendly error for a failed write (same wording as the AppImage installer)."""
    details = f"{type(exc).__name__}: {exc}"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return InstallError(
            _("There is not enough free disk space. Please free up some space and try again."),
            details)
    if exc.errno in _PERMISSION_ERRNOS and exc.filename:
        return InstallError(
            _("Easy Installer is not allowed to change this location: {path}").format(
                path=exc.filename), details)
    return InstallError(
        _("The app could not be installed because a file could not be copied or saved."), details)


@contextmanager
def _reading() -> Iterator[None]:
    """Turn every way the stdlib reports a broken archive into a friendly :class:`ArchiveError`."""
    try:
        yield
    except ArchiveError:
        raise
    except NotImplementedError as exc:   # zip: compression method / format version
        raise ArchiveError(
            _("This archive is packed in a way that Easy Installer cannot read yet."),
            f"{type(exc).__name__}: {exc}") from exc
    except (tarfile.TarError, zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError,
            struct.error, ValueError, OverflowError, RuntimeError, MemoryError) as exc:
        raise _damaged(f"{type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        if exc.errno is None or isinstance(exc, gzip.BadGzipFile):   # bz2/gzip: invalid data
            raise _damaged(f"{type(exc).__name__}: {exc}") from exc
        raise ArchiveError(_("The file could not be read."), f"{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------------------------
# File type
# --------------------------------------------------------------------------------------------

_MAGIC = (
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"\x1f\x8b", "gz"),
    (b"\xfd7zXZ\x00", "xz"), (b"BZh", "bz2"),
)


def _looks_like_tar(block: bytes) -> bool:
    """A tar header block: the "ustar" magic, or (old v7 archives) a correct header checksum."""
    if len(block) < 512:
        return False
    if block[257:262] == b"ustar":
        return True
    try:
        stored = int(block[148:156].split(b"\0", 1)[0].strip() or b"-", 8)
    except ValueError:
        return False
    return stored == sum(block[:148]) + 8 * 0x20 + sum(block[156:512])


def _read_head(path: str | os.PathLike, size: int = 512) -> bytes | None:
    """The first bytes of a regular file; None for anything else (never blocks on a FIFO)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, size)
    except OSError:
        return None
    finally:
        os.close(fd)


def _format_of(head: bytes | None) -> str | None:
    if not head:
        return None
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    return "tar" if _looks_like_tar(head) else None


def archive_suffix(filename: str) -> str | None:
    """The archive suffix of a file name (".tar.gz", ".zip", ...) in lower case, or None."""
    lower = filename.lower()
    return next((s for s in ARCHIVE_SUFFIXES if lower.endswith(s) and len(lower) > len(s)), None)


def archive_stem(filename: str) -> str:
    """``"blender-4.2.0-linux-x64.tar.xz"`` -> ``"blender-4.2.0-linux-x64"``."""
    name = PurePosixPath(filename).name
    suffix = archive_suffix(name)
    return name[: -len(suffix)] if suffix else name


def is_portable_archive(path: Path) -> bool:
    """The file has an archive suffix AND starts like an archive Easy Installer can read.

    The content decides how the file is read, so a ``.tar.gz`` that a browser already
    decompressed (a plain tar under its old name) still counts. Never raises.
    """
    try:
        name = os.fspath(path)
    except TypeError:
        return False
    if archive_suffix(os.path.basename(name)) is None:
        return False
    return _format_of(_read_head(name)) is not None


# --------------------------------------------------------------------------------------------
# Member names and in-archive links
# --------------------------------------------------------------------------------------------

def _safe_relpath(raw: str) -> str:
    """``"./a//b/"`` -> ``"a/b"`` (``""`` = the archive root). Raises for names that must never
    be unpacked: absolute paths, ``..``, control characters, over-long names."""
    if not isinstance(raw, str) or raw.startswith("/") or _CONTROL_RE.search(raw):
        raise _unsafe(f"member name {raw!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise _unsafe(f"member name {raw!r} leaves the archive")
    try:
        lengths = [len(os.fsencode(part)) for part in parts]
    except UnicodeError as exc:
        raise _unsafe(f"member name {raw!r} cannot be encoded") from exc
    if any(n > MAX_NAME_BYTES for n in lengths) or sum(lengths) + len(lengths) > MAX_PATH_BYTES:
        raise _unsafe(f"member name of {sum(lengths)} bytes is too long")
    return "/".join(parts)


def _is_ignored(path: str) -> bool:
    return path == _IGNORED_TOP or path.startswith(_IGNORED_TOP + "/")


def _strip_prefix(path: str, prefix: str | None) -> str | None:
    """``path`` inside the app folder; ``""`` for the stripped folder itself, None if outside."""
    if not prefix:
        return path
    if path == prefix:
        return ""
    return path[len(prefix) + 1:] if path.startswith(prefix + "/") else None


class _LinkResolver:
    """Follows symlinks of the archive on paper - nothing on disk is consulted.

    ``links`` maps a path inside the app folder to its link target. :meth:`resolve` walks a
    target the way the kernel would and answers where it ends, or None when the way leaves the
    app folder (absolute target, ``..`` above the top), loops, or is absurdly long. Components
    that are not links are taken as they are, so a dangling link inside the folder is fine.
    """

    def __init__(self, links: Mapping[str, str]):
        self.links = links
        self.steps = 0

    def resolve(self, base: Sequence[str], target: str) -> list[str] | None:
        if not target or target.startswith("/"):
            return None
        stack = list(base)
        todo = deque(target.split("/"))
        hops = 0
        budget = MAX_LINK_STEPS
        while todo:
            budget -= 1
            self.steps += 1
            if budget < 0:
                return None
            if self.steps > MAX_TOTAL_LINK_STEPS:
                raise _unsafe("the links of this archive are too entangled to check")
            part = todo.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if not stack:
                    return None
                stack.pop()
                continue
            link = self.links.get("/".join([*stack, part]))
            if link is None:
                stack.append(part)
                continue
            hops += 1
            if hops > MAX_LINK_HOPS or not link or link.startswith("/"):
                return None
            todo.extendleft(reversed(link.split("/")))
        return stack

    def resolve_path(self, path: str) -> str | None:
        """The final path ``path`` stands for (itself unless links are involved)."""
        resolved = self.resolve((), path) if path else []
        return None if resolved is None else "/".join(resolved)


def _valid_link_target(target: str) -> bool:
    if not target or "\0" in target:
        return False
    try:
        return len(os.fsencode(target)) <= MAX_LINK_TARGET
    except UnicodeError:
        return False


# --------------------------------------------------------------------------------------------
# Archive readers
# --------------------------------------------------------------------------------------------

@dataclass
class _Member:
    path: str                   # safe relative path as stored ("" = the archive root itself)
    kind: str                   # "file" | "dir" | "symlink" | "hardlink" | "other"
    size: int = 0               # declared size of a regular file
    perm: int | None = None     # permission bits from the archive; None: it has none
    target: str = ""            # symlink: target as stored; hardlink: safe path in the archive
    handle: object = None       # TarInfo / ZipInfo
    data: bytes | None = None   # what a scan kept of the content (may be only the beginning)


class _GuardedStream:
    """The (decompressed) tar stream as tarfile sees it.

    tarfile reads a pax or GNU long-name header in one piece, whatever size the header
    declares; without this guard a few bytes of archive could ask for gigabytes of memory.
    ``last`` is what the most recent read returned (see :meth:`_TarReader._check_end`).
    """

    def __init__(self, stream: io.BufferedIOBase):
        self._stream = stream
        self.last = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > _MAX_TAR_HEADER_READ:
            raise tarfile.ReadError(f"implausible read of {size} bytes")
        self.last = self._stream.read(size)
        return self.last

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if offset < 0:
            raise tarfile.ReadError(f"implausible seek to {offset}")
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        return self._stream.seekable()


def _open_xz(raw: io.BufferedIOBase) -> io.BufferedIOBase:
    """Like ``lzma.LZMAFile(raw)`` but with a memory limit for the decoder."""
    try:
        import _compression as streams
    except ImportError:  # Python 3.14+ moved it
        try:
            from compression._common import _streams as streams  # type: ignore[no-redef]
        except ImportError:
            return lzma.LZMAFile(raw)
    reader = streams.DecompressReader(raw, lzma.LZMADecompressor, trailing_error=lzma.LZMAError,
                                      format=lzma.FORMAT_AUTO, memlimit=_XZ_MEMORY_LIMIT)
    return io.BufferedReader(reader)


class _Reader:
    """What inspection and unpacking need from an archive, whatever its format."""

    #: members can only be read cheaply while the scan passes them (compressed tar)
    sequential = False
    #: any part of a member can be read without decompressing the archive again
    random_access = True
    #: called now and then while a long stretch of the archive is read (for progress)
    tick: Callable[[], None] | None = None

    def members(self) -> Iterator[_Member]:
        raise NotImplementedError

    def read(self, member: _Member, limit: int) -> bytes:
        """Up to ``limit`` bytes from the start of a regular file."""
        raise NotImplementedError

    def read_range(self, member: _Member, offset: int, size: int) -> bytes:
        """``size`` bytes of a regular file from ``offset`` on (fewer at its end)."""
        raise NotImplementedError

    def chunks(self, member: _Member) -> Iterator[bytes]:
        """The whole content of a regular file, piece by piece."""
        raise NotImplementedError

    def fraction(self) -> float | None:
        """How much of the archive file has been read (for progress), if that is known."""
        return None

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _TarReader(_Reader):
    sequential = True

    def __init__(self, path: Path | BinaryIO, fmt: str):
        self._raw = path if not isinstance(path, (str, os.PathLike)) else open(path, "rb")
        self._stream: io.BufferedIOBase | None = None
        self.random_access = fmt == "tar"
        self._guard: _GuardedStream
        try:
            self._size = os.fstat(self._raw.fileno()).st_size
            with _reading():
                if fmt == "gz":
                    self._stream = gzip.GzipFile(fileobj=self._raw, mode="rb")
                elif fmt == "xz":
                    self._stream = _open_xz(self._raw)
                elif fmt == "bz2":
                    self._stream = bz2.BZ2File(self._raw, "rb")
                self._guard = _GuardedStream(self._stream or self._raw)
                try:
                    self._tar = tarfile.TarFile(fileobj=self._guard, mode="r")  # type: ignore[arg-type]
                except tarfile.ReadError as exc:
                    if len(self._guard.last) < tarfile.BLOCKSIZE:
                        raise      # not even one block could be read: damaged (see _reading)
                    # Either damaged compressed data that decodes to nonsense (reading on
                    # makes the checksum fail), or a compressed file that is no tar archive.
                    self._drain()
                    raise _not_an_archive(f"{type(exc).__name__}: {exc}") from exc
        except BaseException:
            self.close()
            raise

    def members(self) -> Iterator[_Member]:
        last_offset = -1
        while True:
            with _reading():
                info = self._tar.next()
            if info is None:
                self._check_end()
                return
            # A header that points backwards would be read again and again, forever.
            if info.offset <= last_offset or info.size < 0:
                raise _damaged(f"tar header at {info.offset} after {last_offset}, size {info.size}")
            last_offset = info.offset
            yield self._member(info)
            self._skip_member_data()

    def _skip_member_data(self) -> None:
        """Pass over what is left of the current member piece by piece.

        tarfile would do it in one step - seconds without a sign of life when a big file of a
        compressed archive goes by. A plain tar is left to tarfile: it seeks, which is free.
        """
        if self._stream is None or self.tick is None:
            return
        with _reading():
            remaining = self._tar.offset - self._guard.tell()   # up to the next header
            while remaining > CHUNK_SIZE:       # the last piece is tarfile's (it checks the end)
                piece = self._guard.read(CHUNK_SIZE)
                if not piece:
                    return
                remaining -= len(piece)
                self.tick()

    def _check_end(self) -> None:
        """Make sure the member list really ended, and that the file is intact up to its end.

        tarfile takes a header it cannot read for the end of the archive, so a damaged file
        would silently lose the rest of its members: the block that ended the list must be
        tar's end marker (zeros) or the end of the file. What follows is read as well (it is
        only padding): gzip, xz and bzip2 verify their checksums when their end is reached -
        without this a download with flipped bits would be unpacked without anyone noticing.
        """
        block = self._guard.last
        if block and (len(block) < tarfile.BLOCKSIZE or block.strip(b"\0")):
            raise _damaged(f"unreadable tar header at {self._tar.offset}")
        self._drain()

    def _drain(self) -> None:
        """Read on to the end of the (decompressed) stream, within reason."""
        remaining = _MAX_TAR_TRAILER
        with _reading():
            while remaining > 0:
                chunk = self._guard.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        log.debug("more than %d bytes follow; not read", _MAX_TAR_TRAILER)

    @staticmethod
    def _member(info: tarfile.TarInfo) -> _Member:
        path = _safe_relpath(info.name)
        perm = info.mode & 0o7777
        if info.isdir():
            return _Member(path, "dir", perm=perm, handle=info)
        if info.issym():
            return _Member(path, "symlink", target=info.linkname, handle=info)
        if info.islnk():
            try:
                target = _safe_relpath(info.linkname)
            except ArchiveError:
                target = ""     # a hard link to somewhere outside: skipped later, never created
            return _Member(path, "hardlink", perm=perm, target=target, handle=info)
        if info.isreg():
            return _Member(path, "file", size=info.size, perm=perm, handle=info)
        if info.ischr() or info.isblk() or info.isfifo():
            raise _unsafe(f"{info.name!r} is a device or pipe (tar type {info.type!r})")
        return _Member(path, "other", handle=info)   # volume labels and the like: ignored

    def _open(self, member: _Member) -> io.BufferedIOBase:
        fh = self._tar.extractfile(member.handle)  # type: ignore[arg-type]
        if fh is None:
            raise _damaged(f"{member.path!r} has no content")
        return fh

    def read(self, member: _Member, limit: int) -> bytes:
        pieces: list[bytes] = []
        remaining = min(limit, member.size)
        with _reading():
            fh = self._open(member)
            while remaining > 0:
                piece = fh.read(min(CHUNK_SIZE, remaining))
                if not piece:
                    break
                pieces.append(piece)
                remaining -= len(piece)
        return b"".join(pieces)

    def read_range(self, member: _Member, offset: int, size: int) -> bytes:
        with _reading():
            fh = self._open(member)
            fh.seek(offset)
            return fh.read(size)

    def chunks(self, member: _Member) -> Iterator[bytes]:
        with _reading():
            fh = self._open(member)
        remaining = member.size
        while remaining > 0:
            with _reading():
                chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise _damaged(f"{member.path!r} ends {remaining} bytes early")
            remaining -= len(chunk)
            yield chunk

    def fraction(self) -> float | None:
        try:
            return min(self._raw.tell() / self._size, 1.0) if self._size else None
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        for stream in (self._stream, self._raw):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class _ZipReader(_Reader):
    def __init__(self, path: Path | BinaryIO):
        self._no_modes = False
        with _reading():
            self._zip = zipfile.ZipFile(path)

    def members(self) -> Iterator[_Member]:
        infos = self._zip.infolist()
        if len(infos) > MAX_ENTRIES:
            raise _too_large(f"{len(infos)} entries")
        # A zip in which no file at all is executable carries no usable permissions (made on
        # Windows, or by a tool that stores 0600/0644 for everything): programs are then
        # recognised by their content instead.
        self._no_modes = not any(
            (info.external_attr >> 16) & 0o111 and not info.filename.endswith("/")
            and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG) for info in infos)
        for info in infos:
            yield self._member(info)

    def _member(self, info: zipfile.ZipInfo) -> _Member:
        if info.flag_bits & 0x1:
            raise ArchiveError(
                _("This archive is protected with a password. Easy Installer cannot open it."),
                f"{info.filename!r} is encrypted")
        path = _safe_relpath(info.filename)
        mode = info.external_attr >> 16          # Unix mode, 0 if the archive has none
        kind = stat.S_IFMT(mode)
        perm = None if self._no_modes else (mode & 0o7777) or None
        if info.filename.endswith("/") or kind == stat.S_IFDIR:
            return _Member(path, "dir", perm=perm, handle=info)
        if info.file_size < 0:
            raise _damaged(f"{info.filename!r} has size {info.file_size}")
        member = _Member(path, "file", size=info.file_size, perm=perm, handle=info)
        if kind == stat.S_IFLNK:
            target = ""     # an over-long target is no target: the link is left out
            if info.file_size <= MAX_LINK_TARGET:
                target = self.read(member, MAX_LINK_TARGET).decode("utf-8", errors="surrogateescape")
            return _Member(path, "symlink", target=target, handle=info)
        if kind not in (0, stat.S_IFREG):
            raise _unsafe(f"{info.filename!r} is a device or pipe (mode {mode:o})")
        return member

    def read(self, member: _Member, limit: int) -> bytes:
        with _reading():
            with self._zip.open(member.handle) as fh:  # type: ignore[arg-type]
                return fh.read(limit)

    def read_range(self, member: _Member, offset: int, size: int) -> bytes:
        with _reading():
            with self._zip.open(member.handle) as fh:  # type: ignore[arg-type]
                fh.seek(offset)
                return fh.read(size)

    def chunks(self, member: _Member) -> Iterator[bytes]:
        with _reading():
            fh = self._zip.open(member.handle)  # type: ignore[arg-type]
        done = 0
        try:
            while True:
                with _reading():
                    chunk = fh.read(CHUNK_SIZE)   # never more than declared; checks the CRC at the end
                if not chunk:
                    break
                done += len(chunk)
                yield chunk
        finally:
            fh.close()
        if done != member.size:
            raise _damaged(f"{member.path!r} has {done} bytes, not {member.size}")

    def close(self) -> None:
        self._zip.close()


def _open_reader(path: Path | BinaryIO, fmt: str | None = None) -> _Reader:
    """A reader of the archive ``path`` - or of the open file ``path`` (read from its start)."""
    if not isinstance(path, (str, os.PathLike)):
        path.seek(0)
        fmt = fmt or _format_of(path.read(512))
        path.seek(0)
    else:
        fmt = fmt or _format_of(_read_head(path))
    if fmt is None:
        raise _not_an_archive(f"{path}: no known archive signature")
    try:
        return _ZipReader(path) if fmt == "zip" else _TarReader(path, fmt)
    except FileNotFoundError as exc:
        raise ArchiveError(_("The file could not be found."), str(exc)) from exc
    except OSError as exc:
        raise ArchiveError(_("The file could not be read."), str(exc)) from exc


# --------------------------------------------------------------------------------------------
# Scanning: the member list and what is kept from it
# --------------------------------------------------------------------------------------------

def _depth(path: str) -> int:
    return path.count("/") + 1 if path else 0


def _near_top(path: str) -> bool:
    """Where the program that starts an app is looked for: at most MAX_EXEC_DEPTH deep, or in
    one of the _PROGRAM_DIRS."""
    return _depth(path) <= MAX_EXEC_DEPTH or path.rpartition("/")[0] in _PROGRAM_DIRS


def _is_utf8(path: str) -> bool:
    """A name that can be written into a menu entry (those are UTF-8; tar names that are not
    are decoded with surrogates)."""
    try:
        path.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _is_image(path: str) -> bool:
    return path.lower().endswith(_IMAGE_SUFFIXES)


def _metadata_limit(path: str) -> int:
    """How many bytes of this file (path inside the app folder) inspection may read; 0 = none."""
    depth = _depth(path)
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".desktop"):
        return MAX_DESKTOP_SIZE if depth <= 4 else 0
    if name in ("product-info.json", "application.ini"):
        return MAX_METADATA_SIZE if depth <= 2 else 0
    if name.lower() in ("version", "version.txt"):
        return MAX_VERSION_FILE_SIZE if depth == 1 else 0
    return 0


class _CapturePolicy:
    """What a tar scan keeps of a member while it passes by.

    A compressed tar can only be read front to back, and the single top-level folder is not
    known before the end. So the scan keeps what inspection will probably ask for (one level
    deeper than needed): the first bytes of possible programs, the small metadata files and -
    within a memory budget - images that sit where icons usually sit. Anything else that turns
    out to be needed is read in a second pass.
    """

    def __init__(self) -> None:
        self.budget = _CAPTURE_BUDGET

    def wanted(self, member: _Member) -> int:
        """How many bytes from the start of ``member`` to keep (its size: all of it; 0: none)."""
        if member.kind != "file" or member.size <= 0:
            return 0
        path = member.path
        inner = path.split("/", 1)[1] if "/" in path else path   # as if a top folder is stripped
        head = 0
        if (_near_top(inner) or _near_top(path)) and (member.perm is None or member.perm & 0o111):
            head = HEAD_SIZE
        whole = member.size <= max(_metadata_limit(path), _metadata_limit(inner))
        if not whole and _is_image(path) and member.size <= MAX_ICON_SIZE \
                and _depth(inner) <= MAX_ICON_DEPTH:
            head = max(head, _PNG_HEAD)
            folders = set(path.lower().split("/")[:-1])
            whole = member.size <= _CAPTURE_IMAGE_SIZE and (
                _depth(inner) <= 2 or bool(folders & _ICON_DIRS))
        if whole and member.size <= self.budget:
            self.budget -= member.size
            return member.size
        if path.endswith("resources/app.asar") and _depth(inner) <= 3:
            # (an Electron app's icon may only be in there: its file list and first files)
            amount = min(member.size, _ASAR_CAPTURE, self.budget)
            self.budget -= amount
            return max(head, amount)
        return head


def _common_prefix(members: Sequence[_Member]) -> str | None:
    """The single top-level folder everything lies in, or None."""
    tops = {m.path.split("/", 1)[0] for m in members}
    if len(tops) != 1:
        return None
    top = tops.pop()
    if any(m.path == top and m.kind != "dir" for m in members):
        return None
    return top if any(m.path.startswith(top + "/") for m in members) else None


class _Tree:
    """The archive's content as it will lie in the app folder (top folder already stripped)."""

    def __init__(self, members: Sequence[_Member]):
        self.prefix = _common_prefix(members)
        self.files: dict[str, _Member] = {}
        self.links: dict[str, str] = {}
        self.tree_size = 0
        self.file_count = 0
        hardlinks: list[tuple[str, str]] = []
        folders: set[str] = set()
        for member in members:
            path = _strip_prefix(member.path, self.prefix)
            if not path or path in (MARKER_NAME, MANIFEST_NAME):
                continue
            if member.kind == "dir":
                folders.add(path)
            elif member.kind == "file":
                self.files[path] = member
                self.tree_size += member.size
                self.file_count += 1
            elif member.kind == "symlink":
                self.links.setdefault(path, member.target)
                self.file_count += 1
            elif member.kind == "hardlink":
                hardlinks.append((path, member.target))
        for path, target in hardlinks:
            source = self.files.get(_strip_prefix(target, self.prefix) or "") if target else None
            if source is not None and path not in self.files:
                self.files[path] = source
                self.file_count += 1
        # Like on disk after unpacking: a name that is a file or a folder is not a link.
        folders.update(parent for path in self.files for parent in _parents(path))
        self.links = {path: target for path, target in self.links.items()
                      if path not in self.files and path not in folders}
        self.resolver = _LinkResolver(self.links)

    def lookup(self, path: str) -> _Member | None:
        """The regular file at ``path``, following links inside the app folder."""
        member = self.files.get(path)
        if member is not None or not self.links:
            return member
        resolved = self.resolver.resolve_path(path)
        return self.files.get(resolved) if resolved else None


def _parents(path: str) -> Iterator[str]:
    index = path.find("/")
    while index >= 0:
        yield path[:index]
        index = path.find("/", index + 1)


class _Scan:
    """The member list of an archive plus on-demand access to small pieces of its content."""

    def __init__(self, reader: _Reader, report: ProgressCallback):
        self.reader = reader
        policy = _CapturePolicy() if reader.sequential else None
        members: list[_Member] = []
        total = 0
        count = 0
        reported = 0.0
        message = _("Reading app information…")

        def tick(force: bool = False) -> None:
            # A tar is read through to list it: say how far that is whenever another percent
            # of the file is done (a single member can be huge).
            nonlocal reported
            fraction = reader.fraction()
            if force or (fraction is not None and fraction - reported >= 0.01):
                reported = fraction or reported
                report(fraction, message)

        reader.tick = tick
        for member in reader.members():
            count += 1
            if count > MAX_ENTRIES:
                raise _too_large(f"more than {MAX_ENTRIES} entries")
            tick(force=count % 512 == 0)
            if not member.path or member.kind == "other" or _is_ignored(member.path):
                continue
            total += member.size
            if total > MAX_TOTAL_SIZE:
                raise _too_large(f"more than {MAX_TOTAL_SIZE} bytes unpacked")
            if policy is not None:
                wanted = policy.wanted(member)
                if wanted:
                    member.data = reader.read(member, wanted)
            members.append(member)
        reader.tick = None
        self.tree = _Tree(members)

    def head(self, member: _Member, size: int) -> bytes:
        """The first ``size`` bytes of a file (fewer if it is shorter)."""
        size = min(size, member.size)
        if member.data is None or len(member.data) < size:
            member.data = self.reader.read(member, size)
        return member.data[:size]

    def content(self, path: str, limit: int) -> bytes | None:
        """The whole file at ``path`` (links inside the app are followed), None if it is
        missing, empty or larger than ``limit``."""
        member = self.tree.lookup(path)
        if member is None or not 0 < member.size <= limit:
            return None
        data = self.head(member, member.size)
        return data if len(data) == member.size else None

    def text(self, path: str, limit: int) -> str | None:
        data = self.content(path, limit)
        return None if data is None else data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------------------------
# Metadata files
# --------------------------------------------------------------------------------------------

def _clean_text(value: object, limit: int = _MAX_TEXT) -> str | None:
    """Single-line text without control characters, at most ``limit`` characters."""
    if not isinstance(value, str) or not value:
        return None
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cs") else c for c in value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip() or None


def _clean_version(value: object) -> str | None:
    text = normalize_version(_clean_text(value, limit=64))
    return text if text and any(c.isdigit() for c in text) else None


def _desktop_paths(tree: _Tree) -> list[str]:
    """Menu entries the archive brings along: at the top, else where packages keep them."""
    names = sorted({*tree.files, *tree.links})
    for folder in ("", "share/applications/", "usr/share/applications/"):
        found = [p for p in names if p.startswith(folder) and "/" not in p[len(folder):]
                 and p.lower().endswith(".desktop")]
        if found:
            return found[:MAX_DESKTOP_FILES]
    return []


def _choose_desktop(scan: _Scan) -> tuple[str | None, DesktopEntry | None]:
    candidates: list[tuple[str, DesktopEntry]] = []
    for path in _desktop_paths(scan.tree):
        text = scan.text(path, MAX_DESKTOP_SIZE)
        if text is None:
            continue
        entry = DesktopEntry.parse(text)
        if entry.has_group(DesktopEntry.MAIN) and (entry.get("Exec") or entry.get("Name")):
            candidates.append((path, entry))
    if not candidates:
        return None, None

    def preference(item: tuple[str, DesktopEntry]) -> tuple[bool, str]:
        path, entry = item
        visible = ((entry.get("Type") or "Application").strip() == "Application"
                   and not entry.get_bool("NoDisplay") and not entry.get_bool("Hidden"))
        return (not visible, path)

    return min(candidates, key=preference)


@dataclass
class _ProductInfo:
    """JetBrains ``product-info.json``."""
    name: str | None = None
    version: str | None = None
    launcher: str | None = None     # launch[].launcherPath of the Linux entry
    icon: str | None = None         # svgIconPath
    wm_class: str | None = None


def _safe_inner_path(value: object) -> str | None:
    """A path from a metadata file as a safe path inside the app folder, else None."""
    if not isinstance(value, str) or not value or len(value) > MAX_LINK_TARGET:
        return None
    try:
        return _safe_relpath(value.lstrip("/")) or None
    except ArchiveError:
        return None


def _parse_product_info(data: bytes | None) -> _ProductInfo | None:
    if not data:
        return None
    try:
        doc = json.loads(data.decode("utf-8-sig", errors="replace"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    info = _ProductInfo(name=_clean_text(doc.get("name")), version=_clean_version(doc.get("version")),
                        icon=_safe_inner_path(doc.get("svgIconPath")))
    launches = [e for e in doc.get("launch") or [] if isinstance(e, dict)] \
        if isinstance(doc.get("launch"), list) else []
    linux = [e for e in launches if str(e.get("os", "")).casefold() == "linux"]
    host = host_arch()
    linux.sort(key=lambda e: normalize_arch(str(e.get("arch", ""))) != host)   # stable: host first
    for entry in linux:
        launcher = _safe_inner_path(entry.get("launcherPath"))
        if launcher:
            info.launcher = launcher
            info.wm_class = _clean_text(entry.get("startupWmClass"), limit=100)
            break
    return info


def _parse_application_ini(text: str | None) -> dict[str, str]:
    """The ``[App]`` section of a Mozilla ``application.ini`` (Name, Version, RemotingName, ...)."""
    values: dict[str, str] = {}
    section = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif section == "App" and "=" in line and not line.startswith((";", "#")):
            key, _sep, value = line.partition("=")
            values.setdefault(key.strip(), value.strip())
    return values


def _version_from_stem(stem: str | None) -> str | None:
    """The version in a folder or archive name; also ``"tsetup.5.2.3"`` -> ``"5.2.3"``."""
    if not stem:
        return None
    version = parse_version_from_filename(stem)
    if version is None:
        match = _TRAILING_VERSION_RE.search(stem)
        version = match.group(1) if match else None
    return version


def _version_file(scan: _Scan) -> str | None:
    for name in ("version", "VERSION", "version.txt", "VERSION.txt"):
        text = scan.text(name, MAX_VERSION_FILE_SIZE)
        line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
        if _VERSION_TEXT_RE.fullmatch(line):
            return normalize_version(line)
    return None


def _is_electron(tree: _Tree) -> bool:
    for path in tree.files:
        parts = path.split("/")
        if len(parts) <= ELECTRON_MAX_DEPTH and (
                parts[-1] in ELECTRON_MARKER_NAMES or parts[-2:] == ["resources", "app.asar"]):
            return True
    return False


# --------------------------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    """Lower-case letters and digits only: ``"balena-etcher"`` and ``"balenaEtcher"`` are equal."""
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _name_from_stem(stem: str | None, *, allow_generic: bool = False) -> str | None:
    """A display name from a folder or archive name: ``"sublime_text_4169"`` -> ``"Sublime Text"``.
    Names that say nothing about the app ("dist", "linux", "app") give None."""
    if not stem:
        return None
    name = _clean_text(name_from_filename(stem)) or (_clean_text(stem) if allow_generic else None)
    if not name or (name.casefold() in _GENERIC_FOLDERS and not allow_generic):
        return None
    if not any(c.isupper() for c in name):
        name = " ".join(word[:1].upper() + word[1:] for word in name.split(" "))
    return name


def _program_stem(filename: str) -> str:
    """``"Game.x86_64"`` -> ``"Game"``, ``"idea.sh"`` -> ``"idea"``."""
    stem = filename
    changed = True
    while changed:
        changed = False
        for suffix in _PROGRAM_SUFFIXES:
            if stem.lower().endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)]
                changed = True
    return stem


def _name_forms(filename: str) -> set[str]:
    """Normalised spellings of a program's file name, with and without version/architecture."""
    stem = _program_stem(filename)
    forms = {_norm(stem), _norm(name_from_filename(stem))}
    forms.discard("")
    return forms


def _name_match(forms: Iterable[str], references: Iterable[str]) -> int:
    """SCORE_NAME_EXACT, SCORE_NAME_PARTIAL (one name contains the other) or 0."""
    forms, references = set(forms), set(references)
    if forms & references:
        return SCORE_NAME_EXACT
    for form in forms:
        for ref in references:
            if len(form) >= 3 and len(ref) >= 3 and (form in ref or ref in form):
                return SCORE_NAME_PARTIAL
    return 0


# --------------------------------------------------------------------------------------------
# The program to start
# --------------------------------------------------------------------------------------------

def _classify(head: bytes) -> tuple[str, int | None, str | None] | None:
    """``(kind, e_machine, arch)`` for a program (ELF executable or ``#!`` script), else None."""
    if head.startswith(ELF_MAGIC):
        if len(head) < 20 or head[4] not in (1, 2) or head[5] not in (1, 2):
            return None
        little = head[5] == 1
        e_type, machine = struct.unpack_from("<HH" if little else ">HH", head, 16)
        if e_type not in (ET_EXEC, ET_DYN):
            return None     # object files, core dumps
        return "elf", machine, arch_name(machine, 64 if head[4] == 2 else 32, little)
    if head.startswith(b"#!") and b"\0" not in head:
        return "script", None, None
    return None


def _is_helper(filename: str) -> bool:
    lower = filename.lower()
    return any(part in lower for part in _HELPER_PARTS) or _program_stem(lower) in _HELPER_STEMS


def desktop_exec_matches(entry: DesktopEntry | None, relpath: str) -> bool:
    """The embedded menu entry starts exactly this program (``Exec=``/``TryExec=``).

    ``Exec=blender %f`` matches ``blender``; ``Exec=/opt/app/bin/app`` matches ``bin/app``;
    ``Exec=sh -c "..."`` matches nothing. The installer may only keep the embedded arguments
    when this is true for the program it starts.
    """
    return _starts(_entry_programs_named(entry), relpath)


def _entry_programs_named(entry: DesktopEntry | None) -> list[str]:
    """The programs an embedded menu entry starts (``Exec=``, ``TryExec=``), as written."""
    if entry is None:
        return []
    names = []
    for value in (exec_program(entry.get("Exec") or ""), entry.get("TryExec")):
        program = (value or "").strip()
        while program.startswith("./"):
            program = program[2:]
        if program:
            names.append(program)
    return names


def _starts(programs: Iterable[str], relpath: str) -> bool:
    for program in programs:
        if "/" not in program:
            if program == relpath.rsplit("/", 1)[-1]:
                return True
        elif program == relpath or program.endswith("/" + relpath):
            return True
    return False


@dataclass
class _Program:
    relpath: str
    kind: str
    machine: int | None
    arch: str | None
    aliases: list[str] = field(default_factory=list)    # names of links that lead to this file
    appimage: bool = False                              # an AppImage (not unpacked itself)


@dataclass
class _ScoreContext:
    references: set[str]                 # normalised app / folder / archive names
    entry: DesktopEntry | None
    launcher: str | None                 # product-info.json launcherPath
    elf_stems: dict[str, set[str]]       # folder -> normalised stems of the ELF programs in it
    host: int | None                     # e_machine of this computer


def score_executable(program: _Program, context: _ScoreContext) -> int:
    """How likely ``program`` is the one that starts the app. Higher is better.

    The score is the sum of these parts (constants of this module), so every choice can be
    explained by looking at the file names alone:

    ==========================  =====  =======================================================
    ``SCORE_DESKTOP_EXEC``       +100  the archive's own menu entry starts it (``Exec=``)
    ``SCORE_PRODUCT_LAUNCHER``    +90  ``product-info.json`` names it (``launcherPath``)
    ``SCORE_NAME_EXACT``          +60  its name is the app's, the folder's or the archive's name,
                                       ignoring case, punctuation, version, architecture and
                                       suffixes such as ``.sh`` (``balena-etcher`` in
                                       ``balenaEtcher-linux-x64-2.1.4.zip``); a link's name that
                                       leads to the file counts as well
    ``SCORE_NAME_PARTIAL``        +30  ... or one of those names contains the other
                                       (``firefox-bin``, ``idea`` for "IntelliJ IDEA")
    ``SCORE_GENERIC_LAUNCHER``    +20  only without a name match: ``AppRun``, ``run.sh``,
                                       ``start.sh``, ``launcher``, ...
    ``SCORE_ROOT``                +10  it lies at the top of the app folder
    ``SCORE_BIN``                  +8  ... or in ``bin/`` (``usr/bin/``, ``usr/local/bin/``)
    ``SCORE_WRAPPER``              +5  a script next to a program of the same name
                                       (``UVtools.sh`` + ``UVtools``): the vendor's start script
                                       sets up what the program needs
    ``SCORE_ELF``                  +2  a real program rather than a script
    ``PENALTY_HELPER``            -60  a helper, not the app: ``chrome-sandbox``, crash reporters,
                                       updaters, (un)installers, ``createdump``, ...
    ``PENALTY_FOREIGN_ARCH``      -40  built for another kind of computer than this one
    ==========================  =====  =======================================================

    Libraries (``*.so``, ``*.so.1``, ``*.node``) and files that are neither ELF programs nor
    ``#!`` scripts are never candidates. Ties are broken by: closer to the top, ELF before
    script, shorter path, alphabetical order - so the result never depends on the order of the
    archive.
    """
    folder, _sep, filename = program.relpath.rpartition("/")
    score = 0
    if desktop_exec_matches(context.entry, program.relpath):
        score += SCORE_DESKTOP_EXEC
    if context.launcher and context.launcher == program.relpath:
        score += SCORE_PRODUCT_LAUNCHER
    forms = set(_name_forms(filename))
    for alias in program.aliases:
        forms |= _name_forms(alias.rsplit("/", 1)[-1])
    match = _name_match(forms, context.references)
    if not match and _norm(_program_stem(filename)) in _GENERIC_LAUNCHERS:
        match = SCORE_GENERIC_LAUNCHER
    score += match
    if not folder:
        score += SCORE_ROOT
    elif folder in _PROGRAM_DIRS:
        score += SCORE_BIN
    if program.kind == "elf":
        score += SCORE_ELF
    elif _norm(_program_stem(filename)) in context.elf_stems.get(folder, ()):
        score += SCORE_WRAPPER
    if _is_helper(filename):
        score += PENALTY_HELPER
    if program.kind == "elf" and context.host is not None and program.machine != context.host:
        score += PENALTY_FOREIGN_ARCH
    return score


def _may_be_program(path: str, member: _Member, trust_modes: bool = True) -> bool:
    lower = path.lower()
    return (member.size > 2 and not lower.endswith(_NOT_A_PROGRAM_SUFFIXES)
            and (not trust_modes or member.perm is None or bool(member.perm & 0o111)))


def _entry_programs(tree: _Tree, entry: DesktopEntry | None) -> set[str]:
    """Files at any depth that the archive's own menu entry starts (``Exec=``/``TryExec=``)."""
    named = _entry_programs_named(entry)
    return {path for path in tree.files if named and _starts(named, path)}


def _find_programs(scan: _Scan, entry: DesktopEntry | None = None
                   ) -> tuple[list[_Program], str | None]:
    """All programs near the top of the app folder (and the one the archive's menu entry
    names, wherever it lies), and the architecture of the app's libraries.

    Programs are files marked executable that start like an ELF program or a script. An
    archive in which nothing near the top is marked executable (packed on Windows, say) is
    looked at again by content alone - unpacking gives the chosen program its exec bit.
    """
    named = _entry_programs(scan.tree, entry)
    programs, library_arch = _programs_by(scan, trust_modes=True, named=named)
    if not programs:
        programs, library_arch = _programs_by(scan, trust_modes=False, named=named)
    return programs, library_arch


def _programs_by(scan: _Scan, *, trust_modes: bool,
                 named: set[str] = frozenset()) -> tuple[list[_Program], str | None]:   # type: ignore[assignment]
    tree = scan.tree
    shallow = sorted((p for p in tree.files if (_near_top(p) or p in named) and _is_utf8(p)),
                     key=lambda p: (_depth(p), p))
    programs: dict[str, _Program] = {}
    library_arch: str | None = None
    reads = 0
    for path in shallow:
        member = tree.files[path]
        if not _may_be_program(path, member, trust_modes):
            continue
        is_library = bool(_LIBRARY_RE.search(path))
        if is_library and library_arch is not None:
            continue
        reads += 1
        if reads > MAX_HEAD_READS:
            log.debug("stopped looking for programs after %d files", MAX_HEAD_READS)
            break
        found = _classify(scan.head(member, HEAD_SIZE))
        if found is None:
            continue
        if is_library:
            library_arch = found[2]
        else:
            head = scan.head(member, HEAD_SIZE)
            programs[path] = _Program(path, *found,
                                      appimage=found[0] == "elf" and head[8:11] in _APPIMAGE_MAGICS)
    # Links near the top that lead to a program: another name for it ("Postman" -> "app/Postman")
    # or, when the program itself lies deeper, the way to start it.
    for link in sorted(p for p in tree.links if _near_top(p) and _is_utf8(p)):
        target = tree.resolver.resolve_path(link)
        member = tree.files.get(target) if target else None
        if member is None or target is None:
            continue
        if target in programs:
            programs[target].aliases.append(link)
        elif not _near_top(target) and _may_be_program(target, member, trust_modes) \
                and not _LIBRARY_RE.search(target):
            head = scan.head(member, HEAD_SIZE)
            found = _classify(head)
            if found is not None:
                programs[link] = _Program(
                    link, *found, appimage=found[0] == "elf" and head[8:11] in _APPIMAGE_MAGICS)
    return list(programs.values()), library_arch


def _rank_programs(programs: Sequence[_Program], references: set[str], entry: DesktopEntry | None,
                   launcher: str | None) -> list[ExecutableCandidate]:
    elf_stems: dict[str, set[str]] = {}
    for program in programs:
        if program.kind == "elf":
            folder, _sep, filename = program.relpath.rpartition("/")
            elf_stems.setdefault(folder, set()).add(_norm(_program_stem(filename)))
    context = _ScoreContext(references, entry, launcher, elf_stems, host_machine())
    ranked = [ExecutableCandidate(p.relpath, p.kind, score_executable(p, context)) for p in programs]
    ranked.sort(key=lambda c: (-c.score, _depth(c.relpath), c.kind != "elf", len(c.relpath), c.relpath))
    return ranked[:MAX_EXECUTABLES]


# --------------------------------------------------------------------------------------------
# Icon
# --------------------------------------------------------------------------------------------

def _image_stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[: name.rfind(".")] if "." in name else name


def _icon_key(stem: str) -> str:
    """``"icon_128x128"`` -> ``"icon"``, ``"default128"`` -> ``"default"``, ``"512x512"`` -> ``""``."""
    return _norm(_ICON_SIZE_RE.sub("", stem.lower()))


def _png_size(scan: _Scan, member: _Member) -> tuple[int, int] | None:
    head = scan.head(member, _PNG_HEAD)
    if len(head) < 24 or not head.startswith(PNG_SIGNATURE) or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return (width, height) if width > 0 and height > 0 else None


class _IconFinder:
    """Orders the images of an archive from "certainly the app icon" to "maybe"."""

    def __init__(self, scan: _Scan, references: set[str]):
        self.scan = scan
        self.references = references
        self.images = sorted(
            (p for p, m in scan.tree.files.items()
             if _is_image(p) and 0 < m.size <= MAX_ICON_SIZE and _depth(p) <= MAX_ICON_DEPTH),
            key=lambda p: (_depth(p), p))
        self._probes = 0
        self._sizes: dict[str, tuple[int, int] | None] = {}

    def _size(self, path: str) -> tuple[int, int] | None:
        if path not in self._sizes:
            self._probes += 1
            self._sizes[path] = (_png_size(self.scan, self.scan.tree.files[path])
                                 if self._probes <= MAX_ICON_PROBES else None)
        return self._sizes[path]

    def _lopsided(self, path: str) -> bool:
        """A PNG that is clearly not square: a banner or a screenshot, not an icon."""
        size = self._size(path) if path.lower().endswith(".png") else None
        return size is not None and max(size) > 1.25 * min(size)

    def _quality(self, path: str) -> tuple:
        """Sort key, best first: SVG, then the largest square PNG, then XPM; shallow before deep."""
        lower = path.lower()
        symbolic = "symbolic" in lower
        if lower.endswith(".svg"):
            return (symbolic, 0, 0, 0, _depth(path), path)
        if lower.endswith(".png"):
            size = self._size(path)
            if size is None:
                return (symbolic, 1, 1, 0, _depth(path), path)
            return (symbolic, 1, int(self._lopsided(path)), -min(size), _depth(path), path)
        return (symbolic, 2, 0, 0, _depth(path), path)

    def named(self, value: str | None, base: str = "") -> list[str]:
        """Images for an ``Icon=`` value or an icon path from a metadata file."""
        value = (value or "").strip()
        if not value:
            return []
        found: list[str] = []
        if "/" in value or _is_image(value):
            for folder in dict.fromkeys(("", base)):
                rel = _safe_inner_path(f"{folder}/{value}" if folder else value)
                if rel and self.scan.tree.lookup(rel) is not None and _is_image(rel):
                    found.append(rel)
        name = value.rsplit("/", 1)[-1]
        stem = _image_stem(name) if _is_image(name) else name
        same = [p for p in self.images if _image_stem(p) == stem]
        if not same:
            same = [p for p in self.images if _image_stem(p).casefold() == stem.casefold()]
        return list(dict.fromkeys(found + sorted(same, key=self._quality)))

    def guessed(self) -> list[str]:
        """Images whose name says "icon of this app": the app's name first, then icon/logo.
        Symbolic (one-colour) icons and images that are not square are left out."""
        classes: dict[int, list[str]] = {3: [], 2: [], 1: []}
        for path in self.images:
            if "symbolic" in path.lower():
                continue
            key = _icon_key(_image_stem(path))
            folders = set(path.lower().split("/")[:-1])
            match = _name_match({key} if key else (), self.references)
            if match == SCORE_NAME_EXACT:
                classes[3].append(path)
            elif match == SCORE_NAME_PARTIAL:
                classes[2].append(path)
            elif (key in _STRONG_ICON_NAMES and _depth(path) <= 4) or \
                    (key in _WEAK_ICON_NAMES | _STRONG_ICON_NAMES and folders & _ICON_DIRS):
                classes[1].append(path)
        ordered: list[str] = []
        for rank in (3, 2, 1):
            ordered += sorted(classes[rank][:MAX_ICON_PROBES], key=self._quality)
        return [path for path in ordered if not self._lopsided(path)]


def _save_icon(scan: _Scan, candidates: Iterable[str], work_dir: Path,
               ) -> tuple[Path | None, ImageInfo | None]:
    folder = work_dir / "icon"
    for attempt, path in enumerate(dict.fromkeys(candidates)):
        if attempt >= MAX_ICON_ATTEMPTS:
            break
        data = scan.content(path, MAX_ICON_SIZE)
        if data is None:
            continue
        suffix = path[path.rfind("."):].lower()
        target = folder / f"{safe_file_stem(_image_stem(path))}{suffix}"
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            target.write_bytes(data)
            info = probe_image(target)
            if info.format != "unknown":
                log.debug("using icon %s (%s)", path, info)
                return target, info
            target.unlink()
        except OSError as exc:      # no room in the temporary folder: go on without an icon
            log.warning("could not keep the icon %s: %s", path, exc)
            return None, None
    return None, None


# --------------------------------------------------------------------------------------------
# inspect_portable
# --------------------------------------------------------------------------------------------

def _asar_images(index: Mapping, references: set[str]) -> list[tuple[tuple, str, int, int]]:
    """``(rank key, path, offset, size)`` of the images in an asar file list that are named
    like the app or like an icon ("icon.png", "assets/logo.svg"), best first."""
    found = []
    stack: list[tuple[Mapping, str, int]] = [(index, "", 0)]
    seen = 0
    while stack:
        node, prefix, depth = stack.pop()
        files = node.get("files") if isinstance(node, Mapping) else None
        if not isinstance(files, Mapping):
            continue
        for name, entry in files.items():
            seen += 1
            if seen > _MAX_ASAR_ENTRIES or not isinstance(name, str) or not isinstance(entry, Mapping):
                break
            path = f"{prefix}/{name}" if prefix else name
            if "files" in entry:
                if depth + 1 < _MAX_ASAR_ICON_DEPTH:
                    stack.append((entry, path, depth + 1))
                continue
            size, offset = entry.get("size"), entry.get("offset")
            if not _is_image(name) or "symbolic" in name.lower() or entry.get("unpacked") \
                    or not isinstance(size, int) or not 0 < size <= MAX_ICON_SIZE \
                    or not isinstance(offset, str) or not offset.isdigit():
                continue
            key = _icon_key(_image_stem(name))
            match = _name_match({key} if key else (), references)
            rank = 3 if match == SCORE_NAME_EXACT else 2 if match else \
                1 if key in _STRONG_ICON_NAMES else 0
            if rank:
                found.append(((-rank, not name.lower().endswith(".svg"), -size, depth, path),
                              path, int(offset), size))
    found.sort(key=lambda item: item[0])
    return found


def _asar_icon(scan: _Scan, references: set[str], work_dir: Path
               ) -> tuple[Path | None, ImageInfo | None]:
    """An Electron app packed as a folder or archive often keeps its icon only inside
    ``resources/app.asar``: its file list (JSON at the start) says where. A compressed tar is
    not decompressed once more for it: only what was kept of the asar's start while the
    archive passed by is looked at (``_ASAR_CAPTURE``)."""
    member = scan.tree.lookup("resources/app.asar")
    if member is None or member.size < 16:
        return None, None

    def piece(offset: int, size: int) -> bytes | None:
        kept = member.data
        if kept is not None and len(kept) >= offset + size:
            return kept[offset:offset + size]
        if not scan.reader.random_access:
            return None
        return scan.reader.read_range(member, offset, size)

    try:
        head = piece(0, 16)
        if head is None:
            return None, None
        four, header_size, _pickle, length = struct.unpack("<IIII", head)
        if four != 4 or not 0 < length <= min(header_size, _MAX_ASAR_INDEX):
            return None, None
        raw = piece(16, length)
        if raw is None:
            return None, None
        index = json.loads(raw.decode("utf-8"))
        base = 8 + header_size
        for attempt, (_rank, path, offset, size) in enumerate(_asar_images(index, references)):
            if attempt >= MAX_ICON_ATTEMPTS or base + offset + size > member.size:
                break
            data = piece(base + offset, size)
            if data is None:
                continue
            suffix = path[path.rfind("."):].lower()
            folder = work_dir / "icon"
            folder.mkdir(mode=0o700, exist_ok=True)
            target = folder / f"{safe_file_stem(_image_stem(path))}{suffix}"
            target.write_bytes(data)
            info = probe_image(target)
            square = not (info.width and info.height) or \
                max(info.width, info.height) <= 1.25 * min(info.width, info.height)
            if len(data) == size and info.format != "unknown" and square:
                log.debug("using icon %s of app.asar (%s)", path, info)
                return target, info
            target.unlink()
    except (ArchiveError, OSError, ValueError, struct.error, RecursionError) as exc:
        log.debug("no icon from app.asar: %s", exc)
    return None, None


def _noop(fraction: float | None, message: str) -> None:
    pass


def _regular_file_size(path: Path) -> int:
    try:
        st = os.stat(path)
    except FileNotFoundError as exc:
        raise ArchiveError(_("The file could not be found."), str(exc)) from exc
    except OSError as exc:
        raise ArchiveError(_("The file could not be read."), str(exc)) from exc
    if stat.S_ISDIR(st.st_mode):
        raise ArchiveError(_("This is a folder, not an app file."), os.fspath(path))
    if not stat.S_ISREG(st.st_mode):
        raise _not_an_archive(f"{path} is not a regular file")
    if st.st_size == 0:
        raise ArchiveError(
            _("This file is empty. The download may have failed — try downloading it again."),
            os.fspath(path))
    return st.st_size


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
        raise ArchiveError(_("The file could not be read."), str(exc)) from exc
    return digest.hexdigest()


def _has_no_sandbox(entry: DesktopEntry | None) -> bool:
    if entry is None:
        return False
    try:
        return "--no-sandbox" in split_exec(entry.get("Exec") or "")
    except ValueError:
        return False


def inspect_portable(path: str | os.PathLike, *, compute_hash: bool = True,
                     progress: ProgressCallback | None = None) -> PortableInfo:
    """Find out what app an archive contains, without unpacking or running it.

    Only the member list is read (a compressed tar is streamed once, with progress) plus a few
    small files: menu entries (at most 256 KiB), ``product-info.json`` and ``application.ini``
    (1 MiB), a ``version`` file, 64 bytes of each possible program and one icon (10 MiB).

    * **Top folder:** when everything lies in one folder, that folder is stripped
      (``strip_prefix``); all paths in the result are relative to the app folder.
    * **Name:** the embedded menu entry, else ``product-info.json`` (JetBrains), else
      ``application.ini`` (Mozilla), else the folder name, else the archive's file name.
    * **Version:** ``product-info.json``, ``application.ini``, the archive's file name, the
      folder name, then a ``version`` file - which is ignored for Electron apps, where it holds
      the version of Electron and not of the app.
    * **Program:** see :func:`score_executable`.
    * **Icon:** the menu entry's ``Icon=``, the ``svgIconPath`` of ``product-info.json``, then
      the best image named like the app or like an icon (SVG, else the largest square PNG).

    Raises :class:`ArchiveError` for files that are damaged, unsafe, too large, password
    protected or do not contain a program.
    """
    report = progress or _noop
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError) as exc:  # e.g. symlink loops
        raise ArchiveError(_("The file could not be found."), str(exc)) from exc
    report(0.0, _("Checking the file…"))
    size = _regular_file_size(path)
    fmt = _format_of(_read_head(path))
    if fmt is None:
        raise _not_an_archive(f"{path}: no known archive signature")

    work_dir = Path(tempfile.mkdtemp(prefix="easy-installer-"))
    owner = _WorkDirOwner(work_dir)
    try:
        os.chmod(work_dir, 0o700)
        info = _inspect(path, size, fmt, work_dir, compute_hash, report)
    except BaseException:
        owner.finalizer()
        raise
    info._own_work_dir(owner)
    return info


def _inspect(path: Path, size: int, fmt: str, work_dir: Path, compute_hash: bool,
             report: ProgressCallback) -> PortableInfo:
    report(None, _("Reading app information…"))
    stem = archive_stem(path.name)
    with _open_reader(path, fmt) as reader:
        scan = _Scan(reader, report)
        tree = scan.tree
        if not tree.files:
            raise _not_an_app("the archive contains no files")

        desktop_path, entry = _choose_desktop(scan)
        product = _parse_product_info(scan.content("product-info.json", MAX_METADATA_SIZE)) \
            or _ProductInfo()
        ini: dict[str, str] = {}
        for ini_path in ("application.ini", *sorted(
                p for p in tree.files if _depth(p) == 2 and p.endswith("/application.ini"))):
            ini = _parse_application_ini(scan.text(ini_path, MAX_METADATA_SIZE))
            if ini:
                break
        is_electron = _is_electron(tree)

        desktop_name = _clean_text(entry.get("Name")) if entry is not None else None
        folder_name = _name_from_stem(tree.prefix)
        archive_name = _name_from_stem(stem)
        specific = (desktop_name or product.name or _clean_text(ini.get("Name")) or folder_name
                    or archive_name)
        name = specific or _name_from_stem(stem, allow_generic=True) or "App"
        desktop_filename = desktop_path.rsplit("/", 1)[-1] if desktop_path else None
        references = {_norm(text) for text in (
            name, desktop_name, product.name, ini.get("Name"), ini.get("RemotingName"),
            folder_name, archive_name,
            desktop_filename[: -len(".desktop")] if desktop_filename else None)}
        references = {ref for ref in references if len(ref) >= 2}

        launcher = product.launcher if product.launcher and tree.lookup(product.launcher) else None
        programs, library_arch = _find_programs(scan, entry)
        executables = _rank_programs(programs, references, entry, launcher)
        if not executables:
            raise _not_an_app("no ELF program or script with the executable bit near the top")
        best = executables[0]
        _refuse_what_is_no_app(tree, executables, {p.relpath: p for p in programs})
        if specific is None:
            # The folder and the archive are named "linux", "app", "release"...: the program's
            # own name says more (and keeps two such apps apart).
            name = _name_from_stem(_program_stem(best.relpath.rsplit("/", 1)[-1])) or name

        report(None, _("Looking for the app icon…"))
        finder = _IconFinder(scan, references | _name_forms(best.relpath.rsplit("/", 1)[-1]))
        icon_candidates = finder.named(
            entry.get("Icon") if entry is not None else None,
            desktop_path.rpartition("/")[0] if desktop_path else "")
        if product.icon:
            icon_candidates += finder.named(product.icon)
        icon_candidates += finder.guessed()
        icon_path, icon_info = _save_icon(scan, icon_candidates, work_dir)
        if icon_path is None and is_electron:
            icon_path, icon_info = _asar_icon(
                scan, references | _name_forms(best.relpath.rsplit("/", 1)[-1]), work_dir)

        version = (
            (_clean_version(entry.get("X-AppImage-Version")) if entry is not None else None)
            or product.version or _clean_version(ini.get("Version"))
            or _version_from_stem(stem) or _version_from_stem(tree.prefix)
            or (None if is_electron else _version_file(scan)))

    by_path = {p.relpath: p for p in programs}
    chosen = by_path[best.relpath]
    elf_programs = [by_path[c.relpath] for c in executables if c.kind == "elf"]
    arch = chosen.arch or (elf_programs[0].arch if elf_programs else library_arch)
    if arch == "unknown":
        arch = None

    warnings: list[str] = []
    host = host_machine()
    arch_source = chosen if chosen.kind == "elf" else (elf_programs[0] if elf_programs else None)
    if arch_source is not None and host is not None and arch_source.machine != host:
        warnings.append(_(
            "This app is made for a different kind of computer ({arch}). It will probably not "
            "start on this one.").format(arch=arch or f"ELF machine {arch_source.machine}"))
    named = _entry_programs_named(entry)
    if named and not any(desktop_exec_matches(entry, c.relpath) for c in executables):
        warnings.append(_(
            "The menu entry in this archive starts “{program}”, which is not in it. Easy "
            "Installer is not sure which program starts this app. Please check the chosen "
            "program before you install it.").format(program=_clean_text(named[0]) or "?"))
    elif len(executables) > 1 and best.score < SCORE_NAME_EXACT \
            and best.score - executables[1].score < SCORE_ROOT:
        warnings.append(_(
            "Easy Installer is not sure which program starts this app. Please check the chosen "
            "program before you install it."))
    if entry is None and icon_path is None and not is_electron and not product.name \
            and not ini and _has_manual_pages(tree):
        warnings.append(_(
            "This looks like a tool for the terminal, not an app with a window. Its entry in "
            "the app menu may seem to do nothing."))
    if icon_path is None:
        warnings.append(_("Easy Installer could not find an icon for this app. A standard "
                          "icon will be used."))

    sha256 = _sha256(path, size, report) if compute_hash else None
    report(1.0, _("Done"))

    info = PortableInfo(
        path=path,
        size=size,
        sha256=sha256,
        app_id=derive_app_id(desktop_filename, name, stem),
        name=name,
        display_name=(_clean_text(entry.get_localized("Name")) if entry is not None else None) or name,
        version=version,
        comment=_clean_text(entry.get_localized("Comment"), limit=500) if entry is not None else None,
        categories=[c.strip() for c in entry.get_list("Categories") if c.strip()]
        if entry is not None else [],
        terminal=entry.get_bool("Terminal") if entry is not None else False,
        strip_prefix=tree.prefix,
        desktop_entry=entry,
        desktop_filename=desktop_filename,
        executables=executables,
        executable=best.relpath,
        icon_path=icon_path,
        icon_info=icon_info,
        is_electron=is_electron,
        arch=arch,
        tree_size=tree.tree_size,
        file_count=tree.file_count,
        work_dir=work_dir,
        warnings=warnings,
        exec_has_no_sandbox=_has_no_sandbox(entry),
        wm_class=product.wm_class,
        origin_url=read_origin(path),
    )
    info.__dict__["_members"] = frozenset(tree.files) | frozenset(tree.links)
    return info


def _refuse_what_is_no_app(tree: _Tree, executables: Sequence[ExecutableCandidate],
                           programs: Mapping[str, _Program]) -> None:
    """Archives that are something else than a ready-to-use app: an installer (``install.sh``,
    a ``.run`` file), source code, an AppImage that was packed into an archive, or nothing but
    helpers (``chrome-sandbox``, an updater, ...). Their "program" would be started every time
    the menu entry is clicked."""
    best = executables[0]
    filename = best.relpath.rsplit("/", 1)[-1]
    if programs[best.relpath].appimage:
        raise _appimage_inside(filename, f"{best.relpath} is an AppImage")
    if _is_installer(filename):
        raise _installer_inside(filename, f"the best program is {best.relpath!r}")
    if all(c.kind == "script" for c in executables) and _SOURCE_MARKERS & {
            path for path in tree.files if "/" not in path}:
        raise _source_code(f"source files at the top; programs: "
                           f"{[c.relpath for c in executables]}")
    if best.score < 0:
        raise _not_an_app(f"only helpers: {[(c.relpath, c.score) for c in executables]}")


_MANUAL_PAGE_RE = re.compile(r"(?:^|/)man[1-9]?/|\.[1-8](?:\.gz)?\Z")


def _has_manual_pages(tree: _Tree) -> bool:
    """Manual pages ("man node") come with tools for the terminal, rarely with apps that open
    a window."""
    return any(_MANUAL_PAGE_RE.search(path) for path in tree.files)


def _is_installer(filename: str) -> bool:
    """``install.sh``, ``setup``, ``myapp-installer``, ``DaVinci_Resolve_19_Linux.run``."""
    lower = filename.lower()
    stem = _norm(_program_stem(lower))
    return lower.endswith(".run") or stem.startswith(("install", "setup", "uninstall")) \
        or "installer" in stem or stem in ("postinstall", "preinstall")


# --------------------------------------------------------------------------------------------
# extract_portable
# --------------------------------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_mode(perm: int | None) -> int:
    """Permissions of an unpacked file: never setuid/setgid/sticky, never writable by others,
    always readable and writable by the owner (what tarfile's ``data`` filter does)."""
    if perm is None:
        return 0o644
    mode = perm & 0o755
    if not mode & 0o100:
        mode &= ~0o111
    return mode | 0o600


class _TreeWriter:
    """Creates files below one folder without ever going through a symlink.

    Every path is walked from the folder's file descriptor with ``O_NOFOLLOW``; files are
    created with ``O_EXCL``; symlinks are only made at the very end (:meth:`make_links`), after
    all of them have been checked against each other on paper.
    """

    def __init__(self, root: Path):
        self.root = root
        self.root_fd = os.open(root, _DIR_FLAGS)
        self.dirs: set[str] = {""}
        self.files: set[str] = set()
        self.links: dict[str, str] = {}
        self._cached: tuple[str, int] | None = None

    def close(self) -> None:
        if self._cached is not None:
            os.close(self._cached[1])
            self._cached = None
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def _open_dir(self, folder: str) -> int:
        """A new descriptor of ``folder`` (created as needed); the caller closes it."""
        fd = os.dup(self.root_fd)
        try:
            done = ""
            for part in folder.split("/") if folder else ():
                done = f"{done}/{part}" if done else part
                created = False
                if done not in self.dirs:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                        raise _unsafe(f"{done!r} is used as a file and as a folder") from exc
                    raise
                os.close(fd)
                fd = child
                if created:
                    os.fchmod(fd, 0o755)    # whatever the umask is
                self.dirs.add(done)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _dir_fd(self, folder: str) -> int:
        """Descriptor of ``folder``, kept open until another folder is asked for."""
        if self._cached is not None and self._cached[0] == folder:
            return self._cached[1]
        fd = self._open_dir(folder)
        if self._cached is not None:
            os.close(self._cached[1])
        self._cached = (folder, fd)
        return fd

    def make_dir(self, path: str) -> None:
        if path in self.files:
            raise _unsafe(f"{path!r} is used as a file and as a folder")
        self._dir_fd(path)

    def _create(self, path: str) -> int:
        """A new, empty file at ``path``; a file of the same name from earlier on is replaced."""
        folder, _sep, name = path.rpartition("/")
        dir_fd = self._dir_fd(folder)
        try:
            return os.open(name, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            if stat.S_ISDIR(os.lstat(name, dir_fd=dir_fd).st_mode):
                raise _unsafe(f"{path!r} is used as a file and as a folder") from None
            os.unlink(name, dir_fd=dir_fd)
            return os.open(name, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)

    def write_file(self, path: str, chunks: Iterable[bytes], perm: int | None,
                   on_bytes: Callable[[int], None]) -> None:
        mode = _file_mode(perm)
        fd = self._create(path)
        try:
            first = True
            for chunk in chunks:
                if first and perm is None and (chunk.startswith(ELF_MAGIC) or chunk.startswith(b"#!")):
                    mode |= 0o111   # the archive has no permissions (made on Windows): programs
                first = False       # and scripts must still be able to start
                on_bytes(len(chunk))
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        self.files.add(path)

    def hard_link(self, path: str, target: str, on_bytes: Callable[[int], None]) -> bool:
        """Another name for a file unpacked before; False if there is no such file."""
        if target not in self.files or target == path:
            return False
        source_folder, _sep, source_name = target.rpartition("/")
        source_fd = self._open_dir(source_folder)
        try:
            folder, _sep, name = path.rpartition("/")
            dir_fd = self._dir_fd(folder)
            try:
                st = os.lstat(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISDIR(st.st_mode):
                    raise _unsafe(f"{path!r} is used as a file and as a folder")
                os.unlink(name, dir_fd=dir_fd)
            try:
                os.link(source_name, name, src_dir_fd=source_fd, dst_dir_fd=dir_fd,
                        follow_symlinks=False)
            except OSError as exc:
                log.debug("hard link %s -> %s not possible (%s), copying", path, target, exc)
                self._copy(source_fd, source_name, path, on_bytes)
        finally:
            os.close(source_fd)
        self.files.add(path)
        return True

    def _copy(self, source_fd: int, source_name: str, path: str,
              on_bytes: Callable[[int], None]) -> None:
        src = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=source_fd)
        try:
            dst = self._create(path)
            try:
                while chunk := os.read(src, CHUNK_SIZE):
                    on_bytes(len(chunk))
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(dst, view):]
                os.fchmod(dst, stat.S_IMODE(os.fstat(src).st_mode))
            finally:
                os.close(dst)
        finally:
            os.close(src)

    def make_links(self, declared: Sequence[tuple[str, str]],
                   refused: Iterable[str] = ()) -> int:
        """Create the archive's symlinks; returns how many were left out.

        Left out are links whose name is already a file or folder, and links that do not stay
        inside the app folder: an absolute target, a way out through ``..``, a loop. Whether a
        link stays inside is decided with :class:`_LinkResolver` over all links that will exist
        - so a link that is only dangerous together with another one is caught as well. A
        dangling link that stays inside is harmless and kept (real archives have them).
        ``refused`` are links that tarfile's ``data`` filter turned down: never created, but
        still part of the picture the other links are checked against.
        """
        refused = set(refused)
        for path, _target in declared:
            self._dir_fd(path.rpartition("/")[0])   # folders first: their names are then taken
        links: dict[str, str] = {}
        skipped = 0
        for path, target in declared:
            if path in self.files or path in self.dirs or path in links:
                skipped += 1
            else:
                links[path] = target
        resolver = _LinkResolver(links)
        for path, target in links.items():
            folder, _sep, name = path.rpartition("/")
            base = folder.split("/") if folder else []
            if not _valid_link_target(target) or resolver.resolve(base, target) is None \
                    or path in refused:
                log.info("not unpacking link %s -> %s (it leads out of the app folder)",
                         path, target[:200])
                skipped += 1
                continue
            try:
                os.symlink(target, name, dir_fd=self._dir_fd(folder))
            except FileExistsError:
                skipped += 1
                continue
            self.links[path] = target
        return skipped

    def make_executable(self, path: str) -> bool:
        """Give the program its exec bits; False if ``path`` is not a file of this tree."""
        resolved = _LinkResolver(self.links).resolve_path(path)
        if not resolved or resolved not in self.files:
            return False
        folder, _sep, name = resolved.rpartition("/")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._dir_fd(folder))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return False
            mode = stat.S_IMODE(st.st_mode)
            os.fchmod(fd, mode | 0o100 | ((mode & 0o044) >> 2))
        finally:
            os.close(fd)
        return True


def _prepare_destination(dest: Path) -> bool:
    """Make sure ``dest`` is an empty real folder; True if it was created here."""
    in_use = _("The app could not be unpacked because its folder already exists.")
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        try:
            os.mkdir(dest, 0o755)
            os.chmod(dest, 0o755)
        except OSError as exc:
            raise _write_error(exc) from exc
        return True
    except OSError as exc:
        raise _write_error(exc) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise InstallError(in_use, f"{dest} is not a folder")
    try:
        with os.scandir(dest) as it:
            if next(it, None) is not None:
                raise InstallError(in_use, f"{dest} is not empty")
    except OSError as exc:
        raise _write_error(exc) from exc
    return False


def _discard(dest: Path, created: bool) -> None:
    """Remove what was unpacked (and the folder itself if it was created for this)."""
    try:
        with os.scandir(dest) as it:
            entries = [entry.path for entry in it]
    except OSError:
        entries = []
    for entry in entries:
        remove_tree(Path(entry))
    if created:
        try:
            os.rmdir(dest)
        except OSError as exc:
            log.warning("could not remove %s: %s", dest, exc)


def _check_free_space(dest: Path, needed: int) -> None:
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        return
    if free < needed + FREE_SPACE_RESERVE:
        raise InstallError(
            _("There is not enough free disk space. Please free up some space and try again."),
            f"{needed} bytes needed, {free} free in {dest}")


def _check_with_data_filter(member: _Member, path: str, target: str, dest: Path) -> bool:
    """Second opinion from tarfile's own ``data`` filter (Python 3.12, or a patched older one).

    False: the filter turns this link down (it leads outside) - leave it out. Any other
    objection of the filter makes the whole archive unsafe. The checks of this module do not
    rely on the filter; whatever it lets through still has to pass them.
    """
    data_filter = getattr(tarfile, "data_filter", None)
    info = member.handle
    if data_filter is None or not isinstance(info, tarfile.TarInfo):
        return True
    try:
        data_filter(info.replace(name=path, linkname=target, deep=False), os.fspath(dest))
    except (getattr(tarfile, "AbsoluteLinkError", tarfile.TarError),
            getattr(tarfile, "LinkOutsideDestinationError", tarfile.TarError)) as exc:
        if member.kind in ("symlink", "hardlink"):
            log.debug("data filter: %s", exc)
            return False
        raise _unsafe(f"{type(exc).__name__}: {exc}") from exc
    except tarfile.TarError as exc:
        raise _unsafe(f"{type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        # The filter looks at the folder on disk and gives up on e.g. a file where it expects
        # a folder; unpacking that member will run into the same thing and report it properly.
        log.debug("data filter could not check %s: %s", path, exc)
    return True


class _Progress:
    """Counts unpacked bytes, enforces the size limit and reports every now and then."""

    def __init__(self, expected: int, report: ProgressCallback):
        self.expected = expected
        self.report = report
        self.done = 0
        self._reported = 0
        self._step = max(expected // 200, 256 * 1024)
        self.message = _("Unpacking the app…")

    def add(self, count: int) -> None:
        self.done += count
        if self.done > MAX_TOTAL_SIZE:
            raise _too_large(f"more than {MAX_TOTAL_SIZE} bytes unpacked")
        if self.done - self._reported >= self._step:
            self._reported = self.done
            self.report(min(self.done / self.expected, 1.0) if self.expected else None, self.message)


def extract_portable(info: PortableInfo, dest_dir: Path, *,
                     progress: ProgressCallback | None = None) -> None:
    """Unpack the app of ``info`` into ``dest_dir`` (without its single top folder).

    ``dest_dir`` must not exist yet (it is created; its parent must exist) or be an empty
    folder. On any failure everything unpacked so far is removed again - and the folder too if
    it was created here - before the error is raised.

    The archive is hostile input. What is guaranteed, whatever it contains:

    * nothing is ever written outside ``dest_dir``: member names that are absolute, contain
      ``..`` or control characters are refused (:class:`ArchiveError`), folders are entered
      without following links, files are created exclusively and never through a link;
    * devices, pipes and other special files are refused; password-protected zips too;
    * symlinks are created last and only if they stay inside the app folder - also when
      followed through each other; links that lead outside (real archives do contain absolute
      ones) are left out, as are hard links to anything but a file unpacked before;
    * setuid, setgid and sticky bits are dropped, nothing is writable by group or others,
      owners and timestamps of the archive are ignored; zip permissions and symlinks come
      from ``external_attr``; tar members also pass tarfile's ``data`` filter;
    * at most ``MAX_ENTRIES`` members and ``MAX_TOTAL_SIZE`` bytes are unpacked, counted from
      what is really written, not from what the archive declares;
    * a ``.easy-installer.json`` or ``.easy-installer-files.json`` of the archive is not
      unpacked (those names are the installer's).

    Afterwards ``info.executable`` exists in the folder and may be started (exec bit).
    Raises :class:`ArchiveError` (damaged, unsafe, too large, program missing) or
    :class:`InstallError` (disk full, no permission, folder in use).
    """
    report = progress or _noop
    dest = Path(dest_dir)
    created = _prepare_destination(dest)
    try:
        _extract(info, dest, report)
    except BaseException:
        _discard(dest, created)
        raise
    report(1.0, _("Done"))


def _extract(info: PortableInfo, dest: Path, report: ProgressCallback) -> None:
    try:
        executable = _safe_relpath(info.executable)
    except ArchiveError:
        executable = ""
    missing_program = ArchiveError(
        _("The program that starts this app was not found in the archive."),
        f"executable {info.executable!r}")
    if not executable:
        raise missing_program
    # What is unpacked must be what was inspected (and what the registry will record): the
    # archive is opened once, its checksum compared, and then read from that same open file -
    # another file put in its place meanwhile changes nothing.
    try:
        archive = open(info.path, "rb")
    except OSError as exc:
        raise ArchiveError(_("The file could not be found."), str(exc)) from exc
    with archive:
        _check_unchanged(info, archive, report)
        _check_free_space(dest, info.tree_size)
        _unpack(info, archive, dest, executable, missing_program, report)


def _check_unchanged(info: PortableInfo, archive: BinaryIO, report: ProgressCallback) -> None:
    try:
        size = os.fstat(archive.fileno()).st_size
    except OSError as exc:
        raise ArchiveError(_("The file could not be read."), str(exc)) from exc
    if size != info.size:
        raise _changed(f"{info.path} no longer has {info.size} bytes")
    if not info.sha256:
        return
    digest = hashlib.sha256()
    report(None, _("Checking the file…"))
    try:
        while chunk := archive.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    except OSError as exc:
        raise ArchiveError(_("The file could not be read."), str(exc)) from exc
    if digest.hexdigest() != info.sha256:
        raise _changed(f"{info.path}: sha256 {digest.hexdigest()}, inspected {info.sha256}")


def _unpack(info: PortableInfo, archive: BinaryIO, dest: Path, executable: str,
            missing_program: ArchiveError, report: ProgressCallback) -> None:
    counter = _Progress(info.tree_size, report)
    report(0.0, counter.message)
    try:
        writer = _TreeWriter(dest)
    except OSError as exc:
        raise _write_error(exc) from exc
    try:
        with _open_reader(archive) as reader:
            links, refused = _unpack_members(reader, writer, info.strip_prefix, dest, counter)
            try:
                writer.make_links(links, refused)
                found = writer.make_executable(executable)
            except OSError as exc:
                raise _write_error(exc) from exc
        if not found:
            raise missing_program
    finally:
        writer.close()


def _unpack_members(reader: _Reader, writer: _TreeWriter, prefix: str | None, dest: Path,
                    counter: _Progress) -> tuple[list[tuple[str, str]], set[str]]:
    """Unpack folders, files and hard links. Returns the symlinks ``(path, target)`` for
    later, and the paths of those that tarfile's ``data`` filter refuses."""
    links: list[tuple[str, str]] = []
    refused: set[str] = set()
    count = 0
    for member in reader.members():
        count += 1
        if count > MAX_ENTRIES:
            raise _too_large(f"more than {MAX_ENTRIES} entries")
        if not member.path or member.kind == "other" or _is_ignored(member.path):
            continue
        path = _strip_prefix(member.path, prefix)
        if path is None:
            raise _changed(f"{member.path!r} is not inside {prefix!r}")
        if not path or path in (MARKER_NAME, MANIFEST_NAME):
            continue
        target = member.target
        if member.kind == "hardlink":
            target = (_strip_prefix(target, prefix) or "") if target else ""
        if not _check_with_data_filter(member, path, target, dest):
            if member.kind != "symlink":
                log.info("not unpacking hard link %s -> %s (it leads out of the app folder)",
                         path, target)
                continue
            refused.add(path)
        try:
            if member.kind == "dir":
                writer.make_dir(path)
            elif member.kind == "file":
                if counter.done + member.size > MAX_TOTAL_SIZE:
                    raise _too_large(f"{member.path!r} declares {member.size} bytes")
                writer.write_file(path, reader.chunks(member), member.perm, counter.add)
            elif member.kind == "symlink":
                links.append((path, target))
            elif member.kind == "hardlink":
                if not writer.hard_link(path, target, counter.add):
                    log.info("not unpacking hard link %s -> %s (no such file in the app folder)",
                             path, target or member.handle)
        except OSError as exc:
            raise _write_error(exc) from exc
    return links, refused
