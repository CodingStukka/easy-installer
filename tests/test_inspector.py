from __future__ import annotations

import dataclasses
import gc
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from easy_installer.core import inspector
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.imageinfo import ImageInfo
from easy_installer.core.inspector import AppImageInfo, inspect_appimage
from easy_installer.core.signature import SignatureInfo
from easy_installer.core.squashfs import ExtractCommandReader
from easy_installer.core.updates import UpdateSource
from easy_installer.errors import EasyInstallerError, ExtractionError, NotAnAppImageError

from fakeappimage import (
    ANYTYPE_DESKTOP,
    ANYTYPE_XWAYLAND_DESKTOP,
    EM_AARCH64,
    T3_DESKTOP,
    build_runtime,
    make_fake_appimage,
    make_png,
    make_runtime_emulator,
    make_sample_appimage,
    make_svg,
    make_xpm,
    requires_mksquashfs,
    requires_unsquashfs,
)

pytestmark = [requires_mksquashfs, requires_unsquashfs]

T3_FILE = "T3-Code-0.0.42-x86_64.AppImage"
OPENSCAD_FILE = "OpenSCAD-2026.03.28-x86_64.AppImage"
FREECAD_FILE = "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"
HICOLOR = "usr/share/icons/hicolor"


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path, monkeypatch):
    """Route tempfile into tmp_path so tests can check that nothing is left behind."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


@pytest.fixture
def inspect():
    created: list[AppImageInfo] = []

    def _inspect(path, **kwargs):
        info = inspect_appimage(path, **kwargs)
        created.append(info)
        return info

    yield _inspect
    for info in created:
        info.cleanup()


def desktop(name="Test App", icon="testapp", exec_="AppRun %U", extra=""):
    return (f"[Desktop Entry]\nType=Application\nName={name}\nExec={exec_}\nIcon={icon}\n"
            f"Categories=Utility;\n{extra}")


def app_with(tmp_path, files, symlinks=None, name="TestApp-1.0-x86_64.AppImage", **kw):
    base = {"testapp.desktop": desktop()}
    base.update(files)
    return make_fake_appimage(tmp_path / name, base, symlinks or {}, **kw)


def icon_rel(info: AppImageInfo) -> str:
    assert info.icon_path is not None
    return info.icon_path.relative_to(info.work_dir / "root").as_posix()


# =============================================================================================
# realistic samples
# =============================================================================================


def test_t3_like(tmp_path, inspect):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    info = inspect(path)
    assert info.path == path.resolve()
    assert info.size == path.stat().st_size
    assert info.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info.appimage_type == 2 and info.elf.appimage_type == 2
    assert info.arch == "x86_64"
    assert info.app_id == "t3code"
    assert info.name == "T3 Code (Alpha)" and info.display_name == "T3 Code (Alpha)"
    assert info.version == "0.0.42"
    assert info.comment == "T3 Code desktop build"
    assert info.categories == ["Development"]
    assert info.desktop_filename == "t3code.desktop"
    assert isinstance(info.desktop_entry, DesktopEntry)
    assert info.desktop_entry.to_text() == T3_DESKTOP
    assert icon_rel(info) == f"{HICOLOR}/512x512/apps/t3code.png"
    assert info.icon_info == ImageInfo("png", 512, 512)
    assert info.icon_path.read_bytes() == make_png(512, 512)
    assert info.is_electron is True
    assert info.exec_has_no_sandbox is True
    assert info.terminal is False
    assert info.update_info is None
    assert info.elf.has_interp is True
    assert info.warnings == []


def test_openscad_like(tmp_path, inspect):
    info = inspect(make_sample_appimage(tmp_path / OPENSCAD_FILE, "openscad"))
    assert info.app_id == "openscad"
    assert info.name == "OpenSCAD"
    assert info.version == "2026.03.28"
    # Icon=openscad-nightly: 512 missing -> 256 is next in the priority list (before 128/64)
    assert icon_rel(info) == f"{HICOLOR}/256x256/apps/openscad-nightly.png"
    assert info.icon_info == ImageInfo("png", 256, 256)
    assert info.is_electron is False and info.exec_has_no_sandbox is False
    assert info.categories == ["Graphics", "3DGraphics", "Engineering"]


def test_freecad_like(tmp_path, inspect):
    info = inspect(make_sample_appimage(tmp_path / FREECAD_FILE, "freecad"))
    assert info.app_id == "org.freecad.FreeCAD"
    assert info.name == "FreeCAD"
    assert info.version == "1.1.3"   # not embedded: parsed from the file name
    assert icon_rel(info) == f"{HICOLOR}/scalable/apps/org.freecad.FreeCAD.svg"
    assert info.icon_info.format == "svg"
    assert info.update_info == "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"
    assert info.elf.has_interp is False


def test_localized_display_name_and_comment(tmp_path, inspect, monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    path = app_with(tmp_path, {"testapp.desktop": desktop(extra="Name[de]=Testprogramm\nComment=Hi\nComment[de]=Hallo\n")})
    info = inspect(path)
    assert info.name == "Test App"
    assert info.display_name == "Testprogramm"
    assert info.comment == "Hallo"


# =============================================================================================
# icon priority
# =============================================================================================

ALL_ICON_CANDIDATES = {
    f"{HICOLOR}/scalable/apps/testapp.svg": make_svg(64, 64),
    **{f"{HICOLOR}/{s}x{s}/apps/testapp.png": make_png(s, s) for s in (512, 256, 1024, 192, 128, 96, 64, 48, 32)},
    "testapp.svg": make_svg(10, 10),
    "testapp.png": make_png(10, 10),
    "testapp.xpm": make_xpm(10, 10),
    "usr/share/pixmaps/testapp.png": make_png(11, 11),
    "usr/share/pixmaps/testapp.xpm": make_xpm(12, 12),
    "diricon.png": make_png(13, 13),
}
PRIORITY = [
    f"{HICOLOR}/scalable/apps/testapp.svg",
    *[f"{HICOLOR}/{s}x{s}/apps/testapp.png" for s in (512, 256, 1024, 192, 128, 96, 64, 48, 32)],
    "testapp.svg", "testapp.png", "testapp.xpm",
    "usr/share/pixmaps/testapp.png", "usr/share/pixmaps/testapp.xpm",
    "diricon.png",
]


@pytest.mark.parametrize("index", range(len(PRIORITY)))
def test_icon_priority(tmp_path, inspect, index):
    files = {k: v for k, v in ALL_ICON_CANDIDATES.items() if k in PRIORITY[index:]}
    path = app_with(tmp_path, files, {".DirIcon": "diricon.png"})
    info = inspect(path, compute_hash=False)
    assert icon_rel(info) == PRIORITY[index]


@pytest.mark.parametrize("icon_value", ["testapp", "testapp.png", "/usr/share/icons/testapp.png", "testapp.svg"])
def test_icon_value_forms(tmp_path, inspect, icon_value):
    files = {"testapp.desktop": desktop(icon=icon_value), f"{HICOLOR}/48x48/apps/testapp.png": make_png(48, 48)}
    info = inspect(app_with(tmp_path, files), compute_hash=False)
    assert icon_rel(info) == f"{HICOLOR}/48x48/apps/testapp.png"


def test_icon_name_with_dots_keeps_its_name(tmp_path, inspect):
    files = {"testapp.desktop": desktop(icon="org.example.App"), "org.example.App.png": make_png(32, 32)}
    assert icon_rel(inspect(app_with(tmp_path, files), compute_hash=False)) == "org.example.App.png"


def test_explicit_icon_path_inside_appimage(tmp_path, inspect):
    files = {"testapp.desktop": desktop(icon="/opt/app/res/logo.png"), "opt/app/res/logo.png": make_png(40, 40)}
    assert icon_rel(inspect(app_with(tmp_path, files), compute_hash=False)) == "opt/app/res/logo.png"


def test_oversized_and_broken_icons_are_skipped(tmp_path, inspect):
    big = make_png(512, 512) + b"\0" * (inspector.MAX_ICON_SIZE + 1)
    files = {
        f"{HICOLOR}/scalable/apps/testapp.svg": "this is not an svg",
        f"{HICOLOR}/512x512/apps/testapp.png": big,
        f"{HICOLOR}/256x256/apps/testapp.png": b"",
        f"{HICOLOR}/128x128/apps/testapp.png": make_png(128, 128),
    }
    info = inspect(app_with(tmp_path, files), compute_hash=False)
    assert icon_rel(info) == f"{HICOLOR}/128x128/apps/testapp.png"


def test_members_over_the_size_limits_are_never_extracted(tmp_path, inspect):
    """Repeated bytes compress about 1000:1 (and equal files are stored once): a small AppImage
    must not fill /tmp just by being opened (limits are checked before extracting)."""
    mb = 1 << 20
    files = {"testapp.desktop": desktop(icon="big", extra="MimeType=application/x-big;\n"),
             "usr/share/mime/packages/": "", f"{HICOLOR}/scalable/apps/": ""}
    pseudo = [f"usr/share/mime/packages/{i}.xml f 644 0 0 head -c {3 * mb} /dev/zero"
              for i in range(8)]
    pseudo += [f"{HICOLOR}/scalable/apps/big.svg f 644 0 0 head -c {12 * mb} /dev/zero",
               f"big.png f 644 0 0 head -c {12 * mb} /dev/zero",
               f"huge.desktop f 644 0 0 head -c {2 * mb} /dev/zero"]
    path = make_fake_appimage(tmp_path / "Big.AppImage", files, {".DirIcon": "big.png"},
                              pseudo=pseudo)
    assert path.stat().st_size < 2 * mb
    info = inspect(path, compute_hash=False)
    extracted = sum(p.lstat().st_size for p in info.work_dir.rglob("*"))
    assert extracted < mb, extracted
    assert info.desktop_filename == "testapp.desktop"
    assert info.icon_path is None and info.mime_types == []


def test_mime_packages_keep_their_declared_encoding(tmp_path, inspect):
    ns = "http://www.freedesktop.org/standards/shared-mime-info"
    def package(mime, ext, encoding):
        return (f'<?xml version="1.0" encoding="{encoding}"?>\n<mime-info xmlns="{ns}">'
                f'<mime-type type="{mime}"><comment>Café-Dokument für Müller</comment>'
                f'<glob pattern="*.{ext}"/></mime-type></mime-info>\n').encode(encoding)
    files = {"testapp.desktop": desktop(extra="MimeType=application/x-cafe;application/x-wide;\n"),
             "usr/share/mime/packages/cafe.xml": package("application/x-cafe", "cafe", "ISO-8859-1"),
             "usr/share/mime/packages/wide.xml": package("application/x-wide", "wide", "UTF-16")}
    info = inspect(app_with(tmp_path, files), compute_hash=False)
    assert [(d.type, d.comment) for d in info.mime_types] == [
        ("application/x-cafe", "Café-Dokument für Müller"),
        ("application/x-wide", "Café-Dokument für Müller")]


def test_dir_icon_symlink_chain(tmp_path, inspect):
    files = {"testapp.desktop": desktop(icon="nothing-matches"), "real/icon.png": make_png(64, 64)}
    links = {".DirIcon": "hop1.png", "hop1.png": "sub/hop2.png", "sub/hop2.png": "../real/icon.png"}
    info = inspect(app_with(tmp_path, files, links), compute_hash=False)
    assert icon_rel(info) == "real/icon.png"
    assert info.icon_info == ImageInfo("png", 64, 64)


def test_symlinked_parent_directory_is_resolved_inside_payload(tmp_path, inspect):
    files = {"shared/icons/hicolor/256x256/apps/testapp.png": make_png(256, 256)}
    links = {"usr/share/icons": "../../shared/icons"}
    info = inspect(app_with(tmp_path, files, links), compute_hash=False)
    assert icon_rel(info) == "shared/icons/hicolor/256x256/apps/testapp.png"


def test_icon_symlink_to_absolute_host_path_is_rejected(tmp_path, inspect):
    host_icon = tmp_path / "host-secret.png"
    host_icon.write_bytes(make_png(99, 99))
    files = {"testapp.desktop": desktop(icon="testapp")}
    links = {".DirIcon": str(host_icon), "testapp.png": str(host_icon)}
    info = inspect(app_with(tmp_path, files, links), compute_hash=False)
    assert info.icon_path is None
    assert any("icon" in w for w in info.warnings)


@pytest.mark.parametrize("target", [
    "../../../../../../../etc/passwd",
    "usr/../../outside.png",
    "a/../../outside.png",
])
def test_icon_symlink_escaping_with_dotdot_is_rejected(tmp_path, inspect, target):
    (tmp_path / "outside.png").write_bytes(make_png(5, 5))
    info = inspect(app_with(tmp_path, {"a/x": "x"}, {".DirIcon": target}), compute_hash=False)
    assert info.icon_path is None


def test_symlink_loops_and_hop_limit(tmp_path, inspect):
    files = {"testapp.desktop": desktop(icon="nothing"), "end.png": make_png(8, 8)}
    loop = {".DirIcon": "l1", "l1": "l2", "l2": "l1"}
    assert inspect(app_with(tmp_path, files, loop, name="loop.AppImage"), compute_hash=False).icon_path is None

    def chain(hops):  # .DirIcon + (hops - 1) intermediate links = `hops` symlinks in total
        links = {".DirIcon": "c1"}
        for i in range(1, hops - 1):
            links[f"c{i}"] = f"c{i + 1}"
        links[f"c{hops - 1}"] = "end.png"
        return links

    ok = inspect(app_with(tmp_path, files, chain(8), name="eight.AppImage"), compute_hash=False)
    assert icon_rel(ok) == "end.png"
    too_long = inspect(app_with(tmp_path, files, chain(9), name="nine.AppImage"), compute_hash=False)
    assert too_long.icon_path is None


def test_symlink_to_directory_is_not_an_icon(tmp_path, inspect):
    files = {"testapp.desktop": desktop(icon="nothing"), "usr/share/big/file.bin": b"x" * 100}
    info = inspect(app_with(tmp_path, files, {".DirIcon": "usr/share"}), compute_hash=False)
    assert info.icon_path is None
    assert not (info.work_dir / "root" / "usr" / "share" / "big").exists()  # nothing pulled in


# =============================================================================================
# desktop entry selection
# =============================================================================================


def test_prefers_visible_application(tmp_path, inspect):
    files = {"anytype.desktop": ANYTYPE_DESKTOP, "anytype-xwayland.desktop": ANYTYPE_XWAYLAND_DESKTOP,
             f"{HICOLOR}/512x512/apps/anytype.png": make_png(512, 512)}
    path = make_fake_appimage(tmp_path / "Anytype-0.55.4.AppImage", files,
                              {".DirIcon": f"{HICOLOR}/512x512/apps/anytype.png"})
    info = inspect(path, compute_hash=False)
    assert info.desktop_filename == "anytype.desktop"
    assert info.app_id == "anytype"
    assert info.exec_has_no_sandbox is False


def test_hidden_and_non_application_entries_rank_lower(tmp_path, inspect):
    files = {
        "a.desktop": desktop(name="A", extra="Hidden=true\n"),
        "b.desktop": "[Desktop Entry]\nType=Link\nName=B\nURL=https://example.org\n",
        "c.desktop": desktop(name="C"),
    }
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", files), compute_hash=False)
    assert info.desktop_filename == "c.desktop"


def test_dir_icon_stem_breaks_ties(tmp_path, inspect):
    files = {"aaa.desktop": desktop(name="Helper", icon="zzz"), "zzz.desktop": desktop(name="Main", icon="zzz"),
             "zzz.png": make_png(16, 16)}
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", files, {".DirIcon": "zzz.png"}), compute_hash=False)
    assert info.desktop_filename == "zzz.desktop"
    assert info.name == "Main"


def test_alphabetical_otherwise(tmp_path, inspect):
    files = {"bbb.desktop": desktop(name="B"), "aaa.desktop": desktop(name="A")}
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", files), compute_hash=False)
    assert info.desktop_filename == "aaa.desktop"


def test_desktop_file_symlink_inside_payload(tmp_path, inspect):
    files = {"usr/share/applications/org.example.Tool.desktop": desktop(name="Tool", icon="tool"),
             "tool.png": make_png(32, 32)}
    links = {"org.example.Tool.desktop": "usr/share/applications/org.example.Tool.desktop"}
    info = inspect(make_fake_appimage(tmp_path / "tool.AppImage", files, links), compute_hash=False)
    assert info.desktop_filename == "org.example.Tool.desktop"
    assert info.app_id == "org.example.Tool"
    assert info.name == "Tool"


def test_desktop_symlink_to_host_file_is_ignored(tmp_path, inspect):
    host = tmp_path / "host.desktop"
    host.write_text(desktop(name="From Host"))
    info = inspect(make_fake_appimage(tmp_path / "Thing-2.0.AppImage", {"x": "y"}, {"evil.desktop": str(host)}),
                   compute_hash=False)
    assert info.desktop_entry is None
    assert info.name == "Thing"


def test_desktop_directory_and_oversized_files_are_ignored(tmp_path, inspect):
    files = {
        "dir.desktop/inner.bin": b"x" * 1000,
        "huge.desktop": desktop(name="Huge") + "X-Pad=" + "p" * inspector.MAX_DESKTOP_SIZE + "\n",
        "ok.desktop": desktop(name="OK"),
    }
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", files), compute_hash=False)
    assert info.desktop_filename == "ok.desktop"
    assert not (info.work_dir / "root" / "dir.desktop").exists()


def test_entry_without_main_group_is_ignored(tmp_path, inspect):
    files = {"junk.desktop": "this is not a desktop file\n"}
    info = inspect(make_fake_appimage(tmp_path / "Junk_3.2.1-x86_64.AppImage", files), compute_hash=False)
    assert info.desktop_entry is None
    assert info.version == "3.2.1"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_desktop_file_mode_is_handled(tmp_path, inspect):
    path = app_with(tmp_path, {}, modes={"testapp.desktop": 0o000})
    info = inspect(path, compute_hash=False)
    assert info.name == "Test App"


def test_no_desktop_file_no_icon(tmp_path, inspect):
    path = make_fake_appimage(tmp_path / "Cool_Tool-2.5.1-x86_64.AppImage", {"usr/bin/cool": b"bin"})
    info = inspect(path, compute_hash=False)
    assert info.desktop_entry is None and info.desktop_filename is None
    assert info.name == "Cool Tool" and info.display_name == "Cool Tool"
    assert info.app_id == "cool-tool"
    assert info.version == "2.5.1"
    assert info.icon_path is None and info.icon_info is None
    assert info.categories == [] and info.comment is None
    assert len(info.warnings) == 2
    assert info.is_electron is False


def test_generic_desktop_name_uses_name_slug(tmp_path, inspect):
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", {"AppRun.desktop": desktop(name="My Great App")}),
                   compute_hash=False)
    assert info.app_id == "my-great-app"


def test_names_are_sanitized(tmp_path, inspect):
    files = {"x.desktop": desktop(name="Evil\\nName\\twith\\rcontrols " + "x" * 300)}
    info = inspect(make_fake_appimage(tmp_path / "x.AppImage", files), compute_hash=False)
    assert "\n" not in info.name and "\t" not in info.name
    assert info.name.startswith("Evil Name with controls")
    assert len(info.name) <= 200


def test_electron_detection_variants(tmp_path, inspect):
    for marker in ("chrome-sandbox", "chrome_crashpad_handler", "resources/app.asar"):
        path = app_with(tmp_path, {marker: b"x"}, name=f"{marker.replace('/', '_')}.AppImage")
        assert inspect(path, compute_hash=False).is_electron is True
    # Repacked .deb packages keep their layout (opt/<App>/, usr/lib/<app>/).
    for marker in ("lib/chrome-sandbox", "opt/Signal/chrome-sandbox",
                   "usr/lib/signal-desktop/resources/app.asar",
                   "usr/lib/foo/chrome_crashpad_handler"):
        path = app_with(tmp_path, {marker: b"x"}, name=f"{marker.replace('/', '_')}.AppImage")
        assert inspect(path, compute_hash=False).is_electron is True, marker
    for other in ("a/b/c/d/e/chrome-sandbox", "usr/share/doc/app.asar", "resources/app.asar.txt"):
        path = app_with(tmp_path, {other: b"x"}, name=f"{other.replace('/', '_')}.AppImage")
        assert inspect(path, compute_hash=False).is_electron is False, other


def test_electron_detection_without_listing_probes_the_exec_folder(tmp_path, inspect, monkeypatch):
    files = {"signal.desktop": desktop(exec_="opt/Signal/signal-desktop %U"),
             "opt/Signal/chrome-sandbox": b"x", "opt/Signal/signal-desktop": b"\x7fELF"}
    path = app_with(tmp_path, files)
    script = make_runtime_emulator(tmp_path, path)
    monkeypatch.setattr(inspector, "open_payload",
                        lambda p, elf: ExtractCommandReader(p, command=[str(script)]))
    assert inspect(path, compute_hash=False).is_electron is True


def test_terminal_and_no_sandbox_flags(tmp_path, inspect):
    files = {"testapp.desktop": desktop(exec_='AppRun "--no-sandbox" %U', extra="Terminal=true\n")}
    info = inspect(app_with(tmp_path, files), compute_hash=False)
    assert info.terminal is True
    assert info.exec_has_no_sandbox is True
    broken = {"testapp.desktop": desktop(exec_='AppRun "unbalanced --no-sandbox')}
    assert inspect(app_with(tmp_path, broken, name="b.AppImage"), compute_hash=False).exec_has_no_sandbox is False


def test_foreign_architecture_warning(tmp_path, inspect):
    path = app_with(tmp_path, {"testapp.png": make_png(8, 8)}, machine=EM_AARCH64)
    info = inspect(path, compute_hash=False)
    assert info.arch == "aarch64"
    assert any("aarch64" in w for w in info.warnings)


@pytest.mark.parametrize("machine, arch", [(243, "riscv64"), (21, "ppc64le"), (22, "s390x"),
                                           (258, "loongarch64"), (4242, "ELF machine 4242")])
def test_other_architectures_are_foreign_too(tmp_path, inspect, machine, arch):
    path = app_with(tmp_path, {"testapp.png": make_png(8, 8)}, machine=machine,
                    name=f"Foo-1.0-{machine}.AppImage")
    info = inspect(path, compute_hash=False)
    assert any(f"({arch})" in w for w in info.warnings)


def test_v_prefixed_embedded_version_is_normalised(tmp_path, inspect):
    files = {"testapp.desktop": desktop(extra="X-AppImage-Version=v1.2.3\n")}
    assert inspect(app_with(tmp_path, files), compute_hash=False).version == "1.2.3"


def test_dwarfs_appimage_gets_a_truthful_message(tmp_path):
    path = make_fake_appimage(tmp_path / "Dwarf-1.0-x86_64.AppImage", {},
                              payload=b"DWARFS\x02\x00" + bytes(64))
    with pytest.raises(NotAnAppImageError) as excinfo:
        inspect_appimage(path)
    assert "format that Easy Installer cannot read" in str(excinfo.value)
    no_magic = make_fake_appimage(tmp_path / "plain.AppImage", {}, appimage_magic=False,
                                  payload=b"DWARFS\x02\x00" + bytes(64))
    with pytest.raises(NotAnAppImageError, match="not an AppImage"):
        inspect_appimage(no_magic)


# =============================================================================================
# hashing, progress, work_dir lifecycle
# =============================================================================================


def test_hash_and_progress(tmp_path, inspect):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    calls = []
    info = inspect(path, progress=lambda fraction, message: calls.append((fraction, message)))
    assert info.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert calls[0][0] == 0.0 and calls[-1][0] == 1.0
    assert all(isinstance(m, str) and m for _, m in calls)
    fractions = [f for f, _ in calls if f is not None]
    assert fractions == sorted(fractions)
    assert all(0.0 <= f <= 1.0 for f in fractions)


def test_compute_hash_false(tmp_path, inspect):
    assert inspect(make_sample_appimage(tmp_path / T3_FILE, "t3code"), compute_hash=False).sha256 is None


def test_hash_uses_chunks(tmp_path, inspect, monkeypatch):
    monkeypatch.setattr(inspector, "HASH_CHUNK_SIZE", 1000)
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    calls = []
    info = inspect(path, progress=lambda f, m: calls.append(f))
    assert info.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len([c for c in calls if c is not None]) >= path.stat().st_size // 1000


def test_work_dir_lifecycle(tmp_path, private_tempdir):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    info = inspect_appimage(path, compute_hash=False)
    work_dir = info.work_dir
    assert work_dir.is_dir() and work_dir.parent == private_tempdir
    assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700
    assert info.icon_path.is_relative_to(work_dir)
    info.cleanup()
    assert not work_dir.exists()
    info.cleanup()  # idempotent

    with inspect_appimage(path, compute_hash=False) as info2:
        work_dir2 = info2.work_dir
        assert work_dir2.is_dir()
    assert not work_dir2.exists()

    info3 = inspect_appimage(path, compute_hash=False)
    work_dir3 = info3.work_dir
    del info3
    gc.collect()
    assert not work_dir3.exists()
    assert list(private_tempdir.iterdir()) == []


def test_work_dir_is_removed_when_the_program_ends_while_reading(tmp_path):
    """Ctrl+Q in the window while "Reading app information…": the inspection runs in a daemon
    thread that simply stops with the program - its folder in /tmp must go anyway."""
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    tmp = tmp_path / "program-tmp"
    tmp.mkdir()
    script = textwrap.dedent(f"""
        import os, tempfile, threading, time
        tempfile.tempdir = {str(tmp)!r}
        from easy_installer.core.inspector import inspect_appimage

        reading = threading.Event()

        def report(fraction, message):
            if fraction is None:  # the work dir exists and the payload is being read
                reading.set()
                time.sleep(3600)

        threading.Thread(target=inspect_appimage, args=({str(path)!r},),
                         kwargs={{"progress": report}}, daemon=True).start()
        assert reading.wait(60)
        assert os.listdir(tempfile.gettempdir())  # the work dir, still in use
    """)
    env = {**os.environ, "PYTHONPATH": str(Path(inspector.__file__).resolve().parents[2])}
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert list(tmp.iterdir()) == []


def test_fields_match_the_contract(tmp_path, inspect):
    info = inspect(make_sample_appimage(tmp_path / T3_FILE, "t3code"), compute_hash=False)
    assert list(dataclasses.asdict(info)) == [
        "path", "size", "sha256", "elf", "appimage_type", "arch", "app_id", "name", "display_name",
        "version", "comment", "categories", "desktop_entry", "desktop_filename", "icon_path", "icon_info",
        "is_electron", "exec_has_no_sandbox", "terminal", "update_info", "work_dir", "warnings",
        "mime_types",
        # 0.2
        "update_source", "origin_url", "signature", "data_hints",
    ]


# ------------------------------------------------------------------------------------------------
# 0.2: where updates come from, where the file came from, its signature, its data folder names
# ------------------------------------------------------------------------------------------------

FREECAD_UPD_INFO = "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"
T3_UPDATE_YML = "owner: pingdotgg\nrepo: t3code\nprovider: github\nupdaterCacheDirName: t3code-updater\n"


def test_update_source_from_the_update_information(tmp_path, inspect):
    info = inspect(app_with(tmp_path, {"testapp.desktop": desktop()}, upd_info=FREECAD_UPD_INFO),
                   compute_hash=False)
    assert info.update_info == FREECAD_UPD_INFO
    assert info.update_source == UpdateSource(
        kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest",
        pattern="FreeCAD*x86_64*.AppImage", via="upd_info")
    assert info.update_source.describe() == "github.com/FreeCAD/FreeCAD"


def test_no_update_source_is_the_normal_case(tmp_path, inspect):
    for upd_info in (None, "", "bintray-zsync|a|b|c|d.zsync", "zsync|http://example.org/a.zsync"):
        info = inspect(app_with(tmp_path, {"testapp.desktop": desktop()}, upd_info=upd_info),
                       compute_hash=False)
        assert info.update_source is None
        assert (info.origin_url, info.signature) == (None, None)


@pytest.mark.parametrize("where", ["resources/app-update.yml", "opt/T3 Code/resources/app-update.yml",
                                   "usr/lib/t3code/resources/app-update.yml"])
def test_update_source_of_an_electron_app(tmp_path, inspect, where):
    info = inspect(app_with(tmp_path, {
        "testapp.desktop": desktop(), "chrome-sandbox": b"x", where: T3_UPDATE_YML,
    }, upd_info=FREECAD_UPD_INFO), compute_hash=False)
    # the app's own updater configuration wins over the AppImage update information
    assert info.update_source == UpdateSource(kind="electron-github", owner="pingdotgg",
                                              repo="t3code", via="app-update.yml")


def test_unusable_update_configurations_are_ignored(tmp_path, inspect):
    for content in (b"\xff\xfe not text", "provider: s3\nbucket: x\n", "{{{{", "a: b\n" * 9000,
                    "provider: github\nowner: a\nrepo: b\nhost: git.example.org\n"):
        info = inspect(app_with(tmp_path, {
            "testapp.desktop": desktop(), "resources/app-update.yml": content,
        }, upd_info=FREECAD_UPD_INFO), compute_hash=False)
        assert info.update_source.kind == "github-assets", content   # falls back to .upd_info
    # too deep in the payload, a folder, or a link out of the app: not read at all
    info = inspect(app_with(tmp_path, {
        "testapp.desktop": desktop(), "a/b/c/d/e/resources/app-update.yml": T3_UPDATE_YML,
        "opt/x/resources/app-update.yml/": b"",
    }, {"resources/app-update.yml": "/etc/hostname"}), compute_hash=False)
    assert info.update_source is None
    deep = inspect(app_with(tmp_path, {
        "testapp.desktop": desktop(), "a/b/c/d/e/resources/app-update.yml": T3_UPDATE_YML,
    }, name="Deep-1.0.AppImage"), compute_hash=False)
    assert deep.update_source is None


def test_update_configuration_over_the_size_limit_is_never_extracted(tmp_path, inspect):
    big = T3_UPDATE_YML + "# padding\n" * (inspector.MAX_UPDATE_CONFIG_SIZE // 10 + 1)
    assert len(big) > inspector.MAX_UPDATE_CONFIG_SIZE
    info = inspect(app_with(tmp_path, {"testapp.desktop": desktop(), "chrome-sandbox": b"x",
                                       "resources/app-update.yml": big}), compute_hash=False)
    assert info.update_source is None
    assert not (info.work_dir / "root" / "resources" / "app-update.yml").exists()


def test_update_source_without_a_payload_listing(tmp_path, inspect, monkeypatch):
    """Without a listing the file is only tried at the root of Electron apps."""
    path = app_with(tmp_path, {"testapp.desktop": desktop(), "chrome-sandbox": b"x",
                               "resources/app-update.yml": T3_UPDATE_YML})
    from easy_installer.core.squashfs import UnsquashfsReader

    monkeypatch.setattr(UnsquashfsReader, "list_members", lambda self: None)
    monkeypatch.setattr(UnsquashfsReader, "member_sizes", lambda self: None)
    info = inspect(path, compute_hash=False)
    assert info.is_electron and info.update_source.kind == "electron-github"


def test_a_broken_update_parser_never_stops_the_inspection(tmp_path, inspect, monkeypatch):
    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(inspector, "choose_source", boom)
    monkeypatch.setattr(inspector, "data_hints_for", boom)
    info = inspect(app_with(tmp_path, {"testapp.desktop": desktop()}, upd_info=FREECAD_UPD_INFO),
                   compute_hash=False)
    assert info.update_source is None and info.data_hints == [] and info.name == "Test App"


def test_origin_and_signature_are_read_from_the_file_itself(tmp_path, inspect, monkeypatch):
    path = app_with(tmp_path, {"testapp.desktop": desktop()})
    seen = []
    signed = SignatureInfo(status="valid", fingerprint="AB" * 20, signer="Test <t@example.org>",
                           details=None)

    def fake_origin(file):
        seen.append(("origin", file))
        return "https://example.org/dl/TestApp.AppImage"

    def fake_signature(file, elf):
        seen.append(("signature", file, elf.payload_offset))
        return signed

    monkeypatch.setattr(inspector, "read_origin", fake_origin)
    monkeypatch.setattr(inspector, "read_signature", fake_signature)
    info = inspect(path, compute_hash=False)
    assert info.origin_url == "https://example.org/dl/TestApp.AppImage" and info.signature is signed
    assert sorted(seen) == [("origin", info.path), ("signature", info.path, info.elf.payload_offset)]


def test_origin_is_read_from_the_download_mark_of_the_browser(tmp_path, inspect):
    path = app_with(tmp_path, {"testapp.desktop": desktop()})
    try:
        os.setxattr(path, "user.xdg.origin.url", b"https://user:secret@example.org/a/TestApp.AppImage#x")
    except OSError:
        pytest.skip("this file system has no extended attributes")
    assert inspect(path, compute_hash=False).origin_url == "https://example.org/a/TestApp.AppImage"


def test_data_hints_of_the_samples(tmp_path, inspect):
    t3 = inspect(make_sample_appimage(tmp_path / T3_FILE, "t3code"), compute_hash=False)
    assert t3.data_hints == ["T3 Code (Alpha)", "t3code", "t3code-updater"]
    freecad = inspect(make_sample_appimage(tmp_path / FREECAD_FILE, "freecad"), compute_hash=False)
    assert freecad.data_hints == ["FreeCAD", "org.freecad.FreeCAD", "FreeCAD-updater"]
    openscad = inspect(make_sample_appimage(tmp_path / OPENSCAD_FILE, "openscad"), compute_hash=False)
    assert openscad.data_hints == ["OpenSCAD", "org.openscad.openscad", "openscad-updater"]


def test_data_hints_without_a_menu_entry_come_from_the_file_name(tmp_path, inspect):
    info = inspect(make_fake_appimage(tmp_path / "Cool-Tool-2.1-x86_64.AppImage",
                                      {"AppRun": "#!/bin/sh\n"}), compute_hash=False)
    assert info.desktop_entry is None and info.data_hints
    assert all("/" not in hint and len(hint) >= 3 for hint in info.data_hints)


def test_inspection_lists_the_payload_only_once(tmp_path, inspect, monkeypatch):
    """The 0.2 details come from the listing that is read anyway."""
    from easy_installer.core.squashfs import UnsquashfsReader

    calls = []
    original = UnsquashfsReader._run

    def counting(self, args, *a, **kw):
        calls.append(list(args))
        return original(self, args, *a, **kw)

    monkeypatch.setattr(UnsquashfsReader, "_run", counting)
    inspect(make_sample_appimage(tmp_path / T3_FILE, "t3code"), compute_hash=False)
    listings = [args for args in calls if any(a in ("-l", "-ll", "-lln", "-lls") for a in args)]
    assert len(listings) == 1, calls


def test_source_is_never_modified(tmp_path, inspect):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code", executable=False)
    before = (path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    inspect(path)
    after = (path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode), path.read_bytes())
    assert before == after
    assert before[1] == 0o644


def test_symlinked_source_is_resolved(tmp_path, inspect):
    real = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    link = tmp_path / "link.AppImage"
    link.symlink_to(real)
    assert inspect(link, compute_hash=False).path == real.resolve()


# =============================================================================================
# errors
# =============================================================================================


def assert_no_leftovers(tmp_dir: Path):
    assert list(tmp_dir.iterdir()) == []


def test_truncated_download(tmp_path, private_tempdir):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    data = path.read_bytes()
    for cut in (len(data) - 5000, len(build_runtime(dynamic_interp=True, upd_info="")) + 100, 2000):
        broken = tmp_path / f"cut-{cut}.AppImage"
        broken.write_bytes(data[:cut])
        with pytest.raises(EasyInstallerError) as exc:
            inspect_appimage(broken)
        assert isinstance(exc.value, (ExtractionError, NotAnAppImageError))
        assert "damaged or incomplete" in str(exc.value)
    assert_no_leftovers(private_tempdir)


def test_runtime_only_without_payload(tmp_path, private_tempdir):
    path = tmp_path / "runtime-only.AppImage"
    path.write_bytes(build_runtime(upd_info=""))
    with pytest.raises(ExtractionError) as exc:
        inspect_appimage(path)
    assert "damaged or incomplete" in str(exc.value)
    assert_no_leftovers(private_tempdir)


def test_corrupted_payload(tmp_path, private_tempdir):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    data = bytearray(path.read_bytes())
    offset = len(build_runtime(dynamic_interp=True, upd_info=""))
    data[offset + 96:] = b"\xaa" * (len(data) - offset - 96)
    path.write_bytes(bytes(data))
    with pytest.raises(ExtractionError) as exc:
        inspect_appimage(path)
    assert str(exc.value) == "The file seems damaged or incomplete. Try downloading it again."
    assert_no_leftovers(private_tempdir)


PROGRAM_NOT_APPIMAGE = ("This is a program, but not an AppImage. If the app came as a .zip or "
                        ".tar archive, install that archive instead.")


@pytest.mark.parametrize("content, message", [
    (b"#!/bin/sh\necho hi\n", PROGRAM_NOT_APPIMAGE),
    (b"\x89PNG\r\n\x1a\n....", "This file is not an AppImage."),
])
def test_not_an_appimage(tmp_path, content, message, private_tempdir):
    path = tmp_path / "file.AppImage"
    path.write_bytes(content)
    with pytest.raises(NotAnAppImageError) as exc:
        inspect_appimage(path)
    assert str(exc.value) == message
    assert_no_leftovers(private_tempdir)


def test_plain_elf_is_not_an_appimage(tmp_path):
    """PORT-11: e.g. the program a beginner unpacked from an app's archive."""
    path = tmp_path / "program"
    path.write_bytes(build_runtime(appimage_type=None) + b"\0" * 100)
    with pytest.raises(NotAnAppImageError) as exc:
        inspect_appimage(path)
    assert str(exc.value) == PROGRAM_NOT_APPIMAGE


