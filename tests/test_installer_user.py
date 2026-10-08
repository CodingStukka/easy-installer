"""Installer tests: user scope end-to-end, AppArmor fallback, system scope via a fake helper."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from easy_installer.core import installer, privileged
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    execute_install,
    find_installed,
    find_uninstall_launcher,
    list_installed,
    plan_install,
    refresh_desktop_caches,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import SandboxFix
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import (
    AuthorizationError,
    HelperError,
    InstallError,
    NotInstalledError,
    UnsupportedArchitectureError,
)

from fakeappimage import (
    ANYTYPE_DESKTOP,
    EM_AARCH64,
    T3_DESKTOP,
    make_fake_appimage,
    make_png,
    make_sample_appimage,
    requires_mksquashfs,
    requires_unsquashfs,
)

pytestmark = [requires_mksquashfs, requires_unsquashfs]

T3_FILE = "T3-Code-0.0.42-x86_64.AppImage"
T3_TARGET = "T3-Code-Alpha.AppImage"


# ------------------------------------------------------------------------------------------------
# fixtures & helpers
# ------------------------------------------------------------------------------------------------


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
    """Deterministic system status (no cache tools) and no uninstall launcher by default."""
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)


@pytest.fixture
def status():
    return make_status()


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


def t3_variant(path: Path, *, version="0.0.42", name="T3 Code (Alpha)", **kw) -> Path:
    desktop = T3_DESKTOP.replace("X-AppImage-Version=0.0.42", f"X-AppImage-Version={version}")
    desktop = desktop.replace("Name=T3 Code (Alpha)", f"Name={name}")
    icon = "usr/share/icons/hicolor/512x512/apps/t3code.png"
    return make_fake_appimage(path, {
        "t3code.desktop": desktop,
        "AppRun": "#!/bin/sh\n",
        "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": f"asar {version} {name}",
        icon: make_png(512, 512),
    }, {".DirIcon": icon}, **kw)


def electron_app(path: Path) -> Path:
    """Anytype-like Electron app whose Exec does NOT contain --no-sandbox."""
    icon = "usr/share/icons/hicolor/256x256/apps/anytype.png"
    return make_fake_appimage(path, {
        "anytype.desktop": ANYTYPE_DESKTOP,
        "AppRun": "#!/bin/sh\n",
        "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": b"asar",
        icon: make_png(256, 256),
    }, {".DirIcon": icon})


def all_files(*roots: Path) -> list[Path]:
    found = []
    for root in roots:
        if root.exists():
            found += [p for p in root.rglob("*") if p.is_file() or p.is_symlink()]
    return sorted(found)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_desktop(path: Path) -> DesktopEntry:
    return DesktopEntry.parse(path.read_text(encoding="utf-8"))


class HelperRecorder:
    def __init__(self, result=None, exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result
        self.exc = exc

    def __call__(self, op, payload, *, timeout=600):
        self.calls.append((op, payload))
        if self.exc is not None:
            raise self.exc
        return self.result(op, payload) if callable(self.result) else (self.result or {"ok": True})


# ------------------------------------------------------------------------------------------------
# user scope: basic flow
# ------------------------------------------------------------------------------------------------


def test_install_moves_file_and_integrates(downloads, infos, status, isolated_env):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    info = infos(src)
    plan = plan_install(info, status=status)
    layout = user_layout()
    assert plan.action == "install"
    assert plan.existing is None
    assert plan.target_appimage == layout.apps_dir / T3_TARGET
    assert plan.desktop_path == layout.desktop_dir / "easyinstaller-t3code.desktop"
    assert plan.icon_name == "easyinstaller-t3code"
    assert plan.icon_target == layout.icons_dir / "512x512/apps/easyinstaller-t3code.png"
    assert plan.requires_root is False
    assert plan.in_place is False
    assert plan.sandbox_fix is SandboxFix.NONE  # T3's Exec already has --no-sandbox
    assert plan.extract_and_run is False

    events = []
    app = execute_install(plan, progress=lambda f, m: events.append((f, m)))

    target = layout.apps_dir / T3_TARGET
    assert not src.exists()
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert sha256(target) == info.sha256
    desktop = plan.desktop_path
    assert stat.S_IMODE(desktop.stat().st_mode) == 0o644
    entry = read_desktop(desktop)
    assert entry.get("Exec").startswith(str(target))
    assert entry.get("TryExec") == str(target)
    assert entry.get("Icon") == "easyinstaller-t3code"
    assert entry.get("X-EasyInstaller-Id") == "t3code"
    assert entry.get("X-EasyInstaller-Scope") == "user"
    assert plan.icon_target.is_file()
    assert plan.icon_target.read_bytes() == make_png(512, 512)

    assert app.id == "t3code"
    assert app.scope is Scope.USER
    assert app.version == "0.0.42"
    assert app.appimage_path == str(target)
    assert app.desktop_path == str(desktop)
    assert app.icon_paths == [str(plan.icon_target)]
    assert app.icon_path == str(plan.icon_target)
    assert app.original_filename == T3_FILE
    assert app.sandbox_fix == "none"
    assert app.status() == "ok"
    assert app.installed_at and app.installed_at == app.updated_at
    assert Registry(layout.registry_path).get("t3code") == app

    fractions = [f for f, _ in events if f is not None]
    assert fractions == sorted(fractions)
    assert events[-1][0] == 1.0
    assert all(isinstance(m, str) and m for _, m in events)
    # no temporary files left behind
    leftovers = [p for p in all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir)
                 if p.name.startswith(".")]
    assert leftovers == []


def test_install_keep_original_copies(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    before = sha256(src)
    plan = plan_install(infos(src), InstallOptions(keep_original=True), status=status)
    events = []
    app = execute_install(plan, progress=lambda f, m: events.append((f, m)))
    assert src.is_file() and sha256(src) == before
    assert sha256(Path(app.appimage_path)) == before
    assert app.sha256 == before
    assert len([f for f, _ in events if f is not None and 0 < f < 1]) >= 2


def test_install_without_hash_still_records_hash_when_copying(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    info = infos(src, compute_hash=False)
    app = execute_install(plan_install(info, InstallOptions(keep_original=True), status=status))
    assert app.sha256 == sha256(src)


def test_in_place_reinstall(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    first = execute_install(plan_install(infos(src), status=status))
    target = Path(first.appimage_path)
    os.chmod(target, 0o644)  # e.g. lost its exec bit

    plan = plan_install(infos(target), status=status)
    assert plan.in_place is True
    assert plan.action == "reinstall"
    assert plan.existing == first
    app = execute_install(plan)
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert app.original_filename == T3_FILE  # remembered from the first install
    assert app.installed_at == first.installed_at
    assert len(Registry(user_layout().registry_path).load()) == 1
    assert Path(app.desktop_path).is_file()


def test_update_replaces_old_files(downloads, infos, status):
    v1 = t3_variant(downloads / "T3-Code-0.0.42-x86_64.AppImage", version="0.0.42")
    old = execute_install(plan_install(infos(v1), status=status))
    old_target = Path(old.appimage_path)
    assert old_target.name == T3_TARGET

    v2 = t3_variant(downloads / "T3-Code-0.1.0-x86_64.AppImage", version="0.1.0", name="T3 Code")
    plan = plan_install(infos(v2), status=status)
    assert plan.action == "update"
    assert plan.existing is not None and plan.existing.version == "0.0.42"
    assert plan.target_appimage.name == "T3-Code.AppImage"
    new = execute_install(plan)

    assert not old_target.exists()  # different path -> removed
    assert Path(new.appimage_path).is_file()
    assert new.version == "0.1.0"
    assert new.installed_at == old.installed_at
    assert new.desktop_path == old.desktop_path
    assert read_desktop(Path(new.desktop_path)).get("Name") == "T3 Code"
    apps_dir = user_layout().apps_dir
    # the replaced version is kept for a while (tests/test_backups.py), nothing else remains
    assert sorted(p.name for p in apps_dir.iterdir()) == [".easyinstaller-backups",
                                                          "T3-Code.AppImage"]
    assert new.previous["version"] == "0.0.42" and Path(new.previous["path"]).is_file()


def test_update_same_path_overwrites(downloads, infos, status):
    v1 = t3_variant(downloads / "a.AppImage", version="1.0")
    execute_install(plan_install(infos(v1), status=status))
    v2 = t3_variant(downloads / "b.AppImage", version="1.1")
    info2 = infos(v2)
    new = execute_install(plan_install(info2, status=status))
    assert Path(new.appimage_path).name == T3_TARGET
    assert sha256(Path(new.appimage_path)) == info2.sha256
    apps_dir = user_layout().apps_dir
    assert sorted(p.name for p in apps_dir.iterdir()) == [".easyinstaller-backups", T3_TARGET]


def test_update_without_backup_leaves_only_the_new_file(downloads, infos, status):
    """``keep_backup=False`` (or "backup_days": 0 in the settings): exactly the 0.1 behaviour."""
    v1 = t3_variant(downloads / "a.AppImage", version="1.0")
    execute_install(plan_install(infos(v1), status=status))
    v2 = t3_variant(downloads / "b.AppImage", version="1.1", name="T3 Code")
    plan = plan_install(infos(v2), InstallOptions(keep_backup=False), status=status)
    assert plan.backup_target is None and plan.keep_backup is False
    new = execute_install(plan)
    assert new.previous is None
    assert [p.name for p in user_layout().apps_dir.iterdir()] == ["T3-Code.AppImage"]


def test_downgrade_and_reinstall_actions(downloads, infos, status):
    v2 = t3_variant(downloads / "new.AppImage", version="2.0")
    execute_install(plan_install(infos(v2), status=status))
    v1 = t3_variant(downloads / "old.AppImage", version="1.9")
    plan = plan_install(infos(v1), status=status)
    assert plan.action == "downgrade"
    assert any("2.0" in w for w in plan.warnings)
    same = t3_variant(downloads / "same.AppImage", version="2.0")
    assert plan_install(infos(same), status=status).action == "reinstall"


def test_collision_with_other_registered_app(downloads, infos, status):
    layout = user_layout()
    layout.apps_dir.mkdir(parents=True)
    foreign = layout.apps_dir / T3_TARGET
    foreign.write_bytes(b"someone else's app")
    Registry(layout.registry_path).put(InstalledApp(
        id="other.app", name="Other", version="1", scope=Scope.USER,
        appimage_path=str(foreign), desktop_path=str(layout.desktop_dir / "x.desktop"),
    ))
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    assert plan.target_appimage != foreign
    assert plan.target_appimage.parent == layout.apps_dir
    assert "t3code" in plan.target_appimage.name
    execute_install(plan)
    assert foreign.read_bytes() == b"someone else's app"


def test_collision_with_unregistered_file(downloads, infos, status):
    layout = user_layout()
    layout.apps_dir.mkdir(parents=True)
    foreign = layout.apps_dir / T3_TARGET
    foreign.write_bytes(b"manually placed file")
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    assert plan.target_appimage != foreign
    app = execute_install(plan)
    assert foreign.read_bytes() == b"manually placed file"
    assert Path(app.appimage_path).is_file()


# ------------------------------------------------------------------------------------------------
# user scope: failures and rollback
# ------------------------------------------------------------------------------------------------


def _fail_desktop_write(monkeypatch, err=errno.ENOSPC):
    def boom(path, text):
        # leave a partial temp file behind like a real failing write would, then fail
        raise OSError(err, os.strerror(err), str(path))

    monkeypatch.setattr(installer, "_write_desktop_file", boom)


def test_rollback_restores_moved_source(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    os.chmod(src, 0o644)
    digest = sha256(src)
    plan = plan_install(infos(src), status=status)
    _fail_desktop_write(monkeypatch)
    with pytest.raises(InstallError) as excinfo:
        execute_install(plan)
    assert "disk space" in str(excinfo.value)
    assert excinfo.value.details
    assert src.is_file() and sha256(src) == digest
    assert stat.S_IMODE(src.stat().st_mode) == 0o644  # mode restored too
    layout = user_layout()
    assert all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir) == []
    assert Registry(layout.registry_path).load() == {}


def test_rollback_after_cross_device_copy(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    digest = sha256(src)
    plan = plan_install(infos(src), status=status)
    monkeypatch.setattr(installer, "_rename",
                        lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    _fail_desktop_write(monkeypatch, errno.EIO)
    with pytest.raises(InstallError):
        execute_install(plan)
    assert src.is_file() and sha256(src) == digest
    layout = user_layout()
    assert all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir) == []


def test_rollback_on_update_restores_previous_version(downloads, infos, status, monkeypatch):
    v1 = t3_variant(downloads / "v1.AppImage", version="1.0")
    info1 = infos(v1)
    old = execute_install(plan_install(info1, status=status))
    old_desktop = Path(old.desktop_path).read_text()
    old_icon = Path(old.icon_paths[0]).read_bytes()

    v2 = t3_variant(downloads / "v2.AppImage", version="2.0")
    plan = plan_install(infos(v2), status=status)
    _fail_desktop_write(monkeypatch)
    with pytest.raises(InstallError):
        execute_install(plan)
    assert v2.is_file()
    assert sha256(Path(old.appimage_path)) == info1.sha256
    assert Path(old.desktop_path).read_text() == old_desktop
    assert Path(old.icon_paths[0]).read_bytes() == old_icon
    assert Registry(user_layout().registry_path).get("t3code") == old
    layout = user_layout()
    hidden = [p for p in all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir)
              if p.name.startswith(".")]
    assert hidden == []


def test_unexpected_exception_also_rolls_back(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)

    def broken_put(self, app):
        raise RuntimeError("bug")

    monkeypatch.setattr(Registry, "put", broken_put)
    with pytest.raises(RuntimeError):
        execute_install(plan)
    assert src.is_file()
    layout = user_layout()
    assert all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir) == []


def test_cross_device_move_copies_then_deletes(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    info = infos(src)
    monkeypatch.setattr(installer, "_rename",
                        lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    monkeypatch.setattr(installer, "CHUNK_SIZE", 1024)
    events = []
    app = execute_install(plan_install(info, status=status), progress=lambda f, m: events.append(f))
    assert not src.exists()
    assert sha256(Path(app.appimage_path)) == info.sha256
    assert len([f for f in events if f is not None and 0.05 < f < 0.85]) >= 2


def test_cross_device_move_unlink_failure_is_only_a_warning(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    monkeypatch.setattr(installer, "_rename",
                        lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))

    def ro_unlink(path):
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(installer, "_unlink_source", ro_unlink)
    app = execute_install(plan)
    assert src.is_file()
    assert Path(app.appimage_path).is_file()
    assert any(T3_FILE in w for w in plan.warnings)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_read_only_source_folder_copies_and_warns(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    downloads.chmod(0o555)
    try:
        app = execute_install(plan)
    finally:
        downloads.chmod(0o755)
    assert src.is_file()
    assert Path(app.appimage_path).is_file()
    assert any(T3_FILE in w for w in plan.warnings)


def test_disk_full_during_copy(downloads, infos, status, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), InstallOptions(keep_original=True), status=status)

    def full(source, part, report, message, start, end):
        part.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device", str(part))

    monkeypatch.setattr(installer, "_copy_with_progress", full)
    with pytest.raises(InstallError) as excinfo:
        execute_install(plan)
    assert "disk space" in str(excinfo.value)
    assert src.is_file()
    layout = user_layout()
    assert all_files(layout.apps_dir, layout.desktop_dir, layout.icons_dir) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_permission_denied(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    apps_dir = user_layout().apps_dir
    apps_dir.mkdir(parents=True)
    apps_dir.chmod(0o555)
    try:
        with pytest.raises(InstallError) as excinfo:
            execute_install(plan)
    finally:
        apps_dir.chmod(0o755)
    assert "not allowed" in str(excinfo.value)
    assert src.is_file()
    assert list(apps_dir.iterdir()) == []


def test_source_changed_after_inspection(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    with open(src, "ab") as fh:
        fh.write(b"more bytes")
    with pytest.raises(InstallError):
        execute_install(plan)
    assert src.is_file()


def test_source_vanished(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    src.unlink()
    with pytest.raises(InstallError):
        execute_install(plan)


# ------------------------------------------------------------------------------------------------
# sandbox (Electron on Ubuntu 24.04+)
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def restricted():
    return make_status(userns_restricted=True)


def test_apparmor_fix_calls_helper(downloads, infos, restricted, monkeypatch):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    info = infos(src)
    assert info.is_electron and not info.exec_has_no_sandbox
    plan = plan_install(info, status=restricted)
    assert plan.sandbox_fix is SandboxFix.APPARMOR
    assert plan.requires_root is True
    assert plan.apparmor_profile_text and str(plan.target_appimage) in plan.apparmor_profile_text
    assert "--no-sandbox" not in read_desktop_text(plan.desktop_text).get("Exec")

    helper = HelperRecorder(result={"ok": True,
                                    "profile_path": "/etc/apparmor.d/easyinstaller-anytype"})
    monkeypatch.setattr(privileged, "run_helper", helper)
    app = execute_install(plan)
    assert helper.calls == [("apparmor-install",
                             {"app_id": "anytype", "appimage_path": str(plan.target_appimage)})]
    assert app.sandbox_fix == "apparmor"
    assert app.apparmor_profile == "/etc/apparmor.d/easyinstaller-anytype"
    assert "--no-sandbox" not in read_desktop(Path(app.desktop_path)).get("Exec")

    # without the administrator's password (standard user, no polkit agent, Cancel) nothing
    # changes - but the app can still be removed, keeping only its permission
    helper.calls.clear()
    helper.exc = AuthorizationError("You are not allowed to do this, or the password was not "
                                    "accepted.")
    with pytest.raises(AuthorizationError):
        uninstall("anytype", Scope.USER)
    assert find_installed("anytype") and Path(app.appimage_path).is_file()
    helper.calls.clear()
    notes = uninstall("anytype", Scope.USER, keep_permission=True)
    assert helper.calls == []
    assert notes == ["The special permission that Anytype needed was left on this computer, "
                     "because only an administrator can remove it."]
    assert not find_installed("anytype") and not Path(app.appimage_path).exists()
    assert not Path(app.desktop_path).exists()

    # uninstall asks the helper to remove the profile again
    helper.exc = None
    app = execute_install(plan_install(infos(electron_app(downloads / "Anytype-0.55.4.AppImage")),
                                       status=restricted))
    helper.calls.clear()
    uninstall("anytype", Scope.USER)
    assert helper.calls == [("apparmor-remove", {"app_id": "anytype"})]
    assert not Path(app.appimage_path).exists()


def read_desktop_text(text: str) -> DesktopEntry:
    return DesktopEntry.parse(text)


@pytest.mark.parametrize("exc", [
    AuthorizationError("Authentication was cancelled."),
    HelperError("helper failed"),
])
def test_apparmor_failure_falls_back_to_no_sandbox(downloads, infos, restricted, monkeypatch, exc):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    plan = plan_install(infos(src), status=restricted)
    before = list(plan.warnings)
    assert any("password" in w for w in before)
    helper = HelperRecorder(exc=exc)
    monkeypatch.setattr(privileged, "run_helper", helper)
    app = execute_install(plan)
    assert [c[0] for c in helper.calls] == ["apparmor-install"]
    assert app.sandbox_fix == "no-sandbox"
    assert app.apparmor_profile is None
    assert "--no-sandbox" in read_desktop(Path(app.desktop_path)).get("Exec")
    new = [w for w in plan.warnings if w not in before]
    assert len(new) == 1 and "sandbox" in new[0]
    assert not any("password" in w for w in plan.warnings)  # the promise no longer applies
    assert Registry(user_layout().registry_path).get("anytype").sandbox_fix == "no-sandbox"


def test_apparmor_with_pkexec_disabled_uses_real_run_helper(downloads, infos, restricted):
    # conftest sets EASY_INSTALLER_DISABLE_PKEXEC=1 -> real run_helper raises HelperError
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    app = execute_install(plan_install(infos(src), status=restricted))
    assert app.sandbox_fix == "no-sandbox"


def test_explicit_no_sandbox(downloads, infos, restricted, monkeypatch):
    helper = HelperRecorder()
    monkeypatch.setattr(privileged, "run_helper", helper)
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    plan = plan_install(infos(src), InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX),
                        status=restricted)
    assert plan.sandbox_fix is SandboxFix.NO_SANDBOX
    assert plan.requires_root is False
    app = execute_install(plan)
    assert helper.calls == []
    assert "--no-sandbox" in read_desktop(Path(app.desktop_path)).get("Exec")


def test_no_apparmor_available_defaults_to_no_sandbox(downloads, infos):
    status = make_status(userns_restricted=True, apparmor_parser=None)
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    plan = plan_install(infos(src), status=status)
    assert plan.sandbox_fix is SandboxFix.NO_SANDBOX
    plan2 = plan_install(infos(src), InstallOptions(sandbox_fix=SandboxFix.APPARMOR), status=status)
    assert plan2.sandbox_fix is SandboxFix.NO_SANDBOX


def test_sandbox_fix_not_needed_without_restriction(downloads, infos, status):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    plan = plan_install(infos(src), InstallOptions(sandbox_fix=SandboxFix.APPARMOR), status=status)
    assert plan.sandbox_fix is SandboxFix.NONE
    assert plan.apparmor_profile_text is None


# ------------------------------------------------------------------------------------------------
# FUSE / architecture / warnings / launcher
# ------------------------------------------------------------------------------------------------


def test_extract_and_run_automatic(downloads, infos):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")  # dynamic runtime
    info = infos(src)
    plan = plan_install(info, status=make_status(libfuse2=False))
    assert plan.extract_and_run is True
    assert any("FUSE" in w for w in plan.warnings)
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in read_desktop_text(plan.desktop_text).get("Exec")
    app = execute_install(plan)
    assert app.extract_and_run is True

    ok = plan_install(info, status=make_status())
    assert ok.extract_and_run is False
    forced = plan_install(info, InstallOptions(extract_and_run=False),
                          status=make_status(dev_fuse=False))
    assert forced.extract_and_run is False


def test_foreign_architecture(downloads, infos, status):
    src = make_sample_appimage(downloads / "Foreign.AppImage", "openscad", machine=EM_AARCH64)
    info = infos(src)
    if installer.host_arch() == "aarch64":  # pragma: no cover
        pytest.skip("host is aarch64")
    with pytest.raises(UnsupportedArchitectureError):
        plan_install(info, status=status)
    plan = plan_install(info, InstallOptions(allow_foreign_arch=True), status=status)
    assert any("aarch64" in w for w in plan.warnings)


def test_plan_warnings_are_friendly_sentences(downloads, infos):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    status = make_status(userns_restricted=True, libfuse2=False)
    plans = [
        plan_install(infos(src), status=status),
        plan_install(infos(src), InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX), status=status),
        plan_install(infos(src), InstallOptions(sandbox_fix=SandboxFix.NONE), status=status),
    ]
    for plan in plans:
        assert plan.warnings
        for warning in plan.warnings:
            assert warning[0].isupper(), warning
            assert warning.rstrip().endswith((".", "!")), warning
            for jargon in ("APPIMAGE_EXTRACT_AND_RUN", "--no-sandbox", "userns", "AppArmor",
                           "pkexec", "libfuse"):
                assert jargon not in warning, warning


def test_uninstall_action_when_launcher_found(downloads, infos, status, monkeypatch):
    monkeypatch.setattr(installer, "find_uninstall_launcher",
                        lambda scope=None: "/usr/bin/easy-installer")
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), status=status)
    assert plan.uninstall_command == ("/usr/bin/easy-installer", "--uninstall", "t3code")
    entry = read_desktop_text(plan.desktop_text)
    assert "easyinstaller-uninstall" in entry.get_list("Actions")
    plan2 = plan_install(infos(src), InstallOptions(add_uninstall_action=False), status=status)
    assert plan2.uninstall_command is None


class TestFindUninstallLauncher:
    @pytest.fixture(autouse=True)
    def real_function(self, monkeypatch):
        # undo the module-wide stub from quiet_system
        monkeypatch.setattr(installer, "find_uninstall_launcher", find_uninstall_launcher)

    def _launcher(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "easy-installer"
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def test_none_from_source_tree(self, monkeypatch):
        repo = Path(installer.__file__).resolve().parents[3]
        fake_venv_launcher = repo / ".venv" / "bin" / "easy-installer"
        monkeypatch.setattr(installer, "_launcher_candidates", lambda: [fake_venv_launcher])
        assert find_uninstall_launcher() is None

    def test_none_for_temp_dir_launcher(self, tmp_path, monkeypatch):
        launcher = self._launcher(tmp_path / "bin")
        monkeypatch.setattr(installer, "_launcher_candidates", lambda: [launcher])
        assert find_uninstall_launcher() is None

    def test_installed_launcher_found(self, tmp_path, monkeypatch):
        launcher = self._launcher(tmp_path / "usr" / "bin")
        monkeypatch.setattr(installer, "_launcher_candidates",
                            lambda: [tmp_path / "missing" / "easy-installer", launcher])
        monkeypatch.setattr(installer, "_transient_roots", lambda: [tmp_path / "elsewhere"])
        assert find_uninstall_launcher() == str(launcher)

    def test_non_executable_ignored(self, tmp_path, monkeypatch):
        launcher = self._launcher(tmp_path / "bin")
        launcher.chmod(0o644)
        monkeypatch.setattr(installer, "_launcher_candidates", lambda: [launcher])
        monkeypatch.setattr(installer, "_transient_roots", lambda: [])
        assert find_uninstall_launcher() is None

    def test_system_scope_only_uses_launchers_the_helper_trusts(self, tmp_path, monkeypatch):
        """~/.local/bin (make install-user) comes first on PATH, but the helper would drop it."""
        from easy_installer.helper import ops

        user = self._launcher(tmp_path / "home" / ".local" / "bin")
        system = self._launcher(tmp_path / "usr" / "bin")
        monkeypatch.setattr(installer, "_launcher_candidates", lambda: [user, system])
        monkeypatch.setattr(installer, "_transient_roots", lambda: [])
        monkeypatch.setattr(ops, "is_trusted_executable", lambda path, *, name: path == str(system))
        assert find_uninstall_launcher() == str(user)
        assert find_uninstall_launcher(Scope.SYSTEM) == str(system)
        monkeypatch.setattr(ops, "is_trusted_executable", lambda path, *, name: False)
        assert find_uninstall_launcher(Scope.SYSTEM) is None

    def test_real_call_is_not_the_source_tree(self):
        result = find_uninstall_launcher()
        repo = Path(installer.__file__).resolve().parents[3]
        if result is not None:
            assert os.path.isabs(result)
            assert not Path(result).resolve().is_relative_to(repo)


# ------------------------------------------------------------------------------------------------
# uninstall / listing (user scope)
# ------------------------------------------------------------------------------------------------


def test_uninstall_user(downloads, infos, status, isolated_env):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    app = execute_install(plan_install(infos(src), status=status))
    user_data = isolated_env / ".config" / "t3code"
    user_data.mkdir(parents=True)
    (user_data / "settings.json").write_text("{}")
    events = []
    uninstall("t3code", Scope.USER, progress=lambda f, m: events.append(f))
    assert not Path(app.appimage_path).exists()
    assert not Path(app.desktop_path).exists()
    assert not Path(app.icon_paths[0]).exists()
    assert Registry(user_layout().registry_path).get("t3code") is None
    assert (user_data / "settings.json").is_file()
    assert events[-1] == 1.0
    with pytest.raises(NotInstalledError):
        uninstall("t3code", Scope.USER)


def test_uninstall_with_missing_files(downloads, infos, status):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    app = execute_install(plan_install(infos(src), status=status))
    Path(app.appimage_path).unlink()
    Path(app.desktop_path).unlink()
    uninstall("t3code", Scope.USER)
    assert find_installed("t3code") == []


def test_uninstall_does_not_delete_unexpected_paths(isolated_env):
    important = isolated_env / "Documents" / "thesis.odt"
    important.parent.mkdir()
    important.write_text("precious")
    layout = user_layout()
    Registry(layout.registry_path).put(InstalledApp(
        id="weird", name="Weird", version=None, scope=Scope.USER,
        appimage_path=str(isolated_env / "missing.AppImage"),
        desktop_path=str(important), icon_paths=[str(important)], icon_name="easyinstaller-weird",
    ))
    uninstall("weird", Scope.USER)
    assert important.read_text() == "precious"


def test_list_and_find_installed(downloads, infos, status, system_root, monkeypatch):
    sys_layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: sys_layout if Scope(scope) is Scope.SYSTEM else user_layout())
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    execute_install(plan_install(infos(src), status=status))
    Registry(sys_layout.registry_path).put(InstalledApp(
        id="t3code", name="T3 Code (Alpha)", version="0.0.41", scope=Scope.SYSTEM,
        appimage_path=str(sys_layout.apps_dir / T3_TARGET),
        desktop_path=str(sys_layout.desktop_dir / "easyinstaller-t3code.desktop"),
    ))
    Registry(sys_layout.registry_path).put(InstalledApp(
        id="aaa", name="aardvark", version="1", scope=Scope.SYSTEM,
        appimage_path="/opt/appimages/a.AppImage", desktop_path="/x.desktop",
    ))
    apps = list_installed()
    assert [(a.id, a.scope) for a in apps] == [
        ("aaa", Scope.SYSTEM), ("t3code", Scope.SYSTEM), ("t3code", Scope.USER)]
    assert [a.id for a in list_installed([Scope.USER])] == ["t3code"]
    assert {a.scope for a in find_installed("t3code")} == {Scope.USER, Scope.SYSTEM}
    assert find_installed("nope") == []
    # the other-scope install is reported by the planner
    plan = plan_install(infos(make_sample_appimage(downloads / "again.AppImage", "t3code")),
                        status=status)
    assert plan.existing_other_scope is not None
    assert any("everyone" in w for w in plan.warnings)


def test_refresh_desktop_caches_runs_tools(isolated_env, monkeypatch):
    layout = user_layout()
    layout.desktop_dir.mkdir(parents=True)
    layout.icons_dir.mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status(
        update_desktop_database="/usr/bin/update-desktop-database",
        icon_cache_tool="/usr/bin/gtk-update-icon-cache"))
    refresh_desktop_caches(layout)
    assert calls == [["/usr/bin/update-desktop-database", str(layout.desktop_dir)]]
    (layout.icons_dir / "icon-theme.cache").write_bytes(b"")
    calls.clear()
    refresh_desktop_caches(layout)
    assert calls[1] == ["/usr/bin/gtk-update-icon-cache", "-f", "-t", str(layout.icons_dir)]


def test_install_and_uninstall_bump_icon_theme_mtime(downloads, infos, status):
    """Running desktops only rescan hicolor when its folder's mtime changes (no cache here)."""
    layout = user_layout()
    size_dir = layout.icons_dir / "512x512" / "apps"
    size_dir.mkdir(parents=True)  # e.g. from an earlier install: adding a file here does not
    os.utime(layout.icons_dir, (1_000_000, 1_000_000))  # change the hicolor folder's mtime
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    app = execute_install(plan_install(infos(src), status=status))
    assert Path(app.icon_paths[0]).parent == size_dir
    assert not (layout.icons_dir / "icon-theme.cache").exists()
    assert layout.icons_dir.stat().st_mtime > 1_000_000

    os.utime(layout.icons_dir, (1_000_000, 1_000_000))
    uninstall(app.id, Scope.USER)
    assert layout.icons_dir.stat().st_mtime > 1_000_000


