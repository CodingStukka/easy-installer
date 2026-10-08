from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from easy_installer.core.sandbox import (
    SandboxFix,
    apparmor_quote_path,
    default_sandbox_fix,
    needs_sandbox_fix,
    render_apparmor_profile,
)


def status(**kw):
    base = dict(userns_restricted=True, apparmor_parser="/usr/sbin/apparmor_parser",
                pkexec="/usr/bin/pkexec", apparmor_enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


def info(**kw):
    base = dict(is_electron=True, exec_has_no_sandbox=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_enum_values():
    assert SandboxFix("none") is SandboxFix.NONE
    assert SandboxFix("apparmor") is SandboxFix.APPARMOR
    assert SandboxFix("no-sandbox") is SandboxFix.NO_SANDBOX
    assert SandboxFix.NO_SANDBOX == "no-sandbox"


def test_needs_sandbox_fix():
    assert needs_sandbox_fix(info(), status()) is True
    assert needs_sandbox_fix(info(is_electron=False), status()) is False
    assert needs_sandbox_fix(info(), status(userns_restricted=False)) is False
    assert needs_sandbox_fix(info(exec_has_no_sandbox=True), status()) is False


def test_default_sandbox_fix():
    assert default_sandbox_fix(status()) is SandboxFix.APPARMOR
    assert default_sandbox_fix(status(apparmor_parser=None)) is SandboxFix.NO_SANDBOX
    assert default_sandbox_fix(status(pkexec=None)) is SandboxFix.NO_SANDBOX
    assert default_sandbox_fix(status(apparmor_enabled=False)) is SandboxFix.NO_SANDBOX


@pytest.mark.parametrize("path, expected", [
    ("/home/u/Applications/T3-Code-Alpha.AppImage", '"/home/u/Applications/T3-Code-Alpha.AppImage"'),
    ("/home/my user/Apps/a b.AppImage", '"/home/my user/Apps/a b.AppImage"'),
    ("/x/*?.AppImage", r'"/x/\*\?.AppImage"'),
    ("/x/[a]{b}^c", r'"/x/\[a\]\{b\}\^c"'),
    ('/x/"quoted"', r'"/x/\"quoted\""'),
    ("/x/back\\slash", r'"/x/back\\slash"'),
    ("/x/@{HOME}/a", r'"/x/@\{HOME\}/a"'),
])
def test_apparmor_quote_path(path, expected):
    assert apparmor_quote_path(path) == expected


@pytest.mark.parametrize("bad", ["relative/path", "/x/new\nline", "/x/tab\there", "/x/\x7f"])
def test_apparmor_quote_path_rejects(bad):
    with pytest.raises(ValueError):
        apparmor_quote_path(bad)


def test_render_profile_exact():
    text = render_apparmor_profile("t3code", "/home/u/Applications/T3-Code-Alpha.AppImage")
    assert text == (
        "# Managed by Easy Installer — allows t3code to use unprivileged user namespaces\n"
        "# (required by the Chromium/Electron sandbox on Ubuntu 24.04 and newer).\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "\n"
        'profile easyinstaller-t3code "/home/u/Applications/T3-Code-Alpha.AppImage" flags=(unconfined) {\n'
        "  userns,\n"
        "\n"
        "  include if exists <local/easyinstaller-t3code>\n"
        "}\n"
    )


@pytest.mark.parametrize("bad_id", ["", "-leading", "has space", "a/b", "x\n", "a" * 200, "ü"])
def test_render_profile_rejects_bad_ids(bad_id):
    with pytest.raises(ValueError):
        render_apparmor_profile(bad_id, "/opt/appimages/X.AppImage")


def test_render_profile_escapes_path():
    text = render_apparmor_profile("org.example.App", "/home/u/Apps*/X.AppImage")
    assert r'"/home/u/Apps\*/X.AppImage"' in text


@pytest.mark.skipif(shutil.which("apparmor_parser") is None, reason="apparmor_parser not installed")
def test_profile_parses_with_apparmor_parser(tmp_path):
    """Syntax check only (-Q: skip kernel load, -K: no cache); never loads anything."""
    profile = tmp_path / "easyinstaller-t3code"
    profile.write_text(render_apparmor_profile(
        "t3code", "/home/my user/Applications/T3-Code (Alpha)*.AppImage"))
    proc = subprocess.run(
        ["apparmor_parser", "-Q", "-K", "-I", "/etc/apparmor.d", str(profile)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 and ("Permission denied" in proc.stderr or "abi" in proc.stderr.lower()):
        pytest.skip(f"apparmor_parser unusable here: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stderr
