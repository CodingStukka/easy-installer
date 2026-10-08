"""Command-line interface: install, inspect, update, list, uninstall and start apps from a terminal.

``main(argv)`` receives the full ``sys.argv``-style list (``argv[0]`` is the program name) and
returns the exit code: 0 ok, 1 error, 2 wrong usage, 3 cancelled (answer "no", Ctrl+C or a
dismissed password prompt).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import difflib
import gettext
import json
import locale
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import APP_NAME, __version__
from .core import (
    appdata,
    backups,
    downloads,
    inspector,
    installer,
    portable,
    reconcile,
    settings,
    system_checks,
    updater,
    updates,
)
from .core.appdata import DataLocation
from .core.desktop_entry import DEPRECATED_FIELD_CODES, FIELD_CODES, DesktopEntry, split_exec
from .core.elf import host_arch, is_foreign_arch
from .core.inspector import AppImageInfo, arch_label
from .core.installer import (
    ACTION_DOWNGRADE,
    ACTION_REINSTALL,
    ACTION_ROLLBACK,
    ACTION_UPDATE,
    InstallOptions,
    InstallPlan,
    display_text,
)
from .core.integration import compare_versions
from .core.origin import origin_host
from .core.paths import Scope, home_dir, settings_path
from .core.portable import ExecutableCandidate, PortableInfo
from .core.reconcile import ReconcileResult
from .core.registry import (
    KIND_PORTABLE,
    STATUS_MISSING_APPIMAGE,
    STATUS_MISSING_LAUNCHER,
    InstalledApp,
)
from .core.sandbox import NO_SANDBOX_ARG, SandboxFix, default_sandbox_fix, needs_sandbox_fix
from .core.signature import STATUS_INVALID, STATUS_VALID, SignatureInfo
from .core.system_checks import SystemStatus
from .core.updates import AvailableUpdate, UpdateCache
from .errors import (
    AuthorizationError,
    EasyInstallerError,
    UnsupportedArchitectureError,
    UpdateCancelled,
    UpdateError,
)
from .i18n import N_, _, ngettext

log = logging.getLogger(__name__)

PROG = "easy-installer"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3

SANDBOX_CHOICES: dict[str, SandboxFix | None] = {
    "auto": None,
    "apparmor": SandboxFix.APPARMOR,
    "no-sandbox": SandboxFix.NO_SANDBOX,
    "none": SandboxFix.NONE,
}
EXTRACT_CHOICES: dict[str, bool | None] = {"auto": None, "yes": True, "no": False}

#: Programs of an archive whose score is at most this much below the best one are offered as a
#: choice ("UVtools.sh" next to "UVtools"); see ``core.portable.score_executable``.
SIMILAR_SCORE_MARGIN = 10

EXTRACT_AND_RUN_VAR = "APPIMAGE_EXTRACT_AND_RUN"
_ALL_FIELD_CODES = FIELD_CODES | DEPRECATED_FIELD_CODES
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VERBOSE_FORMAT = "%(levelname)s %(name)s: %(message)s"
_DEFAULT_FORMAT = "%(levelname)s: %(message)s"
_REGISTRY_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SECONDS_PER_DAY = 24 * 3600

# argparse's own texts ("usage:", "options", ...). Listed here so they end up in our
# translation catalog; see _install_argparse_translations().
_ARGPARSE_MESSAGES = (
    N_("usage: "),
    N_("positional arguments"),
    N_("options"),
    N_("show this help message and exit"),
    N_("%(prog)s: error: %(message)s\n"),
    N_("the following arguments are required: %s"),
    N_("invalid choice: %(value)r (choose from %(choices)s)"),
    N_("unrecognized arguments: %s"),
    N_("expected one argument"),
    N_("argument %(argument_name)s: %(message)s"),
    N_("not allowed with argument %s"),
    N_("ambiguous option: %(option)s could match %(matches)s"),
)


# ------------------------------------------------------------------------------------------------
# terminal output helpers
# ------------------------------------------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text, file=sys.stdout)


def _err(text: str = "") -> None:
    print(text, file=sys.stderr)


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _is_tty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _use_color(stream: TextIO) -> bool:
    return _is_tty(stream) and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _paint(text: str, code: str, stream: TextIO | None = None) -> str:
    return f"\033[{code}m{text}\033[0m" if _use_color(stream or sys.stdout) else text


def _mark_ok(stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    return _paint("✓" if _can_encode(stream, "✓") else "OK", "32", stream)


def _mark_bad(stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    return _paint("✗" if _can_encode(stream, "✗") else "X", "31", stream)


def _mark_warn(stream: TextIO | None = None) -> str:
    return _paint("!", "33", stream or sys.stdout)


def _bold(text: str) -> str:
    return _paint(text, "1")


def _text_width(text: str) -> int:
    """Number of terminal columns ``text`` occupies (wide CJK characters count twice)."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _text_width(text))


def _terminal_columns() -> int:
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 110))


def _print_fields(rows: Sequence[tuple[str, str | None]], indent: str = "  ") -> None:
    """Aligned "Label:  value" lines; rows without a value are skipped."""
    rows = [(label, value) for label, value in rows if value]
    width = max((_text_width(label) for label, _value in rows), default=0)
    for label, value in rows:
        lines = str(value).splitlines() or [""]
        _out(f"{indent}{_pad(label, width)}  {lines[0]}")
        for extra in lines[1:]:
            _out(f"{indent}{' ' * width}  {extra}")


_NO_BREAK_SPACE = "\u00a0"


def _wrap(text: str, width: int) -> list[str]:
    """Lines of at most ``width`` columns. Paths and a command at the end of a sentence
    ("…, run: easy-installer update demo") are never split, so they can be copied as they are."""
    start = text.rfind(PROG + " ")
    if start >= 0 and _NO_BREAK_SPACE not in text:
        text = text[:start] + text[start:].replace(" ", _NO_BREAK_SPACE)
    lines = textwrap.wrap(text, width=max(width, 20), break_on_hyphens=False,
                          break_long_words=False) or [""]
    return [line.replace(_NO_BREAK_SPACE, " ") for line in lines]


def _print_notes(title: str, notes: Sequence[str]) -> None:
    if not notes:
        return
    _out(title)
    width = _terminal_columns()
    for note in notes:
        wrapped = _wrap(note, width - 4)
        _out(f"  {_mark_warn()} {wrapped[0]}")
        for line in wrapped[1:]:
            _out(f"    {line}")


def _print_wrapped(text: str, indent: str = "") -> None:
    for line in _wrap(text, _terminal_columns() - len(indent)):
        _out(f"{indent}{line}")


def _print_json(data: Any) -> None:
    _out(json.dumps(data, indent=2, ensure_ascii=False))


def _format_size(size: int) -> str:
    megabytes = size / 1_000_000
    if 0 < megabytes < 0.1:
        megabytes = 0.1
    # Decimal separator of the user's number format (LC_NUMERIC), e.g. "83,7 MB" in German.
    return _("{size} MB").format(size=locale.format_string("%.1f", megabytes))


def _size_text(size: int | None) -> str | None:
    """A size for display, None when it is not known (0)."""
    return _format_size(size) if size else None


def _yes_no(value: bool) -> str:
    return _("yes") if value else _("no")


def _version_text(version: str | None) -> str:
    return version or _("unknown")


def _scope_text(scope: Scope) -> str:
    return _("Only for me") if Scope(scope) is Scope.USER else _("Everyone on this computer")


def _scope_short(scope: Scope) -> str:
    return _("Only me") if Scope(scope) is Scope.USER else _("Everyone")


def _command(*args: str) -> str:
    return " ".join([PROG, *(shlex.quote(a) for a in args)])


def _short_path(path: str | os.PathLike | None) -> str:
    """A path for display: the home folder shown as "~", names that are not UTF-8 with U+FFFD."""
    if not path:
        return ""
    text = os.fspath(path)
    home = os.fspath(home_dir()).rstrip("/")
    if home and (text == home or text.startswith(home + "/")):
        text = "~" + text[len(home):]
    return display_text(text)


