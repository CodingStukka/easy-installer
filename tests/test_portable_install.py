"""Portable apps (unpacked archives) in the installer: install, replace, go back, repair,
uninstall - and above all what must never be deleted.

A portable app is a whole folder in the apps folder. Easy Installer removes or replaces such a
folder only if it lies directly in the apps folder, is a real folder (no link), is recorded in
the registry AND carries the marker file with the app's id. The hostile cases below tamper
with each of these and check that nothing but the app's own folder is ever touched.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import backups, installer, portable
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    drop_backup,
    execute_install,
    plan_install,
    plan_repair,
    plan_rollback,
    portable_dir_state,
    portable_info_from_folder,
    prune_backups,
    read_portable_marker,
    repair,
    rollback,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.portable import PortableInfo, inspect_portable
from easy_installer.core.reconcile import needs_reconcile, reconcile_all, reconcile_app
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import SandboxFix
from easy_installer.core.settings import Settings, save_settings
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import (
    ArchiveError,
    InstallError,
    NotInstalledError,
    UnsupportedArchitectureError,
)

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs
from fakearchive import (
    FOREIGN_MACHINE,
    blender_tree,
    electron_tree,
    fake_elf,
    fake_script,
    flat_tree,
    jetbrains_tree,
    make_archive,
    make_tar,
    make_zip,
)

MARKER = ".easy-installer.json"
BACKUPS = ".easyinstaller-backups"


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


@pytest.fixture(autouse=True)
def sys_layout(system_root, monkeypatch):
    """The "for everyone" places live in a temporary root (never the real /opt, /var/lib)."""
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def apps() -> Path:
    return user_layout().apps_dir


def blender(downloads: Path, version: str = "4.2.0", suffix: str = ".tar.gz", **extra) -> Path:
    tree = blender_tree(f"blender-{version}-linux-x64")
    top = f"blender-{version}-linux-x64"
    tree["files"][f"{top}/release-{version}.txt"] = f"Blender {version}\n"
    for name, content in extra.items():
        tree["files"][f"{top}/{name}"] = content
    return make_archive(downloads / f"blender-{version}-linux-x64{suffix}", **tree)


def install(archive: Path, options: InstallOptions | None = None, *,
            executable: str | None = None) -> InstalledApp:
    with inspect_portable(archive) as info:
        if executable:
            info.executable = executable
        return execute_install(plan_install(info, options))


def registry() -> Registry:
    return Registry(user_layout().registry_path)


def entry_of(app: InstalledApp) -> DesktopEntry:
    return DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))


def tree(root: Path) -> dict[str, str]:
    """Everything below ``root``: relative path -> "dir" | "link -> target" | sha256 of a file."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            found[rel] = "link -> " + os.readlink(path)
        elif path.is_dir():
            found[rel] = "dir"
        else:
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def everything(home: Path) -> dict[str, str]:
    """The whole fake home, minus lock files and the file that records times."""
    return {rel: what for rel, what in tree(home).items() if not rel.endswith(".lock")}


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ------------------------------------------------------------------------------------------------
# installing
# ------------------------------------------------------------------------------------------------


def test_plan_describes_the_installation_and_changes_nothing(downloads, apps, isolated_env):
    archive = blender(downloads)
    before = everything(isolated_env)
    with inspect_portable(archive) as info:
        plan = plan_install(info)
        assert plan.kind == "portable" and plan.portable is info and plan.info is info
        assert plan.app_id == "blender" and plan.name == "Blender" and plan.version == "4.2.0"
        assert plan.action == "install" and plan.existing is None
        assert plan.install_dir == apps / "Blender"
        assert plan.target_appimage == apps / "Blender" / "blender"
        assert plan.desktop_path == user_layout().desktop_dir / "easyinstaller-blender.desktop"
        assert plan.requires_root is False and plan.in_place is False
        assert plan.keep_both_available is False and plan.backup_target is None
        assert plan.sandbox_fix is SandboxFix.NONE and plan.extract_and_run is False
        assert plan.options.keep_original is True     # the archive is not the app: it stays
        assert plan.source_dir is None and plan.mime_types == []
        assert f"Exec={apps}/Blender/blender %f" in plan.desktop_text
    assert everything(isolated_env) == before


def test_install_from_a_tar_archive(downloads, apps):
    archive = blender(downloads)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    steps = []
    with inspect_portable(archive) as info:
        plan = plan_install(info)
        app = execute_install(plan, progress=lambda fraction, text: steps.append(fraction))
        tree_size, file_count = info.tree_size, info.file_count

    folder = apps / "Blender"
    assert app.kind == "portable" and app.install_dir == str(folder)
    assert app.appimage_path == app.exec_path == str(folder / "blender")
    assert (app.id, app.name, app.version, app.scope) == ("blender", "Blender", "4.2.0", Scope.USER)
    assert app.sha256 == sha and app.size == tree_size > 0 and app.arch == "x86_64"
    assert app.original_filename == archive.name and app.update_info is None
    assert (app.base_id, app.pinned, app.previous, app.update_source) == (None, False, None, None)
    assert app.sandbox_fix == "none" and app.extract_and_run is False and app.apparmor_profile is None
    assert app.data_hints == ["Blender", "blender-updater"]
    assert app.status() == "ok" and registry().get("blender") == app

    # the app folder: everything of the archive (without its top folder), the program startable
    assert os.access(folder / "blender", os.X_OK)
    assert (folder / "release-4.2.0.txt").read_text() == "Blender 4.2.0\n"
    assert not (folder / "blender-4.2.0-linux-x64").exists()
    assert os.stat(folder).st_mode & 0o777 == 0o755
    # the marker says whose folder this is, and what the archive told about the app
    marker = json.loads((folder / MARKER).read_text())
    assert marker["format"] == 1 and marker["id"] == "blender" and marker["executable"] == "blender"
    assert (marker["name"], marker["version"], marker["sha256"]) == ("Blender", "4.2.0", sha)
    assert marker["original_filename"] == archive.name and marker["icon"] == ".easy-installer-icon.svg"
    assert (marker["tree_size"], marker["file_count"]) == (tree_size, file_count)
    assert "Exec=blender %f" in marker["desktop"]
    assert read_portable_marker(folder) == marker
    assert portable_dir_state(user_layout(), app, registry()) == ("ok", "")

    # the launcher starts the program in its folder, with the app's own arguments
    entry = entry_of(app)
    assert split_exec(entry.get("Exec")) == [str(folder / "blender"), "%f"]
    assert entry.get("TryExec") == str(folder / "blender") and entry.get("Path") == str(folder)
    assert entry.get("Name") == "Blender" and entry.get("Icon") == "easyinstaller-blender"
    assert entry.get("X-EasyInstaller-Id") == "blender" and entry.get("X-AppImage-Version") == "4.2.0"
    assert entry.get("X-EasyInstaller-Scope") == "user"
    assert [Path(p).name for p in app.icon_paths] == ["easyinstaller-blender.svg"]
    assert Path(app.icon_paths[0]).is_file()

    # the archive is kept, nothing else is left in the apps folder
    assert archive.is_file() and names(apps) == ["Blender"]
    fractions = [f for f in steps if f is not None]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0


@pytest.mark.parametrize("suffix", [".zip", ".tar.xz", ".tar"])
def test_install_from_other_archive_kinds(downloads, apps, suffix):
    app = install(blender(downloads, suffix=suffix))
    assert app.version == "4.2.0" and os.access(apps / "Blender" / "blender", os.X_OK)
    assert names(apps) == ["Blender"]


def test_the_archive_is_deleted_only_when_asked(downloads, apps):
    archive = blender(downloads)
    install(archive, InstallOptions(keep_original=True))
    assert archive.is_file()
    install(archive, InstallOptions(keep_original=False))
    assert not archive.exists() and (apps / "Blender" / "blender").is_file()


