"""Everything Easy Installer knows about one installed app, and what can be done with it.

Opened by activating an app's row (or "Details" in its menu): icon, name, version, who it is
installed for, where it lives, its size and the size of its settings and data (worked out in a
worker thread), when it was installed, where updates come from ("Check Now"), where the file
was downloaded from, its signature, the kept previous version ("Go Back" / delete it), Repair and
Uninstall.

The dialog never changes anything itself: the window runs the work (and knows what is busy); the
dialog only asks and shows the result - if it is still open when the work is done.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gi.repository import Adw, Gio, GObject, Gtk

from ..core import backups, installer
from ..core.appdata import DataLocation
from ..core.installer import find_installed
from ..core.origin import origin_host
from ..core.paths import Scope
from ..core.registry import STATUS_MISSING_APPIMAGE, STATUS_OK, InstalledApp
from ..core.signature import STATUS_INVALID, STATUS_VALID, SignatureInfo
from ..core.settings import load_settings
from ..core.updater import check_app_update, has_update_source, update_source_of
from ..core.updates import AvailableUpdate, UpdateCache
from ..errors import EasyInstallerError, NetworkError
from ..i18n import _
from .app_row import is_portable, scope_label
from .async_utils import run_in_thread
from .common import (
    app_icon_paintable,
    display_path,
    display_text,
    format_date,
    format_size,
    icon_button,
)
from .uninstall_dialog import data_summary, location_subtitle

log = logging.getLogger(__name__)

ICON_SIZE = 96
SEPARATOR = " · "
SECONDS_PER_DAY = 24 * 3600

SIGNATURE_NONE = "none"
SIGNATURE_VALID = "valid"
SIGNATURE_UNVERIFIED = "unverified"
SIGNATURE_INVALID = "invalid"

Done = Callable[["InstalledApp | None", "BaseException | None"], None]


class DetailsHost(Protocol):
    """What the dialog needs from the main window."""

    def show_in_files(self, app: InstalledApp) -> None: ...
    def repair(self, app: InstalledApp, on_done: Done | None = None) -> None: ...
    def confirm_uninstall(self, app: InstalledApp) -> Adw.AlertDialog: ...
    def start_update(self, app: InstalledApp, update: AvailableUpdate) -> None: ...
    def rollback_app(self, app: InstalledApp, on_done: Done | None = None) -> None: ...
    def drop_backup_app(self, app: InstalledApp, on_done: Done | None = None) -> None: ...
    def remember_update(self, app: InstalledApp, update: AvailableUpdate | None) -> None: ...
    def update_for(self, app: InstalledApp) -> AvailableUpdate | None: ...
    def needs_repair(self, app: InstalledApp) -> bool: ...
    def is_app_busy(self, app: InstalledApp) -> bool: ...


# ------------------------------------------------------------------------------------------------
# texts and facts (no widgets: testable without a display)
# ------------------------------------------------------------------------------------------------


def format_fingerprint(fingerprint: str | None) -> str | None:
    """"ABCD1234…" in groups of four, as gpg shows it."""
    if not fingerprint:
        return None
    return " ".join(fingerprint[i:i + 4] for i in range(0, len(fingerprint), 4))


def signature_state(app: InstalledApp) -> tuple[str, str]:
    """``(state, text)`` for the signature line. Not being signed is the norm, not a warning."""
    info = SignatureInfo.from_dict(app.signature)
    if info is None:
        return SIGNATURE_NONE, _("Not signed (most apps are not)")
    if info.status == STATUS_VALID:
        # (the key travels inside the file and anyone can name a key: the name is the key's)
        text = (_("Signed with a key named “{signer}”").format(signer=display_text(info.signer))
                if info.signer else _("Signed with a key that has no name"))
        fingerprint = format_fingerprint(info.fingerprint)
        if fingerprint:
            text += "\n" + _("Key {fingerprint}").format(fingerprint=fingerprint)
        return SIGNATURE_VALID, text
    if info.status == STATUS_INVALID:
        return SIGNATURE_INVALID, info.explanation or _("The signature of this file does not match. "
                                                    "The file may have been changed.")
    return SIGNATURE_UNVERIFIED, info.explanation or _("Signed, but the signature could not be "
                                                   "checked.")


def origin_text(app: InstalledApp) -> str:
    host = origin_host(app.origin_url)
    return host if host else _("Not known")


def kind_text(app: InstalledApp) -> str:
    if is_portable(app):
        if app.original_filename:
            return _("Portable app, unpacked from {file}").format(
                file=display_text(app.original_filename))
        return _("Portable app")
    return _("AppImage")


def location_of(app: InstalledApp) -> str:
    """The app's folder (portable apps) or file."""
    return app.install_dir if is_portable(app) and app.install_dir else app.appimage_path


