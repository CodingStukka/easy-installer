from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from easy_installer.core import squashfs
from easy_installer.core.elf import read_elf_info
from easy_installer.core.squashfs import (
    ExtractCommandReader,
    UnsquashfsReader,
    check_squashfs_complete,
    escape_pattern,
    open_payload,
    remove_tree,
    sanitized_runtime_env,
)
from easy_installer.errors import ExtractionError, NotAnAppImageError

from fakeappimage import (
    make_fake_appimage,
    make_png,
    make_runtime_emulator,
    make_sample_appimage,
    requires_mksquashfs,
    requires_unsquashfs,
)

pytestmark = [requires_mksquashfs, requires_unsquashfs]

FILES = {
    "app.desktop": "[Desktop Entry]\nName=App\n",
    "other.desktop": "[Desktop Entry]\nName=Other\n",
    "usr/share/applications/nested.desktop": "[Desktop Entry]\nName=Nested\n",
    "usr/share/icons/hicolor/64x64/apps/app.png": make_png(64, 64),
    "a*b.png": b"star",
    "axb.png": b"x",
    "c[1].png": b"bracket",
    "c1.png": b"one",
    "big/data.bin": b"\0" * 1000,
}
LINKS = {
    ".DirIcon": "usr/share/icons/hicolor/64x64/apps/app.png",
    "escape": "/etc/passwd",
    "usr/share/icons/link-dir": "hicolor",
}


@pytest.fixture
def fake(tmp_path):
    return make_fake_appimage(tmp_path / "Fake.AppImage", FILES, LINKS)


@pytest.fixture
def reader(fake):
    return UnsquashfsReader(fake, read_elf_info(fake).payload_offset)


# ---------------------------------------------------------------------------------------------
# UnsquashfsReader
# ---------------------------------------------------------------------------------------------

def test_list_members(reader):
    members = reader.list_members()
    assert "app.desktop" in members
    assert ".DirIcon" in members
    assert "usr/share/icons/hicolor/64x64/apps/app.png" in members
    assert "usr/share/icons" in members           # directories are listed too
    assert "usr/share/icons/link-dir" in members  # symlinks are listed
    assert "a*b.png" in members
    assert not any(m.startswith("squashfs-root") for m in members)
    assert "" not in members
    assert reader.list_members() == members       # cached, returns a copy
    members.clear()
    assert reader.list_members()


def test_member_sizes_come_from_the_long_listing(tmp_path, monkeypatch):
    fake = make_fake_appimage(tmp_path / "S.AppImage",
                              {"a.bin": b"x" * 1234, "dir/b.txt": "hello", "we ird -> x.txt": "12"},
                              {"link -> odd": "a.bin", ".DirIcon": "dir/b.txt"})
    reader = UnsquashfsReader(fake, read_elf_info(fake).payload_offset)
    members = reader.list_members()
    assert set(members) == {"a.bin", "dir", "dir/b.txt", "we ird -> x.txt", "link -> odd",
                            ".DirIcon"}
    assert reader.member_sizes() == {"a.bin": 1234, "dir/b.txt": 5, "we ird -> x.txt": 2}

    # an unsquashfs without a long listing: names only
    monkeypatch.setattr(squashfs, "_unsquashfs_options", lambda tool: frozenset({"-l", "-d"}))
    reader = UnsquashfsReader(fake, read_elf_info(fake).payload_offset)
    assert set(reader.list_members()) == set(members) and reader.member_sizes() is None


def test_extract_top_level_glob_only(reader, tmp_path):
    dest = tmp_path / "out"
    reader.extract(["*.desktop", ".DirIcon"], dest)
    assert sorted(p.name for p in dest.iterdir()) == [".DirIcon", "app.desktop", "other.desktop"]
    assert (dest / "app.desktop").read_text() == FILES["app.desktop"]


def test_symlinks_are_extracted_as_symlinks(reader, tmp_path):
    dest = tmp_path / "out"
    reader.extract([".DirIcon", "escape"], dest)
    assert (dest / ".DirIcon").is_symlink()
    assert os.readlink(dest / ".DirIcon") == LINKS[".DirIcon"]
    assert os.readlink(dest / "escape") == "/etc/passwd"
    assert not (dest / "usr").exists()  # the target was not pulled in


