"""Portable app archives: inspection (what is the app, which program starts it, which icon) and
unpacking that is safe against hostile archives."""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import os
import pwd
import random
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from easy_installer.core import portable
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.elf import arch_name
from easy_installer.core.imageinfo import ImageInfo
from easy_installer.core.portable import (
    ARCHIVE_SUFFIXES,
    MARKER_NAME,
    ExecutableCandidate,
    PortableInfo,
    archive_stem,
    archive_suffix,
    desktop_exec_matches,
    extract_portable,
    inspect_portable,
    is_portable_archive,
)
from easy_installer.errors import ArchiveError, EasyInstallerError, InstallError
from fakeappimage import make_png, make_svg
from fakearchive import (
    BLENDER_DESKTOP,
    ET_EXEC,
    ET_REL,
    FOREIGN_MACHINE,
    HOST_MACHINE,
    blender_tree,
    dir_entry,
    electron_tree,
    fake_elf,
    fake_script,
    file_entry,
    firefox_tree,
    flat_tree,
    hardlink_entry,
    jetbrains_tree,
    libraries_tree,
    make_archive,
    make_tar,
    make_zip,
    special_entry,
    symlink_entry,
    with_top,
)

HOST_ARCH = arch_name(HOST_MACHINE)
FOREIGN_ARCH = arch_name(FOREIGN_MACHINE)
TAR_SUFFIXES = tuple(s for s in ARCHIVE_SUFFIXES if s != ".zip")

DAMAGED = "The file seems damaged or incomplete. Try downloading it again."
NOT_AN_ARCHIVE = "This file is not an app archive that Easy Installer can open."
NOT_AN_APP = ("This archive does not look like an app. Easy Installer could not find a program to "
              "start in it.")
UNSAFE = "This archive is built in an unsafe way, so Easy Installer will not unpack it."
TOO_LARGE = "This archive is too large for Easy Installer to unpack."
NO_ICON = "Easy Installer could not find an icon for this app. A standard icon will be used."
NOT_SURE = ("Easy Installer is not sure which program starts this app. Please check the chosen "
            "program before you install it.")
NO_PROGRAM = "The program that starts this app was not found in the archive."
FOLDER_EXISTS = "The app could not be unpacked because its folder already exists."

# Real archives that may exist on the developer machine: only ever READ, never unpacked here.
# The real home from the password database: conftest.py has already pointed HOME at a sandbox
# when this module is imported (the archives are only read, never changed).
_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_ETCHER = _REAL_HOME / "Downloads" / "balenaEtcher-linux-x64-2.1.4.zip"
REAL_UVTOOLS = _REAL_HOME / "Downloads" / "UVtools_linux-x64_v6.2.0.zip"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