@pytest.mark.parametrize("name, content, kind", [
    ("App-1.0-linux.tar.zst", b"\x28\xb5\x2f\xfd" + b"\0" * 60, ".tar.zst"),
    ("anki-25.02-linux.tar.zst", b"\x28\xb5\x2f\xfd" + b"\0" * 60, ".tar.zst"),
    ("download", b"7z\xbc\xaf\x27\x1c" + b"\0" * 60, ".7z"),
    ("App.rar", b"Rar!\x1a\x07\x00" + b"\0" * 60, ".rar"),
])
def test_archives_that_cannot_be_opened_say_so(tmp_path, name, content, kind):
    path = tmp_path / name
    path.write_bytes(content)
    with pytest.raises(NotAnAppImageError) as exc:
        inspect_appimage(path)
    assert str(exc.value).startswith(f"Easy Installer cannot open this kind of file ({kind}) yet.")


def test_a_folder_says_what_to_install_instead(tmp_path):
    (tmp_path / "UVtools_linux-x64_v6.2.0").mkdir()
    with pytest.raises(NotAnAppImageError) as exc:
        inspect_appimage(tmp_path / "UVtools_linux-x64_v6.2.0")
    assert str(exc.value) == ("This is a folder, not an app file. If the app came as a .zip or "
                              ".tar archive, install that archive instead.")


