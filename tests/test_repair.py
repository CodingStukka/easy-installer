"""installer.plan_repair() / repair(): the launcher, icon and registry entry of an installed
AppImage are made again from the installed file itself. (Portable apps: test_portable_install.)"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import installer, privileged
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    execute_install,
    plan_install,
    plan_repair,
    repair,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import SandboxFix
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import EasyInstallerError, InstallError, NotInstalledError
from easy_installer.helper import ops

from fakeappimage import (
    EM_AARCH64,
    make_fake_appimage,
    make_png,
    requires_mksquashfs,
    requires_unsquashfs,
)

pytestmark = [requires_mksquashfs, requires_unsquashfs]

UPD_INFO = "gh-releases-zsync|demo-org|demo|latest|Demo-*-x86_64.AppImage.zsync"


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
def machine(monkeypatch):
    """The status of "this computer", changeable by a test."""
    state = {"status": make_status()}
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: state["status"])
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)
    return state


@pytest.fixture(autouse=True)
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper(sys_layout, monkeypatch):
    calls: list[tuple[str, dict]] = []
    caller = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}

    def run_helper(op, payload, *, timeout=600):
        request = json.loads(json.dumps(payload))
        calls.append((op, request))
        assert op == "install"
        return ops.op_install(request, layout=sys_layout, run_commands=False, **caller)

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    return calls


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def demo(path: Path, version: str | None, *, electron: bool = False, **kw) -> Path:
    desktop = ("[Desktop Entry]\nType=Application\nName=Demo\n"
               f"Exec=AppRun --v{version} %F\nIcon=demo\nCategories=Utility;\n")
    if version:
        desktop += f"X-AppImage-Version={version}\n"
    files = {"demo.desktop": desktop, "AppRun": "#!/bin/sh\n", "payload": f"demo {version}",
             "demo.png": make_png(32, 32)}
    if electron:
        files.update({"chrome-sandbox": b"\x7fELF", "resources/app.asar": b"asar"})
    return make_fake_appimage(path, files, **kw)


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def exec_args(app: InstalledApp) -> list[str]:
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))
    return split_exec(entry.get("Exec"))


def test_repair_makes_launcher_and_icon_again(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    launcher, icon = Path(app.desktop_path), Path(app.icon_paths[0])
    text = launcher.read_text()
    launcher.unlink()
    icon.unlink()
    assert registry().get("demo").status() == "missing-launcher"
    inode = os.stat(app.appimage_path).st_ino

    plan = plan_repair("demo", Scope.USER)
    try:
        assert plan.action == "reinstall" and plan.in_place and plan.existing == app
        assert plan.target_appimage == Path(app.appimage_path) and plan.backup_target is None
        assert plan.requires_root is False and plan.keep_both_available is False
    finally:
        plan.info.cleanup()

    repaired = repair("demo", Scope.USER)
    assert launcher.read_text() == text and icon.is_file()
    assert os.stat(app.appimage_path).st_ino == inode            # the app file was not touched
    assert dataclasses.replace(repaired, updated_at=app.updated_at) == app
    assert sorted(p.name for p in user_layout().apps_dir.iterdir()) == ["Demo.AppImage"]


def test_repair_keeps_the_choices_the_version_and_the_kept_version(downloads, machine):
    """The installed file no longer tells its version by its name; sandbox fix and start mode
    were chosen when it was installed."""
    machine["status"] = make_status(userns_restricted=True, apparmor_parser=None)
    install(demo(downloads / "Demo-1.0-x86_64.AppImage", None, electron=True),
            InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX, extract_and_run=True))
    app = install(demo(downloads / "Demo-1.1-x86_64.AppImage", None, electron=True, upd_info=UPD_INFO),
                  InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX, extract_and_run=True))
    assert app.version == "1.1" and app.previous["version"] == "1.0"
    machine["status"] = make_status()                 # the computer no longer blocks the sandbox
    Path(app.desktop_path).unlink()

    repaired = repair("demo", Scope.USER)
    assert repaired.version == "1.1" and repaired.previous == app.previous
    assert Path(repaired.previous["path"]).is_file()
    assert repaired.extract_and_run and repaired.extract_and_run_explicit
    assert exec_args(repaired)[:3] == ["env", "APPIMAGE_EXTRACT_AND_RUN=1", repaired.appimage_path]
    assert repaired.original_filename == "Demo-1.1-x86_64.AppImage"
    assert repaired.update_source == app.update_source and repaired.installed_at == app.installed_at


def test_repair_fills_in_what_an_entry_made_by_0_1_lacks(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0", upd_info=UPD_INFO))
    old = dataclasses.replace(app, update_source=None, data_hints=[], mtime_ns=0,
                              installer_version="0.1.0")
    registry().put(old)
    repaired = repair("demo", Scope.USER)
    assert repaired.update_source == app.update_source and repaired.update_source["repo"] == "demo"
    assert repaired.data_hints == ["Demo", "demo-updater"] and repaired.mtime_ns == app.mtime_ns


def test_repair_of_a_kept_copy_stays_that_copy(downloads):
    install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    with inspect_appimage(demo(downloads / "Demo-1.0.AppImage", "1.0")) as info:
        copy = execute_install(plan_install(info, InstallOptions(keep_both=True)))
    Path(copy.desktop_path).unlink()
    repaired = repair(copy.id, Scope.USER)
    assert (repaired.id, repaired.name, repaired.base_id, repaired.pinned) == \
        ("demo--1.0", "Demo 1.0", "demo", True)
    assert dataclasses.replace(repaired, updated_at=copy.updated_at) == copy
    assert "Name=Demo 1.0" in Path(copy.desktop_path).read_text()
    assert registry().get("demo").version == "2.0"


def test_repair_refuses_a_file_that_is_another_app_now(downloads, isolated_env):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    other = make_fake_appimage(downloads / "Other.AppImage", {
        "other.desktop": "[Desktop Entry]\nType=Application\nName=Other\nExec=AppRun\n",
        "AppRun": "#!/bin/sh\n"})
    shutil.copyfile(other, app.appimage_path)
    launcher = Path(app.desktop_path).read_text()
    with pytest.raises(InstallError, match="Demo cannot be repaired automatically") as caught:
        repair("demo", Scope.USER)
    assert "'demo' -> 'other'" in caught.value.details
    assert Path(app.desktop_path).read_text() == launcher and registry().get("demo") == app


def test_repair_of_a_missing_or_unknown_app(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    Path(app.appimage_path).unlink()
    with pytest.raises(EasyInstallerError):
        repair("demo", Scope.USER)
    assert registry().get("demo") == app
    with pytest.raises(NotInstalledError):
        repair("nope", Scope.USER)
    with pytest.raises(NotInstalledError):
        plan_repair("demo", Scope.SYSTEM)


def test_repair_never_refuses_an_installed_app_for_its_architecture(downloads):
    path = demo(downloads / "Demo-1.0-aarch64.AppImage", "1.0", machine=EM_AARCH64)
    app = install(path, InstallOptions(allow_foreign_arch=True))
    Path(app.desktop_path).unlink()
    assert repair("demo", Scope.USER).arch == "aarch64"


def test_rollback_never_refuses_a_kept_version_for_its_architecture(downloads):
    from easy_installer.core.installer import rollback

    allow = InstallOptions(allow_foreign_arch=True)
    install(demo(downloads / "Demo-1.0-aarch64.AppImage", "1.0", machine=EM_AARCH64), allow)
    install(demo(downloads / "Demo-1.1-aarch64.AppImage", "1.1", machine=EM_AARCH64), allow)
    back = rollback("demo", Scope.USER)
    assert back.version == "1.0" and back.arch == "aarch64" and back.previous["version"] == "1.1"


def test_repair_of_a_system_app_goes_through_the_helper(downloads, sys_layout, helper):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"), InstallOptions(scope=Scope.SYSTEM))
    app = install(demo(downloads / "Demo-1.1.AppImage", "1.1"), InstallOptions(scope=Scope.SYSTEM))
    assert app.previous["version"] == "1.0"
    Path(app.desktop_path).unlink()
    plan = plan_repair("demo", Scope.SYSTEM)
    try:
        assert plan.requires_root and plan.action == "reinstall" and plan.backup_target is None
    finally:
        plan.info.cleanup()

    repaired = repair("demo", Scope.SYSTEM)
    op, manifest = helper[-1]
    assert op == "install" and manifest["source_appimage"] == app.appimage_path
    assert manifest["keep_backup"] is False and manifest["consume_backup"] is False
    assert Path(app.desktop_path).is_file() and Path(app.appimage_path).is_file()
    assert repaired.version == "1.1" and repaired.previous == app.previous
    assert Path(repaired.previous["path"]).is_file()
    assert registry(sys_layout).get("demo") == repaired
