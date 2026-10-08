"""Install locations for the two scopes (per-user and system-wide)."""

from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


#: Folder inside ``apps_dir`` where replaced versions are kept for a while (one sub-folder per
#: app id). Hidden, so file managers and the "adopt a renamed app file" scan never show it.
BACKUPS_DIR_NAME = ".easyinstaller-backups"


class Scope(str, Enum):
    USER = "user"
    SYSTEM = "system"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Layout:
    scope: Scope
    apps_dir: Path
    desktop_dir: Path
    icons_dir: Path
    registry_path: Path
    apparmor_dir: Path

    @property
    def registry_dir(self) -> Path:
        return self.registry_path.parent

    @property
    def mime_dir(self) -> Path:
        """``<share>/mime`` next to the launchers: USER ``$XDG_DATA_HOME/mime``, SYSTEM
        ``<root>/usr/local/share/mime`` (packages go to ``packages/``)."""
        return self.desktop_dir.parent / "mime"

    @property
    def backups_dir(self) -> Path:
        """``<apps_dir>/.easyinstaller-backups``: on the same file system as the apps, so a
        replaced version is moved there without copying it."""
        return self.apps_dir / BACKUPS_DIR_NAME


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    """$HOME from ``env``; falls back to the password database."""
    home = _env(env).get("HOME")
    if home:
        return Path(home)
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return Path(os.path.expanduser("~"))


def _xdg_dir(env: Mapping[str, str] | None, name: str, default: str) -> Path:
    # Per the XDG base directory spec, relative paths must be ignored.
    value = _env(env).get(name)
    if value and os.path.isabs(value):
        return Path(value)
    return home_dir(env) / default


def data_home(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_DATA_HOME", ".local/share")


def config_home(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_CONFIG_HOME", ".config")


def cache_home(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_CACHE_HOME", ".cache")


def state_home(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_STATE_HOME", ".local/state")


def user_layout(env: Mapping[str, str] | None = None) -> Layout:
    e = _env(env)
    data = data_home(e)
    apps_override = e.get("EASY_INSTALLER_APPS_DIR")
    if apps_override and os.path.isabs(apps_override):
        apps_dir = Path(apps_override)
    else:
        apps_dir = home_dir(e) / "Applications"
    return Layout(
        scope=Scope.USER,
        apps_dir=apps_dir,
        desktop_dir=data / "applications",
        icons_dir=data / "icons" / "hicolor",
        registry_path=data / "easy-installer" / "registry.json",
        apparmor_dir=Path("/etc/apparmor.d"),
    )


def default_system_root() -> Path:
    """"/", or $EASY_INSTALLER_SYSTEM_ROOT for unprivileged processes (tests use it to keep the
    real system-wide registry out of view). The root helper always works on the real "/"."""
    override = os.environ.get("EASY_INSTALLER_SYSTEM_ROOT", "")
    if override and os.path.isabs(override) and os.geteuid() != 0:
        return Path(override)
    return Path("/")


def system_layout(root: Path | None = None) -> Layout:
    root = default_system_root() if root is None else Path(root)
    return Layout(
        scope=Scope.SYSTEM,
        apps_dir=root / "opt" / "appimages",
        desktop_dir=root / "usr" / "local" / "share" / "applications",
        icons_dir=root / "usr" / "local" / "share" / "icons" / "hicolor",
        registry_path=root / "var" / "lib" / "easy-installer" / "registry.json",
        apparmor_dir=root / "etc" / "apparmor.d",
    )


def layout_for(scope: Scope) -> Layout:
    return system_layout() if Scope(scope) is Scope.SYSTEM else user_layout()


def backups_dir(layout: Layout) -> Path:
    """Where ``layout`` keeps the versions that an update replaced."""
    return layout.backups_dir


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CACHE_HOME/easy-installer`` (update cache, downloads, staging copies)."""
    return cache_home(env) / "easy-installer"


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    return config_home(env) / "easy-installer"


def settings_path(env: Mapping[str, str] | None = None) -> Path:
    return config_dir(env) / "settings.json"