def test_missing_directory_and_empty(tmp_path):
    for target in (tmp_path / "missing.AppImage", tmp_path):
        with pytest.raises(NotAnAppImageError):
            inspect_appimage(target)
    empty = tmp_path / "empty.AppImage"
    empty.touch()
    with pytest.raises(NotAnAppImageError) as exc:
        inspect_appimage(empty)
    assert "empty" in str(exc.value)


def test_symlink_loop_path(tmp_path):
    loop = tmp_path / "loop.AppImage"
    loop.symlink_to(loop)
    with pytest.raises(NotAnAppImageError):
        inspect_appimage(loop)


# =============================================================================================
# fallback reader (no unsquashfs): the AppImage runtime is emulated by a script
# =============================================================================================


def test_fallback_reader_gives_same_result(tmp_path, inspect, monkeypatch):
    path = make_sample_appimage(tmp_path / T3_FILE, "t3code")
    script = make_runtime_emulator(tmp_path, path)
    monkeypatch.setattr(inspector, "open_payload",
                        lambda p, elf: ExtractCommandReader(p, command=[str(script)]))
    info = inspect(path, compute_hash=False)
    assert info.app_id == "t3code"
    assert info.name == "T3 Code (Alpha)"
    assert info.version == "0.0.42"
    assert icon_rel(info) == f"{HICOLOR}/512x512/apps/t3code.png"
    assert info.icon_info == ImageInfo("png", 512, 512)
    assert info.is_electron is True   # via chrome-sandbox
    assert info.exec_has_no_sandbox is True


