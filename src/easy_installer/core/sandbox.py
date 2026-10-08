"""Electron/Chromium sandbox fix for Ubuntu 24.04+ (restricted unprivileged user namespaces).

Chromium-based apps need unprivileged user namespaces for their sandbox. Ubuntu 24.04 and newer
only allow that for programs with an AppArmor profile granting ``userns``. We either install such a
profile for the AppImage (needs the administrator password once) or start the app with
``--no-sandbox``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .integration import ID_RE, apparmor_profile_name

if TYPE_CHECKING:
    from .inspector import AppImageInfo
    from .system_checks import SystemStatus


class SandboxFix(str, Enum):
    NONE = "none"
    APPARMOR = "apparmor"
    NO_SANDBOX = "no-sandbox"

    def __str__(self) -> str:
        return self.value


NO_SANDBOX_ARG = "--no-sandbox"

# Characters with a special meaning in AppArmor path globs (plus the escape char itself).
_APPARMOR_SPECIAL = frozenset('\\*?[]{}^"')


def needs_sandbox_fix(info: AppImageInfo, status: SystemStatus) -> bool:
    return bool(info.is_electron and status.userns_restricted and not info.exec_has_no_sandbox)


def default_sandbox_fix(status: SystemStatus) -> SandboxFix:
    if status.apparmor_parser and status.pkexec and status.apparmor_enabled:
        return SandboxFix.APPARMOR
    return SandboxFix.NO_SANDBOX


def apparmor_quote_path(path: str) -> str:
    """Escape AppArmor glob characters and wrap the path in double quotes.

    Raises ValueError for paths that cannot be expressed safely (relative or containing control
    characters such as newlines, which could otherwise inject rules into the profile).
    """
    if not path.startswith("/"):
        raise ValueError(f"AppArmor attachment path must be absolute: {path!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise ValueError(f"AppArmor attachment path contains control characters: {path!r}")
    escaped = "".join("\\" + ch if ch in _APPARMOR_SPECIAL else ch for ch in path)
    return f'"{escaped}"'


def render_apparmor_profile(app_id: str, appimage_path: str) -> str:
    if not ID_RE.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id!r}")
    profile = apparmor_profile_name(app_id)
    quoted = apparmor_quote_path(str(appimage_path))
    return (
        f"# Managed by Easy Installer — allows {app_id} to use unprivileged user namespaces\n"
        "# (required by the Chromium/Electron sandbox on Ubuntu 24.04 and newer).\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "\n"
        f"profile {profile} {quoted} flags=(unconfined) {{\n"
        "  userns,\n"
        "\n"
        f"  include if exists <local/{profile}>\n"
        "}\n"
    )
