"""Tests for the helper's entry point: JSON in/out, caller identification, entry.py bootstrap."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from easy_installer.core.paths import system_layout
from easy_installer.core.registry import Registry
from easy_installer.core.sandbox import render_apparmor_profile
from easy_installer.core.system_checks import SystemStatus
from easy_installer.helper import entry, ops
from easy_installer.helper import main as helper_main

from fakeappimage import T3_DESKTOP, build_runtime, make_png

ENTRY_SCRIPT = Path(entry.__file__).resolve()
FRIENDLY_INVALID = "Something went wrong, so nothing was changed. Please try again."


# ------------------------------------------------------------------------------------------------
# helpers & fixtures
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(helper_main, "target_layout", lambda: layout)
    monkeypatch.setattr(helper_main, "RUN_COMMANDS", False)
    monkeypatch.setenv("PKEXEC_UID", str(os.getuid()))
    return layout


def run_main(monkeypatch, capsys, argv, stdin: bytes | str | None = b"{}"):
    """Run main() in-process; returns (exit code, the single JSON object on stdout, stderr)."""
    if stdin is None:
        monkeypatch.setattr(sys, "stdin", None)
    else:
        data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
    code = helper_main.main(argv)
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got: {out!r}"
    assert "Traceback" not in out
    result = json.loads(lines[0])
    assert isinstance(result, dict)
    assert code == (0 if result.get("ok") is True else 1)
    return code, result, err


def manifest_for(work: Path) -> dict:
    src = work / "T3-Code-0.0.42-x86_64.AppImage"
    src.write_bytes(build_runtime() + b"payload" * 50)
    icon = work / "t3code.png"
    icon.write_bytes(make_png(512, 512))
    return {
        "app_id": "t3code", "name": "T3 Code (Alpha)", "version": "0.0.42",
        "comment": "T3 Code desktop build", "source_appimage": str(src),
        "sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "embedded_desktop": T3_DESKTOP,
        "embedded_desktop_filename": "t3code.desktop", "icon_source": str(icon),
        "extra_args": [], "extract_and_run": False, "apparmor": False, "uninstall_command": None,
        "arch": "x86_64", "size": src.stat().st_size, "update_info": None,
        "original_filename": src.name, "is_electron": True,
    }


@pytest.fixture
def work(tmp_path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def make_status(**overrides) -> SystemStatus:
    base = dict(
        unsquashfs=None, pkexec=None, apparmor_parser=None, update_desktop_database=None,
        icon_cache_tool=None, desktop_file_validate=None, libfuse2=False, fusermount=None,
        dev_fuse=True, userns_restricted=False, apparmor_enabled=False, distro_id="zorin",
        distro_like=("ubuntu",), distro_version="18", has_apt=True, libfuse2_package="libfuse2t64",
    )
    base.update(overrides)
    return SystemStatus(**base)


# ------------------------------------------------------------------------------------------------
# end to end (in-process)
# ------------------------------------------------------------------------------------------------


def test_install_and_uninstall_end_to_end(layout, work, monkeypatch, capsys):
    manifest = manifest_for(work)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(manifest))
    assert code == 0 and result["ok"] is True
    app = result["app"]
    assert app["scope"] == "system" and app["id"] == "t3code"
    assert Path(app["appimage_path"]).parent == layout.apps_dir
    assert Path(app["appimage_path"]).read_bytes() == Path(manifest["source_appimage"]).read_bytes()
    assert Registry(layout.registry_path).get("t3code") is not None

    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"],
                                  json.dumps({"app_id": "t3code"}))
    assert code == 0 and result["ok"] is True
    assert not Path(app["appimage_path"]).exists()
    assert Registry(layout.registry_path).load() == {}


def test_backup_rollback_and_drop_backup_end_to_end(layout, work, monkeypatch, capsys):
    """0.2: keep_backup / consume_backup in the install request and the new op drop-backup."""
    manifest = manifest_for(work)
    code, first, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(manifest))
    assert code == 0 and first["app"]["previous"] is None
    assert first["app"]["mtime_ns"] == os.stat(first["app"]["appimage_path"]).st_mtime_ns

    newer = work / "T3-Code-0.0.43-x86_64.AppImage"
    newer.write_bytes(build_runtime() + b"newer payload" * 50)
    update = {**manifest, "version": "0.0.43", "source_appimage": str(newer),
              "sha256": hashlib.sha256(newer.read_bytes()).hexdigest(),
              "size": newer.stat().st_size, "original_filename": newer.name,
              "keep_backup": True, "backup_max_age_days": 14,
              "update_source": {"kind": "github-assets", "owner": "t3", "repo": "code"},
              "origin_url": "https://example.org/T3.AppImage", "data_hints": ["t3code"]}
    code, second, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(update))
    assert code == 0 and second["action"] == "update" and second["warnings"] == []
    app = second["app"]
    kept = layout.apps_dir / ".easyinstaller-backups" / "t3code" / "T3-Code-Alpha-0.0.42.AppImage"
    assert app["previous"]["path"] == str(kept) and app["previous"]["version"] == "0.0.42"
    assert kept.read_bytes() == Path(manifest["source_appimage"]).read_bytes()
    assert app["update_source"] == update["update_source"] and app["data_hints"] == ["t3code"]

    # going back: the source is the recorded backup, which the helper uses up itself
    back = {**manifest, "source_appimage": str(kept), "consume_backup": True, "keep_backup": True}
    code, third, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(back))
    assert code == 0 and third["app"]["version"] == "0.0.42" and not kept.exists()
    assert third["app"]["previous"]["version"] == "0.0.43"
    now_kept = Path(third["app"]["previous"]["path"])
    assert now_kept.read_bytes() == newer.read_bytes()

    # a second "consume" of a file that is not the recorded backup is an invalid request
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"],
                                  json.dumps({**update, "consume_backup": True}))
    assert code == 1 and result["error"] == FRIENDLY_INVALID
    assert "consume_backup" in result["details"] and now_kept.is_file()

    code, result, _err = run_main(monkeypatch, capsys, ["helper", "drop-backup"],
                                  json.dumps({"app_id": "t3code"}))
    assert (code, result) == (0, {"ok": True, "removed": [str(now_kept)], "skipped": []})
    assert Registry(layout.registry_path).get("t3code").previous is None
    assert [p.name for p in layout.apps_dir.iterdir()] == ["T3-Code-Alpha.AppImage"]

    code, result, _err = run_main(monkeypatch, capsys, ["helper", "drop-backup"],
                                  '{"app_id": "nope"}')
    assert code == 1 and result == {
        "ok": False, "error": "This app is not installed for everyone on this computer.",
        "details": "nope (system)"}
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "drop-backup"],
                                  '{"app_id": "../etc"}')
    assert code == 1 and result["error"] == FRIENDLY_INVALID


def test_the_client_and_the_helper_know_the_same_operations():
    from easy_installer.core import installer, privileged

    assert installer.DROP_BACKUP_OP in helper_main.OPS
    assert set(helper_main.OPS) == set(privileged.HELPER_OPS)


def test_dev_mode_without_pkexec_uid(layout, work, monkeypatch, capsys):
    monkeypatch.delenv("PKEXEC_UID")
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"],
                                  json.dumps(manifest_for(work)))
    assert code == 0 and result["ok"] is True


def test_failed_operation_reports_friendly_error_and_details(layout, work, monkeypatch, capsys):
    manifest = manifest_for(work)
    manifest["sha256"] = "0" * 64
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(manifest))
    assert code == 1
    assert result["ok"] is False
    assert "changed while it was being installed" in result["error"]
    assert "sha256" in result["details"]
    assert list(layout.apps_dir.iterdir()) == []


def test_uninstall_unknown_app(layout, monkeypatch, capsys):
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "nope"}')
    assert code == 1
    assert result == {"ok": False,
                      "error": "This app is not installed for everyone on this computer.",
                      "details": "nope (system)"}


def test_a_request_of_another_version_of_the_program_is_refused(layout, monkeypatch, capsys):
    """PKG-1: the client says its version; another one's request may mean something else."""
    from easy_installer import __version__

    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"],
                                  '{"app_id": "nope", "client_version": "9.9.9"}')
    assert code == 1 and "belongs to another version" in result["error"]
    assert result["details"] == f"helper {__version__}, program 9.9.9"
    # the same version (and old clients that do not say it) are served as always
    for request in (f'{{"app_id": "nope", "client_version": "{__version__}"}}',
                    '{"app_id": "nope"}'):
        code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], request)
        assert result["error"] == "This app is not installed for everyone on this computer."