def test_a_flat_archive_gets_a_folder_named_after_the_app(downloads, apps):
    archive = make_zip(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    app = install(archive)
    assert app.install_dir == str(apps / "UVtools") and app.version == "6.2.0"
    assert app.appimage_path == str(apps / "UVtools" / "UVtools.sh")
    assert app.icon_paths == [] and app.icon_name == "application-x-executable"
    entry = entry_of(app)
    assert entry.get("Icon") == "application-x-executable"
    assert split_exec(entry.get("Exec")) == [app.appimage_path]
    assert (apps / "UVtools" / "Assets" / "PrusaSlicer" / "printer" / "Some Printer.ini").is_file()


def test_another_program_of_the_archive_can_be_chosen(downloads, apps):
    """--executable / the "Program to start" row: the launcher starts what was chosen; the
    arguments of the app's own menu entry are only kept for the program it names."""
    app = install(blender(downloads), executable="blender-launcher")
    assert app.appimage_path == str(apps / "Blender" / "blender-launcher")
    entry = entry_of(app)
    assert split_exec(entry.get("Exec")) == [app.appimage_path]      # no "%f" of "Exec=blender %f"
    assert entry.get("TryExec") == app.appimage_path and entry.get("Name") == "Blender"
    assert json.loads((apps / "Blender" / MARKER).read_text())["executable"] == "blender-launcher"
    assert "blender-launcher" in app.data_hints


@pytest.mark.parametrize("bad", ["", "/bin/sh", "../../bin/sh", "bin/../../x", "a//b", "./blender",
                                 "blender\n", None])
def test_an_unusable_program_path_is_refused_when_planning(downloads, apps, bad):
    with inspect_portable(blender(downloads)) as info:
        info.executable = bad
        with pytest.raises(InstallError, match="program that starts this app"):
            plan_install(info)
    assert not apps.exists()


def test_a_program_that_is_not_in_the_archive(downloads, apps, isolated_env):
    with inspect_portable(blender(downloads)) as info:
        info.executable = "bin/missing"
        with pytest.raises(InstallError, match="not found in the archive"):
            plan_install(info)           # (CLI-5: before anything is shown or asked)
        info.__dict__.pop("_members")    # not known: unpacking finds out
        plan = plan_install(info)
        with pytest.raises(ArchiveError, match="not found in the archive"):
            execute_install(plan)
    assert names(apps) == [] and registry().all() == []
    assert not (user_layout().desktop_dir / "easyinstaller-blender.desktop").exists()


def test_a_program_path_with_spaces(downloads, apps):
    archive = make_tar(downloads / "My Tool-1.0.tar.gz", {
        "My Tool/bin/my tool": fake_elf(), "My Tool/README": "x"},
        modes={"My Tool/bin/my tool": 0o755})
    app = install(archive)
    assert app.install_dir == str(apps / "My-Tool")
    assert split_exec(entry_of(app).get("Exec")) == [str(apps / "My-Tool" / "bin" / "my tool")]
    assert entry_of(app).get("Path") == str(apps / "My-Tool")


def test_jetbrains_like_app_gets_its_window_class(downloads, apps):
    app = install(make_archive(downloads / "ideaIU-2024.1.4.tar.gz", **jetbrains_tree()))
    entry = entry_of(app)
    assert app.name == "IntelliJ IDEA" and app.version == "2024.1.4"
    assert split_exec(entry.get("Exec")) == [str(Path(app.install_dir) / "bin" / "idea.sh")]
    assert entry.get("StartupWMClass") == "jetbrains-idea"
    assert "jetbrains-idea" in app.data_hints


# ------------------------------------------------------------------------------------------------
# what portable apps cannot do
# ------------------------------------------------------------------------------------------------


def test_not_for_everyone_and_not_next_to_another_version(downloads, apps, isolated_env):
    archive = blender(downloads)
    before = everything(isolated_env)
    with inspect_portable(archive) as info:
        with pytest.raises(InstallError, match="only be installed just for you") as caught:
            plan_install(info, InstallOptions(scope=Scope.SYSTEM))
        assert "per-user only" in caught.value.details
        with pytest.raises(InstallError, match="cannot be installed next to it"):
            plan_install(info, InstallOptions(keep_both=True))
    assert everything(isolated_env) == before


def test_foreign_architecture(downloads, apps):
    archive = make_tar(downloads / "armtool-1.0.tar.gz", {"armtool/armtool": fake_elf(FOREIGN_MACHINE)},
                       modes={"armtool/armtool": 0o755})
    with inspect_portable(archive) as info:
        assert info.arch not in (None, installer.host_arch())
        with pytest.raises(UnsupportedArchitectureError, match="different kind of computer"):
            plan_install(info)
        app = execute_install(plan_install(info, InstallOptions(allow_foreign_arch=True)))
    assert app.arch == info.arch


def test_an_app_for_another_computer_can_still_go_back_and_be_repaired(downloads, apps):
    """It was installed on purpose: its kept version and its launcher are never refused later."""
    allow = InstallOptions(allow_foreign_arch=True)
    for version in ("1.0", "1.1"):
        install(make_tar(downloads / f"armtool-{version}.tar.gz",
                         {"armtool/armtool": fake_elf(FOREIGN_MACHINE), "armtool/v": version},
                         modes={"armtool/armtool": 0o755}), allow)
    back = rollback("armtool", Scope.USER)
    assert back.version == "1.0" and back.previous["version"] == "1.1"
    assert back.arch not in ("", installer.host_arch())
    assert repair("armtool", Scope.USER).arch == back.arch


def test_electron_app_starts_without_its_sandbox_where_the_computer_blocks_it(downloads, apps,
                                                                              monkeypatch):
    archive = make_zip(downloads / "MyApp-linux-x64-2.1.4.zip", **electron_tree())
    restricted = make_status(userns_restricted=True)      # AppArmor would be available
    with inspect_portable(archive) as info:
        assert info.is_electron
        assert plan_install(info).sandbox_fix is SandboxFix.NONE      # nothing blocks it here
        plan = plan_install(info, status=restricted)
        # a permission (AppArmor profile) is only ever given to AppImages: never the password
        assert plan.sandbox_fix is SandboxFix.NO_SANDBOX and plan.requires_root is False
        assert plan.apparmor_profile_text is None
        assert any("without its built-in security sandbox" in w for w in plan.warnings)
        asked = plan_install(info, InstallOptions(sandbox_fix=SandboxFix.APPARMOR), status=restricted)
        assert asked.sandbox_fix is SandboxFix.NO_SANDBOX and asked.requires_root is False
        none = plan_install(info, InstallOptions(sandbox_fix=SandboxFix.NONE), status=restricted)
        assert none.sandbox_fix is SandboxFix.NONE
        assert any("may not start" in w for w in none.warnings)
        app = execute_install(plan)
    assert app.sandbox_fix == "no-sandbox" and app.apparmor_profile is None
    program = str(apps / "MyApp" / "my-app")
    assert split_exec(entry_of(app).get("Exec")) == [program, "--no-sandbox"]
    # an update keeps that choice
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: restricted)
    newer = make_zip(downloads / "MyApp-linux-x64-2.2.0.zip", **electron_tree())
    assert install(newer).sandbox_fix == "no-sandbox"


def test_no_sandbox_of_a_menu_entry_that_starts_something_else_does_not_count(downloads,
                                                                              apps):
    """PORT-07: the archive's entry has --no-sandbox, but names another program: that entry's
    command is dropped, so the flag must be added again where the computer needs it."""
    tree = electron_tree("Skyapp-linux-x64", program="skyapp")
    tree["files"]["Skyapp-linux-x64/skyapp.desktop"] = (
        "[Desktop Entry]\nType=Application\nName=Skyapp\n"
        "Exec=/usr/bin/skyapp-launcher --no-sandbox %U\n")
    archive = make_archive(downloads / "Skyapp-1.0-linux-x64.tar.gz", **tree)
    restricted = make_status(userns_restricted=True)
    with inspect_portable(archive) as info:
        assert info.exec_has_no_sandbox and info.executable == "skyapp"
        plan = plan_install(info, status=restricted)
        assert plan.sandbox_fix is SandboxFix.NO_SANDBOX
        app = execute_install(plan)
    assert split_exec(entry_of(app).get("Exec")) == [str(apps / "Skyapp" / "skyapp"),
                                                     "--no-sandbox"]