@pytest.fixture
def private_tempdir(tmp_path, monkeypatch):
    """Route tempfile into tmp_path so tests can check that nothing is left behind."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


@pytest.fixture
def inspect():
    created: list[PortableInfo] = []

    def _inspect(path, **kwargs) -> PortableInfo:
        kwargs.setdefault("compute_hash", False)
        info = inspect_portable(path, **kwargs)
        created.append(info)
        return info

    yield _inspect
    for info in created:
        info.cleanup()


@pytest.fixture
def sandbox(tmp_path):
    """``apps`` to unpack into and ``outside``, which no archive may ever touch."""
    apps = tmp_path / "apps"
    outside = tmp_path / "outside"
    apps.mkdir()
    outside.mkdir()
    (outside / "sentinel").write_text("untouched")
    return apps, outside


def snapshot(root: Path) -> dict[str, object]:
    """Everything below ``root``: files with content, links with target, folders."""
    found: dict[str, object] = {}
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            full = Path(folder) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                found[rel] = ("link", os.readlink(full))
            elif full.is_dir():
                found[rel] = "dir"
            else:
                found[rel] = full.read_bytes()
    return found


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def assert_links_stay_inside(root: Path) -> None:
    real_root = os.path.realpath(root)
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            full = os.path.join(folder, name)
            if os.path.islink(full):
                real = os.path.realpath(full)
                assert real == real_root or real.startswith(real_root + os.sep), (full, real)


def info_for(archive: Path, *, executable: str = "app", strip_prefix: str | None = None,
             tree_size: int = 0) -> PortableInfo:
    """A PortableInfo made by hand, to unpack archives that inspection would already refuse."""
    return PortableInfo(
        path=archive, size=archive.stat().st_size, sha256=None, app_id="app", name="App",
        display_name="App", version=None, comment=None, categories=[], terminal=False,
        strip_prefix=strip_prefix, desktop_entry=None, desktop_filename=None,
        executables=[ExecutableCandidate(executable, "elf", 0)], executable=executable,
        icon_path=None, icon_info=None, is_electron=False, arch=None, tree_size=tree_size,
        file_count=0, work_dir=archive.parent / "no-such-work-dir")


def relpaths(info: PortableInfo) -> list[str]:
    return [c.relpath for c in info.executables]


def scores(info: PortableInfo) -> dict[str, int]:
    return {c.relpath: c.score for c in info.executables}


def icon_name(info: PortableInfo) -> str | None:
    return info.icon_path.name if info.icon_path else None


APP = {"app": fake_elf()}
APP_MODES = {"app": 0o755}


# =============================================================================================
# is_portable_archive
# =============================================================================================

@pytest.mark.parametrize("suffix", ARCHIVE_SUFFIXES)
def test_is_portable_archive_needs_suffix_and_content(tmp_path, suffix):
    archive = make_archive(tmp_path / f"App-1.0{suffix}", APP, modes=APP_MODES)
    assert is_portable_archive(archive) is True
    assert is_portable_archive(str(archive)) is True
    assert archive_suffix(archive.name) == suffix

    shouting = tmp_path / f"APP-1.0{suffix.upper()}"
    shutil.copy(archive, shouting)
    assert is_portable_archive(shouting) is True

    no_suffix = tmp_path / f"renamed{suffix.replace('.', '-')}.bin"
    shutil.copy(archive, no_suffix)
    assert is_portable_archive(no_suffix) is False

    text = tmp_path / f"text{suffix}"
    text.write_text("This is just a text file that was renamed.\n" * 40)
    assert is_portable_archive(text) is False


def test_is_portable_archive_never_raises(tmp_path):
    (tmp_path / "empty.zip").write_bytes(b"")
    (tmp_path / "folder.tar.gz").mkdir()
    os.mkfifo(tmp_path / "pipe.zip")     # must not block either
    (tmp_path / ".zip").write_bytes(b"PK\x03\x04")
    for name in ("empty.zip", "folder.tar.gz", "pipe.zip", "missing.tar.xz", ".zip"):
        assert is_portable_archive(tmp_path / name) is False
    assert is_portable_archive(None) is False  # type: ignore[arg-type]
    appimage = tmp_path / "App.AppImage"
    appimage.write_bytes(fake_elf())
    assert is_portable_archive(appimage) is False


def test_the_content_decides_how_an_archive_is_read(tmp_path, inspect):
    """Browsers sometimes decompress a .tar.gz while downloading and keep the name."""
    plain = make_tar(tmp_path / "Tool-2.0.tar.gz", {"tool/tool": fake_elf()},
                     modes={"tool/tool": 0o755}, compression="")
    assert plain.read_bytes()[257:262] == b"ustar"
    assert is_portable_archive(plain)
    info = inspect(plain)
    assert (info.name, info.version, info.executable) == ("Tool", "2.0", "tool")


def test_old_tar_without_magic_is_recognised():
    header = bytearray(tarfile.TarInfo("app").tobuf(tarfile.USTAR_FORMAT))
    header[257:265] = bytes(8)                       # v7: no "ustar" magic
    header[148:156] = b" " * 8
    header[148:156] = b"%06o\0 " % sum(header)
    assert portable._looks_like_tar(bytes(header))
    assert portable._format_of(bytes(header)) == "tar"
    assert not portable._looks_like_tar(bytes(512))
    assert not portable._looks_like_tar(b"x" * 512)
    assert not portable._looks_like_tar(b"short")


def test_archive_stem():
    assert archive_stem("blender-4.2.0-linux-x64.tar.xz") == "blender-4.2.0-linux-x64"
    assert archive_stem("/some/where/App.ZIP") == "App"
    assert archive_stem("notes.txt") == "notes.txt"
    assert archive_suffix("a.tar.gz") == ".tar.gz" and archive_suffix("a.tgz") == ".tgz"
    assert archive_suffix("a.gz") is None and archive_suffix("tar") is None


# =============================================================================================
# Realistic layouts
# =============================================================================================

@pytest.mark.parametrize("suffix", ARCHIVE_SUFFIXES)
def test_electron_app_in_a_single_folder(tmp_path, inspect, private_tempdir, suffix):
    archive = make_archive(tmp_path / f"MyApp-linux-x64-1.2.3{suffix}", **electron_tree())
    info = inspect(archive, compute_hash=True)
    assert info.path == archive.resolve()
    assert info.size == archive.stat().st_size
    assert info.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (info.app_id, info.name, info.display_name) == ("myapp", "MyApp", "MyApp")
    assert info.version == "1.2.3"          # not 37.2.4: that "version" file is Electron's
    assert info.strip_prefix == "MyApp-linux-x64"
    assert info.executable == "my-app"
    assert info.executables[0] == ExecutableCandidate("my-app", "elf", 72)
    assert scores(info) == {"my-app": 72, "resources/helper-util": -58, "chrome-sandbox": -48,
                            "chrome_crashpad_handler": -48}
    assert info.is_electron is True
    assert info.exec_has_no_sandbox is False
    assert info.arch == HOST_ARCH
    assert info.desktop_entry is None and info.desktop_filename is None
    assert info.comment is None and info.categories == [] and info.terminal is False
    assert (info.icon_path, info.icon_info) == (None, None)
    assert info.warnings == [NO_ICON]
    tree = electron_tree()
    assert info.tree_size == sum(len(data) for data in tree["files"].values())
    assert info.file_count == len(tree["files"]) + len(tree["symlinks"])


def test_jetbrains_ide(tmp_path, inspect):
    archive = make_archive(tmp_path / "ideaIU-2024.1.4.tar.gz", **jetbrains_tree())
    info = inspect(archive)
    assert (info.app_id, info.name) == ("intellij-idea", "IntelliJ IDEA")
    assert info.version == "2024.1.4"
    assert info.strip_prefix == "idea-IU-241.18034.62"
    assert info.executable == "bin/idea.sh"
    assert info.executables[0].kind == "script"
    assert scores(info)["bin/idea.sh"] == 90 + 30 + 8 + 5      # launcher, "idea", bin/, wrapper
    assert scores(info)["bin/idea"] == 30 + 8 + 2
    assert scores(info)["bin/fsnotifier"] < 0 and scores(info)["bin/restarter"] < 0
    assert "bin/libdbm.so" not in relpaths(info) and "jbr/bin/java" not in relpaths(info)
    assert icon_name(info) == "idea.svg"
    assert info.icon_info == ImageInfo("svg", 128, 128)
    assert info.wm_class == "jetbrains-idea"
    assert info.is_electron is False and info.desktop_entry is None
    assert info.arch == HOST_ARCH           # of bin/idea: the chosen program is a script
    assert info.warnings == []


def test_jetbrains_native_launcher(tmp_path, inspect):
    archive = make_archive(tmp_path / "ideaIU.tar.gz", **jetbrains_tree(launcher="bin/idea",
                                                                        version="2025.2"))
    info = inspect(archive)
    assert (info.executable, info.executables[0].kind, info.version) == ("bin/idea", "elf", "2025.2")


@pytest.mark.parametrize("launcher", ["bin/missing.sh", "../../../bin/sh", "/bin/sh", "", 7, None])
def test_product_info_cannot_point_anywhere(tmp_path, inspect, launcher):
    tree = jetbrains_tree(top="idea")
    product = json.loads(tree["files"]["idea/product-info.json"])
    product["launch"][1]["launcherPath"] = launcher
    product["svgIconPath"] = "../../../usr/share/icons/evil.svg"
    tree["files"]["idea/product-info.json"] = json.dumps(product)
    info = inspect(make_archive(tmp_path / "idea.tar.gz", **tree))
    assert info.executable == "bin/idea.sh"                  # by name, as without product-info
    assert scores(info)["bin/idea.sh"] == 60 + 8 + 5         # the folder is called "idea"
    assert icon_name(info) == "idea.svg"                     # found by its name instead


@pytest.mark.parametrize("content", [b"", b"not json", b"[1, 2]", b'{"name": 5, "launch": "x"}',
                                     b'{"launch": [1, {"os": 3}]}', b"[" * 5000])
def test_broken_product_info_is_ignored(tmp_path, inspect, content):
    tree = jetbrains_tree(top="idea")
    tree["files"]["idea/product-info.json"] = content
    info = inspect(make_archive(tmp_path / "idea-IU-2024.2.tar.gz", **tree))
    assert (info.name, info.version, info.executable) == ("Idea", "2024.2", "bin/idea.sh")
    assert info.wm_class is None


@pytest.mark.parametrize("suffix", [".tar.xz", ".tar.bz2"])
def test_firefox(tmp_path, inspect, suffix):
    archive = make_archive(tmp_path / f"firefox-128.0.3{suffix}", **firefox_tree())
    info = inspect(archive)
    assert (info.app_id, info.name, info.version) == ("firefox", "Firefox", "128.0.3")
    assert info.strip_prefix == "firefox"
    assert relpaths(info)[:2] == ["firefox", "firefox-bin"]
    assert scores(info)["firefox"] == 72 and scores(info)["firefox-bin"] == 42
    for helper in ("crashreporter", "updater", "pingsender", "plugin-container", "glxtest"):
        assert scores(info)[helper] == -48, helper
    assert not any(".so" in path for path in relpaths(info))
    assert icon_name(info) == "default128.png"
    assert info.icon_info == ImageInfo("png", 128, 128)
    assert info.is_electron is False
    assert info.warnings == []


def test_application_ini_version_beats_the_file_name(tmp_path, inspect):
    archive = make_archive(tmp_path / "browser-1.0.tar.gz", **firefox_tree(version="130.0b2"))
    assert inspect(archive).version == "130.0b2"
    nested = {"Browser/application.ini": "[App]\nName=Tor Browser\nVersion=13.5.1\n",
              "Browser/start-tor-browser": fake_script()}
    info = inspect(make_archive(tmp_path / "tor.tar.xz", with_top({"f": nested}, "tor-browser")["f"],
                                modes={"tor-browser/Browser/start-tor-browser": 0o755}))
    assert (info.name, info.version) == ("Tor Browser", "13.5.1")
    assert info.executable == "Browser/start-tor-browser"


def test_blender(tmp_path, inspect, monkeypatch):
    archive = make_archive(tmp_path / "blender-4.2.0-linux-x64.tar.xz", **blender_tree())
    info = inspect(archive)
    assert (info.app_id, info.name, info.display_name) == ("blender", "Blender", "Blender")
    assert info.version == "4.2.0"
    assert info.desktop_filename == "blender.desktop"
    assert isinstance(info.desktop_entry, DesktopEntry)
    assert info.desktop_entry.get("Exec") == "blender %f"
    assert info.comment == "3D modeling, animation, rendering and post-production"
    assert info.categories == ["Graphics", "3DGraphics"]
    assert info.terminal is False
    assert info.executable == "blender"
    assert scores(info)["blender"] == 100 + 60 + 10 + 2
    assert relpaths(info) == ["blender", "blender-launcher", "blender-softwaregl", "blender-thumbnailer"]
    assert scores(info)["blender-thumbnailer"] < 0
    assert icon_name(info) == "blender.svg"
    assert info.icon_info == ImageInfo("svg", 64, 64)
    assert info.warnings == []

    monkeypatch.setenv("LANGUAGE", "de")
    assert inspect(archive).comment == "3D-Modellierung, Animation, Rendering und Nachbearbeitung"


def test_flat_archive_without_a_top_folder(tmp_path, inspect):
    archive = make_zip(tmp_path / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    info = inspect(archive)
    assert (info.app_id, info.name, info.version) == ("uvtools", "UVtools", "6.2.0")
    assert info.strip_prefix is None
    # every file is marked executable in this zip: only real programs are candidates
    assert relpaths(info) == ["UVtools.sh", "UVtools", "UVtoolsCmd", "createdump"]
    assert scores(info) == {"UVtools.sh": 75, "UVtools": 72, "UVtoolsCmd": 42, "createdump": -48}
    assert [c.kind for c in info.executables] == ["script", "elf", "elf", "elf"]
    assert info.arch == HOST_ARCH
    assert info.is_electron is False
    assert info.warnings == [NO_ICON]


@pytest.mark.parametrize("suffix", [".tar.gz", ".zip"])
def test_archive_with_only_libraries_is_not_an_app(tmp_path, private_tempdir, suffix):
    archive = make_archive(tmp_path / f"libfoo-1.2.3{suffix}", **libraries_tree())
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == NOT_AN_APP
    assert "does not look like an app" in str(excinfo.value)
    assert list(private_tempdir.iterdir()) == []


def test_archives_without_programs_are_not_apps(tmp_path):
    cases = {
        "photos.zip": dict(files={"a.png": make_png(8, 8), "b/c.txt": "hello"}),
        "docs.tar.gz": dict(files={"README": "read me", "notes/todo.txt": "later"}),
        "windows.zip": dict(files={"app.exe": b"MZ\x90\0" + bytes(60)}, modes={"app.exe": 0o755}),
        "deep.tar": dict(files={"a/b/c/app": fake_elf()}, modes={"a/b/c/app": 0o755}),
        "objects.tar": dict(files={"app": fake_elf(e_type=ET_REL)}, modes={"app": 0o755}),
        "text.tar": dict(files={"app": "just text"}, modes={"app": 0o755}),
        "folders.tar": dict(dirs=["a", "a/b"]),
    }
    for name, tree in cases.items():
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(make_archive(tmp_path / name, **tree), compute_hash=False)
        assert str(excinfo.value) == NOT_AN_APP, name
    for name in ("empty.tar.gz", "empty.tar.xz", "empty.zip"):
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(make_archive(tmp_path / name), compute_hash=False)
        assert str(excinfo.value) == NOT_AN_APP, name
    empty_tar = make_archive(tmp_path / "empty.tar")      # nothing but zero blocks: no tar header
    assert not is_portable_archive(empty_tar)
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(empty_tar)
    assert str(excinfo.value) == NOT_AN_ARCHIVE


# =============================================================================================
# Top folder, names, versions
# =============================================================================================

def test_single_file_archive(tmp_path, inspect):
    info = inspect(make_tar(tmp_path / "tool-0.9.tar.gz", {"tool": fake_elf(e_type=ET_EXEC)},
                            modes={"tool": 0o755}))
    assert (info.strip_prefix, info.executable, info.name, info.version) == (None, "tool", "Tool", "0.9")
    assert (info.tree_size, info.file_count) == (256, 1)


def test_top_folder_is_only_stripped_when_everything_is_inside(tmp_path, inspect):
    files = {"app/app": fake_elf(), "app/data.bin": b"1234", "README.txt": "hi"}
    info = inspect(make_zip(tmp_path / "app.zip", files, modes={"app/app": 0o755}))
    assert (info.strip_prefix, info.executable) == (None, "app/app")

    # "./" prefixes and explicit folder members are the same top folder
    entries = [dir_entry("./"), dir_entry("./cool-tool-2.0"),
               file_entry("./cool-tool-2.0/cool-tool", fake_elf(), 0o755)]
    info = inspect(make_tar(tmp_path / "download.tar", entries=entries))
    assert info.strip_prefix == "cool-tool-2.0"
    assert (info.name, info.version, info.app_id, info.executable) == ("Cool Tool", "2.0", "cool-tool", "cool-tool")


def test_macos_resource_forks_are_ignored(tmp_path, inspect, sandbox):
    apps, _outside = sandbox
    files = {"MyTool/mytool": fake_elf(), "__MACOSX/MyTool/._mytool": b"\0\5\x16\7", "__MACOSX/._MyTool": b"x"}
    archive = make_zip(tmp_path / "MyTool.zip", files, modes={"MyTool/mytool": 0o755})
    info = inspect(archive)
    assert (info.strip_prefix, info.executable, info.file_count) == ("MyTool", "mytool", 1)
    extract_portable(info, apps / "MyTool")
    assert sorted(snapshot(apps / "MyTool")) == ["mytool"]


@pytest.mark.parametrize("archive_name, top, expected", [
    ("balenaEtcher-linux-x64-2.1.4.zip", "balenaEtcher-linux-x64", ("balenaEtcher", "2.1.4", "balenaetcher")),
    ("download.zip", "sublime_text", ("Sublime Text", None, "sublime-text")),
    ("My-Cool-App-3.1-beta2.tar.gz", "dist", ("My Cool App", "3.1-beta2", "my-cool-app")),
    ("thing.tar.gz", "tool-v1.4.2-linux-amd64", ("Tool", "1.4.2", "tool")),
    ("Godot_v4.2.2-stable_linux.x86_64.zip", None, ("Godot", "4.2.2", "godot")),
    ("app.zip", "app", ("App", None, "app")),
    ("tsetup.5.2.3.tar.xz", "Telegram", ("Telegram", "5.2.3", "telegram")),
    ("linux-x64.zip", "release", ("Linux-x64", None, "linux-x64")),      # nothing better to go by
    ("Программа-2.0.zip", None,
     ("Программа", "2.0", "app-" + hashlib.sha256("Программа".encode()).hexdigest()[:10])),
])
def test_name_and_version_from_folder_and_file_name(tmp_path, inspect, archive_name, top, expected):
    tree = with_top({"files": {"program": fake_elf()}, "modes": {"program": 0o755}}, top)
    info = inspect(make_archive(tmp_path / archive_name, **tree))
    assert (info.name, info.version, info.app_id) == expected
    assert info.display_name == info.name


def test_version_file(tmp_path, inspect):
    def version_of(content: str, **extra) -> str | None:
        files = {"tool/tool": fake_elf(), "tool/VERSION": content, **extra}
        archive = make_archive(tmp_path / "tool.tar.gz", files, modes={"tool/tool": 0o755})
        return inspect(archive).version

    assert version_of("v2.5.1\n") == "2.5.1"
    assert version_of("\n  3.0-rc1  \nsecond line\n") == "3.0-rc1"
    assert version_of("stable") is None
    assert version_of("7") is None
    assert version_of("1.0 && rm -rf /") is None
    assert version_of("1.2.3", **{"tool/chrome-sandbox": fake_elf()}) is None     # Electron's own
    # the file name wins: a "version" file may belong to a bundled runtime
    archive = make_archive(tmp_path / "tool-9.9.tar.gz", {"tool/tool": fake_elf(), "tool/version": "1.0.0"},
                           modes={"tool/tool": 0o755})
    assert inspect(archive).version == "9.9"


def test_desktop_entry_is_chosen_like_for_appimages(tmp_path, inspect):
    hidden = "[Desktop Entry]\nType=Application\nName=Aaa Helper\nExec=helper\nNoDisplay=true\n"
    main = "[Desktop Entry]\nType=Application\nName=Zed Editor\nExec=bin/zed --new %U\nIcon=zed\nTerminal=true\n"
    files = {"aaa.desktop": hidden, "zed.desktop": main, "bin/zed": fake_elf(), "helper": fake_elf(),
             "junk.desktop": "no group here"}
    info = inspect(make_archive(tmp_path / "x.tar.gz", files, modes={"bin/zed": 0o755, "helper": 0o755}))
    assert (info.desktop_filename, info.name, info.app_id) == ("zed.desktop", "Zed Editor", "zed")
    assert info.terminal is True
    assert info.executable == "bin/zed"

    # a menu entry where packages keep it is found as well
    files = {"share/applications/org.example.Tool.desktop": main.replace("Zed Editor", "Tool"),
             "bin/zed": fake_elf()}
    info = inspect(make_archive(tmp_path / "y.tar.gz", files, modes={"bin/zed": 0o755}))
    assert (info.desktop_filename, info.app_id, info.name) == ("org.example.Tool.desktop", "org.example.Tool", "Tool")


def test_exec_has_no_sandbox(tmp_path, inspect):
    desktop = "[Desktop Entry]\nType=Application\nName=E\nExec=e --no-sandbox %U\n"
    info = inspect(make_archive(tmp_path / "e.zip", {"e": fake_elf(), "e.desktop": desktop,
                                                     "chrome-sandbox": fake_elf()},
                                modes={"e": 0o755, "chrome-sandbox": 0o755}))
    assert info.is_electron is True and info.exec_has_no_sandbox is True


# =============================================================================================
# Which program starts the app
# =============================================================================================

def test_choice_does_not_depend_on_the_order_of_the_archive(tmp_path, inspect):
    tree = firefox_tree()
    expected = None
    for seed in range(4):
        names = list(tree["files"])
        random.Random(seed).shuffle(names)
        files = {name: tree["files"][name] for name in names}
        archive = make_archive(tmp_path / f"firefox-{seed}.tar", files, modes=tree["modes"])
        found = [(c.relpath, c.kind, c.score) for c in inspect(archive).executables]
        expected = expected or found
        assert found == expected
    # equal scores: the shorter path first, then alphabetical
    assert [item[0] for item in expected] == [
        "firefox", "firefox-bin", "glxtest", "updater", "pingsender", "crashreporter", "plugin-container"]


def test_candidates_are_real_programs_near_the_top(tmp_path, inspect):
    files = {
        "tool": fake_elf(), "bin/tool-gui": fake_script(), "x/y/deep": fake_elf(),
        "lib.so": fake_elf(), "lib.so.3.1": fake_elf(), "addon.node": fake_elf(), "object.o": fake_elf(e_type=ET_REL),
        "tool.dll": b"MZ" + bytes(62), "notes": "plain text", "tiny": b"#!",
        "big-endian": fake_elf(21, little_endian=False, bits=64),
        "no-exec-bit": fake_elf(), "tool.desktop": "#!/usr/bin/env xdg-open\n[Desktop Entry]\nName=Tool\nExec=tool\n",
        "binary-hashbang": b"#!\0\0\0\0\0\0",
    }
    modes = {name: 0o755 for name in files if name != "no-exec-bit"}
    info = inspect(make_archive(tmp_path / "tool.tar.gz", files, modes=modes))
    assert set(relpaths(info)) == {"tool", "bin/tool-gui", "big-endian"}
    assert info.executable == "tool"
    assert scores(info)["big-endian"] == 10 + 2 - 40         # another kind of computer


def test_generic_launcher_names(tmp_path, inspect):
    files = {"AppRun": fake_script(), "engine": fake_elf(), "tools/convert": fake_elf()}
    info = inspect(make_archive(tmp_path / "Something.zip", files, modes={n: 0o755 for n in files}))
    assert scores(info) == {"AppRun": 30, "engine": 12, "tools/convert": 2}
    assert info.warnings == [NO_ICON]

    files = {"bin/start.sh": fake_script(), "bin/daemon": fake_elf(), "README": "read me"}
    info = inspect(make_archive(tmp_path / "Other.zip", files, modes={"bin/start.sh": 0o755, "bin/daemon": 0o755}))
    assert scores(info) == {"bin/start.sh": 28, "bin/daemon": 10}


def test_unclear_choice_is_a_warning(tmp_path, inspect):
    files = {"bravo": fake_elf(), "alpha": fake_elf(), "bin/thing-helper": fake_elf()}
    info = inspect(make_archive(tmp_path / "Thing.zip", files, modes={n: 0o755 for n in files}))
    assert relpaths(info) == ["alpha", "bravo", "bin/thing-helper"]     # equal scores: alphabetical
    assert info.warnings == [NOT_SURE, NO_ICON]
    # one clear winner: no warning
    files["thing"] = fake_elf()
    info = inspect(make_archive(tmp_path / "Thing.zip", files, modes={n: 0o755 for n in files}))
    assert info.executable == "thing" and info.warnings == [NO_ICON]


def test_program_for_this_computer_is_preferred(tmp_path, inspect):
    files = {"tool": fake_elf(FOREIGN_MACHINE), "bin/tool": fake_elf()}
    info = inspect(make_archive(tmp_path / "tool.zip", files, modes={n: 0o755 for n in files}))
    assert scores(info) == {"bin/tool": 70, "tool": 32}
    assert (info.executable, info.arch) == ("bin/tool", HOST_ARCH)
    assert info.warnings == [NO_ICON]

    info = inspect(make_archive(tmp_path / "foreign-tool.zip", {"tool": fake_elf(FOREIGN_MACHINE)},
                                modes={"tool": 0o755}))
    assert info.arch == FOREIGN_ARCH
    assert info.warnings[0] == (f"This app is made for a different kind of computer ({FOREIGN_ARCH}). "
                                "It will probably not start on this one.")


def test_links_near_the_top_lead_to_the_program(tmp_path, inspect, sandbox):
    apps, _outside = sandbox
    # a link that is another name for a program near the top
    files = {"bin/launcher-x": fake_elf(), "bin/other": fake_elf()}
    info = inspect(make_archive(tmp_path / "CoolApp.tar.gz", files, {"coolapp": "bin/launcher-x"},
                                {n: 0o755 for n in files}))
    assert scores(info) == {"bin/launcher-x": 70, "bin/other": 10}
    # a link to a program that lies deeper: the link is how the app is started
    files = {"opt/postman/app/postman": fake_elf(), "opt/postman/app/chrome-sandbox": fake_elf()}
    info = inspect(make_archive(tmp_path / "Postman.tar.gz", files, {"Postman": "opt/postman/app/postman",
                                                                     "broken": "opt/missing", "up": ".."},
                                {n: 0o755 for n in files}))
    assert relpaths(info) == ["Postman"]
    assert (info.executables[0].kind, info.executables[0].score) == ("elf", 72)
    extract_portable(info, apps / "Postman")
    assert os.readlink(apps / "Postman" / "Postman") == "opt/postman/app/postman"
    assert mode_of(apps / "Postman" / "opt/postman/app/postman") == 0o755


@pytest.mark.parametrize("exec_value, relpath, expected", [
    ("blender %f", "blender", True),
    ("blender %f", "bin/blender", True),
    ("blender %f", "blender-launcher", False),
    ("./bin/app --flag", "bin/app", True),
    ("/opt/vendor/app/bin/app %U", "bin/app", True),
    ("/opt/vendor/app/bin/app %U", "app", True),         # where the vendor installs it is unknown
    ("/opt/vendor/app/bin/app %U", "lib/app", False),
    ("/usr/bin/myapp", "app", False),
    ("env FOO=1 tool", "tool", True),
    ('sh -c "exec ./tool"', "tool", False),
    ("", "tool", False),
    ('"unbalanced', "unbalanced", False),
])
def test_desktop_exec_matches(exec_value, relpath, expected):
    entry = DesktopEntry.parse(f"[Desktop Entry]\nName=X\nExec={exec_value}\n")
    assert desktop_exec_matches(entry, relpath) is expected
    assert desktop_exec_matches(None, relpath) is False


def test_tryexec_counts_like_exec():
    entry = DesktopEntry.parse("[Desktop Entry]\nName=X\nExec=sh -c tool\nTryExec=bin/tool\n")
    assert desktop_exec_matches(entry, "bin/tool") is True


def test_many_programs_are_capped(tmp_path, inspect, monkeypatch):
    files = {f"prog{i:03d}": fake_elf() for i in range(60)}
    archive = make_zip(tmp_path / "many.zip", files, unix=False)
    info = inspect(archive)
    assert len(info.executables) == portable.MAX_EXECUTABLES
    assert info.executable == "prog000"
    monkeypatch.setattr(portable, "MAX_HEAD_READS", 5)
    assert relpaths(inspect(archive)) == [f"prog{i:03d}" for i in range(5)]


# =============================================================================================
# Icons
# =============================================================================================

def desktop(icon: str, exec_: str = "app") -> str:
    return f"[Desktop Entry]\nType=Application\nName=App\nExec={exec_}\nIcon={icon}\n"


def icon_case(tmp_path, inspect, files: dict, name: str = "app.tar.gz", **kwargs) -> PortableInfo:
    files = {"app": fake_elf(), **files}
    return inspect(make_archive(tmp_path / name, files, modes={"app": 0o755}, **kwargs))


def test_icon_from_desktop_entry(tmp_path, inspect):
    images = {
        "share/icons/hicolor/48x48/apps/org.example.App.png": make_png(48, 48),
        "share/icons/hicolor/256x256/apps/org.example.App.png": make_png(256, 256),
        "share/icons/hicolor/32x32/apps/org.example.App.xpm": "/* XPM */\nstatic char *x[] = {\n\"32 32 1 1\",",
        "other.png": make_png(512, 512),
    }
    info = icon_case(tmp_path, inspect, {"app.desktop": desktop("org.example.App"), **images})
    assert info.icon_info == ImageInfo("png", 256, 256)
    assert info.icon_path.read_bytes() == images["share/icons/hicolor/256x256/apps/org.example.App.png"]
    assert info.icon_path.is_relative_to(info.work_dir)

    svg = {"share/icons/hicolor/scalable/apps/org.example.App.svg": make_svg(64, 64)}
    info = icon_case(tmp_path, inspect, {"app.desktop": desktop("org.example.App"), **images, **svg})
    assert info.icon_info.format == "svg"

    # an absolute path from the vendor's own package: the file of that name is meant
    info = icon_case(tmp_path, inspect, {"app.desktop": desktop("/opt/vendor/app/logo256.png"),
                                         "resources/logo256.png": make_png(256, 256),
                                         "icon.png": make_png(64, 64)})
    assert (icon_name(info), info.icon_info.width) == ("logo256.png", 256)

    # a path inside the archive, also relative to the menu entry
    files = {"share/applications/app.desktop": desktop("../pixmaps/a.png"), "share/pixmaps/a.png": make_png(32, 32),
             "share/applications/b.png": make_png(24, 24)}
    assert icon_name(icon_case(tmp_path, inspect, files)) == "a.png"
    files["share/applications/app.desktop"] = desktop("b.png")
    assert icon_name(icon_case(tmp_path, inspect, files)) == "b.png"


def test_icon_is_found_through_links_inside_the_archive(tmp_path, inspect):
    files = {"app.desktop": desktop("pics/current.png"), "pics/v2/real.png": make_png(96, 96)}
    info = icon_case(tmp_path, inspect, files, symlinks={"pics/current.png": "v2/real.png"})
    assert info.icon_info == ImageInfo("png", 96, 96)
    info = icon_case(tmp_path, inspect, files, symlinks={"pics/current.png": "/usr/share/pixmaps/x.png"})
    assert info.icon_path is None


def test_largest_matching_image_is_the_icon(tmp_path, inspect):
    files = {
        "resources/app_16.png": make_png(16, 16), "resources/app_256.png": make_png(256, 256),
        "resources/app_banner.png": make_png(1200, 300), "resources/app-symbolic.svg": make_svg(16, 16),
        "resources/unrelated.png": make_png(512, 512), "data/textures/icon.png": make_png(1024, 1024),
    }
    info = icon_case(tmp_path, inspect, files)
    assert (icon_name(info), info.icon_info) == ("app_256.png", ImageInfo("png", 256, 256))
    # a banner or screenshot named like the app is not its icon
    info = icon_case(tmp_path, inspect, {"app-screenshot.png": make_png(800, 450), "docs/app_wide.png": make_png(64, 20)})
    assert info.icon_path is None
    info = icon_case(tmp_path, inspect, {"app.desktop": desktop("wide"), "wide.png": make_png(64, 40)})
    assert icon_name(info) == "wide.png"         # ... unless the app says so itself

    # nothing named like the app: "icon"/"logo" near the top, or anything in an icons folder
    info = icon_case(tmp_path, inspect, {"assets/logo.png": make_png(64, 64), "x/y/z/w/icon.png": make_png(512, 512),
                                         "screenshot.png": make_png(800, 600)}, name="Thing.zip")
    assert icon_name(info) == "logo.png"
    info = icon_case(tmp_path, inspect, {"resources/icons/512x512.png": make_png(512, 512),
                                         "resources/icons/32x32.png": make_png(32, 32)}, name="Thing.zip")
    assert info.icon_info == ImageInfo("png", 512, 512)
    info = icon_case(tmp_path, inspect, {"docs/diagram.svg": make_svg(), "photo.png": make_png(64, 64)},
                     name="Thing.zip")
    assert info.icon_path is None and NO_ICON in info.warnings


def test_unusable_images_are_skipped(tmp_path, inspect, monkeypatch):
    files = {"app.desktop": desktop("app"), "app.svg": "this is not an image", "app.png": make_png(48, 48)}
    info = icon_case(tmp_path, inspect, files)
    assert icon_name(info) == "app.png"
    assert [p.name for p in info.icon_path.parent.iterdir()] == ["app.png"]

    monkeypatch.setattr(portable, "MAX_ICON_SIZE", 2000)
    big = make_png(64, 64) + bytes(4000)
    info = icon_case(tmp_path, inspect, {"app.desktop": desktop("app"), "app.png": big,
                                         "icons/app.png": make_png(16, 16)})
    assert info.icon_info == ImageInfo("png", 16, 16)


@pytest.mark.parametrize("suffix", [".tar.gz", ".tar", ".zip"])
def test_icon_far_from_the_usual_places(tmp_path, inspect, monkeypatch, suffix):
    """A compressed tar is read front to back; an icon nobody expected needs a second pass."""
    files = {"code": fake_elf(), "resources/app/resources/linux/code.png": make_png(512, 512),
             "resources/app/out/big.bin": os.urandom(300_000)}
    archive = make_archive(tmp_path / f"code-stable{suffix}", with_top({"f": files}, "VSCode-linux-x64")["f"],
                           modes={"VSCode-linux-x64/code": 0o755})
    info = inspect(archive)
    assert (info.executable, info.name) == ("code", "VSCode")
    assert info.icon_info == ImageInfo("png", 512, 512)

    # ... and nothing depends on what the first pass happened to keep
    monkeypatch.setattr(portable, "_CAPTURE_BUDGET", 0)
    for tree in (jetbrains_tree(), firefox_tree(), blender_tree()):
        again = inspect(make_archive(tmp_path / f"again{suffix}", **tree))
        assert again.icon_path is not None and again.warnings == []
    assert inspect(make_archive(tmp_path / f"again{suffix}", **blender_tree())).name == "Blender"


# =============================================================================================
# Inspection reads little, keeps little
# =============================================================================================

def test_inspection_does_not_unpack_the_archive(tmp_path, inspect, monkeypatch):
    tree = electron_tree()
    tree["files"]["MyApp-linux-x64/resources/huge.bin"] = os.urandom(3_000_000)
    tree["files"]["MyApp-linux-x64/my-app.png"] = make_png(64, 64)
    reads: list[tuple[str, int]] = []
    original = portable._ZipReader.read

    def recording(self, member, limit):
        reads.append((member.path.split("/", 1)[1], limit))
        return original(self, member, limit)

    monkeypatch.setattr(portable._ZipReader, "read", recording)
    monkeypatch.setattr(portable._Reader, "chunks", None)       # never needed to inspect
    info = inspect(make_zip(tmp_path / "MyApp.zip", **tree))
    assert info.icon_info == ImageInfo("png", 64, 64)
    kept = [p for p in info.work_dir.rglob("*") if p.is_file()]
    assert [p.name for p in kept] == ["my-app.png"]
    assert all(limit <= 1024 for path, limit in reads if not path.endswith(".png")), reads
    assert not any("huge" in path or path.endswith((".pak", ".dat", ".asar")) for path, _limit in reads)
    assert ("my-app", 64) in reads


def test_size_caps_of_metadata_files(tmp_path, inspect, monkeypatch):
    tree = blender_tree(top=None)
    tree["files"]["blender.desktop"] = BLENDER_DESKTOP + "# padding\n" * 30_000      # > 256 KiB
    info = inspect(make_archive(tmp_path / "blender-4.2.0.tar.gz", **tree))
    assert info.desktop_entry is None and info.comment is None
    assert (info.name, info.executable, icon_name(info)) == ("Blender", "blender", "blender.svg")

    monkeypatch.setattr(portable, "MAX_METADATA_SIZE", 200)
    info = inspect(make_archive(tmp_path / "idea-2024.1.tar.gz", **jetbrains_tree()))
    assert (info.name, info.version, info.wm_class) == ("idea IU", "2024.1", None)
    info = inspect(make_archive(tmp_path / "ff-1.0.tar.gz", **firefox_tree(version="99.0")))
    assert info.version == "99.0"            # application.ini is smaller than that
    monkeypatch.setattr(portable, "MAX_METADATA_SIZE", 20)
    assert inspect(make_archive(tmp_path / "ff-1.0.zip", **firefox_tree(version="99.0"))).version == "1.0"


def test_too_many_desktop_files(tmp_path, inspect):
    files = {f"entry{i:02d}.desktop": f"[Desktop Entry]\nName=Entry {i}\nExec=app\nNoDisplay=true\n"
             for i in range(40)}
    files["zz-main.desktop"] = "[Desktop Entry]\nType=Application\nName=Main\nExec=app\n"
    info = icon_case(tmp_path, inspect, files)
    assert info.name == "Entry 0"            # only the first 16 (by name) are looked at


# =============================================================================================
# work_dir, progress, hash, the source file
# =============================================================================================

def test_work_dir_lifecycle(tmp_path, private_tempdir):
    archive = make_archive(tmp_path / "blender-4.2.tar.gz", **blender_tree())
    info = inspect_portable(archive, compute_hash=False)
    work_dir = info.work_dir
    assert work_dir.is_dir() and work_dir.parent == private_tempdir
    assert work_dir.name.startswith("easy-installer-")
    assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700
    assert info.icon_path.is_relative_to(work_dir)
    info.cleanup()
    assert not work_dir.exists()
    info.cleanup()  # idempotent

    with inspect_portable(archive, compute_hash=False) as info2:
        work_dir2 = info2.work_dir
        assert work_dir2.is_dir()
    assert not work_dir2.exists()

    info3 = inspect_portable(archive, compute_hash=False)
    work_dir3 = info3.work_dir
    del info3
    gc.collect()
    assert not work_dir3.exists()
    assert list(private_tempdir.iterdir()) == []


def test_work_dir_is_removed_when_inspection_fails(tmp_path, private_tempdir, monkeypatch):
    archive = make_archive(tmp_path / "blender.tar.gz", **blender_tree())

    def boom(*args, **kwargs):
        assert len(list(private_tempdir.iterdir())) == 1
        raise KeyboardInterrupt

    monkeypatch.setattr(portable, "_save_icon", boom)
    with pytest.raises(KeyboardInterrupt):
        inspect_portable(archive)
    assert list(private_tempdir.iterdir()) == []


def test_documented_fields_only():
    """asdict()/JSON output of a PortableInfo stays the documented fields (+ the three extras)."""
    names = list(PortableInfo.__dataclass_fields__)
    assert names == [
        "path", "size", "sha256", "app_id", "name", "display_name", "version", "comment", "categories",
        "terminal", "strip_prefix", "desktop_entry", "desktop_filename", "executables", "executable",
        "icon_path", "icon_info", "is_electron", "arch", "tree_size", "file_count", "work_dir", "warnings",
        "exec_has_no_sandbox", "wm_class", "origin_url"]
    assert list(ExecutableCandidate.__dataclass_fields__) == ["relpath", "kind", "score"]
    with pytest.raises(AttributeError):
        ExecutableCandidate("a", "elf", 1).score = 2  # type: ignore[misc]


@pytest.mark.parametrize("suffix", [".tar.gz", ".zip"])
def test_progress_and_hash(tmp_path, suffix):
    files = {f"app/data/file{i:04d}.txt": f"{i}" for i in range(1500)}
    files["app/app"] = fake_elf()
    archive = make_archive(tmp_path / f"app-1.0{suffix}", files, modes={"app/app": 0o755})
    before = archive.stat()
    calls: list[tuple[float | None, str]] = []
    with inspect_portable(archive, progress=lambda fraction, text: calls.append((fraction, text))) as info:
        assert info.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
        assert (info.tree_size, info.file_count) == (256 + sum(len(str(i)) for i in range(1500)), 1501)
    assert calls[0] == (0.0, "Checking the file…")
    assert calls[1] == (None, "Reading app information…")
    assert calls[-1] == (1.0, "Done")
    assert (None, "Looking for the app icon…") in calls
    assert all(f is None or 0.0 <= f <= 1.0 for f, _text in calls)
    assert all(isinstance(text, str) and text for _f, text in calls)
    scanning = [f for f, text in calls[2:] if text == "Reading app information…"]
    if suffix != ".zip":        # a tar is streamed: the list grows while the file is read
        assert len(scanning) >= 2 and all(f is not None for f in scanning)
        assert scanning == sorted(scanning)
    with inspect_portable(archive, compute_hash=False) as info:
        assert info.sha256 is None
    after = archive.stat()
    assert (before.st_mtime_ns, before.st_size, before.st_mode) == (after.st_mtime_ns, after.st_size, after.st_mode)


def test_inspect_resolves_the_path(tmp_path, inspect, monkeypatch):
    archive = make_archive(tmp_path / "tool-1.0.zip", APP, modes=APP_MODES)
    link = tmp_path / "link-2.0.zip"
    link.symlink_to(archive)
    monkeypatch.chdir(tmp_path)
    assert inspect("link-2.0.zip").path == archive.resolve()
    assert inspect(str(link)).version == "1.0"


# =============================================================================================
# Files that are not (good) archives
# =============================================================================================

def test_friendly_errors_for_files_that_are_no_archives(tmp_path, private_tempdir):
    (tmp_path / "folder.zip").mkdir()
    (tmp_path / "empty.zip").write_bytes(b"")
    (tmp_path / "text.zip").write_text("hello " * 200)
    loop = tmp_path / "loop.zip"
    loop.symlink_to(loop)
    cases = {
        "missing.zip": "The file could not be found.",
        "folder.zip": "This is a folder, not an app file.",
        "empty.zip": "This file is empty. The download may have failed — try downloading it again.",
        "text.zip": NOT_AN_ARCHIVE,
        "loop.zip": "The file could not be found.",
    }
    for name, message in cases.items():
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(tmp_path / name)
        assert str(excinfo.value) == message, name
        assert isinstance(excinfo.value, EasyInstallerError)
    assert list(private_tempdir.iterdir()) == []


def test_compressed_file_that_is_not_a_tar(tmp_path):
    import bz2
    import gzip
    import lzma
    for suffix, compress in ((".tar.gz", gzip.compress), (".tar.xz", lzma.compress), (".tar.bz2", bz2.compress)):
        path = tmp_path / f"notes{suffix}"
        path.write_bytes(compress(b"Just some notes, compressed.\n" * 100))
        assert is_portable_archive(path)        # looks like one from the outside ...
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(path)
        assert str(excinfo.value) == NOT_AN_ARCHIVE, suffix


@pytest.mark.parametrize("suffix", ARCHIVE_SUFFIXES)
def test_truncated_download(tmp_path, private_tempdir, sandbox, suffix):
    apps, _outside = sandbox
    tree = electron_tree()
    tree["files"]["MyApp-linux-x64/resources/noise.bin"] = os.urandom(200_000)
    archive = make_archive(tmp_path / f"MyApp{suffix}", **tree)
    data = archive.read_bytes()
    for keep in (len(data) * 2 // 3, 700, 20):
        broken = tmp_path / f"cut-{keep}{suffix}"
        broken.write_bytes(data[:keep])
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(broken)
        assert str(excinfo.value) in (DAMAGED, NOT_AN_ARCHIVE), (suffix, keep)
        assert excinfo.value.details
        # unpacking a file that broke after it was looked at: nothing stays behind
        with pytest.raises(ArchiveError):
            extract_portable(info_for(broken, executable="my-app", strip_prefix="MyApp-linux-x64"), apps / "MyApp")
        assert list(apps.iterdir()) == []
    assert list(private_tempdir.iterdir()) == []


def noise(size: int, seed: int = 1) -> bytes:
    """Incompressible but reproducible bytes."""
    return random.Random(seed).randbytes(size)


@pytest.mark.parametrize("suffix", [".tar.gz", ".tar.xz", ".tar.bz2", ".zip"])
def test_flipped_bits_inside_a_file_are_noticed(tmp_path, sandbox, suffix):
    """A download with a damaged spot in the middle: the archive still opens and every header
    is fine - only the checksums (at the very end of a gzip stream) can tell."""
    apps, _outside = sandbox
    files = {"tool/tool": fake_elf(), "tool/data/noise.bin": noise(400_000), "tool/zz-last.txt": "the end"}
    archive = make_archive(tmp_path / f"tool-1.0{suffix}", files, modes={"tool/tool": 0o755})
    data = bytearray(archive.read_bytes())
    middle = len(data) // 2
    data[middle: middle + 64] = bytes(b ^ 0xFF for b in data[middle: middle + 64])
    archive.write_bytes(bytes(data))
    if suffix == ".zip":
        # a zip is not read through when it is looked at; unpacking checks every file
        with inspect_portable(archive, compute_hash=False) as info:
            assert info.executable == "tool"
    else:
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(archive, compute_hash=False)
        assert str(excinfo.value) == DAMAGED
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive, executable="tool", strip_prefix="tool"), apps / "tool")
    assert str(excinfo.value) == DAMAGED
    assert list(apps.iterdir()) == []


def test_damaged_tar_header_does_not_end_the_member_list_silently(tmp_path, sandbox):
    """tarfile takes an unreadable header for the end of the archive."""
    apps, _outside = sandbox
    files = {"tool/tool": fake_elf(), "tool/a.txt": "a" * 600, "tool/b.txt": "b" * 600, "tool/c.txt": "c"}
    archive = make_tar(tmp_path / "tool.tar", files, modes={"tool/tool": 0o755})
    with tarfile.open(archive) as tar:
        offsets = {member.name: member.offset for member in tar}
    data = bytearray(archive.read_bytes())
    at = offsets["tool/b.txt"]
    for damage in (b"\xff" * 512, bytes(data[at:at + 100]) + b"garbage!" + bytes(data[at + 108:at + 512])):
        broken = bytearray(data)
        broken[at:at + 512] = damage
        archive.write_bytes(bytes(broken))
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(archive)
        assert str(excinfo.value) == DAMAGED
        assert "tar header" in excinfo.value.details
        with pytest.raises(ArchiveError):
            extract_portable(info_for(archive, executable="tool", strip_prefix="tool"), apps / "tool")
        assert list(apps.iterdir()) == []


def test_tar_end_marker_and_padding_are_not_required(tmp_path, inspect, sandbox):
    apps, _outside = sandbox
    files = {"tool/tool": fake_elf(), "tool/a.txt": "a" * 600}
    archive = make_tar(tmp_path / "tool.tar", files, modes={"tool/tool": 0o755})
    data = archive.read_bytes()
    body = data.rstrip(b"\0")
    body += bytes(-len(body) % 512)
    for name, content in {"no-marker.tar": body, "one-block.tar": body + bytes(512),
                          "junk-after.tar": data + b"signature or whatever " * 50}.items():
        variant = tmp_path / name
        variant.write_bytes(content)
        info = inspect(variant)
        assert (info.executable, info.file_count) == ("tool", 2), name
        extract_portable(info, apps / name)
        assert sorted(snapshot(apps / name)) == ["a.txt", "tool"]


def test_password_protected_zip(tmp_path, sandbox):
    apps, _outside = sandbox
    archive = make_zip(tmp_path / "secret.zip", entries=[file_entry("app", fake_elf(), 0o755, encrypted=True)])
    message = "This archive is protected with a password. Easy Installer cannot open it."
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == message
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "secret")
    assert str(excinfo.value) == message
    assert list(apps.iterdir()) == []


def test_unsupported_zip_compression(tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "new.zip", APP, modes=APP_MODES)

    def unsupported(self, *args, **kwargs):
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(zipfile.ZipFile, "open", unsupported)
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == "This archive is packed in a way that Easy Installer cannot read yet."
    assert "NotImplementedError" in excinfo.value.details


# =============================================================================================
# Unpacking
# =============================================================================================

@pytest.mark.parametrize("suffix", ARCHIVE_SUFFIXES)
def test_extract(tmp_path, sandbox, inspect, suffix):
    apps, outside = sandbox
    tree = electron_tree()
    top = "MyApp-linux-x64"
    tree["files"][f"{top}/resources/noise.bin"] = os.urandom(3_500_000)
    tree["symlinks"][f"{top}/my-app-link"] = "my-app"
    tree["symlinks"][f"{top}/locales/default.pak"] = "en-US.pak"
    tree["symlinks"][f"{top}/resources/up"] = "../locales"
    archive = make_archive(tmp_path / f"MyApp-1.2.3{suffix}", dirs=[f"{top}/empty/nested"], **tree)
    info = inspect(archive)
    dest = apps / ".MyApp.easyinstaller-new-abc123"
    calls: list[tuple[float | None, str]] = []
    assert extract_portable(info, dest, progress=lambda f, text: calls.append((f, text))) is None

    found = snapshot(dest)
    expected: dict[str, object] = {name[len(top) + 1:]: data if isinstance(data, bytes) else data.encode()
                                   for name, data in tree["files"].items()}
    expected.update({"my-app-link": ("link", "my-app"), "locales/default.pak": ("link", "en-US.pak"),
                     "resources/up": ("link", "../locales")})
    expected.update({"locales": "dir", "resources": "dir", "empty": "dir", "empty/nested": "dir"})
    assert found == expected                # incl.: the absolute link "MyApp" was left out
    for name, mode in tree["modes"].items():
        assert mode_of(dest / name[len(top) + 1:]) == 0o755, name
    assert mode_of(dest / "icudtl.dat") == 0o644 and mode_of(dest / "resources/app.asar") == 0o644
    assert mode_of(dest) == 0o755 and mode_of(dest / "empty/nested") == 0o755
    assert_links_stay_inside(dest)

    assert calls[0] == (0.0, "Unpacking the app…") and calls[-1] == (1.0, "Done")
    fractions = [f for f, _text in calls]
    assert all(f is not None and 0.0 <= f <= 1.0 for f in fractions) and fractions == sorted(fractions)
    assert len(calls) >= 5
    assert {text for _f, text in calls[:-1]} == {"Unpacking the app…"}
    assert sorted(p.name for p in apps.iterdir()) == [dest.name]
    assert snapshot(outside) == {"sentinel": b"untouched"}


@pytest.mark.parametrize("tree, archive_name, executable", [
    (jetbrains_tree(), "ideaIU-2024.1.4.tar.gz", "bin/idea.sh"),
    (firefox_tree(), "firefox-128.0.3.tar.xz", "firefox"),
    (blender_tree(), "blender-4.2.0-linux-x64.tar.xz", "blender"),
    (flat_tree(), "UVtools_linux-x64_v6.2.0.zip", "UVtools.sh"),
])
def test_extract_realistic_layouts(tmp_path, sandbox, inspect, tree, archive_name, executable):
    apps, _outside = sandbox
    info = inspect(make_archive(tmp_path / archive_name, **tree))
    dest = apps / "App"
    extract_portable(info, dest)
    prefix = len(info.strip_prefix) + 1 if info.strip_prefix else 0
    assert sorted(p for p, kind in snapshot(dest).items() if kind != "dir") == sorted(
        name[prefix:] for name in tree["files"])
    program = dest / executable
    assert program.is_file() and mode_of(program) == 0o755
    for name, mode in tree["modes"].items():
        assert mode_of(dest / name[prefix:]) == 0o755, name       # 0777 of the zip became 0755


def test_permissions_never_include_dangerous_bits(tmp_path, sandbox):
    apps, _outside = sandbox
    modes = {"app": 0o4755, "setgid": 0o2755, "sticky": 0o1777, "world": 0o666, "secret": 0o600, "odd": 0o070,
             "readonly": 0o444, "none": 0o000, "exec-group-only": 0o654, "all": 0o7777}
    expected = {"app": 0o755, "setgid": 0o755, "sticky": 0o755, "world": 0o644, "secret": 0o600, "odd": 0o640,
                "readonly": 0o644, "none": 0o600, "exec-group-only": 0o644, "all": 0o755}
    for suffix in (".tar", ".tar.gz", ".zip"):
        entries = [dir_entry("private", 0o700), dir_entry("sticky-dir", 0o1777), dir_entry("locked", 0o000),
                   file_entry("locked/inside", b"x", 0o644)]
        archive = make_archive(tmp_path / f"modes{suffix}", {name: fake_elf() for name in modes}, modes=modes,
                               entries=entries)
        dest = apps / f"modes{suffix}"
        old_umask = os.umask(0o077)         # the user's umask does not change the result
        try:
            extract_portable(info_for(archive), dest)
        finally:
            os.umask(old_umask)
        found = {name: mode_of(dest / name) for name in modes}
        if suffix == ".zip":
            expected_here = dict(expected, none=0o755)      # mode 0 in a zip means "no information"
        else:
            expected_here = expected
        assert found == expected_here, suffix
        for folder in ("private", "sticky-dir", "locked"):
            assert mode_of(dest / folder) == 0o755
        assert (dest / "locked/inside").read_bytes() == b"x"
        assert mode_of(dest) == 0o755
        assert all(os.lstat(p).st_uid == os.getuid() for p in dest.rglob("*"))


def test_zip_made_on_windows_has_no_permissions(tmp_path, sandbox, inspect):
    apps, _outside = sandbox
    files = {"Tool/tool": fake_elf(), "Tool/run.sh": fake_script(), "Tool/data.bin": b"\0" * 10,
             "Tool/readme.txt": "Read me first.", "Tool/lib/x.so": fake_elf()}
    expected = {"tool": 0o755, "run.sh": 0o755, "data.bin": 0o644, "readme.txt": 0o644, "lib/x.so": 0o755,
                "empty": 0o755}
    # made on Windows (no modes at all), or by a tool that stores 0600 / 0644 for every file
    variants = {
        "dos": make_zip(tmp_path / "dos" / "Tool-1.0.zip", files, dirs=["Tool/empty"], unix=False),
        "0600": make_zip(tmp_path / "0600" / "Tool-1.0.zip", files, modes={n: 0o600 for n in files},
                         dirs=["Tool/empty"]),
        "0644": make_zip(tmp_path / "0644" / "Tool-1.0.zip", files, dirs=["Tool/empty"]),
    }
    for name, archive in variants.items():
        with zipfile.ZipFile(archive) as raw:
            assert not any((i.external_attr >> 16) & 0o111 for i in raw.infolist() if not i.is_dir())
        info = inspect(archive)
        assert scores(info) == {"tool": 72, "run.sh": 30}, name
        dest = apps / name
        extract_portable(info, dest)
        assert {p: mode_of(dest / p) for p in expected} == expected, name


def test_tar_without_any_exec_bit(tmp_path, sandbox, inspect):
    """Packed on a system that knows no exec bits: the program is found by what it is."""
    apps, _outside = sandbox
    files = {"tool/tool.bin": fake_elf(), "tool/helper": fake_elf(), "tool/README": "read me"}
    info = inspect(make_tar(tmp_path / "tool-1.0.tar.gz", files))
    assert relpaths(info) == ["tool.bin", "helper"]
    extract_portable(info, apps / "tool")
    assert mode_of(apps / "tool" / "tool.bin") == 0o755
    assert mode_of(apps / "tool" / "helper") == 0o644        # a tar's modes are taken as they are


def test_chosen_program_always_gets_its_exec_bit(tmp_path, sandbox, inspect):
    apps, _outside = sandbox
    files = {"tool": fake_elf(), "alt.sh": fake_script()}
    archive = make_tar(tmp_path / "tool.tar.gz", files, modes={"tool": 0o755, "alt.sh": 0o640})
    info = inspect(archive)
    assert relpaths(info) == ["tool"]
    info.executable = "alt.sh"              # the user picked another file
    extract_portable(info, apps / "tool")
    assert mode_of(apps / "tool" / "alt.sh") == 0o750
    assert mode_of(apps / "tool" / "tool") == 0o755


@pytest.mark.parametrize("executable", ["missing", "", "../tool", "/bin/sh", "folder", "dangling", "outside-link"])
def test_missing_program_fails_and_cleans_up(tmp_path, sandbox, executable):
    apps, outside = sandbox
    archive = make_tar(tmp_path / "tool.tar", {"tool": fake_elf(), "folder/x": b"1"},
                       {"dangling": "nothing-here", "outside-link": str(outside / "sentinel")}, {"tool": 0o755})
    before = mode_of(outside / "sentinel")
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive, executable=executable), apps / "tool")
    assert str(excinfo.value) == NO_PROGRAM
    assert list(apps.iterdir()) == []
    assert mode_of(outside / "sentinel") == before and not before & 0o111


def test_destination_rules(tmp_path, sandbox):
    apps, _outside = sandbox
    archive = make_zip(tmp_path / "tool.zip", {"app": fake_elf(), "data/x": b"1"}, modes=APP_MODES)
    info = info_for(archive)

    existing = apps / "existing"            # an empty folder is used ...
    existing.mkdir(mode=0o700)
    extract_portable(info, existing)
    assert sorted(snapshot(existing)) == ["app", "data", "data/x"]

    with pytest.raises(InstallError) as excinfo:        # ... a folder with content is not
        extract_portable(info, existing)
    assert str(excinfo.value) == FOLDER_EXISTS
    assert sorted(snapshot(existing)) == ["app", "data", "data/x"]

    (apps / "real").mkdir()
    (apps / "link").symlink_to(apps / "real")
    (apps / "file").write_text("x")
    (apps / "dangling").symlink_to(apps / "nowhere")
    for name in ("link", "file", "dangling"):
        with pytest.raises(InstallError) as excinfo:
            extract_portable(info, apps / name)
        assert str(excinfo.value) == FOLDER_EXISTS, name
    assert list((apps / "real").iterdir()) == [] and not (apps / "nowhere").exists()

    with pytest.raises(InstallError):                   # the parent must exist
        extract_portable(info, apps / "no" / "such" / "parent")
    assert not (apps / "no").exists()
    extract_portable(info, str(apps / "as-string"))     # str paths work too
    assert (apps / "as-string" / "app").is_file()


def test_failure_empties_a_folder_that_existed_before(tmp_path, sandbox):
    apps, _outside = sandbox
    archive = make_tar(tmp_path / "bad.tar", entries=[
        file_entry("app", fake_elf(), 0o755), dir_entry("sub"), file_entry("sub/file", b"data"),
        symlink_entry("sub/link", "file"), special_entry("sub/pipe", "fifo")])
    given = apps / "given"
    given.mkdir()
    with pytest.raises(ArchiveError):
        extract_portable(info_for(archive), given)
    assert given.is_dir() and list(given.iterdir()) == []
    with pytest.raises(ArchiveError):
        extract_portable(info_for(archive), apps / "created")
    assert sorted(p.name for p in apps.iterdir()) == ["given"]


def test_archive_changed_after_inspection(tmp_path, sandbox, inspect):
    apps, _outside = sandbox
    archive = make_zip(tmp_path / "tool.zip", APP, modes=APP_MODES)
    info = inspect(archive)
    make_zip(archive, {"app": fake_elf(), "extra": b"more" * 100}, modes=APP_MODES)
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info, apps / "tool")
    assert str(excinfo.value) == "The archive was changed after it was opened. Please try again."
    archive.unlink()
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info, apps / "tool")
    assert str(excinfo.value) == "The file could not be found."
    assert list(apps.iterdir()) == []

    # same size, but no longer inside the folder that inspection found
    first = make_tar(tmp_path / "a.tar", {"one/app": fake_elf()}, modes={"one/app": 0o755})
    info = inspect(first)
    make_tar(first, {"two/app": fake_elf()}, modes={"two/app": 0o755})
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info, apps / "tool")
    assert str(excinfo.value) == "The archive was changed after it was opened. Please try again."
    assert list(apps.iterdir()) == []


def test_an_archive_of_the_same_size_but_other_content_is_not_unpacked(tmp_path, sandbox,
                                                                         inspect):
    """SEC-2: what is unpacked (and recorded with the inspected sha256) is what was inspected."""
    apps, _outside = sandbox
    archive = make_tar(tmp_path / "Tool-1.0.tar", {"Tool/Tool": fake_elf(size=4096),
                                                   "Tool/data.txt": b"A" * 100},
                       modes={"Tool/Tool": 0o755})
    info = inspect(archive, compute_hash=True)
    other = make_tar(tmp_path / "other.tar", {"Tool/Tool": fake_elf(size=4096),
                                              "Tool/data.txt": b"B" * 100},
                     modes={"Tool/Tool": 0o755})
    assert other.stat().st_size == archive.stat().st_size
    os.replace(other, archive)
    with pytest.raises(ArchiveError, match="changed after it was opened"):
        extract_portable(info, apps / "Tool")
    assert list(apps.iterdir()) == []
    # unchanged: unpacked as usual
    info = inspect(archive, compute_hash=True)
    extract_portable(info, apps / "Tool")
    assert (apps / "Tool" / "data.txt").read_bytes() == b"B" * 100


def test_disk_full(tmp_path, sandbox, monkeypatch):
    apps, _outside = sandbox
    archive = make_zip(tmp_path / "tool.zip", {"app": fake_elf(), "big": os.urandom(100_000)}, modes=APP_MODES)
    message = "There is not enough free disk space. Please free up some space and try again."

    usage = shutil.disk_usage(apps)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage._replace(free=1000))
    with pytest.raises(InstallError) as excinfo:
        extract_portable(info_for(archive, tree_size=100_256), apps / "tool")
    assert str(excinfo.value) == message
    assert list(apps.iterdir()) == []
    monkeypatch.undo()

    real_write = os.write
    written = []

    def failing_write(fd, data):
        written.append(len(data))
        if len(written) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(InstallError) as excinfo:
        extract_portable(info_for(archive), apps / "tool")
    assert str(excinfo.value) == message
    assert "ENOSPC" in excinfo.value.details or "No space left" in excinfo.value.details
    monkeypatch.undo()
    assert list(apps.iterdir()) == []


def test_interrupt_while_unpacking_cleans_up(tmp_path, sandbox):
    apps, _outside = sandbox
    files = {"app": fake_elf(), **{f"data/{i}.bin": os.urandom(300_000) for i in range(6)}}
    archive = make_tar(tmp_path / "tool.tar.gz", files, modes=APP_MODES)

    def interrupt(fraction, text):
        if fraction and fraction > 0.3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        extract_portable(info_for(archive, tree_size=1_800_256), apps / "tool", progress=interrupt)
    assert list(apps.iterdir()) == []


def test_marker_file_of_the_installer_cannot_be_planted(tmp_path, sandbox, inspect):
    apps, _outside = sandbox
    assert MARKER_NAME == ".easy-installer.json"
    files = {"top/app": fake_elf(), f"top/{MARKER_NAME}": '{"id": "someone-else"}', f"top/sub/{MARKER_NAME}": "{}"}
    for suffix in (".tar", ".zip"):
        archive = make_archive(tmp_path / f"planted{suffix}", files, modes={"top/app": 0o755})
        info = inspect(archive)
        assert info.file_count == 2
        extract_portable(info, apps / f"planted{suffix}")
        assert sorted(p for p, kind in snapshot(apps / f"planted{suffix}").items() if kind != "dir") == [
            "app", f"sub/{MARKER_NAME}"]
    archive = make_tar(tmp_path / "planted-link.tar", {"app": fake_elf()}, {MARKER_NAME: "app"}, APP_MODES)
    extract_portable(info_for(archive), apps / "link")
    assert sorted(snapshot(apps / "link")) == ["app"]


def test_duplicate_members(tmp_path, sandbox):
    apps, _outside = sandbox
    for suffix in (".tar", ".zip"):
        archive = make_archive(tmp_path / f"dup{suffix}", entries=[
            file_entry("app", b"first", 0o644), file_entry("app", fake_elf(), 0o755),
            dir_entry("d"), dir_entry("d"), file_entry("d/x", b"1"), file_entry("d/x", b"22")])
        dest = apps / f"dup{suffix}"
        extract_portable(info_for(archive), dest)
        assert (dest / "app").read_bytes() == fake_elf() and (dest / "d/x").read_bytes() == b"22"

        for entries in ([file_entry("app", fake_elf(), 0o755), file_entry("x", b"file"), file_entry("x/y", b"below")],
                        [file_entry("app", fake_elf(), 0o755), file_entry("x/y", b"below"), file_entry("x", b"file")],
                        [file_entry("app", fake_elf(), 0o755), file_entry("x", b"file"), dir_entry("x")]):
            clash = make_archive(tmp_path / f"clash{suffix}", entries=entries)
            with pytest.raises(ArchiveError) as excinfo:
                extract_portable(info_for(clash), apps / "clash")
            assert str(excinfo.value) == UNSAFE
            assert not (apps / "clash").exists()


# =============================================================================================
# Hostile archives
# =============================================================================================

def hostile_names(outside: Path) -> list[str]:
    return [f"{outside}/evil", "../outside/evil", "app-dir/../../outside/evil", "..", "a/../b",
            "bad\nname", "bad\x1bname", "tab\tname", "x" * 300, "/".join(["d" * 200] * 20)]


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".zip"])
def test_member_names_that_leave_the_archive_are_refused(tmp_path, sandbox, private_tempdir, suffix):
    apps, outside = sandbox
    for index, name in enumerate(hostile_names(outside)):
        archive = make_archive(tmp_path / f"evil{index}{suffix}", entries=[
            file_entry("app", fake_elf(), 0o755), file_entry(name, b"pwned", 0o755)])
        with pytest.raises(ArchiveError) as excinfo:
            inspect_portable(archive)
        assert str(excinfo.value) == UNSAFE, name
        with pytest.raises(ArchiveError) as excinfo:
            extract_portable(info_for(archive), apps / "evil")
        assert str(excinfo.value) == UNSAFE, name
        assert list(apps.iterdir()) == []
        assert snapshot(outside) == {"sentinel": b"untouched"}
        # also as a folder or a link of that name
        for entry in (dir_entry(name), symlink_entry(name, "app")):
            archive = make_archive(tmp_path / f"evil{index}b{suffix}",
                                   entries=[file_entry("app", fake_elf(), 0o755), entry])
            with pytest.raises(ArchiveError):
                extract_portable(info_for(archive), apps / "evil")
            assert list(apps.iterdir()) == []
            assert snapshot(outside) == {"sentinel": b"untouched"}
    assert list(private_tempdir.iterdir()) == []


@pytest.mark.parametrize("kind", ["fifo", "chardev", "blockdev"])
@pytest.mark.parametrize("suffix", [".tar", ".tar.xz", ".zip"])
def test_devices_and_pipes_are_refused(tmp_path, sandbox, suffix, kind):
    apps, _outside = sandbox
    archive = make_archive(tmp_path / f"special{suffix}", entries=[
        file_entry("app", fake_elf(), 0o755), special_entry("dev/thing", kind)])
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == UNSAFE
    assert "device or pipe" in excinfo.value.details
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "special")
    assert str(excinfo.value) == UNSAFE
    assert list(apps.iterdir()) == []


@pytest.mark.parametrize("links_first", [False, True])
@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".zip"])
def test_symlinks_never_lead_out_of_the_app_folder(tmp_path, sandbox, inspect, suffix, links_first):
    apps, outside = sandbox
    files = {"app": fake_elf(), "lib/real.so": fake_elf(), "lib/sub/data": b"data"}
    symlinks = {
        # fine: inside the folder, also when dangling or pointing at the top
        "lib/alias.so": "real.so", "lib/sub/up": "..", "lib/sub/root": "../..", "dangling": "lib/nothing",
        "lib/sub/chain": "up/alias.so", "via-dot": "./lib/../app",
        # not fine
        "abs": str(outside), "abs-file": str(outside / "sentinel"), "etc": "/etc/passwd",
        "dotdot": "../outside", "deep": "lib/../../outside/sentinel", "lib/sub/escape": "../../../outside",
        "through": "lib/sub/root/../outside",            # harmless on paper, leaves through "root"
        "through2": "lib/sub/up/../../outside/sentinel",
        "via-bad": "dotdot/sentinel",                    # needs a link that is left out
        "loop-a": "loop-b", "loop-b": "loop-a", "self": "self",
        "empty": "", "long": "lib/" + "x/" * 600 + "y",
    }
    if suffix == ".zip":
        del symlinks["empty"]       # a zip member without content is a file
    good = {"lib/alias.so", "lib/sub/up", "lib/sub/root", "dangling", "lib/sub/chain", "via-dot"}
    archive = make_archive(tmp_path / f"links{suffix}", files, symlinks, APP_MODES, links_first=links_first)
    info = inspect(archive)
    assert info.executable == "app"
    dest = apps / "links"
    extract_portable(info, dest)
    found = snapshot(dest)
    assert {p for p, kind in found.items() if isinstance(kind, tuple)} == good
    assert found["lib/sub/chain"] == ("link", "up/alias.so")         # targets are kept as they are
    assert (dest / "lib/sub/chain").read_bytes() == fake_elf()
    assert_links_stay_inside(dest)
    assert snapshot(outside) == {"sentinel": b"untouched"}
    assert sorted(p.name for p in apps.iterdir()) == ["links"]


@pytest.mark.parametrize("links_first", [False, True])
@pytest.mark.parametrize("suffix", [".tar", ".tar.bz2", ".zip"])
def test_files_are_never_written_through_links(tmp_path, sandbox, suffix, links_first):
    """The classic: a link "out" to somewhere else, then a file "out/pwned"."""
    apps, outside = sandbox
    target = str(outside)
    for link_target in (target, "../../outside", "inside"):
        link = [symlink_entry("out", link_target), symlink_entry("nested/out2", link_target)]
        files = [file_entry("app", fake_elf(), 0o755), dir_entry("inside"), file_entry("out/pwned", b"pwned"),
                 file_entry("nested/out2/deeper/pwned", b"pwned"), symlink_entry("out/link-below", "pwned")]
        archive = make_archive(tmp_path / f"through{suffix}", entries=link + files if links_first else files + link)
        dest = apps / "through"
        extract_portable(info_for(archive), dest)
        found = snapshot(dest)
        assert found["out"] == "dir" and found["out/pwned"] == b"pwned"         # a real folder instead
        assert found["nested/out2/deeper/pwned"] == b"pwned"
        assert found["out/link-below"] == ("link", "pwned")
        assert found["inside"] == "dir" and "inside/pwned" not in found
        assert_links_stay_inside(dest)
        assert snapshot(outside) == {"sentinel": b"untouched"}
        shutil.rmtree(dest)


def test_hard_links(tmp_path, sandbox, inspect):
    apps, outside = sandbox
    entries = [
        file_entry("top/app", fake_elf(), 0o755), file_entry("top/data/original", b"original"),
        hardlink_entry("top/data/twin", "top/data/original"),
        hardlink_entry("top/app2", "top/app"),
        hardlink_entry("top/passwd", "/etc/passwd"),
        hardlink_entry("top/sentinel", str(outside / "sentinel")),
        hardlink_entry("top/escape", "top/../../outside/sentinel"),
        hardlink_entry("top/forward", "top/later"),
        hardlink_entry("top/other-top", "elsewhere/file"),
        hardlink_entry("top/self", "top/self"),
        symlink_entry("top/link", "app"), hardlink_entry("top/link-twin", "top/link"),
        file_entry("top/later", b"later"),
        file_entry("top/data/twin", b"replaced"),       # must not write into "original"
    ]
    archive = make_tar(tmp_path / "hard.tar.gz", entries=entries)
    info = inspect(archive)
    assert info.strip_prefix == "top"
    assert relpaths(info) == ["app", "app2"]
    dest = apps / "hard"
    extract_portable(info, dest)
    found = snapshot(dest)
    assert sorted(found) == ["app", "app2", "data", "data/original", "data/twin", "later", "link"]
    assert found["data/original"] == b"original" and found["data/twin"] == b"replaced"
    assert os.path.samefile(dest / "app", dest / "app2")
    assert mode_of(dest / "app2") == 0o755
    assert snapshot(outside) == {"sentinel": b"untouched"}
    assert os.stat(outside / "sentinel").st_nlink == 1


def test_hard_link_is_copied_when_linking_is_not_possible(tmp_path, sandbox, monkeypatch):
    apps, _outside = sandbox
    archive = make_tar(tmp_path / "hard.tar", entries=[
        file_entry("app", fake_elf(), 0o755), hardlink_entry("bin/app-copy", "app")])

    def no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    extract_portable(info_for(archive), apps / "hard")
    copy = apps / "hard" / "bin" / "app-copy"
    assert copy.read_bytes() == fake_elf() and mode_of(copy) == 0o755
    assert not os.path.samefile(copy, apps / "hard" / "app")


def test_zip_symlinks_come_from_external_attr(tmp_path, sandbox, inspect):
    apps, outside = sandbox
    archive = make_zip(tmp_path / "links.zip", {"app": fake_elf()}, {"inside": "app", "outside": str(outside)},
                       APP_MODES)
    with zipfile.ZipFile(archive) as raw:
        attrs = {i.filename: i.external_attr >> 16 for i in raw.infolist()}
    assert stat.S_ISLNK(attrs["inside"]) and stat.S_ISREG(attrs["app"]) and attrs["app"] & 0o777 == 0o755
    info = inspect(archive)
    assert info.file_count == 3
    extract_portable(info, apps / "links")
    assert snapshot(apps / "links") == {"app": fake_elf(), "inside": ("link", "app")}


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".zip"])
def test_huge_declared_sizes(tmp_path, sandbox, private_tempdir, suffix):
    """A few bytes that claim to be 100 GiB: refused from the header, before any data is read."""
    apps, _outside = sandbox
    archive = make_archive(tmp_path / f"bomb{suffix}", entries=[
        file_entry("app", fake_elf(), 0o755), file_entry("bomb.bin", b"tiny", declared_size=100 * 1024 ** 3)])
    assert archive.stat().st_size < 20_000
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == TOO_LARGE
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "bomb")
    assert str(excinfo.value) == TOO_LARGE
    assert list(apps.iterdir()) == [] and list(private_tempdir.iterdir()) == []


@pytest.mark.parametrize("suffix", [".tar", ".tar.xz", ".zip"])
def test_limits_count_what_is_really_there(tmp_path, sandbox, monkeypatch, inspect, suffix):
    apps, _outside = sandbox
    files = {"app": fake_elf(), **{f"data/{i}": bytes(1000) for i in range(20)}}
    archive = make_archive(tmp_path / f"many{suffix}", files, modes=APP_MODES)
    info = inspect(archive)

    monkeypatch.setattr(portable, "MAX_TOTAL_SIZE", 10_000)
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == TOO_LARGE
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info, apps / "many")
    assert str(excinfo.value) == TOO_LARGE
    assert list(apps.iterdir()) == []

    monkeypatch.setattr(portable, "MAX_TOTAL_SIZE", 8 * 1024 ** 3)
    monkeypatch.setattr(portable, "MAX_ENTRIES", 10)
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) == TOO_LARGE
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info, apps / "many")
    assert str(excinfo.value) == TOO_LARGE
    assert list(apps.iterdir()) == []


def test_zip_members_that_lie_about_their_size(tmp_path, sandbox):
    apps, _outside = sandbox
    for declared in (10, 100_000):
        archive = make_zip(tmp_path / f"lie{declared}.zip", entries=[
            file_entry("app", fake_elf(), 0o755), file_entry("data.bin", bytes(5000), declared_size=declared)])
        with pytest.raises(ArchiveError) as excinfo:
            extract_portable(info_for(archive), apps / "lie")
        assert str(excinfo.value) == DAMAGED
        assert list(apps.iterdir()) == []


def test_tar_member_that_ends_early(tmp_path, sandbox):
    apps, _outside = sandbox
    archive = make_tar(tmp_path / "short.tar", entries=[
        file_entry("app", fake_elf(), 0o755), file_entry("data.bin", b"only this", declared_size=50_000)])
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "short")
    assert str(excinfo.value) == DAMAGED
    assert list(apps.iterdir()) == []


def test_tar_headers_cannot_ask_for_gigabytes_of_memory():
    import io
    stream = portable._GuardedStream(io.BytesIO(b"x" * 100))
    assert stream.read(10) == b"x" * 10 and stream.tell() == 10
    assert stream.seek(0) == 0
    for size in (-1, None, portable._MAX_TAR_HEADER_READ + 1, 1 << 40):
        with pytest.raises(tarfile.ReadError):
            stream.read(size)
    with pytest.raises(tarfile.ReadError):
        stream.seek(-5)


def test_pax_header_bomb_is_refused(tmp_path):
    """A pax header that declares a huge size would be read into memory in one piece."""
    info = tarfile.TarInfo("././@PaxHeader")
    info.type = tarfile.XHDTYPE
    info.size = 6 * 1024 ** 3
    archive = tmp_path / "pax.tar"
    archive.write_bytes(info.tobuf(tarfile.GNU_FORMAT) + bytes(1024))
    assert is_portable_archive(archive)
    with pytest.raises(ArchiveError) as excinfo:
        inspect_portable(archive)
    assert str(excinfo.value) in (DAMAGED, NOT_AN_ARCHIVE)


def test_link_resolver():
    links = {"a/up": "..", "a/b": "c", "a/loop1": "loop2", "a/loop2": "loop1", "abs": "/etc", "a/out": "../../x"}
    resolver = portable._LinkResolver(links)
    assert resolver.resolve(["a"], "c/d") == ["a", "c", "d"]
    assert resolver.resolve(["a"], "b/d/../e") == ["a", "c", "e"]
    assert resolver.resolve(["a"], "up") == []
    assert resolver.resolve(["a"], "up/a/b") == ["a", "c"]
    assert resolver.resolve(["a"], "./up/./a//b/") == ["a", "c"]
    assert resolver.resolve(["a"], "up/..") is None
    assert resolver.resolve(["a"], "../..") is None
    assert resolver.resolve([], "abs/passwd") is None
    assert resolver.resolve([], "a/out") is None
    assert resolver.resolve([], "/etc") is None and resolver.resolve([], "") is None
    assert resolver.resolve(["a"], "loop1") is None
    assert resolver.resolve_path("a/b/x") == "a/c/x" and resolver.resolve_path("") == ""
    assert resolver.resolve_path("abs") is None
    chain = {f"l{i}": f"l{i + 1}" for i in range(60)}
    assert portable._LinkResolver(chain).resolve([], "l30") == ["l60"]
    assert portable._LinkResolver(chain).resolve([], "l0") is None        # more hops than the kernel allows
    assert portable._LinkResolver({}).resolve([], "x/" * 5000) == ["x"] * 5000
    assert portable._LinkResolver({}).resolve([], "x/" * 9000) is None      # absurdly long


def test_entangled_links_are_refused_instead_of_checked_forever(tmp_path, sandbox, monkeypatch):
    apps, _outside = sandbox
    symlinks = {f"l{i}": "/".join(["."] * 200 + [f"l{i + 1}"]) for i in range(30)}
    archive = make_tar(tmp_path / "tangle.tar", APP, symlinks, APP_MODES)
    extract_portable(info_for(archive), apps / "ok")
    assert len(snapshot(apps / "ok")) == 31
    monkeypatch.setattr(portable, "MAX_TOTAL_LINK_STEPS", 5000)
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "tangle")
    assert str(excinfo.value) == UNSAFE
    assert sorted(p.name for p in apps.iterdir()) == ["ok"]


def test_safe_relpath():
    assert portable._safe_relpath("./a//b/./c/") == "a/b/c"
    assert portable._safe_relpath("./") == "" and portable._safe_relpath("") == ""
    assert portable._safe_relpath("a..b/...") == "a..b/..."
    assert portable._safe_relpath("dir\\file") == "dir\\file"           # no separator on Linux
    assert portable._safe_relpath("Ünï/cödé ✓") == "Ünï/cödé ✓"
    for bad in ("/abs", "//abs", "..", "a/..", "../a", "a/../b", "a\0b", "a\nb", "\x7f", "x" * 256, "é" * 128,
                "\ud800", 5, None):
        with pytest.raises(ArchiveError):
            portable._safe_relpath(bad)  # type: ignore[arg-type]


def test_tar_data_filter_is_asked_too(tmp_path, sandbox, monkeypatch):
    """Defence in depth: tarfile's own ``data`` filter sees every member as well."""
    if not hasattr(tarfile, "data_filter"):
        pytest.skip("this Python has no tarfile extraction filters")
    apps, _outside = sandbox
    archive = make_tar(tmp_path / "plain.tar", {"app": fake_elf(), "lib/x": b"1"}, {"link": "app"}, APP_MODES)
    seen: list[tuple[str, str]] = []
    real_filter = tarfile.data_filter

    def watching(member, dest_path):
        seen.append((member.name, dest_path))
        return real_filter(member, dest_path)

    monkeypatch.setattr(tarfile, "data_filter", watching)
    extract_portable(info_for(archive), apps / "plain")
    assert seen == [(name, str(apps / "plain")) for name in ("app", "lib/x", "link")]

    def refusing(member, dest_path):
        raise tarfile.OutsideDestinationError(member, "/somewhere/else")

    monkeypatch.setattr(tarfile, "data_filter", refusing)
    with pytest.raises(ArchiveError) as excinfo:
        extract_portable(info_for(archive), apps / "refused")
    assert str(excinfo.value) == UNSAFE
    assert sorted(p.name for p in apps.iterdir()) == ["plain"]


