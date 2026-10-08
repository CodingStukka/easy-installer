"""Shared fixtures. Every test runs with an isolated HOME/XDG environment and pkexec disabled."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# The tests check the English source texts. Once `make mo` has built catalogs into build/locale,
# a German or Dutch desktop locale would otherwise translate them. LANGUAGE has priority over
# LC_ALL/LC_MESSAGES/LANG for gettext (Python's and GLib's), and it is inherited by subprocesses.
os.environ["LANGUAGE"] = "en"
from easy_installer import i18n  # noqa: E402

i18n.setup()

# Real AppImages that may exist on the developer machine. They are only ever READ
# (inspection) or COPIED into tmp_path - never moved, modified or deleted.
REAL_APPIMAGE_CANDIDATES = [
    "~/Desktop/OpenSCAD-2026.03.28-x86_64.AppImage",
    "~/Downloads/T3-Code-0.0.42-x86_64.AppImage",
    "~/Documents/winboat-0.9.0-x86_64.AppImage",
    "~/Downloads/Anytype-0.55.4.AppImage",
    "~/Downloads/Pen-linux-x86_64.AppImage",
    "~/Downloads/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage",
]

# Resolve against the REAL home before the autouse fixture swaps HOME.
_REAL_HOME = Path(os.path.expanduser("~"))
REAL_APPIMAGES = [
    p for p in (_REAL_HOME / c[2:] for c in REAL_APPIMAGE_CANDIDATES) if p.is_file()
]

# Session-wide safety net, set with os.environ (not monkeypatch) so that a test calling
# monkeypatch.undo() falls back to a throwaway HOME with pkexec disabled - never to the real
# home or the real password prompt. The autouse fixture below narrows this per test.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="easy-installer-tests-"))
atexit.register(shutil.rmtree, _SESSION_HOME, True)
os.environ.update({
    "HOME": str(_SESSION_HOME),
    "XDG_DATA_HOME": str(_SESSION_HOME / ".local" / "share"),
    "XDG_CONFIG_HOME": str(_SESSION_HOME / ".config"),
    "XDG_CACHE_HOME": str(_SESSION_HOME / ".cache"),
    "XDG_STATE_HOME": str(_SESSION_HOME / ".local" / "state"),
    "EASY_INSTALLER_APPS_DIR": str(_SESSION_HOME / "Applications"),
    "EASY_INSTALLER_SYSTEM_ROOT": str(_SESSION_HOME / "sysroot"),
    "EASY_INSTALLER_DISABLE_PKEXEC": "1",
})


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point HOME and all XDG dirs into tmp_path; forbid pkexec. Returns the fake HOME."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "EASY_INSTALLER_APPS_DIR": str(home / "Applications"),
        "EASY_INSTALLER_SYSTEM_ROOT": str(tmp_path / "default-sysroot"),
        "EASY_INSTALLER_DISABLE_PKEXEC": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return home


@pytest.fixture
def system_root(tmp_path):
    """A fake filesystem root for system-scope tests (use with system_layout(root=...))."""
    root = tmp_path / "sysroot"
    root.mkdir()
    return root


@pytest.fixture
def real_appimages():
    if not REAL_APPIMAGES:
        pytest.skip("no real AppImages available on this machine")
    return list(REAL_APPIMAGES)


@pytest.fixture
def small_real_appimage():
    """Smallest real AppImage available (for tests that need to copy one)."""
    if not REAL_APPIMAGES:
        pytest.skip("no real AppImages available on this machine")
    return min(REAL_APPIMAGES, key=lambda p: p.stat().st_size)
