from __future__ import annotations

from pathlib import Path

from easy_installer.core.paths import (
    Layout,
    Scope,
    config_dir,
    layout_for,
    settings_path,
    system_layout,
    user_layout,
)


def test_scope_values():
    assert Scope.USER.value == "user"
    assert Scope.SYSTEM.value == "system"
    assert Scope("user") is Scope.USER
    assert str(Scope.SYSTEM) == "system"
    assert Scope.USER == "user"  # str enum


def test_user_layout_from_isolated_env(isolated_env):
    home = isolated_env
    layout = user_layout()
    assert layout.scope is Scope.USER
    assert layout.apps_dir == home / "Applications"
    assert layout.desktop_dir == home / ".local/share/applications"
    assert layout.icons_dir == home / ".local/share/icons/hicolor"
    assert layout.registry_path == home / ".local/share/easy-installer/registry.json"
    assert layout.apparmor_dir == Path("/etc/apparmor.d")


def test_user_layout_explicit_env_defaults():
    env = {"HOME": "/home/alice"}
    layout = user_layout(env)
    assert layout.apps_dir == Path("/home/alice/Applications")
    assert layout.desktop_dir == Path("/home/alice/.local/share/applications")
    assert layout.icons_dir == Path("/home/alice/.local/share/icons/hicolor")
    assert layout.registry_path == Path("/home/alice/.local/share/easy-installer/registry.json")


def test_user_layout_overrides():
    env = {
        "HOME": "/home/bob",
        "XDG_DATA_HOME": "/data/bob",
        "EASY_INSTALLER_APPS_DIR": "/apps/bob",
    }
    layout = user_layout(env)
    assert layout.apps_dir == Path("/apps/bob")
    assert layout.desktop_dir == Path("/data/bob/applications")
    assert layout.registry_path == Path("/data/bob/easy-installer/registry.json")


def test_relative_xdg_values_are_ignored():
    env = {"HOME": "/home/carol", "XDG_DATA_HOME": "relative/share",
           "EASY_INSTALLER_APPS_DIR": "apps", "XDG_CONFIG_HOME": "cfg"}
    layout = user_layout(env)
    assert layout.desktop_dir == Path("/home/carol/.local/share/applications")
    assert layout.apps_dir == Path("/home/carol/Applications")
    assert config_dir(env) == Path("/home/carol/.config/easy-installer")


def test_system_layout_with_root(system_root):
    layout = system_layout(root=system_root)
    assert layout.scope is Scope.SYSTEM
    assert layout.apps_dir == system_root / "opt/appimages"
    assert layout.desktop_dir == system_root / "usr/local/share/applications"
    assert layout.icons_dir == system_root / "usr/local/share/icons/hicolor"
    assert layout.registry_path == system_root / "var/lib/easy-installer/registry.json"
    assert layout.apparmor_dir == system_root / "etc/apparmor.d"


def test_system_layout_default_root(monkeypatch):
    monkeypatch.delenv("EASY_INSTALLER_SYSTEM_ROOT")
    layout = system_layout()
    assert layout.apps_dir == Path("/opt/appimages")
    assert layout.registry_path == Path("/var/lib/easy-installer/registry.json")


def test_system_root_override_is_for_unprivileged_processes_only(monkeypatch, tmp_path):
    monkeypatch.setenv("EASY_INSTALLER_SYSTEM_ROOT", str(tmp_path))
    assert system_layout().apps_dir == tmp_path / "opt" / "appimages"
    assert system_layout(Path("/")).apps_dir == Path("/opt/appimages")
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert system_layout().apps_dir == Path("/opt/appimages")
    monkeypatch.setenv("EASY_INSTALLER_SYSTEM_ROOT", "relative/path")
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert system_layout().apps_dir == Path("/opt/appimages")


def test_layout_for(isolated_env):
    assert layout_for(Scope.USER) == user_layout()
    assert layout_for(Scope.SYSTEM) == system_layout()
    assert layout_for("user").scope is Scope.USER


def test_layout_is_frozen():
    layout = user_layout({"HOME": "/h"})
    assert isinstance(layout, Layout)
    try:
        layout.apps_dir = Path("/x")  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Layout must be immutable")


def test_config_and_settings_paths(isolated_env):
    home = isolated_env
    assert config_dir() == home / ".config/easy-installer"
    assert settings_path() == home / ".config/easy-installer/settings.json"
    env = {"HOME": "/h", "XDG_CONFIG_HOME": "/cfg"}
    assert settings_path(env) == Path("/cfg/easy-installer/settings.json")
