"""The user's preferences in ``$XDG_CONFIG_HOME/easy-installer/settings.json`` (standard library).

One small JSON object shared by the command line and the window. :class:`Settings` holds the
preferences Easy Installer itself understands; every other key of the file (e.g. the window's
``default_handler_offer_dismissed``) is kept verbatim in ``Settings.extra``.

Settings are a convenience: a missing, unreadable or damaged file means "the defaults", and
saving never raises.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import settings_path

log = logging.getLogger(__name__)

KEY_CHECK_UPDATES = "check_updates"
KEY_UPDATE_INTERVAL_HOURS = "update_interval_hours"
KEY_BACKUP_DAYS = "backup_days"
KNOWN_KEYS = (KEY_CHECK_UPDATES, KEY_UPDATE_INTERVAL_HOURS, KEY_BACKUP_DAYS)

DEFAULT_CHECK_UPDATES = True
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_BACKUP_DAYS = 14
MIN_UPDATE_INTERVAL_HOURS = 1
MAX_UPDATE_INTERVAL_HOURS = 24 * 30
#: 0 = do not keep the replaced version. The administrator helper accepts at most 90 days.
MIN_BACKUP_DAYS = 0
MAX_BACKUP_DAYS = 90

DIR_MODE = 0o755


@dataclass
class Settings:
    #: look for updates automatically (at most every ``update_interval_hours``)
    check_updates: bool = DEFAULT_CHECK_UPDATES
    update_interval_hours: int = DEFAULT_UPDATE_INTERVAL_HOURS
    #: keep the replaced version of an app this long; 0 = do not keep it
    backup_days: int = DEFAULT_BACKUP_DAYS
    #: every other key of settings.json, preserved verbatim
    extra: dict = field(default_factory=dict)
    #: ``extra`` as it was loaded: saving only writes the keys that were changed since, so a
    #: value another window saved in the meantime is not put back to its old state.
    _loaded_extra: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def keep_backups(self) -> bool:
        return self.backup_days > 0

    def to_dict(self) -> dict[str, Any]:
        """The complete content of settings.json for these settings."""
        return {**copy.deepcopy(self.extra), **self._known()}

    def _known(self) -> dict[str, Any]:
        return {
            KEY_CHECK_UPDATES: bool(self.check_updates),
            KEY_UPDATE_INTERVAL_HOURS: _clamp(self.update_interval_hours,
                                              DEFAULT_UPDATE_INTERVAL_HOURS,
                                              MIN_UPDATE_INTERVAL_HOURS, MAX_UPDATE_INTERVAL_HOURS),
            KEY_BACKUP_DAYS: _clamp(self.backup_days, DEFAULT_BACKUP_DAYS,
                                    MIN_BACKUP_DAYS, MAX_BACKUP_DAYS),
        }


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    """``value`` as an int within ``low..high``; anything that is not a whole number -> default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or value != int(value):
            return default
        value = int(value)
    return max(low, min(high, value))


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


# ------------------------------------------------------------------------------------------------
# the file
# ------------------------------------------------------------------------------------------------


def load_raw(path: Path | None = None) -> dict[str, Any]:
    """The JSON object in settings.json; ``{}`` if it is missing, unreadable or not an object."""
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        log.warning("ignoring unreadable settings file %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_raw(values: dict[str, Any], path: Path | None = None) -> bool:
    """Write settings.json atomically. Failures are logged, never raised; returns success."""
    path = path or settings_path()
    try:
        text = json.dumps(values, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        log.warning("could not save settings to %s: %s", path, exc)
        return False
    try:
        path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        log.warning("could not save settings to %s: %s", path, exc)
        return False
    return True


# ------------------------------------------------------------------------------------------------
# typed access
# ------------------------------------------------------------------------------------------------


def settings_from_dict(data: dict[str, Any]) -> Settings:
    """Wrong types fall back to the defaults, numbers are clamped to their allowed range."""
    extra = {key: copy.deepcopy(value) for key, value in data.items() if key not in KNOWN_KEYS}
    return Settings(
        check_updates=_flag(data.get(KEY_CHECK_UPDATES), DEFAULT_CHECK_UPDATES),
        update_interval_hours=_clamp(data.get(KEY_UPDATE_INTERVAL_HOURS),
                                     DEFAULT_UPDATE_INTERVAL_HOURS,
                                     MIN_UPDATE_INTERVAL_HOURS, MAX_UPDATE_INTERVAL_HOURS),
        backup_days=_clamp(data.get(KEY_BACKUP_DAYS), DEFAULT_BACKUP_DAYS,
                           MIN_BACKUP_DAYS, MAX_BACKUP_DAYS),
        extra=extra,
        _loaded_extra=copy.deepcopy(extra),
    )


def load_settings(path: Path | None = None) -> Settings:
    return settings_from_dict(load_raw(path))


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Save atomically; never raises (a failure is logged and the old file stays).

    The known keys are always written. Of ``settings.extra`` only what was changed since
    loading is written over the file's current content; keys removed from ``extra`` are removed.
    """
    try:
        data = load_raw(path)
        loaded = settings._loaded_extra
        for key in loaded:
            if key not in settings.extra:
                data.pop(key, None)
        for key, value in settings.extra.items():
            if key in KNOWN_KEYS:
                continue
            if key not in loaded or loaded[key] != value or key not in data:
                data[key] = copy.deepcopy(value)
        data.update(settings._known())
        if save_raw(data, path):
            settings._loaded_extra = copy.deepcopy(settings.extra)
    except Exception:  # noqa: BLE001 - settings are never worth a crash
        log.warning("could not save settings", exc_info=True)


def get_setting(key: str, default: Any = None, path: Path | None = None) -> Any:
    """One raw value of settings.json (for keys :class:`Settings` does not know)."""
    return load_raw(path).get(key, default)


def set_setting(key: str, value: Any, path: Path | None = None) -> None:
    """Change one raw value, keeping everything else in the file."""
    values = load_raw(path)
    values[key] = value
    save_raw(values, path)