def test_fallback_reader_icon_priority_and_symlinks(tmp_path, inspect, monkeypatch):
    files = {"testapp.desktop": desktop(icon="testapp"), "usr/share/pixmaps/testapp.xpm": make_xpm(16, 16),
             "real/icon.png": make_png(24, 24)}
    path = app_with(tmp_path, files, {".DirIcon": "real/icon.png", "evil": "/etc/passwd"})
    script = make_runtime_emulator(tmp_path, path)
    monkeypatch.setattr(inspector, "open_payload",
                        lambda p, elf: ExtractCommandReader(p, command=[str(script)]))
    info = inspect(path, compute_hash=False)
    assert icon_rel(info) == "usr/share/pixmaps/testapp.xpm"
    assert info.icon_info == ImageInfo("xpm", 16, 16)
    assert info.is_electron is False


# =============================================================================================
# real AppImages on this machine (read-only)
# =============================================================================================

EXPECTED_REAL = {
    "OpenSCAD-2026.03.28-x86_64.AppImage": dict(
        app_id="openscad", name="OpenSCAD", version="2026.03.28", icon="png", electron=False, no_sandbox=False),
    "T3-Code-0.0.42-x86_64.AppImage": dict(
        app_id="t3code", name="T3 Code (Alpha)", version="0.0.42", icon="png", electron=True, no_sandbox=True),
    "winboat-0.9.0-x86_64.AppImage": dict(
        app_id="winboat", name="winboat", version="0.9.0", icon="svg", electron=True, no_sandbox=True),
    "Anytype-0.55.4.AppImage": dict(
        app_id="anytype", name="Anytype", version="0.55.4", icon="png", electron=True, no_sandbox=False),
    "Pen-linux-x86_64.AppImage": dict(
        app_id="pen", name="Pen", version="1.2.8", icon="png", electron=True, no_sandbox=True),
    "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage": dict(
        app_id="org.freecad.FreeCAD", name="FreeCAD", version="1.1.3", icon="svg", electron=False,
        no_sandbox=False),
}