def test_apparmor_ops_use_the_caller_from_the_password_database(layout, tmp_path, monkeypatch,
                                                                capsys):
    home = tmp_path / "alice"
    app = home / "Applications" / "Anytype.AppImage"
    app.parent.mkdir(parents=True)
    app.write_bytes(build_runtime())
    real_getpwuid = pwd.getpwuid

    def fake_getpwuid(uid):
        if uid == os.getuid():
            return SimpleNamespace(pw_name="alice", pw_gid=os.getgid(), pw_dir=str(home))
        return real_getpwuid(uid)

    monkeypatch.setattr(helper_main.pwd, "getpwuid", fake_getpwuid)
    request = json.dumps({"app_id": "anytype", "appimage_path": str(app)})
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "apparmor-install"], request)
    assert code == 0, result
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    assert result["profile_path"] == str(profile)
    assert profile.read_text() == render_apparmor_profile("anytype", str(app))

    # A file outside the caller's home is refused.
    outside = tmp_path / "Outside.AppImage"
    outside.write_bytes(build_runtime())
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "apparmor-install"],
                                  json.dumps({"app_id": "other", "appimage_path": str(outside)}))
    assert code == 1 and "home folder" in result["error"]

    # Someone else's profile is kept on apparmor-remove; our own is removed.
    others = layout.apparmor_dir / "easyinstaller-bobs"
    others.write_text(render_apparmor_profile("bobs", "/home/bob/Applications/B.AppImage"))
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "apparmor-remove"],
                                  '{"app_id": "bobs"}')
    assert code == 0 and result["removed"] is False and others.is_file()
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "apparmor-remove"],
                                  '{"app_id": "anytype"}')
    assert code == 0 and result["removed"] is True and not profile.exists()