def test_extract_nested_file_creates_parents(reader, tmp_path):
    dest = tmp_path / "out"
    reader.extract(["usr/share/icons/hicolor/64x64/apps/app.png"], dest)
    assert (dest / "usr/share/icons/hicolor/64x64/apps/app.png").read_bytes() == FILES[
        "usr/share/icons/hicolor/64x64/apps/app.png"]
    assert not (dest / "big").exists()


def test_missing_members_are_skipped(reader, tmp_path):
    dest = tmp_path / "out"
    reader.extract(["does/not/exist.png", "app.desktop"], dest)
    assert [p.name for p in dest.iterdir()] == ["app.desktop"]
    reader.extract(["nothing-here"], tmp_path / "empty")
    assert list((tmp_path / "empty").iterdir()) == []


def test_extract_twice_into_existing_dest(reader, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    reader.extract(["app.desktop"], dest)
    reader.extract(["app.desktop", "other.desktop"], dest)
    assert sorted(p.name for p in dest.iterdir()) == ["app.desktop", "other.desktop"]


def test_extract_nothing_is_a_no_op(reader, tmp_path):
    reader.extract([], tmp_path / "never")
    assert not (tmp_path / "never").exists()


def test_escape_pattern_matches_literally(reader, tmp_path):
    assert escape_pattern("a*b.png") == "a\\*b.png"
    assert escape_pattern("c[1].png") == "c\\[1\\].png"
    assert escape_pattern("plain/path-1.0_x.png") == "plain/path-1.0_x.png"
    dest = tmp_path / "out"
    reader.extract([escape_pattern("a*b.png"), escape_pattern("c[1].png")], dest)
    assert sorted(p.name for p in dest.iterdir()) == ["a*b.png", "c[1].png"]


def test_damaged_payload_raises_friendly_error(tmp_path, fake):
    data = bytearray(fake.read_bytes())
    offset = read_elf_info(fake).payload_offset
    for i in range(offset + 96, len(data)):
        data[i] = 0xAA  # destroy everything after the superblock
    broken = tmp_path / "broken.AppImage"
    broken.write_bytes(bytes(data))
    reader = UnsquashfsReader(broken, offset)
    with pytest.raises(ExtractionError) as exc:
        reader.list_members()
    assert str(exc.value) == "The file seems damaged or incomplete. Try downloading it again."
    assert exc.value.details and "exited with" in exc.value.details
    with pytest.raises(ExtractionError):
        reader.extract(["app.desktop"], tmp_path / "out")


def test_wrong_offset_raises(fake, tmp_path):
    with pytest.raises(ExtractionError):
        UnsquashfsReader(fake, 3).list_members()


def test_missing_tool(fake, monkeypatch):
    monkeypatch.setattr(squashfs, "find_unsquashfs", lambda: None)
    with pytest.raises(ExtractionError):
        UnsquashfsReader(fake, 0)
    reader = UnsquashfsReader(fake, 0, tool="/nonexistent/unsquashfs")
    with pytest.raises(ExtractionError) as exc:
        reader.list_members()
    assert "squashfs-tools" in str(exc.value)


def test_timeout(fake, tmp_path, monkeypatch):
    slow = tmp_path / "slow-unsquashfs"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    # (its "-help" would hang as well; that query has its own limit)
    monkeypatch.setattr(squashfs, "_unsquashfs_options", lambda tool: frozenset())
    reader = UnsquashfsReader(fake, 0, tool=str(slow), timeout=0.3)
    start = time.monotonic()
    with pytest.raises(ExtractionError) as exc:
        reader.list_members()
    assert time.monotonic() - start < 4
    assert "too long" in str(exc.value)


def test_unsquashfs_option_detection():
    options = squashfs._unsquashfs_options(shutil.which("unsquashfs"))
    assert {"-o", "-d", "-f", "-l"} <= options
    assert "-no-progress" in options and "-no-xattrs" in options


def test_source_is_not_modified(reader, fake, tmp_path):
    fake.chmod(0o644)
    before = (fake.stat().st_mtime_ns, fake.stat().st_mode, fake.read_bytes())
    reader.list_members()
    reader.extract(["*.desktop", "usr/share/icons/hicolor/64x64/apps/app.png"], tmp_path / "out")
    after = (fake.stat().st_mtime_ns, fake.stat().st_mode, fake.read_bytes())
    assert before == after


# ---------------------------------------------------------------------------------------------
# squashfs completeness & open_payload
# ---------------------------------------------------------------------------------------------

def test_check_squashfs_complete(fake, tmp_path):
    offset = read_elf_info(fake).payload_offset
    check_squashfs_complete(fake, offset)
    data = fake.read_bytes()
    truncated = tmp_path / "truncated.AppImage"
    truncated.write_bytes(data[: offset + 200])
    with pytest.raises(ExtractionError) as exc:
        check_squashfs_complete(truncated, offset)
    assert "damaged or incomplete" in str(exc.value)
    truncated.write_bytes(data[: offset + 50])
    with pytest.raises(ExtractionError):
        check_squashfs_complete(truncated, offset)


def test_open_payload_prefers_unsquashfs(fake):
    reader = open_payload(fake, read_elf_info(fake))
    assert isinstance(reader, UnsquashfsReader)


def test_open_payload_fallback_without_unsquashfs(fake, monkeypatch):
    monkeypatch.setattr(squashfs, "find_unsquashfs", lambda: None)
    reader = open_payload(fake, read_elf_info(fake))
    assert isinstance(reader, ExtractCommandReader)
    assert reader.list_members() is None


def test_open_payload_type1_uses_runtime(tmp_path):
    path = make_fake_appimage(tmp_path / "t1.AppImage", {}, payload=b"\0" * 64, appimage_type=1)
    assert isinstance(open_payload(path, read_elf_info(path)), ExtractCommandReader)


def test_open_payload_rejects_non_appimage(tmp_path):
    path = make_fake_appimage(tmp_path / "x.AppImage", {}, payload=b"NOTSQUASH" * 10, appimage_magic=False)
    with pytest.raises(NotAnAppImageError):
        open_payload(path, read_elf_info(path))


def test_open_payload_truncated_squashfs(fake, tmp_path):
    offset = read_elf_info(fake).payload_offset
    truncated = tmp_path / "t.AppImage"
    truncated.write_bytes(fake.read_bytes()[: offset + 300])
    with pytest.raises(ExtractionError):
        open_payload(truncated, read_elf_info(truncated))


def test_open_payload_squashfs_without_appimage_magic(tmp_path):
    path = make_fake_appimage(tmp_path / "x.AppImage", {"a": "b"}, appimage_magic=False)
    assert isinstance(open_payload(path, read_elf_info(path)), UnsquashfsReader)


# ---------------------------------------------------------------------------------------------
# ExtractCommandReader (with a script that emulates the AppImage runtime)
# ---------------------------------------------------------------------------------------------

def test_extract_command_reader(tmp_path, fake):
    script = make_runtime_emulator(tmp_path, fake)
    reader = ExtractCommandReader(fake, command=[str(script)])
    dest = tmp_path / "work" / "root"
    reader.extract(["*.desktop", ".DirIcon", "usr/share/icons/hicolor/64x64/apps/app.png"], dest)
    assert (dest / "app.desktop").read_text() == FILES["app.desktop"]
    assert (dest / ".DirIcon").is_symlink()
    assert (dest / "usr/share/icons/hicolor/64x64/apps/app.png").is_file()
    # merging a second extraction into the same tree works
    reader.extract([escape_pattern("a*b.png"), "missing.png"], dest)
    assert (dest / "a*b.png").read_bytes() == b"star"
    # the private scratch directories are gone
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["root"]


def test_extract_command_reader_sanitizes_environment(tmp_path, fake, monkeypatch):
    dump = tmp_path / "env.txt"
    script = make_runtime_emulator(tmp_path, fake, env_dump=dump)
    for var in ("APPIMAGE", "APPIMAGE_EXTRACT_AND_RUN", "APPIMAGE_SILENT_INSTALL", "TARGET_APPIMAGE",
                "APPDIR", "ARGV0", "OWD"):
        monkeypatch.setenv(var, "1")
    monkeypatch.setenv("KEEP_ME", "yes")
    ExtractCommandReader(fake, command=[str(script)]).extract(["app.desktop"], tmp_path / "out")
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines() if "=" in line}
    assert "KEEP_ME" in names
    assert not names & {"APPIMAGE", "APPIMAGE_EXTRACT_AND_RUN", "APPIMAGE_SILENT_INSTALL",
                        "TARGET_APPIMAGE", "APPDIR", "ARGV0", "OWD"}