def updates_text(app: InstalledApp) -> str:
    """Where new versions come from - or why this app is not checked."""
    if is_portable(app):
        return _("Portable apps are not checked for updates. To update this app, open a "
                 "newer download of it with Easy Installer.")
    if app.base_id or app.pinned:
        if app.version:
            return _("This copy stays at version {version}. It is never updated.").format(
                version=app.version)
        return _("This copy stays as it is. It is never updated.")
    source = update_source_of(app)
    if source is None or not has_update_source(app):
        return _("This app does not say where to find new versions. To update it, open a "
                 "newer download of it with Easy Installer.")
    return _("From {source}").format(source=source.describe())


def last_checked_text(app: InstalledApp, cache: UpdateCache | None = None) -> str | None:
    try:
        cache = cache or UpdateCache()
        checked = cache.checked_at(Scope(app.scope).value, app.id)
        failed = cache.failed(Scope(app.scope).value, app.id)
    except Exception:  # noqa: BLE001 - the cache is a nicety
        log.debug("cannot read the update cache", exc_info=True)
        return None
    date = format_date(checked)
    if date and failed:
        return _("Last checked on {date}, but that check failed").format(date=date)
    return _("Last checked on {date}").format(date=date) if date else None


def update_text(update: AvailableUpdate) -> str:
    if update.version:
        return _("Version {version} is available").format(version=update.version)
    return _("A new version is available")


def update_subtitle(update: AvailableUpdate) -> str | None:
    parts = []
    date = format_date(update.published_at)
    if date:
        parts.append(_("Released on {date}").format(date=date))
    if update.size:
        parts.append(_("Download size {size}").format(size=format_size(update.size)))
    return SEPARATOR.join(parts) or None


def kept_until(previous: dict, backup_days: int, *, now: float | None = None) -> str | None:
    """The date after which the kept version is deleted (None: not known)."""
    saved = backups.saved_time(previous)
    if saved is None or backup_days <= 0:
        return None
    moment = saved + backup_days * SECONDS_PER_DAY
    now = time.time() if now is None else now
    if moment < now:
        moment = now
    return format_date(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)))


def previous_title(previous: dict) -> str:
    version = previous.get("version")
    return _("Version {version}").format(version=version) if version else _("Previous version")


def go_back_text(previous: dict) -> str:
    """"Go back to version 1.1.2" (the button's tooltip)."""
    version = previous.get("version")
    if version:
        return _("Go back to version {version}").format(version=version)
    return _("Go back to the previous version")


def go_back_question(previous: dict) -> str:
    """"Go back to version 1.1.2?" (the heading of the confirmation)."""
    version = previous.get("version")
    if version:
        return _("Go back to version {version}?").format(version=version)
    return _("Go back to the previous version?")


def delete_backup_heading(previous: dict) -> str:
    version = previous.get("version")
    if version:
        return _("Delete version {version}?").format(version=version)
    return _("Delete the previous version?")


def previous_subtitle(previous: dict, backup_days: int, *, system: bool = False,
                      now: float | None = None) -> str:
    """"Kept until 14 October 2026 · 820 MB". ``system``: the app is installed for everyone -
    its kept version is deleted by the next installation for everyone, not at the next start."""
    parts = []
    saved = backups.saved_time(previous)
    now = time.time() if now is None else now
    expired = backup_days <= 0 or (
        saved is not None and saved + backup_days * SECONDS_PER_DAY < now)
    until = kept_until(previous, backup_days, now=now)
    if system and expired:
        parts.append(_("Deleted the next time an app is installed or updated for everyone"))
    elif until:
        parts.append(_("Kept until {date}").format(date=until))
    elif backup_days <= 0:
        parts.append(_("Deleted the next time Easy Installer starts"))
    else:
        parts.append(_("Kept on this computer"))
    if previous.get("size"):
        parts.append(format_size(previous["size"]))
    return SEPARATOR.join(parts)