def test_install_fuse_uses_system_status(layout, monkeypatch, capsys):
    monkeypatch.setattr(helper_main, "get_system_status",
                        lambda refresh=False: make_status(has_apt=False))
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install-fuse"], "{}")
    assert code == 1 and "cannot be installed automatically" in result["error"]
    monkeypatch.setattr(helper_main, "get_system_status", lambda refresh=False: make_status())
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install-fuse"],
                                  '{"package": "evil"}')
    assert code == 0 and result == {"ok": True, "package": "libfuse2t64"}


# ------------------------------------------------------------------------------------------------
# command line and input validation
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["helper"],
    ["helper", "rm-rf"],
    ["helper", "INSTALL"],
    ["helper", "install", "--evil"],
    ["helper", "uninstall", "t3code"],
    ["helper", ""],
    ["helper", "drop_backup"],
    ["helper", "drop-backup", "t3code"],
    ["helper", "prune-backups"],
])
def test_bad_command_lines(layout, monkeypatch, capsys, argv):
    code, result, _err = run_main(monkeypatch, capsys, argv, '{"app_id": "t3code"}')
    assert code == 1
    assert result["ok"] is False and result["error"] == FRIENDLY_INVALID
    assert result["details"]


@pytest.mark.parametrize("stdin, fragment", [
    (b"", "not valid JSON"),
    (b"   ", "not valid JSON"),
    (b"{not json", "not valid JSON"),
    (b"\xff\xfe{}", "not valid JSON"),
    (b'{"app_id": NaN}', "not valid JSON"),
    (b'{"app_id": Infinity}', "not valid JSON"),
    (b"[" * 600_000 + b"]" * 600_000, "larger than"),
    (b"[" * 100_000, "not valid JSON"),
    (b"[]", "JSON object"),
    (b'"install"', "JSON object"),
    (b"null", "JSON object"),
    (b"1" * 5000, "not valid JSON"),
    (b" " * (helper_main.MAX_INPUT_BYTES + 1), "larger than"),
], ids=["empty", "blank", "broken", "not-utf8", "nan", "infinity", "too-large-nesting",
        "deep-nesting", "array", "string", "null", "huge-int", "too-large"])
def test_bad_input(layout, monkeypatch, capsys, stdin, fragment):
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], stdin)
    assert code == 1
    assert result["error"] == FRIENDLY_INVALID
    assert fragment in result["details"]