def test_a_program_with_a_percent_sign_gets_a_launcher_that_loads(downloads, apps):
    """PORT-06: GLib refuses ``Exec="…/App 100%%"`` - the program is started through env."""
    archive = make_zip(downloads / "Chrpct-1.0.zip",
                       {"Chrpct/App 100%": fake_elf(), "Chrpct/Other": fake_elf()},
                       modes={"Chrpct/App 100%": 0o755, "Chrpct/Other": 0o755})
    app = install(archive, executable="App 100%")
    program = str(apps / "Chrpct" / "App 100%")
    entry = entry_of(app)
    assert split_exec(entry.get("Exec")) == ["env", program]
    assert entry.get("TryExec") == program
    gi = pytest.importorskip("gi")
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
    loaded = Gio.DesktopAppInfo.new_from_filename(app.desktop_path)
    assert loaded is not None and loaded.get_commandline().startswith("env ")
    assert repair(app.id, Scope.USER).appimage_path == program      # read back as it is


@requires_mksquashfs
@requires_unsquashfs
def test_an_appimage_and_an_archive_of_the_same_app_do_not_replace_each_other(downloads, apps):
    image = make_fake_appimage(downloads / "Blender-4.1.AppImage", {
        "blender.desktop": "[Desktop Entry]\nType=Application\nName=Blender\nExec=AppRun\nIcon=blender\n",
        "AppRun": "#!/bin/sh\n", "blender.png": make_png(32, 32)})
    archive = blender(downloads)
    with inspect_appimage(image) as info:
        installed = execute_install(plan_install(info, InstallOptions(keep_original=True)))
    assert installed.id == "blender" and installed.kind == "appimage"
    with inspect_portable(archive) as info:
        with pytest.raises(InstallError, match="different kind of file"):
            plan_install(info)
    assert registry().get("blender") == installed and names(apps) == ["Blender.AppImage"]

    uninstall("blender", Scope.USER)
    unpacked = install(archive)
    with inspect_appimage(image) as info:
        with pytest.raises(InstallError, match="different kind of file"):
            plan_install(info)
        with pytest.raises(InstallError, match="different kind of file"):
            plan_install(info, InstallOptions(keep_both=True))
    assert registry().get("blender") == unpacked and names(apps) == ["Blender"]


def test_reconcile_leaves_portable_apps_alone(downloads, apps):
    app = install(blender(downloads))
    before = tree(apps)
    (apps / "Blender" / "blender").write_bytes(fake_elf(size=9999))     # the app changed itself
    (apps / "Blender" / "settings.ini").write_text("x")
    changed = tree(apps)
    assert changed != before
    assert needs_reconcile(app) is False
    assert reconcile_app(app).change == "unchanged"
    assert [(r.app.id, r.change) for r in reconcile_all()] == [("blender", "unchanged")]
    assert registry().get("blender") == app and tree(apps) == changed
    shutil.rmtree(apps / "Blender")                                     # even when it is gone
    assert reconcile_app(app).change == "unchanged" and registry().get("blender") == app
    assert app.status() == "missing-appimage"


# ------------------------------------------------------------------------------------------------
# replacing, keeping the old version, going back
# ------------------------------------------------------------------------------------------------


def test_update_swaps_the_folders_and_keeps_the_old_one(downloads, apps):
    v1 = install(blender(downloads, "4.2.0"))
    (apps / "Blender" / "my-notes.txt").write_text("made by the user inside the app folder")
    old_inode = os.stat(apps / "Blender").st_ino
    with inspect_portable(blender(downloads, "4.3.0", ".tar.xz")) as info:
        plan = plan_install(info)
        kept = apps / BACKUPS / "blender" / "Blender-4.2.0"
        assert plan.action == "update" and plan.existing == v1 and plan.backup_target == kept
        assert plan.install_dir == apps / "Blender"                  # the same place
        v2 = execute_install(plan)

    assert v2.version == "4.3.0" and v2.install_dir == v1.install_dir
    assert v2.installed_at == v1.installed_at
    assert (apps / "Blender" / "release-4.3.0.txt").is_file()
    assert not (apps / "Blender" / "release-4.2.0.txt").exists()
    # what the user saved in the app folder went over into the new version's folder ...
    assert (apps / "Blender" / "my-notes.txt").read_text() == \
        "made by the user inside the app folder"
    # ... and the old version is the very same folder, moved (never copied)
    assert v2.previous == {"version": "4.2.0", "path": str(kept), "sha256": v1.sha256,
                           "size": v1.size, "saved_at": v2.previous["saved_at"],
                           "original_filename": "blender-4.2.0-linux-x64.tar.gz"}
    assert kept.is_dir() and os.stat(kept).st_ino == old_inode
    assert not (kept / "my-notes.txt").exists() and (kept / "release-4.2.0.txt").is_file()
    assert read_portable_marker(kept)["version"] == "4.2.0"
    assert read_portable_marker(apps / "Blender")["version"] == "4.3.0"
    assert names(apps) == [BACKUPS, "Blender"] and names(apps / BACKUPS / "blender") == ["Blender-4.2.0"]
    assert entry_of(v2).get("X-AppImage-Version") == "4.3.0"

    # a third version: one backup per app, the older one goes
    v3 = install(blender(downloads, "4.4.0"))
    assert v3.previous["version"] == "4.3.0" and names(apps / BACKUPS / "blender") == ["Blender-4.3.0"]


def test_rollback_swaps_the_two_versions(downloads, apps):
    v1 = install(blender(downloads, "4.2.0"))
    v2 = install(blender(downloads, "4.3.0"), executable="blender-launcher")
    (apps / "Blender" / "new-notes.txt").write_text("4.3")
    Path(v2.desktop_path).write_text("[Desktop Entry]\nName=tampered\n")

    plan = plan_rollback("blender", Scope.USER)
    try:
        assert plan.action == "rollback" and plan.kind == "portable" and plan.consumes_backup
        assert plan.source_dir == apps / BACKUPS / "blender" / "Blender-4.2.0"
        assert plan.version == "4.2.0" and plan.requires_root is False
        assert plan.backup_target == apps / BACKUPS / "blender" / "Blender-4.3.0"
    finally:
        plan.info.cleanup()

    back = rollback("blender", Scope.USER)
    assert back.version == "4.2.0" and back.install_dir == v1.install_dir
    assert back.appimage_path == v1.appimage_path           # the program that version started
    assert back.sha256 == v1.sha256 and back.size == v1.size and back.installed_at == v1.installed_at
    assert (apps / "Blender" / "release-4.2.0.txt").is_file()
    assert split_exec(entry_of(back).get("Exec")) == [str(apps / "Blender" / "blender"), "%f"]
    assert entry_of(back).get("X-AppImage-Version") == "4.2.0"
    assert Path(back.icon_paths[0]).is_file()
    kept = apps / BACKUPS / "blender" / "Blender-4.3.0"
    assert back.previous["version"] == "4.3.0" and back.previous["path"] == str(kept)
    assert (apps / "Blender" / "new-notes.txt").read_text() == "4.3"    # taken along
    assert not (kept / "new-notes.txt").exists() and (kept / "release-4.3.0.txt").is_file()
    assert names(apps / BACKUPS / "blender") == ["Blender-4.3.0"]
    assert registry().get("blender") == back

    # ... and forth again
    forth = rollback("blender", Scope.USER)
    assert forth.version == "4.3.0" and forth.previous["version"] == "4.2.0"
    assert forth.appimage_path == str(apps / "Blender" / "blender-launcher")
    assert (apps / "Blender" / "new-notes.txt").is_file()


def test_update_without_backups_removes_the_old_folder(downloads, apps):
    save_settings(Settings(backup_days=0))
    install(blender(downloads, "4.2.0"))
    (apps / "Blender" / "old-file.txt").write_text("x")
    with inspect_portable(blender(downloads, "4.3.0")) as info:
        plan = plan_install(info)
        assert plan.backup_target is None and plan.keep_backup is False
        v2 = execute_install(plan)
    assert v2.previous is None and names(apps) == ["Blender"]
    assert not (apps / "Blender" / "release-4.2.0.txt").exists()
    assert (apps / "Blender" / "old-file.txt").read_text() == "x"   # the user's: taken over
    with pytest.raises(InstallError, match="no previous version"):
        rollback("blender", Scope.USER)