# =============================================================================================
# real archives on this machine (read-only; they are never unpacked by the tests)
# =============================================================================================

EXPECTED_REAL = {
    REAL_ETCHER: dict(
        app_id="balenaetcher", name="balenaEtcher", version="2.1.4", strip_prefix="balenaEtcher-linux-x64",
        executable="balena-etcher", kind="elf", electron=True, file_count=80, tree_size=429938390,
        others=["resources/etcher-util", "chrome-sandbox", "chrome_crashpad_handler"]),
    REAL_UVTOOLS: dict(
        app_id="uvtools", name="UVtools", version="6.2.0", strip_prefix=None,
        executable="UVtools.sh", kind="script", electron=False, file_count=533, tree_size=280783402,
        others=["UVtools", "UVtoolsCmd", "createdump"]),
}


@pytest.mark.real
@pytest.mark.parametrize("archive", list(EXPECTED_REAL), ids=lambda p: p.name)
def test_real_archives(archive, private_tempdir):
    if not archive.is_file():
        pytest.skip(f"{archive.name} is not on this machine")
    expected = EXPECTED_REAL[archive]
    before = archive.stat()
    assert is_portable_archive(archive)
    with inspect_portable(archive, compute_hash=False) as info:
        assert info.path == archive.resolve() and info.size == before.st_size
        assert (info.app_id, info.name, info.display_name) == (expected["app_id"], expected["name"], expected["name"])
        assert info.version == expected["version"]
        assert info.strip_prefix == expected["strip_prefix"]
        assert info.executable == expected["executable"]
        assert info.executables[0].kind == expected["kind"] and info.executables[0].score >= 60
        assert relpaths(info)[1:len(expected["others"]) + 1] == expected["others"]
        assert not any(path.endswith((".so", ".dll")) for path in relpaths(info))
        assert info.is_electron is expected["electron"]
        assert info.arch == "x86_64"
        assert (info.file_count, info.tree_size) == (expected["file_count"], expected["tree_size"])
        # neither archive has an image outside its app bundle (app.asar / .dll resources)
        assert info.icon_path is None and NO_ICON in info.warnings
        assert info.desktop_entry is None and info.sha256 is None
        kept = sum(p.stat().st_size for p in info.work_dir.rglob("*") if p.is_file())
        assert kept < 1024 * 1024
    after = archive.stat()
    assert (before.st_mtime_ns, before.st_size, before.st_mode) == (after.st_mtime_ns, after.st_size, after.st_mode)
    assert list(private_tempdir.iterdir()) == []


