from __future__ import annotations

import json
import subprocess

import pytest

from easy_installer.core import privileged
from easy_installer.core.privileged import (
    HELPER_INSTALLED_PATH,
    helper_command,
    parse_helper_output,
    run_helper,
)
from easy_installer.errors import AuthorizationError, HelperError


class FakeRun:
    """Stands in for subprocess.Popen: records the call and answers with a canned result.

    ``communicate`` raises the exceptions in ``raises`` first (one per call), e.g. a
    TimeoutExpired or a KeyboardInterrupt; ``kill_error`` is what ``kill()`` raises (a
    PermissionError when the helper already runs as root).
    """

    def __init__(self, returncode=0, stdout=b"", stderr=b"", exc=None, raises=(), kill_error=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc
        self.raises = list(raises)
        self.kill_error = kill_error
        self.calls: list[dict] = []
        self.killed = False

    def __call__(self, cmd, **kwargs):
        stdin = kwargs["stdin"]
        self.calls.append({"cmd": cmd, "input": stdin.read(), **kwargs})
        if self.exc is not None:
            raise self.exc
        return _FakeProc(self)


class _FakeProc:
    def __init__(self, fake: FakeRun):
        self.fake = fake
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        self.fake.calls[-1].setdefault("timeouts", []).append(timeout)
        if self.fake.raises:
            raise self.fake.raises.pop(0)
        self.returncode = -9 if self.fake.killed else self.fake.returncode
        return self.fake.stdout, self.fake.stderr

    def kill(self):
        if self.fake.kill_error is not None:
            raise self.fake.kill_error
        self.fake.killed = True


@pytest.fixture
def enabled(monkeypatch):
    """Allow run_helper to get past the kill switch; subprocess.run is always faked."""
    monkeypatch.delenv("EASY_INSTALLER_DISABLE_PKEXEC", raising=False)
    monkeypatch.setattr(privileged, "find_pkexec", lambda: "/usr/bin/pkexec")
    monkeypatch.setattr(privileged, "installed_helper_usable", lambda path=HELPER_INSTALLED_PATH: False)


def install_fake(monkeypatch, **kw) -> FakeRun:
    fake = FakeRun(**kw)
    monkeypatch.setattr(privileged.subprocess, "Popen", fake)
    return fake


def test_disabled_by_env_runs_nothing(monkeypatch):
    fake = install_fake(monkeypatch, stdout=b'{"ok": true}')
    with pytest.raises(HelperError) as excinfo:
        run_helper("install", {"app_id": "x"})
    assert fake.calls == []
    assert excinfo.value.details and "EASY_INSTALLER_DISABLE_PKEXEC" in excinfo.value.details


def test_unknown_op_rejected(enabled):
    with pytest.raises(ValueError):
        run_helper("rm-rf", {})


def test_helper_command_dev_mode(enabled):
    cmd = helper_command()
    assert cmd[0] == "/usr/bin/pkexec"
    assert cmd[1] == "/usr/bin/python3"
    assert cmd[2].endswith("easy_installer/helper/entry.py")
    assert cmd[2].startswith("/")


def test_helper_command_installed(enabled, monkeypatch):
    monkeypatch.setattr(privileged, "installed_helper_usable", lambda path=HELPER_INSTALLED_PATH: True)
    assert helper_command() == ["/usr/bin/pkexec", HELPER_INSTALLED_PATH]


def test_installed_helper_must_be_root_owned(tmp_path):
    fake_helper = tmp_path / "easy-installer-helper"
    fake_helper.write_text("#!/bin/sh\n")
    fake_helper.chmod(0o755)
    # owned by the test user, not root -> not trusted
    assert privileged.installed_helper_usable(str(fake_helper)) is False
    assert privileged.installed_helper_usable(str(tmp_path / "missing")) is False


def test_missing_pkexec(monkeypatch):
    monkeypatch.delenv("EASY_INSTALLER_DISABLE_PKEXEC", raising=False)
    monkeypatch.setattr(privileged, "find_pkexec", lambda: None)
    fake = install_fake(monkeypatch)
    with pytest.raises(HelperError):
        run_helper("install", {})
    assert fake.calls == []


def test_success_passes_json_on_stdin(enabled, monkeypatch):
    fake = install_fake(monkeypatch, stdout=b'{"ok": true, "app": {"id": "x"}}\n')
    result = run_helper("install", {"app_id": "x", "name": "Ünïcode"})
    assert result == {"ok": True, "app": {"id": "x"}}
    call = fake.calls[0]
    assert call["cmd"][-1] == "install"
    assert json.loads(call["input"].decode("utf-8")) == {
        "app_id": "x", "name": "Ünïcode", "client_version": privileged.__version__}
    assert call["timeouts"] == [600]


def _fake_installed_helper(tmp_path, version: str):
    """A helper wrapper like the packaged one, whose _LIBDIR holds a package of ``version``."""
    import sys

    package = tmp_path / f"lib-{version}" / "easy_installer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n')
    helper = tmp_path / f"helper-{version}"
    helper.write_text(f"#!{sys.executable} -I\nimport sys\n\n"
                      f'_LIBDIR = "{package.parent}"\n_LOCALEDIR = ""\n')
    return str(helper)


def test_the_version_of_the_installed_helper_is_known_without_the_password(tmp_path):
    assert privileged.installed_helper_version(_fake_installed_helper(tmp_path, "0.1.0")) == "0.1.0"
    assert privileged.installed_helper_version(str(tmp_path / "missing")) is None
    (tmp_path / "plain").write_text("not a script\n")
    assert privileged.installed_helper_version(str(tmp_path / "plain")) is None


def test_an_installed_helper_of_another_version_is_never_started(enabled, monkeypatch, tmp_path):
    """PKG-1: e.g. the 0.1 helper of the .deb next to a newer program run from its source:
    it would drop every 0.2 field of the system registry."""
    old = _fake_installed_helper(tmp_path, "0.1.0")
    same = _fake_installed_helper(tmp_path, privileged.__version__)
    versions = {old: privileged.installed_helper_version(old),
                same: privileged.installed_helper_version(same)}
    monkeypatch.setattr(privileged, "installed_helper_version", versions.get)
    monkeypatch.setattr(privileged, "HELPER_INSTALLED_PATHS", (old, same))
    monkeypatch.setattr(privileged, "installed_helper_usable", lambda path: path == old)
    fake = install_fake(monkeypatch, stdout=b'{"ok": true}')
    with pytest.raises(HelperError, match="belongs to another version") as excinfo:
        run_helper("uninstall", {"app_id": "x"})
    assert fake.calls == [] and "helper 0.1.0" in excinfo.value.details
    monkeypatch.setattr(privileged, "installed_helper_usable", lambda path: path == same)
    assert run_helper("uninstall", {"app_id": "x"}) == {"ok": True}
    assert fake.calls[0]["cmd"][1] == same


@pytest.mark.parametrize("rc", [126, 127])
def test_pkexec_auth_failures(enabled, monkeypatch, rc):
    install_fake(monkeypatch, returncode=rc, stderr=b"Error executing command as another user")
    with pytest.raises(AuthorizationError) as excinfo:
        run_helper("uninstall", {"app_id": "x"})
    assert str(excinfo.value)
    assert "exit code" in excinfo.value.details


def test_helper_reports_error(enabled, monkeypatch):
    install_fake(monkeypatch, returncode=1,
                 stdout=b'{"ok": false, "error": "The file changed while it was being copied."}')
    with pytest.raises(HelperError) as excinfo:
        run_helper("install", {})
    assert str(excinfo.value) == "The file changed while it was being copied."


def test_helper_error_details_forwarded(enabled, monkeypatch):
    install_fake(monkeypatch, returncode=1,
                 stdout=b'{"ok": false, "error": "Nope", "details": "Traceback..."}')
    with pytest.raises(HelperError) as excinfo:
        run_helper("install", {})
    assert excinfo.value.details == "Traceback..."


@pytest.mark.parametrize("stdout", [
    b"",
    b"this is not json at all",
    b"[1, 2, 3]",
    b'{"ok": tru',
    b"\xff\xfe garbage bytes",
])
def test_garbage_output(enabled, monkeypatch, stdout):
    install_fake(monkeypatch, returncode=1, stdout=stdout, stderr=b"Traceback: boom")
    with pytest.raises(HelperError) as excinfo:
        run_helper("install", {})
    assert "boom" in excinfo.value.details
    assert "exit code: 1" in excinfo.value.details


def test_ok_false_without_message(enabled, monkeypatch):
    install_fake(monkeypatch, returncode=1, stdout=b'{"ok": false}')
    with pytest.raises(HelperError) as excinfo:
        run_helper("install", {})
    assert str(excinfo.value)


def test_json_with_noise_lines(enabled, monkeypatch):
    install_fake(monkeypatch, stdout=b'some warning from a tool\n{"ok": true, "removed": 3}\n')
    assert run_helper("uninstall", {"app_id": "x"}) == {"ok": True, "removed": 3}


def test_timeout_while_waiting_for_the_password(enabled, monkeypatch):
    fake = install_fake(monkeypatch, raises=[subprocess.TimeoutExpired(["pkexec"], 5)])
    with pytest.raises(HelperError, match="took too long"):
        run_helper("install", {}, timeout=5)
    assert fake.killed


def test_timeout_of_a_root_helper_waits_for_its_answer(enabled, monkeypatch):
    """After authentication the helper runs as root: kill() fails with EPERM (it used to be
    reported as "could not be started", although the app had been installed)."""
    fake = install_fake(monkeypatch, stdout=b'{"ok": true, "app": {"id": "x"}}',
                        raises=[subprocess.TimeoutExpired(["pkexec"], 5)],
                        kill_error=PermissionError(1, "Operation not permitted"))
    assert run_helper("install", {}, timeout=5) == {"ok": True, "app": {"id": "x"}}
    assert fake.calls[0]["timeouts"] == [5, None]


@pytest.mark.parametrize("answer, outcome", [
    (b'{"ok": false, "error": "Cancelled."}', KeyboardInterrupt),
    (b'{"ok": true, "removed": []}', {"ok": True, "removed": []}),
])
def test_ctrl_c_waits_for_the_root_helper(enabled, monkeypatch, answer, outcome):
    """Ctrl+C: the helper got SIGINT too and answers; its result decides what happened."""
    install_fake(monkeypatch, stdout=answer, returncode=0 if outcome is not KeyboardInterrupt else 1,
                 raises=[KeyboardInterrupt()], kill_error=PermissionError(1, "not permitted"))
    if outcome is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            run_helper("uninstall", {"app_id": "x"})
    else:
        assert run_helper("uninstall", {"app_id": "x"}) == outcome


def test_real_process_answer_and_timeout(enabled, monkeypatch, tmp_path):
    """The request arrives on stdin (a private temporary file) of a real child process."""
    import sys

    script = tmp_path / "helper.py"
    script.write_text(
        "import json, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "time.sleep(request.get('sleep', 0))\n"
        "print(json.dumps({'ok': True, 'echo': request}))\n")
    monkeypatch.setattr(privileged, "helper_command", lambda: [sys.executable, str(script)])
    big = "x" * 300_000  # larger than a pipe buffer
    assert run_helper("install", {"big": big})["echo"] == {
        "big": big, "client_version": privileged.__version__}
    # A root helper cannot be killed: wait for its answer instead of failing.
    monkeypatch.setattr(subprocess.Popen, "kill", lambda self: (_ for _ in ()).throw(
        PermissionError(1, "Operation not permitted")))
    assert run_helper("install", {"sleep": 0.5}, timeout=0.1)["echo"]["sleep"] == 0.5


def test_oserror(enabled, monkeypatch):
    install_fake(monkeypatch, exc=FileNotFoundError("pkexec"))
    with pytest.raises(HelperError):
        run_helper("install", {})


def test_unserialisable_payload(enabled, monkeypatch):
    fake = install_fake(monkeypatch)
    with pytest.raises(HelperError):
        run_helper("install", {"x": object()})
    assert fake.calls == []


def test_parse_helper_output():
    assert parse_helper_output('{"ok": true}') == {"ok": True}
    assert parse_helper_output('noise\n{"ok": false, "error": "x"}\n') == {"ok": False, "error": "x"}
    assert parse_helper_output("") is None
    assert parse_helper_output('"just a string"') is None



def test_real_pkexec_is_refused_during_a_test_run_even_without_isolation(monkeypatch):
    """Safety interlock: undoing the environment isolation must never reach the password prompt."""
    monkeypatch.delenv("EASY_INSTALLER_DISABLE_PKEXEC", raising=False)
    monkeypatch.setattr(privileged, "installed_helper_usable", lambda path=HELPER_INSTALLED_PATH: True)
    started = []

    class NeverStart:                  # stands in for the genuine Popen, so nothing can start
        def __init__(self, *args, **kwargs):
            started.append(args)
            raise AssertionError("pkexec must not be started")

    monkeypatch.setattr(privileged, "_REAL_POPEN", NeverStart)
    monkeypatch.setattr(privileged.subprocess, "Popen", NeverStart)
    with pytest.raises(HelperError) as exc:
        run_helper("drop-backup", {"app_id": "x"})
    assert "real pkexec" in (exc.value.details or "")
    assert started == []