def test_input_of_exactly_the_limit_is_read(layout, monkeypatch, capsys):
    body = b'{"app_id": "nope"}'
    data = body + b" " * (helper_main.MAX_INPUT_BYTES - len(body))
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], data)
    assert code == 1 and result["details"] == "nope (system)"


def test_missing_stdin(layout, monkeypatch, capsys):
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], None)
    assert code == 1 and "no standard input" in result["details"]


@pytest.mark.parametrize("value", ["abc", "-1", "1e3", " 1000", "1000 ", "0x3e8", "\u0661",
                                   "99999999999", "", "4294967295"])
def test_invalid_pkexec_uid(layout, monkeypatch, capsys, value):
    monkeypatch.setenv("PKEXEC_UID", value)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1
    assert result["error"] == "Easy Installer's administrator helper was started in an unexpected way."
    assert "PKEXEC_UID" in result["details"]


def test_pkexec_uid_must_match_in_dev_mode(layout, monkeypatch, capsys):
    monkeypatch.setenv("PKEXEC_UID", str(os.getuid() + 1))
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and "PKEXEC_UID" in result["details"]


# ------------------------------------------------------------------------------------------------
# simulated root: caller identification
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_root(layout, monkeypatch):
    """euid 0 without doing anything as root: dispatch is replaced by a recorder."""
    seen: list = []

    def fake_dispatch(op, request, caller, *, layout, run_commands):
        seen.append((op, request, caller, layout, run_commands))
        return {"ok": True}

    chdirs: list[str] = []
    monkeypatch.setattr(helper_main, "dispatch", fake_dispatch)
    monkeypatch.setattr(helper_main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(helper_main.os, "chdir", chdirs.append)
    return SimpleNamespace(seen=seen, chdirs=chdirs)


def test_root_requires_pkexec_uid(fake_root, monkeypatch, capsys):
    monkeypatch.delenv("PKEXEC_UID")
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and "pkexec" in result["details"]
    assert fake_root.seen == []


def test_root_resolves_caller_from_pkexec_uid(fake_root, layout, monkeypatch, capsys):
    me = pwd.getpwuid(os.getuid())
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 0 and result == {"ok": True}
    [(op, request, caller, used_layout, run_commands)] = fake_root.seen
    assert op == "uninstall" and request == {"app_id": "x"}
    assert caller == helper_main.Caller(uid=me.pw_uid, gid=me.pw_gid, home=Path(me.pw_dir),
                                        name=me.pw_name)
    assert used_layout == layout and run_commands is False
    assert fake_root.chdirs == ["/"]


def test_root_rejects_unknown_user(fake_root, monkeypatch, capsys):
    monkeypatch.setenv("PKEXEC_UID", "4294967000")
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and "no user" in result["details"]
    assert fake_root.seen == []


def test_default_layout_is_the_real_system():
    assert helper_main.target_layout() == system_layout(Path("/"))
    assert helper_main.RUN_COMMANDS is True


# ------------------------------------------------------------------------------------------------
# robustness: never a traceback or noise on stdout
# ------------------------------------------------------------------------------------------------


def test_unexpected_exception_becomes_json(layout, monkeypatch, capsys):
    def boom(m, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ops, "op_uninstall", boom)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1
    assert result == {"ok": False, "error": "The administrator task failed unexpectedly.",
                      "details": "RuntimeError: kaboom"}


def test_keyboard_interrupt_becomes_json(layout, monkeypatch, capsys):
    def interrupted(m, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ops, "op_uninstall", interrupted)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and result["ok"] is False


def _ctrl_c() -> None:
    """What pressing Ctrl+C in the terminal does to the root helper (same process group)."""
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.2)  # the signal arrives (and would raise KeyboardInterrupt) here


def test_ctrl_c_after_the_change_is_saved_keeps_the_answer_truthful(layout, work, monkeypatch,
                                                                    capsys):
    """Once the registry is written the app is installed (or gone): an interrupt during the
    cache refresh must not make the answer "failed" (the CLI would print "Cancelled.")."""
    before = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(ops, "refresh_caches", lambda *args, **kwargs: _ctrl_c())
    manifest = manifest_for(work)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"], json.dumps(manifest))
    assert code == 0 and result["ok"] is True, result
    assert Registry(layout.registry_path).get("t3code") is not None
    assert signal.getsignal(signal.SIGINT) is before  # restored after answering

    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"],
                                  json.dumps({"app_id": "t3code"}))
    assert code == 0 and result["ok"] is True, result
    assert Registry(layout.registry_path).load() == {}
    assert signal.getsignal(signal.SIGINT) is before


