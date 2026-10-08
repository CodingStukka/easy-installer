"""`make install` / `make uninstall` round trip into a DESTDIR (on a copy of the source tree)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_make_uninstall_removes_everything_make_install_installed(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    for name in ("Makefile", "src", "data", "po", "tools", "packaging"):
        source = REPO / name
        if source.is_dir():
            shutil.copytree(source, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, tree / name)
    dest = tmp_path / "dest"

    def make(*args: str) -> None:
        proc = subprocess.run(["make", "-s", "-C", str(tree), *args, f"DESTDIR={dest}"],
                              capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    make("install")
    installed = sorted(p for p in dest.rglob("*") if not p.is_dir())
    assert any(p.name == "com.roothirsch.EasyInstaller.policy" for p in installed)
    assert any(p.name == "easy-installer.mo" for p in installed)
    make("uninstall")
    assert [p for p in dest.rglob("*") if not p.is_dir()] == []

    # A policy that belongs to another installation (e.g. the .deb) is left alone.
    make("install")
    policy = dest / "usr/share/polkit-1/actions/com.roothirsch.EasyInstaller.policy"
    policy.write_text(policy.read_text().replace("/usr/local/libexec", "/usr/libexec"))
    make("uninstall")
    assert [p for p in dest.rglob("*") if not p.is_dir()] == [policy]