def test_refresh_desktop_caches_never_raises(isolated_env, monkeypatch):
    def failing(cmd, **kw):
        raise OSError("tool vanished")

    monkeypatch.setattr(installer.subprocess, "run", failing)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status(
        update_desktop_database="/usr/bin/update-desktop-database"))
    layout = user_layout()
    layout.desktop_dir.mkdir(parents=True)
    refresh_desktop_caches(layout)  # no exception


# ------------------------------------------------------------------------------------------------
# system scope (helper faked)
# ------------------------------------------------------------------------------------------------

MANIFEST_KEYS = {
    "app_id", "name", "version", "comment", "source_appimage", "sha256", "embedded_desktop",
    "embedded_desktop_filename", "icon_source", "extra_args", "extract_and_run",
    "extract_and_run_explicit", "apparmor", "uninstall_command", "arch", "size", "update_info",
    "original_filename", "is_electron", "mime_types",
    # 0.2: backups, "keep both" and what the helper only stores for the client
    "keep_backup", "consume_backup", "backup_max_age_days", "base_id", "pinned", "update_source",
    "origin_url", "signature", "data_hints",
}


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


def fake_system_install(layout):
    def result(op, manifest):
        if op != "install":
            return {"ok": True}
        return {"ok": True, "app": InstalledApp(
            id=manifest["app_id"], name=manifest["name"], version=manifest["version"],
            scope=Scope.SYSTEM, appimage_path=str(layout.apps_dir / "X.AppImage"),
            desktop_path=str(layout.desktop_dir / f"easyinstaller-{manifest['app_id']}.desktop"),
            sha256=manifest["sha256"], size=manifest["size"], arch=manifest["arch"],
        ).to_dict()}
    return result


