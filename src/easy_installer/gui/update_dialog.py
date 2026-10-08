"""The update dialog: confirm → download (can be cancelled) → install → done (or error).

``UpdateDialog(window, app, update, *, autostart=False, on_done=None)`` presents itself on
``window`` (calling ``present()`` once more is harmless) and replaces the installed ``app`` by
the ``update`` found by ``core.updater`` with :func:`core.updater.apply_update`, which runs in a
worker thread. Widgets are only ever changed on the main loop.

* ``autostart``: skip the confirmation page and start downloading right away ("Update all").
* ``on_done(new_app_or_None)`` is called exactly once, after the dialog was closed and nothing
  runs any more: with the new registry entry if the update was installed, else None. If the
  dialog is closed while the update is being installed, that is when the installation ended.

Cancel (or Escape / Ctrl+W) while downloading stops the download; once the download is
complete the update runs to its end and the dialog cannot be closed. A refused password prompt
(system-wide apps are updated with the administrator helper) leads back to the confirmation
page with a toast, and so does an update that is not signed by the installed version's maker
(the button then reads "Update Anyway").
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.installer import find_installed
from ..core.paths import Scope
from ..core.registry import InstalledApp
from ..core.settings import load_settings
from ..core.updater import (
    SignerChangedError,
    UpdateNotNeededError,
    apply_update,
    update_source_of,
)
from ..core.updates import AvailableUpdate
from ..errors import AuthorizationError, EasyInstallerError, NotInstalledError, UpdateCancelled
from ..i18n import _
from .async_utils import MainLoopProgress, main_loop_progress, run_in_thread
from .common import app_icon_image, app_icon_paintable, escape, launch_desktop_file, toast_text
from .dialog_common import (
    ICON_BACKUP,
    ICON_DOWNLOAD,
    ICON_PASSWORD,
    ICON_SOURCE,
    Fact,
    FactList,
    backup_note,
    describe_update_error,
    download_text,
    ensure_css,
    pill,
    progress_share,
    version_change_label,
    version_change_text,
    warning_row,
)

log = logging.getLogger(__name__)

CONTENT_WIDTH = 460
#: Fixed height (like the install dialog), so the dialog does not jump between its pages.
CONTENT_HEIGHT = 560
ICON_SIZE = 96
PROGRESS_ICON_SIZE = 64
DONE_ICON_SIZE = 128

PAGE_CONFIRM = "confirm"
PAGE_DOWNLOAD = "download"
PAGE_INSTALL = "install"
PAGE_DONE = "done"
PAGE_ERROR = "error"

#: ``apply_update`` reports 0-0.7 for the download, then checking the file and installing it
#: (DESIGN section 22). Once the download is complete the update runs to its end.
DOWNLOAD_END = 0.7
#: How often and how far apart closing after Cancel is tried again (see ``_close_now``).
CLOSE_RETRY_MS = 200
CLOSE_RETRIES = 50

OnDone = Callable[[InstalledApp | None], None]


def _run_update(app: InstalledApp, update: AvailableUpdate, progress: MainLoopProgress,
                cancel: threading.Event, allow_signer_change: bool,
                notes: list[str] | None = None) -> InstalledApp:
    """Worker thread: download and install (``apply_update`` is looked up at call time, so
    tests and the screenshot tool can replace it). ``notes`` gets what the user should read
    afterwards (e.g. the previous version could not be kept); it is read on the main loop
    only once the worker has ended."""
    if notes is None:
        return apply_update(app, update, progress=progress, cancel=cancel,
                            allow_signer_change=allow_signer_change)
    return apply_update(app, update, progress=progress, cancel=cancel,
                        allow_signer_change=allow_signer_change, warnings=notes)


class UpdateDialog(Adw.Dialog):
    """Update one installed app to ``update`` (see the module documentation)."""

    __gtype_name__ = "EasyInstallerUpdateDialog"
    __gsignals__ = {
        # Emitted on the main loop with the new InstalledApp once the update is installed.
        "updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, window: Gtk.Widget | None, app: InstalledApp, update: AvailableUpdate, *,
                 autostart: bool = False, on_done: OnDone | None = None):
        super().__init__(title=_("Update App"), content_width=CONTENT_WIDTH,
                         content_height=CONTENT_HEIGHT)
        ensure_css(window)
        self.app = app
        self.update = update
        #: the new registry entry once the update is installed
        self.new_app: InstalledApp | None = None
        self._window = window
        self._on_done = on_done
        self._done_called = False
        self._presented = False
        self._closed = False
        self._running = False
        self._cancel = threading.Event()
        self._cancelling = False
        self._cancelled = False
        self._allow_signer_change = False
        self._signer_warning: str | None = None
        self._progress: MainLoopProgress | None = None
        #: notes of the last run of the update (shown on the done page)
        self._notes: list[str] = []
        self._pulse_source = 0
        self._close_retries = 0
        self._page = ""
        self._backup_days = load_settings().backup_days

        self._toasts = Adw.ToastOverlay()
        self._toolbar = Adw.ToolbarView()
        self._header = Adw.HeaderBar(show_title=False)
        self._toolbar.add_top_bar(self._header)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                hhomogeneous=False, vhomogeneous=False, interpolate_size=False)
        self._stack.add_named(self._build_confirm_page(), PAGE_CONFIRM)
        self._stack.add_named(self._build_download_page(), PAGE_DOWNLOAD)
        self._stack.add_named(self._build_install_page(), PAGE_INSTALL)
        self._stack.add_named(self._build_done_page(), PAGE_DONE)
        self._stack.add_named(self._build_error_page(), PAGE_ERROR)
        # Toasts show above the Cancel/Update bar, never on top of its buttons.
        self._toasts.set_child(self._stack)
        self._toolbar.set_content(self._toasts)
        self._toolbar.add_bottom_bar(self._build_action_bar())
        self.set_child(self._toolbar)

        self.connect("close-attempt", self._on_close_attempt)
        self.connect("closed", self._on_closed)
        self._fill_confirm()
        self._show_page(PAGE_CONFIRM)
        if window is not None:
            self.present(window)
        if autostart:
            self.start_update()

    # ------------------------------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------------------------------

    @property
    def page(self) -> str:
        return self._page

    @property
    def busy(self) -> bool:
        """The update is being downloaded or installed."""
        return self._running

    @property
    def cancelling(self) -> bool:
        return self._cancelling

    @property
    def cancelled(self) -> bool:
        """The person stopped it: Cancel while downloading, or the dialog was closed before
        the update ran (to its end or to an error)."""
        return self._cancelled

    def present(self, parent: Gtk.Widget | None = None) -> None:
        """Show the dialog on ``parent`` (default: its window). The constructor already
        presents it when it got a window, so calling this again is harmless."""
        if self._presented or self._closed:
            return
        self._presented = True
        Adw.Dialog.present(self, parent if parent is not None else self._window)

    def start_update(self) -> None:
        """Download and install the update (from the confirmation page)."""
        if self._running or self._closed or self._page not in (PAGE_CONFIRM, PAGE_ERROR, ""):
            return
        self._running = True
        self._cancelling = False
        self._cancel = threading.Event()
        self.set_can_close(False)
        name = self._name
        version = self.update.version
        self._download_title.set_label(
            _("Downloading {name} {version}…").format(name=name, version=version) if version
            else _("Downloading the update of {name}…").format(name=name))
        self._install_title.set_label(
            _("Installing {name} {version}…").format(name=name, version=version) if version
            else _("Installing the update of {name}…").format(name=name))
        self._download_bar.set_fraction(0.0)
        self._install_bar.set_fraction(0.0)
        self._download_label.set_label(_("Starting the download…"))
        self._install_label.set_label(_("Checking the download…"))
        self._cancel_button.set_sensitive(True)
        self._show_page(PAGE_DOWNLOAD)
        self._progress = main_loop_progress(self._on_progress)
        self._notes = []
        run_in_thread(_run_update, self._on_finished, self.app, self.update, self._progress,
                      self._cancel, self._allow_signer_change, self._notes)

    def cancel_update(self) -> None:
        """Stop the download; the dialog closes once it has stopped. Ignored while installing."""
        if not self._running or self._page != PAGE_DOWNLOAD or self._cancelling:
            return
        self._cancelling = True
        self._cancelled = True
        self._cancel.set()
        self._cancel_button.set_sensitive(False)
        self._download_label.set_label(_("Cancelling…"))

    def add_toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=toast_text(text)))

    # ------------------------------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------------------------------

    @property
    def _name(self) -> str:
        return self.app.name

    def _build_confirm_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=0,
                      margin_bottom=18, margin_start=18, margin_end=18)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_bottom=6)
        header.add_css_class("install-header")
        self._icon_slot = Gtk.Box(halign=Gtk.Align.CENTER, margin_bottom=6)
        header.append(self._icon_slot)
        self._name_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                     max_width_chars=28)
        self._name_label.add_css_class("title-1")
        header.append(self._name_label)
        self._versions_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                         justify=Gtk.Justification.CENTER)
        self._versions_label.add_css_class("title-4")
        self._versions_label.add_css_class("version-change")
        header.append(self._versions_label)
        box.append(header)

        # Download size, where it comes from, the kept version, the password.
        self._facts = FactList()
        box.append(self._facts)

        self._warnings_group = Adw.PreferencesGroup(visible=False)
        box.append(self._warnings_group)
        self._warning_row: Adw.ActionRow | None = None

        self._notes_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="adw-external-link-symbolic",
                                    label=_("_What’s New"), use_underline=True),
            halign=Gtk.Align.CENTER, visible=False,
            tooltip_text=_("Read about this version on the maker’s website"))
        self._notes_button.add_css_class("flat")
        self._notes_button.connect("clicked", self._on_notes_clicked)
        box.append(self._notes_button)

        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  propagate_natural_height=True, child=box)

    def _build_action_bar(self) -> Gtk.Widget:
        buttons = Gtk.Box(spacing=12, homogeneous=True, margin_top=12, margin_bottom=12,
                          margin_start=18, margin_end=18)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True, can_shrink=True)
        cancel.connect("clicked", lambda *_a: self.close())
        self._update_button = Gtk.Button(label=_("_Update"), use_underline=True, can_shrink=True)
        self._update_button.add_css_class("suggested-action")
        self._update_button.connect("clicked", lambda *_a: self.start_update())
        buttons.append(cancel)
        buttons.append(self._update_button)
        return buttons

    def _progress_box(self) -> tuple[Gtk.Box, Gtk.Label, Gtk.ProgressBar, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                      margin_top=36, margin_bottom=48, margin_start=36, margin_end=36)
        icon_slot = Gtk.Box(halign=Gtk.Align.CENTER, margin_bottom=12)
        icon_slot.append(app_icon_image(self.app.icon_path, PROGRESS_ICON_SIZE))
        box.append(icon_slot)
        title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        title.add_css_class("title-2")
        box.append(title)
        bar = Gtk.ProgressBar(margin_top=12, show_text=True)
        box.append(bar)
        label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        label.add_css_class("dim-label")
        label.add_css_class("numeric")
        box.append(label)
        return box, title, bar, label

    def _build_download_page(self) -> Gtk.Widget:
        box, self._download_title, self._download_bar, self._download_label = \
            self._progress_box()
        self._cancel_button = pill(_("_Cancel"))
        self._cancel_button.set_halign(Gtk.Align.CENTER)
        self._cancel_button.set_margin_top(18)
        self._cancel_button.connect("clicked", lambda *_a: self.cancel_update())
        box.append(self._cancel_button)
        return box

    def _build_install_page(self) -> Gtk.Widget:
        box, self._install_title, self._install_bar, self._install_label = self._progress_box()
        # The bar shows the steps' own texts; the percentage would only jump.
        self._install_bar.set_show_text(False)
        note = Gtk.Label(label=_("This takes only a moment. Please keep Easy Installer open."),
                         wrap=True, justify=Gtk.Justification.CENTER, margin_top=18)
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        box.append(note)
        return box

    def _build_done_page(self) -> Gtk.Widget:
        self._done_page = Adw.StatusPage()
        self._done_page.add_css_class("compact")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        # What came up while updating (e.g. the previous version could not be kept).
        self._done_notes = Adw.PreferencesGroup(visible=False)
        self._done_note_rows: list[Adw.ActionRow] = []
        box.append(self._done_notes)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, homogeneous=True)
        self._done_button = pill(_("_Done"))
        self._done_button.connect("clicked", lambda *_a: self.close())
        self._open_button = pill(_("_Open App"), suggested=True)
        self._open_button.connect("clicked", self._on_open_clicked)
        buttons.append(self._done_button)
        buttons.append(self._open_button)
        box.append(buttons)
        self._done_page.set_child(box)
        return self._done_page

    def _build_error_page(self) -> Gtk.Widget:
        self._error_page = Adw.StatusPage(icon_name="dialog-error-symbolic")
        self._error_page.add_css_class("compact")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._error_note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._error_note.add_css_class("dim-label")
        box.append(self._error_note)
        self._details_expander = Gtk.Expander(label=_("Technical details"))
        self._details_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                        selectable=True)
        self._details_label.add_css_class("monospace")
        self._details_label.add_css_class("caption")
        self._details_label.add_css_class("details-text")
        details_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            max_content_height=160,
                                            propagate_natural_height=True,
                                            child=self._details_label, margin_top=6)
        details_scroll.add_css_class("card")
        self._details_expander.set_child(details_scroll)
        box.append(self._details_expander)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, homogeneous=True)
        self._retry_button = pill(_("_Try Again"))
        self._retry_button.connect("clicked", self._on_retry_clicked)
        self._close_button = pill(_("_Close"))
        self._close_button.connect("clicked", lambda *_a: self.close())
        buttons.append(self._retry_button)
        buttons.append(self._close_button)
        box.append(buttons)
        self._error_page.set_child(box)
        return self._error_page

    def _show_page(self, name: str) -> None:
        self._page = name
        self._stack.set_visible_child_name(name)
        self._toolbar.set_reveal_bottom_bars(name == PAGE_CONFIRM)
        # While installing nothing can be closed (while downloading, closing = Cancel).
        self._header.set_show_end_title_buttons(name != PAGE_INSTALL)
        focus = {
            PAGE_CONFIRM: self._update_button,
            PAGE_DOWNLOAD: self._cancel_button,
            PAGE_DONE: self._open_button,
            PAGE_ERROR: self._close_button,
        }.get(name)
        if focus is not None:
            self.set_focus(focus)

    # ------------------------------------------------------------------------------------------
    # confirmation page
    # ------------------------------------------------------------------------------------------

    def _facts_for_confirm(self) -> list[Fact]:
        facts: list[Fact] = []
        if self.update.size:
            facts.append(Fact(ICON_DOWNLOAD, _("{size} to download").format(
                size=GLib.format_size(self.update.size))))
        source = update_source_of(self.app)
        where = source.describe() if source is not None else ""
        if where:
            facts.append(Fact(ICON_SOURCE, _("From {source}").format(source=where)))
        if self._backup_days > 0:
            facts.append(Fact(ICON_BACKUP, backup_note(self.app.version, self._backup_days)))
        if Scope(self.app.scope) is Scope.SYSTEM:
            facts.append(Fact(ICON_PASSWORD, _("Asks for your password, because the app is "
                                               "installed for everyone on this computer")))
        return facts

    def _fill_confirm(self) -> None:
        self._icon_slot.append(app_icon_image(self.app.icon_path, ICON_SIZE))
        self._name_label.set_label(self._name)
        self._versions_label.set_label(version_change_text(self.app.version, self.update.version))
        self._versions_label.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [version_change_label(self.app.version, self.update.version)])
        self._facts.set_facts(self._facts_for_confirm())
        self._notes_button.set_visible(bool(self.update.release_url))
        self._refresh_warning()

    def _refresh_warning(self) -> None:
        if self._warning_row is not None:
            self._warnings_group.remove(self._warning_row)
            self._warning_row = None
        if self._signer_warning:
            self._warning_row = warning_row(self._signer_warning, serious=True)
            self._warnings_group.add(self._warning_row)
        self._warnings_group.set_visible(self._warning_row is not None)
        risky = self._allow_signer_change
        self._update_button.set_label(_("_Update Anyway") if risky else _("_Update"))
        self._update_button.remove_css_class("destructive-action" if not risky
                                             else "suggested-action")
        self._update_button.add_css_class("destructive-action" if risky else "suggested-action")

    def _on_notes_clicked(self, *_args: object) -> None:
        url = self.update.release_url
        if not url:
            return
        launcher = Gtk.UriLauncher.new(url)
        root = self.get_root()
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None,
                        self._on_notes_opened)

    def _on_notes_opened(self, launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                log.warning("cannot open %s: %s", launcher.get_uri(), exc)
                self.add_toast(_("The website could not be opened"))

    # ------------------------------------------------------------------------------------------
    # downloading and installing
    # ------------------------------------------------------------------------------------------

    def _on_progress(self, fraction: float | None, message: str) -> None:
        if self._closed:
            return
        if self._page == PAGE_DOWNLOAD and fraction is not None and fraction >= DOWNLOAD_END:
            # The download is complete: from now on the update runs to its end.
            self._show_page(PAGE_INSTALL)
        if self._page == PAGE_DOWNLOAD:
            share = progress_share(fraction, 0.0, DOWNLOAD_END)
            self._set_fraction(self._download_bar, share)
            if not self._cancelling:
                self._download_label.set_label(
                    download_text(share, self.update.size) or message or "")
        elif self._page == PAGE_INSTALL:
            self._set_fraction(self._install_bar, progress_share(fraction, DOWNLOAD_END, 1.0))
            if message:
                self._install_label.set_label(message)

    def _set_fraction(self, bar: Gtk.ProgressBar, fraction: float | None) -> None:
        if fraction is None:
            if not self._pulse_source:
                self._pulse_source = GLib.timeout_add(120, self._pulse)
            return
        self._stop_pulse()
        bar.set_fraction(fraction)

    def _pulse(self) -> bool:
        bar = self._install_bar if self._page == PAGE_INSTALL else self._download_bar
        bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _stop_pulse(self) -> None:
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def _on_finished(self, app: InstalledApp | None, error: BaseException | None) -> None:
        self._running = False
        self._stop_pulse()
        if self._progress is not None:
            self._progress.close()
        self.set_can_close(True)
        if error is None:
            self.new_app = app
        if self._closed:  # force-closed meanwhile (e.g. the app quit): only report the result
            self._report_done()
            return
        if error is None:
            assert app is not None
            self._show_done(app)
            self.emit("updated", app)
            return
        if isinstance(error, UpdateCancelled) or self._cancelling:
            # Whatever stopped the download after Cancel was pressed: the answer is "cancelled".
            log.info("update of %s cancelled: %s", self.app.id, error)
            self._close_now()
            return
        if isinstance(error, UpdateNotNeededError):
            # It updated itself (or was updated otherwise) meanwhile: nothing to do any more.
            log.info("update of %s is no longer needed: %s", self.app.id, error.details or error)
            self._show_not_needed(error)
            return
        if isinstance(error, SignerChangedError):
            log.warning("update of %s is signed by someone else: %s", self.app.id,
                        error.details or error)
            self._signer_warning = _(
                "Careful: the installed version of {name} was signed by its maker, but this "
                "update is not signed by the same maker. It may come from someone else. Only go "
                "on if you trust where it comes from.").format(name=self._name)
            self._allow_signer_change = True
            self._refresh_warning()
            self._show_page(PAGE_CONFIRM)
            return
        if isinstance(error, AuthorizationError):
            log.info("authorization failed: %s", error.details or error)
            self._show_page(PAGE_CONFIRM)
            # "Authentication was cancelled" (or: the password was not accepted).
            self.add_toast(str(error))
            return
        log.warning("updating %s failed: %s", self.app.id, error,
                    exc_info=None if isinstance(error, EasyInstallerError) else error)
        self._show_error(error)

    def _show_done(self, app: InstalledApp) -> None:
        self._done_page.set_title(_("{name} was updated").format(name=app.name))
        version = app.version or self.update.version
        self._done_page.set_description(escape(
            _("Version {version} is ready to use.").format(version=version) if version
            else _("The new version is ready to use.")))
        self._done_page.set_paintable(app_icon_paintable(self, app.icon_path, DONE_ICON_SIZE))
        for row in self._done_note_rows:
            self._done_notes.remove(row)
        self._done_note_rows = [warning_row(note) for note in self._notes]
        for row in self._done_note_rows:
            self._done_notes.add(row)
        self._done_notes.set_visible(bool(self._done_note_rows))
        self._open_button.set_visible(os.path.isfile(app.desktop_path))
        self._show_page(PAGE_DONE)

    def _show_not_needed(self, error: UpdateNotNeededError) -> None:
        app = self._installed_now() or self.app
        self._done_page.set_title(_("{name} is up to date").format(name=app.name))
        self._done_page.set_description(escape(str(error)))
        self._done_page.set_paintable(app_icon_paintable(self, app.icon_path, DONE_ICON_SIZE))
        for row in self._done_note_rows:
            self._done_notes.remove(row)
        self._done_note_rows = []
        self._done_notes.set_visible(False)
        self._open_button.set_visible(False)
        self._show_page(PAGE_DONE)
        self.set_focus(self._done_button)

    def _installed_now(self) -> InstalledApp | None:
        """The registry's entry of the app now; None if it is no longer installed."""
        try:
            return next((entry for entry in find_installed(self.app.id)
                         if Scope(entry.scope) is Scope(self.app.scope)), None)
        except Exception:  # noqa: BLE001 - an unreadable registry: assume it is still there
            log.warning("cannot look up %s", self.app.id, exc_info=True)
            return self.app

    def _on_open_clicked(self, *_args: object) -> None:
        app = self.new_app
        if app is None:
            return
        try:
            launch_desktop_file(app.desktop_path, self)
        except (GLib.Error, ValueError) as exc:
            log.warning("cannot start %s: %s", app.id, exc)
            self.add_toast(_("{name} could not be started").format(name=app.name))
            return
        self.close()

    # ------------------------------------------------------------------------------------------
    # errors
    # ------------------------------------------------------------------------------------------

    def _show_error(self, error: BaseException) -> None:
        friendly = describe_update_error(error)
        self._error_page.set_title(friendly.title)
        self._error_page.set_description(escape(friendly.message))
        gone = isinstance(error, NotInstalledError)
        if not gone and self._installed_now() is None:
            # e.g. uninstalled (from its menu entry) while the update was downloaded
            gone = True
            self._error_page.set_description(escape(
                _("{name} is no longer installed, so it was not updated.").format(
                    name=self._name)))
        if gone:
            self._error_note.set_label("")
        elif self.app.version:
            self._error_note.set_label(_("{name} {version} is still installed and works as "
                                         "before.").format(name=self._name,
                                                           version=self.app.version))
        else:
            self._error_note.set_label(_("{name} is still installed and works as before.").format(
                name=self._name))
        self._error_note.set_visible(not gone)
        self._details_label.set_label(friendly.details or "")
        self._details_expander.set_visible(bool(friendly.details))
        self._details_expander.set_expanded(False)
        self._retry_button.set_visible(not gone)
        self._show_page(PAGE_ERROR)

    def _on_retry_clicked(self, *_args: object) -> None:
        if self._running or self._closed:
            return
        self._show_page(PAGE_CONFIRM)

    # ------------------------------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------------------------------

    def _close_now(self) -> None:
        """Close after a cancelled download. libadwaita 1.5 ignores a close request that comes
        before a freshly presented dialog was painted, so it is repeated until it happened."""
        self._close_retries = 0
        self.force_close()
        if not self._closed:
            GLib.timeout_add(CLOSE_RETRY_MS, self._retry_close)

    def _retry_close(self) -> bool:
        if self._closed or self._close_retries >= CLOSE_RETRIES:
            return GLib.SOURCE_REMOVE
        self._close_retries += 1
        self.force_close()
        return GLib.SOURCE_CONTINUE

    def _on_close_attempt(self, *_args: object) -> None:
        if self._page == PAGE_DOWNLOAD:
            self.cancel_update()   # Escape / Ctrl+W while downloading = Cancel
        elif self._running:
            self.add_toast(_("Please wait until the update is finished"))

    def _on_closed(self, *_args: object) -> None:
        if self._page == PAGE_CONFIRM or self._cancelling:
            self._cancelled = True   # closed before the update ran: the person said no
        self._closed = True
        self._cancel.set()   # a download still running stops; an installation runs to its end
        if not self._running:
            self._stop_pulse()
            if self._progress is not None:
                self._progress.close()
        self._report_done()

    def _report_done(self) -> None:
        """``on_done`` once: after the dialog was closed and the worker has ended."""
        if self._done_called or not self._closed or self._running:
            return
        self._done_called = True
        if self._on_done is None:
            return
        try:
            self._on_done(self.new_app)
        except Exception:  # noqa: BLE001 - the caller's bug must not break the main loop
            log.exception("on_done callback of the update dialog failed")
