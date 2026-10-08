"""Uninstalling: the confirmation (what goes, what stays, the app's settings and data folders),
the offer to uninstall without the administrator password, and the texts for the result.

The window runs the work (:func:`uninstall_and_trash` in a worker thread) and shows the result;
nothing here touches files on the main loop.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Adw, GObject, Gtk

from ..core import appdata
from ..core.appdata import DataLocation
from ..core.installer import (
    other_installations,
    removable_data,
    uninstall,
    uninstall_needs_password,
)
from ..core.paths import Scope
from ..core.registry import KIND_PORTABLE, STATUS_MISSING_APPIMAGE, InstalledApp
from ..i18n import _, ngettext
from .async_utils import run_in_thread
from .common import display_path, format_size

log = logging.getLogger(__name__)

RESPONSE_CANCEL = "cancel"
RESPONSE_UNINSTALL = "uninstall"


# ------------------------------------------------------------------------------------------------
# texts (no widgets: testable without a display)
# ------------------------------------------------------------------------------------------------


def kind_label(kind: str) -> str:
    """What kind of folder a data location is, in plain words."""
    return {
        "config": _("Settings"),
        "data": _("App data"),
        "cache": _("Temporary files"),
        "state": _("App state"),
        "home": _("Settings and data"),
    }.get(kind, _("App data"))


def location_subtitle(location: DataLocation) -> str:
    return f"{kind_label(location.kind)} · {format_size(location.size)}"


def data_summary(locations: Sequence[DataLocation]) -> str:
    """"12.3 MB in 2 folders" (or that nothing was found)."""
    if not locations:
        return _("No settings or data found")
    total = sum(max(location.size, 0) for location in locations)
    return ngettext("{size} in {count} folder", "{size} in {count} folders",
                    len(locations)).format(size=format_size(total), count=len(locations))


def is_broken(app: InstalledApp) -> bool:
    """The app file is already gone: the entry is "removed" rather than "uninstalled"."""
    return app.status() == STATUS_MISSING_APPIMAGE


def uninstall_heading(app: InstalledApp) -> str:
    if is_broken(app):
        return _("Remove {name}?").format(name=app.name)
    return _("Uninstall {name}?").format(name=app.name)


def other_installations_note(app: InstalledApp, others: Iterable[InstalledApp]) -> str | None:
    """What stays installed: the same app for the other scope, or other kept versions."""
    others = list(others)
    scope = Scope(app.scope)
    if any(other.id == app.id and Scope(other.scope) is not scope for other in others):
        if scope is Scope.USER:
            return _("It is also installed for everyone on this computer. That copy stays "
                     "installed.")
        return _("It is also installed just for you. That copy stays installed.")
    if others:
        return _("The other installed versions of this app stay installed.")
    return None


def uninstall_body(app: InstalledApp, others: Iterable[InstalledApp] = (), *,
                   offers_data: bool = False) -> str:
    """The confirmation text. ``offers_data``: the dialog lists the settings and data folders
    that can go, too (so it does not promise that the settings are kept)."""
    if is_broken(app):
        parts = [_("The app file is already gone. This removes the app from this list and "
                   "from the app menu.")]
    elif app.kind == KIND_PORTABLE and offers_data:
        parts = [_("The app will be removed. Anything it saved in its own folder is moved to "
                   "the trash. Your other personal files are kept.")]
    elif app.kind == KIND_PORTABLE:
        parts = [_("The app will be removed. Anything it saved in its own folder is moved to "
                   "the trash. Your other personal files and settings are kept.")]
    elif offers_data:
        parts = [_("The app will be removed. Your personal files are kept.")]
    else:
        parts = [_("The app will be removed. Your personal files and settings are kept.")]
    if app.previous:
        parts.append(_("The previous version that was kept is removed, too."))
    if Scope(app.scope) is Scope.SYSTEM:
        parts.append(_("It is installed for everyone on this computer, so you will be asked "
                       "for your password."))
    elif uninstall_needs_password(app):
        parts.append(_("You will be asked for your password to remove the special permission "
                       "this app needed."))
    note = other_installations_note(app, others)
    if note:
        parts.append(note)
    return "\n\n".join(parts)


@dataclass
class UninstallOutcome:
    """What an uninstall did (see :func:`uninstall_and_trash`)."""

    #: translated notes of the uninstall itself (e.g. a folder that was left in place)
    notes: list[str] = field(default_factory=list)
    #: data folders that are in the trash now
    trashed: list[Path] = field(default_factory=list)
    #: data folders that stayed, with the reason (translated)
    trash_failures: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def messages(self) -> list[str]:
        """Everything the user should read in full (a toast would cut it off)."""
        lines = list(self.notes)
        for path, reason in self.trash_failures:
            lines.append(f"{display_path(path)}: {reason}")
        return lines


def uninstall_and_trash(app_id: str, scope: Scope, data: Sequence[Path] = (), *,
                        keep_permission: bool = False) -> UninstallOutcome:
    """Worker thread: uninstall the app, then move the chosen data folders to the trash.

    The data is only touched once the app is gone: a failed or cancelled uninstall raises
    before anything was moved.
    """
    outcome = UninstallOutcome(notes=list(uninstall(app_id, Scope(scope),
                                                    keep_permission=keep_permission) or []))
    if data:
        for path, error in appdata.move_to_trash([Path(p) for p in data]):
            if error is None:
                outcome.trashed.append(path)
            else:
                outcome.trash_failures.append((path, error))
    return outcome


def done_text(app: InstalledApp, was_broken: bool) -> str:
    return (_("{name} was removed") if was_broken else _("{name} was uninstalled")).format(
        name=app.name)


def result_toast_text(app: InstalledApp, outcome: UninstallOutcome, was_broken: bool) -> str:
    if outcome.trashed:
        return _("{name} was uninstalled, its settings and data are in the trash").format(
            name=app.name)
    return done_text(app, was_broken)


# ------------------------------------------------------------------------------------------------
# dialogs
# ------------------------------------------------------------------------------------------------


class UninstallDialog(Adw.AlertDialog):
    """"Uninstall <Name>?" with Cancel / Uninstall. The app's settings and data folders are
    looked for in the background and listed as check rows - none of them ticked: only what the
    user ticks is moved to the trash (``selected_data``)."""

    __gtype_name__ = "EasyInstallerUninstallDialog"
    __gsignals__ = {
        # the search for settings and data folders finished (tests, screenshots)
        "data-ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: InstalledApp, *, others: Iterable[InstalledApp] | None = None,
                 find_data: bool = True):
        self.app = app
        self.was_broken = is_broken(app)
        if others is None:
            try:
                others = other_installations(app)
            except Exception:  # an unreadable registry must not prevent uninstalling
                log.warning("cannot look up the other installations of %s", app.id,
                            exc_info=True)
                others = []
        self._others = list(others)
        super().__init__(heading=uninstall_heading(app),
                         body=uninstall_body(app, self._others))
        self.add_response(RESPONSE_CANCEL, _("_Cancel"))
        self.add_response(RESPONSE_UNINSTALL,
                          _("_Remove") if self.was_broken else _("_Uninstall"))
        self.set_response_appearance(RESPONSE_UNINSTALL, Adw.ResponseAppearance.DESTRUCTIVE)
        self.set_default_response(RESPONSE_CANCEL)
        self.set_close_response(RESPONSE_CANCEL)

        self.locations: list[DataLocation] = []
        self._checks: list[tuple[Gtk.CheckButton, DataLocation]] = []
        self._closed = False
        self.data_ready = False
        self.connect("closed", self._on_closed)

        self._extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._searching = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True)
        self._searching.append(spinner)
        searching_label = Gtk.Label(label=_("Looking for its settings and data…"), wrap=True)
        searching_label.add_css_class("dim-label")
        self._searching.append(searching_label)
        self._extra.append(self._searching)
        self.set_extra_child(self._extra)

        if find_data and not self._others:
            run_in_thread(removable_data, self._on_data_found, app)
        else:
            # Another installation still uses the settings: nothing to offer (§24).
            self._on_data_found([], None)

    # -- data rows --------------------------------------------------------------------------------

    def _on_closed(self, *_args: object) -> None:
        self._closed = True

    def _on_data_found(self, locations: list[DataLocation] | None,
                       error: BaseException | None) -> None:
        if self._closed:
            return
        if error is not None:
            log.warning("cannot look for the data of %s: %s", self.app.id, error)
        self.show_data(locations or [])

    def show_data(self, locations: Sequence[DataLocation]) -> None:
        """List ``locations`` as unticked check rows (or hide the area if there are none)."""
        self.data_ready = True
        self.locations = list(locations)
        self._extra.remove(self._searching)
        if not self.locations:
            self._extra.set_visible(False)
            self.emit("data-ready")
            return
        self.set_body(uninstall_body(self.app, self._others, offers_data=True))
        title = Gtk.Label(label=_("Also move settings and data to the trash:"), xalign=0,
                          wrap=True)
        title.add_css_class("heading")
        self._extra.append(title)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        for location in self.locations:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            row = Adw.ActionRow(title=display_path(location.path),
                                subtitle=location_subtitle(location), use_markup=False,
                                title_lines=1, activatable_widget=check,
                                tooltip_text=display_path(location.path))
            row.add_prefix(check)
            rows.append(row)
            self._checks.append((check, location))
        self._extra.append(rows)
        hint = Gtk.Label(label=_("Only what you tick is moved. You can get it back from the "
                                 "trash."),
                         xalign=0, wrap=True)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        self._extra.append(hint)
        self.emit("data-ready")

    def set_data_selected(self, path: Path, selected: bool = True) -> None:
        for check, location in self._checks:
            if location.path == Path(path):
                check.set_active(selected)

    @property
    def selected_data(self) -> list[Path]:
        return [location.path for check, location in self._checks if check.get_active()]


def keep_permission_dialog(app: InstalledApp, error: BaseException) -> Adw.AlertDialog:
    """The password prompt was refused (e.g. a standard user): uninstall all the same?"""
    dialog = Adw.AlertDialog(
        heading=_("Uninstall {name} without the password?").format(name=app.name),
        body=str(error) + "\n\n" + _("{name} can still be uninstalled. Only the special "
                                      "permission it needed then stays on this computer, "
                                      "because only an administrator can remove it.").format(
                                          name=app.name))
    dialog.add_response(RESPONSE_CANCEL, _("_Cancel"))
    dialog.add_response(RESPONSE_UNINSTALL, _("_Uninstall"))
    dialog.set_response_appearance(RESPONSE_UNINSTALL, Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response(RESPONSE_CANCEL)
    dialog.set_close_response(RESPONSE_CANCEL)
    return dialog


def notes_dialog(heading: str, messages: Sequence[str]) -> Adw.AlertDialog:
    """A finished uninstall with something to say (a toast would cut the notes off)."""
    dialog = Adw.AlertDialog(heading=heading, body="\n\n".join(messages))
    dialog.add_response("close", _("_Close"))
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    return dialog