def test_no_backup_option_drops_the_older_backup_too(downloads, apps):
    install(blender(downloads, "4.2.0"))
    install(blender(downloads, "4.3.0"))
    v3 = install(blender(downloads, "4.4.0"), InstallOptions(keep_backup=False))
    assert v3.previous is None and names(apps) == ["Blender"]


def test_installing_the_same_archive_again_makes_no_backup(downloads, apps):
    install(blender(downloads, "4.2.0"))
    archive = blender(downloads, "4.3.0")
    v2 = install(archive)
    (apps / "Blender" / "saved-by-user.txt").write_text("x")
    (apps / "Blender" / "blender").write_bytes(b"broken")
    with inspect_portable(archive) as info:
        plan = plan_install(info)
        assert plan.action == "reinstall" and plan.backup_target is None
        again = execute_install(plan)
    # a fresh copy of the same version (with what the user saved in the folder); the kept
    # 4.2.0 stays the version to go back to
    assert (apps / "Blender" / "blender").read_bytes() != b"broken"
    assert (apps / "Blender" / "saved-by-user.txt").read_text() == "x"
    assert again.previous == v2.previous and again.previous["version"] == "4.2.0"
    assert names(apps) == [BACKUPS, "Blender"] and names(apps / BACKUPS / "blender") == ["Blender-4.2.0"]


def test_downgrade_is_called_a_downgrade(downloads, apps):
    install(blender(downloads, "4.3.0"))
    with inspect_portable(blender(downloads, "4.2.0")) as info:
        plan = plan_install(info)
        assert plan.action == "downgrade"
        assert any("newer version (4.3.0) is already installed" in w for w in plan.warnings)
        old = execute_install(plan)
    assert old.version == "4.2.0" and old.previous["version"] == "4.3.0"


def test_drop_and_prune_delete_the_kept_folder(downloads, apps):
    install(blender(downloads, "4.2.0"))
    install(blender(downloads, "4.3.0"))
    drop_backup("blender", Scope.USER)
    assert registry().get("blender").previous is None and names(apps) == ["Blender"]

    install(blender(downloads, "4.4.0"))
    assert names(apps / BACKUPS / "blender") == ["Blender-4.3.0"]
    assert prune_backups(14) == []                                       # still young
    pruned = prune_backups(0)
    assert [app.id for app in pruned] == ["blender"] and names(apps) == ["Blender"]
    assert registry().get("blender").previous is None