def test_system_install_sends_manifest(downloads, infos, status, sys_layout, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    info = infos(src)
    plan = plan_install(info, InstallOptions(scope=Scope.SYSTEM), status=status)
    assert plan.requires_root is True
    assert plan.layout == sys_layout
    assert plan.target_appimage.parent == sys_layout.apps_dir
    assert plan.desktop_path == sys_layout.desktop_dir / "easyinstaller-t3code.desktop"

    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    app = execute_install(plan)
    assert [op for op, _ in helper.calls] == ["install"]
    manifest = helper.calls[0][1]
    assert set(manifest) == MANIFEST_KEYS
    assert manifest["app_id"] == "t3code"
    assert manifest["name"] == "T3 Code (Alpha)"
    assert manifest["version"] == "0.0.42"
    assert manifest["source_appimage"] == str(src)
    assert manifest["sha256"] == info.sha256
    assert "Name=T3 Code (Alpha)" in manifest["embedded_desktop"]
    assert manifest["embedded_desktop_filename"] == "t3code.desktop"
    assert manifest["icon_source"] and Path(manifest["icon_source"]).is_file()
    assert manifest["extra_args"] == []
    assert manifest["extract_and_run"] is False
    assert manifest["extract_and_run_explicit"] is False
    assert manifest["apparmor"] is False
    assert manifest["uninstall_command"] is None
    assert manifest["arch"] == "x86_64"
    assert manifest["size"] == info.size
    assert manifest["original_filename"] == T3_FILE
    assert manifest["is_electron"] is True
    assert manifest["keep_backup"] is True and manifest["consume_backup"] is False
    assert manifest["backup_max_age_days"] == 14   # the default of the settings
    assert manifest["base_id"] is None and manifest["pinned"] is False
    assert manifest["update_source"] is None and manifest["origin_url"] is None
    assert manifest["signature"] is None
    assert manifest["data_hints"] == ["T3 Code (Alpha)", "t3code", "t3code-updater"]
    assert app.scope is Scope.SYSTEM
    assert app.id == "t3code"
    assert not src.exists()  # user-owned source deleted after success
    assert user_layout().apps_dir.exists() is False  # nothing written to the user layout


def test_system_install_keep_original_and_options(downloads, infos, sys_layout, monkeypatch):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    status = make_status(userns_restricted=True, dev_fuse=False)
    monkeypatch.setattr(installer, "find_uninstall_launcher",
                        lambda scope=None: "/usr/bin/easy-installer")
    plan = plan_install(infos(src, compute_hash=False),
                        InstallOptions(scope=Scope.SYSTEM, keep_original=True), status=status)
    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    execute_install(plan)
    manifest = helper.calls[0][1]
    assert manifest["apparmor"] is True
    assert manifest["extra_args"] == []
    assert manifest["extract_and_run"] is True
    assert manifest["extract_and_run_explicit"] is False  # automatic: FUSE is missing
    assert manifest["uninstall_command"] == ["/usr/bin/easy-installer", "--uninstall", "anytype"]
    assert manifest["sha256"] == sha256(src)  # computed on demand
    assert src.is_file()


def test_system_install_no_sandbox_extra_args(downloads, infos, sys_layout, monkeypatch):
    src = electron_app(downloads / "Anytype-0.55.4.AppImage")
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM,
                                                   sandbox_fix=SandboxFix.NO_SANDBOX),
                        status=make_status(userns_restricted=True))
    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    execute_install(plan)
    assert helper.calls[0][1]["extra_args"] == ["--no-sandbox"]
    assert helper.calls[0][1]["apparmor"] is False


