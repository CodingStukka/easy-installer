"""Minimal, defensive ELF reader for AppImage runtimes.

Only the parts needed to locate the AppImage payload are parsed: the ELF header, the program
header table (for ``PT_INTERP``) and the section header table (for ``.upd_info`` & co.).
Every read is bounds-checked; malformed input raises :class:`NotAnAppImageError`.
"""

from __future__ import annotations

import errno
import functools
import logging
import os
import platform
import stat
import struct
import sys
from dataclasses import dataclass, field

from ..errors import NotAnAppImageError
from ..i18n import _

log = logging.getLogger(__name__)

ELF_MAGIC = b"\x7fELF"
PT_INTERP = 3
SHT_NOBITS = 8

#: e_machine -> architecture name used throughout Easy Installer (like ``uname -m``); see
#: :func:`arch_name` for the names that also depend on word size or byte order.
MACHINE_ARCH = {
    62: "x86_64", 183: "aarch64", 3: "i686", 40: "armhf", 243: "riscv64", 21: "ppc64le",
    20: "ppc", 22: "s390x", 258: "loongarch64", 8: "mips", 2: "sparc", 43: "sparc64", 50: "ia64",
}


def arch_name(machine: int, bits: int = 64, little_endian: bool = True) -> str:
    """Architecture name of an ELF ``e_machine`` ("unknown" if Easy Installer does not know it)."""
    if machine == 21:
        return "ppc64le" if little_endian else "ppc64"
    if machine == 243:
        return "riscv64" if bits == 64 else "riscv32"
    if machine == 22:
        return "s390x" if bits == 64 else "s390"
    if machine == 8:
        return ("mips64" if bits == 64 else "mips") + ("el" if little_endian else "")
    return MACHINE_ARCH.get(machine, "unknown")

MAX_SECTIONS = 4096          # real runtimes have ~30; e_shnum is 16 bit, but refuse silly values
MAX_PROGRAM_HEADERS = 4096
MAX_SHSTRTAB = 1 << 20


@dataclass(frozen=True)
class ElfInfo:
    bits: int
    little_endian: bool
    machine: int
    arch: str
    payload_offset: int
    appimage_type: int | None
    has_interp: bool
    sections: dict[str, tuple[int, int]] = field(default_factory=dict, hash=False)


class _Truncated(Exception):
    """Internal: a read went past the end of the file or a header is inconsistent."""


def _not_appimage(details: str | None = None) -> NotAnAppImageError:
    return NotAnAppImageError(_("This file is not an AppImage."), details)


def _damaged(details: str | None = None) -> NotAnAppImageError:
    return NotAnAppImageError(
        _("The file seems damaged or incomplete. Try downloading it again."), details
    )


