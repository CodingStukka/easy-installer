"""Client side of the privileged helper (runs ``pkexec <helper> <op>`` with JSON on stdin/stdout)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import IO, Any

from .. import __version__
from ..errors import AuthorizationError, HelperError
from ..i18n import _

log = logging.getLogger(__name__)

HELPER_INSTALLED_PATH = "/usr/libexec/easy-installer/easy-installer-helper"
#: Where `make install` with its default PREFIX=/usr/local puts the helper.
HELPER_LOCAL_PATH = "/usr/local/libexec/easy-installer/easy-installer-helper"
HELPER_INSTALLED_PATHS = (HELPER_INSTALLED_PATH, HELPER_LOCAL_PATH)
DEV_PYTHON = "/usr/bin/python3"
PKEXEC_FALLBACKS = ("/usr/bin/pkexec", "/bin/pkexec")
#: "drop-backup" is new in 0.2 (delete the kept previous version of a system-wide app).
HELPER_OPS = frozenset({"install", "uninstall", "apparmor-install", "apparmor-remove", "install-fuse",
                        "drop-backup"})

# pkexec exit codes: 126 = the authentication dialog was dismissed,
# 127 = not authorized / authentication failed.
PKEXEC_DISMISSED = 126
PKEXEC_NOT_AUTHORIZED = 127

_DETAILS_LIMIT = 4000


def pkexec_disabled() -> bool:
    return os.environ.get("EASY_INSTALLER_DISABLE_PKEXEC", "").strip() not in ("", "0")


_REAL_POPEN = subprocess.Popen


def _would_prompt_in_test_run(cmd: list[str]) -> bool:
    """Safety interlock: a test run must never reach the real password prompt.

    Even if a test undid its environment isolation, running the real pkexec through the real
    Popen is refused while pytest runs (PYTEST_CURRENT_TEST is inherited by subprocesses).
    Tests that fake the subprocess or the helper command are unaffected.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST") or subprocess.Popen is not _REAL_POPEN:
        return False
    real = {os.path.realpath(p) for p in (shutil.which("pkexec"), *PKEXEC_FALLBACKS) if p}
    return bool(cmd) and os.path.realpath(cmd[0]) in real


def find_pkexec() -> str | None:
    found = shutil.which("pkexec")
    if found:
        return found
    for candidate in PKEXEC_FALLBACKS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def dev_entry_script() -> Path:
    return Path(__file__).resolve().parent.parent / "helper" / "entry.py"