# =============================================================================================
# Archives made by the real tools
# =============================================================================================

def build_source_tree(root: Path) -> Path:
    """A little app folder with everything that makes archives interesting."""
    top = root / "MyTool-1.0"
    (top / "lib").mkdir(parents=True)
    (top / "mytool").write_bytes(fake_elf(size=2048))
    (top / "mytool").chmod(0o755)
    (top / "run me.sh").write_bytes(fake_script())
    (top / "run me.sh").chmod(0o755)
    (top / "lib" / "libx.so.1.2").write_bytes(fake_elf())
    (top / "lib" / "libx.so").symlink_to("libx.so.1.2")
    (top / "lib" / "up").symlink_to("..")
    (top / ("long-" + "n" * 140 + ".txt")).write_text("a name longer than a tar header field")
    deep = top.joinpath(*[f"level-{i:02d}-{'d' * 24}" for i in range(10)])
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_text("deep down")
    (top / "ünïcode ✓.txt").write_text("names are bytes")
    (top / "hard-a").write_bytes(b"one file, two names")
    os.link(top / "hard-a", top / "hard-b")
    with open(top / "sparse.bin", "wb") as fh:
        fh.truncate(1_000_000)
        fh.seek(700_000)
        fh.write(b"island")
    (top / "empty-dir").mkdir()
    (top / "readonly.txt").write_text("r")
    (top / "readonly.txt").chmod(0o444)
    return top