def test_system_install_auth_cancelled_keeps_source(downloads, infos, status, sys_layout,
                                                    monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status)
    monkeypatch.setattr(privileged, "run_helper",
                        HelperRecorder(exc=AuthorizationError("Authentication was cancelled.")))
    with pytest.raises(AuthorizationError):
        execute_install(plan)
    assert src.is_file()


def test_system_install_with_real_run_helper_disabled(downloads, infos, status, sys_layout):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status)
    with pytest.raises(HelperError):
        execute_install(plan)
    assert src.is_file()


def test_system_install_garbage_result(downloads, infos, status, sys_layout, monkeypatch):
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status)
    monkeypatch.setattr(privileged, "run_helper", HelperRecorder(result={"ok": True}))
    with pytest.raises(HelperError):
        execute_install(plan)
    assert src.is_file()


MOUNTINFO_SAMPLE = (
    "29 1 253:1 / / rw,relatime shared:1 - ext4 /dev/mapper/vg-root rw\n"
    "46 28 0:37 / /sys/fs/fuse/connections rw,nosuid shared:20 - fusectl fusectl rw\n"
    "1162 687 0:71 / /run/user/1000/gvfs rw,nosuid,nodev,relatime shared:1194 - "
    "fuse.gvfsd-fuse gvfsd-fuse rw,user_id=1000,group_id=1000\n"
    "1354 687 0:76 / /run/user/1000/doc rw,nosuid,nodev,relatime shared:1324 - "
    "fuse.portal portal rw,user_id=1000,group_id=1000\n"
    "1143 33 8:2 / /media/u/Games\\040&\\040More rw,nosuid,nodev shared:746 - fuseblk /dev/sda2 "
    "rw,user_id=0,group_id=0,default_permissions,allow_other,blksize=4096\n"
)