def installed_helper_usable(path: str = HELPER_INSTALLED_PATH) -> bool:
    """The packaged helper exists, is a regular file owned by root and not writable by others."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022


def helper_command() -> list[str]:
    pkexec = find_pkexec()
    if pkexec is None:
        raise HelperError(
            _("Administrator tasks are not possible on this computer because the "
              "password prompt (pkexec) is not installed."),
            details="pkexec not found",
        )
    for path in HELPER_INSTALLED_PATHS:
        if installed_helper_usable(path):
            return [pkexec, path]
    return [pkexec, DEV_PYTHON, str(dev_entry_script())]


_VERSION_PROBE = ("import sys\n"
                  "if sys.argv[1]:\n"
                  "    sys.path.insert(0, sys.argv[1])\n"
                  "import easy_installer\n"
                  "sys.stdout.write(easy_installer.__version__)\n")
_LIBDIR_RE = re.compile(r'^_LIBDIR = "([^"\n]*)"', re.MULTILINE)


def installed_helper_version(path: str) -> str | None:
    """The version of Easy Installer that the installed helper ``path`` runs - found out
    without the password: its interpreter imports the same package as the user (never as
    root). None if it cannot be told."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(64 * 1024)
    except OSError:
        return None
    first = text.split("\n", 1)[0]
    interpreter = first[2:].split()[0] if first.startswith("#!") and first[2:].split() else ""
    if not os.path.isabs(interpreter):
        return None
    match = _LIBDIR_RE.search(text)
    try:
        proc = subprocess.run([interpreter, "-I", "-c", _VERSION_PROBE,
                               match.group(1) if match else ""],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                              check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return None
    version = proc.stdout.decode("utf-8", "replace").strip()
    return version if proc.returncode == 0 and 0 < len(version) <= 64 else None


def _helper_mismatch(helper: str | None) -> HelperError:
    return HelperError(
        _("The part of Easy Installer that works for everyone on this computer belongs to "
          "another version of Easy Installer. Nothing was changed. Please restart Easy "
          "Installer, or install it again."),
        details=f"helper {helper or 'unknown'}, program {__version__}")


def _check_installed_helper(cmd: list[str]) -> None:
    """A helper of another version would misread the request or drop what it does not know
    from the registry (an older one rewrites every entry without the newer fields)."""
    if len(cmd) < 2 or cmd[1] not in HELPER_INSTALLED_PATHS:
        return   # the helper script of this very program
    helper = installed_helper_version(cmd[1])
    if helper is not None and helper != __version__:
        raise _helper_mismatch(helper)


def _truncate(text: str) -> str:
    return text if len(text) <= _DETAILS_LIMIT else text[:_DETAILS_LIMIT] + "\n[...]"


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def parse_helper_output(stdout: str) -> dict[str, Any] | None:
    """The JSON object printed by the helper; tolerates extra noise lines around it."""
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _details(cmd: list[str], returncode: int | None, stdout: str, stderr: str) -> str:
    return _truncate(
        f"command: {' '.join(cmd)}\nexit code: {returncode}\n"
        f"stdout: {stdout.strip()}\nstderr: {stderr.strip()}"
    )


def _payload_file(data: bytes) -> IO[bytes]:
    """The request as the helper's stdin: an anonymous private file, read at the helper's pace."""
    fh = tempfile.TemporaryFile()
    fh.write(data)
    fh.flush()
    fh.seek(0)
    return fh


def _stop(proc: subprocess.Popen) -> bool:
    """Kill ``proc``; False if that is not allowed any more.

    Once pkexec has authenticated it becomes the helper running as root (same pid, real and
    saved uid 0), which the user may not signal.
    """
    try:
        proc.kill()
    except PermissionError:
        return False
    return True


def _communicate(proc: subprocess.Popen, op: str, cmd: list[str],
                 timeout: float | None) -> tuple[bytes, bytes, bool]:
    """(stdout, stderr, interrupted) of the finished helper; see :func:`run_helper`."""
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if _stop(proc):  # still at the password prompt (or not started yet)
                out, err = proc.communicate()
                raise HelperError(
                    _("The administrator task took too long and was stopped."),
                    details=_details(cmd, proc.returncode, _decode(out), _decode(err))) from None
            # Already running as root: it has its own time limits, and its answer says what
            # really happened (e.g. that the app was installed), so wait for it.
            log.warning("helper op %s still runs after %s s; waiting for it to finish", op, timeout)
            out, err = proc.communicate()
        return out, err, False
    except KeyboardInterrupt:
        # Ctrl+C in a terminal reaches the helper, too (same process group); it rolls back and
        # answers. It cannot be killed from here once it runs as root.
        _stop(proc)
        out, err = proc.communicate()
        return out, err, True


def run_helper(op: str, payload: dict, *, timeout: float | None = 600) -> dict:
    if op not in HELPER_OPS:
        raise ValueError(f"unknown helper operation: {op!r}")
    if pkexec_disabled():
        raise HelperError(
            _("Administrator tasks are turned off in this environment."),
            details=f"EASY_INSTALLER_DISABLE_PKEXEC is set; refused to run helper op {op!r}",
        )
    cmd = helper_command() + [op]
    if _would_prompt_in_test_run(cmd):
        raise HelperError(
            _("Administrator tasks are turned off in this environment."),
            details=f"refused to run the real pkexec during a test run (helper op {op!r})",
        )
    _check_installed_helper(cmd)
    try:
        # (a helper of this version or newer refuses a request of another version itself)
        data = json.dumps({**payload, "client_version": __version__}).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HelperError(_("Something went wrong while preparing the administrator task."),
                          details=str(exc)) from exc
    log.info("running privileged helper: %s", " ".join(cmd))
    try:
        with _payload_file(data) as stdin:
            proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
    except OSError as exc:
        raise HelperError(_("The administrator task could not be started."),
                          details=f"{' '.join(cmd)}: {exc}") from exc
    out, err, interrupted = _communicate(proc, op, cmd, timeout)

    stdout, stderr = _decode(out), _decode(err)
    if interrupted:
        result = parse_helper_output(stdout)
        if result is not None and result.get("ok") is True and proc.returncode == 0:
            return result  # it had finished anyway
        raise KeyboardInterrupt
    returncode = proc.returncode
    details = _details(cmd, returncode, stdout, stderr)
    if returncode == PKEXEC_DISMISSED:
        raise AuthorizationError(_("Authentication was cancelled."), details=details)
    if returncode == PKEXEC_NOT_AUTHORIZED:
        raise AuthorizationError(
            _("You are not allowed to do this, or the password was not accepted."),
            details=details,
        )

    result = parse_helper_output(stdout)
    if result is None:
        log.warning("helper op %s returned no usable JSON (exit %s)", op, returncode)
        raise HelperError(_("The administrator task failed unexpectedly."), details=details)
    if result.get("ok") is True:
        if returncode != 0:
            log.warning("helper op %s reported ok but exited with %s", op, returncode)
        return result

    error = result.get("error")
    # The helper runs without the user's locale; translate its English message here when possible.
    message = _(error) if isinstance(error, str) and error.strip() else \
        _("The administrator task failed unexpectedly.")
    helper_details = result.get("details")
    raise HelperError(
        message,
        details=helper_details if isinstance(helper_details, str) and helper_details else details,
    )