def exec_bits(root: Path) -> dict[str, bool]:
    return {p.relative_to(root).as_posix(): bool(mode_of(p) & 0o100)
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize("options, name", [
    (["--format=gnu", "-z"], "MyTool-1.0.tar.gz"),
    (["--format=pax", "-J"], "MyTool-1.0.tar.xz"),
    (["--format=pax", "-j", "--sparse"], "MyTool-1.0.tar.bz2"),
    (["--format=gnu", "--sparse", "--label=MyVolume"], "MyTool-1.0.tar"),
    (["--format=oldgnu", "-z", "--owner=0", "--group=0", "--numeric-owner"], "MyTool-1.0.tgz"),
])
def test_archives_made_by_gnu_tar(tmp_path, sandbox, inspect, options, name):
    tar = shutil.which("tar")
    if tar is None:
        pytest.skip("tar is not installed")
    import subprocess
    apps, _outside = sandbox
    source = build_source_tree(tmp_path / "src")
    archive = tmp_path / name
    subprocess.run([tar, "-C", str(source.parent), *options, "-cf", str(archive), source.name],
                   check=True, env={**os.environ, "LC_ALL": "C"})
    assert is_portable_archive(archive)
    info = inspect(archive)
    assert (info.name, info.version, info.strip_prefix) == ("MyTool", "1.0", "MyTool-1.0")
    assert relpaths(info) == ["mytool", "run me.sh"]
    assert info.file_count == 12 and info.tree_size == 2048 + 1_000_000 + sum(
        p.stat().st_size for p in source.rglob("*") if p.is_file() and not p.is_symlink()
        and p.name not in ("mytool", "sparse.bin", "hard-b"))
    dest = apps / "MyTool"
    extract_portable(info, dest)
    assert snapshot(dest) == snapshot(source)
    assert exec_bits(dest) == exec_bits(source)
    assert os.path.samefile(dest / "hard-a", dest / "hard-b")
    assert mode_of(dest / "readonly.txt") == 0o644        # the owner can always change the app
    assert_links_stay_inside(dest)


def test_archive_made_by_zip(tmp_path, sandbox, inspect):
    zip_tool = shutil.which("zip")
    if zip_tool is None:
        pytest.skip("zip is not installed")
    import subprocess
    apps, _outside = sandbox
    source = build_source_tree(tmp_path / "src")
    archive = tmp_path / "MyTool-1.0.zip"
    subprocess.run([zip_tool, "-q", "-r", "-y", str(archive), source.name], check=True, cwd=source.parent,
                   env={**os.environ, "LC_ALL": "C"})
    info = inspect(archive)
    assert (info.name, info.version, info.strip_prefix) == ("MyTool", "1.0", "MyTool-1.0")
    assert relpaths(info) == ["mytool", "run me.sh"]
    dest = apps / "MyTool"
    extract_portable(info, dest)
    assert snapshot(dest) == snapshot(source)
    assert exec_bits(dest) == exec_bits(source)
    assert os.readlink(dest / "lib" / "libx.so") == "libx.so.1.2"
    assert_links_stay_inside(dest)


# =============================================================================================
# Small pieces
# =============================================================================================

def test_file_mode():
    assert portable._file_mode(None) == 0o644
    assert [oct(portable._file_mode(m)) for m in (0o777, 0o7777, 0o755, 0o644, 0o600, 0o400, 0o000, 0o111, 0o011,
                                                  0o500, 0o070, 0o666)] == [
        "0o755", "0o755", "0o755", "0o644", "0o600", "0o600", "0o600", "0o711", "0o600", "0o700", "0o640", "0o644"]


def test_application_ini_parser():
    text = ("; comment\n[Gecko]\nName=wrong\n[App]\nName = Thunderbird \nVersion=115.1\n#x=y\nName=second\n"
            "[XRE]\nVersion=9\n")
    assert portable._parse_application_ini(text) == {"Name": "Thunderbird", "Version": "115.1"}
    assert portable._parse_application_ini(None) == {} and portable._parse_application_ini("\0\0garbage") == {}


def test_names_from_stems():
    name = portable._name_from_stem
    assert name("sublime_text") == "Sublime Text"
    assert name("balenaEtcher-linux-x64") == "balenaEtcher"
    assert name("firefox-128.0.3") == "Firefox"
    assert name("linux-x64") is None and name("dist") is None and name("") is None and name(None) is None
    assert name("dist", allow_generic=True) == "Dist"
    assert name("line\nbreak") == "Line Break"
    assert portable._norm("Balena-Etcher_2") == "balenaetcher2" and portable._norm("Ünï Cödé") == "unicode"
    assert portable._name_forms("Game.x86_64") == {"game"}
    assert portable._name_forms("Godot_v4.2.2-stable_linux.x86_64") == {"godotv422stablelinux", "godot"}
    assert portable._name_forms("idea.sh") == {"idea"}


def test_tar_scan_keeps_only_a_limited_amount(tmp_path, inspect, monkeypatch):
    """Images near the top are kept while a tar passes by - but never more than the budget."""
    files = {"app/app": fake_elf(), **{f"app/pic{i:02d}.png": make_png(64, 64) + bytes(20_000) for i in range(30)},
             "app/app.png": make_png(48, 48)}
    archive = make_tar(tmp_path / "app.tar.gz", files, modes={"app/app": 0o755})
    monkeypatch.setattr(portable, "_CAPTURE_BUDGET", 100_000)
    kept: list[int] = []
    original = portable._TarReader.read

    def recording(self, member, limit):
        data = original(self, member, limit)
        kept.append(len(data))
        return data

    monkeypatch.setattr(portable._TarReader, "read", recording)
    info = inspect(archive)
    assert info.icon_info == ImageInfo("png", 48, 48)
    assert sum(size for size in kept if size > 1000) <= 100_000 + 1000


# =============================================================================================
# Random hostile archives
# =============================================================================================

_FUZZ_NAMES = ["app", "a", "b", "a/b", "a/c", "a/b/c", "a/b/d", "d", "d/e", "d/e/f", "l", "l/x", "l/y/z", "m", "m/n"]
_FUZZ_TARGETS = ["app", "a", "b", "c", "..", "../..", "../../..", ".", "a/b", "../a", "../outside",
                 "../outside/sentinel",
                 "l", "m", "l/..", "m/../..", "l/../../outside", "d/e/../../..", "d/e/../../../outside", "/", "/etc",
                 "x/../../y", "./a/./b", "a/b/c/../../../..", "nothing", "l/x", "m/n/../../../outside/sentinel"]


def random_entries(rng: random.Random, outside: Path, *, zip_archive: bool) -> list:
    entries = [file_entry("app", fake_elf(), 0o755)]
    for _ in range(rng.randint(3, 14)):
        name = rng.choice(_FUZZ_NAMES)
        kind = rng.choice(["file", "file", "dir", "symlink", "symlink", "symlink", "hardlink"])
        if kind == "file":
            entries.append(file_entry(name, rng.randbytes(rng.randint(0, 40)),
                                      rng.choice([0o644, 0o755, 0o4755, 0o600])))
        elif kind == "dir":
            entries.append(dir_entry(name, rng.choice([0o755, 0o700, 0o1777])))
        elif kind == "symlink":
            target = rng.choice([*_FUZZ_TARGETS, str(outside), str(outside / "sentinel")])
            entries.append(symlink_entry(name, target))
        elif not zip_archive:
            entries.append(hardlink_entry(
                name, rng.choice([*_FUZZ_NAMES, "../outside/sentinel", str(outside / "sentinel")])))
    return entries


@pytest.mark.parametrize("suffix", [".tar", ".zip"])
def test_random_hostile_archives_never_escape(tmp_path, sandbox, suffix):
    """Whatever mix of files, folders and links an archive brings: either it is unpacked with
    every link staying inside, or it is refused and nothing is left - never anything outside."""
    apps, outside = sandbox
    sentinel = outside / "sentinel"
    before = (sentinel.stat().st_mode, sentinel.stat().st_nlink, sentinel.stat().st_mtime_ns)
    rng = random.Random(20260930)
    outcomes = {"unpacked": 0, "refused": 0}
    for round_number in range(150):
        entries = random_entries(rng, outside, zip_archive=suffix == ".zip")
        archive = make_archive(tmp_path / f"fuzz{suffix}", entries=entries)
        dest = apps / "fuzz"
        try:
            extract_portable(info_for(archive), dest)
        except (ArchiveError, InstallError):
            outcomes["refused"] += 1
            assert list(apps.iterdir()) == [], (round_number, entries)
        else:
            outcomes["unpacked"] += 1
            assert_links_stay_inside(dest)
            assert (dest / "app").is_file() and not (dest / "app").is_symlink()
            for folder, dirs, files in os.walk(dest):
                for name in [*dirs, *files]:
                    path = os.path.join(folder, name)
                    if not os.path.islink(path):
                        assert not os.lstat(path).st_mode & 0o7022, (round_number, path)
            shutil.rmtree(dest)
        assert snapshot(outside) == {"sentinel": b"untouched"}, (round_number, entries)
        assert (sentinel.stat().st_mode, sentinel.stat().st_nlink, sentinel.stat().st_mtime_ns) == before
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["apps", "home", "outside", f"fuzz{suffix}"])
    assert outcomes["unpacked"] > 30 and outcomes["refused"] > 5, outcomes


