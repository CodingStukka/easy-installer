"""Main window: installed apps, updates, empty state, banners, drag & drop, install queue.

Besides the list itself the window keeps it true and current (DESIGN sections 18 and 28):

* at start (worker thread): kept previous versions older than the settings allow are deleted,
  apps that replaced their own file are brought up to date ("reconcile") and the updates found
  by earlier checks are shown; then, if the settings allow it and a check is due, the update
  sources are asked - silently, a missing connection is no reason for a message;
* when the window gets the focus again, apps whose file changed are reconciled (one ``stat``
  per app first);
* "Check for Updates" (header button, menu, Ctrl+R) asks every source now and reports the result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from .. import APP_ID, APP_NAME, __version__
from ..core import privileged
from ..core.installer import (
    can_keep_permission,
    can_start_directly,
    drop_backup,
    find_installed,
    list_installed,
    prune_backups,
    repair as repair_installed,
    rollback,
    switches_start_mode,
    update_start_modes,
)
from ..core.downloads import remove_leftovers as remove_leftover_downloads
from ..core.paths import Scope
from ..core.portable import ARCHIVE_SUFFIXES
from ..core.reconcile import (
    CHANGE_ADOPTED,
    CHANGE_NEEDS_ADMIN,
    CHANGE_UPDATED,
    ReconcileResult,
    needs_reconcile,
    reconcile_all,
    reconcile_app,
    went_back,
)
from ..core.registry import STATUS_MISSING_LAUNCHER, STATUS_OK, InstalledApp
from ..core.settings import Settings, load_settings
from ..core.system_checks import SystemStatus, get_system_status
from ..core.updater import (
    UpdateCheckResult,
    apply_update,
    cached_updates,
    check_all_updates,
    has_update_source,
    is_newer_than_installed,
)
from ..core.updates import AvailableUpdate, UpdateCache
from ..errors import (
    AuthorizationError,
    EasyInstallerError,
    NetworkError,
    NotInstalledError,
    UpdateCancelled,
)
from ..i18n import _, ngettext
from . import settings
from .app_row import AppRow
from .async_utils import run_in_thread
from .common import APPIMAGE_MIME_TYPES, describe_error, launch_desktop_file, toast_text
from .details_dialog import DetailsDialog
from .install_dialog import InstallDialog
from .preferences_dialog import PreferencesDialog
from .system_dialog import SystemCheckDialog, debug_info
from .uninstall_dialog import (
    RESPONSE_UNINSTALL,
    UninstallDialog,
    UninstallOutcome,
    done_text,
    is_broken,
    keep_permission_dialog,
    notes_dialog,
    result_toast_text,
    uninstall_and_trash,
)

try:
    from .update_dialog import UpdateDialog
except ModuleNotFoundError as exc:  # pragma: no cover - depends on what is installed
    if exc.name != f"{__package__}.update_dialog":
        raise
    UpdateDialog = None  # a simple confirmation is used instead (see MainWindow.start_update)

log = logging.getLogger(__name__)

DESKTOP_ID = f"{APP_ID}.desktop"
SEARCH_THRESHOLD = 6
PAGE_EMPTY = "empty"
PAGE_LIST = "list"
#: Below this window width rows get compact (see AppRow.set_compact) and the header bar
#: leaves out the window's name.
COMPACT_CONDITION = "max-width: 560sp"
#: The "N updates are available — Update All" banner appears from this many updates on.
UPDATE_ALL_THRESHOLD = 2

UpdateKey = tuple[str, str]
Done = Callable[["InstalledApp | None", "BaseException | None"], None]


def app_key(app: InstalledApp) -> UpdateKey:
    """The key of an app in the update cache and in ``check_all_updates`` results."""
    return (Scope(app.scope).value, app.id)


# ------------------------------------------------------------------------------------------------
# work done in worker threads (no widgets; tested in tests/test_gui_logic.py)
# ------------------------------------------------------------------------------------------------


def repair_app(app: InstalledApp) -> InstalledApp:
    """Make the launcher, icon and registry entry of an installed app again (worker thread).

    Works for both kinds and scopes and for kept copies ("keep both"); the choices made when
    the app was installed are kept.
    """
    return repair_installed(app.id, Scope(app.scope))


def go_back(app: InstalledApp) -> InstalledApp:
    """Go back to the kept previous version (worker thread). An app that replaced its own file
    since it was last looked at is brought up to date first, so that the version it has now
    is kept under its real version."""
    if needs_reconcile(app):
        reconcile_app(app)
    return rollback(app.id, Scope(app.scope))


def speed_up_apps(status: SystemStatus, *, system: bool) -> list[InstalledApp]:
    """Apps no longer unpack themselves on every start once FUSE works (worker thread).

    Per-user launchers are updated directly; with ``system`` the apps installed for everyone are
    repaired through the helper (right after installing FUSE the password is still remembered).
    """
    updated = update_start_modes(status)
    if system:
        for app in list_installed([Scope.SYSTEM]):
            if not switches_start_mode(app) or app.status() != STATUS_OK \
                    or not can_start_directly(app, status):
                continue
            try:
                updated.append(repair_app(app))
            except AuthorizationError as exc:
                # One password prompt per app: a Cancel means "not now" for all of them.
                log.warning("%s still unpacks itself on every start: %s", app.id, exc)
                break
            except EasyInstallerError as exc:
                log.warning("%s still unpacks itself on every start: %s", app.id, exc)
    return updated


def updates_due(apps: Iterable[InstalledApp], interval_hours: int,
                cache: UpdateCache | None = None) -> bool:
    """At least one app that can be checked was not checked within ``interval_hours``."""
    checkable = [app for app in apps if has_update_source(app)]
    if not checkable:
        return False
    cache = cache or UpdateCache()
    return any(cache.is_due(Scope(app.scope).value, app.id, interval_hours) for app in checkable)


@dataclass
class StartupReport:
    """What :func:`startup_maintenance` did and found."""

    results: list[ReconcileResult] = field(default_factory=list)
    updates: dict[UpdateKey, AvailableUpdate] = field(default_factory=dict)
    #: an automatic update check is wanted now (enabled in the settings, and due)
    check_due: bool = False
    pruned: list[InstalledApp] = field(default_factory=list)


def startup_maintenance() -> StartupReport:
    """Worker thread, once at start: delete kept versions older than the settings allow, bring
    apps that updated themselves up to date, and read what earlier update checks found.

    No step may keep the others from running; nothing here uses the network.
    """
    report = StartupReport()
    prefs = load_settings()
    try:
        report.pruned = prune_backups(prefs.backup_days)
    except Exception:  # noqa: BLE001
        log.warning("could not delete the old kept versions", exc_info=True)
    try:
        report.results = reconcile_all()
    except Exception:  # noqa: BLE001
        log.warning("could not check the installed apps", exc_info=True)
    report.updates = cached_updates()
    remove_leftover_downloads()   # (an update the computer was switched off in)
    if prefs.check_updates:
        try:
            report.check_due = updates_due(list_installed(), prefs.update_interval_hours)
        except Exception:  # noqa: BLE001
            log.warning("cannot tell whether an update check is due", exc_info=True)
    return report


def reconcile_apps(apps: Iterable[InstalledApp]) -> list[ReconcileResult]:
    """Worker thread: :func:`reconcile_app` for ``apps`` (e.g. when the window gets the focus)."""
    results = []
    for app in apps:
        try:
            results.append(reconcile_app(app))
        except Exception:  # noqa: BLE001 - one odd app must not stop the others
            log.warning("reconciling %s failed", app.id, exc_info=True)
    return results


def self_update_text(results: Iterable[ReconcileResult]) -> str | None:
    """"T3 Code was updated to 0.0.44" when apps were found to have updated themselves - or
    "T3 Code went back to version 0.0.44" when one replaced itself with an older version."""
    changed = [r for r in results if r.change in (CHANGE_UPDATED, CHANGE_ADOPTED)
               and (r.change == CHANGE_UPDATED or r.new_version != r.old_version)]
    back = [r for r in changed if went_back(r)]
    forward = [r for r in changed if not went_back(r)]
    texts = []
    if len(forward) == 1:
        result = forward[0]
        if result.new_version and result.new_version != result.old_version:
            texts.append(_("{name} was updated to {version}").format(
                name=result.app.name, version=result.new_version))
        else:
            texts.append(_("{name} was updated").format(name=result.app.name))
    elif forward:
        texts.append(ngettext("{count} app was updated", "{count} apps were updated",
                              len(forward)).format(count=len(forward)))
    if len(back) == 1:
        texts.append(_("{name} went back to version {version} by itself").format(
            name=back[0].app.name, version=back[0].new_version))
    elif back:
        texts.append(ngettext("{count} app went back to an older version by itself",
                              "{count} apps went back to an older version by itself",
                              len(back)).format(count=len(back)))
    return " · ".join(texts) or None


def merge_updates(known: Mapping[UpdateKey, AvailableUpdate],
                  result: UpdateCheckResult) -> dict[UpdateKey, AvailableUpdate]:
    """What is known after a check: its answers, and for apps it could not check what was
    known before."""
    merged = {key: update for key, update in known.items() if key in result.errors}
    merged.update(result.updates)
    return merged


def check_result_text(result: UpdateCheckResult) -> str:
    """The toast after "Check for Updates"."""
    found = len(result.updates)
    failed = len(result.errors)
    if result.checked == 0:
        return _("None of your apps tells where to find updates")
    if failed and failed == result.checked:
        first = next(iter(result.errors.values()))
        if all(isinstance(error, NetworkError) for error in result.errors.values()):
            return str(first)
        return _("Updates could not be checked")
    if found:
        text = ngettext("{count} update is available", "{count} updates are available",
                        found).format(count=found)
    else:
        text = _("All apps are up to date")
    if failed:
        text += " · " + ngettext("{count} app could not be checked",
                                 "{count} apps could not be checked", failed).format(count=failed)
    return text


def can_offer_default_handler() -> bool:
    """Our launcher is installed but double-clicking an AppImage opens something else."""
    ours = Gio.DesktopAppInfo.new(DESKTOP_ID)
    if ours is None:
        return False
    current = Gio.AppInfo.get_default_for_type(APPIMAGE_MIME_TYPES[0], False)
    return current is None or current.get_id() != DESKTOP_ID


def make_default_handler() -> None:
    """Raises GLib.Error if the association cannot be saved."""
    ours = Gio.DesktopAppInfo.new(DESKTOP_ID)
    if ours is None:
        raise GLib.Error(f"{DESKTOP_ID} is not installed")
    for mime_type in APPIMAGE_MIME_TYPES:
        ours.set_as_default_for_type(mime_type)


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "EasyInstallerMainWindow"

    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title=APP_NAME, default_width=760,
                         default_height=640, icon_name=APP_ID)
        self.set_size_request(360, 400)
        self.status: SystemStatus | None = None
        self.current_dialog: InstallDialog | None = None
        self._queue: list[Path] = []
        self._rows: list[AppRow] = []
        self._busy: set[UpdateKey] = set()
        self._signature: tuple | None = None
        self._closing = False
        self._compact = False
        #: updates known for the installed apps: (scope, id) -> update
        self._updates: dict[UpdateKey, AvailableUpdate] = {}
        #: apps whose update dialog is open (reconcile leaves them alone meanwhile)
        self._updating: set[UpdateKey] = set()
        self._update_queue: list[tuple[InstalledApp, AvailableUpdate]] = []
        #: the last update dialog was cancelled by the user (that stops Update All)
        self._update_cancelled = False
        self._checking = False
        self._check_manual = False
        #: system-wide apps that changed on disk: only "Repair" (with the password) updates them
        self._needs_repair: set[UpdateKey] = set()
        self._announced_repair: set[UpdateKey] = set()
        # apps whose menu entry is missing that a toast already offered to uninstall
        self._announced_missing: set[UpdateKey] = set()
        self._maintenance_running = False
        self._reconciling = False
        #: automatic update checks are on (as far as this window knows)
        self._auto_check = load_settings().check_updates

        self._build()
        self._setup_actions()
        self._setup_type_to_search()
        self._setup_drop_target()
        self._setup_breakpoint()
        self.connect("notify::is-active", self._on_active_changed)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", self._on_destroy)
        self._close_hook = GObject.add_emission_hook(Gtk.Window, "close-request",
                                                     self._on_any_close_request)
        self.reload()
        self._update_banners()
        run_in_thread(get_system_status, self._on_status_loaded)
        self._start_maintenance()

    # ------------------------------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------------------------------

    def _build(self) -> None:
        self._toasts = Adw.ToastOverlay()
        overlay = Gtk.Overlay()
        self._toasts.set_child(overlay)
        self.set_content(self._toasts)

        toolbar = Adw.ToolbarView()
        overlay.set_child(toolbar)

        header = self._header = Adw.HeaderBar()
        # can_shrink: long translations ("App installieren …") ellipsize instead of making the
        # header bar wider than the window's minimum width.
        self._install_content = Adw.ButtonContent(icon_name="list-add-symbolic",
                                                  label=_("Install _App…"),
                                                  use_underline=True, can_shrink=True)
        install = self._install_button = Gtk.Button(
            child=self._install_content, action_name="app.open",
            tooltip_text=_("Choose an AppImage file to install"), can_shrink=True)
        header.pack_start(install)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self._primary_menu(),
                                     primary=True, tooltip_text=_("Main Menu"))
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Main Menu")])
        header.pack_end(menu_button)
        header.pack_end(self._build_check_button())
        toolbar.add_top_bar(header)

        # The banners are part of the content, not top bars: Adw.ToolbarView 1.5 reserves only
        # one line per banner, so a banner whose text wraps (narrow window, German) would cover
        # the top of the list.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._build_handler_banner())
        self._fuse_banner = Adw.Banner(
            title=_("Apps will start a bit slower because a system component (FUSE) is missing."),
            revealed=False)
        self._fuse_banner.connect("button-clicked", self._on_install_fuse)
        content.append(self._fuse_banner)
        # Not "Update _All": "Install _App…" in the header bar has that access key.
        self._updates_banner = Adw.Banner(button_label=_("Update A_ll"), revealed=False,
                                          use_markup=False)
        self._updates_banner.connect("button-clicked", self._on_update_all_clicked)
        content.append(self._updates_banner)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._stack.add_named(self._build_empty_page(), PAGE_EMPTY)
        self._stack.add_named(self._build_list_page(), PAGE_LIST)
        content.append(self._stack)
        toolbar.set_content(content)

        self._drop_overlay = self._build_drop_overlay()
        overlay.add_overlay(self._drop_overlay)

    def _build_check_button(self) -> Gtk.Widget:
        """"Check for Updates" in the header bar; a spinner takes its place while checking."""
        self._check_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                        action_name="win.check-updates",
                                        tooltip_text=_("Check for Updates"))
        self._check_button.update_property([Gtk.AccessibleProperty.LABEL],
                                           [_("Check for Updates")])
        self._check_spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                          tooltip_text=_("Checking for updates…"))
        self._check_spinner.update_property([Gtk.AccessibleProperty.LABEL],
                                            [_("Checking for updates…")])
        self._check_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._check_stack.add_named(self._check_button, "button")
        self._check_stack.add_named(self._check_spinner, "spinner")
        return self._check_stack

    def _primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Check for _Updates"), "win.check-updates")
        section.append(_("_Check This Computer"), "app.check-system")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("_Preferences"), "app.preferences")
        section.append(_("_Keyboard Shortcuts"), "win.show-help-overlay")
        section.append(_("_About Easy Installer"), "app.about")
        menu.append_section(None, section)
        return menu

    def _build_handler_banner(self) -> Gtk.Widget:
        self._handler_banner = Adw.Banner(
            title=_("Open AppImages with Easy Installer when you double-click them?"),
            button_label=_("_Make Default"), revealed=False)
        self._handler_banner.connect("button-clicked", self._on_make_default)
        self._handler_banner.add_css_class("dismissable-banner")
        # Adw.Banner has a single button; a small close button lets people say "no, thanks".
        dismiss = Gtk.Button(icon_name="window-close-symbolic", halign=Gtk.Align.START,
                             valign=Gtk.Align.CENTER, tooltip_text=_("Don’t Ask Again"))
        dismiss.add_css_class("flat")
        dismiss.add_css_class("circular")
        dismiss.add_css_class("banner-dismiss")
        dismiss.update_property([Gtk.AccessibleProperty.LABEL], [_("Don’t Ask Again")])
        dismiss.connect("clicked", self._on_dismiss_handler_banner)
        self._handler_banner.bind_property("revealed", dismiss, "visible",
                                           GObject.BindingFlags.SYNC_CREATE)
        box = Gtk.Overlay(child=self._handler_banner)
        box.add_overlay(dismiss)
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
        page = Adw.StatusPage(
            icon_name=APP_ID if icon_theme.has_icon(APP_ID) else "system-software-install-symbolic",
            title=_("No apps installed yet"),
            description=_("Drag an AppImage file into this window, or choose one with the button "
                          "below. Easy Installer puts it in a safe place and adds it to your app "
                          "menu."))
        button = Gtk.Button(label=_("C_hoose AppImage…"), use_underline=True,
                            action_name="app.open", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        page.set_child(button)
        return page

    def _build_list_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=24,
                      margin_bottom=24, margin_start=12, margin_end=12)
        self._search = Gtk.SearchEntry(placeholder_text=_("Search installed apps"), visible=False)
        self._search.connect("search-changed", lambda *_a: self._apply_filter())
        box.append(self._search)
        self._group = Adw.PreferencesGroup(
            title=_("Installed apps"),
            description=_("You can also find these apps in your app menu and search."))
        box.append(self._group)
        self._no_results = Adw.StatusPage(icon_name="edit-find-symbolic",
                                          title=_("No matching apps"), visible=False)
        self._no_results.add_css_class("compact")
        box.append(self._no_results)
        clamp = Adw.Clamp(maximum_size=640, child=box)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)

    def _build_drop_overlay(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, visible=False,
                      can_target=False)
        box.add_css_class("drop-overlay")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        valign=Gtk.Align.CENTER, vexpand=True)
        icon = Gtk.Image(icon_name="folder-download-symbolic", pixel_size=96)
        inner.append(icon)
        title = Gtk.Label(label=_("Drop to install"))
        title.add_css_class("title-1")
        inner.append(title)
        hint = Gtk.Label(label=_("Release the AppImage file to install it"))
        hint.add_css_class("dim-label")
        inner.append(hint)
        box.append(inner)
        return box

    def _setup_actions(self) -> None:
        close = Gio.SimpleAction.new("close", None)
        close.connect("activate", self._on_close_action)
        self.add_action(close)
        check = Gio.SimpleAction.new("check-updates", None)
        check.connect("activate", lambda *_a: self.check_for_updates())
        self.add_action(check)

    def _setup_type_to_search(self) -> None:
        # Not Gtk.SearchEntry.set_key_capture_widget(): it hands Return to the (hidden) entry
        # as well, so Enter on a focused row would no longer open the app's details.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_type_to_search)
        self.add_controller(keys)

    def starts_search(self, keyval: int, state: Gdk.ModifierType) -> bool:
        """Typing a letter (or any other printable character) in the list starts the search;
        Return, Space, Tab, the arrows and shortcuts keep working on the focused row."""
        if not self._search.get_visible() or self.get_visible_dialog() is not None:
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK):
            return False
        code = Gdk.keyval_to_unicode(keyval)
        char = chr(code) if code else ""
        if not char or not char.isprintable() or char.isspace():
            return False
        focus = self.get_focus()
        return not (isinstance(focus, Gtk.Editable) or (focus is not None and (
            focus is self._search or focus.is_ancestor(self._search))))

    def _on_type_to_search(self, controller: Gtk.EventControllerKey, keyval: int,
                           _keycode: int, state: Gdk.ModifierType) -> bool:
        if not self.starts_search(keyval, state):
            return False
        self._search.grab_focus()
        text = self._search.get_delegate() or self._search
        return bool(controller.forward(text))

    def _setup_drop_target(self) -> None:
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("enter", self._on_drop_enter)
        target.connect("leave", self._on_drop_leave)
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _setup_breakpoint(self) -> None:
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(COMPACT_CONDITION))
        breakpoint.connect("apply", lambda *_a: self._set_compact(True))
        breakpoint.connect("unapply", lambda *_a: self._set_compact(False))
        self.add_breakpoint(breakpoint)

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        # The window's name makes room for the header buttons ("Install App…").
        self._header.set_show_title(not compact)
        # Even so, a long translation of "Install App…" (German, Dutch) would be cut off next to
        # the other header buttons of a narrow window: the button shows only its icon there.
        if compact:
            self._install_button.set_icon_name("list-add-symbolic")
            self._install_button.update_property([Gtk.AccessibleProperty.LABEL],
                                                 [_("Choose an AppImage file to install")])
        else:
            self._install_button.set_child(self._install_content)
            self._install_button.reset_property(Gtk.AccessibleProperty.LABEL)
        for row in self._rows:
            row.set_compact(compact)

    @property
    def compact(self) -> bool:
        return self._compact

    # ------------------------------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------------------------------

    @property
    def rows(self) -> list[AppRow]:
        return list(self._rows)

    def update_for(self, app: InstalledApp) -> AvailableUpdate | None:
        """The known update of ``app``, if it is still newer than what is installed."""
        update = self._updates.get(app_key(app))
        if update is None or not has_update_source(app) or not is_newer_than_installed(update, app):
            return None
        return update

    def needs_repair(self, app: InstalledApp) -> bool:
        return app_key(app) in self._needs_repair

    def is_app_busy(self, app: InstalledApp) -> bool:
        key = app_key(app)
        return key in self._busy or key in self._updating

    def reload(self, *, force: bool = False) -> None:
        """Re-read the registries; rebuilds the list only if something changed."""
        try:
            apps = list_installed()
        except Exception:  # an unreadable registry must not take the window down
            log.exception("cannot read the list of installed apps")
            apps = []
        updates = {app_key(a): self.update_for(a) for a in apps}
        signature = tuple(
            (a.id, Scope(a.scope).value, a.name, a.version, a.status(), a.icon_path, a.updated_at,
             a.size, a.kind, updates[app_key(a)], app_key(a) in self._needs_repair)
            for a in apps)
        if signature == self._signature and not force:
            for row, app in zip(self._rows, apps):
                row.app = app   # (what the list shows is the same)
            return
        self._signature = signature

        focus = self.get_focus()
        focused = next((app_key(row.app) for row in self._rows if focus is not None
                        and (focus is row or focus.is_ancestor(row))), None)
        for row in self._rows:
            self._group.remove(row)
        self._rows = []
        for app in apps:
            row = AppRow(app, update=updates[app_key(app)], needs_repair=self.needs_repair(app),
                         compact=self._compact)
            row.connect("open-app", lambda r: self.launch_app(r.app))
            row.connect("show-in-files", lambda r: self.show_in_files(r.app))
            row.connect("repair", lambda r: self.repair(r.app))
            row.connect("uninstall", lambda r: self.confirm_uninstall(r.app))
            row.connect("details", lambda r: self.show_details(r.app))
            row.connect("update", self._on_row_update)
            if self.is_app_busy(app):
                row.set_busy(True)
            self._group.add(row)
            self._rows.append(row)

        many = len(apps) > SEARCH_THRESHOLD
        self._search.set_visible(many)
        if not many:
            self._search.set_text("")
        self._apply_filter()
        self._stack.set_visible_child_name(PAGE_LIST if apps else PAGE_EMPTY)
        self._check_stack.set_visible(bool(apps))
        self._update_updates_banner()
        self._announce_missing_launchers(apps)
        if focused is not None:
            # The keyboard focus stays with its app (else GTK moves it to the first row).
            row = next((r for r in self._rows if app_key(r.app) == focused and r.get_visible()),
                       None)
            if row is not None:
                row.grab_focus()

    def _announce_missing_launchers(self, apps: list[InstalledApp]) -> None:
        """An app's menu entry was deleted from outside, e.g. with Linux Mint's own "Uninstall"
        (it only knows packages and offers to delete just the menu entry): offer to uninstall the
        rest, once per app and session. Its row offers "Repair", too."""
        missing = {app_key(a): a for a in apps
                   if a.status() == STATUS_MISSING_LAUNCHER and not self.is_app_busy(a)}
        self._announced_missing &= set(missing)   # (a repaired app is announced again later)
        newly = [a for key, a in missing.items() if key not in self._announced_missing]
        self._announced_missing.update(app_key(a) for a in newly)
        if len(newly) == 1:
            app = newly[0]
            self.add_toast(_("{name} is no longer in the app menu").format(name=app.name),
                           timeout=10, button_label=_("Uninstall…"),
                           on_button=lambda: self._uninstall_by_key(app))
        elif newly:
            self.add_toast(ngettext("{count} app is no longer in the app menu",
                                    "{count} apps are no longer in the app menu",
                                    len(newly)).format(count=len(newly)), timeout=10)

    def _uninstall_by_key(self, app: InstalledApp) -> None:
        row = self._row_for(app)
        if row is not None and not row.busy:
            self.confirm_uninstall(row.app)

    def _on_row_update(self, row: AppRow) -> None:
        if row.update is not None:
            self.start_update(row.app, row.update)

    def _return_focus_after(self, dialog: Adw.Dialog, app: InstalledApp) -> None:
        """When ``dialog`` closes, the keyboard focus goes back to the app's row (the list is
        rebuilt after every change, so the row that had it may be gone)."""
        key = app_key(app)

        def restore() -> bool:
            if self.get_visible_dialog() is not None:
                return GLib.SOURCE_REMOVE
            row = next((r for r in self._rows if app_key(r.app) == key and r.get_visible()),
                       None)
            focus = self.get_focus()
            if row is not None and (focus is None or not focus.get_mapped()
                                    or not focus.is_ancestor(self._group)):
                row.grab_focus()
            return GLib.SOURCE_REMOVE

        dialog.connect("closed", lambda *_a: GLib.idle_add(restore))

    def _apply_filter(self) -> None:
        query = self._search.get_text() if self._search.get_visible() else ""
        visible = 0
        for row in self._rows:
            match = row.matches(query)
            row.set_visible(match)
            visible += match
        self._group.set_visible(visible > 0 or not self._rows)
        self._no_results.set_visible(bool(self._rows) and visible == 0)

    def _row_for(self, app: InstalledApp) -> AppRow | None:
        key = app_key(app)
        return next((row for row in self._rows if app_key(row.app) == key), None)

    def _set_busy(self, app: InstalledApp, busy: bool) -> None:
        key = app_key(app)
        if busy:
            self._busy.add(key)
        else:
            self._busy.discard(key)
        row = self._row_for(app)
        if row is not None:
            row.set_busy(busy)

    def _on_active_changed(self, *_args: object) -> None:
        if self.is_active():
            self.reload()
            self._reconcile_changed_apps()

    # ------------------------------------------------------------------------------------------
    # toasts & alerts
    # ------------------------------------------------------------------------------------------

    def add_toast(self, text: str, *, timeout: int = 5, button_label: str | None = None,
                  on_button: Callable[[], None] | None = None) -> Adw.Toast:
        toast = Adw.Toast(title=toast_text(text), timeout=timeout)
        if button_label and on_button is not None:
            toast.set_button_label(button_label)
            toast.connect("button-clicked", lambda *_a: on_button())
        self._toasts.add_toast(toast)
        return toast

    def show_error_alert(self, heading: str, error: BaseException) -> Adw.AlertDialog:
        friendly = describe_error(error)
        dialog = Adw.AlertDialog(heading=heading, body=friendly.message)
        dialog.add_response("close", _("_Close"))
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        if friendly.details:
            label = Gtk.Label(label=friendly.details, wrap=True, xalign=0, selectable=True)
            label.add_css_class("monospace")
            label.add_css_class("caption")
            scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          max_content_height=160, propagate_natural_height=True,
                                          child=label)
            expander = Gtk.Expander(label=_("Technical details"), child=scrolled)
            dialog.set_extra_child(expander)
        dialog.present(self)
        return dialog

    # ------------------------------------------------------------------------------------------
    # installing
    # ------------------------------------------------------------------------------------------

    def choose_files(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose an App"), modal=True,
                                accept_label=_("_Install"))
        apps = Gtk.FileFilter(name=_("Apps (AppImages and archives)"))
        appimages = Gtk.FileFilter(name=_("AppImage apps"))
        archives = Gtk.FileFilter(name=_("Portable apps in archives"))
        for mime_type in (*APPIMAGE_MIME_TYPES, "application/x-appimage"):
            appimages.add_mime_type(mime_type)
            apps.add_mime_type(mime_type)
        appimages.add_suffix("AppImage")
        apps.add_suffix("AppImage")
        for suffix in ARCHIVE_SUFFIXES:
            archives.add_suffix(suffix.lstrip("."))
            apps.add_suffix(suffix.lstrip("."))
        everything = Gtk.FileFilter(name=_("All files"))
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for item in (apps, appimages, archives, everything):
            filters.append(item)
        dialog.set_filters(filters)
        dialog.set_default_filter(apps)
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if downloads and Path(downloads).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(downloads))
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                log.warning("file chooser failed: %s", exc)
            return
        if files is not None:
            self.install_files([files.get_item(i) for i in range(files.get_n_items())])

    def install_files(self, files: Iterable[Gio.File | str | Path]) -> None:
        """Queue files for installation; they are offered one dialog after another."""
        self._closing = False
        added: list[Path] = []
        for item in files:
            if isinstance(item, Gio.File):
                path = item.get_path()
                if not path:
                    self.add_toast(_("“{name}” is not on this computer. Please download it "
                                     "first.").format(name=item.get_basename() or item.get_uri()))
                    continue
            else:
                path = str(item)
            path_obj = Path(path)
            if path_obj in self._queue:
                continue
            if self.current_dialog is not None and self.current_dialog.path == path_obj:
                continue
            self._queue.append(path_obj)
            added.append(path_obj)
        if added and self.current_dialog is not None:
            # Dropped (or opened from Files) while a dialog is open: say that it was noticed.
            if len(added) == 1:
                text = _("“{name}” will be installed after this app").format(
                    name=GLib.filename_display_basename(str(added[0])))
            else:
                text = ngettext("{count} more app is waiting to be installed",
                                "{count} more apps are waiting to be installed",
                                len(self._queue)).format(count=len(self._queue))
            self.current_dialog.add_toast(text)
        self._show_next_dialog()

    @property
    def queued_files(self) -> list[Path]:
        return list(self._queue)

    def _show_next_dialog(self) -> bool:
        if self.current_dialog is not None or not self._queue:
            return GLib.SOURCE_REMOVE
        path = self._queue.pop(0)
        dialog = InstallDialog(path)
        dialog.connect("installed", self._on_app_installed)
        dialog.connect("closed", self._on_install_dialog_closed)
        self.current_dialog = dialog
        dialog.present(self)
        dialog.start()
        if self._queue:
            count = len(self._queue)
            dialog.add_toast(ngettext("{count} more app is waiting to be installed",
                                      "{count} more apps are waiting to be installed",
                                      count).format(count=count))
        return GLib.SOURCE_REMOVE

    def _on_app_installed(self, _dialog: InstallDialog, app: InstalledApp | None) -> None:
        if isinstance(app, InstalledApp):
            # Installing another file of an app answers a known update in any case.
            self._updates.pop(app_key(app), None)
            self._needs_repair.discard(app_key(app))
        self.reload()

    def _on_install_dialog_closed(self, dialog: InstallDialog) -> None:
        if dialog is self.current_dialog:
            self.current_dialog = None
        self.reload()
        if not self._closing:  # closing the window or quitting drops the queued files
            GLib.idle_add(self._show_next_dialog)

    # ------------------------------------------------------------------------------------------
    # per-app actions
    # ------------------------------------------------------------------------------------------

    def launch_app(self, app: InstalledApp) -> None:
        try:
            launch_desktop_file(app.desktop_path, self)
        except (GLib.Error, ValueError) as exc:
            log.warning("cannot start %s: %s", app.id, exc)
            self.add_toast(_("{name} could not be started").format(name=app.name))
            self.reload()
            return
        self.add_toast(_("Starting {name}…").format(name=app.name), timeout=3)

    def show_in_files(self, app: InstalledApp) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(app.appimage_path))
        launcher.open_containing_folder(self, None, self._on_folder_opened, app)

    def _on_folder_opened(self, launcher: Gtk.FileLauncher, result: Gio.AsyncResult,
                          app: InstalledApp) -> None:
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                log.warning("cannot show %s: %s", app.appimage_path, exc)
                self.add_toast(_("The folder could not be opened"))

    def show_details(self, app: InstalledApp) -> DetailsDialog:
        dialog = DetailsDialog(self, app)
        self._return_focus_after(dialog, app)
        dialog.present(self)
        return dialog

    def _run_for_app(self, app: InstalledApp, work: Callable[..., object],
                     finished: Callable[[object, BaseException | None], None],
                     *args: object, **kwargs: object) -> None:
        """Run ``work(*args, **kwargs)`` in a worker thread while the app's row shows a spinner."""
        self._set_busy(app, True)

        def done(result: object, error: BaseException | None) -> None:
            self._set_busy(app, False)
            finished(result, error)

        run_in_thread(work, done, *args, **kwargs)

    def repair(self, app: InstalledApp, on_done: Done | None = None) -> None:
        was_missing = app.status() == STATUS_MISSING_LAUNCHER
        self._run_for_app(app, repair_app,
                          lambda result, error: self._on_repaired(app, result, error, was_missing,
                                                                  on_done), app)

    def _on_repaired(self, app: InstalledApp, result: InstalledApp | None,
                     error: BaseException | None, was_missing: bool = True,
                     on_done: Done | None = None) -> None:
        if error is None:
            self._needs_repair.discard(app_key(app))
        self.reload(force=True)
        if error is None:
            self.add_toast((_("{name} is back in the app menu") if was_missing
                            else _("{name} was repaired")).format(name=app.name))
        elif isinstance(error, AuthorizationError):
            self.add_toast(str(error))
        else:
            log.warning("repairing %s failed: %s", app.id, error)
            self.show_error_alert(_("{name} could not be repaired").format(name=app.name), error)
        if on_done is not None:
            on_done(result, error)

    def rollback_app(self, app: InstalledApp, on_done: Done | None = None) -> None:
        """Go back to the kept previous version (the current one is kept in its place)."""
        self._run_for_app(app, go_back,
                          lambda result, error: self._on_rolled_back(app, result, error, on_done),
                          app)

    def _on_rolled_back(self, app: InstalledApp, result: InstalledApp | None,
                        error: BaseException | None, on_done: Done | None) -> None:
        if error is None:
            # A deliberate step back: the newer version is not offered again right away.
            self._updates.pop(app_key(app), None)
        self.reload(force=True)
        if error is None and result is not None:
            if result.version:
                self.add_toast(_("{name} is back at version {version}").format(
                    name=result.name, version=result.version))
            else:
                self.add_toast(_("{name} is back at the previous version").format(name=result.name))
        elif isinstance(error, AuthorizationError):
            self.add_toast(str(error))
        elif error is not None:
            log.warning("going back to the previous version of %s failed: %s", app.id, error)
            self.show_error_alert(_("{name} could not go back to the previous version").format(
                name=app.name), error)
        if on_done is not None:
            on_done(result, error)

    def drop_backup_app(self, app: InstalledApp, on_done: Done | None = None) -> None:
        """Delete the kept previous version of ``app``."""
        self._run_for_app(app, drop_backup,
                          lambda _result, error: self._on_backup_dropped(app, error, on_done),
                          app.id, Scope(app.scope))

    def _on_backup_dropped(self, app: InstalledApp, error: BaseException | None,
                           on_done: Done | None) -> None:
        self.reload(force=True)
        if error is None:
            self.add_toast(_("The previous version of {name} was deleted").format(name=app.name))
        elif isinstance(error, AuthorizationError):
            self.add_toast(str(error))
        else:
            log.warning("deleting the previous version of %s failed: %s", app.id, error)
            self.show_error_alert(_("The previous version of {name} could not be deleted").format(
                name=app.name), error)
        if on_done is not None:
            on_done(None, error)

    # ------------------------------------------------------------------------------------------
    # uninstalling (the dialogs are in uninstall_dialog.py)
    # ------------------------------------------------------------------------------------------

    def confirm_uninstall(self, app: InstalledApp) -> UninstallDialog:
        dialog = UninstallDialog(app)
        dialog.connect("response", self._on_uninstall_response, app)
        self._return_focus_after(dialog, app)
        dialog.present(self)
        return dialog

    def _on_uninstall_response(self, dialog: UninstallDialog | None, response: str,
                               app: InstalledApp) -> None:
        if response != RESPONSE_UNINSTALL:
            return
        if self.is_app_busy(app):
            # e.g. "Uninstall…" from the app menu while the app is being updated
            self.add_toast(_("Please wait until Easy Installer has finished with {name}").format(
                name=app.name))
            return
        data = dialog.selected_data if isinstance(dialog, UninstallDialog) else []
        # Decide the wording now: afterwards the app file is always gone.
        self._uninstall(app, data, was_broken=is_broken(app))

    def _uninstall(self, app: InstalledApp, data: list[Path], *, was_broken: bool,
                   keep_permission: bool = False) -> None:
        self._run_for_app(
            app, uninstall_and_trash,
            lambda outcome, error: self._on_uninstalled(app, error, was_broken, outcome, data),
            app.id, Scope(app.scope), data, keep_permission=keep_permission)

    def _on_uninstalled(self, app: InstalledApp, error: BaseException | None,
                        was_broken: bool = False, outcome: UninstallOutcome | None = None,
                        data: list[Path] | None = None) -> None:
        self.reload(force=True)
        if error is None:
            outcome = outcome or UninstallOutcome()
            self._updates.pop(app_key(app), None)
            self._needs_repair.discard(app_key(app))
            if outcome.messages:
                notes_dialog(done_text(app, was_broken), outcome.messages).present(self)
            elif outcome.trashed:
                self.add_toast(result_toast_text(app, outcome, was_broken),
                               button_label=_("Open Trash"), on_button=self.open_trash)
            else:
                self.add_toast(done_text(app, was_broken))
        elif isinstance(error, AuthorizationError) and can_keep_permission(app):
            self._offer_uninstall_keeping_permission(app, error, was_broken, data or [])
        elif isinstance(error, AuthorizationError):
            self.add_toast(str(error))
        elif isinstance(error, NotInstalledError):
            self.add_toast(_("{name} was already uninstalled").format(name=app.name))
        else:
            log.warning("uninstalling %s failed: %s", app.id, error)
            self.show_error_alert(_("{name} could not be uninstalled").format(name=app.name),
                                  error)

    def _offer_uninstall_keeping_permission(self, app: InstalledApp, error: BaseException,
                                            was_broken: bool,
                                            data: list[Path] | None = None) -> Adw.AlertDialog:
        """The password prompt was refused (e.g. a standard user): uninstall all the same?"""
        dialog = keep_permission_dialog(app, error)
        dialog.connect("response", self._on_keep_permission_response, app, was_broken,
                       list(data or []))
        dialog.present(self)
        return dialog

    def _on_keep_permission_response(self, _dialog: Adw.AlertDialog | None, response: str,
                                     app: InstalledApp, was_broken: bool,
                                     data: list[Path] | None = None) -> None:
        if response != RESPONSE_UNINSTALL:
            return
        self._uninstall(app, list(data or []), was_broken=was_broken, keep_permission=True)

    def open_trash(self) -> None:
        Gtk.UriLauncher.new("trash:///").launch(self, None, self._on_trash_opened)

    def _on_trash_opened(self, launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                log.warning("cannot open the trash: %s", exc)
                self.add_toast(_("The trash could not be opened"))

    def request_uninstall(self, app_id: str) -> Adw.AlertDialog:
        """``easy-installer --uninstall ID`` (the "Uninstall…" entry of an app's launcher)."""
        try:
            apps = find_installed(app_id)
        except Exception:
            log.exception("cannot look up %s", app_id)
            apps = []
        if not apps:
            dialog = Adw.AlertDialog(
                heading=_("Nothing to uninstall"),
                body=_("This app is not installed. It may have been uninstalled already."))
            dialog.add_response("close", _("_Close"))
            dialog.present(self)
            return dialog
        # The per-user launcher is the one people see in their menu if both exist.
        app = next((a for a in apps if Scope(a.scope) is Scope.USER), apps[0])
        if self.is_app_busy(app):
            dialog = Adw.AlertDialog(
                heading=_("{name} cannot be uninstalled right now").format(name=app.name),
                body=_("Easy Installer is working on {name} right now (for example, updating "
                       "it). Please try again when it is finished.").format(name=app.name))
            dialog.add_response("close", _("_Close"))
            dialog.present(self)
            return dialog
        return self.confirm_uninstall(app)

    # ------------------------------------------------------------------------------------------
    # updates
    # ------------------------------------------------------------------------------------------

    @property
    def checking(self) -> bool:
        return self._checking

    def check_for_updates(self, *, manual: bool = True) -> None:
        """Ask every app's update source (worker thread). ``manual``: asked for by the user -
        every source is asked now and the result is reported; else only sources whose last
        check is older than the settings' interval, and quietly."""
        self._check_manual = self._check_manual or manual
        if self._checking:
            return
        self._checking = True
        self._set_checking(True)
        run_in_thread(check_all_updates, self._on_updates_checked, force=manual)

    def _set_checking(self, checking: bool) -> None:
        self._check_stack.set_visible_child_name("spinner" if checking else "button")
        self._check_spinner.set_spinning(checking)
        action = self.lookup_action("check-updates")
        if action is not None:
            action.set_enabled(not checking)

    def _on_updates_checked(self, result: UpdateCheckResult | None,
                            error: BaseException | None) -> None:
        manual = self._check_manual
        self._checking = False
        self._check_manual = False
        self._set_checking(False)
        if error is not None or result is None:
            log.warning("checking for updates failed: %s", error)
            if manual:
                self.add_toast(_("Updates could not be checked"))
            return
        for key, problem in result.errors.items():
            log.info("%s/%s could not be checked: %s", *key, problem.details or problem)
        self._updates = merge_updates(self._updates, result)
        self.reload()
        if manual:
            self._report_check(result)

    def _report_check(self, result: UpdateCheckResult) -> None:
        text = check_result_text(result)
        # When nothing could be reached at all, the toast already says why.
        offline = bool(result.errors) and len(result.errors) == result.checked and all(
            isinstance(error, NetworkError) for error in result.errors.values())
        if result.errors and not offline:
            self.add_toast(text, button_label=_("Details"),
                           on_button=lambda: self._show_check_errors(result))
        else:
            self.add_toast(text)

    def _show_check_errors(self, result: UpdateCheckResult) -> Adw.AlertDialog:
        names = {app_key(row.app): row.app.name for row in self._rows}
        lines = [f"{names.get(key, key[1])}: {error}" for key, error in result.errors.items()]
        dialog = Adw.AlertDialog(heading=_("Some apps could not be checked"),
                                 body="\n\n".join(lines))
        dialog.add_response("close", _("_Close"))
        dialog.set_close_response("close")
        dialog.present(self)
        return dialog

    def remember_update(self, app: InstalledApp, update: AvailableUpdate | None) -> None:
        """What a check of one app found (e.g. "Check Now" in its details)."""
        if update is None:
            self._updates.pop(app_key(app), None)
        else:
            self._updates[app_key(app)] = update
        self.reload()

    def _update_updates_banner(self) -> None:
        count = sum(row.update is not None for row in self._rows)
        if count >= UPDATE_ALL_THRESHOLD and not self._update_queue:
            self._updates_banner.set_title(ngettext(
                "{count} update is available", "{count} updates are available",
                count).format(count=count))
            self._updates_banner.set_revealed(True)
        else:
            self._updates_banner.set_revealed(False)

    @property
    def updates_banner_revealed(self) -> bool:
        return self._updates_banner.get_revealed()

    def _installed_now(self, app: InstalledApp) -> InstalledApp | None:
        """The registry's entry of ``app`` as it is now (the window's object may be older)."""
        try:
            return next((entry for entry in find_installed(app.id)
                         if Scope(entry.scope) is Scope(app.scope)), None)
        except Exception:  # noqa: BLE001 - an unreadable registry: go on with what is known
            log.warning("cannot look up %s", app.id, exc_info=True)
            return app

    def start_update(self, app: InstalledApp, update: AvailableUpdate, *,
                     autostart: bool = False,
                     on_done: Callable[[InstalledApp | None], None] | None = None) -> bool:
        """Offer ``update`` for ``app`` in the update dialog (download, install, done).

        The update that is known for the app *now* is offered: an app that updated itself or
        was updated otherwise since ``update`` was found (e.g. while Update All worked on
        another app, or while its details were open) is never taken back to that version.
        False (and ``on_done`` is never called) if something else runs for the app right now,
        or if there is nothing to update any more.
        """
        key = app_key(app)
        if key in self._busy or key in self._updating:
            return False
        current = self._installed_now(app)
        if current is None:
            self.add_toast(_("{name} is no longer installed").format(name=app.name))
            self.reload()
            return False
        wanted = self.update_for(current)
        if wanted is None and has_update_source(current) \
                and is_newer_than_installed(update, current):
            wanted = update
        if wanted is None:
            self.add_toast(_("{name} is already up to date").format(name=current.name))
            self.reload()
            return False
        app, update = current, wanted
        self._updating.add(key)
        dialogs: list[object] = []

        def done(new_app: InstalledApp | None) -> None:
            self._updating.discard(key)
            self._update_cancelled = bool(getattr(next(iter(dialogs), None), "cancelled", False))
            if isinstance(new_app, InstalledApp):
                self._updates.pop(key, None)
                self._needs_repair.discard(key)
            self.reload(force=True)
            if on_done is not None:
                on_done(new_app)

        if UpdateDialog is None:
            self._confirm_update_fallback(app, update, done)
            return True
        dialog = UpdateDialog(self, app, update, autostart=autostart, on_done=done)
        dialogs.append(dialog)
        self._return_focus_after(dialog, app)
        dialog.present(self)
        return True

    def _confirm_update_fallback(self, app: InstalledApp, update: AvailableUpdate,
                                 done: Callable[[InstalledApp | None], None]) -> None:
        """Without the update dialog: a plain confirmation, then a spinner in the row."""
        heading = (_("Update {name} to version {version}?").format(name=app.name,
                                                                    version=update.version)
                   if update.version else _("Update {name}?").format(name=app.name))
        dialog = Adw.AlertDialog(heading=heading, body=_("The new version is downloaded and "
                                                         "installed."))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("update", _("_Update"))
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")
        dialog.set_close_response("cancel")

        def finished(new_app: InstalledApp | None, error: BaseException | None) -> None:
            if error is None:
                self.add_toast(_("{name} was updated").format(name=app.name))
            elif isinstance(error, (AuthorizationError, UpdateCancelled)):
                self.add_toast(str(error))
            else:
                self.show_error_alert(_("{name} could not be updated").format(name=app.name),
                                      error)
            done(new_app if error is None else None)

        def response(_dialog: Adw.AlertDialog, answer: str) -> None:
            if answer != "update":
                done(None)
                return
            self._run_for_app(app, apply_update, finished, app, update)

        dialog.connect("response", response)
        dialog.present(self)

    def _on_update_all_clicked(self, _banner: Adw.Banner) -> None:
        # The banner's access key (Alt+L) also works while a dialog is open: not there.
        if self.get_visible_dialog() is None:
            self.update_all()

    def update_all(self) -> None:
        """Offer every known update, one dialog after the other (each starts right away)."""
        if self._update_queue:
            return
        self._update_queue = [(row.app, row.update) for row in self._rows
                              if row.update is not None and not self.is_app_busy(row.app)]
        self._update_updates_banner()
        self._next_queued_update()

    @property
    def queued_updates(self) -> list[tuple[InstalledApp, AvailableUpdate]]:
        return list(self._update_queue)

    def _next_queued_update(self) -> bool:
        while self._update_queue and not self._closing:
            app, update = self._update_queue.pop(0)
            if self.start_update(app, update, autostart=True,
                                 on_done=self._on_queued_update_done):
                return GLib.SOURCE_REMOVE
            # busy with something else meanwhile (e.g. its details' Repair): the next one
        self._update_queue = []
        self._update_updates_banner()
        return GLib.SOURCE_REMOVE

    def _on_queued_update_done(self, new_app: InstalledApp | None) -> None:
        if new_app is None and self._update_cancelled:
            # Cancelled: the rest waits (the banner offers them again). One that failed does
            # not keep the others from being updated: its dialog said why.
            self._update_queue = []
            self._update_updates_banner()
            return
        GLib.idle_add(self._next_queued_update)

    # ------------------------------------------------------------------------------------------
    # keeping the list true: start-up maintenance and reconcile
    # ------------------------------------------------------------------------------------------

    def _start_maintenance(self) -> None:
        self._maintenance_running = True
        run_in_thread(startup_maintenance, self._on_maintenance_done)

    @property
    def maintenance_running(self) -> bool:
        return self._maintenance_running

    @property
    def reconciling(self) -> bool:
        return self._reconciling

    def _on_maintenance_done(self, report: StartupReport | None,
                             error: BaseException | None) -> None:
        self._maintenance_running = False
        if error is not None or report is None:
            log.warning("the start-up checks failed: %s", error)
            return
        if report.pruned:
            log.info("deleted the old kept versions of %s", ", ".join(a.id for a in report.pruned))
        self._updates = {**report.updates, **self._updates}
        self._handle_reconcile(report.results)
        self.reload()
        # What the preferences say now counts (they may have changed while the checks ran).
        if self._auto_check and (report.check_due or self._check_due()):
            self.check_for_updates(manual=False)

    def _reconcile_changed_apps(self) -> None:
        """The window got the focus: bring apps whose file changed up to date (worker thread)."""
        if self._reconciling or self._maintenance_running:
            return
        stale = []
        for row in self._rows:
            if self.is_app_busy(row.app) or self.needs_repair(row.app):
                continue
            try:
                if needs_reconcile(row.app):
                    stale.append(row.app)
            except OSError:
                continue
        if not stale:
            return
        self._reconciling = True
        run_in_thread(reconcile_apps, self._on_apps_reconciled, stale)

    def _on_apps_reconciled(self, results: list[ReconcileResult] | None,
                            error: BaseException | None) -> None:
        self._reconciling = False
        if error is not None:
            log.warning("checking the changed apps failed: %s", error)
            return
        self._handle_reconcile(results or [])
        self.reload()

    def _handle_reconcile(self, results: Iterable[ReconcileResult]) -> None:
        results = list(results)
        text = self_update_text(results)
        if text:
            self.add_toast(text)
        newly: list[InstalledApp] = []
        for result in results:
            key = app_key(result.app)
            if result.change == CHANGE_NEEDS_ADMIN:
                self._needs_repair.add(key)
                if key not in self._announced_repair:
                    self._announced_repair.add(key)
                    newly.append(result.app)
            else:
                self._needs_repair.discard(key)
        if len(newly) == 1:
            app = newly[0]
            self.add_toast(_("{name} changed since it was installed").format(name=app.name),
                           timeout=10, button_label=_("Repair"),
                           on_button=lambda: self._repair_by_key(app))
        elif newly:
            self.add_toast(ngettext("{count} app changed and needs a repair",
                                    "{count} apps changed and need a repair",
                                    len(newly)).format(count=len(newly)), timeout=10)

    def _repair_by_key(self, app: InstalledApp) -> None:
        row = self._row_for(app)
        if row is not None and not row.busy:
            self.repair(row.app)

    # ------------------------------------------------------------------------------------------
    # preferences
    # ------------------------------------------------------------------------------------------

    def show_preferences(self) -> PreferencesDialog:
        dialog = PreferencesDialog(on_changed=self._on_settings_changed)
        dialog.present(self)
        return dialog

    def _check_due(self) -> bool:
        """A quiet automatic check is due for one of the listed apps (no network)."""
        try:
            return updates_due([row.app for row in self._rows],
                               load_settings().update_interval_hours)
        except Exception:  # noqa: BLE001
            return False

    def _on_settings_changed(self, prefs: Settings) -> None:
        switched_on = prefs.check_updates and not self._auto_check
        self._auto_check = prefs.check_updates
        if switched_on and not self._checking and not self._maintenance_running:
            # Automatic checks were just switched on: look where one is due (quietly). (While
            # the start-up checks run, their end decides.)
            if self._check_due():
                self.check_for_updates(manual=False)

    # ------------------------------------------------------------------------------------------
    # banners
    # ------------------------------------------------------------------------------------------

    def _on_status_loaded(self, status: SystemStatus | None, error: BaseException | None) -> None:
        if error is not None:
            log.warning("system check failed: %s", error)
            return
        self.status = status
        self._update_banners()
        # FUSE may have been installed in the meantime (e.g. with apt): per-user apps then no
        # longer need to unpack themselves on every start.
        run_in_thread(speed_up_apps, self._on_apps_sped_up, status, system=False)

    def _on_apps_sped_up(self, updated: list[InstalledApp] | None,
                         error: BaseException | None) -> None:
        if error is not None:
            log.warning("updating the start mode of the apps failed: %s", error)
        if updated:
            self.reload(force=True)

    def _update_banners(self) -> None:
        dismissed = bool(settings.get_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED, False))
        try:
            offer = not dismissed and can_offer_default_handler()
        except Exception:
            log.debug("cannot check the default AppImage handler", exc_info=True)
            offer = False
        self._handler_banner.set_revealed(offer)

        status = self.status
        fuse_missing = status is not None and not status.libfuse2
        # Not "_Install": the install dialog's button shows over this window with that access key.
        self._fuse_banner.set_button_label(_("In_stall") if fuse_missing and status.has_apt
                                           else None)
        self._fuse_banner.set_revealed(fuse_missing)

    def _on_make_default(self, _banner: Adw.Banner) -> None:
        if self.get_visible_dialog() is not None:
            return   # its access key (Alt+M) reached the banner behind a dialog
        try:
            make_default_handler()
        except GLib.Error as exc:
            log.warning("cannot make %s the default for AppImages: %s", DESKTOP_ID, exc)
            self.add_toast(_("Easy Installer could not be made the default app for AppImages"))
            return
        self._handler_banner.set_revealed(False)
        self.add_toast(_("AppImages now open with Easy Installer"))

    def _on_dismiss_handler_banner(self, _button: Gtk.Button) -> None:
        settings.set_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED, True)
        self._handler_banner.set_revealed(False)

    def _on_install_fuse(self, _banner: Adw.Banner) -> None:
        if self.get_visible_dialog() is None:   # (not by its access key behind a dialog)
            self.install_fuse()

    def install_fuse(self) -> None:
        """Install the missing FUSE library with the helper (asks for the password)."""
        self._fuse_banner.set_sensitive(False)
        run_in_thread(privileged.run_helper, self._on_fuse_installed, "install-fuse", {})

    def _on_fuse_installed(self, _result: object, error: BaseException | None) -> None:
        self._fuse_banner.set_sensitive(True)
        if error is None:
            self.add_toast(_("The missing system component was installed"))
            run_in_thread(self._refresh_after_fuse, self._on_fuse_refreshed)
        elif isinstance(error, AuthorizationError):
            self.add_toast(str(error))
        else:
            log.warning("installing FUSE failed: %s", error)
            self.show_error_alert(_("The system component could not be installed"), error)

    @staticmethod
    def _refresh_after_fuse() -> tuple[SystemStatus, list[InstalledApp]]:
        """Worker: re-check the system; installed apps stop unpacking themselves on every start."""
        status = get_system_status(refresh=True)
        return status, speed_up_apps(status, system=True)

    def _on_fuse_refreshed(self, result: tuple | None, error: BaseException | None) -> None:
        if error is not None or result is None:
            log.warning("checking the system after installing FUSE failed: %s", error)
            return
        self.status, updated = result
        self._update_banners()
        if updated:
            self.reload(force=True)
            self.add_toast(ngettext("{count} app now starts faster", "{count} apps now start faster",
                                    len(updated)).format(count=len(updated)))

    # ------------------------------------------------------------------------------------------
    # about & system check
    # ------------------------------------------------------------------------------------------

    def show_about(self) -> Adw.AboutDialog:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            developer_name="roothirsch",
            copyright="© 2026 roothirsch",
            comments=_("Install downloaded AppImage apps with one click. Easy Installer puts "
                       "them in a safe place, adds them to your app menu and search, finds "
                       "updates for them, and removes them again when you no longer need them."),
            debug_info=debug_info(self.status),
            debug_info_filename="easy-installer-debug-info.txt",
        )
        credits = _("translator-credits")
        if credits != "translator-credits":
            about.set_translator_credits(credits)
        about.present(self)
        return about

    def show_system_check(self) -> None:
        """Re-check the system in the background, then show the results."""
        run_in_thread(get_system_status, self._on_system_checked, True)

    def _on_system_checked(self, status: SystemStatus | None, error: BaseException | None) -> None:
        if error is not None or status is None:
            log.warning("system check failed: %s", error)
            self.add_toast(_("This computer could not be checked"))
            return
        self.status = status
        self._update_banners()
        SystemCheckDialog(status, install_fuse=self.install_fuse).present(self)

    # ------------------------------------------------------------------------------------------
    # drag and drop
    # ------------------------------------------------------------------------------------------

    def _on_drop_enter(self, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        self._drop_overlay.set_visible(True)
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, _target: Gtk.DropTarget) -> None:
        self._drop_overlay.set_visible(False)

    def _on_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        self._drop_overlay.set_visible(False)
        files = value.get_files() if isinstance(value, Gdk.FileList) else []
        if not files:
            return False
        self.install_files(files)
        return True

    def set_drop_overlay_visible(self, visible: bool) -> None:
        self._drop_overlay.set_visible(visible)

    # ------------------------------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------------------------------

    def _busy_dialog(self) -> Adw.Dialog | None:
        """An open dialog that is in the middle of something (installing, updating)."""
        dialogs = self.get_dialogs()
        for index in range(dialogs.get_n_items()):
            dialog = dialogs.get_item(index)
            if getattr(dialog, "busy", False):
                return dialog
        return None

    def _refuse_closing(self) -> bool:
        """Tell why the window cannot close now; False if it can."""
        dialog = self._busy_dialog()
        if dialog is not None:
            text = (_("Please wait until the installation is finished")
                    if isinstance(dialog, InstallDialog)
                    else _("Please wait until the update is finished"))
            add_toast = getattr(dialog, "add_toast", None)
            (add_toast if callable(add_toast) else self.add_toast)(text)
            return True
        if self._busy:
            self.add_toast(_("Please wait until Easy Installer has finished"))
            return True
        return False

    def _on_close_action(self, *_args: object) -> None:
        """Ctrl+W: close the topmost dialog, or the window if there is none."""
        dialog = self.get_visible_dialog()
        if dialog is not None:
            dialog.close()
        else:
            self.close()

    def _on_close_request(self, *_args: object) -> bool:
        return self._refuse_closing()

    def _on_any_close_request(self, window: Gtk.Window, *_args: object) -> bool:
        """Emission hook: runs before libadwaita's own close-request handler.

        With a dialog open, libadwaita answers a close request (Alt+F4, the title bar) by closing
        only the topmost dialog. If that is the install dialog, the files still waiting must not
        pop up one after the other then; an alert on top of it just closes.
        """
        if window is self and not self.is_busy and self.current_dialog is not None \
                and self.get_visible_dialog() is self.current_dialog:
            self._closing = True
            self._queue.clear()
        return True  # keep the hook

    def _on_destroy(self, *_args: object) -> None:
        if self._close_hook:
            GObject.remove_emission_hook(Gtk.Window, "close-request", self._close_hook)
            self._close_hook = 0

    def request_quit(self) -> None:
        """Quit (Ctrl+Q, the dock's Quit): also with a dialog open or more files waiting.

        ``close()`` would only close the open dialog (libadwaita) and the next queued file would
        be offered. Refuses (with a hint) while something is being installed.
        """
        if self._refuse_closing():
            return
        self._closing = True
        self._queue.clear()
        self._update_queue.clear()
        dialogs = self.get_dialogs()
        for dialog in [dialogs.get_item(i) for i in range(dialogs.get_n_items())]:
            dialog.force_close()
        self.destroy()

    @property
    def is_busy(self) -> bool:
        return bool(self._busy) or self._busy_dialog() is not None or (
            self.current_dialog is not None and self.current_dialog.busy)
