"""Entry point of the privileged helper: ``<helper> <op>`` with one JSON object on stdin.

Started through pkexec (``data/easy-installer-helper`` when installed, ``helper/entry.py`` in a
source checkout). Prints exactly one JSON object on stdout - ``{"ok": true, ...}`` or
``{"ok": false, "error": <friendly message>, "details": <technical text>}`` - and exits with 0 or 1.
Logging goes to stderr; nothing else is ever written to stdout, not even on crashes.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .. import __version__
from ..core.paths import Layout, system_layout
from ..core.system_checks import get_system_status
from ..errors import EasyInstallerError, HelperError
from ..i18n import _
from . import ops

log = logging.getLogger(__name__)

OPS = ("install", "uninstall", "apparmor-install", "apparmor-remove", "install-fuse",
       "drop-backup")
MAX_INPUT_BYTES = 1 << 20
MAX_DETAILS = 8000
MAX_UID = 2**32 - 2
EXIT_OK = 0
EXIT_ERROR = 1
#: Run update-desktop-database, apparmor_parser, apt-get, ... (tests switch this off).
RUN_COMMANDS = True

_UID_RE = re.compile(r"[0-9]{1,10}")


@dataclass(frozen=True)
class Caller:
    uid: int
    gid: int
    home: Path
    name: str


def target_layout() -> Layout:
    """Destinations always come from the real system layout (tests monkeypatch this)."""
    return system_layout(Path("/"))


def _started_wrongly(details: str) -> HelperError:
    return HelperError(_("Easy Installer's administrator helper was started in an unexpected way."),
                       details=details)


def resolve_caller(environ: Mapping[str, str], euid: int) -> Caller:
    """Who asked for this? ``PKEXEC_UID`` is mandatory when running as root."""
    raw = environ.get("PKEXEC_UID")
    if raw is None:
        if euid == 0:
            raise _started_wrongly("PKEXEC_UID is not set; the helper must be started with pkexec")
        uid = os.getuid()
    else:
        if not _UID_RE.fullmatch(raw) or int(raw) > MAX_UID:
            raise _started_wrongly(f"PKEXEC_UID is not a valid user id: {raw[:40]!r}")
        uid = int(raw)
        if euid != 0 and uid != os.getuid():
            raise _started_wrongly(f"PKEXEC_UID={uid} but the helper runs as uid {os.getuid()}")
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        if euid == 0:
            raise _started_wrongly(f"no user with uid {uid}") from None
        return Caller(uid=uid, gid=os.getgid(), home=Path.home(), name=str(uid))
    return Caller(uid=uid, gid=entry.pw_gid, home=Path(entry.pw_dir), name=entry.pw_name)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def read_request(stream: BinaryIO | TextIO | None) -> dict:
    """Read at most 1 MiB of UTF-8 JSON; it must be an object."""
    if stream is None:
        raise ops.RequestError("no standard input")
    data = stream.read(MAX_INPUT_BYTES + 1)
    if isinstance(data, str):
        data = data.encode("utf-8", errors="surrogateescape")
    if len(data) > MAX_INPUT_BYTES:
        raise ops.RequestError(f"request is larger than {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ops.RequestError(f"request is not valid JSON: {type(exc).__name__}: {str(exc)[:200]}") \
            from None
    if not isinstance(value, dict):
        raise ops.RequestError(f"request must be a JSON object, got {type(value).__name__}")
    return value


def dispatch(op: str, request: dict, caller: Caller, *, layout: Layout, run_commands: bool) -> dict:
    if op == "install":
        return ops.op_install(request, layout=layout, caller_uid=caller.uid, caller_gid=caller.gid,
                              run_commands=run_commands)
    if op == "uninstall":
        return ops.op_uninstall(request, layout=layout, run_commands=run_commands)
    if op == "apparmor-install":
        return ops.op_apparmor_install(request, layout=layout, caller_uid=caller.uid,
                                       caller_home=caller.home, caller_gid=caller.gid,
                                       run_commands=run_commands)
    if op == "apparmor-remove":
        return ops.op_apparmor_remove(request, layout=layout, run_commands=run_commands,
                                      caller_home=caller.home)
    if op == "install-fuse":
        return ops.op_install_fuse(request, status=get_system_status(refresh=True),
                                   run_commands=run_commands)
    if op == "drop-backup":
        return ops.op_drop_backup(request, layout=layout, run_commands=run_commands)
    raise ops.RequestError(f"unknown operation {op[:40]!r}")


def run(argv: Sequence[str], stdin: BinaryIO | TextIO | None, environ: Mapping[str, str]) -> dict:
    """Validate the command line, identify the caller, read the request and run the operation."""
    if len(argv) != 2:
        raise ops.RequestError(f"expected exactly one operation argument, got {len(argv) - 1}")
    op = argv[1]
    if op not in OPS:
        raise ops.RequestError(f"unknown operation {op[:40]!r}")
    caller = resolve_caller(environ, os.geteuid())
    request = read_request(stdin)
    client = request.get("client_version")
    if client is not None and client != __version__:
        # Another version's request may mean something else, and this helper would drop from
        # the registry what a newer version recorded: nothing is done.
        raise HelperError(
            _("The part of Easy Installer that works for everyone on this computer belongs to "
              "another version of Easy Installer. Nothing was changed. Please restart Easy "
              "Installer, or install it again."),
            details=f"helper {__version__}, program {str(client)[:64]}")
    log.info("operation %s requested by uid %d", op, caller.uid)
    result = dispatch(op, request, caller, layout=target_layout(), run_commands=RUN_COMMANDS)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise HelperError(_("The administrator task failed unexpectedly."),
                          details=f"operation {op} returned {type(result).__name__}")
    return result


def error_result(exc: BaseException) -> dict:
    if isinstance(exc, EasyInstallerError):
        message, details = exc.message, exc.details or ""
    else:
        message = _("The administrator task failed unexpectedly.")
        details = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "error": message, "details": details[:MAX_DETAILS]}


def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "easy-installer-helper[%(process)d] %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _emit(result: dict, stream: TextIO) -> int:
    try:
        text = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        log.error("helper result is not serialisable: %s", exc)
        result = error_result(exc)
        text = json.dumps(result, ensure_ascii=True)
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, ValueError) as exc:
        log.error("cannot write the result: %s", exc)
        return EXIT_ERROR
    return EXIT_OK if result.get("ok") is True else EXIT_ERROR


def _ignore_ctrl_c() -> None:
    """The operation is saved: finish it (and answer truthfully) even if Ctrl+C comes now.

    Commands started from here on (update-desktop-database, ...) inherit the ignored signal.
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:  # not in the main thread
        pass


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _setup_logging()
    try:
        previous_sigint = signal.getsignal(signal.SIGINT)
    except ValueError:
        previous_sigint = None
    if os.geteuid() == 0:
        try:
            os.chdir("/")
        except OSError:
            pass
    old_umask = os.umask(0o022)
    real_stdout = sys.stdout
    # Anything printed by accident (a library, a warning) must not corrupt the JSON answer.
    sys.stdout = sys.stderr
    previous_hook, ops.on_committed = ops.on_committed, _ignore_ctrl_c
    try:
        stdin = sys.stdin
        stream = getattr(stdin, "buffer", stdin) if stdin is not None else None
        result = run(argv, stream, os.environ)
    except BaseException as exc:  # noqa: BLE001 - the helper must always answer with JSON
        if isinstance(exc, EasyInstallerError):
            log.warning("operation failed: %s (%s)", exc.message, exc.details)
        else:
            log.error("operation crashed", exc_info=True)
        result = error_result(exc)
    finally:
        ops.on_committed = previous_hook
        sys.stdout = real_stdout
        os.umask(old_umask)
    code = _emit(result, real_stdout)
    if previous_sigint is not None and signal.getsignal(signal.SIGINT) is not previous_sigint:
        signal.signal(signal.SIGINT, previous_sigint)  # the answer is out
    return code