def _local_time(stamp: str | None) -> str | None:
    """A registry time stamp ("2026-09-29T15:04:05Z") in local time: "2026-09-29 17:04"."""
    if not stamp:
        return None
    try:
        when = datetime.strptime(stamp, _REGISTRY_TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return display_text(stamp)
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def _local_date(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")


def _fingerprint_text(fingerprint: str | None) -> str | None:
    if not fingerprint:
        return None
    return " ".join(fingerprint[i:i + 4] for i in range(0, len(fingerprint), 4))


def _signature_of(value: object) -> SignatureInfo | None:
    return value if isinstance(value, SignatureInfo) else SignatureInfo.from_dict(value)


def _signature_text(value: object, installed: InstalledApp | None = None) -> str | None:
    """What is known about a signature, in a line or two; None when the file is not signed.
    Who made the file is only said when its key is the one of the installed version
    (``installed``): the key travels inside the file, and anyone can give a key any name."""
    info = _signature_of(value)
    if info is None:
        return None
    reference = _signature_of(installed.signature) if installed is not None else None
    if info.status == STATUS_VALID and reference is not None \
            and reference.status == STATUS_VALID and reference.fingerprint \
            and reference.fingerprint == info.fingerprint:
        lines = [_("signed by the same maker as the installed version")]
    elif info.status == STATUS_VALID:
        lines = [_("signed with a key named “{signer}”").format(signer=info.signer)
                 if info.signer else _("signed with a key that has no name")]
    elif info.status == STATUS_INVALID:
        lines = [_("the signature does not match"), info.explanation or ""]
    else:
        lines = [_("signed, but the signature could not be checked"), info.explanation or ""]
    key = _fingerprint_text(info.fingerprint)
    if key:
        lines.append(_("key {fingerprint}").format(fingerprint=key))
    return "\n".join(line for line in lines if line)


def _origin_text(url: str | None, *, full: bool = False) -> str | None:
    host = origin_host(url)
    if host is None:
        return None
    return f"{host}\n{display_text(url or '')}" if full and url else host


def _data_kind_text(kind: str) -> str:
    texts = {
        "config": _("settings"),
        "data": _("app data"),
        "cache": _("temporary files"),
        "state": _("app state"),
        "home": _("settings and data"),
    }
    return texts.get(kind, kind)


# ------------------------------------------------------------------------------------------------
# progress
# ------------------------------------------------------------------------------------------------


class ProgressLine:
    """A single self-updating status line on stderr; silent unless stderr is a terminal.

    Usable directly as the ``progress(fraction, message)`` callback of the core functions.
    """

    BAR_WIDTH = 24

    def __init__(self, stream: TextIO | None = None, *, enabled: bool | None = None):
        self._stream = stream
        self.enabled = _is_tty(self.stream) if enabled is None else enabled
        self._last: tuple[int | None, str] | None = None
        self._visible = False

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def __call__(self, fraction: float | None, message: str) -> None:
        if not self.enabled:
            return
        percent = None if fraction is None else max(0, min(100, int(fraction * 100)))
        if (percent, message) == self._last:
            return
        self._last = (percent, message)
        self._draw(percent, message)

    def _draw(self, percent: int | None, message: str) -> None:
        if percent is None:
            line = f"  {message}"
        else:
            full, empty = ("█", "░") if _can_encode(self.stream, "█░") else ("#", "-")
            filled = round(self.BAR_WIDTH * percent / 100)
            line = f"  {full * filled}{empty * (self.BAR_WIDTH - filled)} {percent:3d}%  {message}"
        columns = shutil.get_terminal_size((80, 24)).columns
        self.stream.write("\r\033[K" + line[: max(columns - 1, 20)])
        self.stream.flush()
        self._visible = True

    def clear(self) -> None:
        """Remove the status line (before printing anything else)."""
        if self._visible:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self._visible = False
        self._last = None


class _StderrLogHandler(logging.Handler):
    """Log records to the *current* ``sys.stderr``, clearing the progress line first."""

    def __init__(self, progress: ProgressLine, level: int):
        super().__init__(level)
        self.progress = progress

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.progress.clear()
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:  # never let logging break the command
            self.handleError(record)


def _setup_logging(verbosity: int, progress: ProgressLine) -> Callable[[], None]:
    """Route log messages to stderr (-v: info, -vv: debug). Returns a function undoing it."""
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handler = _StderrLogHandler(progress, level)
    handler.setFormatter(logging.Formatter(_VERBOSE_FORMAT if verbosity >= 2 else _DEFAULT_FORMAT))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(level)

    def restore() -> None:
        root.removeHandler(handler)
        root.setLevel(old_level)

    return restore


# ------------------------------------------------------------------------------------------------
# questions
# ------------------------------------------------------------------------------------------------


def _yes_words() -> set[str]:
    # Translators: the short answer for "yes" accepted at [Y/n] questions (lower case).
    return {"y", "yes", _("y").casefold(), _("yes").casefold()}


def _no_words() -> set[str]:
    # Translators: the short answer for "no" accepted at [Y/n] questions (lower case).
    return {"n", "no", _("n").casefold(), _("no").casefold()}


def _ask(prompt: str) -> str | None:
    """One line of input, or None if there is nothing to read (stdin closed or missing)."""
    try:
        if sys.stdin is None:  # started with a closed stdin (`<&-`)
            raise EOFError
        answer = input(prompt)
    except (EOFError, RuntimeError):  # RuntimeError: "input(): lost sys.stdin"
        _out()
        _err(_("No answer was given. Add -y to skip the question."))
        return None
    if not _is_tty(sys.stdin):
        _out(answer)  # piped answers are not echoed by the terminal; keep the output readable
    return answer


def confirm(prompt: str, *, default: bool) -> bool:
    """Ask a yes/no question; an empty answer means ``default``, no input at all means no."""
    while True:
        answer = _ask(prompt)
        if answer is None:
            return False
        answer = answer.strip().casefold()
        if not answer:
            return default
        if answer in _yes_words():
            return True
        if answer in _no_words():
            return False
        _out(_("Please answer y (yes) or n (no)."))


def _choose_number(prompt: str, count: int, *, default: int | None) -> int | None:
    """Ask for one of the numbers 1..count; Enter means ``default`` (None: cancel)."""
    choices = [str(n) for n in range(1, count + 1)]
    while True:
        answer = _ask(prompt)
        if answer is None:
            return None
        answer = answer.strip()
        if not answer:
            return default
        if answer in choices:
            return int(answer)
        _out(_("Please type one of: {choices}").format(choices=", ".join(choices)))


# ------------------------------------------------------------------------------------------------
# argument parsing
# ------------------------------------------------------------------------------------------------


def _argparse_gettext(message: str) -> str:
    translated = _(message)
    return translated if translated != message else gettext.dgettext("argparse", message)


def _install_argparse_translations() -> None:
    """Let argparse's own texts ("usage:", "options", ...) use our translations when we have them."""
    argparse._ = _argparse_gettext  # type: ignore[attr-defined]


def _id_help() -> str:
    return _("the ID of the app (see: {command})").format(command=_command("list"))


def _add_scope_options(parser: argparse.ArgumentParser, user_help: str, system_help: str) -> None:
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--user", action="store_true", help=user_help)
    which.add_argument("--system", action="store_true", help=system_help)


def build_parser() -> argparse.ArgumentParser:
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS,
                         help=_("show more details, for example the technical cause of an error "
                                "(use -vv for even more)"))

    parser = argparse.ArgumentParser(
        prog=PROG,
        description=_("Install AppImage apps so they appear in your app menu, update them, and "
                      "remove them again. Start {prog} without a command to open its window."
                      ).format(prog=PROG),
        epilog=_("Examples:\n"
                 "  {prog} install ~/Downloads/MyApp.AppImage\n"
                 "  {prog} list\n"
                 "  {prog} uninstall myapp").format(prog=PROG),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}",
                        help=_("show the version of Easy Installer and exit"))
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help=_("show more details, for example the technical cause of an error "
                               "(use -vv for even more)"))
    commands = parser.add_subparsers(dest="command", metavar=_("COMMAND"), title=_("commands"))
    commands.required = True

    _add_install_parser(commands, verbose)

    p = commands.add_parser(
        "info", parents=[verbose], help=_("show what is inside an AppImage or app archive (without installing it)"),
        description=_("Show the name, version, icon and other details of an AppImage. The app is "
                      "not started.") + " " + _("App archives (.zip, .tar.gz, …) can be looked "
                                                "at, too."))
    p.add_argument("file", metavar="FILE", help=_("the .AppImage file to look at"))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    p.set_defaults(handler=cmd_info)

    p = commands.add_parser("list", parents=[verbose], help=_("list the installed apps"),
                            description=_("List the apps installed with Easy Installer."))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    _add_scope_options(p, _("only apps installed just for you"),
                       _("only apps installed for everyone on this computer"))
    p.set_defaults(handler=cmd_list)

    _add_update_parser(commands, verbose)

    p = commands.add_parser(
        "rollback", parents=[verbose],
        help=_("go back to the version of an app you had before its last update"),
        description=_("Go back to the version of an app that was installed before its last "
                      "update. The version you have now is kept, so you can switch back."))
    p.add_argument("id", metavar="ID", help=_id_help())
    _add_scope_options(p, _("the copy installed just for you"),
                       _("the copy installed for everyone (asks for your password)"))
    p.add_argument("-y", "--yes", action="store_true", help=_("do not ask before going back"))
    p.set_defaults(handler=cmd_rollback)

    p = commands.add_parser(
        "details", parents=[verbose], help=_("show everything known about an installed app"),
        description=_("Show everything Easy Installer knows about an installed app: version, "
                      "size, where its updates come from, where it was downloaded from, its "
                      "signature, the kept previous version and its settings and data folders."))
    p.add_argument("id", metavar="ID", help=_id_help())
    _add_scope_options(p, _("the copy installed just for you"),
                       _("the copy installed for everyone on this computer"))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    p.set_defaults(handler=cmd_details)

    p = commands.add_parser(
        "uninstall", parents=[verbose], help=_("remove an installed app"),
        description=_("Remove an installed app, its menu entry and its icon. Your personal files "
                      "and settings are kept."))
    p.add_argument("id", metavar="ID", help=_id_help())
    which = p.add_mutually_exclusive_group()
    which.add_argument("--system", action="store_true",
                       help=_("remove the copy installed for everyone (asks for your password)"))
    which.add_argument("--user", action="store_true", help=_("remove the copy installed just for you"))
    p.add_argument("--keep-permission", action="store_true",
                   help=_("remove an app installed just for you without the administrator "
                          "password; the special permission it needed stays on this computer"))
    p.add_argument("--delete-data", action="store_true",
                   help=_("also move the app's settings and data folders to the trash (they are "
                          "listed first)"))
    p.add_argument("-y", "--yes", action="store_true", help=_("do not ask before uninstalling"))
    p.set_defaults(handler=cmd_uninstall)

    p = commands.add_parser(
        "repair", parents=[verbose], help=_("make the menu entry and icon of an installed app again"),
        description=_("Make the menu entry, the icon and the saved information of an installed "
                      "app again from the app itself. The app stays where it is."))
    p.add_argument("id", metavar="ID", help=_id_help())
    _add_scope_options(p, _("the copy installed just for you"),
                       _("the copy installed for everyone (asks for your password)"))
    p.set_defaults(handler=cmd_repair)

    p = commands.add_parser("launch", parents=[verbose], help=_("start an installed app"),
                            description=_("Start an installed app, just like its menu entry does."))
    p.add_argument("id", metavar="ID", help=_id_help())
    p.set_defaults(handler=cmd_launch)

    _add_settings_parser(commands, verbose)

    p = commands.add_parser(
        "check", parents=[verbose], help=_("check whether this computer is ready for AppImages"),
        description=_("Check the parts of this computer that AppImages need, with tips on how to "
                      "fix what is missing."))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    p.set_defaults(handler=cmd_check)
    return parser


def _add_install_parser(commands: Any, verbose: argparse.ArgumentParser) -> None:
    p = commands.add_parser(
        "install", parents=[verbose], help=_("install an AppImage or app archive so it appears in your app menu"),
        description=_("Install an AppImage: the file is moved to your Applications folder and the "
                      "app gets a menu entry with its own icon.") + " " + _(
                          "An app that comes as an archive (.zip, .tar.gz, …) is unpacked into "
                          "its own folder there; the archive itself is kept."))
    p.add_argument("file", metavar="FILE",
                   help=_("the .AppImage file or app archive (.zip, .tar.gz, …) to install"))
    p.add_argument("--system", action="store_true",
                   help=_("install for everyone on this computer (asks for your password)"))
    p.add_argument("--keep", action="store_true",
                   help=_("keep the original file where it is and install a copy"))
    p.add_argument("--keep-both", action="store_true",
                   help=_("if another version of the app is installed, keep it and install this "
                          "one next to it instead of replacing it"))
    p.add_argument("--no-backup", action="store_true",
                   help=_("do not keep the version that is replaced (normally it is kept for a "
                          "while, so you can go back to it)"))
    p.add_argument("--executable", metavar="RELPATH",
                   help=_("app archives only: the program in the archive that starts the app, "
                          "for example bin/myapp"))
    p.add_argument("--sandbox-fix", choices=tuple(SANDBOX_CHOICES), default="auto",
                   help=_("how to handle apps whose security sandbox is blocked by this computer: "
                          "auto (recommended), apparmor (add a system permission), "
                          "no-sandbox (start without the sandbox) or none (change nothing)"))
    p.add_argument("--extract-and-run", choices=tuple(EXTRACT_CHOICES), default="auto",
                   help=_("let the app unpack itself on every start (needed when FUSE is "
                          "missing): auto, yes or no"))
    p.add_argument("--allow-foreign-arch", action="store_true",
                   help=_("install even if the app is made for a different kind of computer"))
    p.add_argument("-y", "--yes", action="store_true", help=_("do not ask before installing"))
    p.set_defaults(handler=cmd_install)


def _add_update_parser(commands: Any, verbose: argparse.ArgumentParser) -> None:
    p = commands.add_parser(
        "update", parents=[verbose], help=_("look for new versions of your apps and install them"),
        description=_("Look for new versions of the installed apps and install them. Without an "
                      "app ID or --all, it only shows which new versions there are."),
        epilog=_("Examples:\n"
                 "  {prog} update             (only look)\n"
                 "  {prog} update --all       (install every new version)\n"
                 "  {prog} update freecad     (update one app)").format(prog=PROG),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ids", nargs="*", metavar="ID",
                   help=_("the ID of an app to update (see: {command})").format(
                       command=_command("list")))
    p.add_argument("--all", action="store_true", help=_("update every app that has a new version"))
    p.add_argument("--check", action="store_true",
                   help=_("only show which new versions there are, do not install them"))
    _add_scope_options(p, _("only apps installed just for you"),
                       _("only apps installed for everyone on this computer"))
    p.add_argument("-y", "--yes", action="store_true", help=_("do not ask before updating"))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    p.set_defaults(handler=cmd_update)


# Settings the command line can change: name on the command line -> key in settings.json.
SETTING_KEYS: dict[str, str] = {
    "check-updates": settings.KEY_CHECK_UPDATES,
    "update-interval-hours": settings.KEY_UPDATE_INTERVAL_HOURS,
    "backup-days": settings.KEY_BACKUP_DAYS,
}
_SETTING_RANGES: dict[str, tuple[int, int]] = {
    "update-interval-hours": (settings.MIN_UPDATE_INTERVAL_HOURS,
                              settings.MAX_UPDATE_INTERVAL_HOURS),
    "backup-days": (settings.MIN_BACKUP_DAYS, settings.MAX_BACKUP_DAYS),
}


def _setting_description(key: str) -> str:
    texts = {
        "check-updates": _("look for new versions of your apps automatically (yes or no)"),
        "update-interval-hours": _("hours between two automatic checks for new versions"),
        "backup-days": _("days the replaced version of an app is kept after an update, so you "
                         "can go back to it (0 = do not keep it)"),
    }
    return texts[key]