@pytest.mark.real
def test_real_appimages(real_appimages, private_tempdir):
    for path in real_appimages:
        expected = EXPECTED_REAL.get(path.name)
        before = path.stat()
        big = before.st_size > 300 * 1024 * 1024
        with inspect_appimage(path, compute_hash=not big) as info:
            assert info.path == path.resolve()
            assert info.appimage_type == 2
            assert info.arch == "x86_64"
            assert info.desktop_entry is not None
            assert info.icon_path is not None and info.icon_path.is_file()
            assert info.icon_info is not None and info.icon_info.format in ("png", "svg")
            assert info.warnings == []
            assert (info.sha256 is None) == big
            if expected:
                assert info.app_id == expected["app_id"], path.name
                assert info.name == expected["name"]
                assert info.version == expected["version"]
                assert info.icon_info.format == expected["icon"]
                assert info.is_electron is expected["electron"]
                assert info.exec_has_no_sandbox is expected["no_sandbox"]
            if path.name.startswith("FreeCAD"):
                assert info.update_info and info.update_info.startswith("gh-releases-zsync|FreeCAD")
                assert icon_rel(info).endswith("org.freecad.FreeCAD.svg")
        after = path.stat()
        assert (before.st_mtime_ns, before.st_size, before.st_mode) == (after.st_mtime_ns, after.st_size,
                                                                         after.st_mode)
    assert_no_leftovers(private_tempdir)