def test_mount_of_and_private_fuse_detection(tmp_path, monkeypatch):
    info = tmp_path / "mountinfo"
    info.write_text(MOUNTINFO_SAMPLE)
    monkeypatch.setattr(installer, "MOUNTINFO", str(info))
    smb = "/run/user/1000/gvfs/smb-share:server=nas,share=public/Tools-1.0-x86_64.AppImage"
    assert installer.mount_of(smb)[:2] == ("/run/user/1000/gvfs", "fuse.gvfsd-fuse")
    assert installer.mount_of("/media/u/Games & More/x.AppImage")[1] == "fuseblk"
    assert installer.mount_of("/run/user/1000/gvfsx/a")[0] == "/"
    assert installer.on_private_fuse_mount(smb)
    assert installer.on_private_fuse_mount("/run/user/1000/doc/1234/App.AppImage")
    assert not installer.on_private_fuse_mount("/media/u/Games & More/x.AppImage")  # allow_other
    assert not installer.on_private_fuse_mount("/home/u/Downloads/x.AppImage")
    monkeypatch.setattr(installer, "MOUNTINFO", str(tmp_path / "missing"))
    assert installer.on_private_fuse_mount(smb) is False


def test_system_install_from_private_fuse_mount_uses_local_copy(downloads, infos, status,
                                                                sys_layout, monkeypatch):
    """The root helper cannot enter gvfs/sshfs/portal mounts, so it gets a private local copy."""
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    digest = sha256(src)
    monkeypatch.setattr(installer, "on_private_fuse_mount", lambda p: Path(p).parent == downloads)
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status)
    seen = {}

    def helper(op, manifest):
        staged = Path(manifest["source_appimage"])
        seen["staged"] = staged
        assert staged != src and staged.parent.parent != downloads
        assert sha256(staged) == digest == manifest["sha256"]
        assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
        assert manifest["original_filename"] == T3_FILE
        return fake_system_install(sys_layout)(op, manifest)

    monkeypatch.setattr(privileged, "run_helper", HelperRecorder(result=helper))
    app = execute_install(plan)
    assert app.scope is Scope.SYSTEM
    assert not seen["staged"].parent.exists()  # the copy is removed again
    assert not src.exists()  # "move": the original is deleted after success

    # A cancelled password prompt removes the copy too and keeps the original.
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status)
    recorder = HelperRecorder(exc=AuthorizationError("Authentication was cancelled."))
    monkeypatch.setattr(privileged, "run_helper", recorder)
    with pytest.raises(AuthorizationError):
        execute_install(plan)
    staged = Path(recorder.calls[0][1]["source_appimage"])
    assert not staged.parent.exists() and src.is_file()


def test_stale_staging_copies_are_removed(downloads, infos, status, sys_layout, monkeypatch,
                                          isolated_env):
    base = isolated_env / ".cache" / "easy-installer"
    stale = base / "staging-old"
    stale.mkdir(parents=True)
    (stale / "source.AppImage").write_bytes(b"x" * 10)
    os.utime(stale, (1_000_000, 1_000_000))
    fresh = base / "staging-running"  # another installation that is still running
    fresh.mkdir()
    src = make_sample_appimage(downloads / T3_FILE, "t3code")
    monkeypatch.setattr(installer, "on_private_fuse_mount", lambda p: Path(p).parent == downloads)
    plan = plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM, keep_original=True),
                        status=status)
    monkeypatch.setattr(privileged, "run_helper",
                        HelperRecorder(result=fake_system_install(sys_layout)))
    execute_install(plan)
    assert not stale.exists() and fresh.is_dir() and src.is_file()


def test_system_uninstall_calls_helper(sys_layout, monkeypatch):
    helper = HelperRecorder()
    monkeypatch.setattr(privileged, "run_helper", helper)
    with pytest.raises(NotInstalledError):
        uninstall("t3code", Scope.SYSTEM)
    assert helper.calls == []
    Registry(sys_layout.registry_path).put(InstalledApp(
        id="t3code", name="T3", version="1", scope=Scope.SYSTEM,
        appimage_path=str(sys_layout.apps_dir / "T3.AppImage"),
        desktop_path=str(sys_layout.desktop_dir / "easyinstaller-t3code.desktop"),
    ))
    uninstall("t3code", Scope.SYSTEM)
    assert helper.calls == [("uninstall", {"app_id": "t3code"})]


# ------------------------------------------------------------------------------------------------
# real AppImages (copied to tmp, never touched in place)
# ------------------------------------------------------------------------------------------------


@pytest.mark.real
def test_real_appimage_install_and_uninstall(small_real_appimage, downloads, infos, status):
    copy = downloads / small_real_appimage.name
    shutil.copyfile(small_real_appimage, copy)
    info = infos(copy)
    plan = plan_install(info, status=status)
    app = execute_install(plan)
    assert not copy.exists()
    assert small_real_appimage.is_file()  # the original sample is untouched
    target = Path(app.appimage_path)
    assert target.parent == user_layout().apps_dir
    assert os.access(target, os.X_OK)
    validator = shutil.which("desktop-file-validate")
    if validator:
        proc = subprocess.run([validator, app.desktop_path], capture_output=True, text=True)
        errors = [line for line in proc.stdout.splitlines() if "error:" in line]
        assert errors == [], proc.stdout
    if info.icon_path is not None:
        assert app.icon_path is not None
    uninstall(app.id, Scope.USER)
    assert not target.exists()
    assert list_installed([Scope.USER]) == []


# ------------------------------------------------------------------------------------------------
# planning: versions, names, files that belong to another installation
# ------------------------------------------------------------------------------------------------


def foo_app(path: Path, *, version: str | None = None, tag: str = "", stem: str = "foo",
            name: str = "Foo") -> Path:
    desktop = f"[Desktop Entry]\nType=Application\nName={name}\nExec=foo %U\nIcon=foo\n"
    if version:
        desktop += f"X-AppImage-Version={version}\n"
    return make_fake_appimage(path, {f"{stem}.desktop": desktop,
                                     "usr/bin/foo": b"\x7fELF foo " + tag.encode(),
                                     "foo.png": make_png(32, 32)}, {"AppRun": "usr/bin/foo"})


