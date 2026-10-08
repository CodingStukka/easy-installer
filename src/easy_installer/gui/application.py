"""The Adw.Application: command line, actions, shortcuts and the main window."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Sequence
from html import escape as xml_escape
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME
from ..i18n import _
from . import MIN_ADW_VERSION
from .common import load_css
from .window import MainWindow

log = logging.getLogger(__name__)

#: Icons of a source checkout (``data/icons``), used when Easy Installer is not installed.
_SOURCE_ICONS = Path(__file__).resolve().parents[3] / "data" / "icons"

ACCELS = {
    "app.open": ["<Control>o"],
    "app.quit": ["<Control>q"],
    "app.preferences": ["<Control>comma"],
    "win.close": ["<Control>w"],
    "win.check-updates": ["<Control>r"],
    "win.show-help-overlay": ["<Control>question"],
}


def _shortcuts_window() -> Gtk.ShortcutsWindow:
    shortcuts = [
        (_("Install an app"), "<Control>o"),
        (_("Check for updates"), "<Control>r"),
        (_("Preferences"), "<Control>comma"),
        (_("Close the dialog or window"), "<Control>w"),
        (_("Quit"), "<Control>q"),
        (_("Main menu"), "F10"),
        (_("Keyboard shortcuts"), "<Control>question"),
    ]
    items = "".join(
        '<child><object class="GtkShortcutsShortcut">'
        f'<property name="title">{xml_escape(title)}</property>'
        f'<property name="accelerator">{xml_escape(accel)}</property>'
        "</object></child>"
        for title, accel in shortcuts)
    ui = (
        '<interface><object class="GtkShortcutsWindow" id="help_overlay">'
        '<property name="modal">True</property>'
        '<child><object class="GtkShortcutsSection"><property name="section-name">main</property>'
        '<child><object class="GtkShortcutsGroup">'
        f'<property name="title">{xml_escape(_("General"))}</property>'
        f"{items}</object></child></object></child></object></interface>"
    )
    return Gtk.Builder.new_from_string(ui, -1).get_object("help_overlay")


class EasyInstallerApp(Adw.Application):
    __gtype_name__ = "EasyInstallerApp"

    def __init__(self, *, flags: Gio.ApplicationFlags = Gio.ApplicationFlags.FLAGS_NONE):
        super().__init__(
            application_id=APP_ID,
            flags=flags | Gio.ApplicationFlags.HANDLES_OPEN
            | Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.add_main_option("uninstall", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             _("Ask whether to uninstall the app with this ID"), _("ID"))
        self.set_option_context_parameter_string(_("[FILE…]"))
        self.set_option_context_summary(_("Install AppImage apps like real apps."))

    # -- life cycle -----------------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        load_css()
        display = Gdk.Display.get_default()
        if _SOURCE_ICONS.is_dir() and display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(_SOURCE_ICONS))

        for name, callback in (
            ("open", self._on_open),
            ("about", self._on_about),
            ("quit", self._on_quit),
            ("check-system", self._on_check_system),
            ("preferences", self._on_preferences),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        for action_name, accels in ACCELS.items():
            self.set_accels_for_action(action_name, accels)

    def do_activate(self) -> None:
        self.main_window().present()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        window = self.main_window()
        window.present()
        window.install_files(files)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict()
        uninstall_id = options.lookup_value("uninstall", GLib.VariantType.new("s"))
        args = [a for a in command_line.get_arguments()[1:] if a != "--"]
        files = [command_line.create_file_for_arg(a) for a in args]

        window = self.main_window()
        window.present()
        if files:
            window.install_files(files)
        if uninstall_id is not None and uninstall_id.get_string():
            window.request_uninstall(uninstall_id.get_string())
        return 0

    # -- windows --------------------------------------------------------------------------------

    def main_window(self) -> MainWindow:
        for window in self.get_windows():
            if isinstance(window, MainWindow):
                return window
        window = MainWindow(self)
        # Our own "win.show-help-overlay" instead of Gtk.ApplicationWindow.set_help_overlay(),
        # which connects the deprecated GtkWindow::keys-changed signal in GTK 4.14.
        action = Gio.SimpleAction.new("show-help-overlay", None)
        action.connect("activate", lambda *_args: self._show_shortcuts(window))
        window.add_action(action)
        return window

    def _show_shortcuts(self, window: MainWindow) -> None:
        try:
            shortcuts = _shortcuts_window()
        except GLib.Error:
            log.warning("cannot build the keyboard shortcuts window", exc_info=True)
            return
        shortcuts.set_transient_for(window)
        shortcuts.present()

    # -- actions --------------------------------------------------------------------------------

    def _on_open(self, *_args: object) -> None:
        window = self.main_window()
        window.present()
        window.choose_files()

    def _on_about(self, *_args: object) -> None:
        self.main_window().show_about()

    def _on_check_system(self, *_args: object) -> None:
        self.main_window().show_system_check()

    def _on_preferences(self, *_args: object) -> None:
        window = self.main_window()
        window.present()
        window.show_preferences()

    def _on_quit(self, *_args: object) -> None:
        windows = [w for w in self.get_windows() if isinstance(w, MainWindow)]
        if not windows:
            self.quit()
            return
        for window in windows:
            window.request_quit()  # refuses (with a hint) while something is being installed


def adw_supported() -> bool:
    return (Adw.get_major_version(), Adw.get_minor_version()) >= MIN_ADW_VERSION


_VERBOSE_RE = re.compile(r"^(?:-v+|--verbose)$")


def split_verbosity(argv: Sequence[str]) -> tuple[list[str], int]:
    """Remove ``-v``/``-vv``/``--verbose`` (accepted like in the CLI) before GApplication sees them."""
    args = list(argv)
    rest, verbosity, options_ended = args[:1], 0, False
    for arg in args[1:]:
        if not options_ended and _VERBOSE_RE.match(arg):
            verbosity += 1 if arg == "--verbose" else len(arg) - 1
            continue
        options_ended = options_ended or arg == "--"
        rest.append(arg)
    return rest, verbosity


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    if verbosity > 0 or root.level == logging.NOTSET:
        root.setLevel(level)


def run(argv: Sequence[str] | None = None) -> int:
    """Start the GUI. ``argv`` includes the program name (like ``sys.argv``)."""
    args, verbosity = split_verbosity(sys.argv if argv is None else argv)
    _setup_logging(verbosity)
    if not adw_supported():
        log.critical(
            "Easy Installer needs libadwaita %d.%d or newer, but this computer has %d.%d.",
            *MIN_ADW_VERSION, Adw.get_major_version(), Adw.get_minor_version())
        return 1
    # Lets GNOME Shell match the window to com.roothirsch.EasyInstaller.desktop on X11.
    GLib.set_prgname(APP_ID)
    app = EasyInstallerApp()
    return app.run(args or ["easy-installer"])
