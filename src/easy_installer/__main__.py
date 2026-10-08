"""Entry point: ``easy-installer`` opens the window, ``easy-installer <command>`` runs the CLI.

The GUI is imported lazily, so the command line works on computers without GTK.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import shlex
import sys
from collections.abc import Sequence

from .i18n import _

log = logging.getLogger(__name__)

PROG = "easy-installer"
CLI_ARGUMENTS = frozenset({
    "install", "info", "list", "update", "rollback", "details", "uninstall", "repair", "launch",
    "settings", "check", "--help", "-h", "--version",
})
#: Options of the window (see gui/application.py); "--" ends the options before file names.
GUI_OPTIONS = frozenset({"--uninstall", "--"})
MIN_ADW_VERSION = (1, 5)
GUI_MODULE = "easy_installer.gui.application"

_VERBOSE_RE = re.compile(r"^(?:-v+|--verbose)$")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def looks_like_file(arg: str) -> bool:
    """A file for the window: it exists, is a path or a URI, or is called ``*.AppImage``."""
    if not arg or arg.startswith("-"):
        return False
    if "/" in arg or arg.lower().endswith(".appimage") or _URI_RE.match(arg):
        return True
    try:
        return os.path.lexists(arg)
    except (OSError, ValueError):
        return False


def is_cli_invocation(argv: Sequence[str]) -> bool:
    """False if the window should open: no arguments, files or a window option (``--uninstall``).

    Leading -v/--verbose flags are skipped. Everything else - the CLI commands, but also
    mistyped ones like "remove" or "help" - goes to the command line, which explains the
    mistake instead of opening an install dialog for a "file" called "remove".
    """
    for arg in argv[1:]:
        if _VERBOSE_RE.match(arg):
            continue
        if arg in CLI_ARGUMENTS:
            return True
        return not (arg.split("=", 1)[0] in GUI_OPTIONS or looks_like_file(arg))
    return False


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv) or [PROG]
    if is_cli_invocation(argv):
        from . import cli

        return cli.main(argv)
    return run_gui(argv)


# ------------------------------------------------------------------------------------------------
# GUI
# ------------------------------------------------------------------------------------------------


def gui_problem() -> str | None:
    """Why the window cannot be shown (translated), or None if GTK 4 + libadwaita are usable."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gtk  # noqa: F401  (importing Gtk initialises it)
    except (ImportError, ValueError) as exc:
        log.debug("GTK 4 / libadwaita not available: %s", exc)
        return _("The window toolkit it needs (GTK 4 and libadwaita) is not installed.")
    found = (Adw.get_major_version(), Adw.get_minor_version())
    if found < MIN_ADW_VERSION:
        return _("The installed libadwaita {found} is too old; version {needed} or newer is "
                 "needed.").format(found="{}.{}".format(*found),
                                   needed="{}.{}".format(*MIN_ADW_VERSION))
    if Gdk.Display.get_default() is None:
        return _("No graphical desktop was found.")
    return None


def _cli_suggestions(argv: Sequence[str]) -> list[str]:
    args = list(argv[1:])
    if len(args) >= 2 and args[0] == "--uninstall":
        return [f"{PROG} uninstall {shlex.quote(args[1])}"]
    files = [a for a in args if looks_like_file(a) and not _URI_RE.match(a)]
    if files:
        return [f"{PROG} install {shlex.quote(f)}" for f in files]
    return [f"{PROG} install FILE.AppImage", f"{PROG} list"]


def _print_gui_unavailable(problem: str, argv: Sequence[str]) -> None:
    lines = [
        _("Easy Installer cannot open its window: {problem}").format(problem=problem),
        _("You can still use Easy Installer in the terminal, for example:"),
        *(f"  {command}" for command in _cli_suggestions(argv)),
        f"  {PROG} --help",
    ]
    sys.stderr.write("\n".join(lines) + "\n")


def run_gui(argv: Sequence[str]) -> int:
    problem = gui_problem()
    application = None
    if problem is None:
        try:
            application = importlib.import_module(GUI_MODULE)
        except Exception:  # ImportError, gi version errors, or a broken installation
            log.debug("cannot load %s", GUI_MODULE, exc_info=True)
            problem = _("Its window could not be loaded.")
    if problem is not None or application is None:
        _print_gui_unavailable(problem or "", argv)
        return 1
    return int(application.run(list(argv)) or 0)


if __name__ == "__main__":
    sys.exit(main())