def _add_settings_parser(commands: Any, verbose: argparse.ArgumentParser) -> None:
    keys = "\n".join(f"  {key:<23} {_setting_description(key)}" for key in SETTING_KEYS)
    p = commands.add_parser(
        "settings", parents=[verbose], help=_("show or change the settings"),
        description=_("Show the settings, or change one of them."),
        epilog=_("Settings:\n{keys}\n\nExamples:\n"
                 "  {prog} settings\n"
                 "  {prog} settings backup-days 30").format(keys=keys, prog=PROG),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("key", nargs="?", metavar="KEY", help=_("the setting to show or change"))
    p.add_argument("value", nargs="?", metavar="VALUE", help=_("the new value"))
    p.add_argument("--json", action="store_true", help=_("print machine-readable JSON"))
    p.set_defaults(handler=cmd_settings)


# ------------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------------


@dataclass
class Context:
    verbosity: int
    progress: ProgressLine


def _system_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return EXIT_OK
    if isinstance(exc.code, int):
        return exc.code
    _err(str(exc.code))
    return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _install_argparse_translations()
    parser = build_parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:  # --help, --version and usage errors
        return _flush_stdout(_system_exit_code(exc))
    verbosity = getattr(args, "verbose", 0) or 0
    progress = ProgressLine()
    restore_logging = _setup_logging(verbosity, progress)
    try:
        code = _run(args, Context(verbosity=verbosity, progress=progress))
    finally:
        progress.clear()
        restore_logging()
    return _flush_stdout(code)


def _flush_stdout(code: int) -> int:
    """Write the buffered output now, while a broken pipe can still be handled.

    Otherwise Python flushes at exit, where a reader that already quit (``| true``) makes it
    print "Exception ignored ... BrokenPipeError" and exit with 120.
    """
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except BrokenPipeError:
        _silence_stdout()
    except OSError as exc:  # e.g. a full disk: say so; the unwritten rest is dropped
        _silence_stdout()
        return _fail(_("The output could not be written: {reason}").format(
            reason=exc.strerror or exc))
    except ValueError:
        pass
    return code


def _run(args: argparse.Namespace, ctx: Context) -> int:
    try:
        return args.handler(args, ctx)
    except KeyboardInterrupt:
        ctx.progress.clear()
        _err()
        _err(_("Cancelled."))
        return EXIT_CANCELLED
    except EasyInstallerError as exc:
        ctx.progress.clear()
        _report_error(exc, ctx.verbosity)
        return EXIT_CANCELLED if _is_cancelled_authorization(exc) else EXIT_ERROR
    except BrokenPipeError:
        # The output was piped into a program that stopped reading (e.g. "| head").
        _silence_stdout()
        return EXIT_ERROR
    except Exception as exc:  # a bug: stay friendly, show the traceback with --verbose
        ctx.progress.clear()
        log.debug("unexpected error", exc_info=True)
        _err(_("Error: {message}").format(message=_("Something unexpected went wrong.")))
        if ctx.verbosity:
            _err("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        else:
            _err(_("Run the command again with --verbose to see the technical details."))
        return EXIT_ERROR


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _is_cancelled_authorization(exc: EasyInstallerError) -> bool:
    return isinstance(exc, AuthorizationError) and exc.message == _("Authentication was cancelled.")


def _error_hint(exc: EasyInstallerError) -> str | None:
    if isinstance(exc, UnsupportedArchitectureError):
        return _("If you are sure, run the command again with --allow-foreign-arch.")
    return None


def _report_error(exc: EasyInstallerError, verbosity: int) -> None:
    _err(_("Error: {message}").format(message=exc.message))
    hint = _error_hint(exc)
    if hint:
        _err(hint)
    if verbosity and exc.details:
        _err(_("Details:"))
        _err(textwrap.indent(exc.details.rstrip(), "  "))


def _fail(message: str, hint: str | None = None, code: int = EXIT_ERROR) -> int:
    _err(_("Error: {message}").format(message=message))
    if hint:
        _err(hint)
    return code


# ------------------------------------------------------------------------------------------------
# keeping the registry true (apps that updated themselves) before showing it
# ------------------------------------------------------------------------------------------------


def _reconcile_note(result: ReconcileResult) -> tuple[bool, str] | None:
    """``(good news?, sentence)`` about what reconcile found, or None if there is nothing to say."""
    app = result.app
    if reconcile.went_back(result):
        return False, _("{name} went back to version {version} by itself.").format(
            name=app.name, version=result.new_version)
    if result.change in (reconcile.CHANGE_UPDATED, reconcile.CHANGE_ADOPTED):
        if result.new_version and result.new_version != result.old_version:
            return True, _("{name} updated itself to version {version}.").format(
                name=app.name, version=result.new_version)
        return True, _("{name} updated itself.").format(name=app.name)
    if result.change == reconcile.CHANGE_NEEDS_ADMIN:
        return False, _("{name} (installed for everyone) changed its own app file. To bring its "
                        "menu entry up to date, run: {command}").format(
                            name=app.name, command=_command("repair", app.id, "--system"))
    if result.change == reconcile.CHANGE_FOREIGN:
        return False, _("The app file of {name} now contains a different app. Nothing was "
                        "changed. To repair it, install {name} again.").format(name=app.name)
    return None


def _print_reconcile_notes(results: Iterable[ReconcileResult]) -> None:
    printed = False
    for result in results:
        note = _reconcile_note(result)
        if note is None:
            continue
        good, text = note
        wrapped = _wrap(text, _terminal_columns() - 4)
        _out(f"{_mark_ok() if good else _mark_warn()} {wrapped[0]}")
        for line in wrapped[1:]:
            _out(f"  {line}")
        printed = True
    if printed:
        _out()


def _refresh_installed(ctx: Context, scopes: Iterable[Scope], *,
                       apps: Sequence[InstalledApp] | None = None,
                       report: bool = True) -> list[ReconcileResult]:
    """Before showing or updating apps: pick up apps that updated themselves (reconcile) and
    delete kept versions that are older than the settings allow. Never fails the command."""
    scopes = tuple(Scope(scope) for scope in scopes)
    ctx.progress(None, _("Looking at the installed apps…"))
    results: list[ReconcileResult] = []
    try:
        if apps is None:
            results = reconcile.reconcile_all(scopes)
        else:
            results = [reconcile.reconcile_app(app) for app in apps]
    except Exception:  # noqa: BLE001 - a nicety; the list is shown anyway
        log.warning("could not bring the installed apps up to date", exc_info=True)
    if Scope.USER in scopes:
        try:
            installer.prune_backups(settings.load_settings().backup_days)
        except Exception:  # noqa: BLE001
            log.warning("could not delete the expired earlier versions", exc_info=True)
    downloads.remove_leftovers()   # what an update that was stopped hard left in the cache
    ctx.progress.clear()
    if report:
        _print_reconcile_notes(results)
    return results


# ------------------------------------------------------------------------------------------------
# install
# ------------------------------------------------------------------------------------------------


def _inspect(path: str, ctx: Context, *, archive: bool | None = None) -> AppImageInfo | PortableInfo:
    """Inspect an AppImage or (``archive``; None = decide by the file) a portable app archive."""
    if archive is None:
        archive = portable.is_portable_archive(Path(path))
    try:
        if archive:
            return portable.inspect_portable(path, progress=ctx.progress)
        return inspector.inspect_appimage(path, progress=ctx.progress)
    finally:
        ctx.progress.clear()


def install_options(args: argparse.Namespace) -> InstallOptions:
    return InstallOptions(
        scope=Scope.SYSTEM if args.system else Scope.USER,
        # None: the usual for the kind of file (an AppImage is moved, an archive is kept)
        keep_original=True if args.keep else None,
        sandbox_fix=SANDBOX_CHOICES[args.sandbox_fix],
        extract_and_run=EXTRACT_CHOICES[args.extract_and_run],
        allow_foreign_arch=bool(args.allow_foreign_arch),
        keep_both=bool(getattr(args, "keep_both", False)),
        keep_backup=False if getattr(args, "no_backup", False) else None,
    )


def similar_programs(info: PortableInfo) -> list[ExecutableCandidate]:
    """The programs of an archive that start the app about as likely as the best one."""
    if not info.executables:
        return []
    best = max(candidate.score for candidate in info.executables)
    return [c for c in info.executables if c.score >= best - SIMILAR_SCORE_MARGIN]


def _program_kind_text(candidate: ExecutableCandidate) -> str:
    return _("script") if candidate.kind == "script" else _("program")


def _archive_relpath(value: str, info: PortableInfo) -> str:
    """``--executable`` as a path inside the app folder: "./bin/app" and a path that still
    starts with the archive's top folder ("MyApp-linux-x64/my-app") are accepted as well."""
    text = value.strip()
    while text.startswith("./"):
        text = text[2:]
    prefix = (info.strip_prefix or "").strip("/")
    if prefix and text.startswith(prefix + "/"):
        text = text[len(prefix) + 1:]
    return text


def _choose_program(info: PortableInfo, args: argparse.Namespace) -> bool:
    """Decide which program of the archive the menu entry starts (``info.executable``): the one
    given with --executable, else the one the installed version starts (a newer archive keeps
    that choice), else the best one - or, when several fit about equally well and the user may
    be asked, the one the user picks. False: cancelled."""
    if args.executable:
        info.executable = _archive_relpath(args.executable, info)
        return True
    kept = installer.installed_program(info)
    if kept:
        info.executable = kept
    similar = similar_programs(info)
    if info.executable not in {candidate.relpath for candidate in similar}:
        similar[:0] = [c for c in info.executables if c.relpath == info.executable]
    if len(similar) < 2 or args.yes:
        return True
    _out(_("{name} contains several programs that could start it:").format(
        name=info.display_name or info.name))
    width = max(_text_width(display_text(c.relpath)) for c in similar)
    for number, candidate in enumerate(similar, start=1):
        kind = _program_kind_text(candidate)
        if candidate.relpath == kept:
            kind = _("{kind}, started by the installed version").format(kind=kind)
        elif candidate.relpath == info.executable:
            kind = _("{kind}, recommended").format(kind=kind)
        _out(f"  {number}) {_pad(display_text(candidate.relpath), width)}  ({kind})")
    default = next((n for n, c in enumerate(similar, start=1) if c.relpath == info.executable), 1)
    choice = _choose_number(_("Which one should start the app? [{first}-{last}, Enter = {default}] ")
                            .format(first=1, last=len(similar), default=default),
                            len(similar), default=default)
    if choice is None:
        return False
    info.executable = similar[choice - 1].relpath
    _out()
    return True


def cmd_install(args: argparse.Namespace, ctx: Context) -> int:
    options = install_options(args)
    archive = portable.is_portable_archive(Path(args.file))
    if args.executable and not archive and os.path.lexists(args.file):
        return _fail(_("--executable can only be used for an app that comes as an archive "
                       "(.zip, .tar.gz, …)."), code=EXIT_USAGE)
    with _inspect(args.file, ctx, archive=archive) as info:
        if isinstance(info, PortableInfo) and args.executable:
            wanted = _archive_relpath(args.executable, info)
            if info.has_member(wanted) is False:
                names = ", ".join(display_text(c.relpath) for c in info.executables[:5])
                return _fail(_("There is no program “{name}” in this archive.").format(
                    name=display_text(wanted)),
                    _("Programs that could start the app: {names}").format(names=names),
                    EXIT_USAGE)
        if isinstance(info, PortableInfo) and not _choose_program(info, args):
            _err(_("Cancelled. Nothing was changed."))
            return EXIT_CANCELLED
        plan = installer.plan_install(info, options)
        shown_warnings = list(plan.warnings)
        print_install_summary(plan, requested=options)
        if not args.yes and not confirm(_("Proceed? [Y/n] "), default=True):
            _err(_("Cancelled. Nothing was changed."))
            return EXIT_CANCELLED
        if plan.requires_root:
            _out(_("You may be asked for your password."))
        app = installer.execute_install(plan, progress=ctx.progress)
        ctx.progress.clear()
        new_warnings = [w for w in plan.warnings if w not in shown_warnings]
        print_installed(app, new_warnings)
    return EXIT_OK


def _is_portable_plan(plan: InstallPlan) -> bool:
    return plan.kind == KIND_PORTABLE


def _summary_heading(plan: InstallPlan, name: str) -> str:
    if plan.options.keep_both:
        return _("Ready to install {name} next to the installed version").format(name=name)
    if plan.action == ACTION_ROLLBACK:
        return _("Ready to go back to the previous version of {name}").format(name=name)
    if plan.action == ACTION_UPDATE:
        return _("Ready to update {name}").format(name=name)
    if plan.action == ACTION_REINSTALL:
        return _("Ready to install {name} again").format(name=name)
    if plan.action == ACTION_DOWNGRADE:
        return _("Ready to install an older version of {name}").format(name=name)
    return _("Ready to install {name}").format(name=name)


def _change_text(plan: InstallPlan) -> str | None:
    existing = plan.existing
    if existing is None or plan.options.keep_both:
        return None
    old, new = _version_text(existing.version), _version_text(plan.version)
    if plan.action == ACTION_ROLLBACK:
        return "\n".join((
            _("Version {old} is replaced by the kept version {new}.").format(old=old, new=new),
            _("Version {old} is kept, so you can switch back.").format(old=old)))
    if plan.action == ACTION_UPDATE:
        if existing.version is None or plan.version is None:
            return _("The installed copy of this app will be replaced by this file.")
        return _("Version {old} is installed and will be replaced by {new}.").format(old=old, new=new)
    if plan.action == ACTION_DOWNGRADE:
        return _("Version {old} is installed and will be replaced by the older version {new}.").format(
            old=old, new=new)
    return _("This version is already installed. It will be installed again.")


def _original_file_text(plan: InstallPlan) -> str:
    if plan.in_place:
        return _("is already in the right place")
    if _is_portable_plan(plan):
        if plan.options.keep_original:
            return _("is kept where it is (the app is unpacked from it)")
        return _("is deleted once the app is unpacked")
    if plan.options.keep_original:
        return _("is kept where it is (a copy is installed)")
    return _("is moved to the app file location above")


def _backup_text(plan: InstallPlan) -> str | None:
    """What happens to the version that is replaced (updates and downgrades)."""
    existing = plan.existing
    if existing is None or plan.options.keep_both or plan.in_place \
            or plan.action not in (ACTION_UPDATE, ACTION_DOWNGRADE):
        return None
    if plan.backup_target is not None:
        days = settings.load_settings().backup_days
        if existing.version:
            return ngettext("{version} is kept for {n} day, so you can go back to it",
                            "{version} is kept for {n} days, so you can go back to it",
                            days).format(version=existing.version, n=days)
        return ngettext("it is kept for {n} day, so you can go back to it",
                        "it is kept for {n} days, so you can go back to it", days).format(n=days)
    if not plan.keep_backup:
        return _("is replaced and not kept")
    return None


def _keep_both_text(plan: InstallPlan) -> str | None:
    main = plan.main_installed
    if not plan.options.keep_both or main is None:
        return None
    return _("a separate app called \"{name}\"; version {version} stays installed as well").format(
        name=plan.name, version=_version_text(main.version))


def _tips(plan: InstallPlan, requested: InstallOptions | None) -> list[str]:
    tips: list[str] = []
    main = plan.main_installed
    if plan.keep_both_available and not plan.options.keep_both and main is not None \
            and (main.version is None or plan.version is None
                 or compare_versions(main.version, plan.version) != 0):
        tips.append(_("To keep version {version} and install this one next to it, add "
                      "--keep-both.").format(version=_version_text(main.version)))
    if requested is not None and requested.keep_both and not plan.options.keep_both \
            and not _is_portable_plan(plan):
        if main is None:
            tips.append(_("--keep-both is not needed: there is no other version of this app to "
                          "keep."))
        else:
            tips.append(_("--keep-both is not used: this very file is already installed."))
    return tips


def _sandbox_text(fix: SandboxFix) -> str | None:
    if fix is SandboxFix.APPARMOR:
        return _("allow it with a system permission (asks for your password once)")
    if fix is SandboxFix.NO_SANDBOX:
        return _("start the app without its security sandbox")
    return None


def _install_size_text(plan: InstallPlan) -> str:
    info = plan.info
    if _is_portable_plan(plan) and plan.portable is not None and plan.portable.tree_size:
        return _("{size} (unpacked: {unpacked})").format(
            size=_format_size(info.size), unpacked=_format_size(plan.portable.tree_size))
    return _format_size(info.size)


def _location_fields(plan: InstallPlan) -> list[tuple[str, str | None]]:
    if _is_portable_plan(plan):
        program = plan.portable.executable if plan.portable is not None else None
        return [(_("App folder:"), display_text(str(plan.install_dir or ""))),
                (_("Program:"), display_text(program) if program else None)]
    return [(_("App file:"), display_text(str(plan.target_appimage)))]


def print_install_summary(plan: InstallPlan, requested: InstallOptions | None = None) -> None:
    info = plan.info
    name = info.display_name or plan.name
    scope = _scope_text(plan.scope)
    if plan.scope is Scope.SYSTEM:
        scope = _("{scope} (asks for your password)").format(scope=scope)
    _out(_bold(_summary_heading(plan, name)))
    _out()
    _print_fields([
        (_("Version:"), _version_text(plan.version)),
        (_("Size:"), _install_size_text(plan)),
        (_("Install for:"), scope),
        (_("Installed as:"), _keep_both_text(plan)),
        *_location_fields(plan),
        (_("Original file:"), _original_file_text(plan)),
        (_("Change:"), _change_text(plan)),
        (_("Previous version:"), _backup_text(plan)),
        (_("Downloaded from:"), _origin_text(getattr(info, "origin_url", None))),
        (_("Signature:"), _signature_text(getattr(info, "signature", None),
                                          plan.existing or plan.main_installed)),
        (_("Sandbox:"), _sandbox_text(plan.sandbox_fix)),
        (_("Start mode:"), _("unpacks itself on every start (a little slower)")
         if plan.extract_and_run else None),
    ])
    if plan.warnings:
        _out()
        _print_notes(_("Please note:"), plan.warnings)
    tips = _tips(plan, requested)
    if tips:
        _out()
        for tip in tips:
            _print_wrapped(tip)
    _out()
    _out(_("Only install apps from sources you trust."))


def _scope_args(app: InstalledApp) -> list[str]:
    """--user/--system for a command about ``app``, when the ID alone would be ambiguous."""
    if Scope(app.scope) is Scope.SYSTEM:
        return ["--system"]
    try:
        both = len(installer.find_installed(app.id)) > 1
    except EasyInstallerError:
        both = False
    return ["--user"] if both else []


def print_installed(app: InstalledApp, warnings: Sequence[str] = ()) -> None:
    _out(f"{_mark_ok()} " + _bold(_("{name} is installed.").format(name=app.name)))
    _out("  " + _("You can find it in your app menu and search."))
    uninstall_args = ["uninstall", app.id] + (["--system"] if app.scope is Scope.SYSTEM else [])
    if app.kind == KIND_PORTABLE:
        where = [(_("App folder:"), display_text(app.install_dir or "")),
                 (_("Program:"), display_text(app.appimage_path))]
    else:
        where = [(_("App file:"), display_text(app.appimage_path))]
    _print_fields([
        *where,
        (_("Start it:"), _command("launch", app.id)),
        (_("Remove it:"), _command(*uninstall_args)),
    ])
    if warnings:
        _print_notes(_("Please note:"), warnings)


# ------------------------------------------------------------------------------------------------
# info
# ------------------------------------------------------------------------------------------------


def _icon_member(info: AppImageInfo) -> str | None:
    """Path of the chosen icon inside the AppImage (not the temporary extracted copy)."""
    if info.icon_path is None:
        return None
    try:
        return display_text(info.icon_path.relative_to(info.work_dir / "root").as_posix())
    except ValueError:
        return display_text(info.icon_path.name)


def _value_dict(value: object) -> dict | None:
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def info_to_dict(info: AppImageInfo, status: SystemStatus,
                 installed: Sequence[InstalledApp] = ()) -> dict[str, Any]:
    """JSON-ready description of an inspected AppImage (stable keys, no icon data)."""
    icon = None
    if info.icon_path is not None:
        icon = {
            "path_in_appimage": _icon_member(info),
            "format": info.icon_info.format if info.icon_info else None,
            "width": info.icon_info.width if info.icon_info else None,
            "height": info.icon_info.height if info.icon_info else None,
        }
    elf = info.elf
    return {
        "kind": "appimage",
        # file names that are not UTF-8 (e.g. from an old Windows zip) are shown with U+FFFD
        "path": display_text(str(info.path)),
        "file_name": display_text(info.path.name),
        "size": info.size,
        "sha256": info.sha256,
        "app_id": info.app_id,
        "name": info.name,
        "display_name": info.display_name,
        "version": info.version,
        "comment": info.comment,
        "categories": list(info.categories),
        "appimage_type": info.appimage_type,
        "arch": info.arch,
        "host_arch": host_arch(),
        "desktop_filename": display_text(info.desktop_filename) if info.desktop_filename else None,
        "desktop_entry": info.desktop_entry.to_text() if info.desktop_entry is not None else None,
        "icon": icon,
        "is_electron": info.is_electron,
        "exec_has_no_sandbox": info.exec_has_no_sandbox,
        "terminal": info.terminal,
        "update_info": info.update_info,
        "update_source": _value_dict(getattr(info, "update_source", None)),
        "origin_url": getattr(info, "origin_url", None),
        "signature": _value_dict(getattr(info, "signature", None)),
        "data_hints": list(getattr(info, "data_hints", None) or []),
        "elf": {
            "bits": elf.bits,
            "little_endian": elf.little_endian,
            "machine": elf.machine,
            "arch": elf.arch,
            "payload_offset": elf.payload_offset,
            "appimage_type": elf.appimage_type,
            "has_interp": elf.has_interp,
            "sections": {name: {"offset": offset, "size": size}
                         for name, (offset, size) in elf.sections.items()},
        },
        "needs_sandbox_fix": needs_sandbox_fix(info, status),
        "can_run_directly": status.can_run_appimage(info.elf),
        "installed": [app_to_dict(app) for app in installed],
        "warnings": list(info.warnings),
    }


def portable_info_to_dict(info: PortableInfo, installed: Sequence[InstalledApp] = ()) -> dict[str, Any]:
    """JSON-ready description of an inspected app archive (stable keys, no icon data)."""
    icon = None
    if info.icon_path is not None and info.icon_info is not None:
        icon = {"format": info.icon_info.format, "width": info.icon_info.width,
                "height": info.icon_info.height}
    return {
        "kind": "portable",
        "path": display_text(str(info.path)),
        "file_name": display_text(info.path.name),
        "size": info.size,
        "sha256": info.sha256,
        "app_id": info.app_id,
        "name": info.name,
        "display_name": info.display_name,
        "version": info.version,
        "comment": info.comment,
        "categories": list(info.categories),
        "arch": info.arch,
        "host_arch": host_arch(),
        "top_folder": display_text(info.strip_prefix) if info.strip_prefix else None,
        "desktop_filename": display_text(info.desktop_filename) if info.desktop_filename else None,
        "desktop_entry": info.desktop_entry.to_text() if info.desktop_entry is not None else None,
        "executable": display_text(info.executable),
        "executables": [{"path": display_text(c.relpath), "kind": c.kind, "score": c.score}
                        for c in info.executables],
        "icon": icon,
        "is_electron": info.is_electron,
        "terminal": info.terminal,
        "unpacked_size": info.tree_size,
        "file_count": info.file_count,
        "origin_url": info.origin_url,
        "data_hints": list(info.data_hints),
        "installed": [app_to_dict(app) for app in installed],
        "warnings": list(info.warnings),
    }


def _installed_text(installed: Sequence[InstalledApp]) -> str:
    if not installed:
        return _("no")
    return "\n".join(
        _("version {version} ({scope})").format(version=_version_text(app.version),
                                                scope=_scope_text(app.scope))
        for app in installed)


def _update_source_text(info: AppImageInfo) -> str | None:
    source = getattr(info, "update_source", None)
    return source.describe() if source is not None else None


def print_info(info: AppImageInfo, data: Mapping[str, Any],
               installed: Sequence[InstalledApp] = ()) -> None:
    _out(_bold(info.display_name or info.name))
    icon = None
    if data["icon"]:
        icon_data = data["icon"]
        size = ""
        if icon_data["width"] and icon_data["height"]:
            size = f", {icon_data['width']}x{icon_data['height']}"
        icon = f"{icon_data['path_in_appimage']} ({str(icon_data['format']).upper()}{size})"
    arch = info.arch
    if is_foreign_arch(info.elf):
        arch = _("{arch} (this computer: {host})").format(arch=arch_label(info.elf),
                                                         host=data["host_arch"])
    kind = _("AppImage type {type}").format(type=info.appimage_type) if info.appimage_type \
        else _("AppImage")
    start = _("yes") if data["can_run_directly"] else \
        _("no, it unpacks itself on every start (FUSE is missing)")
    _print_fields([
        (_("Description:"), info.comment),
        (_("Version:"), _version_text(info.version)),
        (_("App ID:"), info.app_id),
        (_("Categories:"), ", ".join(info.categories) or None),
        (_("File:"), data["path"]),
        (_("Size:"), _format_size(info.size)),
        (_("SHA-256:"), info.sha256),
        (_("Format:"), f"{kind}, {arch}"),
        (_("Menu entry:"), data["desktop_filename"] or _("none")),
        (_("Icon:"), icon or _("none")),
        (_("Electron app:"), _yes_no(info.is_electron)),
        (_("Terminal app:"), _yes_no(info.terminal)),
        (_("Update info:"), info.update_info),
        (_("Updates from:"), _update_source_text(info)),
        (_("Downloaded from:"), _origin_text(getattr(info, "origin_url", None), full=True)),
        (_("Signature:"), _signature_text(getattr(info, "signature", None))
         or _("not signed (most apps are not)")),
        (_("Starts directly:"), start),
        (_("Sandbox fix needed:"), _yes_no(data["needs_sandbox_fix"])),
        (_("Installed:"), _installed_text(installed)),
    ])
    if info.warnings:
        _out()
        _print_notes(_("Please note:"), info.warnings)


def _programs_text(info: PortableInfo, limit: int = 5) -> str | None:
    lines = []
    for candidate in info.executables[:limit]:
        kind = _program_kind_text(candidate)
        if candidate.relpath == info.executable:
            kind = _("{kind}, recommended").format(kind=kind)
        lines.append(f"{display_text(candidate.relpath)} ({kind})")
    return "\n".join(lines) or None


def print_portable_info(info: PortableInfo, installed: Sequence[InstalledApp] = ()) -> None:
    _out(_bold(info.display_name or info.name))
    arch = info.arch or _("unknown")
    if info.arch and info.arch != host_arch():
        arch = _("{arch} (this computer: {host})").format(arch=info.arch, host=host_arch())
    icon = None
    if info.icon_info is not None:
        icon = info.icon_info.format.upper()
        if info.icon_info.width and info.icon_info.height:
            icon += f", {info.icon_info.width}x{info.icon_info.height}"
    _print_fields([
        (_("Description:"), info.comment),
        (_("Version:"), _version_text(info.version)),
        (_("App ID:"), info.app_id),
        (_("Categories:"), ", ".join(info.categories) or None),
        (_("File:"), display_text(str(info.path))),
        (_("Size:"), _("{size} (unpacked: {unpacked})").format(
            size=_format_size(info.size), unpacked=_format_size(info.tree_size))),
        (_("SHA-256:"), info.sha256),
        (_("Format:"), _("app archive, {arch}").format(arch=arch)),
        (_("Top folder:"), display_text(info.strip_prefix) if info.strip_prefix else None),
        (_("Programs:"), _programs_text(info)),
        (_("Menu entry:"), display_text(info.desktop_filename) if info.desktop_filename
         else _("none")),
        (_("Icon:"), icon or _("none")),
        (_("Electron app:"), _yes_no(info.is_electron)),
        (_("Terminal app:"), _yes_no(info.terminal)),
        (_("Downloaded from:"), _origin_text(info.origin_url, full=True)),
        (_("Installed:"), _installed_text(installed)),
    ])
    if info.warnings:
        _out()
        _print_notes(_("Please note:"), info.warnings)


def cmd_info(args: argparse.Namespace, ctx: Context) -> int:
    with _inspect(args.file, ctx) as info:
        installed = installer.find_installed(info.app_id)
        if isinstance(info, PortableInfo):
            if args.json:
                _print_json(portable_info_to_dict(info, installed))
            else:
                print_portable_info(info, installed)
            return EXIT_OK
        status = system_checks.get_system_status()
        data = info_to_dict(info, status, installed)
        if args.json:
            _print_json(data)
        else:
            print_info(info, data, installed)
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# list
# ------------------------------------------------------------------------------------------------


def _update_key(app: InstalledApp) -> tuple[str, str]:
    return (Scope(app.scope).value, app.id)


def app_to_dict(app: InstalledApp, update: AvailableUpdate | None = None) -> dict[str, Any]:
    """The registry entry plus its ``status`` and the ``update`` found for it (null: none known)."""
    return {**app.to_dict(), "status": app.status(),
            "update": update.to_dict() if update is not None else None}


def _status_text(status: str) -> str:
    if status == STATUS_MISSING_APPIMAGE:
        return _("App file missing")
    if status == STATUS_MISSING_LAUNCHER:
        return _("Menu entry missing")
    return _("OK")


def _selected_scopes(args: argparse.Namespace) -> tuple[Scope, ...]:
    if getattr(args, "user", False):
        return (Scope.USER,)
    if getattr(args, "system", False):
        return (Scope.SYSTEM,)
    return (Scope.USER, Scope.SYSTEM)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [_text_width(h) for h in headers]
    for row in rows:
        widths = [max(w, _text_width(cell)) for w, cell in zip(widths, row)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(_pad(cell, width) for cell, width in zip(cells, widths)).rstrip()

    _out(_bold(line(headers)))
    for row in rows:
        _out(line(row))


def _update_cell(update: AvailableUpdate | None) -> str:
    if update is None:
        return ""
    return update.version or _("new version")


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    scopes = _selected_scopes(args)
    _refresh_installed(ctx, scopes, report=not args.json)
    apps = installer.list_installed(scopes)
    found = updater.cached_updates() if apps else {}
    if args.json:
        _print_json([app_to_dict(app, found.get(_update_key(app))) for app in apps])
        return EXIT_OK
    if not apps:
        _out(_("No apps installed yet."))
        _out(_("Install one with: {command}").format(command=_command("install", "FILE.AppImage")))
        return EXIT_OK
    rows = []
    missing_file = False
    no_launcher: list[InstalledApp] = []
    for app in apps:
        status = app.status()
        missing_file = missing_file or status == STATUS_MISSING_APPIMAGE
        if status == STATUS_MISSING_LAUNCHER:
            no_launcher.append(app)
        rows.append([app.name, app.id, _version_text(app.version), _scope_short(app.scope),
                     _size_text(app.size) or "", _update_cell(found.get(_update_key(app))),
                     _status_text(status)])
    print_table([_("NAME"), _("ID"), _("VERSION"), _("FOR"), _("SIZE"), _("UPDATE"),
                 _("STATUS")], rows)
    updatable = [app for app in apps if _update_key(app) in found]
    available = len(updatable)
    if available:
        _out()
        command = (_command("update", updatable[0].id, *_scope_args(updatable[0]))
                   if available == 1 else _command("update", "--all"))
        _print_wrapped(ngettext("{n} new version is available. To install it, run: {command}",
                                "{n} new versions are available. To install them, run: {command}",
                                available).format(n=available, command=command))
    if missing_file or no_launcher:
        _out()
    if missing_file:
        _out(_("Apps with a problem can be repaired by installing them again."))
    for app in no_launcher:
        _out(_("To make the menu entry of {name} again, run: {command}").format(
            name=app.name, command=_command("repair", app.id, *_scope_args(app))))
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# finding an installed app by its ID
# ------------------------------------------------------------------------------------------------


def _suggestions(app_id: str) -> list[str]:
    """IDs of installed apps whose ID or name resembles ``app_id``."""
    try:
        apps = installer.list_installed()
    except EasyInstallerError:
        return []
    keys: dict[str, str] = {}
    for app in apps:
        keys.setdefault(app.id.casefold(), app.id)
        keys.setdefault(app.name.casefold(), app.id)
    matches = difflib.get_close_matches(app_id.casefold(), list(keys), n=3, cutoff=0.6)
    return list(dict.fromkeys(keys[m] for m in matches))


def _not_installed(app_id: str) -> int:
    suggestions = _suggestions(app_id)
    if suggestions:
        hint = _("Did you mean: {ids}?").format(ids=", ".join(suggestions))
    else:
        hint = _("To see the installed apps and their IDs, run: {command}").format(
            command=_command("list"))
    return _fail(_("No app with the ID \"{id}\" is installed.").format(id=app_id), hint)


def _wrong_scope(app: InstalledApp) -> int:
    if app.scope is Scope.USER:
        message = _("{name} is not installed for everyone on this computer, only for you.")
        hint = _("Use --user instead of --system.")
    else:
        message = _("{name} is not installed just for you, but for everyone on this computer.")
        hint = _("Use --system instead of --user.")
    return _fail(message.format(name=app.name), hint)


def _choose_installation(apps: Sequence[InstalledApp], question: str) -> InstalledApp | None:
    """Ask which of several installations (one per scope) is meant; None = cancelled.
    ``question`` is a prompt with a ``{choices}`` placeholder."""
    _out(_("{name} is installed twice:").format(name=apps[0].name))
    for number, app in enumerate(apps, start=1):
        _out(f"  {number}) " + _("{scope} (version {version})").format(
            scope=_scope_text(app.scope), version=_version_text(app.version)))
    choices = [str(n) for n in range(1, len(apps) + 1)]
    choice = _choose_number(question.format(choices="/".join(choices)), len(apps), default=None)
    return apps[choice - 1] if choice is not None else None


def _matching_installations(args: argparse.Namespace) -> list[InstalledApp] | int:
    """The installations of ``args.id`` in the scope asked for (or an exit code)."""
    found = installer.find_installed(args.id)
    wanted = Scope.USER if args.user else Scope.SYSTEM if args.system else None
    candidates = [app for app in found if wanted is None or app.scope is wanted]
    if not candidates:
        return _wrong_scope(found[0]) if found else _not_installed(args.id)
    return candidates


def _pick_installation(args: argparse.Namespace, *, question: str, hint: str,
                       prefer: Callable[[InstalledApp], bool] | None = None) -> InstalledApp | int:
    """The one installation a command is about; asks when the app is installed in both scopes
    (with -y that is a usage error with ``hint``). ``prefer``: if exactly one installation
    qualifies, it is taken without asking."""
    candidates = _matching_installations(args)
    if isinstance(candidates, int):
        return candidates
    if len(candidates) > 1 and prefer is not None:
        preferred = [app for app in candidates if prefer(app)]
        if len(preferred) == 1:
            return preferred[0]
    if len(candidates) == 1:
        return candidates[0]
    if getattr(args, "yes", False):
        return _fail(
            _("{name} is installed both just for you and for everyone on this computer.").format(
                name=candidates[0].name), hint, EXIT_USAGE)
    app = _choose_installation(candidates, question)
    if app is None:
        _err(_("Cancelled. Nothing was changed."))
        return EXIT_CANCELLED
    return app


# ------------------------------------------------------------------------------------------------
# uninstall
# ------------------------------------------------------------------------------------------------


def _print_data_locations(locations: Sequence[DataLocation], indent: str = "  ") -> None:
    paths = [_short_path(loc.path) for loc in locations]
    width = max((_text_width(path) for path in paths), default=0)
    sizes = [_format_size(loc.size) if loc.size else _("empty") for loc in locations]
    size_width = max((_text_width(size) for size in sizes), default=0)
    for loc, path, size in zip(locations, paths, sizes):
        _out(f"{indent}{_pad(path, width)}  {' ' * (size_width - _text_width(size))}{size}  "
             f"{_data_kind_text(loc.kind)}")


def _data_to_remove(app: InstalledApp, ctx: Context) -> list[DataLocation]:
    """The settings and data folders ``uninstall --delete-data`` offers (and lists them)."""
    ctx.progress(None, _("Looking for the settings and data of {name}…").format(name=app.name))
    try:
        others = installer.other_installations(app)
        locations = [] if others else installer.removable_data(app)
    finally:
        ctx.progress.clear()
    if others:
        _print_wrapped(_("The settings and data of {name} are kept, because another installation "
                         "of it still uses them.").format(name=app.name))
        return []
    if not locations:
        _out(_("No settings or data folders of {name} were found.").format(name=app.name))
        return []
    _out(ngettext("This settings and data folder of {name} was found:",
                  "These settings and data folders of {name} were found:",
                  len(locations)).format(name=app.name))
    _print_data_locations(locations)
    return locations


def _trash_data(locations: Sequence[DataLocation]) -> int:
    """Move the folders to the trash; report what moved and what could not be moved."""
    results = appdata.move_to_trash([loc.path for loc in locations])
    moved = [path for path, error in results if error is None]
    failed = [(path, error) for path, error in results if error is not None]
    if moved:
        _out("  " + ngettext("Its settings and data folder was moved to the trash:",
                             "Its settings and data folders were moved to the trash:",
                             len(moved)))
        for path in moved:
            _out(f"    {_short_path(path)}")
        _out("  " + _("If you need them again, you can restore them from the trash."))
    if not failed:
        return EXIT_OK
    _err(_("Error: {message}").format(message=ngettext(
        "{n} folder could not be moved to the trash:",
        "{n} folders could not be moved to the trash:", len(failed)).format(n=len(failed))))
    for path, error in failed:
        _err(f"  {_short_path(path)}: {error}")
    return EXIT_ERROR


def cmd_uninstall(args: argparse.Namespace, ctx: Context) -> int:
    app = _pick_installation(
        args, question=_("Which one do you want to uninstall? [{choices}, Enter = cancel] "),
        hint=_("Add --user or --system to choose which one to uninstall."))
    if isinstance(app, int):
        return app

    locations = _data_to_remove(app, ctx) if args.delete_data else []
    if not args.yes:
        if locations and not confirm(ngettext("Also move this folder to the trash? [y/N] ",
                                              "Also move these folders to the trash? [y/N] ",
                                              len(locations)), default=False):
            locations = []
        portable_app = app.kind == KIND_PORTABLE
        if locations:
            _out(_("The app will be removed, and the folders listed above will be moved to the "
                   "trash."))
            if portable_app:
                _out(_("Anything it saved in its own folder is moved to the trash, too."))
        elif portable_app:
            _print_wrapped(_("The app will be removed. Anything it saved in its own folder is "
                             "moved to the trash. Your other personal files and settings are "
                             "kept."))
        else:
            _out(_("The app will be removed. Your personal files and settings are kept."))
        prompt = _("Uninstall {name} ({scope})? [y/N] ").format(
            name=app.name, scope=_scope_text(app.scope))
        if not confirm(prompt, default=False):
            _err(_("Cancelled. Nothing was changed."))
            return EXIT_CANCELLED
    keep_permission = bool(args.keep_permission) and installer.can_keep_permission(app)
    if installer.uninstall_needs_password(app) and not keep_permission:
        _out(_("You may be asked for your password."))
    try:
        notes = installer.uninstall(app.id, app.scope, progress=ctx.progress,
                                    keep_permission=keep_permission)
    except AuthorizationError as exc:
        if not installer.can_keep_permission(app):
            raise
        # E.g. a standard user who cannot type an administrator's password, or ssh without a
        # password agent: only the permission needs the administrator.
        ctx.progress.clear()
        _report_error(exc, ctx.verbosity)
        _err(_("To uninstall {name} without the administrator password, add --keep-permission. "
               "Only the special permission it needed then stays on this computer.").format(
                   name=app.name))
        return EXIT_CANCELLED if _is_cancelled_authorization(exc) else EXIT_ERROR
    ctx.progress.clear()
    _out(f"{_mark_ok()} " + _("{name} was uninstalled.").format(name=app.name))
    code = EXIT_OK
    if locations:
        code = _trash_data(locations)
    elif app.kind == KIND_PORTABLE:
        _out("  " + _("Your other personal files and settings were kept."))
    else:
        _out("  " + _("Your personal files and settings were kept."))
    if notes:
        _print_notes(_("Please note:"), notes)
    return code


# ------------------------------------------------------------------------------------------------
# update
# ------------------------------------------------------------------------------------------------

#: What happened to one app in ``update`` (also the ``result`` key of ``update --json``).
RESULT_AVAILABLE = "update-available"
RESULT_UP_TO_DATE = "up-to-date"
RESULT_NOT_CHECKED = "error"
RESULT_UPDATED = "updated"
RESULT_FAILED = "failed"
RESULT_CANCELLED = "cancelled"
RESULT_SKIPPED = "skipped"


@dataclass
class UpdateRow:
    """One installation in ``update``: what was found for it and what became of it."""

    app: InstalledApp
    update: AvailableUpdate | None = None
    error: EasyInstallerError | None = None
    result: str = RESULT_UP_TO_DATE
    new: InstalledApp | None = None
    #: what the update reported for the user (e.g. the previous version could not be kept)
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.app.name


def _cannot_update_text(app: InstalledApp) -> str:
    """Why ``app`` is never checked for updates (shown after "<name>: ")."""
    if app.kind == KIND_PORTABLE:
        return _("It was installed from an archive and is not updated automatically. To update "
                 "it, install the archive of the new version: {command}").format(
                     command=_command("install", "FILE"))
    if app.base_id or app.pinned:
        return _("It is a copy that was kept next to another version, so it stays at its "
                 "version and is not updated.")
    return _("It does not say where its new versions are published, so it cannot be checked "
             "for updates.")


def _select_for_update(args: argparse.Namespace, scopes: Sequence[Scope]
                       ) -> tuple[list[UpdateRow], list[InstalledApp]] | int:
    """The installations to check (``rows``) and, without IDs, the ones that cannot be checked."""
    if args.ids:
        apps: list[InstalledApp] = []
        for app_id in dict.fromkeys(args.ids):
            found = installer.find_installed(app_id)
            chosen = [app for app in found if Scope(app.scope) in scopes]
            if not chosen:
                return _wrong_scope(found[0]) if found else _not_installed(app_id)
            apps += chosen
        rows = []
        for app in apps:
            if updater.has_update_source(app):
                rows.append(UpdateRow(app))
            else:
                rows.append(UpdateRow(app, error=UpdateError(_cannot_update_text(app)),
                                      result=RESULT_NOT_CHECKED))
        return rows, []
    apps = installer.list_installed(scopes)
    rows = [UpdateRow(app) for app in apps if updater.has_update_source(app)]
    unchecked = [app for app in apps
                 if not updater.has_update_source(app) and not app.base_id and not app.pinned]
    return rows, unchecked


#: When updates are installed (``update ID``/``--all``), an answer of the update source that is
#: younger than this is used as it is: ``update`` shows what is available and says to run
#: ``update ID`` - asking the source again a minute later would only use up its hourly
#: allowance of requests (GitHub counts conditional requests, too). ``update --check`` always asks.
FRESH_ANSWER_HOURS = 10 / 60


def _fresh_answers(apps: Sequence[InstalledApp]) -> list[InstalledApp]:
    """The apps whose update source answered within :data:`FRESH_ANSWER_HOURS` (a check that
    failed is asked again: its reason is told, never "up to date")."""
    cache = UpdateCache()
    return [app for app in apps
            if not cache.is_due(Scope(app.scope).value, app.id, FRESH_ANSWER_HOURS)
            and not cache.failed(Scope(app.scope).value, app.id)]


def _check_rows(rows: Sequence[UpdateRow], ctx: Context, *, reuse_fresh: bool = False) -> None:
    """Ask the network for every row that can be checked (one failure never stops the others).

    ``reuse_fresh``: an answer from the last few minutes is used instead (see
    :data:`FRESH_ANSWER_HOURS`)."""
    apps = [row.app for row in rows if row.error is None]
    if not apps:
        return
    fresh = _fresh_answers(apps) if reuse_fresh else []
    ask = [app for app in apps if app not in fresh]
    found = updater.UpdateCheckResult()
    if ask:
        ctx.progress(0.0, _("Checking for updates…"))
        try:
            found = updater.check_all_updates(ask, force=True, progress=ctx.progress)
        finally:
            ctx.progress.clear()
    if fresh:
        remembered = updater.check_all_updates(fresh, force=False)
        found.updates.update(remembered.updates)
        found.errors.update(remembered.errors)
        found.checked += remembered.checked
    for row in rows:
        if row.error is not None:
            continue
        key = _update_key(row.app)
        if key in found.errors:
            row.error, row.result = found.errors[key], RESULT_NOT_CHECKED
        elif key in found.updates:
            row.update, row.result = found.updates[key], RESULT_AVAILABLE
        else:
            row.result = RESULT_UP_TO_DATE


def _source_text(app: InstalledApp) -> str | None:
    source = updater.update_source_of(app)
    return source.describe() if source is not None else None


def update_row_to_dict(row: UpdateRow) -> dict[str, Any]:
    """One entry of ``update --json`` (stable keys)."""
    update = row.update
    return {
        "id": row.app.id,
        "scope": Scope(row.app.scope).value,
        "name": row.app.name,
        "installed": row.app.version,
        "available": update.version if update is not None else None,
        "source": _source_text(row.app),
        "size": update.size if update is not None else None,
        "url": update.url if update is not None else None,
        "result": row.result,
        "version": row.new.version if row.new is not None else row.app.version,
        "error": row.error.message if row.error is not None else None,
        "notes": list(row.notes),
    }


def _print_update_table(rows: Sequence[UpdateRow]) -> None:
    print_table(
        [_("NAME"), _("ID"), _("FOR"), _("INSTALLED"), _("AVAILABLE"), _("SIZE"), _("FROM")],
        [[row.name, row.app.id, _scope_short(row.app.scope), _version_text(row.app.version),
          _update_cell(row.update), _size_text(row.update.size if row.update else None) or "",
          _source_text(row.app) or ""] for row in rows])


def _print_check_errors(rows: Sequence[UpdateRow]) -> None:
    failed = [row for row in rows if row.result == RESULT_NOT_CHECKED and row.error is not None]
    if failed:
        _out()
        _print_notes(_("Could not be checked:"),
                     [_("{name}: {problem}").format(name=row.name, problem=row.error.message)
                      for row in failed])


def _print_up_to_date(rows: Sequence[UpdateRow]) -> None:
    current = [row for row in rows if row.result == RESULT_UP_TO_DATE]
    if not current:
        return
    if len(current) == 1:
        _out(_("{name} is up to date (version {version}).").format(
            name=current[0].name, version=_version_text(current[0].app.version)))
    else:
        _out(ngettext("{n} app is up to date.", "{n} apps are up to date.",
                      len(current)).format(n=len(current)))


def _print_unchecked(unchecked: Sequence[InstalledApp], *, first: bool = False) -> None:
    """Why some apps were not checked; ``first``: nothing was printed before."""
    portable = [app for app in unchecked if app.kind == KIND_PORTABLE]
    others = [app for app in unchecked if app.kind != KIND_PORTABLE]
    if others:
        if not first:
            _out()
        first = False
        _print_wrapped(_("Not checked, because they do not say where their new versions are "
                         "published: {names}").format(names=", ".join(a.name for a in others)))
    if portable:
        if not first:
            _out()
        _print_wrapped(_("Not checked, because they were installed from an archive: {names}. "
                         "To update one, install the archive of its new version: {command}")
                       .format(names=", ".join(a.name for a in portable),
                               command=_command("install", "FILE")))


def _check_exit_code(rows: Sequence[UpdateRow]) -> int:
    """``update --check``: 0 unless nothing at all could be checked."""
    if rows and all(row.result == RESULT_NOT_CHECKED for row in rows):
        return EXIT_ERROR
    return EXIT_OK


def _report_update_check(rows: Sequence[UpdateRow], unchecked: Sequence[InstalledApp],
                         args: argparse.Namespace) -> int:
    code = _check_exit_code(rows)
    if args.json:
        _print_json([update_row_to_dict(row) for row in rows])
        return code
    if not rows:
        if not unchecked:
            _out(_("No apps installed yet."))
        elif all(app.kind != KIND_PORTABLE for app in unchecked):
            _print_wrapped(_("None of your apps says where its new versions are published, so "
                             "Easy Installer cannot check them for updates."))
        else:
            _print_unchecked(unchecked, first=True)
        return code
    available = [row for row in rows if row.update is not None]
    if available:
        _print_update_table(available)
    else:
        _print_up_to_date(rows)
    _print_check_errors(rows)
    _print_unchecked(unchecked)
    if available and not args.check:
        _out()
        if len(available) == 1:
            command = _command("update", available[0].app.id, *_scope_args(available[0].app))
            _out(_("To install it, run: {command}").format(command=command))
        else:
            _out(_("To install them, run: {command}").format(command=_command("update", "--all")))
    return code


@contextlib.contextmanager
def _ctrl_c_cancels(cancel: threading.Event,
                    on_cancel: Callable[[], None] | None = None) -> Iterator[None]:
    """While active, Ctrl+C sets ``cancel`` instead of stopping the program at once: an update
    that is still downloading stops cleanly (nothing is left behind), and an installation that
    has already started runs to its end, so the app is never left half replaced.
    ``on_cancel`` is called at the first Ctrl+C (e.g. to say that the download stops)."""
    previous: Any = None

    def handler(signum: int, frame: object) -> None:
        first = not cancel.is_set()
        cancel.set()
        if first and on_cancel is not None:
            on_cancel()

    if threading.current_thread() is threading.main_thread():
        current = signal.getsignal(signal.SIGINT)
        if current is not signal.SIG_IGN:
            try:
                previous = signal.signal(signal.SIGINT, handler)
            except (ValueError, OSError):
                previous = None
            else:
                previous = previous if previous is not None else signal.SIG_DFL
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _run_update(app: InstalledApp, update: AvailableUpdate, ctx: Context,
                cancel: threading.Event, *, allow_signer_change: bool,
                notes: list[str] | None = None,
                finishing: list[bool] | None = None,
                confirm_signer_change: Callable[[str], bool] | None = None) -> InstalledApp:
    """``updater.apply_update`` with the download's own progress (size and percent) on the
    status line, then the steps of the installation. ``finishing[0]`` becomes True once the
    download is complete."""
    finishing = finishing if finishing is not None else [False]

    def download(update: AvailableUpdate, folder: Path, *, progress: Any = None,
                 cancel: threading.Event | None = None) -> Path:
        return updates.download_update(update, folder, progress=ctx.progress, cancel=cancel)

    told = [False]

    def installing(fraction: float | None, message: str) -> None:
        finishing[0] = True
        if cancel.is_set() and not told[0]:
            told[0] = True
            ctx.progress.clear()
            _err(_("The installation has already started and is finished first…"))
        ctx.progress(None, message)

    try:
        return updater.apply_update(app, update, progress=installing, cancel=cancel,
                                    allow_signer_change=allow_signer_change, downloader=download,
                                    warnings=notes, confirm_signer_change=confirm_signer_change)
    finally:
        ctx.progress.clear()


def _kept_previous(old: InstalledApp, new: InstalledApp) -> bool:
    """The update kept the version it replaced (``new.previous`` is ``old``)."""
    previous = new.previous or {}
    if previous.get("sha256") and old.sha256:
        return previous.get("sha256") == old.sha256
    return bool(previous) and previous.get("version") == old.version


def _print_updated(row: UpdateRow) -> None:
    new = row.new
    assert new is not None
    _out(f"{_mark_ok()} " + _("{name} was updated to version {version}.").format(
        name=new.name, version=_version_text(new.version)))
    if _kept_previous(row.app, new):
        days = settings.load_settings().backup_days
        _print_wrapped(ngettext(
            "The previous version is kept for {n} day. To go back to it, run: {command}",
            "The previous version is kept for {n} days. To go back to it, run: {command}",
            days).format(n=days, command=_command("rollback", new.id, *_scope_args(new))), "  ")
    if row.notes:
        _print_notes(_("Please note:"), row.notes)


def _report_update_failure(row: UpdateRow, exc: BaseException, ctx: Context, *,
                           quiet: bool) -> None:
    ctx.progress.clear()
    if isinstance(exc, EasyInstallerError):
        row.error = exc
    else:  # a bug in one update must not stop the others
        log.debug("unexpected error while updating %s", row.app.id, exc_info=True)
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        row.error = EasyInstallerError(_("Something unexpected went wrong."), details=details)
    row.result = RESULT_FAILED
    if quiet:
        return
    _err(_("Error: {message}").format(message=row.error.message))
    if ctx.verbosity and row.error.details:
        _err(_("Details:"))
        _err(textwrap.indent(row.error.details.rstrip(), "  "))
    if _("Nothing was changed.") not in row.error.message:   # most update errors say it already
        _err("  " + _("{name} was not changed.").format(name=row.name))


def _ask_signer_change(row: UpdateRow, message: str, args: argparse.Namespace,
                       ctx: Context, cancel: threading.Event) -> bool:
    """The update is not signed by the maker of the installed version: True = install anyway.
    Asked while the downloaded file waits (it is not downloaded again for a yes); Ctrl+C here
    is a "no" that stops the remaining updates."""
    ctx.progress.clear()
    if args.yes or args.json:
        _report_update_failure(row, updater.SignerChangedError(message), ctx, quiet=args.json)
        if not args.json:
            _err(_("To decide yourself, run the update without -y: {command}").format(
                command=_command("update", row.app.id, *_scope_args(row.app))))
        return False
    _print_notes(_("Careful:"), [message])
    previous: Any = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        if confirm(_("Install this update anyway? [y/N] "), default=False):
            return True
    except KeyboardInterrupt:
        _err()
        cancel.set()
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    row.result = RESULT_SKIPPED
    _out("  " + _("{name} was not changed.").format(name=row.name))
    return False


def _update_one(row: UpdateRow, args: argparse.Namespace, ctx: Context) -> bool:
    """Download and install the update of one app. Returns True if Ctrl+C was pressed (the
    remaining updates are then not started)."""
    app, update = row.app, row.update
    assert update is not None
    quiet = bool(args.json)
    if not quiet:
        _out(_bold(_("Updating {name} from version {old} to {new}…").format(
            name=app.name, old=_version_text(app.version), new=_version_text(update.version))))
        if Scope(app.scope) is Scope.SYSTEM:
            _out("  " + _("You may be asked for your password."))
    cancel = threading.Event()
    finishing = [False]

    def stopping() -> None:
        if not finishing[0] and not quiet:
            ctx.progress.clear()
            _err(_("Stopping the download…"))

    def ask(message: str) -> bool:
        return _ask_signer_change(row, message, args, ctx, cancel)

    try:
        with _ctrl_c_cancels(cancel, stopping):
            row.notes = []
            row.new = _run_update(app, update, ctx, cancel, allow_signer_change=False,
                                  notes=row.notes, finishing=finishing,
                                  confirm_signer_change=ask)
    except updater.SignerChangedError as exc:
        if row.result not in (RESULT_SKIPPED, RESULT_FAILED):
            _report_update_failure(row, exc, ctx, quiet=quiet)
        return cancel.is_set()
    except updater.UpdateNotNeededError as exc:
        # It updated itself (or was updated otherwise) since it was checked: nothing to do.
        ctx.progress.clear()
        row.result, row.update = RESULT_UP_TO_DATE, None
        if not quiet:
            _out(f"{_mark_ok()} " + exc.message)
        return cancel.is_set()
    except (UpdateCancelled, AuthorizationError, EasyInstallerError) as exc:
        cancelled = isinstance(exc, UpdateCancelled) or cancel.is_set() or (
            isinstance(exc, AuthorizationError) and _is_cancelled_authorization(exc))
        if not cancelled:
            _report_update_failure(row, exc, ctx, quiet=quiet)
            return False
        row.result = RESULT_CANCELLED
        if not quiet:
            _err(_("Cancelled. {name} was not changed.").format(name=app.name))
        return cancel.is_set() or isinstance(exc, UpdateCancelled)
    except Exception as exc:  # noqa: BLE001 - a bug: report it, go on with the others
        _report_update_failure(row, exc, ctx, quiet=quiet)
        return cancel.is_set()
    row.result = RESULT_UPDATED
    if not quiet:
        _print_updated(row)
    return cancel.is_set()


def _result_text(row: UpdateRow) -> str:
    texts = {
        RESULT_UPDATED: _("updated"),
        RESULT_FAILED: _("failed"),
        RESULT_CANCELLED: _("cancelled"),
        RESULT_SKIPPED: _("skipped"),
        RESULT_NOT_CHECKED: _("could not be checked"),
        RESULT_AVAILABLE: _("not updated"),
        RESULT_UP_TO_DATE: _("up to date"),
    }
    return texts.get(row.result, row.result)


def _print_update_summary(rows: Sequence[UpdateRow]) -> None:
    shown = [row for row in rows if row.result != RESULT_UP_TO_DATE]
    if len(shown) < 2:
        return
    _out()
    _out(_bold(_("Summary:")))
    print_table([_("NAME"), _("FOR"), _("BEFORE"), _("NOW"), _("RESULT")],
                [[row.name, _scope_short(row.app.scope), _version_text(row.app.version),
                  _version_text(row.new.version if row.new is not None else row.app.version),
                  _result_text(row)] for row in shown])


def _update_exit_code(rows: Sequence[UpdateRow]) -> int:
    results = {row.result for row in rows}
    if results & {RESULT_FAILED, RESULT_NOT_CHECKED}:
        return EXIT_ERROR
    if results & {RESULT_CANCELLED, RESULT_SKIPPED, RESULT_AVAILABLE}:
        return EXIT_CANCELLED
    return EXIT_OK


def _install_updates(rows: list[UpdateRow], args: argparse.Namespace, ctx: Context) -> int:
    pending = [row for row in rows if row.update is not None]
    if not args.json:
        if pending:
            _print_update_table(pending)
        else:
            _print_up_to_date(rows)
        _print_check_errors(rows)
    if not pending:
        if args.json:
            _print_json([update_row_to_dict(row) for row in rows])
        return _update_exit_code(rows)
    if not args.yes:
        _out()
        # (Two sentences: every plural form of a translation must keep the {n}.)
        prompt = _("Install this update? [Y/n] ") if len(pending) == 1 else ngettext(
            "Install {n} update? [Y/n] ", "Install these {n} updates? [Y/n] ",
            len(pending)).format(n=len(pending))
        if not confirm(prompt, default=True):
            _err(_("Cancelled. Nothing was changed."))
            return EXIT_CANCELLED
    stop = False
    for row in pending:
        if stop:
            row.result = RESULT_CANCELLED
            continue
        if not args.json:
            _out()
        stop = _update_one(row, args, ctx)
    if args.json:
        _print_json([update_row_to_dict(row) for row in rows])
    else:
        _print_update_summary(rows)
    return _update_exit_code(rows)


def cmd_update(args: argparse.Namespace, ctx: Context) -> int:
    if args.ids and args.all:
        return _fail(_("Name the apps to update, or use --all - not both."), code=EXIT_USAGE)
    install = bool(args.ids or args.all) and not args.check
    if install and args.json and not args.yes:
        return _fail(_("To install updates with --json, add -y: there is no way to ask "
                       "questions then."), code=EXIT_USAGE)
    scopes = _selected_scopes(args)
    _refresh_installed(ctx, scopes, report=not args.json)
    selection = _select_for_update(args, scopes)
    if isinstance(selection, int):
        return selection
    rows, unchecked = selection
    _check_rows(rows, ctx, reuse_fresh=install)
    if not install:
        return _report_update_check(rows, unchecked, args)
    return _install_updates(rows, args, ctx)


# ------------------------------------------------------------------------------------------------
# rollback
# ------------------------------------------------------------------------------------------------


def _kept_since_text(previous: Mapping[str, Any]) -> str | None:
    saved = backups.saved_time(dict(previous))
    if saved is None:
        return None
    return _("kept since {date}").format(date=datetime.fromtimestamp(saved).strftime(
        "%Y-%m-%d %H:%M"))


def _no_backup_hint() -> str:
    days = settings.load_settings().backup_days
    if days <= 0:
        return _("Keeping the replaced version is turned off. To turn it on, run: {command}").format(
            command=_command("settings", "backup-days", str(settings.DEFAULT_BACKUP_DAYS)))
    return ngettext("After an update, the replaced version is kept for {n} day.",
                    "After an update, the replaced version is kept for {n} days.",
                    days).format(n=days)


def print_rollback_summary(plan: InstallPlan, app: InstalledApp) -> None:
    previous = app.previous or {}
    target = _version_text(plan.version or previous.get("version"))
    kept = _kept_since_text(previous)
    scope = _scope_text(plan.scope)
    if plan.requires_root:
        scope = _("{scope} (asks for your password)").format(scope=scope)
    _out(_bold(_summary_heading(plan, app.name)))
    _out()
    _print_fields([
        (_("Installed now:"), _version_text(app.version)),
        (_("Go back to:"), f"{target} ({kept})" if kept else target),
        (_("Install for:"), scope),
        (_("Change:"), _change_text(plan)),
        (_("Sandbox:"), _sandbox_text(plan.sandbox_fix)),
    ])
    if plan.warnings:
        _out()
        _print_notes(_("Please note:"), plan.warnings)
    _out()


def cmd_rollback(args: argparse.Namespace, ctx: Context) -> int:
    app = _pick_installation(
        args, question=_("Which one do you want to take back to its previous version? "
                         "[{choices}, Enter = cancel] "),
        hint=_("Add --user or --system to choose which one to take back."),
        prefer=lambda candidate: bool(candidate.previous))
    if isinstance(app, int):
        return app
    if not app.previous:
        return _fail(_("There is no previous version of {name} to go back to.").format(
            name=app.name), _no_backup_hint())
    # An app that replaced its own file: the version it has now is what is kept.
    app = next((r.app for r in _refresh_installed(ctx, [Scope(app.scope)], apps=[app])), app)
    plan = installer.plan_rollback(app.id, app.scope)
    try:
        shown_warnings = list(plan.warnings)
        print_rollback_summary(plan, app)
        if not args.yes and not confirm(_("Proceed? [Y/n] "), default=True):
            _err(_("Cancelled. Nothing was changed."))
            return EXIT_CANCELLED
        if plan.requires_root:
            _out(_("You may be asked for your password."))
        new = installer.execute_install(plan, progress=ctx.progress)
        ctx.progress.clear()
    finally:
        plan.info.cleanup()
    _out(f"{_mark_ok()} " + _bold(_("{name} is back at version {version}.").format(
        name=new.name, version=_version_text(new.version))))
    if new.previous:
        _print_wrapped(_("Version {version} is kept. To switch back to it, run: {command}").format(
            version=_version_text((new.previous or {}).get("version")),
            command=_command("rollback", new.id, *_scope_args(new))), "  ")
    new_warnings = [w for w in plan.warnings if w not in shown_warnings]
    if new_warnings:
        _print_notes(_("Please note:"), new_warnings)
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# repair
# ------------------------------------------------------------------------------------------------


def cmd_repair(args: argparse.Namespace, ctx: Context) -> int:
    app = _pick_installation(
        args, question=_("Which one do you want to repair? [{choices}, Enter = cancel] "),
        hint=_("Add --user or --system to choose which one to repair."))
    if isinstance(app, int):
        return app
    if app.kind != KIND_PORTABLE and app.status() == STATUS_MISSING_APPIMAGE:
        return _fail(_("The app file of {name} is missing: {path}").format(
            name=app.name, path=display_text(app.appimage_path)),
            _("Install the app again to repair it."))
    if Scope(app.scope) is Scope.SYSTEM:
        _out(_("You may be asked for your password."))
    new = installer.repair(app.id, app.scope, progress=ctx.progress)
    ctx.progress.clear()
    _out(f"{_mark_ok()} " + _("{name} was repaired.").format(name=new.name))
    _out("  " + _("Its menu entry and icon were made again."))
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# details
# ------------------------------------------------------------------------------------------------


def _data_locations(app: InstalledApp) -> tuple[list[DataLocation], list[InstalledApp]]:
    """The settings and data folders of ``app``, and the other installations sharing them."""
    others = installer.other_installations(app)
    if not others:
        return installer.removable_data(app), []
    try:
        user = installer.layout_for(Scope.USER)
        exclude = [Path(path) for path in (app.install_dir, app.appimage_path, app.desktop_path,
                                           (app.previous or {}).get("path")) if path]
        exclude += [installer.layout_for(Scope(app.scope)).apps_dir, user.apps_dir,
                    user.backups_dir, user.registry_dir]
        return appdata.find_app_data(installer.data_hints_of(app), exclude=exclude), others
    except Exception:  # noqa: BLE001 - the folders are extra information
        log.warning("cannot look for the data of %s", app.id, exc_info=True)
        return [], others


def _previous_until(app: InstalledApp) -> float | None:
    saved = backups.saved_time(app.previous) if app.previous else None
    if saved is None:
        return None
    return saved + settings.load_settings().backup_days * _SECONDS_PER_DAY


def _previous_usable(app: InstalledApp) -> bool:
    try:
        layout = installer.layout_for(Scope(app.scope))
        return backups.usable_previous(layout, app) is not None
    except Exception:  # noqa: BLE001
        return False


@dataclass
class AppDetails:
    """Everything ``details`` shows about one installation."""

    app: InstalledApp
    update: AvailableUpdate | None
    checked_at: str | None
    data: list[DataLocation]
    shared_with: list[InstalledApp]
    #: the last check could not be answered
    check_failed: bool = False


def _collect_details(app: InstalledApp, ctx: Context) -> AppDetails:
    ctx.progress(None, _("Looking for the settings and data of {name}…").format(name=app.name))
    try:
        data, shared_with = _data_locations(app)
    finally:
        ctx.progress.clear()
    update = updater.cached_updates().get(_update_key(app)) \
        if updater.has_update_source(app) else None
    try:
        cache = UpdateCache()
        checked_at = cache.checked_at(Scope(app.scope).value, app.id)
        failed = cache.failed(Scope(app.scope).value, app.id)
    except Exception:  # noqa: BLE001 - the cache is a nicety
        checked_at, failed = None, False
    return AppDetails(app=app, update=update, checked_at=checked_at, data=data,
                      shared_with=shared_with, check_failed=failed)


def details_to_dict(details: AppDetails) -> dict[str, Any]:
    """JSON of ``details`` (stable keys): the ``list --json`` entry plus what was looked up."""
    app = details.app
    source = updater.update_source_of(app)
    until = _previous_until(app)
    return {
        **app_to_dict(app, details.update),
        "can_check_updates": updater.has_update_source(app),
        "source": source.describe() if source is not None else None,
        "source_homepage": source.homepage() if source is not None else None,
        "last_checked": details.checked_at,
        "last_check_failed": details.check_failed,
        "origin_host": origin_host(app.origin_url),
        "previous_available": _previous_usable(app),
        "previous_kept_until": datetime.fromtimestamp(until, timezone.utc).strftime(
            _REGISTRY_TIME_FORMAT) if until is not None else None,
        "data": [{"path": display_text(str(loc.path)), "kind": loc.kind, "size": loc.size,
                  "file_count": loc.file_count} for loc in details.data],
        "data_size": sum(loc.size for loc in details.data),
        "data_shared_with": [{"id": other.id, "scope": Scope(other.scope).value,
                              "version": other.version} for other in details.shared_with],
    }


def _kind_text(app: InstalledApp) -> str:
    if app.kind == KIND_PORTABLE:
        return _("portable app, unpacked from an archive into its own folder")
    return _("AppImage")


def _updates_text(details: AppDetails) -> str:
    app = details.app
    if app.kind == KIND_PORTABLE:
        return _("not checked (the app was installed from an archive)")
    if app.base_id or app.pinned:
        return _("never (this copy stays at its version)")
    source = updater.update_source_of(app)
    if source is None or not updater.has_update_source(app):
        return _("not checked (the app does not say where its new versions are published)")
    text = _("from {source}").format(source=source.describe())
    checked = _local_time(details.checked_at)
    if checked and details.check_failed:
        text += "\n" + _("last checked {time}, but that check failed (to see why, run: "
                         "{command})").format(time=checked, command=_command(
                             "update", "--check", app.id, *_scope_args(app)))
    elif checked:
        text += "\n" + _("last checked {time}").format(time=checked)
    return text


def _new_version_text(details: AppDetails) -> str | None:
    update, app = details.update, details.app
    if update is None:
        return None
    text = _("{version} is available").format(version=_update_cell(update))
    if update.size:
        text = _("{version} is available ({size})").format(version=_update_cell(update),
                                                             size=_format_size(update.size))
    return text + "\n" + _("To install it, run: {command}").format(
        command=_command("update", app.id, *_scope_args(app)))


def _previous_text(app: InstalledApp) -> str | None:
    previous = app.previous
    if not previous:
        return None
    version = _version_text(previous.get("version"))
    if not _previous_usable(app):
        return _("{version} (its files are gone)").format(version=version)
    until = _previous_until(app)
    if Scope(app.scope) is Scope.SYSTEM and until is not None and until < time.time():
        # (pruned by the administrator helper, which only runs for an installation)
        text = _("{version}, deleted the next time an app is installed or updated for "
                 "everyone").format(version=version)
    elif until is not None:
        text = _("{version}, kept until {date}").format(version=version, date=_local_date(until))
    else:
        text = version
    return text + "\n" + _("To go back to it, run: {command}").format(
        command=_command("rollback", app.id, *_scope_args(app)))


def _copy_of_text(app: InstalledApp) -> str | None:
    if not app.base_id:
        return None
    return _("{id} (this copy was kept next to another version)").format(id=app.base_id)


def _location_rows(app: InstalledApp) -> list[tuple[str, str | None]]:
    if app.kind == KIND_PORTABLE:
        return [(_("App folder:"), display_text(app.install_dir or "")),
                (_("Program:"), display_text(app.appimage_path))]
    return [(_("App file:"), display_text(app.appimage_path))]


def _details_sandbox_text(app: InstalledApp) -> str | None:
    if app.sandbox_fix == SandboxFix.APPARMOR.value:
        return _("allowed with a system permission")
    if app.sandbox_fix == SandboxFix.NO_SANDBOX.value:
        return _("started without its security sandbox")
    return None


def print_details(details: AppDetails) -> None:
    app = details.app
    _out(_bold(app.name))
    _print_fields([
        (_("App ID:"), app.id),
        (_("Version:"), _version_text(app.version)),
        (_("Installed for:"), _scope_text(app.scope)),
        (_("Kind:"), _kind_text(app)),
        (_("Copy of:"), _copy_of_text(app)),
        (_("Status:"), _status_text(app.status())),
        *_location_rows(app),
        (_("Menu entry:"), display_text(app.desktop_path) or None),
        (_("App size:"), _size_text(app.size)),
        (_("Installed on:"), _local_time(app.installed_at)),
        (_("Last changed:"), _local_time(app.updated_at)
         if app.updated_at and app.updated_at != app.installed_at else None),
        (_("Installed from:"), display_text(app.original_filename) or None),
        (_("Updates:"), _updates_text(details)),
        (_("New version:"), _new_version_text(details)),
        (_("Downloaded from:"), _origin_text(app.origin_url, full=True)),
        (_("Signature:"), None if app.kind == KIND_PORTABLE   # archives carry no signature
         else _signature_text(app.signature) or _("not signed (most apps are not)")),
        (_("Previous version:"), _previous_text(app)),
        (_("Sandbox:"), _details_sandbox_text(app)),
        (_("Start mode:"), _("unpacks itself on every start (a little slower)")
         if app.extract_and_run else None),
    ])
    _out()
    if not details.data:
        _out(_("No settings or data folders were found."))
        return
    _out(_("Settings and data:"))
    _print_data_locations(details.data)
    total = sum(loc.size for loc in details.data)
    _out("  " + _("Together: {size}").format(size=_format_size(total) if total else _("empty")))
    if details.shared_with:
        _print_wrapped(_("Another installation of this app uses them, too."), "  ")
    else:
        _print_wrapped(_("They are kept when the app is uninstalled. To move them to the trash "
                         "as well, run: {command}").format(
                             command=_command("uninstall", app.id, "--delete-data",
                                              *_scope_args(app))), "  ")


def cmd_details(args: argparse.Namespace, ctx: Context) -> int:
    candidates = _matching_installations(args)
    if isinstance(candidates, int):
        return candidates
    if args.json and len(candidates) > 1:
        return _fail(_("{name} is installed both just for you and for everyone on this "
                       "computer.").format(name=candidates[0].name),
                     _("Add --user or --system to choose which one to show."), EXIT_USAGE)
    results = _refresh_installed(ctx, {Scope(app.scope) for app in candidates}, apps=candidates,
                                 report=not args.json)
    current = {(Scope(r.app.scope), r.app.id): r.app for r in results}
    apps = [current.get((Scope(app.scope), app.id), app) for app in candidates]
    collected = [_collect_details(app, ctx) for app in apps]
    if args.json:
        _print_json(details_to_dict(collected[0]))
        return EXIT_OK
    for index, details in enumerate(collected):
        if index:
            _out()
        print_details(details)
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# settings
# ------------------------------------------------------------------------------------------------


def _setting_key(value: str) -> str | None:
    key = value.strip().lower().replace("_", "-")
    return key if key in SETTING_KEYS else None


def _setting_value(current: settings.Settings, key: str) -> bool | int:
    return current.to_dict()[SETTING_KEYS[key]]


def _setting_text(value: bool | int) -> str:
    return _yes_no(value) if isinstance(value, bool) else str(value)


def _parse_flag(text: str) -> bool | None:
    word = text.strip().casefold()
    if word in _yes_words() | {"true", "on", "1"}:
        return True
    if word in _no_words() | {"false", "off", "0"}:
        return False
    return None


def _parse_setting(key: str, text: str) -> bool | int | None:
    """The value for ``key`` typed on the command line; None if it is not allowed."""
    if key == "check-updates":
        return _parse_flag(text)
    low, high = _SETTING_RANGES[key]
    try:
        number = int(text.strip())
    except ValueError:
        return None
    return number if low <= number <= high else None


def _invalid_setting(key: str) -> int:
    if key == "check-updates":
        message = _("{key} must be yes or no.").format(key=key)
    else:
        low, high = _SETTING_RANGES[key]
        message = _("{key} must be a whole number from {low} to {high}.").format(
            key=key, low=low, high=high)
    return _fail(message, code=EXIT_USAGE)


def _print_settings(current: settings.Settings, keys: Sequence[str], as_json: bool) -> None:
    values = {key: _setting_value(current, key) for key in keys}
    if as_json:
        _print_json(values)
        return
    if len(keys) == 1:
        _out(_setting_text(values[keys[0]]))
        return
    _out(_bold(_("Settings")) + " " + _("(saved in {path})").format(
        path=_short_path(settings_path())))
    width = max(_text_width(key) for key in keys)
    value_width = max(_text_width(_setting_text(v)) for v in values.values())
    for key in keys:
        text = _setting_text(values[key])
        _out(f"  {_pad(key, width)}  {_pad(text, value_width)}  {_setting_description(key)}")
    _out()
    _out(_("To change a setting, run: {command}").format(command=_command("settings", "KEY",
                                                                          "VALUE")))


def _after_setting(key: str, old: bool | int, new: bool | int) -> None:
    """Notes about what a changed setting means right away."""
    if key != "backup-days" or not isinstance(new, int) or not isinstance(old, int) or new >= old:
        return
    pruned = installer.prune_backups(new)
    if pruned:
        _out("  " + ngettext("{n} previous version was kept longer than that and was deleted.",
                             "{n} previous versions were kept longer than that and were deleted.",
                             len(pruned)).format(n=len(pruned)))
    if new == 0:
        _out("  " + _("From now on, the replaced version of an app is not kept after an update."))


def cmd_settings(args: argparse.Namespace, ctx: Context) -> int:
    current = settings.load_settings()
    if args.key is None:
        _print_settings(current, list(SETTING_KEYS), args.json)
        return EXIT_OK
    key = _setting_key(args.key)
    if key is None:
        return _fail(_("There is no setting called \"{key}\".").format(key=args.key),
                     _("The settings are: {keys}").format(keys=", ".join(SETTING_KEYS)),
                     EXIT_USAGE)
    if args.value is None:
        _print_settings(current, [key], args.json)
        return EXIT_OK
    value = _parse_setting(key, args.value)
    if value is None:
        return _invalid_setting(key)
    old = _setting_value(current, key)
    setattr(current, SETTING_KEYS[key], value)
    settings.save_settings(current)
    saved = settings.load_settings()
    if _setting_value(saved, key) != value:
        return _fail(_("The setting could not be saved."),
                     _("Please check that you may write to {path}.").format(
                         path=_short_path(settings_path())))
    if args.json:
        _print_settings(saved, [key], True)
    else:
        _out(f"{_mark_ok()} " + _("{key} is now {value}.").format(key=key,
                                                                  value=_setting_text(value)))
    _after_setting(key, old, value)
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# launch
# ------------------------------------------------------------------------------------------------


@dataclass
class LaunchCommand:
    argv: list[str]
    env: dict[str, str]
    cwd: str | None


def parse_exec_command(value: str) -> tuple[dict[str, str], list[str]] | None:
    """Split a desktop ``Exec`` value into (env assignments, argv) without field codes."""
    try:
        tokens = split_exec(value)
    except ValueError:
        return None
    env: dict[str, str] = {}
    if tokens and tokens[0] == "env":
        index = 1
        while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
            name, _sep, val = tokens[index].partition("=")
            env[name] = val
            index += 1
        tokens = tokens[index:]
    argv = [token for token in tokens if token not in _ALL_FIELD_CODES]
    return (env, argv) if argv else None


def _read_desktop_entry(path: str) -> DesktopEntry | None:
    if not path:
        return None
    try:
        return DesktopEntry.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.info("cannot read launcher %s: %s", path, exc)
        return None


def _same_file(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _registry_command(app: InstalledApp) -> tuple[dict[str, str], list[str]]:
    argv = [app.appimage_path]
    if app.sandbox_fix == SandboxFix.NO_SANDBOX.value:
        argv.append(NO_SANDBOX_ARG)
    env = {EXTRACT_AND_RUN_VAR: "1"} if app.extract_and_run else {}
    return env, argv


def launch_command(app: InstalledApp, environ: Mapping[str, str] | None = None) -> LaunchCommand:
    """The command the app's menu entry would run (without file arguments).

    Falls back to the registry data when the launcher is missing, unreadable or does not start
    the registered AppImage.
    """
    entry = _read_desktop_entry(app.desktop_path)
    parsed = None
    cwd = None
    if entry is not None:
        parsed = parse_exec_command(entry.get("Exec") or "")
        if parsed is not None and not _same_file(parsed[1][0], app.appimage_path):
            log.warning("the launcher %s does not start %s; using the registered settings",
                        app.desktop_path, app.appimage_path)
            parsed = None
        work_dir = entry.get("Path")
        if work_dir and os.path.isdir(work_dir):
            cwd = work_dir
    env_vars, argv = parsed if parsed is not None else _registry_command(app)
    env = dict(os.environ if environ is None else environ)
    env.update(env_vars)
    if cwd is None and app.kind == KIND_PORTABLE and app.install_dir \
            and os.path.isdir(app.install_dir):
        cwd = app.install_dir
    if cwd is None:
        home = home_dir()
        cwd = str(home) if home.is_dir() else None
    return LaunchCommand(argv=argv, env=env, cwd=cwd)


def cmd_launch(args: argparse.Namespace, ctx: Context) -> int:
    found = installer.find_installed(args.id)
    if not found:
        return _not_installed(args.id)
    # Like the app menu: the per-user installation wins, but a broken one must not hide a working one.
    usable = [a for a in found if a.status() != STATUS_MISSING_APPIMAGE] or found
    app = next((a for a in usable if a.scope is Scope.USER), usable[0])
    if app.status() == STATUS_MISSING_APPIMAGE:
        return _fail(_("The app file of {name} is missing: {path}").format(
            name=app.name, path=app.appimage_path),
            _("Install the app again to repair it."))
    command = launch_command(app)
    log.info("starting %s", " ".join(shlex.quote(a) for a in command.argv))
    try:
        subprocess.Popen(
            command.argv,
            env=command.env,
            cwd=command.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise EasyInstallerError(_("{name} could not be started.").format(name=app.name),
                                 details=f"{command.argv!r}: {exc}") from exc
    _out(_("Starting {name}…").format(name=app.name))
    return EXIT_OK


# ------------------------------------------------------------------------------------------------
# check
# ------------------------------------------------------------------------------------------------


@dataclass
class Check:
    id: str
    ok: bool
    label: str
    detail: str
    hint: str | None = None
    important: bool = True


def _install_hint(package: str, status: SystemStatus) -> str:
    if status.has_apt:
        return _("To fix: sudo apt install {package}").format(package=package)
    return _("To fix: install the package \"{package}\" with the software manager of your "
             "system.").format(package=package)


def _found(path: str | None) -> str:
    return path if path else _("not installed")


def _computer_name(status: SystemStatus, os_release: Mapping[str, str]) -> str:
    pretty = os_release.get("PRETTY_NAME")
    if pretty:
        return pretty
    parts = [p for p in (status.distro_id, status.distro_version) if p]
    return " ".join(parts) or _("unknown Linux system")


def _sandbox_check(status: SystemStatus) -> Check:
    label = _("Security sandbox of Electron apps")
    if not status.userns_restricted:
        return Check("userns", True, label, _("allowed"))
    if default_sandbox_fix(status) is SandboxFix.APPARMOR:
        return Check("userns", True, label, _(
            "restricted by the system; Easy Installer adds a permission for each app that needs "
            "it (asks for your password once)"))
    if not status.apparmor_parser:
        hint = _install_hint("apparmor", status)
    elif not status.pkexec:
        hint = _install_hint("pkexec", status)
    else:
        hint = None
    return Check("userns", False, label, _(
        "restricted by the system; such apps will be started without their sandbox"), hint)


def _apparmor_check(status: SystemStatus) -> Check:
    label = _("AppArmor security system")
    if not status.apparmor_enabled:
        return Check("apparmor", True, label, _("not active (nothing to do)"), important=False)
    if status.apparmor_parser:
        return Check("apparmor", True, label, _("active ({path})").format(path=status.apparmor_parser),
                     important=status.userns_restricted)
    return Check("apparmor", False, label, _("active, but its tools are not installed"),
                 _install_hint("apparmor", status), important=status.userns_restricted)


def system_checks_report(status: SystemStatus) -> list[Check]:
    fuse_note = _("Without it, apps still work but start a little slower.")
    checks = [
        Check("unsquashfs", bool(status.unsquashfs),
              _("Read apps without starting them (unsquashfs)"), _found(status.unsquashfs),
              None if status.unsquashfs else _install_hint("squashfs-tools", status)),
        Check("libfuse2", status.libfuse2, _("FUSE library for AppImages (libfuse2)"),
              _("installed") if status.libfuse2 else _("not installed"),
              None if status.libfuse2 else
              f"{_install_hint(status.libfuse2_package, status)} {fuse_note}"),
        Check("fusermount", bool(status.fusermount), _("FUSE mount tool (fusermount)"),
              _found(status.fusermount),
              None if status.fusermount else f"{_install_hint('fuse3', status)} {fuse_note}"),
        Check("dev_fuse", status.dev_fuse, _("FUSE device (/dev/fuse)"),
              _("available") if status.dev_fuse else _("missing"),
              None if status.dev_fuse else
              _("To fix: sudo modprobe fuse (or restart the computer).") + f" {fuse_note}"),
        _sandbox_check(status),
        _apparmor_check(status),
        Check("pkexec", bool(status.pkexec),
              _("Password prompt for installing for everyone (pkexec)"), _found(status.pkexec),
              None if status.pkexec else _install_hint("pkexec", status)),
        Check("update_desktop_database", bool(status.update_desktop_database),
              _("App menu refresh (update-desktop-database)"), _found(status.update_desktop_database),
              None if status.update_desktop_database else _install_hint("desktop-file-utils", status),
              important=False),
        Check("icon_cache", bool(status.icon_cache_tool),
              _("Icon refresh (gtk-update-icon-cache)"), _found(status.icon_cache_tool),
              None if status.icon_cache_tool else _install_hint("gtk-update-icon-cache", status),
              important=False),
    ]
    return checks


def _status_to_dict(status: SystemStatus) -> dict[str, Any]:
    data = dataclasses.asdict(status)
    data["distro_like"] = list(status.distro_like)
    return data


def cmd_check(args: argparse.Namespace, ctx: Context) -> int:
    status = system_checks.get_system_status()
    os_release = system_checks.read_os_release()
    checks = system_checks_report(status)
    problems = [c for c in checks if not c.ok and c.important]
    optional_missing = [c for c in checks if not c.ok and not c.important]
    if args.json:
        _print_json({
            "ready": not problems,
            "problems": len(problems),
            "computer": {
                "name": _computer_name(status, os_release),
                "id": status.distro_id,
                "like": list(status.distro_like),
                "version": status.distro_version,
                "host_arch": host_arch(),
            },
            "checks": [dataclasses.asdict(c) for c in checks],
            "status": _status_to_dict(status),
        })
        return EXIT_OK

    _out(_bold(_("Computer: {name}").format(name=_computer_name(status, os_release))))
    _out()
    width = max(_text_width(c.label) for c in checks)
    for check in checks:
        mark = _mark_ok() if check.ok else _mark_bad()
        _out(f"  {mark} {_pad(check.label, width)}  {check.detail}")
        if check.hint:
            for line in textwrap.wrap(check.hint, width=_terminal_columns() - 6):
                _out(f"      {line}")
    _out()
    if problems:
        _out(ngettext("{n} problem found. See the tips above.",
                      "{n} problems found. See the tips above.", len(problems)).format(n=len(problems)))
    elif optional_missing:
        _out(_("Everything important is ready. Some optional tools are missing (see above)."))
    else:
        _out(_("Everything is ready for installing AppImages."))
    return EXIT_OK