def test_ctrl_c_before_the_change_is_saved_still_cancels(layout, work, monkeypatch, capsys):
    real_copy = ops._copy_appimage

    def interrupted_copy(*args, **kwargs):
        _ctrl_c()
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(ops, "_copy_appimage", interrupted_copy)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "install"],
                                  json.dumps(manifest_for(work)))
    assert code == 1 and result["ok"] is False and "KeyboardInterrupt" in result["details"]
    assert Registry(layout.registry_path).load() == {}
    assert not layout.apps_dir.exists() or os.listdir(layout.apps_dir) == []


def test_prints_during_an_operation_go_to_stderr(layout, monkeypatch, capsys):
    def noisy(m, **kwargs):
        print("some library chatter")
        return {"ok": True, "removed": []}

    monkeypatch.setattr(ops, "op_uninstall", noisy)
    code, result, err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 0 and result == {"ok": True, "removed": []}
    assert "some library chatter" in err


@pytest.mark.parametrize("returned", [None, {"ok": False}, {"removed": []}, {"ok": "yes"}])
def test_operation_must_report_ok(layout, monkeypatch, capsys, returned):
    monkeypatch.setattr(ops, "op_uninstall", lambda m, **kwargs: returned)
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and result["ok"] is False


def test_unserialisable_result_becomes_error(layout, monkeypatch, capsys):
    monkeypatch.setattr(ops, "op_uninstall", lambda m, **kwargs: {"ok": True, "x": object()})
    code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert code == 1 and result["ok"] is False


def test_umask_is_set_during_the_operation_and_restored(layout, monkeypatch, capsys):
    seen = []

    def record_umask(m, **kwargs):
        current = os.umask(0)
        os.umask(current)
        seen.append(current)
        return {"ok": True}

    monkeypatch.setattr(ops, "op_uninstall", record_umask)
    before = os.umask(0o077)
    try:
        run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
        after = os.umask(0o077)
    finally:
        os.umask(before)
    assert seen == [0o022]
    assert after == 0o077


def test_details_are_truncated(layout, monkeypatch, capsys):
    def verbose(m, **kwargs):
        raise ops.RequestError("x" * 100_000)

    monkeypatch.setattr(ops, "op_uninstall", verbose)
    _code, result, _err = run_main(monkeypatch, capsys, ["helper", "uninstall"], '{"app_id": "x"}')
    assert len(result["details"]) == helper_main.MAX_DETAILS


# ------------------------------------------------------------------------------------------------
# entry.py (pkexec dev mode: wiped environment, cwd / or /root)
# ------------------------------------------------------------------------------------------------


def run_entry(args, stdin: bytes, cwd: str = "/") -> subprocess.CompletedProcess:
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return subprocess.run([python, str(ENTRY_SCRIPT), *args], input=stdin, capture_output=True,
                          cwd=cwd, env={}, timeout=60, check=False)


def test_entry_script_works_with_a_wiped_environment():
    proc = run_entry(["no-such-op"], b"{}")
    assert proc.returncode == 1, proc.stderr
    lines = proc.stdout.decode().splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["ok"] is False and "unknown operation" in result["details"]
    assert b"Traceback" not in proc.stdout and b"Traceback" not in proc.stderr


def test_entry_script_extra_arguments_are_refused():
    proc = run_entry(["uninstall", "--layout=/tmp"], b'{"app_id": "x"}', cwd=str(Path.home()))
    result = json.loads(proc.stdout)
    assert proc.returncode == 1 and "exactly one operation" in result["details"]


def test_entry_prepares_sys_path(monkeypatch):
    helper_dir = str(ENTRY_SCRIPT.parent)
    monkeypatch.setattr(sys, "path", [helper_dir, "", "/usr/lib/python3/dist-packages"])
    root = entry.prepare_sys_path()
    assert root == str(ENTRY_SCRIPT.parents[2])
    assert (Path(root) / "easy_installer" / "__init__.py").is_file()
    assert sys.path == [root, "/usr/lib/python3/dist-packages"]


def test_importing_entry_has_no_side_effects():
    import importlib

    before = list(sys.path)
    importlib.reload(entry)
    assert sys.path == before