def test_sanitized_runtime_env():
    env = sanitized_runtime_env({"APPIMAGE": "x", "APPIMAGE_FOO": "y", "APPIMAGES_DIR": "keep",
                                 "PATH": "/bin", "TARGET_APPIMAGE": "z", "APPDIR": "d"})
    assert env == {"APPIMAGES_DIR": "keep", "PATH": "/bin"}


def test_extract_command_reader_adds_exec_bit(tmp_path, fake):
    # The "AppImage" here is the emulator script itself, stored without the exec bit.
    script = make_runtime_emulator(tmp_path, fake, name="NoExec.AppImage")
    script.chmod(0o644)
    reader = ExtractCommandReader(script)
    reader.extract(["app.desktop"], tmp_path / "out")
    assert script.stat().st_mode & stat.S_IXUSR
    assert (tmp_path / "out" / "app.desktop").is_file()


def test_extract_command_reader_failure(tmp_path, fake):
    script = make_runtime_emulator(tmp_path, fake, extra="echo broken >&2; exit 3")
    with pytest.raises(ExtractionError) as exc:
        ExtractCommandReader(fake, command=[str(script)]).extract(["app.desktop"], tmp_path / "out")
    assert "broken" in exc.value.details


def test_extract_command_reader_timeout(tmp_path, fake):
    script = make_runtime_emulator(tmp_path, fake, extra="sleep 10")
    reader = ExtractCommandReader(fake, command=[str(script)], timeout=0.3)
    start = time.monotonic()
    with pytest.raises(ExtractionError) as exc:
        reader.extract(["app.desktop"], tmp_path / "out")
    assert time.monotonic() - start < 5
    assert "too long" in str(exc.value)