def usable_previous(app: InstalledApp) -> dict | None:
    """The kept previous version, if it is really there."""
    try:
        return backups.usable_previous(installer.layout_for(Scope(app.scope)), app)
    except (OSError, ValueError):
        return None


def app_data_locations(app: InstalledApp) -> list[DataLocation]:
    """The app's settings and data folders with their sizes (worker thread).

    Unlike ``installer.removable_data`` this also answers while another installation of the
    app remains (they share the folders): it is only shown, never deleted from here.
    """
    return installer.app_data(app)


# ------------------------------------------------------------------------------------------------
# the dialog
# ------------------------------------------------------------------------------------------------


def _row(title: str, subtitle: str | None = None, *, lines: int = 0) -> Adw.ActionRow:
    # Not selectable: the first selectable label would take the focus when the dialog opens
    # and show all of its text selected.
    row = Adw.ActionRow(title=title, use_markup=False, subtitle_lines=lines)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def _row_spinner() -> Gtk.Spinner:
    spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, spinning=True)
    spinner.set_size_request(16, 16)
    return spinner


class DetailsDialog(Adw.Dialog):
    __gtype_name__ = "EasyInstallerDetailsDialog"
    __gsignals__ = {
        # the size of the settings and data folders is known (tests, screenshots)
        "data-ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # a "Check Now" finished (tests, screenshots)
        "checked": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, host: DetailsHost, app: InstalledApp):
        super().__init__(title=app.name, content_width=520, content_height=680)
        self._host = host
        self.app = app
        self._closed = False
        self._generation = 0
        self._check_state: tuple[str, object] | None = None   # ("current"|"error", …)
        self._checking = False
        self._update_rows: list[Gtk.Widget] = []
        self.locations: list[DataLocation] | None = None
        self.connect("closed", self._on_closed)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        toolbar.add_top_bar(header)
        self._content = Adw.Bin()
        toolbar.set_content(self._content)
        self.set_child(toolbar)
        self._build()

    # -- building ---------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def _on_closed(self, *_args: object) -> None:
        self._closed = True

    def refresh(self, app: InstalledApp | None = None) -> None:
        """Show ``app`` (the entry after a change); None: close if the app is gone."""
        if self._closed:
            return
        if app is None:
            self.close()
            return
        self.app = app
        self.set_title(app.name)
        self._build()

    def _build(self) -> None:
        self._generation += 1
        app = self.app
        busy = self._host.is_app_busy(app)
        self.page = Adw.PreferencesPage()
        self.page.add(self._build_header(app))
        self.updates_group = Adw.PreferencesGroup(title=_("Updates"))
        self._update_rows = []
        self._fill_updates_group()
        self.page.add(self.updates_group)
        self.page.add(self._build_app_group(app))
        previous = usable_previous(app)
        if previous is not None:
            self.page.add(self._build_previous_group(app, previous, busy))
        self.page.add(self._build_origin_group(app))
        self.page.add(self._build_actions_group(app, busy))
        self._content.set_child(self.page)
        generation = self._generation
        run_in_thread(app_data_locations,
                      lambda result, error: self._on_data_found(generation, result, error), app)

    def _build_header(self, app: InstalledApp) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)
        box.add_css_class("details-header")
        image = Gtk.Image(pixel_size=ICON_SIZE, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        paintable = app_icon_paintable(self, app.icon_path, ICON_SIZE) \
            if self.get_display() is not None else None
        if paintable is not None:
            image.set_from_paintable(paintable)
        else:
            image.set_from_icon_name("application-x-executable")
        image.add_css_class("icon-dropshadow")
        box.append(image)
        name = Gtk.Label(label=app.name, wrap=True, justify=Gtk.Justification.CENTER)
        name.add_css_class("title-1")
        box.append(name)
        facts = [_("Version {version}").format(version=app.version) if app.version else None,
                 scope_label(app.scope)]
        line = Gtk.Label(label=SEPARATOR.join(f for f in facts if f), wrap=True,
                         justify=Gtk.Justification.CENTER)
        line.add_css_class("dim-label")
        box.append(line)
        if app.comment:
            comment = Gtk.Label(label=app.comment, wrap=True, justify=Gtk.Justification.CENTER,
                                max_width_chars=50)
            comment.add_css_class("app-comment")
            box.append(comment)
        group.add(box)
        return group

    def _build_app_group(self, app: InstalledApp) -> Adw.PreferencesGroup:
        # Version and who it is installed for are shown right under the name.
        group = Adw.PreferencesGroup(title=_("App"))
        group.add(_row(_("Type"), kind_text(app)))

        status = app.status()
        where = location_of(app)
        location = _row(_("Location"), display_path(where) or _("Not known"))
        if status == STATUS_MISSING_APPIMAGE:
            location.set_subtitle(_("The app file is missing. It was expected at {path}").format(
                path=display_path(where)))
        else:
            show = icon_button("folder-open-symbolic", _("Show in Files"))
            show.connect("clicked", lambda *_a: self._host.show_in_files(self.app))
            location.add_suffix(show)
        group.add(location)

        if app.size > 0:
            group.add(_row(_("App size"), format_size(app.size)))

        self.data_row = Adw.ExpanderRow(title=_("Settings and data"), use_markup=False,
                                        subtitle=_("Working out the size…"),
                                        enable_expansion=False)
        self._data_spinner = _row_spinner()
        self.data_row.add_suffix(self._data_spinner)
        group.add(self.data_row)

        installed = format_date(app.installed_at)
        if installed:
            group.add(_row(_("Installed"), installed))
        updated = format_date(app.updated_at)
        if updated and updated != installed:
            group.add(_row(_("Last changed"), updated))
        return group

    def _build_origin_group(self, app: InstalledApp) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Origin"))
        origin = _row(_("Downloaded from"), origin_text(app))
        if app.origin_url:
            origin.set_tooltip_text(display_text(app.origin_url))
        group.add(origin)
        state, text = signature_state(app)
        signature = _row(_("Signature"), text)
        icon_name, css = {
            SIGNATURE_VALID: ("emblem-ok-symbolic", "success"),
            SIGNATURE_INVALID: ("dialog-warning-symbolic", "warning"),
            SIGNATURE_UNVERIFIED: ("dialog-information-symbolic", "dim-label"),
        }.get(state, (None, None))
        if icon_name:
            icon = Gtk.Image(icon_name=icon_name, valign=Gtk.Align.CENTER,
                             accessible_role=Gtk.AccessibleRole.PRESENTATION)
            icon.add_css_class(css)
            signature.add_prefix(icon)
        group.add(signature)
        return group

    def _build_previous_group(self, app: InstalledApp, previous: dict,
                              busy: bool) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Previous version"),
            description=_("Kept when it was replaced, so you can go back to it."))
        days = load_settings().backup_days
        row = _row(previous_title(previous), previous_subtitle(
            previous, days, system=Scope(app.scope) is Scope.SYSTEM))
        self.go_back_button = Gtk.Button(label=_("Go Back"), valign=Gtk.Align.CENTER,
                                         sensitive=not busy, tooltip_text=go_back_text(previous))
        self.go_back_button.connect("clicked", self._on_go_back_clicked)
        row.add_suffix(self.go_back_button)
        self.delete_backup_button = icon_button("user-trash-symbolic", _("Delete Previous Version"))
        self.delete_backup_button.set_sensitive(not busy)
        self.delete_backup_button.connect("clicked", self._on_delete_backup_clicked)
        row.add_suffix(self.delete_backup_button)
        group.add(row)
        return group

    def _build_actions_group(self, app: InstalledApp, busy: bool) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Troubleshooting"))
        needs_repair = self._host.needs_repair(app)
        if needs_repair:
            subtitle = _("This app changed since it was installed. Repair it to bring its "
                         "details and its menu entry up to date.")
        else:
            subtitle = _("Adds the app to the app menu again and refreshes its icon and "
                         "details.")
        if Scope(app.scope) is Scope.SYSTEM:
            subtitle += " " + _("You will be asked for your password.")
        repair = _row(_("Repair"), subtitle)
        broken = app.status() == STATUS_MISSING_APPIMAGE
        self.repair_button = Gtk.Button(label=_("Repair"), valign=Gtk.Align.CENTER,
                                        sensitive=not busy and not broken)
        if needs_repair:
            self.repair_button.add_css_class("suggested-action")
        self.repair_button.connect("clicked", self._on_repair_clicked)
        repair.add_suffix(self.repair_button)
        group.add(repair)

        self.uninstall_button = Gtk.Button(
            label=_("Remove from List…") if broken else _("Uninstall…"),
            halign=Gtk.Align.CENTER, margin_top=18, sensitive=not busy)
        self.uninstall_button.add_css_class("pill")
        self.uninstall_button.add_css_class("destructive-action")
        self.uninstall_button.connect("clicked", self._on_uninstall_clicked)
        group.add(self.uninstall_button)
        return group

    # -- updates ----------------------------------------------------------------------------------

    def _fill_updates_group(self) -> None:
        for row in self._update_rows:
            self.updates_group.remove(row)
        self._update_rows = []
        app = self.app
        checkable = has_update_source(app)
        subtitle = updates_text(app)
        if checkable:
            checked = last_checked_text(app)
            if checked:
                subtitle += "\n" + checked
        source = _row(_("New versions"), subtitle)
        if checkable:
            if self._checking:
                source.add_suffix(_row_spinner())
            else:
                self.check_button = Gtk.Button(label=_("Check Now"), valign=Gtk.Align.CENTER,
                                               sensitive=not self._host.is_app_busy(app))
                self.check_button.connect("clicked", self._on_check_clicked)
                source.add_suffix(self.check_button)
        self._add_update_row(source)

        update = self._host.update_for(app) if checkable else None
        if update is not None and app.status() == STATUS_OK:
            row = _row(update_text(update), update_subtitle(update))
            icon = Gtk.Image(icon_name="software-update-available-symbolic",
                             valign=Gtk.Align.CENTER,
                             accessible_role=Gtk.AccessibleRole.PRESENTATION)
            icon.add_css_class("accent")
            row.add_prefix(icon)
            self.update_button = Gtk.Button(label=_("Update"), valign=Gtk.Align.CENTER,
                                            sensitive=not self._host.is_app_busy(app))
            self.update_button.add_css_class("suggested-action")
            self.update_button.connect("clicked", self._on_update_clicked, update)
            row.add_suffix(self.update_button)
            self._add_update_row(row)
        elif self._check_state is not None:
            state, value = self._check_state
            if state == "current":
                row = _row(_("{name} is up to date").format(name=app.name))
                icon = Gtk.Image(icon_name="emblem-ok-symbolic", valign=Gtk.Align.CENTER,
                                 accessible_role=Gtk.AccessibleRole.PRESENTATION)
                icon.add_css_class("success")
                row.add_prefix(icon)
            else:
                row = _row(_("Could not check for updates"), str(value))
                icon = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER,
                                 accessible_role=Gtk.AccessibleRole.PRESENTATION)
                icon.add_css_class("warning")
                row.add_prefix(icon)
            self._add_update_row(row)

    def _add_update_row(self, row: Gtk.Widget) -> None:
        self.updates_group.add(row)
        self._update_rows.append(row)

    def check_now(self) -> None:
        """Ask the app's update source right now (worker thread)."""
        if self._checking or not has_update_source(self.app):
            return
        self._checking = True
        self._check_state = None
        self._fill_updates_group()
        app = self.app
        run_in_thread(check_app_update, lambda result, error: self._on_checked(app, result, error),
                      app, force=True)

    def _on_check_clicked(self, *_args: object) -> None:
        self.check_now()

    def _on_checked(self, app: InstalledApp, update: AvailableUpdate | None,
                    error: BaseException | None) -> None:
        # The window learns the answer even if this dialog was closed in the meantime.
        if error is None:
            self._host.remember_update(app, update)
        elif not isinstance(error, EasyInstallerError):
            log.warning("checking %s for updates failed: %s", app.id, error)
        if self._closed:
            return
        self._checking = False
        if error is None:
            self._check_state = ("current", None) if update is None else None
        elif isinstance(error, EasyInstallerError):
            self._check_state = ("error", error)
        else:
            self._check_state = ("error", _("The update information of this app could not be "
                                            "read."))
        if isinstance(error, NetworkError):
            log.info("no connection for %s: %s", app.id, error.details or error)
        self._fill_updates_group()
        self.emit("checked")

    def _on_update_clicked(self, _button: Gtk.Button, update: AvailableUpdate) -> None:
        app = self.app
        self.close()
        self._host.start_update(app, update)

    # -- settings and data ------------------------------------------------------------------------

    def _on_data_found(self, generation: int, locations: list[DataLocation] | None,
                       error: BaseException | None) -> None:
        if self._closed or generation != self._generation:
            return
        if error is not None:
            log.warning("cannot look for the data of %s: %s", self.app.id, error)
        self.show_data(locations or [])

    def show_data(self, locations: list[DataLocation]) -> None:
        self.locations = list(locations)
        self._data_spinner.set_visible(False)
        self._data_spinner.set_spinning(False)
        self.data_row.set_subtitle(data_summary(self.locations))
        for location in self.locations:
            row = Adw.ActionRow(title=display_path(location.path),
                                subtitle=location_subtitle(location), use_markup=False,
                                title_lines=1, tooltip_text=display_path(location.path))
            show = icon_button("folder-open-symbolic", _("Show in Files"))
            show.connect("clicked", self._on_show_data_clicked, location.path)
            row.add_suffix(show)
            self.data_row.add_row(row)
        self.data_row.set_enable_expansion(bool(self.locations))
        self.emit("data-ready")

    def _on_show_data_clicked(self, _button: Gtk.Button, path: Path) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(os.fspath(path)))
        launcher.launch(self.get_root(), None, _log_launch_result, path)

    # -- previous version, repair, uninstall ------------------------------------------------------

    def _set_working(self) -> None:
        for name in ("go_back_button", "delete_backup_button", "repair_button",
                     "uninstall_button", "check_button", "update_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.set_sensitive(False)

    def _operation_done(self, app: InstalledApp | None, error: BaseException | None) -> None:
        """The window finished what this dialog asked for: show the app as it is now."""
        if self._closed:
            return
        self.refresh(app if app is not None else self.current_entry())

    def current_entry(self) -> InstalledApp | None:
        """The app's registry entry as it is now (None if it is gone)."""
        try:
            return next((a for a in find_installed(self.app.id)
                         if Scope(a.scope) is Scope(self.app.scope)), None)
        except Exception:  # noqa: BLE001 - an unreadable registry: show what is known
            log.warning("cannot look up %s", self.app.id, exc_info=True)
            return self.app

    def _on_go_back_clicked(self, *_args: object) -> Adw.AlertDialog | None:
        previous = usable_previous(self.app)
        if previous is None:
            self.refresh(self.current_entry())
            return None
        return self.confirm_go_back(previous)

    def confirm_go_back(self, previous: dict) -> Adw.AlertDialog:
        app = self.app
        body = _("{name} goes back to the version that was kept. The version you have now is "
                 "kept instead, so you can switch back later. Your settings and files stay as "
                 "they are.").format(name=app.name)
        if Scope(app.scope) is Scope.SYSTEM:
            body += "\n\n" + _("It is installed for everyone on this computer, so you will be "
                               "asked for your password.")
        dialog = Adw.AlertDialog(heading=go_back_question(previous), body=body)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("go-back", _("_Go Back"))
        dialog.set_response_appearance("go-back", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_go_back_response)
        dialog.present(self)
        return dialog

    def _on_go_back_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "go-back" or self._closed:
            return
        self._set_working()
        self._host.rollback_app(self.app, self._operation_done)

    def _on_delete_backup_clicked(self, *_args: object) -> Adw.AlertDialog | None:
        previous = usable_previous(self.app)
        if previous is None:
            self.refresh(self.current_entry())
            return None
        return self.confirm_delete_backup(previous)

    def confirm_delete_backup(self, previous: dict) -> Adw.AlertDialog:
        app = self.app
        body = _("The previous version of {name} that was kept is deleted. You can no longer go "
                 "back to it.").format(name=app.name)
        if Scope(app.scope) is Scope.SYSTEM:
            body += "\n\n" + _("It is installed for everyone on this computer, so you will be "
                               "asked for your password.")
        dialog = Adw.AlertDialog(heading=delete_backup_heading(previous), body=body)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("delete", _("_Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_backup_response)
        dialog.present(self)
        return dialog

    def _on_delete_backup_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "delete" or self._closed:
            return
        self._set_working()
        self._host.drop_backup_app(self.app, self._operation_done)

    def _on_repair_clicked(self, *_args: object) -> None:
        self._set_working()
        self._host.repair(self.app, self._operation_done)

    def _on_uninstall_clicked(self, *_args: object) -> None:
        app = self.app
        self.close()
        self._host.confirm_uninstall(app)


def _log_launch_result(launcher: Gtk.FileLauncher, result: Gio.AsyncResult, path: Path) -> None:
    try:
        launcher.launch_finish(result)
    except Exception as exc:  # noqa: BLE001 - GLib.Error; there is nothing to do but log it
        log.warning("cannot open %s: %s", path, exc)
