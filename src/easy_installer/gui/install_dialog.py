"""The install dialog: read the file → confirm → progress → done (or error).

The file is an AppImage or (per-user only) a portable app archive (``.tar.gz``, ``.zip``, …).
All blocking work (inspection, installation) runs in worker threads; the dialog only ever changes
widgets on the main loop. The dialog owns the inspection result (``AppImageInfo`` or
``PortableInfo``) it creates and deletes its temporary files when it is closed.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from ..core.inspector import AppImageInfo, inspect_appimage
from ..core.installer import (
    ACTION_DOWNGRADE,
    ACTION_INSTALL,
    ACTION_REINSTALL,
    ACTION_UPDATE,
    InstallOptions,
    InstallPlan,
    display_name,
    execute_install,
    find_installed,
    installed_program,
    plan_install,
)
from ..core.integration import compare_versions, variant_name
from ..core.paths import Scope
from ..core.portable import PortableInfo, inspect_portable, is_portable_archive
from ..core.registry import KIND_PORTABLE, InstalledApp
from ..core.sandbox import SandboxFix, needs_sandbox_fix
from ..core.settings import load_settings
from ..core.system_checks import SystemStatus, get_system_status
from ..errors import AuthorizationError, EasyInstallerError
from ..i18n import _
from .async_utils import MainLoopProgress, main_loop_progress, run_in_thread
from .common import (
    app_icon_image,
    app_icon_paintable,
    describe_error,
    escape,
    launch_desktop_file,
    toast_text,
)
from .dialog_common import (
    ICON_BACKUP,
    Fact,
    FactList,
    backup_note,
    ensure_css,
    file_facts,
    pill,
    program_choices,
    signature_is_broken,
    warning_row,
)

log = logging.getLogger(__name__)

CONTENT_WIDTH = 460
#: Fixed height, so the dialog does not jump between the pages (libadwaita 1.5 dialogs keep the
#: size they had when presented unless ``follows-content-size`` is set). Adw shrinks it to fit
#: small windows; the confirmation page scrolls then.
CONTENT_HEIGHT = 620
ICON_SIZE = 96
DONE_ICON_SIZE = 128

PAGE_LOADING = "loading"
PAGE_CONFIRM = "confirm"
PAGE_PROGRESS = "progress"
PAGE_DONE = "done"
PAGE_ERROR = "error"

#: Order of the entries in the sandbox combo row.
SANDBOX_CHOICES: tuple[SandboxFix | None, ...] = (None, SandboxFix.APPARMOR, SandboxFix.NO_SANDBOX)

AnyInfo = AppImageInfo | PortableInfo


class _Cancelled(Exception):
    """Raised inside the worker when the dialog was closed while the file was being read."""


def _folder_text(path: Path, home: str, elsewhere: str) -> str:
    folder = path.parent
    if folder == Path(GLib.get_home_dir()):
        return home
    name = GLib.filename_display_basename(str(folder)) or str(folder)
    return elsewhere.format(folder=name)


def _keep_original_subtitle(path: Path) -> str:
    """Where the original file stays when "Keep the original file" is on (a full sentence)."""
    return _folder_text(path, _("Leave a copy in your Home folder"),
                        _("Leave a copy in the folder “{folder}”"))


def _keep_archive_subtitle(path: Path) -> str:
    """Where the archive stays when "Keep the archive" is on (a full sentence)."""
    return _folder_text(path, _("Leave it in your Home folder"),
                        _("Leave it in the folder “{folder}”"))


def _default_scope(app_id: str, status: SystemStatus) -> Scope:
    """Update an app where it already is; new apps go to the user by default."""
    installed = find_installed(app_id)
    if len(installed) == 1 and (installed[0].scope is Scope.USER or status.pkexec):
        return Scope(installed[0].scope)
    return Scope.USER


class InstallDialog(Adw.Dialog):
    """Install one AppImage or portable app archive. Call :meth:`start` after presenting it."""

    __gtype_name__ = "EasyInstallerInstallDialog"
    __gsignals__ = {
        # Emitted on the main loop with the InstalledApp once installation succeeded.
        "installed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, path: str | os.PathLike):
        super().__init__(title=_("Install App"), content_width=CONTENT_WIDTH,
                         content_height=CONTENT_HEIGHT)
        ensure_css()
        self.path = Path(path)
        self.info: AnyInfo | None = None
        self.status: SystemStatus | None = None
        self.plan: InstallPlan | None = None
        self.installed_app: InstalledApp | None = None

        self._page = ""
        self._closed = False
        self._installing = False
        self._updating_widgets = False
        self._cancel = threading.Event()
        self._progress: MainLoopProgress | None = None
        self._pulse_source = 0
        self._warning_rows: list[Adw.ActionRow] = []
        self._done_warning_rows: list[Adw.ActionRow] = []
        self._warnings_before_install: list[str] = []
        self._can_retry = False
        self._backup_days = 0
        self._programs: list[str] = []

        self._toasts = Adw.ToastOverlay()
        self._toolbar = Adw.ToolbarView()
        self._header = Adw.HeaderBar(show_title=False)
        self._toolbar.add_top_bar(self._header)
        # Not homogeneous in either direction: the (wrapping) titles of hidden pages must not make
        # the dialog wider than the window (a hidden page's width is measured for one line).
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                hhomogeneous=False, vhomogeneous=False, interpolate_size=False)
        self._stack.add_named(self._build_loading_page(), PAGE_LOADING)
        self._stack.add_named(self._build_confirm_page(), PAGE_CONFIRM)
        self._stack.add_named(self._build_progress_page(), PAGE_PROGRESS)
        self._stack.add_named(self._build_done_page(), PAGE_DONE)
        self._stack.add_named(self._build_error_page(), PAGE_ERROR)
        # Toasts show above the Cancel/Install bar, never on top of its buttons.
        self._toasts.set_child(self._stack)
        self._toolbar.set_content(self._toasts)
        self._toolbar.add_bottom_bar(self._build_action_bar())
        self._toolbar.set_reveal_bottom_bars(False)
        self.set_child(self._toolbar)

        self.connect("close-attempt", self._on_close_attempt)
        self.connect("closed", self._on_closed)
        self._show_page(PAGE_LOADING)

    # ------------------------------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------------------------------

    @property
    def page(self) -> str:
        return self._page

    @property
    def busy(self) -> bool:
        return self._installing

    @property
    def is_portable(self) -> bool:
        """The file is an app archive (known once it was read)."""
        return isinstance(self.info, PortableInfo)

    @property
    def program_choices(self) -> list[str]:
        """The programs offered in "Program to start" (empty: the row is hidden)."""
        return list(self._programs)

    def start(self) -> None:
        """Read the file in a worker thread, then show the confirmation page."""
        self._show_page(PAGE_LOADING)
        self._loading_file.set_label(display_name(self.path))  # the name may not be UTF-8
        self._progress = main_loop_progress(self._on_loading_progress)
        run_in_thread(self._inspect_worker, self._on_inspected, self.path, self._cancel,
                      self._progress)

    def install(self) -> None:
        """Start installing with the options chosen on the confirmation page."""
        if self._page != PAGE_CONFIRM or self.info is None or self._installing:
            return
        plan = self._replan()
        if plan is None:
            return
        self._installing = True
        self.set_can_close(False)
        self._warnings_before_install = list(plan.warnings)
        name = self._installed_name(plan)
        if plan.action == ACTION_UPDATE and not plan.options.keep_both:
            title = _("Updating {name}…").format(name=name)
        else:
            title = _("Installing {name}…").format(name=name)
        self._progress_title.set_label(title)
        self._progress_label.set_label(_("Getting ready…"))
        self._progress_bar.set_fraction(0.0)
        self._show_page(PAGE_PROGRESS)
        self._progress = main_loop_progress(self._on_install_progress)
        run_in_thread(execute_install, self._on_install_finished, plan, progress=self._progress)

    def set_scope(self, scope: Scope) -> None:
        if Scope(scope) is Scope.SYSTEM and self.is_portable:
            return   # archives are only ever installed for the user
        (self._system_check if Scope(scope) is Scope.SYSTEM else self._user_check).set_active(True)

    def set_keep_both(self, keep_both: bool) -> None:
        """Choose "Keep both" (True) or "Replace version X" (False)."""
        (self._keep_both_check if keep_both else self._replace_check).set_active(True)

    def set_program(self, relpath: str) -> None:
        """Choose the program a portable app starts (one of :attr:`program_choices`)."""
        if relpath in self._programs:
            self._program_row.set_selected(self._programs.index(relpath))

    def expand_options(self, expanded: bool = True) -> None:
        self._more_row.set_expanded(expanded)

    def add_toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=toast_text(text)))

    # ------------------------------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------------------------------

    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                      margin_top=48, margin_bottom=64, margin_start=24, margin_end=24)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_bottom=12)
        spinner.set_size_request(32, 32)
        box.append(spinner)
        self._loading_title = Gtk.Label(label=_("Reading app information…"), wrap=True,
                                        justify=Gtk.Justification.CENTER)
        self._loading_title.add_css_class("title-2")
        box.append(self._loading_title)
        self._loading_file = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE,
                                       justify=Gtk.Justification.CENTER)
        self._loading_file.add_css_class("dim-label")
        box.append(self._loading_file)
        self._loading_bar = Gtk.ProgressBar(visible=False, margin_top=12, margin_start=48,
                                            margin_end=48)
        box.append(self._loading_bar)
        return box

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
        self._meta_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._meta_label.add_css_class("dim-label")
        header.append(self._meta_label)
        self._comment_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                        max_width_chars=48)
        self._comment_label.add_css_class("app-comment")
        header.append(self._comment_label)
        box.append(header)

        # Another signer than the installed version's, a broken signature: right at the top.
        self._trust_group = Adw.PreferencesGroup(visible=False)
        self._trust_rows: list[Adw.ActionRow] = []
        box.append(self._trust_group)

        # "Replace version 1.1.3" / "Keep both" - only when both are possible.
        self._choice_group = Adw.PreferencesGroup(visible=False)
        self._replace_check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self._keep_both_check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self._replace_check)
        self._replace_row = Adw.ActionRow(activatable_widget=self._replace_check, use_markup=False)
        self._replace_row.add_prefix(self._replace_check)
        self._keep_both_row = Adw.ActionRow(
            title=_("Keep both"), subtitle=_("Both versions will be in your app menu"),
            activatable_widget=self._keep_both_check, use_markup=False)
        self._keep_both_row.add_prefix(self._keep_both_check)
        self._choice_group.add(self._replace_row)
        self._choice_group.add(self._keep_both_row)
        self._replace_check.set_active(True)
        self._replace_check.connect("toggled", self._on_choice_toggled)
        self._keep_both_check.connect("toggled", self._on_choice_toggled)
        box.append(self._choice_group)

        # "Version 1.1.3 is installed and will be replaced by 1.1.4"
        self._action_group = Adw.PreferencesGroup()
        self._action_row = Adw.ActionRow(use_markup=False, title_lines=0)
        self._action_icon = Gtk.Image(valign=Gtk.Align.CENTER)
        self._action_row.add_prefix(self._action_icon)
        self._action_group.add(self._action_row)
        box.append(self._action_group)

        # "Version 1.1.3 is kept for 14 days …", "Downloaded from …", "Signed with a key …",
        # "Gets updates from …"
        self._facts = FactList()
        box.append(self._facts)

        # Portable archives with several likely programs: which one starts the app?
        self._program_group = Adw.PreferencesGroup(
            description=_("This archive contains several programs. Choose the one that starts "
                          "the app."), visible=False)
        self._program_row = Adw.ComboRow(title=_("Program to start"), use_markup=False,
                                         model=Gtk.StringList.new([]))
        self._program_row.connect("notify::selected", self._on_program_changed)
        self._program_group.add(self._program_row)
        box.append(self._program_group)

        self._scope_group = Adw.PreferencesGroup(title=_("Install for"))
        self._user_check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self._system_check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self._user_check)
        self._user_row = Adw.ActionRow(title=_("Only for me"), subtitle=_("No password needed"),
                                       activatable_widget=self._user_check, use_markup=False)
        self._user_row.add_prefix(self._user_check)
        self._system_row = Adw.ActionRow(title=_("Everyone on this computer"),
                                         subtitle=_("Requires your password"),
                                         activatable_widget=self._system_check, use_markup=False)
        self._system_row.add_prefix(self._system_check)
        self._scope_group.add(self._user_row)
        self._scope_group.add(self._system_row)
        self._user_check.set_active(True)
        # Grouped check buttons: react to the one that becomes active (the other one's
        # "toggled" fires first, while neither is active yet).
        self._user_check.connect("toggled", self._on_scope_toggled)
        self._system_check.connect("toggled", self._on_scope_toggled)
        box.append(self._scope_group)

        self._warnings_group = Adw.PreferencesGroup()
        box.append(self._warnings_group)

        options_group = Adw.PreferencesGroup()
        self._more_row = Adw.ExpanderRow(title=_("More options"), use_markup=False)
        self._keep_row = Adw.SwitchRow(title=_("Keep the original file"), use_markup=False)
        self._keep_row.connect("notify::active", self._on_option_changed)
        self._more_row.add_row(self._keep_row)
        self._sandbox_row = Adw.ComboRow(
            title=_("Security sandbox"), use_subtitle=True, use_markup=False,
            model=Gtk.StringList.new([
                _("Automatic"),
                _("Allow with system permission (recommended)"),
                _("Start without sandbox"),
            ]))
        self._sandbox_row.connect("notify::selected", self._on_option_changed)
        self._more_row.add_row(self._sandbox_row)
        help_label = Gtk.Label(
            label=_("This app protects itself with a security sandbox, which this computer blocks "
                    "for apps it does not know (an AppArmor rule on Ubuntu-based systems). "
                    "“Allow with system permission” asks for your password once and keeps the "
                    "protection. “Start without sandbox” needs no password, but the app runs "
                    "less protected."),
            wrap=True, xalign=0, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        help_label.add_css_class("dim-label")
        help_label.add_css_class("caption")
        self._sandbox_help = Gtk.ListBoxRow(child=help_label, activatable=False, selectable=False)
        self._more_row.add_row(self._sandbox_help)
        options_group.add(self._more_row)
        box.append(options_group)

        self._target_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                       selectable=True, margin_start=6, margin_end=6)
        self._target_label.add_css_class("caption")
        self._target_label.add_css_class("dim-label")
        box.append(self._target_label)

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, child=box)
        return scrolled

    def _build_action_bar(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10,
                        margin_bottom=12, margin_start=18, margin_end=18)
        # Always visible, right above the button that matters.
        trust = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        trust_icon = Gtk.Image(icon_name="security-high-symbolic")
        trust_icon.add_css_class("dim-label")
        trust.append(trust_icon)
        trust_label = Gtk.Label(label=_("Only install apps from sources you trust."), wrap=True)
        trust_label.add_css_class("caption")
        trust_label.add_css_class("dim-label")
        trust.append(trust_label)
        outer.append(trust)

        buttons = Gtk.Box(spacing=12, homogeneous=True)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True, can_shrink=True)
        cancel.connect("clicked", lambda *_a: self.close())
        # can_shrink: "Go Back to <a long version>" ellipsizes instead of widening the dialog.
        self._install_button = Gtk.Button(label=_("_Install"), use_underline=True, can_shrink=True)
        self._install_button.add_css_class("suggested-action")
        self._install_button.connect("clicked", lambda *_a: self.install())
        buttons.append(cancel)
        buttons.append(self._install_button)
        outer.append(buttons)
        return outer

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                      margin_top=36, margin_bottom=64, margin_start=36, margin_end=36)
        self._progress_icon_slot = Gtk.Box(halign=Gtk.Align.CENTER, margin_bottom=12)
        box.append(self._progress_icon_slot)
        self._progress_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._progress_title.add_css_class("title-2")
        box.append(self._progress_title)
        self._progress_bar = Gtk.ProgressBar(margin_top=12)
        box.append(self._progress_bar)
        self._progress_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._progress_label.add_css_class("dim-label")
        box.append(self._progress_label)
        return box

    def _build_done_page(self) -> Gtk.Widget:
        self._done_page = Adw.StatusPage()
        self._done_page.add_css_class("compact")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self._done_warnings = Adw.PreferencesGroup(visible=False)
        box.append(self._done_warnings)
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
        self._error_file = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                     justify=Gtk.Justification.CENTER, selectable=True)
        self._error_file.add_css_class("dim-label")
        self._error_file.add_css_class("caption")
        box.append(self._error_file)
        self._details_expander = Gtk.Expander(label=_("Technical details"))
        self._details_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0,
                                        selectable=True)
        self._details_label.add_css_class("monospace")
        self._details_label.add_css_class("caption")
        self._details_label.add_css_class("details-text")
        details_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            max_content_height=180,
                                            propagate_natural_height=True,
                                            child=self._details_label, margin_top=6)
        details_scroll.add_css_class("card")
        self._details_expander.set_child(details_scroll)
        box.append(self._details_expander)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, homogeneous=True)
        self._back_button = pill(_("_Back"))
        self._back_button.connect("clicked", self._on_back_clicked)
        self._close_button = pill(_("_Close"))
        self._close_button.connect("clicked", lambda *_a: self.close())
        buttons.append(self._back_button)
        buttons.append(self._close_button)
        box.append(buttons)
        self._error_page.set_child(box)
        return self._error_page

    def _show_page(self, name: str) -> None:
        self._page = name
        self._stack.set_visible_child_name(name)
        self._toolbar.set_reveal_bottom_bars(name == PAGE_CONFIRM)
        # Nothing can be closed while installing: no close button that only shows a toast.
        self._header.set_show_end_title_buttons(name != PAGE_PROGRESS)
        focus = {
            PAGE_CONFIRM: self._install_button,
            PAGE_DONE: self._open_button,
            PAGE_ERROR: self._close_button,
        }.get(name)
        if focus is not None:
            self.set_focus(focus)

    # ------------------------------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------------------------------

    @staticmethod
    def _inspect_worker(path: Path, cancel: threading.Event, progress: MainLoopProgress
                        ) -> tuple[AnyInfo, SystemStatus, InstallPlan, int]:
        def report(fraction: float | None, message: str) -> None:
            if cancel.is_set():
                raise _Cancelled()
            progress(fraction, message)

        portable = is_portable_archive(path)
        info: AnyInfo = (inspect_portable(path, progress=report) if portable
                         else inspect_appimage(path, progress=report))
        try:
            if cancel.is_set():
                raise _Cancelled()
            status = get_system_status()
            if portable:
                program = installed_program(info)
                if program:
                    info.executable = program
                options = InstallOptions(scope=Scope.USER)
            else:
                options = InstallOptions(scope=_default_scope(info.app_id, status))
            plan = plan_install(info, options, status=status)
            backup_days = load_settings().backup_days
        except BaseException:
            info.cleanup()
            raise
        return info, status, plan, backup_days

    def _on_loading_progress(self, fraction: float | None, message: str) -> None:
        if self._page != PAGE_LOADING:
            return
        if message:
            self._loading_title.set_label(message)
        # Only big files take long enough (hashing) for a progress bar to be useful.
        if fraction is not None and 0.0 < fraction < 1.0:
            self._loading_bar.set_visible(True)
            self._loading_bar.set_fraction(fraction)

    def _on_inspected(self, result: tuple | None, error: BaseException | None) -> None:
        if self._progress is not None:
            self._progress.close()
        if self._closed:
            if result is not None:
                result[0].cleanup()
            return
        if error is not None:
            if isinstance(error, _Cancelled):
                return
            log.info("cannot install %s: %s", self.path, error)
            self._show_error(error, reading=True)
            return
        self.info, self.status, self.plan, self._backup_days = result
        self._fill_header()
        self._fill_options_from_plan()
        self._refresh_confirm()
        self._show_page(PAGE_CONFIRM)

    # ------------------------------------------------------------------------------------------
    # confirmation page
    # ------------------------------------------------------------------------------------------

    def _fill_header(self) -> None:
        info = self.info
        assert info is not None
        child = self._icon_slot.get_first_child()
        if child is not None:
            self._icon_slot.remove(child)
        self._icon_slot.append(app_icon_image(info.icon_path, ICON_SIZE))
        child = self._progress_icon_slot.get_first_child()
        if child is not None:
            self._progress_icon_slot.remove(child)
        self._progress_icon_slot.append(app_icon_image(info.icon_path, 64))

        self._name_label.set_label(info.display_name)
        # An archive is shown with the space the unpacked app takes.
        size = GLib.format_size(info.tree_size if isinstance(info, PortableInfo) else info.size)
        version = self.plan.version if self.plan is not None else info.version
        if version:
            self._meta_label.set_label(_("Version {version} · {size}").format(
                version=version, size=size))
        else:
            self._meta_label.set_label(size)
        self._comment_label.set_label(info.comment or "")
        self._comment_label.set_visible(bool(info.comment))

        if isinstance(info, PortableInfo):
            self._keep_row.set_title(_("Keep the archive"))
            self._keep_row.set_subtitle(_keep_archive_subtitle(info.path))
            # Unpacked apps are per-user only (the administrator helper never unpacks archives).
            self._system_row.set_sensitive(False)
            self._system_row.set_subtitle(_("Not possible for apps that come as an archive"))
            self._programs = program_choices(info)
            self._updating_widgets = True   # a new model selects its first entry
            try:
                self._program_row.set_model(Gtk.StringList.new(self._programs))
            finally:
                self._updating_widgets = False
            self._program_group.set_visible(bool(self._programs))
            return
        self._keep_row.set_subtitle(_keep_original_subtitle(info.path))
        pkexec_available = bool(self.status and self.status.pkexec)
        self._system_row.set_sensitive(pkexec_available)
        if not pkexec_available:
            self._system_row.set_subtitle(_("Not possible on this computer"))

    def _fill_options_from_plan(self) -> None:
        plan = self.plan
        assert plan is not None
        self._updating_widgets = True
        try:
            self.set_scope(plan.layout.scope)
            self._keep_row.set_active(bool(plan.options.keep_original))
            self._sandbox_row.set_selected(SANDBOX_CHOICES.index(plan.options.sandbox_fix)
                                           if plan.options.sandbox_fix in SANDBOX_CHOICES else 0)
            self.set_keep_both(plan.options.keep_both)
            executable = getattr(self.info, "executable", None)
            if executable in self._programs:
                self._program_row.set_selected(self._programs.index(executable))
        finally:
            self._updating_widgets = False

    def _selected_options(self) -> InstallOptions:
        selected = self._sandbox_row.get_selected()
        sandbox = SANDBOX_CHOICES[selected] if 0 <= selected < len(SANDBOX_CHOICES) else None
        portable = self.is_portable
        return InstallOptions(
            scope=Scope.SYSTEM if self._system_check.get_active() and not portable else Scope.USER,
            keep_original=self._keep_row.get_active(),
            sandbox_fix=None if portable else sandbox,
            keep_both=self._keep_both_check.get_active() and not portable,
        )

    def _replan(self) -> InstallPlan | None:
        """Plan again with the current options (quick: reads the registries, renders text)."""
        assert self.info is not None
        try:
            self.plan = plan_install(self.info, self._selected_options(), status=self.status)
        except Exception as exc:  # e.g. the registry became unreadable
            log.warning("planning failed: %s", exc)
            self._show_error(exc, reading=True)
            return None
        return self.plan

    def _on_scope_toggled(self, button: Gtk.CheckButton) -> None:
        if button.get_active():
            self._on_option_changed()

    def _on_choice_toggled(self, button: Gtk.CheckButton) -> None:
        if button.get_active():
            self._on_option_changed()

    def _on_program_changed(self, *_args: object) -> None:
        if self._updating_widgets or not isinstance(self.info, PortableInfo):
            return
        selected = self._program_row.get_selected()
        if 0 <= selected < len(self._programs):
            self.info.executable = self._programs[selected]
            self._on_option_changed()

    def _on_option_changed(self, *_args: object) -> None:
        if self._updating_widgets or self.info is None or self._page != PAGE_CONFIRM:
            return
        if self._replan() is not None:
            self._refresh_confirm()

    def _installed_name(self, plan: InstallPlan) -> str:
        """The name the app will have: a kept copy's ends in its version ("FreeCAD 1.1.4")."""
        name = self.info.display_name if self.info is not None else plan.name
        return variant_name(name, plan.name_suffix) if plan.name_suffix else name

    @staticmethod
    def _action_text(plan: InstallPlan) -> tuple[str | None, str, str]:
        """(info text, icon name, install button label) for the plan's action."""
        new = plan.version
        old = plan.existing.version if plan.existing is not None else None
        if plan.options.keep_both:
            return None, "", _("_Install")
        if plan.action == ACTION_UPDATE:
            if old and new:
                text = _("Version {old} is installed and will be replaced by {new}").format(
                    old=old, new=new)
            else:
                text = _("The installed copy of this app will be replaced by this version")
            return text, "software-update-available-symbolic", _("_Update")
        if plan.action == ACTION_REINSTALL:
            return (_("This version is already installed. Installing it again can fix problems "
                      "with it."), "view-refresh-symbolic", _("_Reinstall"))
        if plan.action == ACTION_DOWNGRADE:
            if old and new:
                text = _("A newer version ({old}) is installed. It will be replaced by the older "
                         "version {new}.").format(old=old, new=new)
                label = _("_Go Back to {version}").format(version=new)
            else:
                text = _("A newer version is installed. It will be replaced by this older one.")
                label = _("_Install Anyway")
            return text, "dialog-warning-symbolic", label
        return None, "", _("_Install")

    @staticmethod
    def _button_label(plan: InstallPlan, label: str) -> str:
        """Another signer than the installed version's: the button says it goes on anyway."""
        if not plan.signer_changed:
            return label
        if plan.action == ACTION_UPDATE and not plan.options.keep_both:
            return _("_Update Anyway")
        return _("_Install Anyway")

    @staticmethod
    def _replace_texts(plan: InstallPlan) -> tuple[str, str]:
        """Title and one-line explanation of the "Replace version X" choice."""
        main = plan.main_installed
        installed = main.version if main is not None else None
        title = (_("Replace version {version}").format(version=installed) if installed
                 else _("Replace the installed version"))
        order = compare_versions(plan.version, installed) if plan.version and installed else 0
        if order > 0:
            subtitle = _("This newer version takes its place")
        elif order < 0:
            subtitle = _("This older version takes its place")
        else:
            subtitle = _("This version takes its place")
        return title, subtitle

    def _backup_text(self, plan: InstallPlan) -> str | None:
        if plan.backup_target is None or plan.existing is None:
            return None
        return backup_note(plan.existing.version, max(self._backup_days, 1))

    def _visible_warnings(self, plan: InstallPlan) -> list[str]:
        hidden: set[str] = set()
        if plan.action == ACTION_DOWNGRADE and plan.existing is not None:
            # Already explained by the action row (or the "Replace version X" choice) above.
            hidden.add(_("A newer version ({installed}) is already installed. This file contains "
                         "the older version {new}.").format(installed=plan.existing.version,
                                                            new=plan.version))
        return [w for w in plan.warnings if w not in hidden]

    def _split_warnings(self, plan: InstallPlan) -> tuple[list[str], list[str]]:
        """(trust warnings shown at the top, the other warnings). The installer puts its notes
        about signatures first (DESIGN section 25): another signer than the installed version's
        (``plan.warnings[0]`` when ``plan.signer_changed``) and a signature that does not match."""
        visible = self._visible_warnings(plan)
        count = int(plan.signer_changed) + int(signature_is_broken(getattr(self.info, "signature",
                                                                             None)))
        trust = [w for w in plan.warnings[:count] if w in visible]
        return trust, [w for w in visible if w not in trust]

    def _refresh_confirm(self) -> None:
        plan, info = self.plan, self.info
        assert plan is not None and info is not None

        choice = plan.keep_both_available
        self._choice_group.set_visible(choice)
        if choice:
            title, subtitle = self._replace_texts(plan)
            self._replace_row.set_title(title)
            self._replace_row.set_subtitle(subtitle)

        text, icon, button_label = self._action_text(plan)
        show_action = text is not None and not choice
        self._action_group.set_visible(show_action)
        if show_action:
            self._action_row.set_title(text)
            self._action_icon.set_from_icon_name(icon)
            if plan.action == ACTION_DOWNGRADE:
                self._action_icon.add_css_class("warning")
            else:
                self._action_icon.remove_css_class("warning")

        self._install_button.set_label(self._button_label(plan, button_label))
        # A changed signer is the one case where going on is the risky choice.
        if plan.signer_changed:
            self._install_button.remove_css_class("suggested-action")
            self._install_button.add_css_class("destructive-action")
        else:
            self._install_button.remove_css_class("destructive-action")
            self._install_button.add_css_class("suggested-action")
        updating = plan.action == ACTION_UPDATE and not plan.options.keep_both
        self.set_title(_("Update App") if updating else _("Install App"))

        backup = self._backup_text(plan)
        facts = [Fact(ICON_BACKUP, backup)] if backup else []
        self._facts.set_facts(facts + file_facts(
            info, pinned=plan.pinned,
            installed=plan.existing if plan.existing is not None else plan.main_installed))

        trust, warnings = self._split_warnings(plan)
        for row in self._trust_rows:
            self._trust_group.remove(row)
        self._trust_rows = [warning_row(w, serious=True) for w in trust]
        for row in self._trust_rows:
            self._trust_group.add(row)
        self._trust_group.set_visible(bool(self._trust_rows))
        for row in self._warning_rows:
            self._warnings_group.remove(row)
        self._warning_rows = [warning_row(w) for w in warnings]
        for row in self._warning_rows:
            self._warnings_group.add(row)
        self._warnings_group.set_visible(bool(self._warning_rows))

        # The file of an installed copy is always kept (a warning row explains it).
        keep_choice = not plan.in_place and not plan.source_is_installed
        self._keep_row.set_visible(keep_choice)
        # A portable app never gets a sandbox permission: the core decides (a warning says so).
        sandbox_needed = (not self.is_portable and self.status is not None
                          and needs_sandbox_fix(info, self.status))
        self._sandbox_row.set_visible(sandbox_needed)
        self._sandbox_help.set_visible(sandbox_needed)
        self._more_row.set_visible(keep_choice or sandbox_needed)

        # "Only for me" needs the password, too, when the app gets its sandbox permission.
        if plan.scope is Scope.USER:
            user_password = plan.requires_root
        else:
            user_password = plan.sandbox_fix is SandboxFix.APPARMOR
        self._user_row.set_subtitle(_("Asks for your password once, for a special permission")
                                    if user_password else _("No password needed"))

        if plan.kind == KIND_PORTABLE and plan.install_dir is not None:
            self._target_label.set_label(_("The app will be unpacked to {path}").format(
                path=plan.install_dir))
        elif plan.in_place:
            self._target_label.set_label(_("The app stays at {path}").format(
                path=plan.target_appimage))
        else:
            self._target_label.set_label(_("The app will be saved as {path}").format(
                path=plan.target_appimage))

    # ------------------------------------------------------------------------------------------
    # installing
    # ------------------------------------------------------------------------------------------

    def _on_install_progress(self, fraction: float | None, message: str) -> None:
        if message:
            self._progress_label.set_label(message)
        if fraction is None:
            if not self._pulse_source:
                self._pulse_source = GLib.timeout_add(120, self._pulse)
        else:
            self._stop_pulse()
            self._progress_bar.set_fraction(max(0.0, min(1.0, fraction)))

    def _pulse(self) -> bool:
        self._progress_bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _stop_pulse(self) -> None:
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def _on_install_finished(self, app: InstalledApp | None, error: BaseException | None) -> None:
        self._installing = False
        self._stop_pulse()
        if self._progress is not None:
            self._progress.close()
        self.set_can_close(True)
        if self._closed:  # force-closed while installing (e.g. the app quit)
            self._cleanup()
            return
        if isinstance(error, AuthorizationError):
            log.info("authorization failed: %s", error.details or error)
            if self._replan() is not None:
                self._refresh_confirm()
                self._show_page(PAGE_CONFIRM)
            # "Authentication was cancelled" (or: the password was not accepted).
            self.add_toast(str(error))
            return
        if error is not None:
            # Full traceback in the log only for unexpected errors.
            log.warning("installing %s failed: %s", self.path, error,
                        exc_info=None if isinstance(error, EasyInstallerError) else error)
            self._show_error(error, reading=False)
            return
        assert app is not None
        self.installed_app = app
        self._show_done(app)
        self.emit("installed", app)

    def _show_done(self, app: InstalledApp) -> None:
        plan = self.plan
        name = self._installed_name(plan) if plan is not None else app.name
        action = plan.action if plan is not None else ACTION_INSTALL
        if action == ACTION_UPDATE and not (plan is not None and plan.options.keep_both):
            title = _("{name} was updated").format(name=name)
        else:
            title = _("{name} is installed").format(name=name)
        if Scope(app.scope) is Scope.SYSTEM:
            description = _("Everyone on this computer can find it in the app menu and search.")
        else:
            description = _("You can find it in your app menu and search.")
        self._done_page.set_title(title)
        self._done_page.set_description(escape(description))
        self._done_page.set_paintable(app_icon_paintable(self, app.icon_path, DONE_ICON_SIZE))

        for row in self._done_warning_rows:
            self._done_warnings.remove(row)
        # Warnings that appeared while installing (e.g. the original file could not be deleted).
        before = set(self._warnings_before_install)
        new = [w for w in (plan.warnings if plan else []) if w not in before]
        self._done_warning_rows = [warning_row(w) for w in new]
        for row in self._done_warning_rows:
            self._done_warnings.add(row)
        self._done_warnings.set_visible(bool(new))
        self._open_button.set_visible(os.path.isfile(app.desktop_path))
        self._show_page(PAGE_DONE)

    def _on_open_clicked(self, *_args: object) -> None:
        app = self.installed_app
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

    def _show_error(self, error: BaseException, *, reading: bool) -> None:
        friendly = describe_error(error, reading=reading)
        self._error_page.set_title(friendly.title)
        self._error_page.set_description(escape(friendly.message))
        self._error_file.set_label(_("File: {name}").format(name=display_name(self.path)))
        self._details_label.set_label(friendly.details or "")
        self._details_expander.set_visible(bool(friendly.details))
        self._details_expander.set_expanded(False)
        # After a failed installation the user can go back and e.g. choose "Only for me".
        self._can_retry = not reading and self.info is not None and self.path.is_file()
        self._back_button.set_visible(self._can_retry)
        self._show_page(PAGE_ERROR)

    def _on_back_clicked(self, *_args: object) -> None:
        if not self._can_retry or self.info is None:
            return
        if self._replan() is not None:
            self._refresh_confirm()
            self._show_page(PAGE_CONFIRM)

    # ------------------------------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------------------------------

    def _on_close_attempt(self, *_args: object) -> None:
        if self._installing:
            self.add_toast(_("Please wait until the installation is finished"))

    def _on_closed(self, *_args: object) -> None:
        self._closed = True
        self._cancel.set()
        if self._progress is not None and not self._installing:
            self._progress.close()
        if not self._installing:
            self._stop_pulse()
            self._cleanup()

    def _cleanup(self) -> None:
        if self.info is not None:
            self.info.cleanup()
