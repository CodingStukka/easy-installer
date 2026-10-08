from __future__ import annotations

import os
import random
import struct
import sys

import pytest

from easy_installer.core import elf
from easy_installer.core.elf import (
    ElfInfo,
    host_arch,
    normalize_arch,
    payload_magic,
    read_elf_info,
    read_section,
    read_update_info,
)
from easy_installer.errors import NotAnAppImageError

from fakeappimage import (
    EM_386,
    EM_AARCH64,
    EM_ARM,
    EM_X86_64,
    build_runtime,
    make_fake_appimage,
    requires_mksquashfs,
)

PAYLOAD = b"hsqs" + b"\0" * 60


def write(tmp_path, data: bytes, name: str = "app.AppImage"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------------------------

def test_payload_offset_matches_end_of_section_headers(tmp_path):
    runtime = build_runtime(upd_info="")
    path = write(tmp_path, runtime + PAYLOAD)
    info = read_elf_info(path)
    assert isinstance(info, ElfInfo)
    assert info.bits == 64 and info.little_endian
    assert info.machine == EM_X86_64 and info.arch == "x86_64"
    assert info.payload_offset == len(runtime)
    assert info.appimage_type == 2
    assert info.has_interp is False
    assert ".shstrtab" in info.sections and ".upd_info" in info.sections
    assert payload_magic(path, info) == b"hsqs"


@requires_mksquashfs
def test_fake_appimage_payload_is_squashfs(tmp_path):
    path = make_fake_appimage(tmp_path / "x.AppImage", {"a.desktop": "[Desktop Entry]\n"},
                              upd_info="zsync|https://example.org/x.zsync", dynamic_interp=True)
    info = read_elf_info(path)
    assert payload_magic(path, info) == b"hsqs"
    assert info.has_interp is True
    assert ".interp" in info.sections
    assert read_update_info(path, info) == "zsync|https://example.org/x.zsync"


def test_update_info_empty_or_missing_is_none(tmp_path):
    empty = write(tmp_path, build_runtime(upd_info="") + PAYLOAD, "empty.AppImage")
    missing = write(tmp_path, build_runtime(upd_info=None) + PAYLOAD, "missing.AppImage")
    whitespace = write(tmp_path, build_runtime(upd_info="  \n") + PAYLOAD, "ws.AppImage")
    for path in (empty, missing, whitespace):
        assert read_update_info(path, read_elf_info(path)) is None
    assert ".upd_info" not in read_elf_info(missing).sections


def test_read_section(tmp_path):
    runtime = build_runtime(upd_info="hello", extra_sections={".sig_key": b"KEY" * 10})
    path = write(tmp_path, runtime + PAYLOAD)
    info = read_elf_info(path)
    assert read_section(path, info, ".sig_key") == b"KEY" * 10
    assert read_section(path, info, ".sig_key", max_size=4) == b"KEYK"
    assert read_section(path, info, ".upd_info")[:6] == b"hello\0"
    assert len(read_section(path, info, ".upd_info")) == 1024
    assert read_section(path, info, ".nope") is None


@pytest.mark.parametrize("machine, arch", [
    (EM_X86_64, "x86_64"), (EM_AARCH64, "aarch64"), (EM_386, "i686"), (EM_ARM, "armhf"),
    (243, "riscv64"), (21, "ppc64le"), (22, "s390x"), (258, "loongarch64"), (4242, "unknown"),
])
def test_arch_mapping(tmp_path, machine, arch):
    path = write(tmp_path, build_runtime(machine=machine) + PAYLOAD)
    info = read_elf_info(path)
    assert info.machine == machine
    assert info.arch == arch


def test_arch_names_depending_on_word_size_and_byte_order():
    assert elf.arch_name(21, 64, little_endian=False) == "ppc64"
    assert elf.arch_name(243, 32) == "riscv32"
    assert elf.arch_name(8, 32, little_endian=True) == "mipsel"
    assert elf.arch_name(8, 64, little_endian=False) == "mips64"


def test_is_foreign_arch_uses_the_host_machine(tmp_path, monkeypatch):
    assert elf.host_machine() == read_elf_info(sys.executable).machine
    riscv = read_elf_info(write(tmp_path, build_runtime(machine=243) + PAYLOAD))
    unknown = read_elf_info(write(tmp_path, build_runtime(machine=4242) + PAYLOAD, "u.AppImage"))
    native = read_elf_info(write(tmp_path, build_runtime(machine=EM_X86_64) + PAYLOAD, "n.AppImage"))
    monkeypatch.setattr(elf, "host_machine", lambda: EM_X86_64)
    assert elf.is_foreign_arch(riscv) and elf.is_foreign_arch(unknown)
    assert not elf.is_foreign_arch(native)
    monkeypatch.setattr(elf, "host_machine", lambda: None)  # fall back to comparing names
    monkeypatch.setattr(elf, "host_arch", lambda: "x86_64")
    assert elf.is_foreign_arch(riscv) and not elf.is_foreign_arch(unknown)


def test_32bit_little_endian(tmp_path):
    runtime = build_runtime(bits=32, machine=EM_386, upd_info="x", dynamic_interp=True)
    path = write(tmp_path, runtime + PAYLOAD)
    info = read_elf_info(path)
    assert info.bits == 32 and info.little_endian
    assert info.arch == "i686"
    assert info.payload_offset == len(runtime)
    assert info.has_interp
    assert read_update_info(path, info) == "x"


def test_big_endian(tmp_path):
    runtime = build_runtime(little_endian=False, machine=EM_AARCH64, upd_info="be")
    path = write(tmp_path, runtime + PAYLOAD)
    info = read_elf_info(path)
    assert not info.little_endian
    assert info.arch == "aarch64"
    assert info.payload_offset == len(runtime)
    assert read_update_info(path, info) == "be"


@pytest.mark.parametrize("appimage_type, expected", [(1, 1), (2, 2), (None, None), (3, None)])
def test_appimage_type(tmp_path, appimage_type, expected):
    path = write(tmp_path, build_runtime(appimage_type=appimage_type) + PAYLOAD)
    assert read_elf_info(path).appimage_type == expected


def test_payload_magic_at_end_of_file(tmp_path):
    runtime = build_runtime()
    path = write(tmp_path, runtime + b"hs")
    info = read_elf_info(path)
    assert payload_magic(path, info) == b"hs"
    path.write_bytes(runtime)
    assert payload_magic(path, read_elf_info(path)) == b""


def test_elfinfo_is_hashable_and_frozen(tmp_path):
    info = read_elf_info(write(tmp_path, build_runtime() + PAYLOAD))
    hash(info)
    with pytest.raises(Exception):
        info.bits = 32  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# hostile / broken input: always NotAnAppImageError with a friendly message
# ---------------------------------------------------------------------------------------------

def assert_friendly(exc_info):
    message = str(exc_info.value)
    assert message and message[0].isupper() and message.endswith(".")
    assert "struct" not in message and "Traceback" not in message


def test_missing_file(tmp_path):
    with pytest.raises(NotAnAppImageError) as exc:
        read_elf_info(tmp_path / "nope.AppImage")
    assert_friendly(exc)


def test_directory(tmp_path):
    with pytest.raises(NotAnAppImageError) as exc:
        read_elf_info(tmp_path)
    assert "folder" in str(exc.value)


def test_empty_file(tmp_path):
    with pytest.raises(NotAnAppImageError) as exc:
        read_elf_info(write(tmp_path, b""))
    assert "empty" in str(exc.value)


def test_fifo_does_not_block(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(NotAnAppImageError):
        read_elf_info(fifo)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file(tmp_path):
    path = write(tmp_path, build_runtime() + PAYLOAD)
    path.chmod(0)
    try:
        with pytest.raises(NotAnAppImageError) as exc:
            read_elf_info(path)
        assert "permission" in str(exc.value)
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize("data", [
    b"#!/bin/sh\necho hello\n",
    b"PK\x03\x04 zip file",
    b"\x7fELG" + b"\0" * 100,
    b"\x89PNG\r\n\x1a\n" + b"\0" * 100,
])
def test_not_elf(tmp_path, data):
    with pytest.raises(NotAnAppImageError) as exc:
        read_elf_info(write(tmp_path, data))
    assert str(exc.value) == "This file is not an AppImage."


def test_every_truncation_raises_cleanly(tmp_path):
    runtime = build_runtime(upd_info="x", dynamic_interp=True)
    path = tmp_path / "t.AppImage"
    for length in range(1, len(runtime)):
        path.write_bytes(runtime[:length])
        with pytest.raises(NotAnAppImageError):
            read_elf_info(path)
    path.write_bytes(runtime)  # complete runtime without payload is still parseable
    assert read_elf_info(path).payload_offset == len(runtime)


def patch_header(data: bytes, fmt: str, offset: int, *values) -> bytes:
    out = bytearray(data)
    struct.pack_into(fmt, out, offset, *values)
    return bytes(out)


# ELF64 header field offsets
E_PHOFF, E_SHOFF, E_PHENTSIZE, E_PHNUM, E_SHENTSIZE, E_SHNUM, E_SHSTRNDX = 32, 40, 54, 56, 58, 60, 62


@pytest.mark.parametrize("fmt, offset, value", [
    ("<H", E_SHNUM, 0xFFFF),              # huge e_shnum
    ("<H", E_SHNUM, elf.MAX_SECTIONS + 1),
    ("<H", E_SHNUM, 0),                   # no sections -> no payload offset
    ("<Q", E_SHOFF, 0),
    ("<Q", E_SHOFF, 1 << 62),             # far beyond EOF
    ("<Q", E_SHOFF, (1 << 64) - 1),
    ("<H", E_SHENTSIZE, 0),
    ("<H", E_SHENTSIZE, 8),
    ("<H", E_PHNUM, 0xFFFF),
    ("<Q", E_PHOFF, 1 << 40),
    ("<H", E_PHENTSIZE, 3),
])
def test_hostile_headers(tmp_path, fmt, offset, value):
    data = patch_header(build_runtime(dynamic_interp=True) + PAYLOAD, fmt, offset, value)
    with pytest.raises(NotAnAppImageError) as exc:
        read_elf_info(write(tmp_path, data))
    assert_friendly(exc)


@pytest.mark.parametrize("ident_index, value", [(4, 0), (4, 3), (5, 0), (5, 7)])
def test_bad_class_or_data(tmp_path, ident_index, value):
    data = bytearray(build_runtime() + PAYLOAD)
    data[ident_index] = value
    with pytest.raises(NotAnAppImageError):
        read_elf_info(write(tmp_path, bytes(data)))


def test_bad_shstrndx_gives_no_section_names(tmp_path):
    for value in (0, 200):
        data = patch_header(build_runtime(upd_info="x") + PAYLOAD, "<H", E_SHSTRNDX, value)
        info = read_elf_info(write(tmp_path, data))
        assert info.sections == {}
        assert read_update_info(tmp_path / "app.AppImage", info) is None


def test_section_name_offsets_out_of_range_are_ignored(tmp_path):
    runtime = bytearray(build_runtime(upd_info="x"))
    shoff = struct.unpack_from("<Q", runtime, E_SHOFF)[0]
    struct.pack_into("<I", runtime, shoff + 64 * 1, 0xFFFFFF)  # sh_name of section 1
    info = read_elf_info(write(tmp_path, bytes(runtime) + PAYLOAD))
    assert ".upd_info" not in info.sections
    assert ".shstrtab" in info.sections


def test_random_corruption_never_leaks_other_exceptions(tmp_path):
    rng = random.Random(1234)
    original = build_runtime(upd_info="x", dynamic_interp=True) + PAYLOAD
    path = tmp_path / "fuzz.AppImage"
    for _ in range(400):
        data = bytearray(original)
        for _ in range(rng.randint(1, 8)):
            index = rng.randrange(0, 64) if rng.random() < 0.7 else rng.randrange(len(data))
            data[index] = rng.randrange(256)
        cut = rng.choice([len(data), rng.randrange(1, len(data))])
        path.write_bytes(bytes(data[:cut]))
        try:
            info = read_elf_info(path)
        except NotAnAppImageError:
            continue
        # whatever we got must be internally consistent
        assert 0 < info.payload_offset <= cut
        read_update_info(path, info)
        payload_magic(path, info)


# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("machine, arch", [
    ("x86_64", "x86_64"), ("AMD64", "x86_64"), ("aarch64", "aarch64"), ("arm64", "aarch64"),
    ("i686", "i686"), ("i386", "i686"), ("armv7l", "armhf"), ("riscv64", "riscv64"), ("", "unknown"),
])
def test_normalize_arch(machine, arch):
    assert normalize_arch(machine) == arch


def test_host_arch(monkeypatch):
    monkeypatch.setattr(elf.platform, "machine", lambda: "amd64")
    assert host_arch() == "x86_64"
