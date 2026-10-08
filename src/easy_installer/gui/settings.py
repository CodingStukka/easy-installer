"""Tiny persistent GUI settings in ``$XDG_CONFIG_HOME/easy-installer/settings.json``.

A thin wrapper over :mod:`easy_installer.core.settings` (the same file): the functions here work
on the raw JSON object, for keys only the window uses. The preferences shared with the command
line (update checks, how long the previous version is kept) are ``core.settings.Settings``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import settings as core_settings

#: The user closed the "Open AppImages with Easy Installer…" banner.
DEFAULT_HANDLER_OFFER_DISMISSED = "default_handler_offer_dismissed"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    return core_settings.load_raw(path)


def save_settings(values: dict[str, Any], path: Path | None = None) -> None:
    """Write atomically; failures are logged (settings are a convenience, never fatal)."""
    core_settings.save_raw(values, path)


def get_setting(key: str, default: Any = None, path: Path | None = None) -> Any:
    return core_settings.get_setting(key, default, path)


def set_setting(key: str, value: Any, path: Path | None = None) -> None:
    core_settings.set_setting(key, value, path)
