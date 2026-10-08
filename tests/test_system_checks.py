from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from easy_installer.core import system_checks
from easy_installer.core.system_checks import (
    SystemStatus,
    detect_apparmor_enabled,
    detect_libfuse2,
    detect_system_status,
    detect_userns_restricted,
    get_system_status,
    libfuse2_package_for,
    parse_os_release,
    read_os_release,
)

ZORIN_18 = """\
PRETTY_NAME="Zorin OS 18.1"
NAME="Zorin OS"
VERSION_ID="18"
VERSION="18.1"
VERSION_CODENAME=noble
ID=zorin
ID_LIKE="ubuntu debian"
UBUNTU_CODENAME=noble
"""

ZORIN_18_MINIMAL = 'ID=zorin\nID_LIKE="ubuntu debian"\nVERSION_ID=18\n'
ZORIN_17 = 'ID=zorin\nID_LIKE="ubuntu debian"\nVERSION_ID=17\n'
UBUNTU_2204 = 'NAME="Ubuntu"\nVERSION_ID="22.04"\nID=ubuntu\nID_LIKE=debian\nUBUNTU_CODENAME=jammy\n'
UBUNTU_2404 = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\nUBUNTU_CODENAME=noble\n'
UBUNTU_2510 = 'VERSION_ID="25.10"\nID=ubuntu\nID_LIKE=debian\n'
MINT_22 = 'NAME="Linux Mint"\nVERSION_ID="22"\nID=linuxmint\nID_LIKE="ubuntu debian"\nUBUNTU_CODENAME=noble\n'
MINT_22_MINIMAL = 'ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\n'
MINT_21 = 'ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="21.3"\nUBUNTU_CODENAME=jammy\n'
POP_2404 = 'ID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="24.04"\n'
POP_2204 = 'ID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="22.04"\n'
DEBIAN_12 = 'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
DEBIAN_13 = 'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n'
DEBIAN_SID = 'ID=debian\nVERSION_CODENAME=sid\n'
FEDORA = 'ID=fedora\nVERSION_ID=42\n'


@pytest.mark.parametrize("text, expected", [
    (ZORIN_18, "libfuse2t64"),
    (ZORIN_18_MINIMAL, "libfuse2t64"),
    (ZORIN_17, "libfuse2"),
    (UBUNTU_2204, "libfuse2"),
    (UBUNTU_2404, "libfuse2t64"),
    (UBUNTU_2510, "libfuse2t64"),
    (MINT_22, "libfuse2t64"),
    (MINT_22_MINIMAL, "libfuse2t64"),
    (MINT_21, "libfuse2"),
    (POP_2404, "libfuse2t64"),
    (POP_2204, "libfuse2"),
    (DEBIAN_12, "libfuse2"),
    (DEBIAN_13, "libfuse2t64"),
    (DEBIAN_SID, "libfuse2t64"),
    (FEDORA, "libfuse2"),
    ("", "libfuse2"),
])
def test_libfuse2_package(text, expected):
    assert libfuse2_package_for(parse_os_release(text)) == expected


def test_parse_os_release_quoting():
    data = parse_os_release(ZORIN_18 + "# comment\nBROKEN LINE\nX='single quoted'\n")
    assert data["ID"] == "zorin"
    assert data["ID_LIKE"] == "ubuntu debian"
    assert data["PRETTY_NAME"] == "Zorin OS 18.1"
    assert data["VERSION_ID"] == "18"
    assert data["X"] == "single quoted"
    assert "BROKEN LINE" not in data


def test_read_os_release_injectable(tmp_path):
    missing = tmp_path / "missing"
    second = tmp_path / "os-release"
    second.write_text(MINT_22)
    assert read_os_release([missing, second])["ID"] == "linuxmint"
    assert read_os_release([missing]) == {}


def _fake_ldconfig(tmp_path: Path, output: str, rc: int = 0) -> str:
    script = tmp_path / "ldconfig"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{output}\nEOF\nexit {rc}\n")
    script.chmod(0o755)
    return str(script)


