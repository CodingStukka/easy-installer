"""Keep both: a second version of an app installed next to the first one, as a separate app."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import installer, privileged, variants
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    execute_install,
    plan_install,
    uninstall,
    variant_id,
)
from easy_installer.core.integration import (
    ID_RE,
    backup_file_name,
    version_label,
    version_slug,
    with_name_suffix,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.system_checks import SystemStatus
from easy_installer.helper import ops

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs

LAUNCHER = "/usr/bin/easy-installer"
KEEP_BOTH = InstallOptions(keep_both=True)


def make_status(**overrides) -> SystemStatus:
    base = dict(
        unsquashfs=shutil.which("unsquashfs"), pkexec="/usr/bin/pkexec",
        apparmor_parser="/usr/sbin/apparmor_parser", update_desktop_database=None,
        icon_cache_tool=None, desktop_file_validate=None, libfuse2=True,
        fusermount="/usr/bin/fusermount3", dev_fuse=True, userns_restricted=False,
        apparmor_enabled=True, distro_id="zorin", distro_like=("ubuntu", "debian"),
        distro_version="18", has_apt=True, libfuse2_package="libfuse2t64",
    )
    base.update(overrides)
    return SystemStatus(**base)


@pytest.fixture(autouse=True)
def quiet_system(monkeypatch):
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def infos():
    created = []

    def _inspect(path, **kw):
        info = inspect_appimage(path, **kw)
        created.append(info)
        return info

    yield _inspect
    for info in created:
        info.cleanup()


def cad(path: Path, version: str | None, *, tag: str = "", name: str = "Free CAD (Beta)") -> Path:
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\nName[de]=Freies CAD\n"
               "Name[nl]=Vrije CAD\nGenericName=CAD\nGenericName[de]=CAD-Programm\n"
               "Comment=Draw things\nExec=AppRun --single %F\nIcon=cad\nCategories=Graphics;\n"
               "Actions=new;\n\n[Desktop Action new]\nName=New Drawing\nExec=AppRun --new\n")
    if version:
        desktop = desktop.replace("Categories=", f"X-AppImage-Version={version}\nCategories=")
    return make_fake_appimage(path, {
        "org.example.Cad.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "payload.bin": f"cad {version} {tag}".encode() * 4,
        "usr/share/icons/hicolor/64x64/apps/cad.png": make_png(64, 64),
    })


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def launcher(app: InstalledApp) -> DesktopEntry:
    return DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))


MAIN = "org.example.Cad"


# ------------------------------------------------------------------------------------------------
# ids and names (no AppImages needed)
# ------------------------------------------------------------------------------------------------


def test_variant_id():
    assert variant_id("org.freecad.FreeCAD", "1.1.3", None) == "org.freecad.FreeCAD--1.1.3"
    assert variant_id("t3code", "v0.0.42", "ab" * 32) == "t3code--0.0.42"
    assert variant_id("demo", "2.0 beta/1 (nightly)", None) == "demo--2.0-beta-1-nightly"
    assert variant_id("demo", None, "3A853EB69EE595F7" + "0" * 48) == "demo--3a853eb6"
    assert variant_id("demo", "", "0123456789abcdef") == "demo--01234567"
    assert variant_id("demo", "…", None) == "demo--copy"            # nothing usable at all
    assert variant_id("demo", "1." + "9" * 80, None) == "demo--1." + "9" * 30
    assert variant_id is variants.variant_id is installer.variant_id

    # always a valid id, also for the longest ids; different long ids never collide
    long_a, long_b = "a" * 128, "a" * 127 + "b"
    ids = {variant_id(base, version, sha)
           for base in ("x", "org.example.App", long_a, long_b, "A-._z" * 25)
           for version in (None, "1", "1.0.0-rc.1", "ü", "../../etc", " ", "1\n2", "%f", "-.-")
           for sha in (None, "f" * 64, "not hex!")}
    assert all(ID_RE.fullmatch(i) and len(i) <= 128 and "--" in i for i in ids)
    assert variant_id(long_a, "1.0", None) != variant_id(long_b, "1.0", None)
    for bad in ("", "../x", "a b", ".hidden", "x" * 129, None, 5):
        with pytest.raises(ValueError):
            variant_id(bad, "1.0", None)   # type: ignore[arg-type]


def test_version_slug_label_and_backup_name():
    assert version_slug("1.2.3") == "1.2.3" and version_slug(None, "ABCDEF0123") == "abcdef01"
    assert version_slug(None) == "copy" and version_slug("  ", "zz") == "copy"
    assert version_label("v1.2 beta") == "1.2 beta"                 # shown to people: as it is
    assert version_label(None, "abcdef0123456789") == "abcdef01"
    assert version_label("1\u2028 2\n3") == "1 2 3"                  # no line breaks in names
    assert version_label("1\x072", "ab" * 32) == "abababab"         # nor control characters
    assert backup_file_name("T3 Code (Alpha)", "0.0.42", None) == "T3-Code-Alpha-0.0.42.AppImage"
    assert backup_file_name("Калькулятор", None, "12345678ff") == "App-12345678.AppImage"


def test_with_name_suffix():
    entry = DesktopEntry.parse(
        "[Desktop Entry]\nName=Foo\nName[de]=Fu\nName[x]=\nGenericName=Tool\nGenericName[de]=Werkzeug\n"
        "Comment=Name=tricky\n\n[Desktop Action a]\nName=Action\n")
    suffixed = with_name_suffix(entry, "1.0")
    assert suffixed is not entry and entry.get("Name") == "Foo"          # a copy
    assert suffixed.get("Name") == "Foo 1.0" and suffixed.get("Name[de]") == "Fu 1.0"
    assert suffixed.get("Name[x]") == ""                                  # empty stays empty
    assert suffixed.get("GenericName") == "Tool" and suffixed.get("GenericName[de]") == "Werkzeug"
    assert suffixed.get("Comment") == "Name=tricky"
    assert suffixed.get("Name", "Desktop Action a") == "Action"           # only the app's name
    assert with_name_suffix(None, "1.0") is None
    assert with_name_suffix(entry, "") is entry
    nameless = DesktopEntry.parse("[Desktop Entry]\nExec=x\n")
    assert with_name_suffix(nameless, "1.0").to_text() == nameless.to_text()


# ------------------------------------------------------------------------------------------------
# planning: when is "keep both" offered, and which entry does a file belong to?
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_keep_both_is_only_offered_for_a_different_file_of_an_installed_app(downloads, infos):
    first = cad(downloads / "Cad-1.0.AppImage", "1.0")
    plan = plan_install(infos(first), KEEP_BOTH)
    assert plan.keep_both_available is False and plan.options.keep_both is False
    assert (plan.app_id, plan.action, plan.base_id, plan.pinned) == (MAIN, "install", None, False)
    assert plan.main_installed is None
    main = execute_install(plan)

    # the installed file itself, or the same bytes again: nothing to keep both of
    for same in (Path(main.appimage_path), shutil.copyfile(main.appimage_path,
                                                           downloads / "again.AppImage")):
        plan = plan_install(infos(same), KEEP_BOTH)
        assert plan.keep_both_available is False and plan.options.keep_both is False
        assert plan.app_id == MAIN and plan.action == "reinstall" and plan.existing == main

    # another version: replacing is the default, keeping both the option
    newer = cad(downloads / "Cad-1.1.AppImage", "1.1")
    plan = plan_install(infos(newer))
    assert plan.keep_both_available is True and plan.options.keep_both is False
    assert (plan.app_id, plan.action, plan.existing, plan.main_installed) == \
        (MAIN, "update", main, main)
    plan = plan_install(infos(newer), KEEP_BOTH)
    assert plan.keep_both_available is True and plan.options.keep_both is True
    assert (plan.app_id, plan.name, plan.action) == (f"{MAIN}--1.1", "Free CAD (Beta) 1.1", "install")
    assert (plan.base_id, plan.pinned, plan.name_suffix) == (MAIN, True, "1.1")
    assert plan.existing is None and plan.main_installed == main
    assert plan.target_appimage == user_layout().apps_dir / "Free-CAD-Beta-1.1.AppImage"
    assert plan.backup_target is None                       # nothing is replaced
    assert not any("Another app called" in w for w in plan.warnings)

    # installed in the other scope only: not "installed in this scope"
    assert plan_install(infos(newer), InstallOptions(scope=Scope.SYSTEM, keep_both=True)
                        ).keep_both_available is False


@requires_mksquashfs
@requires_unsquashfs
def test_keep_both_installs_a_separate_pinned_app(downloads, infos, monkeypatch):
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: LAUNCHER)
    layout = user_layout()
    main = install(cad(downloads / "Cad-1.0-x86_64.AppImage", "1.0"))
    main_files = {p: Path(p).read_bytes()
                  for p in (main.appimage_path, main.desktop_path, *main.icon_paths)}

    newer = cad(downloads / "Cad-2.0-x86_64.AppImage", "2.0")
    new_sha = sha256(newer)
    copy = execute_install(plan_install(infos(newer), KEEP_BOTH))

    vid = f"{MAIN}--2.0"
    assert (copy.id, copy.name, copy.version) == (vid, "Free CAD (Beta) 2.0", "2.0")
    assert (copy.base_id, copy.pinned, copy.main_id) == (MAIN, True, MAIN)
    assert copy.appimage_path == str(layout.apps_dir / "Free-CAD-Beta-2.0.AppImage")
    assert sha256(copy.appimage_path) == new_sha == copy.sha256 and not newer.exists()
    assert copy.desktop_path == str(layout.desktop_dir / f"easyinstaller-{vid}.desktop")
    assert copy.icon_name == f"easyinstaller-{vid}"
    assert [Path(p).name for p in copy.icon_paths] == [f"easyinstaller-{vid}.png"]
    assert copy.previous is None and copy.status() == "ok" and copy.scope is Scope.USER

    entry = launcher(copy)
    assert entry.get("Name") == "Free CAD (Beta) 2.0"
    assert entry.get("Name[de]") == "Freies CAD 2.0" and entry.get("Name[nl]") == "Vrije CAD 2.0"
    assert entry.get("GenericName") == "CAD" and entry.get("GenericName[de]") == "CAD-Programm"
    assert split_exec(entry.get("Exec")) == [copy.appimage_path, "--single", "%F"]
    assert entry.get("TryExec") == copy.appimage_path
    assert entry.get("Icon") == f"easyinstaller-{vid}" and entry.get("X-EasyInstaller-Id") == vid
    assert entry.get("X-AppImage-Version") == "2.0"
    assert entry.get_list("Actions") == ["new", "easyinstaller-uninstall"]
    assert split_exec(entry.get("Exec", "Desktop Action new")) == [copy.appimage_path, "--new"]
    assert entry.get("Name", "Desktop Action new") == "New Drawing"
    assert split_exec(entry.get("Exec", "Desktop Action easyinstaller-uninstall")) == \
        [LAUNCHER, "--uninstall", vid]

    # the installed version is untouched - files, launcher, icon and registry entry
    assert registry().get(MAIN) == main
    assert {p: Path(p).read_bytes() for p in main_files} == main_files
    assert launcher(main).get("Name") == "Free CAD (Beta)"
    assert not layout.backups_dir.exists()
    assert [a.id for a in registry().all()] == [MAIN, vid]
    assert sorted(p.name for p in layout.apps_dir.iterdir()) == ["Free-CAD-Beta-2.0.AppImage",
                                                                 "Free-CAD-Beta.AppImage"]


@requires_mksquashfs
@requires_unsquashfs
def test_matching_rules(downloads, infos):
    main = install(cad(downloads / "Cad-1.0.AppImage", "1.0"))
    copy_file = cad(downloads / "Cad-2.0.AppImage", "2.0")
    shutil.copyfile(copy_file, downloads / "Cad-2.0-downloaded-again.AppImage")
    copy = install(copy_file, KEEP_BOTH)
    vid = copy.id

    # 1. the copy's own installed file (repair): that copy, in place
    plan = plan_install(infos(Path(copy.appimage_path)), InstallOptions(keep_original=True))
    assert (plan.app_id, plan.action, plan.in_place, plan.existing) == (vid, "reinstall", True, copy)
    assert (plan.base_id, plan.pinned, plan.name, plan.keep_both_available) == \
        (MAIN, True, "Free CAD (Beta) 2.0", False)
    Path(copy.desktop_path).unlink()
    repaired = execute_install(plan)
    assert repaired.id == vid and repaired.pinned and repaired.base_id == MAIN
    assert launcher(repaired).get("Name[de]") == "Freies CAD 2.0"
    copy = repaired

    # 2. the same bytes again (sha256): a reinstall of that copy, never an "update" of the main app
    plan = plan_install(infos(downloads / "Cad-2.0-downloaded-again.AppImage"))
    assert (plan.app_id, plan.action, plan.existing) == (vid, "reinstall", copy)
    assert plan.keep_both_available is False and plan.target_appimage == Path(copy.appimage_path)
    again = execute_install(plan)
    assert (again.id, again.base_id, again.pinned) == (vid, MAIN, True)
    assert registry().get(MAIN) == main and len(registry().load()) == 2

    # 3. any other file is compared with the MAIN installation
    plan = plan_install(infos(cad(downloads / "Cad-1.5.AppImage", "1.5")))
    assert (plan.app_id, plan.action, plan.existing) == (MAIN, "update", main)
    assert plan.keep_both_available is True
    plan = plan_install(infos(cad(downloads / "Cad-0.9.AppImage", "0.9")))
    assert (plan.app_id, plan.action) == (MAIN, "downgrade")
    assert any("newer version (1.0)" in w for w in plan.warnings)   # compared with 1.0, not 2.0
    # ... a third version can be kept as well
    third = execute_install(plan_install(infos(downloads / "Cad-0.9.AppImage"), KEEP_BOTH))
    assert third.id == f"{MAIN}--0.9" and sorted(registry().load()) == [MAIN, third.id, vid]

    # another build of a kept version replaces that copy (one copy per version)
    rebuilt = cad(downloads / "Cad-2.0-rebuilt.AppImage", "2.0", tag="rebuilt")
    plan = plan_install(infos(rebuilt), KEEP_BOTH)
    assert (plan.app_id, plan.action, plan.existing) == (vid, "reinstall", again)
    assert plan.backup_target is not None
    replaced = execute_install(plan)
    assert replaced.sha256 == sha256(replaced.appimage_path) != again.sha256
    assert replaced.previous["sha256"] == again.sha256
    assert registry().get(MAIN) == main

    # updating the main app leaves the copies alone
    updated = install(downloads / "Cad-1.5.AppImage")
    assert updated.id == MAIN and updated.version == "1.5" and not updated.pinned
    assert registry().get(vid) == replaced and registry().get(third.id) == third


@requires_mksquashfs
@requires_unsquashfs
def test_copy_of_an_app_without_a_version(downloads, infos):
    install(cad(downloads / "cad-latest.AppImage", None))
    other = cad(downloads / "cad-nightly.AppImage", None, tag="nightly")
    digest = sha256(other)
    plan = plan_install(infos(other, compute_hash=False), KEEP_BOTH)   # even without the hash
    assert plan.app_id == f"{MAIN}--{digest[:8]}" and plan.name == f"Free CAD (Beta) {digest[:8]}"
    copy = execute_install(plan)
    assert copy.version is None and copy.sha256 == digest
    assert Path(copy.appimage_path).name == f"Free-CAD-Beta-{digest[:8]}.AppImage"
    assert launcher(copy).get("Name[nl]") == f"Vrije CAD {digest[:8]}"
    # found again by its content
    plan = plan_install(infos(shutil.copyfile(copy.appimage_path, downloads / "x.AppImage")))
    assert plan.app_id == copy.id and plan.name == copy.name and plan.action == "reinstall"


@requires_mksquashfs
@requires_unsquashfs
def test_uninstalling_one_keeps_the_other(downloads, infos):
    layout = user_layout()
    main = install(cad(downloads / "Cad-1.0.AppImage", "1.0"))
    copy = install(cad(downloads / "Cad-2.0.AppImage", "2.0"), KEEP_BOTH)
    uninstall(MAIN, Scope.USER)
    assert registry().get(copy.id) == copy and copy.status() == "ok"
    assert sorted(p.name for p in layout.apps_dir.iterdir()) == ["Free-CAD-Beta-2.0.AppImage"]
    assert not Path(main.desktop_path).exists() and Path(copy.desktop_path).is_file()

    # Only a copy is left: a new file is a plain installation of the app itself again.
    plan = plan_install(infos(cad(downloads / "Cad-3.0.AppImage", "3.0")), KEEP_BOTH)
    assert (plan.app_id, plan.action, plan.keep_both_available) == (MAIN, "install", False)
    assert plan.existing is None and plan.main_installed is None
    new_main = execute_install(plan)
    assert new_main.base_id is None and not new_main.pinned
    uninstall(copy.id, Scope.USER)
    assert registry().get(MAIN) == new_main and Path(new_main.appimage_path).is_file()
    assert [a.id for a in registry().all()] == [MAIN]


# ------------------------------------------------------------------------------------------------
# system scope: the copy's id, name, base_id and pinned travel in the manifest
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper(sys_layout, monkeypatch):
    calls: list[tuple[str, dict]] = []

    def run_helper(op, payload, *, timeout=600):
        request = json.loads(json.dumps(payload))
        calls.append((op, request))
        if op == "install":
            return ops.op_install(request, layout=sys_layout, run_commands=False,
                                  caller_uid=os.getuid(), caller_gid=os.getgid())
        if op == "uninstall":
            return ops.op_uninstall(request, layout=sys_layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    return calls


@requires_mksquashfs
@requires_unsquashfs
def test_keep_both_for_everyone(downloads, infos, sys_layout, helper):
    system = InstallOptions(scope=Scope.SYSTEM)
    main = install(cad(downloads / "Cad-1.0.AppImage", "1.0"), system)
    assert helper[-1][1]["base_id"] is None and helper[-1][1]["pinned"] is False
    main_launcher = Path(main.desktop_path).read_bytes()

    plan = plan_install(infos(cad(downloads / "Cad-2.0.AppImage", "2.0")),
                        InstallOptions(scope=Scope.SYSTEM, keep_both=True))
    vid = f"{MAIN}--2.0"
    assert plan.keep_both_available and plan.app_id == vid and plan.requires_root
    assert plan.target_appimage == sys_layout.apps_dir / "Free-CAD-Beta-2.0.AppImage"
    copy = execute_install(plan)

    op, manifest = helper[-1]
    assert op == "install"
    assert (manifest["app_id"], manifest["name"]) == (vid, "Free CAD (Beta) 2.0")
    assert (manifest["base_id"], manifest["pinned"]) == (MAIN, True)
    assert "Name[de]=Freies CAD 2.0" in manifest["embedded_desktop"]

    assert (copy.id, copy.name, copy.base_id, copy.pinned) == (vid, "Free CAD (Beta) 2.0", MAIN, True)
    assert copy.scope is Scope.SYSTEM and copy.appimage_path == str(plan.target_appimage)
    assert Path(copy.desktop_path).read_text(encoding="utf-8") == plan.desktop_text
    entry = launcher(copy)
    assert entry.get("Name") == "Free CAD (Beta) 2.0" and entry.get("Name[nl]") == "Vrije CAD 2.0"
    assert entry.get("X-EasyInstaller-Id") == vid and entry.get("X-EasyInstaller-Scope") == "system"
    assert registry(sys_layout).get(vid) == copy
    # the main installation is untouched and no backup was made
    assert registry(sys_layout).get(MAIN) == main
    assert Path(main.desktop_path).read_bytes() == main_launcher
    assert sorted(p.name for p in sys_layout.apps_dir.iterdir()) == [
        "Free-CAD-Beta-2.0.AppImage", "Free-CAD-Beta.AppImage"]

    # the same file again: a reinstall of the copy, still pinned
    plan = plan_install(infos(shutil.copyfile(copy.appimage_path, downloads / "again.AppImage")),
                        system)
    assert (plan.app_id, plan.action, plan.base_id, plan.pinned) == (vid, "reinstall", MAIN, True)
    again = execute_install(plan)
    assert (again.base_id, again.pinned, again.previous) == (MAIN, True, None)

    uninstall(MAIN, Scope.SYSTEM)
    assert [a.id for a in registry(sys_layout).all()] == [vid]
    assert [p.name for p in sys_layout.apps_dir.iterdir()] == ["Free-CAD-Beta-2.0.AppImage"]