@pytest.mark.parametrize("suffix", [".tar.gz", ".tar.xz", ".tar.bz2"])
def test_compressed_tar_is_read_through_exactly_once(tmp_path, inspect, monkeypatch, suffix):
    """Going back in a compressed stream means decompressing it again from the start. For the
    usual layouts everything inspection needs is picked up while the archive passes by."""
    _compression = pytest.importorskip("_compression")      # where gzip/bz2/lzma readers live
    rewinds: list[int] = []
    original = _compression.DecompressReader._rewind

    def counting(self):
        rewinds.append(1)
        return original(self)

    monkeypatch.setattr(_compression.DecompressReader, "_rewind", counting)
    big_first = {"MyApp-linux-x64/aaa-first.bin": noise(3_000_000), **electron_tree()["files"]}
    trees = {"electron": dict(electron_tree(), files=big_first), "jetbrains": jetbrains_tree(),
             "firefox": firefox_tree(), "blender": blender_tree(), "flat": flat_tree()}
    for name, tree in trees.items():
        calls: list[float | None] = []
        info = inspect(make_archive(tmp_path / f"{name}-1.0{suffix}", **tree),
                       progress=lambda fraction, text: calls.append(fraction))
        assert info.executable and rewinds == [], name
        if name == "electron":      # a big member passes: progress does not stand still
            assert len([f for f in calls if f not in (None, 0.0, 1.0)]) >= 2