def test_reinstalling_the_installed_file_keeps_its_version(downloads, infos, status):
    """FreeCAD has no X-AppImage-Version: the version comes from the download's name only."""
    src = make_sample_appimage(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")
    first = execute_install(plan_install(infos(src), status=status))
    installed = Path(first.appimage_path)
    assert first.version == "1.1.3" and installed.name == "FreeCAD.AppImage"

    info = infos(installed)
    assert info.version is None
    plan = plan_install(info, InstallOptions(keep_original=True), status=status)  # like "Repair"
    assert plan.in_place and plan.action == "reinstall" and plan.version == "1.1.3"
    assert not any("older" in w or "None" in w for w in plan.warnings)
    Path(first.desktop_path).unlink()
    app = execute_install(plan)
    assert app.version == "1.1.3"
    assert read_desktop(Path(app.desktop_path)).get("X-AppImage-Version") == "1.1.3"

    # The same bytes under another name (downloaded again, renamed) are the same version, too.
    copy = downloads / "freecad.AppImage"
    shutil.copyfile(installed, copy)
    assert plan_install(infos(copy), status=status).version == "1.1.3"
    older = make_sample_appimage(downloads / "FreeCAD_1.0.2-Linux-x86_64-py311.AppImage", "freecad")
    assert plan_install(infos(older), status=status).action == "downgrade"


def test_a_file_without_any_version_is_not_called_older(downloads, infos, status):
    execute_install(plan_install(infos(foo_app(downloads / "Foo-1.2.AppImage")), status=status))
    plan = plan_install(infos(foo_app(downloads / "Foo-x86_64.AppImage", tag="new")), status=status)
    assert plan.version is None and plan.existing.version == "1.2"
    assert plan.action == "update"  # a neutral "replace": nothing is known about which is newer
    assert not any("older" in w or "None" in w for w in plan.warnings)


def test_repair_of_a_suffixed_app_file_stays_in_place(downloads, infos, status):
    """B was installed as Foo-com.b.Foo.AppImage because A had Foo.AppImage; A is gone now."""
    a = execute_install(plan_install(infos(foo_app(downloads / "A.AppImage", stem="com.a.Foo")),
                                     status=status))
    b = execute_install(plan_install(infos(foo_app(downloads / "B.AppImage", stem="com.b.Foo",
                                                   tag="b")), status=status))
    assert Path(a.appimage_path).name == "Foo.AppImage"
    assert Path(b.appimage_path).name == "Foo-com.b.Foo.AppImage"
    uninstall("com.a.Foo", Scope.USER)
    Path(b.desktop_path).unlink()
    assert Registry(user_layout().registry_path).get("com.b.Foo").status() == "missing-launcher"

    plan = plan_install(infos(Path(b.appimage_path)), InstallOptions(keep_original=True),
                        status=status)
    assert plan.in_place and plan.target_appimage == Path(b.appimage_path)
    repaired = execute_install(plan)
    assert repaired.status() == "ok"
    assert sorted(p.name for p in user_layout().apps_dir.iterdir()) == ["Foo-com.b.Foo.AppImage"]
    uninstall("com.b.Foo", Scope.USER)
    assert list(user_layout().apps_dir.iterdir()) == []


def test_update_with_keep_original_does_not_leave_the_old_app_file(downloads, infos, status):
    first = execute_install(plan_install(infos(foo_app(downloads / "Foo-1.0.AppImage")),
                                         status=status))
    old = Path(first.appimage_path)
    # An update from the installed file itself to a new name (e.g. the name became free).
    info = infos(old)
    plan = installer.plan_install(info, InstallOptions(keep_original=True), status=status)
    plan.target_appimage = old.with_name("Foo-new.AppImage")
    plan.in_place = False
    new = execute_install(plan)
    assert Path(new.appimage_path).is_file() and not old.exists()


def test_same_name_different_id_is_pointed_out(downloads, infos, status):
    old = foo_app(downloads / "FreeCAD-0.21.2.AppImage", stem="org.freecadweb.FreeCAD",
                  name="FreeCAD")
    execute_install(plan_install(infos(old), status=status))
    new = foo_app(downloads / "FreeCAD-1.1.3.AppImage", stem="org.freecad.FreeCAD",
                  name="FreeCAD", tag="new")
    plan = plan_install(infos(new), status=status)
    assert plan.action == "install" and plan.existing is None
    assert any("Another app called FreeCAD is already installed" in w for w in plan.warnings)


def test_unrelated_apps_without_ascii_names_do_not_replace_each_other(downloads, infos, status):
    calc = foo_app(downloads / "Калькулятор-1.0-x86_64.AppImage", stem="калькулятор",
                   name="Калькулятор")
    editor = foo_app(downloads / "Редактор-2.0-x86_64.AppImage", stem="редактор", name="Редактор",
                     tag="editor")
    first = execute_install(plan_install(infos(calc), status=status))
    plan = plan_install(infos(editor), status=status)
    assert plan.app_id != first.id and plan.action == "install" and plan.existing is None
    second = execute_install(plan)
    assert Path(first.appimage_path).is_file() and Path(second.appimage_path).is_file()
    assert len(Registry(user_layout().registry_path).load()) == 2


def test_a_different_build_without_any_version_is_an_update(downloads, infos, status):
    """E.g. "Foo-latest-x86_64.AppImage" downloaded again a week later: not "already installed"."""
    first = execute_install(plan_install(infos(foo_app(downloads / "Foo-latest-x86_64.AppImage")),
                                         status=status))
    assert first.version is None
    plan = plan_install(infos(foo_app(downloads / "Foo-latest-x86_64.AppImage", tag="newer")),
                        status=status)
    assert plan.version is None and plan.action == "update"
    execute_install(plan)
    # the very same file again (e.g. Repair) is a reinstall
    installed = find_installed("foo")[0]
    assert plan_install(infos(Path(installed.appimage_path)), status=status).action == "reinstall"
    copy = downloads / "Foo-copy.AppImage"
    shutil.copyfile(installed.appimage_path, copy)
    assert plan_install(infos(copy), status=status).action == "reinstall"  # same sha256


def test_the_file_of_another_installed_app_is_never_adopted_in_place(downloads, infos, status):
    """An entry registered under another id (e.g. an id derived differently by an older version)
    owns the file: installing that file must copy it, or uninstalling one breaks the other."""
    first = execute_install(plan_install(infos(foo_app(downloads / "calc-1.0-x86_64.AppImage")),
                                         status=status))
    registry = Registry(user_layout().registry_path)
    registry.remove("foo")
    registry.put(dataclasses.replace(first, id="calc"))
    installed = Path(first.appimage_path)
    plan = plan_install(infos(installed), status=status)
    assert plan.app_id == "foo" and plan.existing is None
    assert not plan.in_place and plan.source_is_installed and plan.options.keep_original
    assert plan.target_appimage != installed
    assert any("stays where it is" in w for w in plan.warnings)
    second = execute_install(plan)
    assert installed.is_file() and Path(second.appimage_path).is_file()
    uninstall("foo", Scope.USER)
    assert installed.is_file() and Registry(user_layout().registry_path).get("calc") is not None


def test_an_app_file_two_entries_share_is_kept_until_the_last_one_goes(downloads, infos, status):
    """Such pairs could be created before the check above: uninstalling one must not break the
    other (nor must updating it)."""
    first = execute_install(plan_install(infos(foo_app(downloads / "Foo-1.0.AppImage")),
                                         status=status))
    registry = Registry(user_layout().registry_path)
    registry.put(dataclasses.replace(first, id="calc", desktop_path=str(
        Path(first.desktop_path).with_name("easyinstaller-calc.desktop")), icon_paths=[]))
    shared = Path(first.appimage_path)
    execute_install(plan_install(infos(foo_app(downloads / "Foo-2.0.AppImage", tag="2",
                                               name="Foo Two")), status=status))
    assert shared.is_file()  # the update of "foo" moved to Foo-Two.AppImage; calc keeps its file
    calc = registry.get("calc")
    registry.put(dataclasses.replace(calc, id="calc2", desktop_path=str(
        Path(calc.desktop_path).with_name("easyinstaller-calc2.desktop"))))
    uninstall("calc", Scope.USER)
    assert shared.is_file()  # calc2 still uses it
    uninstall("calc2", Scope.USER)
    assert not shared.exists()  # the last entry using it is gone


def test_installing_for_everyone_from_the_installed_user_copy_keeps_it(
        downloads, infos, status, sys_layout, monkeypatch):
    src = make_sample_appimage(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")
    user_app = execute_install(plan_install(infos(src), status=status))
    installed = Path(user_app.appimage_path)
    plan = plan_install(infos(installed), InstallOptions(scope=Scope.SYSTEM), status=status)
    assert plan.options.keep_original is True and plan.source_is_installed
    assert plan.version == "1.1.3"
    assert any("stays where it is" in w for w in plan.warnings)
    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    execute_install(plan)
    assert helper.calls[0][1]["version"] == "1.1.3"
    assert installed.is_file()
    assert Registry(user_layout().registry_path).get(user_app.id).status() == "ok"


def test_apparmor_profile_is_compared_with_the_resolved_path(downloads, infos, restricted,
                                                             isolated_env, monkeypatch, tmp_path):
    """A symlinked home or Applications folder must not ask for the password on every update."""
    from easy_installer.helper import ops

    real_home = tmp_path / "data" / "home-real"  # e.g. /home/u -> /data/home/u
    real_apps = real_home / "Applications"
    real_apps.mkdir(parents=True)
    home_link = tmp_path / "home-link"
    home_link.symlink_to(real_home)
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(home_link / "Applications"))
    profiles = tmp_path / "sysroot" / "etc" / "apparmor.d"
    monkeypatch.setattr(installer, "layout_for", lambda scope: dataclasses.replace(
        user_layout(), apparmor_dir=profiles))
    app = execute_install(plan_install(infos(electron_app(downloads / "Anytype.AppImage")),
                                       InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX),
                                       status=restricted))
    # What the helper writes for this app file:
    ops.op_apparmor_install({"app_id": "anytype", "appimage_path": app.appimage_path},
                            layout=system_layout(tmp_path / "sysroot"), caller_uid=os.getuid(),
                            caller_home=home_link, run_commands=False)
    written = (profiles / "easyinstaller-anytype").read_text(encoding="utf-8")
    assert str(real_apps) in written and app.appimage_path.startswith(str(home_link))
    plan = plan_install(infos(Path(app.appimage_path)), status=restricted)
    assert plan.sandbox_fix is SandboxFix.APPARMOR
    assert plan.apparmor_profile_text == written
    assert plan.requires_root is False


