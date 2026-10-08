"""One installed app in the main window's list."""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GObject, Gtk

from ..core.paths import Scope
from ..core.registry import (
    KIND_PORTABLE,
    STATUS_MISSING_APPIMAGE,
    STATUS_MISSING_LAUNCHER,
    STATUS_OK,
    InstalledApp,
)
from ..core.updates import AvailableUpdate
from ..i18n import _
from .common import app_icon_image, format_size

ICON_SIZE = 48
SEPARATOR = " · "
UPDATE_ICON = "software-update-available-symbolic"


def scope_label(scope: Scope) -> str:
    return _("Only for me") if Scope(scope) is Scope.USER else _("Everyone on this computer")


def is_portable(app: InstalledApp) -> bool:
    return app.kind == KIND_PORTABLE


def app_subtitle(app: InstalledApp, status: str, *, portable_text: bool = False) -> str:
    """"Version 1.1.3 · Only for me · 820.3 MB", or the problem the app has.

    ``portable_text``: say "Portable app" in words (narrow windows hide the "Portable" tag).
    """
    if status == STATUS_MISSING_APPIMAGE:
        return _("App file is missing")
    if status == STATUS_MISSING_LAUNCHER:
        return _("Missing from the app menu")
    parts = []
    if portable_text and is_portable(app):
        parts.append(_("Portable app"))
    if app.version:
        parts.append(_("Version {version}").format(version=app.version))
    parts.append(scope_label(app.scope))
    if app.size > 0:
        parts.append(format_size(app.size))
    return SEPARATOR.join(parts)


def update_label(update: AvailableUpdate, *, compact: bool = False) -> str:
    """The row's update button: "Update to 1.1.4" (just "Update" in narrow windows)."""
    if update.version and not compact:
        return _("Update to {version}").format(version=update.version)
    return _("Update")


def update_tooltip(app: InstalledApp, update: AvailableUpdate) -> str:
    if update.version:
        return _("Update {name} to version {version}").format(name=app.name,
                                                              version=update.version)
    return _("Install the new version of {name}").format(name=app.name)