def _which_only(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


LDCONFIG_WITH_FUSE2 = """\
1234 libs found in cache `/etc/ld.so.cache'
\tlibfuse3.so.3 (libc6,x86-64) => /lib/x86_64-linux-gnu/libfuse3.so.3
\tlibfuse.so.2 (libc6,x86-64) => /lib/x86_64-linux-gnu/libfuse.so.2"""
LDCONFIG_WITHOUT_FUSE2 = """\
1234 libs found in cache `/etc/ld.so.cache'
\tlibfuse3.so.3 (libc6,x86-64) => /lib/x86_64-linux-gnu/libfuse3.so.3"""


def test_libfuse2_found_by_ldconfig(tmp_path):
    ldconfig = _fake_ldconfig(tmp_path, LDCONFIG_WITH_FUSE2)
    empty_root = tmp_path / "root"
    empty_root.mkdir()
    assert detect_libfuse2(which=_which_only({"ldconfig": ldconfig}), root=empty_root,
                           ldconfig_candidates=()) is True


def test_libfuse2_ldconfig_from_sbin_candidates(tmp_path):
    ldconfig = _fake_ldconfig(tmp_path, LDCONFIG_WITH_FUSE2)
    empty_root = tmp_path / "root"
    empty_root.mkdir()
    # not on PATH, but found at one of the fixed locations
    assert detect_libfuse2(which=_which_only({}), root=empty_root,
                           ldconfig_candidates=("/nonexistent/ldconfig", ldconfig)) is True


def test_libfuse2_missing(tmp_path):
    ldconfig = _fake_ldconfig(tmp_path, LDCONFIG_WITHOUT_FUSE2)
    empty_root = tmp_path / "root"
    empty_root.mkdir()
    assert detect_libfuse2(which=_which_only({"ldconfig": ldconfig}), root=empty_root,
                           ldconfig_candidates=()) is False


def test_libfuse2_glob_fallback(tmp_path):
    root = tmp_path / "root"
    libdir = root / "usr/lib/x86_64-linux-gnu"
    libdir.mkdir(parents=True)
    (libdir / "libfuse.so.2.9.9").write_bytes(b"")
    failing = _fake_ldconfig(tmp_path, "", rc=1)
    assert detect_libfuse2(which=_which_only({"ldconfig": failing}), root=root,
                           ldconfig_candidates=()) is True
    assert detect_libfuse2(which=_which_only({}), root=root, ldconfig_candidates=()) is True
    lib64 = tmp_path / "root2" / "lib64"
    lib64.mkdir(parents=True)
    (lib64 / "libfuse.so.2").write_bytes(b"")
    assert detect_libfuse2(which=_which_only({}), root=tmp_path / "root2",
                           ldconfig_candidates=()) is True


def test_userns_and_apparmor_probes(tmp_path):
    sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
    sysctl.write_text("1\n")
    assert detect_userns_restricted(sysctl) is True
    sysctl.write_text("0\n")
    assert detect_userns_restricted(sysctl) is False
    assert detect_userns_restricted(tmp_path / "missing") is False

    securityfs = tmp_path / "securityfs"
    param = tmp_path / "enabled"
    assert detect_apparmor_enabled(securityfs, param) is False
    param.write_text("Y\n")
    assert detect_apparmor_enabled(securityfs, param) is True
    param.write_text("N\n")
    securityfs.mkdir()
    assert detect_apparmor_enabled(securityfs, param) is True


def _full_status(tmp_path, *, tools: dict[str, str], os_release: str, userns="1",
                 fuse_dev=True, libfuse=True) -> SystemStatus:
    osr = tmp_path / "os-release"
    osr.write_text(os_release)
    root = tmp_path / "fsroot"
    (root / "usr/lib").mkdir(parents=True, exist_ok=True)
    if libfuse:
        (root / "usr/lib/libfuse.so.2").write_bytes(b"")
    dev = tmp_path / "dev-fuse"
    if fuse_dev:
        dev.write_bytes(b"")
    sysctl = tmp_path / "userns"
    sysctl.write_text(userns)
    securityfs = tmp_path / "apparmor"
    securityfs.mkdir(exist_ok=True)
    return detect_system_status(
        which=_which_only(tools),
        ldconfig_candidates=(),
        os_release_paths=[osr],
        fs_root=root,
        dev_fuse=dev,
        userns_sysctl=sysctl,
        apparmor_securityfs=securityfs,
        apparmor_module_param=tmp_path / "no-param",
    )


def test_detect_system_status_injected(tmp_path):
    tools = {
        "unsquashfs": "/usr/bin/unsquashfs",
        "pkexec": "/usr/bin/pkexec",
        "apparmor_parser": "/usr/sbin/apparmor_parser",
        "update-desktop-database": "/usr/bin/update-desktop-database",
        "gtk4-update-icon-cache": "/usr/bin/gtk4-update-icon-cache",
        "desktop-file-validate": "/usr/bin/desktop-file-validate",
        "fusermount": "/usr/bin/fusermount",
        "apt-get": "/usr/bin/apt-get",
    }
    status = _full_status(tmp_path, tools=tools, os_release=ZORIN_18_MINIMAL)
    assert status.unsquashfs == "/usr/bin/unsquashfs"
    assert status.icon_cache_tool == "/usr/bin/gtk4-update-icon-cache"
    assert status.fusermount == "/usr/bin/fusermount"
    assert status.libfuse2 is True
    assert status.dev_fuse is True
    assert status.userns_restricted is True
    assert status.apparmor_enabled is True
    assert status.distro_id == "zorin"
    assert status.distro_like == ("ubuntu", "debian")
    assert status.distro_version == "18"
    assert status.has_apt is True
    assert status.libfuse2_package == "libfuse2t64"


def test_detect_system_status_bare(tmp_path):
    status = _full_status(tmp_path, tools={}, os_release=FEDORA, userns="0",
                          fuse_dev=False, libfuse=False)
    assert status.unsquashfs is None
    assert status.pkexec is None
    assert status.icon_cache_tool is None
    assert status.fusermount is None
    assert status.libfuse2 is False
    assert status.dev_fuse is False
    assert status.userns_restricted is False
    assert status.has_apt is False
    assert status.distro_id == "fedora"


def _status(**overrides) -> SystemStatus:
    base = dict(
        unsquashfs="/usr/bin/unsquashfs", pkexec="/usr/bin/pkexec",
        apparmor_parser="/usr/sbin/apparmor_parser",
        update_desktop_database="/usr/bin/update-desktop-database",
        icon_cache_tool="/usr/bin/gtk-update-icon-cache",
        desktop_file_validate="/usr/bin/desktop-file-validate",
        libfuse2=True, fusermount="/usr/bin/fusermount3", dev_fuse=True,
        userns_restricted=True, apparmor_enabled=True, distro_id="zorin",
        distro_like=("ubuntu", "debian"), distro_version="18", has_apt=True,
        libfuse2_package="libfuse2t64",
    )
    base.update(overrides)
    return SystemStatus(**base)


def test_can_run_appimage_dynamic_runtime():
    elf = SimpleNamespace(has_interp=True)
    assert _status().can_run_appimage(elf) is True
    assert _status(libfuse2=False).can_run_appimage(elf) is False
    assert _status(dev_fuse=False).can_run_appimage(elf) is False
    assert _status(fusermount=None).can_run_appimage(elf) is False


def test_can_run_appimage_static_runtime():
    elf = SimpleNamespace(has_interp=False)
    assert _status(libfuse2=False).can_run_appimage(elf) is True
    assert _status(libfuse2=False, dev_fuse=False).can_run_appimage(elf) is False
    assert _status(libfuse2=False, fusermount=None).can_run_appimage(elf) is False


def test_status_is_frozen():
    status = _status()
    with pytest.raises(dataclasses.FrozenInstanceError):
        status.libfuse2 = False  # type: ignore[misc]


def test_get_system_status_is_cached(monkeypatch):
    calls = []

    def fake_detect(**kwargs):
        calls.append(kwargs)
        return _status()

    monkeypatch.setattr(system_checks, "_cache", None)
    monkeypatch.setattr(system_checks, "detect_system_status", fake_detect)
    first = get_system_status()
    second = get_system_status()
    assert first is second
    assert len(calls) == 1
    get_system_status(refresh=True)
    assert len(calls) == 2


def test_get_system_status_real_machine_smoke(monkeypatch):
    monkeypatch.setattr(system_checks, "_cache", None)
    status = get_system_status(refresh=True)
    assert isinstance(status, SystemStatus)
    assert status.libfuse2_package in ("libfuse2", "libfuse2t64")
    assert status.dev_fuse == os.path.exists("/dev/fuse")