# --------------------------------------------------------------------------------------------
# archives that are no ready-to-use app (PORT-03, PORT-04), names (PORT-05), usr/ layouts
# (PORT-08), names that are not UTF-8 (CLI-7)
# --------------------------------------------------------------------------------------------

def _all_exec(files: dict) -> dict:
    return {name: 0o755 for name in files}


def _in(top: str, mapping: dict) -> dict:
    return {f"{top}/{name}": value for name, value in mapping.items()}


def test_a_source_code_tarball_is_refused(tmp_path, inspect):
    files = {"configure": fake_script(), "autogen.sh": fake_script(), "Makefile.am": "all:\n",
             "htop.c": "int main(){}\n", "htop.desktop": "[Desktop Entry]\nType=Application\n"
             "Name=Htop\nExec=htop\nTerminal=true\nIcon=htop\n", "htop.png": make_png(64, 64)}
    archive = make_archive(tmp_path / "htop-3.3.0.tar.xz", _in("htop-3.3.0", files),
                           modes=_in("htop-3.3.0", {"configure": 0o755, "autogen.sh": 0o755}))
    with pytest.raises(ArchiveError, match="source code of a program"):
        inspect(archive)


@pytest.mark.parametrize("program, archive_name", [
    ("install.sh", "cnijfilter2-6.60-1-deb.zip"),
    ("install.sh", "Orchis-theme-2024-05-01.zip"),
    ("DaVinci_Resolve_19.0.1_Linux.run", "DaVinci_Resolve_19.0.1_Linux.zip"),
    ("setup", "Tool-1.0-linux.tar.gz"),
])
def test_an_installer_in_an_archive_is_refused(program, archive_name, tmp_path, inspect):
    files = {program: fake_elf() if program.endswith(".run") else fake_script(),
             "packages/driver.deb": b"!<arch>\n", "README.txt": "Run the installer.\n"}
    archive = make_archive(tmp_path / archive_name, files, modes={program: 0o755})
    with pytest.raises(ArchiveError, match=f"contains an installer \\({program}\\)"):
        inspect(archive)


