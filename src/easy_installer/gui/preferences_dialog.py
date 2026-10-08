"""Preferences: automatic update checks and how long the previous version of an app is kept.

The values live in settings.json (``core.settings``), shared with the command line
(``easy-installer settings``). Every change is saved at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gtk

from ..core.settings import Settings, load_settings, save_settings
from ..i18n import _, ngettext

log = logging.getLogger(__name__)

#: "Keep the previous version" choices in days (0 = off).
BACKUP_CHOICES = (0, 7, 14, 30)


def backup_choices(current: int) -> list[int]:
    """The offered choices; a value set elsewhere (e.g. 90 days with the command line) is
    offered, too, so opening the preferences never changes it."""
    choices = list(BACKUP_CHOICES)
    if current not in choices:
        choices.append(current)
        choices.sort()
    return choices


def backup_label(days: int) -> str:
    if days <= 0:
        return _("Off")
    return ngettext("{count} day", "{count} days", days).format(count=days)


def interval_text(hours: int) -> str:
    """"once a day", "every 2 days", "every 6 hours"."""
    if hours == 24:
        return _("once a day")
    if hours == 1:
        return _("once an hour")
    if hours % 24 == 0:
        days = hours // 24
        return ngettext("every {count} day", "every {count} days", days).format(count=days)
    return ngettext("every {count} hour", "every {count} hours", hours).format(count=hours)


def check_updates_subtitle(settings: Settings) -> str:
    return _("When Easy Installer starts, it looks for new versions of your apps, at most "
             "{interval}. Nothing is installed without asking you.").format(
                 interval=interval_text(settings.update_interval_hours))


def backup_subtitle(days: int) -> str:
    if days <= 0:
        return _("Replaced versions are not kept. Versions kept until now are deleted the next "
                 "time Easy Installer starts - those of apps for everyone the next time an app "
                 "is installed or updated for everyone.")
    return _("After an update, the replaced version stays on this computer for a while, so "
             "you can go back to it.")


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "EasyInstallerPreferencesDialog"

    def __init__(self, *, path: Path | None = None,
                 on_changed: Callable[[Settings], None] | None = None):
        super().__init__(title=_("Preferences"), search_enabled=False, content_width=520,
                         content_height=360)
        self._path = path
        self._on_changed = on_changed
        settings = load_settings(path)
        self._choices = backup_choices(settings.backup_days)

        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")

        updates = Adw.PreferencesGroup(title=_("Updates"))
        self.check_row = Adw.SwitchRow(title=_("Check for updates automatically"),
                                       subtitle=check_updates_subtitle(settings),
                                       active=settings.check_updates)
        self.check_row.connect("notify::active", self._on_check_updates_changed)
        updates.add(self.check_row)
        page.add(updates)

        backups = Adw.PreferencesGroup(title=_("Previous versions"))
        self.backup_row = Adw.ComboRow(
            title=_("Keep the previous version"), subtitle=backup_subtitle(settings.backup_days),
            model=Gtk.StringList.new([backup_label(days) for days in self._choices]),
            selected=self._choices.index(settings.backup_days))
        self.backup_row.connect("notify::selected", self._on_backup_days_changed)
        backups.add(self.backup_row)
        page.add(backups)

        self.add(page)

    # -- saving -----------------------------------------------------------------------------------

    @property
    def backup_days(self) -> int:
        index = self.backup_row.get_selected()
        return self._choices[index] if 0 <= index < len(self._choices) else self._choices[0]

    def _save(self, change: Callable[[Settings], None]) -> None:
        # Read the file again: the command line (or another window) may have changed it.
        settings = load_settings(self._path)
        change(settings)
        save_settings(settings, self._path)
        if self._on_changed is not None:
            try:
                self._on_changed(settings)
            except Exception:
                log.exception("reacting to changed settings failed")

    def _on_check_updates_changed(self, row: Adw.SwitchRow, _pspec: object) -> None:
        active = row.get_active()
        self._save(lambda settings: setattr(settings, "check_updates", active))

    def _on_backup_days_changed(self, row: Adw.ComboRow, _pspec: object) -> None:
        days = self.backup_days
        row.set_subtitle(backup_subtitle(days))
        self._save(lambda settings: setattr(settings, "backup_days", days))