def _open_regular(path: str | os.PathLike) -> tuple[int, int]:
    """Open ``path`` read-only and return ``(fd, size)``; raise friendly errors otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise NotAnAppImageError(_("The file could not be found."), str(exc)) from exc
    except IsADirectoryError as exc:
        raise NotAnAppImageError(_("This is a folder, not an app file."), str(exc)) from exc
    except PermissionError as exc:
        raise NotAnAppImageError(
            _("The file could not be opened. You may not have permission to read it."), str(exc)
        ) from exc
    except OSError as exc:
        raise NotAnAppImageError(_("The file could not be opened."), str(exc)) from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise NotAnAppImageError(_("The file could not be opened."), str(exc)) from exc
    if stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise NotAnAppImageError(_("This is a folder, not an app file."), os.fspath(path))
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise _not_appimage(f"{os.fspath(path)} is not a regular file")
    if st.st_size == 0:
        os.close(fd)
        raise NotAnAppImageError(
            _("This file is empty. The download may have failed — try downloading it again."),
            os.fspath(path),
        )
    return fd, st.st_size


def _pread_exact(fd: int, size: int, offset: int, file_size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > file_size:
        raise _Truncated(f"read of {size} bytes at {offset} is outside the file ({file_size} bytes)")
    data = os.pread(fd, size, offset)
    if len(data) != size:
        raise _Truncated(f"short read at {offset}: wanted {size}, got {len(data)}")
    return data


def _parse(fd: int, file_size: int) -> ElfInfo:
    ident = _pread_exact(fd, min(16, file_size), 0, file_size)
    if not ident.startswith(ELF_MAGIC) and not ELF_MAGIC.startswith(ident):
        raise _not_appimage("missing ELF magic")
    if len(ident) < 16:
        raise _Truncated(f"only {file_size} bytes")
    ei_class, ei_data = ident[4], ident[5]
    if ei_class not in (1, 2) or ei_data not in (1, 2):
        raise _not_appimage(f"unsupported ELF class/data {ei_class}/{ei_data}")
    bits = 64 if ei_class == 2 else 32
    little = ei_data == 1
    e = "<" if little else ">"

    ehdr_size = 64 if bits == 64 else 52
    ehdr = _pread_exact(fd, ehdr_size, 0, file_size)
    if bits == 64:
        (_type, machine, _version, _entry, phoff, shoff, _flags, _ehsize,
         phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from(e + "HHIQQQIHHHHHH", ehdr, 16)
        min_ph, min_sh = 56, 64
    else:
        (_type, machine, _version, _entry, phoff, shoff, _flags, _ehsize,
         phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from(e + "HHIIIIIHHHHHH", ehdr, 16)
        min_ph, min_sh = 32, 40

    magic = ident[8:11]
    appimage_type = magic[2] if magic[:2] == b"AI" and magic[2] in (1, 2) else None

    # Section header table: its end is where the AppImage payload starts.
    if shoff == 0 or shnum == 0:
        raise _not_appimage("ELF file has no section header table")
    if shnum > MAX_SECTIONS:
        raise _not_appimage(f"implausible number of sections: {shnum}")
    if shentsize < min_sh:
        raise _not_appimage(f"implausible section header size: {shentsize}")
    payload_offset = shoff + shentsize * shnum
    if payload_offset > file_size:
        raise _Truncated(f"section headers end at {payload_offset}, file has {file_size} bytes")
    table = _pread_exact(fd, shentsize * shnum, shoff, file_size)
    raw_sections = []
    for i in range(shnum):
        at = i * shentsize
        if bits == 64:
            sh_name, sh_type, _fl, _addr, sh_offset, sh_size = struct.unpack_from(e + "IIQQQQ", table, at)
        else:
            sh_name, sh_type, _fl, _addr, sh_offset, sh_size = struct.unpack_from(e + "IIIIII", table, at)
        raw_sections.append((sh_name, sh_type, sh_offset, sh_size))

    sections: dict[str, tuple[int, int]] = {}
    if 0 < shstrndx < shnum:
        _n, str_type, str_off, str_size = raw_sections[shstrndx]
        if str_type != SHT_NOBITS and 0 < str_size <= MAX_SHSTRTAB and str_off + str_size <= file_size:
            strtab = _pread_exact(fd, str_size, str_off, file_size)
            for sh_name, sh_type, sh_offset, sh_size in raw_sections[1:]:
                if sh_name >= len(strtab):
                    continue
                end = strtab.find(b"\0", sh_name)
                name = strtab[sh_name:end if end >= 0 else len(strtab)].decode("latin-1")
                if not name or name in sections:
                    continue
                sections[name] = (sh_offset, 0 if sh_type == SHT_NOBITS else sh_size)
        else:
            log.debug("ignoring implausible .shstrtab (offset %s, size %s)", str_off, str_size)

    # Program headers: only needed for PT_INTERP.
    has_interp = False
    if phnum:
        if phnum > MAX_PROGRAM_HEADERS or phentsize < min_ph:
            raise _not_appimage(f"implausible program header table ({phnum} x {phentsize})")
        ptable = _pread_exact(fd, phentsize * phnum, phoff, file_size)
        for i in range(phnum):
            (p_type,) = struct.unpack_from(e + "I", ptable, i * phentsize)
            if p_type == PT_INTERP:
                has_interp = True
                break

    return ElfInfo(
        bits=bits,
        little_endian=little,
        machine=machine,
        arch=arch_name(machine, bits, little),
        payload_offset=payload_offset,
        appimage_type=appimage_type,
        has_interp=has_interp,
        sections=sections,
    )


def read_elf_info(path: str | os.PathLike) -> ElfInfo:
    """Parse the ELF headers of ``path``. Raises :class:`NotAnAppImageError` for anything odd."""
    fd, size = _open_regular(path)
    try:
        return _parse(fd, size)
    except NotAnAppImageError:
        raise
    except _Truncated as exc:
        raise _damaged(str(exc)) from exc
    except (struct.error, IndexError, ValueError, OverflowError) as exc:  # defensive: never leak these
        raise _damaged(f"malformed ELF headers: {exc}") from exc
    except OSError as exc:
        if exc.errno == errno.EIO:
            raise _damaged(str(exc)) from exc
        raise NotAnAppImageError(_("The file could not be read."), str(exc)) from exc
    finally:
        os.close(fd)


def _read_at(path: str | os.PathLike, offset: int, size: int) -> bytes:
    """Read up to ``size`` bytes at ``offset``; returns fewer bytes at EOF (b"" on errors)."""
    if offset < 0 or size <= 0:
        return b""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.read(size)
    except (OSError, OverflowError, ValueError) as exc:
        log.debug("reading %s bytes at %s from %s failed: %s", size, offset, path, exc)
        return b""


def read_section(path: str | os.PathLike, info: ElfInfo, name: str, max_size: int = 1 << 20) -> bytes | None:
    """Raw bytes of section ``name`` (at most ``max_size``), or None if the section is absent."""
    entry = info.sections.get(name)
    if entry is None:
        return None
    offset, size = entry
    return _read_at(path, offset, min(size, max_size))


def read_update_info(path: str | os.PathLike, info: ElfInfo) -> str | None:
    """The embedded update information (``.upd_info``), e.g. ``gh-releases-zsync|...``."""
    data = read_section(path, info, ".upd_info", max_size=64 * 1024)
    if not data:
        return None
    text = data.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    return text or None


def payload_magic(path: str | os.PathLike, info: ElfInfo) -> bytes:
    """The first 4 bytes of the payload (``b"hsqs"`` for squashfs); shorter at EOF."""
    return _read_at(path, info.payload_offset, 4)


_ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64", "armv8l": "aarch64",
    "i386": "i686", "i486": "i686", "i586": "i686", "i686": "i686", "x86": "i686",
    "armv7l": "armhf", "armv7": "armhf", "armv6l": "armhf", "armhf": "armhf", "arm": "armhf",
}


def normalize_arch(machine: str) -> str:
    """Map ``platform.machine()``-style names onto the names used in :class:`ElfInfo`."""
    return _ARCH_ALIASES.get(machine.strip().lower(), machine.strip().lower() or "unknown")


def host_arch() -> str:
    return normalize_arch(platform.machine())


@functools.lru_cache(maxsize=1)
def host_machine() -> int | None:
    """``e_machine`` of the running Python (what this computer's programs are built for)."""
    for path in ("/proc/self/exe", sys.executable):
        try:
            with open(path, "rb") as fh:
                head = fh.read(20)
        except (OSError, TypeError, ValueError):
            continue
        if len(head) == 20 and head.startswith(ELF_MAGIC) and head[5] in (1, 2):
            return struct.unpack_from("<H" if head[5] == 1 else ">H", head, 18)[0]
    return None


def is_foreign_arch(info: ElfInfo) -> bool:
    """The AppImage is built for another kind of computer than this one.

    Compares ``e_machine`` with the running Python's, so also architectures Easy Installer has no
    name for are recognised; falls back to comparing names (unknown ones are not flagged then).
    """
    host = host_machine()
    if host is not None:
        return info.machine != host
    return info.arch not in ("", "unknown") and info.arch != host_arch()