def test_an_archive_with_only_helpers_is_no_app(tmp_path, inspect):
    files = {"chrome-sandbox": fake_elf(), "crashpad_handler": fake_elf()}
    with pytest.raises(ArchiveError, match="does not look like an app"):
        inspect(make_archive(tmp_path / "Helpers.zip", files, modes=_all_exec(files)))


def test_an_installer_next_to_the_app_is_not_chosen(tmp_path, inspect):
    files = {"install.sh": fake_script(), "mytool": fake_elf(), "uninstall.sh": fake_script()}
    info = inspect(make_archive(tmp_path / "MyTool-1.0.zip", files, modes=_all_exec(files)))
    assert info.executable == "mytool"


def test_an_appimage_in_an_archive_is_refused(tmp_path, inspect):
    """PORT-04: no FUSE fallback, sandbox fix, icon or version for it as a portable app."""
    appimage = bytearray(fake_elf(size=4096))
    appimage[8:11] = b"AI\x02"
    files = {"winboat-0.9.0-x86_64.AppImage": bytes(appimage), "README.md": "hi\n"}
    archive = make_archive(tmp_path / "winboat-linux.zip", files, modes=_all_exec(files))
    with pytest.raises(ArchiveError) as err:
        inspect(archive)
    assert "contains the AppImage “winboat-0.9.0-x86_64.AppImage”" in str(err.value)
    assert "unpack the archive first" in str(err.value)


def test_a_tool_for_the_terminal_gets_a_warning(tmp_path, inspect):
    files = {"bin/node": fake_elf(), "bin/npm": fake_script(), "include/node/node.h": "x\n",
             "share/man/man1/node.1": ".TH NODE 1\n", "README.md": "Node\n"}
    info = inspect(make_archive(tmp_path / "node-v24.13.1-linux-x64.tar.xz",
                                _in("node-v24.13.1-linux-x64", files),
                                modes=_in("node-v24.13.1-linux-x64",
                                          {"bin/node": 0o755, "bin/npm": 0o755})))
    assert info.executable == "bin/node"
    assert any("tool for the terminal" in warning for warning in info.warnings)


def test_a_menu_entry_whose_program_is_not_in_the_archive_is_a_warning(tmp_path, inspect):
    files = {"skyapp": fake_elf(), "chrome-sandbox": fake_elf(),
             "skyapp.desktop": "[Desktop Entry]\nType=Application\nName=Skyapp\n"
                               "Exec=/usr/bin/skyapp-launcher --no-sandbox %U\n"}
    info = inspect(make_archive(tmp_path / "Skyapp-1.0-linux-x64.tar.gz", files,
                                modes={"skyapp": 0o755, "chrome-sandbox": 0o755}))
    assert info.executable == "skyapp"
    assert any("starts “/usr/bin/skyapp-launcher”, which is not in it" in warning
               for warning in info.warnings)


@pytest.mark.parametrize("archive_name", ["linux.zip", "app-linux.zip", "release-linux-x64.zip",
                                          "artifact.zip", "dist.tar.gz"])
def test_generic_archive_names_take_the_programs_name(archive_name, tmp_path, inspect):
    """PORT-05: two unrelated apps in archives called "linux.zip" do not get the same id."""
    first = inspect(make_archive(tmp_path / "1" / archive_name,
                                 {"mytool": fake_elf(), "README.txt": "x\n"},
                                 modes={"mytool": 0o755}))
    second = inspect(make_archive(tmp_path / "2" / archive_name,
                                  {"photoeditor": fake_elf(), "README.txt": "x\n"},
                                  modes={"photoeditor": 0o755}))
    assert (first.name, first.app_id) == ("Mytool", "mytool")
    assert (second.name, second.app_id) == ("Photoeditor", "photoeditor")


def test_an_archive_laid_out_like_a_package(tmp_path, inspect):
    """PORT-08: usr/bin/<app> with its menu entry in usr/share/applications."""
    files = {"usr/bin/deskapp": fake_elf(), "usr/lib/libdesk.so.1": fake_elf(),
             "usr/share/applications/deskapp.desktop":
                 "[Desktop Entry]\nType=Application\nName=DeskApp\nExec=deskapp %F\nIcon=deskapp\n",
             "usr/share/icons/hicolor/256x256/apps/deskapp.png": make_png(256, 256)}
    archive = make_archive(tmp_path / "deskapp-2.3-linux-x86_64.tar.gz",
                           _in("deskapp-2.3", files),
                           modes=_in("deskapp-2.3", {"usr/bin/deskapp": 0o755}))
    info = inspect(archive)
    assert (info.name, info.executable, info.version) == ("DeskApp", "usr/bin/deskapp", "2.3")
    assert info.icon_path is not None and info.warnings == []
    # a program that only the menu entry names, wherever it lies
    files = {"opt/sky/sky-bin": fake_elf(), "opt/sky/other": fake_elf(),
             "sky.desktop": "[Desktop Entry]\nType=Application\nName=Sky\nExec=sky-bin\n"}
    info = inspect(make_archive(tmp_path / "Sky.tar.gz", files, modes=_all_exec(files)))
    assert info.executable == "opt/sky/sky-bin"


def test_programs_whose_name_is_not_utf8_are_not_offered(tmp_path, inspect):
    """CLI-7: a menu entry is UTF-8; such a program could never be started from it."""
    files = {"Caf-1.0/caf\udce9": fake_elf(), "Caf-1.0/caf": fake_elf()}
    archive = make_tar(tmp_path / "Caf-1.0-linux-x64.tar.gz", files,
                       modes={name: 0o755 for name in files}, tar_format=tarfile.GNU_FORMAT)
    info = inspect(archive)
    assert [c.relpath for c in info.executables] == ["caf"]
    assert info.has_member("caf\udce9") and info.has_member("caf")
    assert info.has_member("nothing") is False


def asar(files: dict[str, bytes]) -> bytes:
    """An Electron ``app.asar``: a pickled JSON file list, then the files' contents."""
    import struct as st

    index: dict = {"files": {}}
    data = b""
    for path, content in files.items():
        node = index
        *folders, name = path.split("/")
        for folder in folders:
            node = node["files"].setdefault(folder, {"files": {}})
        node["files"][name] = {"size": len(content), "offset": str(len(data))}
        data += content
    text = json.dumps(index).encode()
    padded = text + b"\0" * (-len(text) % 4)
    header = st.pack("<II", 4 + len(padded), len(text)) + padded
    return st.pack("<II", 4, len(header)) + header + data


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".zip"])
def test_the_icon_of_an_electron_app_is_found_in_app_asar(suffix, tmp_path, inspect):
    """PORT-12: e.g. Pen keeps its icon only in resources/app.asar (out/assets/icon.png).
    A compressed tar is not decompressed twice for it: its start is kept as it passes by."""
    tree = electron_tree("Pen-linux-x64", program="pen")
    tree["files"]["Pen-linux-x64/resources/app.asar"] = asar({
        "package.json": b"{}", "out/main.js": b"x" * 5000,
        "out/editor/images/scripts/chart.png": make_png(64, 32),
        "out/assets/banner.png": make_png(400, 100),
        "out/assets/icon.png": make_png(512, 512, (1, 2, 3, 255)),
    })
    info = inspect(make_archive(tmp_path / f"Pen-1.2.15-linux-x64{suffix}", **tree))
    assert info.is_electron and info.icon_path is not None
    assert info.icon_path.read_bytes() == make_png(512, 512, (1, 2, 3, 255))
    assert (info.icon_info.width, info.icon_info.height) == (512, 512)
    assert NO_ICON not in info.warnings
    # nothing usable in there: the standard icon, and the warning says so honestly
    tree["files"]["Pen-linux-x64/resources/app.asar"] = asar({"out/fonts/x.ttf": b"f"})
    info = inspect(make_archive(tmp_path / "other" / f"Pen-linux-x64{suffix}", **tree))
    assert info.icon_path is None and NO_ICON in info.warnings
    tree["files"]["Pen-linux-x64/resources/app.asar"] = b"not an asar at all" * 10
    info = inspect(make_archive(tmp_path / "third" / f"Pen-linux-x64{suffix}", **tree))
    assert info.icon_path is None