# ------------------------------------------------------------------------------------------------
# execution: plans that got stale, leftovers of interrupted installations, odd file names
# ------------------------------------------------------------------------------------------------


def test_stale_plan_never_replaces_another_apps_file(downloads, infos, status):
    """`install B` waits at "Proceed?" while app A with the same name is installed elsewhere."""
    plan_b = plan_install(infos(foo_app(downloads / "B.AppImage", stem="com.b.Foo", tag="b")),
                          status=status)
    assert plan_b.target_appimage.name == "Foo.AppImage"
    a = execute_install(plan_install(infos(foo_app(downloads / "A.AppImage", stem="com.a.Foo")),
                                     status=status))
    assert Path(a.appimage_path) == plan_b.target_appimage
    a_bytes = Path(a.appimage_path).read_bytes()
    with pytest.raises(InstallError, match="Please try again"):
        execute_install(plan_b)
    assert Path(a.appimage_path).read_bytes() == a_bytes
    assert (downloads / "B.AppImage").is_file()  # nothing was moved
    assert list(Registry(user_layout().registry_path).load()) == ["com.a.Foo"]
    # planning again gives B its own file
    app_b = execute_install(plan_install(infos(downloads / "B.AppImage"), status=status))
    assert app_b.appimage_path != a.appimage_path


def test_stale_plan_of_the_same_app_is_refused(downloads, infos, status):
    old_plan = plan_install(infos(foo_app(downloads / "Foo-1.0.AppImage")), status=status)
    execute_install(plan_install(infos(foo_app(downloads / "Foo-2.0.AppImage", tag="2")),
                                 status=status))
    with pytest.raises(InstallError, match="Please try again"):
        execute_install(old_plan)  # would silently "downgrade" without asking
    assert Registry(user_layout().registry_path).get("foo").version == "2.0"


def test_leftovers_of_an_interrupted_update_are_cleaned_up(downloads, infos, status):
    first = execute_install(plan_install(infos(foo_app(downloads / "Foo-1.0.AppImage")),
                                         status=status))
    bar = execute_install(plan_install(infos(foo_app(downloads / "Bar-1.0.AppImage", stem="bar",
                                                     name="Bar", tag="bar")), status=status))
    apps = user_layout().apps_dir
    # killed after moving the old version aside (backup) and while copying the new one
    stale_backup = apps / ".Foo.AppImage.easyinstaller-old-e8a3a264"
    shutil.copyfile(first.appimage_path, stale_backup)
    (apps / ".Foo.AppImage.part").write_bytes(b"half")
    # killed between moving Bar's file aside and putting the new one there: the only copy
    bar_file = Path(bar.appimage_path)
    bar_bytes = bar_file.read_bytes()
    bar_file.rename(apps / ".Bar.AppImage.easyinstaller-old-0badc0de")
    # a backup of a file no installed app uses any more (e.g. uninstalled since)
    (apps / ".Baz.AppImage.easyinstaller-old-12345678").write_bytes(b"baz")
    unrelated = apps / ".hidden-notes.txt"
    unrelated.write_text("mine")

    execute_install(plan_install(infos(foo_app(downloads / "Foo-2.0.AppImage", tag="2")),
                                 status=status))
    names = sorted(p.name for p in apps.iterdir())
    assert names == [".easyinstaller-backups", ".hidden-notes.txt", "Bar.AppImage", "Foo.AppImage"]
    assert bar_file.read_bytes() == bar_bytes  # the registered app's only copy is put back


def test_backups_of_an_uninstalled_app_never_come_back(downloads, infos, status, monkeypatch):
    """An update killed before it finished leaves backups; after uninstalling the app they must
    not be "restored" as a launcher and app file nobody knows about."""
    execute_install(plan_install(infos(foo_app(downloads / "Foo-1.0.AppImage", version="1.0")),
                                 status=status))
    plan = plan_install(infos(foo_app(downloads / "Foo-2.0.AppImage", version="2.0", tag="2")),
                        status=status)

    def killed(self, app):  # e.g. logout or power loss while saving: no rollback happens
        raise SystemExit("killed")

    with monkeypatch.context() as m:
        m.setattr(Registry, "put", killed)
        m.setattr(installer._Transaction, "rollback", lambda self: None)
        with pytest.raises(SystemExit):
            execute_install(plan)
    layout = user_layout()
    assert any(".easyinstaller-old-" in p.name for p in layout.desktop_dir.iterdir())

    uninstall("foo", Scope.USER)
    leftovers = [p for d in (layout.apps_dir, layout.desktop_dir) for p in d.iterdir()]
    assert leftovers == [], leftovers  # its backups went with it

    # and an install of another app puts nothing back
    execute_install(plan_install(infos(foo_app(downloads / "Bar.AppImage", stem="bar",
                                               name="Bar", tag="bar")), status=status))
    assert sorted(p.name for p in layout.apps_dir.iterdir()) == ["Bar.AppImage"]
    assert sorted(p.name for p in layout.desktop_dir.iterdir()) == ["easyinstaller-bar.desktop"]
    assert [a.id for a in list_installed([Scope.USER])] == ["bar"]


def test_file_name_that_is_not_utf8(downloads, infos, status, sys_layout, monkeypatch):
    """e.g. unpacked from a Windows zip archive: b"Caf\\xe9-1.2.AppImage"."""
    name = os.fsdecode(b"Caf\xe9-1.2.AppImage")
    src = foo_app(downloads / name)
    app = execute_install(plan_install(infos(src), status=status))
    assert app.original_filename == "Caf\ufffd-1.2.AppImage"
    assert Registry(user_layout().registry_path).get("foo") == app

    src = foo_app(downloads / name, tag="system")
    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    execute_install(plan_install(infos(src), InstallOptions(scope=Scope.SYSTEM), status=status))
    manifest = helper.calls[0][1]
    manifest["source_appimage"].encode("utf-8")  # a local copy with a plain name
    assert manifest["original_filename"] == "Caf\ufffd-1.2.AppImage"
    assert not src.exists()


def test_start_mode_is_updated_once_fuse_works(downloads, infos):
    """Stock Ubuntu 24.04 lacks libfuse2: apps unpack themselves until FUSE is installed."""
    no_fuse = make_status(libfuse2=False)
    src = make_sample_appimage(downloads / T3_FILE, "t3code")  # dynamic runtime: needs libfuse2
    plan = plan_install(infos(src), status=no_fuse)
    assert plan.extract_and_run
    app = execute_install(plan)
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in read_desktop(Path(app.desktop_path)).get("Exec")

    assert installer.update_start_modes(no_fuse) == []  # still missing: nothing changes
    updated = installer.update_start_modes(make_status(libfuse2=True))
    assert [a.id for a in updated] == ["t3code"] and not updated[0].extract_and_run
    entry = read_desktop(Path(app.desktop_path))
    assert entry.get("Exec") == f"{app.appimage_path} --no-sandbox %U"
    assert Registry(user_layout().registry_path).get("t3code").extract_and_run is False
    assert installer.update_start_modes(make_status(libfuse2=True)) == []