def test_a_backup_that_cannot_be_made_is_only_a_warning(downloads, apps, monkeypatch):
    v1 = install(blender(downloads, "4.2.0"))

    def no_move(source, target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(backups, "_move", no_move)
    with inspect_portable(blender(downloads, "4.3.0")) as info:
        plan = plan_install(info)
        v2 = execute_install(plan)
    assert v2.version == "4.3.0" and v2.previous is None
    assert any("could not be kept" in warning for warning in plan.warnings)
    assert names(apps) == ["Blender"] and v1.version == "4.2.0"


# ------------------------------------------------------------------------------------------------
# failures change nothing
# ------------------------------------------------------------------------------------------------


def state(home: Path) -> dict[str, str]:
    return everything(home)


def files(home: Path) -> dict[str, str]:
    """Like :func:`state`, without the folders."""
    return {rel: what for rel, what in everything(home).items() if what != "dir"}


def test_an_archive_that_fails_to_unpack_leaves_the_installed_version(downloads, apps, isolated_env,
                                                                     monkeypatch):
    v1 = install(blender(downloads, "4.2.0"))
    before = state(isolated_env)
    newer = blender(downloads, "4.3.0")
    before_with_archive = state(isolated_env)
    with inspect_portable(newer) as info:
        plan = plan_install(info)
        with open(newer, "ab") as fh:                 # the file changes after it was inspected
            fh.write(b"\0" * 4096)
        with pytest.raises(InstallError, match="changed after it was checked"):
            execute_install(plan)
    newer.unlink()
    assert state(isolated_env) == before and registry().get("blender") == v1

    # a failure in the middle of unpacking: the half unpacked folder is removed again
    newer = blender(downloads, "4.3.0")
    real = portable._TreeWriter.make_links

    def fails(self, *args, **kw):
        real(self, *args, **kw)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(portable._TreeWriter, "make_links", fails)
    with inspect_portable(newer) as info:
        with pytest.raises(InstallError, match="free disk space"):
            execute_install(plan_install(info))
    assert state(isolated_env) == before_with_archive and registry().get("blender") == v1


@pytest.mark.parametrize("step", ["_copy_icon", "_write_desktop_file", "registry"])
@pytest.mark.parametrize("keep_backup", [True, False])
def test_a_failure_after_the_swap_puts_the_old_folder_back(downloads, apps, isolated_env,
                                                           monkeypatch, step, keep_backup):
    save_settings(Settings(backup_days=14 if keep_backup else 0))
    v1 = install(blender(downloads, "4.2.0"))
    (apps / "Blender" / "notes.txt").write_text("mine")
    newer = blender(downloads, "4.3.0")
    before = state(isolated_env)
    inode = os.stat(apps / "Blender").st_ino

    def boom(*args, **kw):
        raise OSError(28, "No space left on device")

    if step == "registry":
        monkeypatch.setattr(Registry, "put", boom)
    else:
        monkeypatch.setattr(installer, step, boom)
    with inspect_portable(newer) as info:
        with pytest.raises(InstallError, match="free disk space"):
            execute_install(plan_install(info))
    assert state(isolated_env) == before
    assert os.stat(apps / "Blender").st_ino == inode            # the very folder, moved back
    assert Registry(user_layout().registry_path).load()["blender"] == v1


def test_a_failed_rollback_changes_nothing(downloads, apps, isolated_env, monkeypatch):
    install(blender(downloads, "4.2.0"))
    v2 = install(blender(downloads, "4.3.0"))
    before = state(isolated_env)

    def boom(*args, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(installer, "_write_desktop_file", boom)
    with pytest.raises(InstallError, match="free disk space"):
        rollback("blender", Scope.USER)
    assert state(isolated_env) == before and registry().get("blender") == v2

    # the current version cannot be kept: going back changes nothing either
    monkeypatch.setattr(installer, "_write_desktop_file", lambda path, text: None)
    monkeypatch.setattr(backups, "_move", boom)
    with pytest.raises(InstallError, match="could not be kept, so nothing was changed"):
        rollback("blender", Scope.USER)
    assert state(isolated_env) == before and registry().get("blender") == v2


def test_rollback_without_or_with_an_unusable_backup(downloads, apps, isolated_env):
    v1 = install(blender(downloads, "4.2.0"))
    with pytest.raises(InstallError, match="no previous version of Blender"):
        rollback("blender", Scope.USER)
    with pytest.raises(NotInstalledError):
        rollback("nope", Scope.USER)
    v2 = install(blender(downloads, "4.3.0"))
    kept = Path(v2.previous["path"])

    # the kept folder lost its marker, or carries another app's: it is not used
    marker = (kept / MARKER).read_text()
    for content in (None, marker.replace('"id": "blender"', '"id": "other"'), "[1, 2]", "{"):
        (kept / MARKER).unlink(missing_ok=True)
        if content is not None:
            (kept / MARKER).write_text(content)
        before = state(isolated_env)
        with pytest.raises(InstallError, match="cannot be used any more"):
            rollback("blender", Scope.USER)
        assert state(isolated_env) == before and registry().get("blender") == v2
    # its program is gone
    (kept / MARKER).write_text(marker)
    (kept / "blender").unlink()
    with pytest.raises(InstallError, match="cannot be used any more"):
        rollback("blender", Scope.USER)
    assert registry().get("blender") == v2 and v1.version == "4.2.0"


def test_rollback_keeps_the_current_version_whatever_the_kept_folder_claims(downloads, apps):
    """The marker of the kept folder says it is the very version that is installed (same
    checksum): the installed folder is still kept, never just replaced. What the registry
    recorded about the kept version counts."""
    v1 = install(blender(downloads, "4.2.0"))
    v2 = install(blender(downloads, "4.3.0"))
    (apps / "Blender" / "mine.txt").write_text("current")
    kept = Path(v2.previous["path"])
    marker = json.loads((kept / MARKER).read_text())
    marker.update(sha256=v2.sha256, version="9.9", tree_size=1)
    (kept / MARKER).write_text(json.dumps(marker))

    back = rollback("blender", Scope.USER)
    assert (back.version, back.sha256, back.size) == ("4.2.0", v1.sha256, v1.size)
    assert back.previous["version"] == "4.3.0" and back.previous["sha256"] == v2.sha256
    assert (Path(back.previous["path"]) / "release-4.3.0.txt").is_file()
    assert (apps / "Blender" / "mine.txt").read_text() == "current"    # taken along
    assert names(apps / BACKUPS / "blender") == ["Blender-4.3.0"]

    # ... not even when the registry has no checksum of the kept version to set against it
    kept = Path(back.previous["path"])
    marker = json.loads((kept / MARKER).read_text())
    marker["sha256"] = back.sha256
    (kept / MARKER).write_text(json.dumps(marker))
    tamper(previous={**back.previous, "sha256": None})
    (apps / "Blender" / "mine-too.txt").write_text("4.2")
    forth = rollback("blender", Scope.USER)
    assert forth.previous is not None and forth.previous["version"] == "4.2.0"
    assert (Path(forth.previous["path"]) / "release-4.2.0.txt").is_file()
    assert (apps / "Blender" / "mine-too.txt").read_text() == "4.2"
    assert (apps / "Blender" / "mine.txt").read_text() == "current"


def test_a_kept_version_that_changes_after_planning_is_not_used(downloads, apps, isolated_env):
    """Between "Go back?" and the click the kept folder is no longer what was planned."""
    install(blender(downloads, "4.2.0"))
    v2 = install(blender(downloads, "4.3.0"))
    kept = Path(v2.previous["path"])
    for change in ("marker", "program", "link"):
        plan = plan_rollback("blender", Scope.USER)
        try:
            saved = (kept / MARKER).read_text()
            if change == "marker":
                (kept / MARKER).write_text(saved.replace('"id": "blender"', '"id": "other"'))
            elif change == "program":
                os.rename(kept / "blender", kept / "blender.bak")
            else:
                os.rename(kept, apps / "Elsewhere")
                os.symlink(apps / "Elsewhere", kept)
            before = state(isolated_env)
            with pytest.raises(InstallError, match="cannot be used any more"):
                execute_install(plan)
            assert state(isolated_env) == before and registry().get("blender") == v2
        finally:
            plan.info.cleanup()
        if change == "marker":
            (kept / MARKER).write_text(saved)
        elif change == "program":
            os.rename(kept / "blender.bak", kept / "blender")
        else:
            os.unlink(kept)
            os.rename(apps / "Elsewhere", kept)
    assert rollback("blender", Scope.USER).version == "4.2.0"


def test_rollback_when_the_installed_folder_is_gone_restores_the_app(downloads, apps):
    install(blender(downloads, "4.2.0"))
    install(blender(downloads, "4.3.0"))
    shutil.rmtree(apps / "Blender")                       # deleted by hand
    back = rollback("blender", Scope.USER)
    assert back.version == "4.2.0" and back.previous is None and back.status() == "ok"
    assert names(apps) == ["Blender"]


# ------------------------------------------------------------------------------------------------
# uninstalling
# ------------------------------------------------------------------------------------------------


def test_uninstall_removes_the_folder_the_launcher_and_the_kept_version(downloads, apps, isolated_env):
    archive_1, archive_2 = blender(downloads, "4.2.0"), blender(downloads, "4.3.0")
    install(archive_1)
    app = install(archive_2)
    (apps / "Blender" / "cache").mkdir()
    (apps / "Blender" / "cache" / "big.bin").write_bytes(b"x" * 1000)
    notes = uninstall("blender", Scope.USER)
    # the folder holds what the app saved: it goes to the trash, not away for good
    assert len(notes) == 1 and "Blender was moved to the trash" in notes[0]
    assert registry().all() == [] and names(apps) == []
    assert (isolated_env / ".local/share/Trash/files/Blender/cache/big.bin").stat().st_size == 1000
    assert not Path(app.desktop_path).exists() and not Path(app.icon_paths[0]).exists()
    assert archive_1.is_file() and archive_2.is_file()       # the downloads are the user's
    with pytest.raises(NotInstalledError):
        uninstall("blender", Scope.USER)


def test_uninstall_when_the_folder_is_already_gone(downloads, apps):
    app = install(blender(downloads))
    shutil.rmtree(apps / "Blender")
    assert uninstall("blender", Scope.USER) == []
    assert registry().all() == [] and not Path(app.desktop_path).exists()


def test_uninstall_copes_with_read_only_folders_inside_the_app(downloads, apps):
    install(blender(downloads))
    locked = apps / "Blender" / "lib"
    os.chmod(locked, 0o555)
    try:
        assert uninstall("blender", Scope.USER) == []
    finally:
        if locked.exists():
            os.chmod(locked, 0o755)
    assert names(apps) == []


@pytest.mark.parametrize("how", ["uninstall", "drop", "prune", "replace"])
def test_a_kept_version_with_read_only_folders_can_be_deleted(downloads, apps, how):
    install(blender(downloads, "4.2.0"))
    os.chmod(apps / "Blender" / "lib", 0o555)
    os.chmod(apps / "Blender" / "4.2", 0o000)
    v2 = install(blender(downloads, "4.3.0"))
    kept = Path(v2.previous["path"])
    os.chmod(kept, 0o555)                       # ... and the kept folder itself
    try:
        assert kept.is_dir() and os.stat(kept / "lib").st_mode & 0o777 == 0o555
        if how == "uninstall":
            assert uninstall("blender", Scope.USER) == []
            assert names(apps) == []
        elif how == "drop":
            drop_backup("blender", Scope.USER)
        elif how == "prune":
            assert [app.id for app in prune_backups(0)] == ["blender"]
        else:
            assert install(blender(downloads, "4.4.0")).previous["version"] == "4.3.0"
        assert not kept.exists()
    finally:
        for folder in (kept, kept / "lib", kept / "4.2"):
            if folder.exists():
                os.chmod(folder, 0o755)


# --- hostile cases: what a tampered registry or folder must never achieve -------------------------


def left_alone(notes: list[str]) -> bool:
    return len(notes) == 1 and "was not removed, because Easy Installer cannot be sure" in notes[0]


def victim(folder: Path, app_id: str | None = "blender") -> Path:
    """A folder full of personal files - optionally even with a marker claiming to be the app."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "thesis.odt").write_text("three years of work")
    (folder / "photos").mkdir(exist_ok=True)
    (folder / "photos" / "wedding.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
    if app_id is not None:
        (folder / MARKER).write_text(json.dumps({"format": 1, "id": app_id, "executable": "blender"}))
    return folder


def test_dir_state_names_each_reason(downloads, apps, isolated_env):
    """portable_dir_state on its own (uninstall looks a second time, so each rule is checked
    here directly): "ok" only for the app's own, marked, real folder in the apps folder."""
    app = install(blender(downloads))
    layout, folder = user_layout(), apps / "Blender"
    marker = (folder / MARKER).read_text()

    def state(entry: InstalledApp = app) -> str:
        return portable_dir_state(layout, entry, registry())[0]

    def why(entry: InstalledApp = app) -> str:
        return portable_dir_state(layout, entry, registry())[1]

    assert (state(), why()) == ("ok", "")
    # the entry
    assert state(dataclasses.replace(app, kind="appimage")) == "unsafe"
    assert state(dataclasses.replace(app, install_dir=None)) == "unsafe"
    assert state(dataclasses.replace(app, id="../x")) == "unsafe"
    assert state(dataclasses.replace(app, install_dir=str(apps / "Gone"))) == "missing"
    assert state(dataclasses.replace(app, install_dir=str(isolated_env / "Gone"))) == "unsafe"
    # the marker
    for content in ('{"format": 1, "id": "other"}', '{"id": "BLENDER"}', '{"id": null}', "[]", ""):
        (folder / MARKER).write_text(content)
        assert state() == "unsafe" and "no marker of 'blender'" in why()
    (folder / MARKER).unlink()
    assert state() == "unsafe"
    (folder / MARKER).write_text(marker)
    assert state() == "ok"
    # a link in place of the folder - even one that leads to a folder with the right marker
    moved = apps / "Moved"
    os.rename(folder, moved)
    os.symlink(moved, folder)
    assert state() == "unsafe" and "link" in why()
    os.unlink(folder)
    assert state() == "missing"
    folder.write_text("a file")
    assert state() == "unsafe"
    folder.unlink()
    os.rename(moved, folder)
    assert state() == "ok"
    # another installed app inside it (or around it)
    other = dataclasses.replace(app, id="other", kind="appimage", install_dir=None,
                                appimage_path=str(folder / "lib" / "Other.AppImage"))
    registry().put(other)
    assert state() == "unsafe" and "also used by 'other'" in why()
    assert portable_dir_state(layout, app) == ("ok", "")       # (without a registry to ask)


def test_uninstall_refuses_a_folder_without_the_marker(downloads, apps):
    app = install(blender(downloads))
    (apps / "Blender" / MARKER).unlink()
    before = tree(apps)
    notes = uninstall("blender", Scope.USER)
    assert left_alone(notes) and str(apps / "Blender") in notes[0] and "Blender" in notes[0]
    assert tree(apps) == before                              # the folder is exactly as it was
    # the app itself is uninstalled: no entry, no launcher
    assert registry().all() == [] and not Path(app.desktop_path).exists()


@pytest.mark.parametrize("marker", [
    '{"format": 1, "id": "other"}', '{"format": 1}', '{"id": ["blender"]}', '["blender"]', "blender",
    "", '{"id": "Blender"}', '{"id": "blender" ', "x" * (1024 * 1024 + 1),
])
def test_uninstall_refuses_a_folder_with_another_marker(downloads, apps, marker):
    install(blender(downloads))
    (apps / "Blender" / MARKER).write_text(marker)
    before = tree(apps)
    assert left_alone(uninstall("blender", Scope.USER))
    assert tree(apps) == before and registry().all() == []


def test_uninstall_refuses_a_marker_that_is_a_link(downloads, apps, tmp_path):
    install(blender(downloads))
    real = tmp_path / "marker.json"
    real.write_text((apps / "Blender" / MARKER).read_text())
    (apps / "Blender" / MARKER).unlink()
    os.symlink(real, apps / "Blender" / MARKER)
    before = tree(apps)
    assert left_alone(uninstall("blender", Scope.USER))
    assert tree(apps) == before and real.is_file()


def tamper(**changes) -> InstalledApp:
    app = dataclasses.replace(registry().get("blender"), **changes)
    registry().put(app)
    return app


def test_a_tampered_registry_cannot_get_the_home_folder_deleted(downloads, apps, isolated_env):
    """install_dir points at the home folder - which even carries a matching marker."""
    install(blender(downloads))
    home = victim(isolated_env)
    tamper(install_dir=str(home), appimage_path=str(home / "thesis.odt"))
    before = tree(isolated_env / "photos"), (home / "thesis.odt").read_text(), tree(apps)
    notes = uninstall("blender", Scope.USER)
    assert left_alone(notes)
    assert (tree(isolated_env / "photos"), (home / "thesis.odt").read_text(), tree(apps)) == before
    assert (apps / "Blender" / "blender").is_file()          # nothing recorded = nothing deleted
    assert registry().all() == []


@pytest.mark.parametrize("where", [
    "{home}/Documents", "{home}/.config", "{home}/.local/share", "{apps}", "{apps}/.easyinstaller-backups",
    "{apps}/Blender/lib", "{apps}/../Documents", "{apps}/Blender/../Other", "{apps}/./Other",
    "{apps}/Other/", "/", "/tmp", "Other", "", "{apps}/.hidden", "{apps}/Other\x00x",
])
def test_a_tampered_registry_cannot_get_other_folders_deleted(downloads, apps, isolated_env, where):
    install(blender(downloads))
    for folder in (isolated_env / "Documents", apps / "Other", apps / ".hidden",
                   apps / BACKUPS, isolated_env / ".config", isolated_env / ".local" / "share"):
        victim(folder)
    path = where.format(home=isolated_env, apps=apps)
    tampered = tamper(install_dir=path, appimage_path=os.path.join(path, "blender"))
    assert portable_dir_state(user_layout(), tampered, registry())[0] == "unsafe"
    before = state(isolated_env)
    desktop = Path(tampered.desktop_path)
    notes = uninstall("blender", Scope.USER)
    assert left_alone(notes)
    after = state(isolated_env)
    gone = sorted(set(before) - set(after))
    # only Easy Installer's own files of the app went: its launcher, its icon (and the registry
    # changed) - no folder, no personal file
    assert all("easyinstaller-blender" in rel for rel in gone), gone
    assert not desktop.exists() and registry().all() == []
    for folder in (isolated_env / "Documents", apps / "Other", apps / ".hidden", apps / BACKUPS):
        assert (folder / "thesis.odt").read_text() == "three years of work"
        assert (folder / "photos" / "wedding.jpg").is_file()
    assert (apps / "Blender" / "blender").is_file()


def test_uninstall_never_follows_a_symlinked_app_folder(downloads, apps, isolated_env):
    """The app folder was replaced by a link to the user's documents (with a fitting marker)."""
    install(blender(downloads))
    documents = victim(isolated_env / "Documents")
    shutil.rmtree(apps / "Blender")
    os.symlink(documents, apps / "Blender")
    before = tree(documents)
    assert left_alone(uninstall("blender", Scope.USER))
    assert tree(documents) == before
    assert (apps / "Blender").is_symlink()                   # not even the link is touched
    assert registry().all() == []


def test_uninstall_never_deletes_through_a_symlinked_apps_folder_entry(downloads, apps, isolated_env):
    """A link inside the app folder leads to personal files: only the link goes."""
    install(blender(downloads))
    documents = victim(isolated_env / "Documents", app_id=None)
    os.symlink(documents, apps / "Blender" / "my-documents")
    before = tree(documents)
    notes = uninstall("blender", Scope.USER)
    assert len(notes) == 1 and "moved to the trash" in notes[0]    # (the link was added)
    assert tree(documents) == before and names(apps) == []


def test_uninstall_leaves_a_folder_another_app_uses(downloads, apps):
    """Two registry entries claim the same folder: neither uninstall removes it."""
    app = install(blender(downloads))
    other = dataclasses.replace(app, id="other", name="Other",
                                desktop_path=app.desktop_path.replace("blender", "other"))
    registry().put(other)
    before = tree(apps)
    assert left_alone(uninstall("blender", Scope.USER))
    assert tree(apps) == before
    # the entry that remains has the marker of "blender", not its own: refused as well
    assert left_alone(uninstall("other", Scope.USER))
    assert tree(apps) == before and registry().all() == []


def test_uninstall_of_an_appimage_entry_never_removes_a_folder(downloads, apps, isolated_env):
    """kind says "appimage" but the path is a folder (a confused or tampered entry)."""
    install(blender(downloads))
    tamper(kind="appimage", appimage_path=str(apps / "Blender"))
    before = tree(apps)
    uninstall("blender", Scope.USER)
    assert tree(apps) == before and registry().all() == []


def test_uninstall_deletes_no_single_file_of_a_portable_app(downloads, apps, isolated_env):
    """The program of a portable app is never deleted on its own - not even when the tampered
    entry names a file that looks like an installed AppImage."""
    install(blender(downloads))
    precious = isolated_env / "Documents" / "Precious.AppImage"
    precious.parent.mkdir()
    precious.write_bytes(b"not yours to delete")
    tamper(appimage_path=str(precious), install_dir=str(isolated_env / "Documents"))
    assert left_alone(uninstall("blender", Scope.USER))
    assert precious.read_bytes() == b"not yours to delete" and registry().all() == []


def test_a_folder_swapped_during_uninstall_is_put_back(downloads, apps, monkeypatch):
    """Between the check and the removal the folder is looked at once more."""
    install(blender(downloads))
    real = installer.read_portable_marker
    answers = iter([real(apps / "Blender"), None])       # second look: no longer the app's

    def marker(folder):
        return next(answers, None)

    monkeypatch.setattr(installer, "read_portable_marker", marker)
    before = tree(apps)
    assert left_alone(uninstall("blender", Scope.USER))
    assert tree(apps) == before


# --- hostile cases: replacing -------------------------------------------------------------------


def test_update_never_replaces_a_folder_that_is_not_verifiably_the_apps(downloads, apps, isolated_env):
    """The registry points at the home folder: the new version goes to a fresh folder and the
    home folder is left alone."""
    install(blender(downloads, "4.2.0"))
    home = victim(isolated_env)
    tamper(install_dir=str(home), appimage_path=str(home / "blender"))
    before = (home / "thesis.odt").read_text(), tree(home / "photos"), tree(apps / "Blender")
    with inspect_portable(blender(downloads, "4.3.0")) as info:
        plan = plan_install(info)
        # "Blender" is taken by a folder nobody vouches for any more
        assert plan.install_dir == apps / "Blender-blender" and plan.backup_target is None
        v2 = execute_install(plan)
    assert v2.install_dir == str(apps / "Blender-blender") and v2.previous is None
    assert ((home / "thesis.odt").read_text(), tree(home / "photos"), tree(apps / "Blender")) == before
    assert (home / MARKER).is_file()


def test_a_folder_in_the_way_is_never_overwritten(downloads, apps):
    mine = victim(apps / "Blender", app_id=None)
    before = tree(mine)
    app = install(blender(downloads))
    assert app.install_dir == str(apps / "Blender-blender") and tree(mine) == before
    uninstall("blender", Scope.USER)
    assert tree(mine) == before and names(apps) == ["Blender"]


def test_a_folder_that_appears_after_planning_stops_the_installation(downloads, apps, isolated_env):
    with inspect_portable(blender(downloads)) as info:
        plan = plan_install(info)
        mine = victim(apps / "Blender")               # even with a fitting marker
        before = files(isolated_env)
        with pytest.raises(InstallError, match="folder is in the way"):
            execute_install(plan)
    # (empty folders for launchers and icons may have been made; no file was added or changed)
    assert files(isolated_env) == before and registry().all() == []
    assert names(apps) == ["Blender"] and (mine / "thesis.odt").is_file()


def test_update_of_a_folder_that_lost_its_marker(downloads, apps, isolated_env):
    v1 = install(blender(downloads, "4.2.0"))
    (apps / "Blender" / MARKER).unlink()
    before = tree(apps / "Blender")
    v2 = install(blender(downloads, "4.3.0"))
    assert v2.install_dir == str(apps / "Blender-blender") != v1.install_dir
    assert tree(apps / "Blender") == before and v2.previous is None
    assert uninstall("blender", Scope.USER) == []
    assert tree(apps / "Blender") == before and names(apps) == ["Blender"]


@pytest.mark.parametrize("target", ["{outside}", "tool", "data/settings.json"])
def test_the_archive_cannot_plant_the_marker_or_redirect_it(downloads, apps, tmp_path, target):
    """An archive that brings its own marker (claiming another app), and a link where the
    installer keeps the icon - leading out of the app, to its program or to one of its files."""
    outside = tmp_path / "outside.json"
    outside.write_text("untouched")
    program = fake_elf()
    archive = make_tar(downloads / "tool-1.0.tar.gz", {
        "tool/tool": program, "tool/tool.png": make_png(64, 64),
        "tool/data/settings.json": "the app's own file",
        "tool/.easy-installer.json": '{"format": 1, "id": "firefox"}',
    }, symlinks={"tool/.easy-installer-icon.png": target.format(outside=outside)},
        modes={"tool/tool": 0o755})
    app = install(archive)
    folder = Path(app.install_dir)
    marker = read_portable_marker(folder)
    assert marker["id"] == app.id == "tool" and not (folder / MARKER).is_symlink()
    assert marker["icon"] == ".easy-installer-icon.png"
    icon = folder / ".easy-installer-icon.png"
    assert not icon.is_symlink() and icon.read_bytes()[:4] == b"\x89PNG"
    # nothing was written through the link
    assert outside.read_text() == "untouched" and (folder / "tool").read_bytes() == program
    assert (folder / "data" / "settings.json").read_text() == "the app's own file"
    assert uninstall("tool", Scope.USER) == [] and outside.read_text() == "untouched"


def test_the_archive_cannot_block_the_marker_with_folders_of_its_name(downloads, apps):
    archive = make_tar(downloads / "tool-1.0.tar.gz", {
        "tool/tool": fake_elf(), "tool/tool.png": make_png(64, 64),
        "tool/.easy-installer.json/x": "x", "tool/.easy-installer-icon.png/y/z": "z",
    }, modes={"tool/tool": 0o755})
    app = install(archive)
    folder = Path(app.install_dir)
    assert read_portable_marker(folder)["id"] == "tool" and (folder / MARKER).is_file()
    assert (folder / ".easy-installer-icon.png").read_bytes()[:4] == b"\x89PNG"
    assert repair("tool", Scope.USER).status() == "ok"
    assert uninstall("tool", Scope.USER) == [] and names(apps) == []


# ------------------------------------------------------------------------------------------------
# the marker as a description of the app (repair, going back)
# ------------------------------------------------------------------------------------------------


def test_info_from_the_folder(downloads, apps):
    app = install(blender(downloads))
    with portable_info_from_folder(app.install_dir, "blender") as info:
        assert isinstance(info, PortableInfo) and info.path == apps / "Blender"
        assert (info.app_id, info.name, info.version, info.executable) == \
            ("blender", "Blender", "4.2.0", "blender")
        assert info.sha256 == app.sha256 and info.tree_size == app.size and info.arch == "x86_64"
        assert info.desktop_entry.get("Exec") == "blender %f" and info.desktop_filename == "blender.desktop"
        assert info.icon_path.is_file() and info.icon_info.format == "svg"
        assert info.icon_path.parent == info.work_dir      # a copy: the folder may move
        assert [c.relpath for c in info.executables] == ["blender"]
        assert info.data_hints == app.data_hints
        work_dir = info.work_dir
    assert not work_dir.exists()


@pytest.mark.parametrize("executable", ["../../../bin/sh", "/bin/sh", "lib/../../x", "", 7, None,
                                        "missing", "lib", "link-out"])
def test_info_from_a_folder_with_an_unusable_program(downloads, apps, executable, tmp_path):
    app = install(blender(downloads))
    folder = Path(app.install_dir)
    outside = tmp_path / "outside"
    outside.write_bytes(fake_script())
    os.symlink(outside, folder / "link-out")
    marker = json.loads((folder / MARKER).read_text())
    marker["executable"] = executable
    (folder / MARKER).write_text(json.dumps(marker))
    with pytest.raises(InstallError, match="folder is incomplete"):
        portable_info_from_folder(folder, "blender")
    with pytest.raises(InstallError, match="cannot be repaired automatically"):
        plan_repair("blender", Scope.USER)


def test_info_from_a_folder_treats_the_marker_as_untrusted_text(downloads, apps):
    app = install(blender(downloads))
    folder = Path(app.install_dir)
    marker = json.loads((folder / MARKER).read_text())
    # an icon that is not the one the installer put into the folder is never read
    (apps / "outside.png").write_bytes(make_png(64, 64))
    (folder / "lib" / "inside.png").write_bytes(make_png(64, 64))
    for icon in ("../outside.png", "lib/inside.png", str(apps / "outside.png"), ".easy-installer-icon.exe",
                 ["x"], 5):
        marker["icon"] = icon
        (folder / MARKER).write_text(json.dumps(marker))
        with portable_info_from_folder(folder, "blender") as info:
            assert info.icon_path is None and info.icon_info is None, icon
    for arch in ("x86_64; rm -rf ~", "a" * 40, "", ["x86_64"], 64):
        marker["arch"] = arch
        (folder / MARKER).write_text(json.dumps(marker))
        with portable_info_from_folder(folder, "blender") as info:
            assert info.arch is None, arch
    marker.update(name="Evil\nExec=rm -rf ~", version=["1"], comment=5, categories="x",
                  desktop="[Desktop Entry]\nName=Blender\nExec=sh -c 'rm -rf ~'\nIcon=x\n",
                  icon="../../../etc/passwd", terminal="yes", tree_size=-5, wm_class="a\x00b",
                  desktop_filename="../x.desktop", sha256="zz", origin_url="javascript:alert(1)")
    (folder / MARKER).write_text(json.dumps(marker))
    with portable_info_from_folder(folder, "blender") as info:
        assert info.name == "Evil Exec=rm -rf ~" and info.version is None and info.comment is None
        assert info.categories == [] and info.terminal is False and info.tree_size == 0
        assert info.icon_path is None and info.desktop_filename is None and info.sha256 is None
        assert info.origin_url is None and info.wm_class == "a b"
    repaired = repair("blender", Scope.USER)
    # the launcher starts the app's program - never what the tampered entry says
    text = Path(repaired.desktop_path).read_text()
    assert [line for line in text.splitlines() if line.startswith(("Exec", "TryExec"))] == \
        [f"Exec={folder}/blender", f"TryExec={folder}/blender"]
    assert "rm -rf" not in text and "\nName=Blender\n" in text
    assert repaired.name == "Evil Exec=rm -rf ~"        # one line of text, nothing more


NASTY = [None, True, False, 0, -1, 7, 1e308, [], ["x"], {}, {"a": 1}, "", " ", "x" * 5000, "..", "../..",
         "/etc/passwd", "a/b", "a\x00b", "a\nb", "\ud800", "blender", "lib", "Exec=rm -rf ~", "%f %U",
         "[Desktop Entry]\nExec=/bin/sh -c 'touch /tmp/pwned'\n", ".easy-installer-icon.svg",
         ".easy-installer-icon.png", "../.easy-installer-icon.svg", "ä" * 300, "x.desktop", 2 ** 70]


def test_a_random_marker_never_breaks_repair_or_lets_the_launcher_start_something_else(downloads, apps):
    """Seeded fuzz: every field of the marker gets values it should never have."""
    import random

    app = install(blender(downloads))
    folder = Path(app.install_dir)
    good = json.loads((folder / MARKER).read_text())
    rng = random.Random(20260930)
    repaired = 0
    for _round in range(250):
        marker = dict(good)
        for key in rng.sample(sorted(good), rng.randint(1, 6)):
            if key != "id":
                marker[key] = rng.choice(NASTY)
        if rng.random() < 0.1:
            marker[rng.choice(["extra", "id2", "path"])] = rng.choice(NASTY)
        (folder / MARKER).write_text(json.dumps(marker))
        try:
            with portable_info_from_folder(folder, "blender") as info:
                assert info.path == folder and info.app_id == "blender"
                program = (folder / info.executable).resolve()
                assert program.is_file() and folder.resolve() in program.parents
                assert info.icon_path is None or info.icon_path.parent == info.work_dir
            new = repair("blender", Scope.USER)
        except InstallError:
            continue                          # "cannot be repaired": fine, nothing else may come
        repaired += 1
        entry = entry_of(new)
        started = split_exec(entry.get("Exec"))[0]
        assert started == new.appimage_path == entry.get("TryExec")
        assert Path(started).is_file() and folder in Path(started).parents
        assert entry.get("Path") == str(folder) and new.install_dir == str(folder)
        for group in entry.groups():          # no action starts anything but the app either
            value = entry.get("Exec", group)
            assert value is None or split_exec(value)[0] == started
    assert repaired > 50                      # the fuzz really got through to the launcher
    assert names(apps) == ["Blender"]


def test_repair_makes_the_launcher_again(downloads, apps, isolated_env):
    app = install(blender(downloads))
    launcher, icon = Path(app.desktop_path), Path(app.icon_paths[0])
    text = launcher.read_text()
    launcher.unlink()
    icon.unlink()
    assert registry().get("blender").status() == "missing-launcher"
    folder_before = tree(apps)

    plan = plan_repair("blender", Scope.USER)
    try:
        assert plan.kind == "portable" and plan.in_place and plan.action == "reinstall"
        assert plan.install_dir == apps / "Blender" and plan.backup_target is None
    finally:
        plan.info.cleanup()
    repaired = repair("blender", Scope.USER)
    assert launcher.read_text() == text and icon.is_file()
    assert tree(apps) == folder_before                      # the app's files were not touched
    assert dataclasses.replace(repaired, updated_at=app.updated_at) == app
    assert repaired.status() == "ok"


def test_repair_keeps_the_kept_version_and_the_installed_icon(downloads, apps):
    install(blender(downloads, "4.2.0"))
    v2 = install(blender(downloads, "4.3.0"))
    (apps / "Blender" / ".easy-installer-icon.svg").unlink()     # the folder lost its icon copy
    repaired = repair("blender", Scope.USER)
    assert repaired.previous == v2.previous and repaired.icon_paths == v2.icon_paths
    assert entry_of(repaired).get("Icon") == "easyinstaller-blender"
    assert Path(repaired.icon_paths[0]).is_file()


def test_repair_of_a_folder_that_is_not_verifiably_the_apps(downloads, apps, isolated_env):
    install(blender(downloads))
    (apps / "Blender" / MARKER).unlink()
    before = state(isolated_env)
    with pytest.raises(InstallError, match="cannot be repaired automatically"):
        repair("blender", Scope.USER)
    assert state(isolated_env) == before
    with pytest.raises(NotInstalledError):
        repair("nope", Scope.USER)


# ------------------------------------------------------------------------------------------------
# what an interrupted installation leaves behind
# ------------------------------------------------------------------------------------------------


def test_a_half_unpacked_app_is_removed_by_the_next_installation(downloads, apps):
    install(blender(downloads, "4.2.0"))
    half = apps / ".Blender.easyinstaller-new-0a1b2c3d"
    half.mkdir()
    (half / "blender").write_bytes(b"half")
    not_ours = [apps / ".Blender.easyinstaller-new-XYZ", apps / ".my-hidden-folder",
                apps / "Blender.easyinstaller-new-0a1b2c3d"]
    for folder in not_ours:
        folder.mkdir()
        (folder / "file").write_text("x")
    install(blender(downloads, "4.3.0"))
    assert not half.exists()
    assert all((folder / "file").is_file() for folder in not_ours)


def test_an_interrupted_swap_is_put_back(downloads, apps):
    """Killed after the installed folder was set aside: the next operation restores it."""
    v1 = install(blender(downloads, "4.2.0"))
    aside = apps / ".Blender.easyinstaller-old-0a1b2c3d"
    os.rename(apps / "Blender", aside)
    assert registry().get("blender").status() == "missing-appimage"
    other = make_tar(downloads / "tool-1.0.tar.gz", {"tool/tool": fake_elf()}, modes={"tool/tool": 0o755})
    install(other)
    assert not aside.exists() and registry().get("blender").status() == "ok"
    assert read_portable_marker(apps / "Blender")["id"] == "blender" and v1.version == "4.2.0"


def test_a_replaced_folder_that_was_not_cleaned_up_is_removed(downloads, apps):
    """Killed after the new version was saved: the set-aside old folder is deleted later -
    but only if it carries Easy Installer's marker."""
    install(blender(downloads, "4.2.0"))
    old = apps / ".Blender.easyinstaller-old-0a1b2c3d"
    shutil.copytree(apps / "Blender", old)
    stranger = apps / ".Photos.easyinstaller-old-0a1b2c3d"
    victim(stranger, app_id=None)
    before = tree(stranger)
    install(blender(downloads, "4.3.0"))
    assert not old.exists()
    assert tree(stranger) == before                     # no marker: not ours, left alone
    uninstall("blender", Scope.USER)
    assert tree(stranger) == before