def test_extract_command_reader_cannot_execute(tmp_path):
    # e.g. an AppImage for another CPU or on a noexec mount -> friendly error, no traceback
    unrunnable = tmp_path / "Unrunnable.AppImage"
    unrunnable.write_bytes(b"\0\0\0\0 not a program")
    unrunnable.chmod(0o755)
    with pytest.raises(ExtractionError) as exc:
        ExtractCommandReader(unrunnable).extract(["app.desktop"], tmp_path / "out")
    assert "could not be read" in str(exc.value)


def test_merge_never_writes_through_symlinks(tmp_path, fake):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "work" / "root"
    dest.mkdir(parents=True)
    os.symlink(outside, dest / "usr")  # a hostile earlier extraction result
    script = make_runtime_emulator(tmp_path, fake)
    ExtractCommandReader(fake, command=[str(script)]).extract(
        ["usr/share/icons/hicolor/64x64/apps/app.png"], dest)
    assert list(outside.iterdir()) == []
    assert not (dest / "usr").is_symlink()
    assert (dest / "usr/share/icons/hicolor/64x64/apps/app.png").is_file()


# ---------------------------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_remove_tree_handles_read_only_directories(tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f").write_text("x")
    os.symlink("/etc", root / "a" / "etc-link")
    (root / "a" / "b").chmod(0o500)
    (root / "a").chmod(0o500)
    remove_tree(root)
    assert not root.exists()
    assert Path("/etc").is_dir()


def test_remove_tree_missing_and_file(tmp_path):
    remove_tree(tmp_path / "missing")
    f = tmp_path / "file"
    f.write_text("x")
    remove_tree(f)
    assert not f.exists()


def test_sample_appimage_listing(tmp_path):
    path = make_sample_appimage(tmp_path / "T3-Code-0.0.42-x86_64.AppImage", "t3code")
    members = UnsquashfsReader(path, read_elf_info(path).payload_offset).list_members()
    assert {"t3code.desktop", ".DirIcon", "t3code.png", "chrome-sandbox", "resources/app.asar"} <= set(members)
