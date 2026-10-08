"""installer.removable_data(): which settings and data folders may be offered for removal when
an app is uninstalled (DESIGN section 24) - and, more important, when nothing is offered."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import appdata, installer, privileged
from easy_installer.core.appdata import DataLocation, move_to_trash
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    data_hints_of,
    execute_install,
    other_installations,
    plan_install,
    removable_data,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.portable import inspect_portable
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.system_checks import SystemStatus
from easy_installer.helper import ops

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs
from fakearchive import blender_tree, make_archive

pytestmark = [requires_mksquashfs, requires_unsquashfs]


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
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper(sys_layout, monkeypatch):
    caller = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}

    def run_helper(op, payload, *, timeout=600):
        request = json.loads(json.dumps(payload))
        if op == "install":
            return ops.op_install(request, layout=sys_layout, run_commands=False, **caller)
        if op == "uninstall":
            return ops.op_uninstall(request, layout=sys_layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)


@pytest.fixture
def home(isolated_env, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_env / ".local" / "state"))
    for name in (".config", ".local/share", ".cache", ".local/state", "Downloads", "Documents"):
        (isolated_env / name).mkdir(parents=True, exist_ok=True)
    return isolated_env


def make(folder: Path, size: int = 10) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.ini").write_bytes(b"x" * size)
    return folder


def pen(path: Path, version: str = "1.2.8", *, wm_class: str = "Pen") -> Path:
    desktop = ("[Desktop Entry]\nName=Pen\nExec=AppRun --no-sandbox %U\nType=Application\nIcon=pen\n"
               f"StartupWMClass={wm_class}\nX-AppImage-Version={version}\nCategories=Graphics;\n")
    return make_fake_appimage(path, {"pen.desktop": desktop, "AppRun": "#!/bin/sh\n",
                                     "payload": f"pen {version}", "pen.png": make_png(32, 32)})


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def offered(app: InstalledApp) -> dict[str, str]:
    """{path relative to the home: kind} of removable_data()."""
    root = Path(os.environ["HOME"]).resolve()
    return {str(location.path.relative_to(root)): location.kind for location in removable_data(app)}


def pen_data(home: Path) -> None:
    make(home / ".config" / "Pen", 100)
    make(home / ".cache" / "pen-updater", 2000)
    make(home / ".local" / "share" / "pen")
    make(home / ".pen")
    # never offered: other apps, shared folders, files
    make(home / ".config" / "Pencil")
    make(home / ".config" / "pen-and-paper")
    make(home / ".local" / "share" / "applications")
    make(home / "Documents" / "Pen")
    (home / ".config" / "pen.conf").write_text("a file, not a folder")


PEN_DATA = {".config/Pen": "config", ".local/share/pen": "data", ".cache/pen-updater": "cache",
            ".pen": "home"}


def test_data_of_an_installed_app(home):
    app = install(pen(home / "Downloads" / "Pen-linux-x86_64.AppImage"))
    assert app.data_hints == ["Pen", "pen-updater"] == data_hints_of(app)
    assert removable_data(app) == []                  # the app has not written anything yet
    pen_data(home)
    found = removable_data(app)
    assert all(isinstance(location, DataLocation) for location in found)
    assert offered(app) == PEN_DATA
    sizes = {location.path.name: (location.size, location.file_count) for location in found}
    assert sizes["Pen"] == (100, 1) and sizes["pen-updater"] == (2000, 1)
    # nothing of the installation itself, and looking changes nothing
    assert Path(app.appimage_path).is_file() and (home / ".config" / "Pencil").is_dir()


def test_the_offer_still_stands_after_the_uninstall(home):
    """CLI and GUI ask before or after removing the app: both must work."""
    app = install(pen(home / "Downloads" / "Pen-linux-x86_64.AppImage"))
    pen_data(home)
    before = offered(app)
    uninstall("pen", Scope.USER)
    assert offered(app) == before == PEN_DATA
    # uninstalling alone never removes data
    assert (home / ".config" / "Pen" / "settings.ini").is_file()

    results = move_to_trash([location.path for location in removable_data(app)])
    assert [error for _path, error in results] == [None] * 4
    assert not (home / ".config" / "Pen").exists() and not (home / ".pen").exists()
    assert (home / ".config" / "Pencil").is_dir() and (home / ".local" / "share" / "applications").is_dir()
    trashed = sorted(p.name for p in (home / ".local" / "share" / "Trash" / "files").iterdir())
    assert len(trashed) == 4 and removable_data(app) == []       # in the trash, not deleted


def test_nothing_is_offered_while_a_kept_copy_remains(home):
    """Keep both: the other version still uses the same settings."""
    main = install(pen(home / "Downloads" / "Pen-1.2.8.AppImage", "1.2.8"))
    with inspect_appimage(pen(home / "Downloads" / "Pen-1.1.0.AppImage", "1.1.0")) as info:
        copy = execute_install(plan_install(info, InstallOptions(keep_both=True)))
    assert copy.base_id == "pen" and copy.data_hints == ["Pen", "pen-updater"]
    pen_data(home)
    assert [app.id for app in other_installations(main)] == [copy.id]
    assert [app.id for app in other_installations(copy)] == ["pen"]
    assert removable_data(main) == [] and removable_data(copy) == []

    uninstall(copy.id, Scope.USER)
    assert removable_data(copy) == []                 # the main app is still there
    assert offered(main) == PEN_DATA                  # ... and is now the last one
    uninstall("pen", Scope.USER)
    assert offered(main) == PEN_DATA and offered(copy) == PEN_DATA


def test_nothing_is_offered_while_the_app_is_installed_in_the_other_scope(home, sys_layout, helper):
    mine = install(pen(home / "Downloads" / "Pen-1.2.8.AppImage"), InstallOptions(keep_original=True))
    for_all = install(home / "Downloads" / "Pen-1.2.8.AppImage", InstallOptions(scope=Scope.SYSTEM))
    assert for_all.scope is Scope.SYSTEM and for_all.data_hints == ["Pen", "pen-updater"]
    pen_data(home)
    assert removable_data(mine) == [] and removable_data(for_all) == []
    uninstall("pen", Scope.USER)
    assert removable_data(mine) == []                 # everyone's copy remains
    assert offered(for_all) == PEN_DATA               # the last installation: this user's data
    uninstall("pen", Scope.SYSTEM)
    assert offered(for_all) == PEN_DATA and offered(mine) == PEN_DATA


def test_an_entry_made_by_0_1_gets_its_names_from_the_launcher(home):
    """No recorded hints: name, id and the StartupWMClass of the installed launcher."""
    app = install(pen(home / "Downloads" / "Pen.AppImage", wm_class="dev.pen.Desktop"))
    old = dataclasses.replace(app, data_hints=[], mtime_ns=0)
    Registry(user_layout().registry_path).put(old)
    assert data_hints_of(old) == ["Pen", "dev.pen.Desktop", "pen-updater"]
    make(home / ".config" / "Pen")
    make(home / ".config" / "dev.pen.Desktop")
    make(home / ".cache" / "pen-updater")
    assert offered(old) == {".config/Pen": "config", ".config/dev.pen.Desktop": "config",
                            ".cache/pen-updater": "cache"}
    # without a readable launcher the name and the id are still enough
    Path(old.desktop_path).unlink()
    assert data_hints_of(old) == ["Pen", "pen-updater"]
    assert offered(old) == {".config/Pen": "config", ".cache/pen-updater": "cache"}


@pytest.mark.parametrize("hints", [
    ["applications"], ["icons", "mime", "Trash", "easy-installer"], ["..", ".", "/"], [".config"],
    ["a", "ab"], ["../Documents"], ["*"], ["Pen/../.."], [7, None, ""], ["google-chrome", "mozilla"],
])
def test_tampered_hints_in_the_registry_offer_nothing_dangerous(home, hints):
    app = install(pen(home / "Downloads" / "Pen.AppImage"))
    for name in ("applications", "icons", "mime", "Trash", "easy-installer", "google-chrome", "mozilla",
                 "a", "ab"):
        make(home / ".local" / "share" / name)
        make(home / ".config" / name)
    make(home / "Documents")
    tampered = dataclasses.replace(app, data_hints=hints)
    assert offered(tampered) == {}
    # unusable names fall back to the app's own names at most - never to "everything"
    make(home / ".config" / "Pen")
    assert offered(tampered) in ({}, {".config/Pen": "config"})


def test_the_apps_own_folders_are_never_offered(home, monkeypatch):
    """Even when the apps folder itself is named like the app and lies in a data folder."""
    apps = home / ".local" / "share" / "pen"
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(apps))
    install(pen(home / "Downloads" / "Pen-1.0.AppImage", "1.0"))
    app = install(pen(home / "Downloads" / "Pen-1.1.AppImage", "1.1"))
    assert Path(app.appimage_path).parent == apps and Path(app.previous["path"]).is_file()
    make(home / ".config" / "Pen")
    assert offered(app) == {".config/Pen": "config"}
    uninstall("pen", Scope.USER)
    assert offered(app) == {".config/Pen": "config"}


def test_data_of_a_portable_app(home):
    archive = make_archive(home / "Downloads" / "blender-4.2.0-linux-x64.tar.gz", **blender_tree())
    with inspect_portable(archive) as info:
        app = execute_install(plan_install(info))
    assert app.data_hints == ["Blender", "blender-updater"]
    make(home / ".config" / "blender")
    make(home / ".cache" / "blender")
    assert offered(app) == {".config/blender": "config", ".cache/blender": "cache"}
    assert uninstall("blender", Scope.USER) == []
    assert offered(app) == {".config/blender": "config", ".cache/blender": "cache"}
    assert not Path(app.install_dir).exists() and (home / ".config" / "blender").is_dir()


def test_folders_that_another_installed_app_uses_are_not_offered(home):
    """DATA-4: the same program as an AppImage (id balena-etcher) and from its zip (id
    balenaetcher) - different ids, the same settings folder."""
    from fakearchive import electron_tree, make_zip

    desktop = ("[Desktop Entry]\nName=balenaEtcher\nExec=AppRun --no-sandbox %U\n"
               "Type=Application\nIcon=balena-etcher\nStartupWMClass=balenaEtcher\n"
               "X-AppImage-Version=1.18.11\n")
    image = install(make_fake_appimage(home / "Downloads" / "balenaEtcher-1.18.11-x64.AppImage", {
        "balena-etcher.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "balena-etcher.png": make_png(32, 32)}))
    tree = electron_tree("balenaEtcher-linux-x64", program="balena-etcher")
    with inspect_portable(make_zip(home / "Downloads" / "balenaEtcher-linux-x64-2.1.4.zip",
                                   tree["files"], tree["symlinks"], tree["modes"])) as info:
        portable = execute_install(plan_install(info))
    assert (image.id, portable.id) == ("balena-etcher", "balenaetcher")
    make(home / ".config" / "balenaEtcher")
    make(home / ".cache" / "balenaetcher-updater")
    assert ".config/balenaEtcher" not in offered(portable)
    assert ".config/balenaEtcher" not in offered(image)
    # once the other one is gone, the folder is offered again
    uninstall(image.id, Scope.USER)
    assert ".config/balenaEtcher" in offered(portable)


def test_removable_data_never_raises(home, monkeypatch):
    app = install(pen(home / "Downloads" / "Pen.AppImage"))
    pen_data(home)

    def boom(*args, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(appdata, "find_app_data", boom)
    assert removable_data(app) == []
    monkeypatch.setattr(installer, "list_installed", boom)
    assert removable_data(app) == []