class AppRow(Adw.ActionRow):
    """Row with icon, name, version · scope · size (or a problem) and Update / Open / menu buttons.

    Activating the row shows the app's details. The row only emits signals; the window does
    the actual work.
    """

    __gtype_name__ = "EasyInstallerAppRow"
    __gsignals__ = {
        "open-app": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "show-in-files": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "repair": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "uninstall": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "details": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "update": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: InstalledApp, *, update: AvailableUpdate | None = None,
                 needs_repair: bool = False, compact: bool = False):
        # Two subtitle lines: in narrow windows "Version … · Everyone on this computer · …"
        # (longer in German/Dutch) wraps instead of cutting off who the app is installed for.
        super().__init__(use_markup=False, title_lines=1, subtitle_lines=2, activatable=True)
        self.app = app
        self.status = app.status()
        # Only an app that works can be updated (a missing file is removed, not updated).
        self.update = update if self.status == STATUS_OK else None
        self.needs_repair = needs_repair
        self._busy = False
        self._compact = compact

        self.set_title(app.name)
        if self.status == STATUS_MISSING_APPIMAGE and app.appimage_path:
            self.set_tooltip_text(_("Expected at {path}").format(path=app.appimage_path))
        self.add_prefix(app_icon_image(app.icon_path, ICON_SIZE))
        self.connect("activated", lambda *_args: self.emit("details"))

        self._actions = Gio.SimpleActionGroup()
        self._add_action("open", "open-app", self.status == STATUS_OK)
        self._add_action("show-in-files", "show-in-files", self.status != STATUS_MISSING_APPIMAGE)
        self._add_action("repair", "repair", self.can_repair)
        self._add_action("uninstall", "uninstall", True)
        self._add_action("details", "details", True)
        self._add_action("update", "update", self.update is not None)
        self.insert_action_group("row", self._actions)

        self._spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self._spinner.set_size_request(16, 16)
        self.add_suffix(self._spinner)

        self._tag = Gtk.Label(label=_("Portable"), valign=Gtk.Align.CENTER,
                              visible=is_portable(app),
                              tooltip_text=_("Unpacked from an archive. It is not checked for "
                                             "updates."))
        self._tag.add_css_class("app-tag")
        self.add_suffix(self._tag)

        if self.status != STATUS_OK:
            warning = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=app_subtitle(app, self.status))
            warning.add_css_class("warning")
            warning.update_property([Gtk.AccessibleProperty.LABEL],
                                    [app_subtitle(app, self.status)])
            self.add_suffix(warning)

        self._update_button: Gtk.Button | None = None
        if self.update is not None:
            self._update_button = Gtk.Button(
                label=update_label(self.update), action_name="row.update",
                valign=Gtk.Align.CENTER, can_shrink=True,
                tooltip_text=update_tooltip(app, self.update))
            self._update_button.update_property([Gtk.AccessibleProperty.LABEL],
                                                [update_tooltip(app, self.update)])
            self._update_button.add_css_class("suggested-action")
            self.add_suffix(self._update_button)

        # Its menu entry was deleted from outside (e.g. with Linux Mint's own "Uninstall", which
        # only removes the menu entry): uninstalling the rest is offered next to "Repair".
        self._uninstall_button: Gtk.Button | None = None
        if self.status == STATUS_MISSING_LAUNCHER:
            self._uninstall_button = Gtk.Button(
                label=_("Uninstall…"), action_name="row.uninstall", valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove {name} from this computer").format(name=app.name))
            self.add_suffix(self._uninstall_button)

        self._main_button = self._make_main_button()
        self.add_suffix(self._main_button)

        self._menu_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, menu_model=self._make_menu(),
            tooltip_text=_("More Actions"))
        self._menu_button.add_css_class("flat")
        self._menu_button.update_property([Gtk.AccessibleProperty.LABEL],
                                          [_("More actions for {name}").format(name=app.name)])
        self.add_suffix(self._menu_button)
        self.set_compact(compact)

    # -- construction ---------------------------------------------------------------------------

    @property
    def can_repair(self) -> bool:
        return self.status == STATUS_MISSING_LAUNCHER or (self.needs_repair
                                                          and self.status == STATUS_OK)

    def _add_action(self, name: str, signal: str, enabled: bool) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.set_enabled(enabled)
        action.connect("activate", lambda *_args: self.emit(signal))
        self._actions.add_action(action)

    def _make_main_button(self) -> Gtk.Button:
        if self.status == STATUS_MISSING_APPIMAGE:
            label, action, tooltip = _("Remove"), "row.uninstall", _("Remove it from this list")
        elif self.status == STATUS_MISSING_LAUNCHER:
            label, action, tooltip = _("Repair"), "row.repair", _("Add it to the app menu again")
        else:
            label, action, tooltip = _("Open"), "row.open", _("Start {name}").format(name=self.app.name)
        return Gtk.Button(label=label, action_name=action, valign=Gtk.Align.CENTER,
                          tooltip_text=tooltip)

    def _make_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        if self.update is not None:
            section.append(update_label(self.update), "row.update")
        if self.status == STATUS_OK:
            section.append(_("Open"), "row.open")
        if self.status != STATUS_MISSING_APPIMAGE:
            section.append(_("Show in Files"), "row.show-in-files")
        section.append(_("Details"), "row.details")
        if self.can_repair:
            section.append(_("Repair"), "row.repair")
        menu.append_section(None, section)
        danger = Gio.Menu()
        if self.status == STATUS_MISSING_APPIMAGE:
            danger.append(_("Remove from List…"), "row.uninstall")
        else:
            danger.append(_("Uninstall…"), "row.uninstall")
        menu.append_section(None, danger)
        return menu

    # -- state ----------------------------------------------------------------------------------

    @property
    def key(self) -> tuple[str, str]:
        return (self.app.id, Scope(self.app.scope).value)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        """Narrow window: the "Portable" tag becomes words in the subtitle, the update button
        becomes a round icon button (its tooltip says what it does), and "Open" moves into the
        menu while an update is offered - the app's name keeps its room."""
        self._compact = compact
        portable = is_portable(self.app)
        self._tag.set_visible(portable and not compact)
        self.set_subtitle(app_subtitle(self.app, self.status,
                                       portable_text=portable and compact))
        if self._update_button is not None:
            if compact:
                self._update_button.set_icon_name(UPDATE_ICON)
                self._update_button.add_css_class("circular")
            else:
                self._update_button.set_label(update_label(self.update))
                self._update_button.remove_css_class("circular")
            self._main_button.set_visible(not compact)
        if self._uninstall_button is not None:
            self._uninstall_button.set_visible(not compact)   # (it is in the menu, too)

    def set_busy(self, busy: bool) -> None:
        """Show a spinner and block the buttons while something runs for this app."""
        self._busy = busy
        self._spinner.set_visible(busy)
        self._spinner.set_spinning(busy)
        self._main_button.set_sensitive(not busy)
        self._menu_button.set_sensitive(not busy)
        if self._uninstall_button is not None:
            self._uninstall_button.set_sensitive(not busy)
        if self._update_button is not None:
            self._update_button.set_sensitive(not busy)

    def matches(self, query: str) -> bool:
        query = query.strip().casefold()
        if not query:
            return True
        haystack = " ".join(filter(None, (self.app.name, self.app.comment, self.app.id,
                                          os.path.basename(self.app.appimage_path or ""))))
        return query in haystack.casefold()
