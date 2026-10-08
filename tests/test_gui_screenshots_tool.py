"""tools/gui_screenshots.py must never drive windows on the developer's real screen by accident."""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "gui_screenshots.py"


@pytest.fixture
def tool(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("gui_screenshots_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runs: list[dict] = []

    def run_child(env, out):
        runs.append(env)
        return 1  # a scenario step failed

    monkeypatch.setattr(module, "run_child", run_child)
    monkeypatch.setattr(module, "stop_process", lambda proc: None)
    monkeypatch.setattr(module, "isolated_env", lambda root, language="en": {
        "HOME": str(root), "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
    monkeypatch.setenv("DISPLAY", ":0")
    module.runs = runs
    module.out = tmp_path / "shots"
    return module


def run(tool, backend: str) -> int:
    args = types.SimpleNamespace(out=str(tool.out), backend=backend, language="en", keep=False)
    return tool.parent_main(args)


def test_default_backend_is_broadway(tool):
    import argparse

    captured = {}
    real = tool.parent_main
    tool.parent_main = lambda args: captured.setdefault("args", args) and 0
    try:
        tool.main(["--out", str(tool.out)])
    finally:
        tool.parent_main = real
    assert isinstance(captured["args"], argparse.Namespace)
    assert captured["args"].backend == "broadway"


@pytest.mark.parametrize("backend", ["broadway", "auto"])
def test_failed_broadway_run_is_not_repeated_on_the_real_display(tool, monkeypatch, backend):
    monkeypatch.setattr(tool, "start_broadway", lambda env, log: (object(), 9))
    assert run(tool, backend) == 1
    assert len(tool.runs) == 1
    env = tool.runs[0]
    assert env["GDK_BACKEND"] == "broadway"
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env


def test_real_display_only_when_broadway_is_unavailable_or_asked_for(tool, monkeypatch):
    monkeypatch.setattr(tool, "start_broadway", lambda env, log: None)
    run(tool, "broadway")
    assert tool.runs == []
    run(tool, "auto")
    assert [env["GDK_BACKEND"] for env in tool.runs] == ["x11"]
    tool.runs.clear()
    run(tool, "x11")
    assert [env["GDK_BACKEND"] for env in tool.runs] == ["x11"]


def test_the_child_runs_in_a_temporary_home_without_pkexec(monkeypatch, tmp_path):
    """Every folder Easy Installer uses points into the tool's temporary root (the real
    ``isolated_env``, not the fixture's stand-in), and the password prompt is disabled."""
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location("gui_screenshots_env_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", "/home/someone/Applications")
    monkeypatch.setenv("EASY_INSTALLER_DISABLE_PKEXEC", "0")
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    env = module.isolated_env(root, "de")
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                "XDG_RUNTIME_DIR", "EASY_INSTALLER_APPS_DIR", "EASY_INSTALLER_SYSTEM_ROOT"):
        assert env[key].startswith(str(root)), (key, env[key])
    assert env["EASY_INSTALLER_DISABLE_PKEXEC"] == "1"
    assert env["LANGUAGE"] == "de" and env["GSETTINGS_BACKEND"] == "memory"
    assert not any(key.startswith("DBUS_") for key in env)
    # the dialog scenes of 0.2 are part of this one tool
    assert "35-update-confirm.png" in module.SHOTS and "30-install-keep-both.png" in module.SHOTS
    assert not (TOOL.parent / "gui_screenshots_dialogs.py").exists()