def test_app_file_types_are_registered_and_removed(downloads, infos, status, monkeypatch, tmp_path):
    """FreeCAD opens *.FCStd, a type only its own MIME package defines."""
    from easy_installer.core import integration

    monkeypatch.setattr(integration, "HOST_MIME_DIR", tmp_path / "empty-host-mime")
    runs = []
    monkeypatch.setattr(installer, "_mime_tool", lambda: "/usr/bin/update-mime-database")
    monkeypatch.setattr(installer, "_run_tool", lambda cmd: runs.append(cmd))
    src = make_sample_appimage(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")
    info = infos(src)
    assert [d.type for d in info.mime_types] == ["application/x-extension-fcstd"]
    plan = plan_install(info, status=status)
    assert [d.type for d in plan.mime_types] == ["application/x-extension-fcstd"]
    app = execute_install(plan)
    layout = user_layout()
    package = layout.mime_dir / "packages" / "easyinstaller-org.freecad.FreeCAD.xml"
    assert app.mime_package == str(package) and package.is_file()
    assert '<glob pattern="*.fcstd"/>' in package.read_text()
    assert ["/usr/bin/update-mime-database", str(layout.mime_dir)] in runs

    runs.clear()
    uninstall(app.id, Scope.USER)
    assert not package.exists()
    assert ["/usr/bin/update-mime-database", str(layout.mime_dir)] in runs


def test_file_types_outside_real_media_types_never_reach_the_mime_database(
        downloads, infos, status, monkeypatch, tmp_path):
    """update-mime-database writes <mime_dir>/<media>/<subtype>.xml: "packages/<x>" would replace
    another package file, "mime.cache/x" would break the whole database for good."""
    from easy_installer.core import integration

    monkeypatch.setattr(integration, "HOST_MIME_DIR", tmp_path / "empty-host-mime")
    tool = shutil.which("update-mime-database")
    if tool:
        monkeypatch.setattr(installer, "_mime_tool", lambda: tool)
    layout = user_layout()
    victim = layout.mime_dir / "packages" / "x-wine-extension-foo.xml"
    victim.parent.mkdir(parents=True)
    victim_text = ('<?xml version="1.0"?>\n<mime-info xmlns="http://www.freedesktop.org/standards/'
                   'shared-mime-info"><mime-type type="application/x-wine-extension-foo">'
                   '<glob pattern="*.foo"/></mime-type></mime-info>\n')
    victim.write_text(victim_text)
    ns = 'xmlns="http://www.freedesktop.org/standards/shared-mime-info"'
    hostile_xml = (f'<mime-info {ns}>'
                   '<mime-type type="packages/x-wine-extension-foo"><glob pattern="*.zzqq"/></mime-type>'
                   '<mime-type type="mime.cache/x"><glob pattern="*.zzqr"/></mime-type>'
                   '</mime-info>')
    desktop = T3_DESKTOP.replace("MimeType=x-scheme-handler/t3code;",
                                 "MimeType=packages/x-wine-extension-foo;mime.cache/x;"
                                 "x-scheme-handler/t3code;")
    assert desktop != T3_DESKTOP
    src = make_fake_appimage(downloads / "Hostile-1.0.AppImage", {
        "t3code.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "usr/share/mime/packages/hostile.xml": hostile_xml})
    info = infos(src)
    assert info.mime_types == []
    plan = plan_install(info, status=status)
    assert plan.mime_types == [] and plan.mime_package is None
    execute_install(plan)
    assert victim.read_text() == victim_text
    assert not (layout.mime_dir / "mime.cache").is_dir()
    assert sorted(os.listdir(layout.mime_dir / "packages")) == ["x-wine-extension-foo.xml"]


def test_known_file_types_are_not_registered_again(downloads, infos, status, monkeypatch, tmp_path):
    from easy_installer.core import integration

    host = tmp_path / "host-mime"
    host.mkdir()
    (host / "types").write_text("application/x-openscad\n")
    monkeypatch.setattr(integration, "HOST_MIME_DIR", host)
    src = make_sample_appimage(downloads / "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad")
    plan = plan_install(infos(src), status=status)
    assert plan.mime_types == [] and plan.mime_package is None
    app = execute_install(plan)
    assert app.mime_package is None and not user_layout().mime_dir.exists()


# ------------------------------------------------------------------------------------------------
# 0.2: what the inspected file says about updates, origin, signature and data folders
# ------------------------------------------------------------------------------------------------


class FakeSource:
    """Stands in for core.updates.UpdateSource / core.signature.SignatureInfo (value objects)."""

    def __init__(self, **values):
        self.values = values

    def to_dict(self) -> dict:
        return dict(self.values)


SOURCE = {"kind": "github-assets", "owner": "foo", "repo": "foo", "release": "latest",
          "pattern": "Foo-*.AppImage", "via": "upd_info"}
SIGNED = {"status": "unverified", "fingerprint": "ABCD" * 10, "signer": None, "details": None}


def test_optional_details_of_the_file_are_recorded(downloads, infos, status):
    """The installer stores whatever update source, origin, signature and data hints the
    inspected file carries (optional attributes; value objects via to_dict())."""
    info = infos(foo_app(downloads / "Foo-1.0.AppImage", version="1.0"))
    plain = execute_install(plan_install(info, status=status))
    assert (plain.update_source, plain.origin_url, plain.signature) == (None, None, None)
    assert plain.data_hints == ["Foo", "foo-updater"]    # what the inspector works out itself
    assert plain.kind == "appimage" and plain.install_dir is None
    assert (plain.base_id, plain.pinned, plain.previous) == (None, False, None)

    info = infos(foo_app(downloads / "Foo-1.1.AppImage", version="1.1"))
    info.update_source = FakeSource(**SOURCE)
    info.origin_url = "https://example.org/dl/Foo-1.1.AppImage"
    info.signature = FakeSource(**SIGNED)
    info.data_hints = ["foo", "Foo", 5, ""]
    app = execute_install(plan_install(info, status=status))
    assert app.update_source == SOURCE and app.signature == SIGNED
    assert app.origin_url == "https://example.org/dl/Foo-1.1.AppImage"
    assert app.data_hints == ["foo", "Foo"]
    assert Registry(user_layout().registry_path).get("foo") == app

    # The installed file again (repair): what the file itself tells is recorded anew (it is
    # not signed; its data folder names). What it does not tell is kept: where its updates
    # come from and - it is the same file - where it was downloaded from.
    repaired = execute_install(plan_install(infos(Path(app.appimage_path)),
                                            InstallOptions(keep_original=True), status=status))
    assert (repaired.update_source, repaired.origin_url) == (SOURCE, app.origin_url)
    assert repaired.signature is None and repaired.data_hints == ["Foo", "foo-updater"]

    # An info that knows nothing about signatures (no such attribute, like an archive's):
    # the very same file keeps what is recorded.
    class Plain:
        """Stands in for an inspected file without the optional attributes."""

        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            if name in ("update_source", "origin_url", "signature", "data_hints"):
                raise AttributeError(name)
            return getattr(self._info, name)

    Registry(user_layout().registry_path).put(dataclasses.replace(repaired, signature=SIGNED))
    kept = execute_install(plan_install(Plain(infos(Path(app.appimage_path))),
                                        InstallOptions(keep_original=True), status=status))
    assert (kept.update_source, kept.origin_url, kept.signature, kept.data_hints) == \
        (SOURCE, app.origin_url, SIGNED, ["Foo", "foo-updater"])

    # A new file that says nothing: where updates come from belongs to the app; origin and
    # signature belonged to the old file.
    newer = execute_install(plan_install(infos(foo_app(downloads / "Foo-1.2.AppImage",
                                                       version="1.2")), status=status))
    assert newer.update_source == SOURCE and newer.data_hints == ["Foo", "foo-updater"]
    assert newer.origin_url is None and newer.signature is None

    # "Not signed" (the attribute is there and None) replaces a recorded signature, plain
    # dicts are taken as they are, other types are ignored.
    info = infos(Path(newer.appimage_path))
    info.signature = None
    info.update_source = {"kind": "zsync-url", "url": "https://example.org/Foo.zsync"}
    info.origin_url = 12345
    info.data_hints = "foo"
    again = execute_install(plan_install(info, InstallOptions(keep_original=True), status=status))
    assert again.signature is None and again.origin_url is None
    assert again.update_source == {"kind": "zsync-url", "url": "https://example.org/Foo.zsync"}
    assert again.data_hints == ["Foo", "foo-updater"]


def test_optional_details_travel_cleaned_in_the_system_manifest(downloads, infos, status,
                                                                sys_layout, monkeypatch):
    from easy_installer.helper import ops

    info = infos(foo_app(downloads / "Foo-1.0.AppImage", version="1.0"))
    info.update_source = {**SOURCE, "nested": {"x": 1}, "note": "a\x00b"}
    info.origin_url = "https://example.org/dl/Foo.AppImage"
    info.signature = {**SIGNED, "details": "gpg: line 1\r\ngpg: line 2"}
    info.data_hints = ["foo", "a/b", "..", "Foo"]
    plan = plan_install(info, InstallOptions(scope=Scope.SYSTEM), status=status)
    helper = HelperRecorder(result=fake_system_install(sys_layout))
    monkeypatch.setattr(privileged, "run_helper", helper)
    execute_install(plan)
    manifest = helper.calls[0][1]
    assert manifest["update_source"] == {**SOURCE, "note": "ab"}
    assert manifest["origin_url"] == "https://example.org/dl/Foo.AppImage"
    assert manifest["signature"] == {**SIGNED, "details": "gpg: line 1\ngpg: line 2"}
    assert manifest["data_hints"] == ["foo", "Foo"]
    # ... which is exactly what the helper accepts
    request = ops.InstallRequest.from_manifest({**manifest, "source_appimage": "/x.AppImage"})
    assert request.update_source == manifest["update_source"]
    assert request.signature == manifest["signature"]
    assert request.data_hints == ("foo", "Foo")

    info = infos(foo_app(downloads / "Foo-1.1.AppImage", version="1.1"))
    info.origin_url = "file:///etc/passwd"
    execute_install(plan_install(info, InstallOptions(scope=Scope.SYSTEM), status=status))
    assert helper.calls[1][1]["origin_url"] is None
