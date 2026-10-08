"""Gettext setup. Use ``from easy_installer.i18n import _`` for every user-facing string."""

from __future__ import annotations

import gettext
import locale
import os
import sys
from pathlib import Path

from . import GETTEXT_DOMAIN

_translation: gettext.NullTranslations = gettext.NullTranslations()
_localedir: Path | None = None


def _has_catalog(directory: Path) -> bool:
    try:
        return any(directory.glob(f"*/LC_MESSAGES/{GETTEXT_DOMAIN}.mo"))
    except OSError:
        return False


def find_localedir() -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("EASY_INSTALLER_LOCALEDIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[2] / "build" / "locale")
    # `make install-user` puts the catalogs into ~/.local/share/locale.
    data_home = os.environ.get("XDG_DATA_HOME", "")
    if not os.path.isabs(data_home):
        data_home = os.path.join(os.path.expanduser("~"), ".local", "share")
    candidates.append(Path(data_home) / "locale")
    candidates.append(Path(sys.prefix) / "share" / "locale")
    candidates.append(Path("/usr/local/share/locale"))
    candidates.append(Path("/usr/share/locale"))
    for candidate in candidates:
        if candidate.is_dir() and _has_catalog(candidate):
            return candidate
    return None


def setup() -> None:
    """(Re)initialise translations. Called automatically on import."""
    global _translation, _localedir
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    _localedir = find_localedir()
    _translation = gettext.translation(
        GETTEXT_DOMAIN,
        localedir=str(_localedir) if _localedir else None,
        fallback=True,
    )


def localedir() -> Path | None:
    return _localedir


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def N_(message: str) -> str:
    """Mark a string for extraction without translating it now."""
    return message


setup()
