"""What does this computer offer? (FUSE, AppArmor user-namespace restriction, helper tools, distro)."""

from __future__ import annotations

import glob
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .elf import ElfInfo

log = logging.getLogger(__name__)

OS_RELEASE_PATHS = (Path("/etc/os-release"), Path("/usr/lib/os-release"))
LDCONFIG_CANDIDATES = ("/sbin/ldconfig", "/usr/sbin/ldconfig")
LIBFUSE2_GLOBS = (
    "usr/lib*/libfuse.so.2*",
    "usr/lib*/*/libfuse.so.2*",
    "lib*/libfuse.so.2*",
    "lib*/*/libfuse.so.2*",
)
USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
APPARMOR_SECURITYFS = Path("/sys/kernel/security/apparmor")
APPARMOR_MODULE_PARAM = Path("/sys/module/apparmor/parameters/enabled")
DEV_FUSE = Path("/dev/fuse")

LIBFUSE2 = "libfuse2"
LIBFUSE2_T64 = "libfuse2t64"

# Ubuntu releases before the 64-bit time_t transition (package still called "libfuse2").
_UBUNTU_OLD_CODENAMES = frozenset({
    "trusty", "xenial", "yakkety", "zesty", "artful", "bionic", "cosmic", "disco", "eoan",
    "focal", "groovy", "hirsute", "impish", "jammy", "kinetic", "lunar", "mantic",
})
_DEBIAN_OLD_CODENAMES = frozenset({"jessie", "stretch", "buster", "bullseye", "bookworm"})
# Derivatives whose own VERSION_ID maps to an Ubuntu base: first major version based on 24.04.
_UBUNTU_DERIVATIVE_T64_FROM = {
    "zorin": 18,
    "linuxmint": 22,
    "elementary": 8,
    "pop": 24,
    "neon": 24,
    "tuxedo": 24,
    "kubuntu": 24,
    "xubuntu": 24,
    "lubuntu": 24,
    "ubuntu-budgie": 24,
    "ubuntustudio": 24,
}
# Debian derivatives: first major version based on Debian 13 (trixie).
_DEBIAN_DERIVATIVE_T64_FROM = {
    "lmde": 7,
    "raspbian": 13,
    "kali": 2025,
}


@dataclass(frozen=True)
class SystemStatus:
    unsquashfs: str | None
    pkexec: str | None
    apparmor_parser: str | None
    update_desktop_database: str | None
    icon_cache_tool: str | None
    desktop_file_validate: str | None
    libfuse2: bool
    fusermount: str | None
    dev_fuse: bool
    userns_restricted: bool
    apparmor_enabled: bool
    distro_id: str | None
    distro_like: tuple[str, ...]
    distro_version: str | None
    has_apt: bool
    libfuse2_package: str

    def can_run_appimage(self, elf: ElfInfo) -> bool:
        """Can the AppImage runtime mount its payload with FUSE (no extract-and-run needed)?"""
        fuse_usable = self.dev_fuse and self.fusermount is not None
        if getattr(elf, "has_interp", True):
            return fuse_usable and self.libfuse2
        return fuse_usable


# -- os-release / distro --------------------------------------------------------------------------


def parse_os_release(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            parts = shlex.split(value, posix=True)
            value = " ".join(parts) if parts else ""
        except ValueError:
            value = value.strip().strip("\"'")
        result[key] = value
    return result


def read_os_release(paths: Iterable[Path] = OS_RELEASE_PATHS) -> dict[str, str]:
    for path in paths:
        try:
            return parse_os_release(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return {}


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _at_least(version: str | None, minimum: tuple[int, ...]) -> bool | None:
    parsed = _version_tuple(version)
    if parsed is None:
        return None
    return parsed >= minimum


def libfuse2_package_for(os_release: dict[str, str]) -> str:
    """Name of the Debian/Ubuntu package that provides libfuse.so.2 on this distro release.

    Ubuntu 24.04+ and Debian 13+ (and their derivatives) renamed it to "libfuse2t64".
    """
    distro_id = os_release.get("ID", "").lower()
    like = os_release.get("ID_LIKE", "").lower().split()
    version = os_release.get("VERSION_ID")

    if distro_id == "ubuntu":
        newer = _at_least(version, (24, 4))
        if newer is not None:
            return LIBFUSE2_T64 if newer else LIBFUSE2
    if distro_id == "debian":
        newer = _at_least(version, (13,))
        if newer is not None:
            return LIBFUSE2_T64 if newer else LIBFUSE2
        codename = os_release.get("VERSION_CODENAME", "").lower()
        # testing/unstable have no VERSION_ID and are past the transition.
        return LIBFUSE2 if codename in _DEBIAN_OLD_CODENAMES else LIBFUSE2_T64

    ubuntu_codename = os_release.get("UBUNTU_CODENAME", "").lower()
    if ubuntu_codename:
        return LIBFUSE2 if ubuntu_codename in _UBUNTU_OLD_CODENAMES else LIBFUSE2_T64
    debian_codename = os_release.get("DEBIAN_CODENAME", "").lower()
    if debian_codename:
        return LIBFUSE2 if debian_codename in _DEBIAN_OLD_CODENAMES else LIBFUSE2_T64

    if distro_id in _UBUNTU_DERIVATIVE_T64_FROM:
        newer = _at_least(version, (_UBUNTU_DERIVATIVE_T64_FROM[distro_id],))
        if newer is not None:
            return LIBFUSE2_T64 if newer else LIBFUSE2
    if distro_id in _DEBIAN_DERIVATIVE_T64_FROM:
        newer = _at_least(version, (_DEBIAN_DERIVATIVE_T64_FROM[distro_id],))
        if newer is not None:
            return LIBFUSE2_T64 if newer else LIBFUSE2
    if "ubuntu" in like:
        # Unknown Ubuntu derivative: if its version looks like an Ubuntu version, use that.
        newer = _at_least(version, (24, 4))
        parsed = _version_tuple(version)
        if newer is not None and parsed and parsed[0] >= 14:
            return LIBFUSE2_T64 if newer else LIBFUSE2
    return LIBFUSE2


# -- libfuse2 ---------------------------------------------------------------------------------------


def _ldconfig_paths(which: Callable[[str], str | None], extra: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    found = which("ldconfig")
    if found:
        candidates.append(found)
    for path in extra:
        if path not in candidates and os.access(path, os.X_OK):
            candidates.append(path)
    return candidates


def _ldconfig_has_libfuse2(which: Callable[[str], str | None], extra: Iterable[str]) -> bool | None:
    """True/False from the linker cache, None if no ldconfig could be run."""
    for ldconfig in _ldconfig_paths(which, extra):
        try:
            proc = subprocess.run(
                [ldconfig, "-p"], capture_output=True, text=True, errors="replace",
                timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("ldconfig %s failed: %s", ldconfig, exc)
            continue
        if proc.returncode != 0:
            log.debug("ldconfig %s exited with %s", ldconfig, proc.returncode)
            continue
        return any(
            line.strip().startswith("libfuse.so.2") for line in proc.stdout.splitlines()
        )
    return None


def _glob_has_libfuse2(root: Path) -> bool:
    for pattern in LIBFUSE2_GLOBS:
        if glob.glob(os.path.join(os.fspath(root), pattern)):
            return True
    return False


def detect_libfuse2(*, which: Callable[[str], str | None] | None = None,
                    root: Path = Path("/"),
                    ldconfig_candidates: Iterable[str] = LDCONFIG_CANDIDATES) -> bool:
    if _ldconfig_has_libfuse2(which or default_which, ldconfig_candidates):
        return True
    # Stale or missing linker cache: look at the usual library directories directly.
    return _glob_has_libfuse2(root)


# -- misc probes -------------------------------------------------------------------------------------


def _read_flag(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def detect_userns_restricted(path: Path = USERNS_SYSCTL) -> bool:
    return _read_flag(path) == "1"


def detect_apparmor_enabled(securityfs: Path = APPARMOR_SECURITYFS,
                            module_param: Path = APPARMOR_MODULE_PARAM) -> bool:
    if securityfs.exists():
        return True
    return (_read_flag(module_param) or "").upper().startswith("Y")


def default_which(name: str) -> str | None:
    """shutil.which plus /usr/sbin and /sbin, which are not on PATH in every desktop session."""
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/sbin", "/sbin"):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _first_tool(which: Callable[[str], str | None], *names: str) -> str | None:
    for name in names:
        found = which(name)
        if found:
            return found
    return None


def detect_system_status(
    *,
    which: Callable[[str], str | None] | None = None,
    ldconfig_candidates: Iterable[str] = LDCONFIG_CANDIDATES,
    os_release_paths: Iterable[Path] = OS_RELEASE_PATHS,
    fs_root: Path = Path("/"),
    dev_fuse: Path = DEV_FUSE,
    userns_sysctl: Path = USERNS_SYSCTL,
    apparmor_securityfs: Path = APPARMOR_SECURITYFS,
    apparmor_module_param: Path = APPARMOR_MODULE_PARAM,
) -> SystemStatus:
    """Probe the system (uncached). Every source is injectable for tests."""
    which = which or default_which
    osr = read_os_release(os_release_paths)
    distro_id = osr.get("ID") or None
    return SystemStatus(
        unsquashfs=which("unsquashfs"),
        pkexec=which("pkexec"),
        apparmor_parser=which("apparmor_parser"),
        update_desktop_database=which("update-desktop-database"),
        icon_cache_tool=_first_tool(which, "gtk-update-icon-cache", "gtk4-update-icon-cache"),
        desktop_file_validate=which("desktop-file-validate"),
        libfuse2=detect_libfuse2(which=which, root=fs_root, ldconfig_candidates=ldconfig_candidates),
        fusermount=_first_tool(which, "fusermount3", "fusermount"),
        dev_fuse=dev_fuse.exists(),
        userns_restricted=detect_userns_restricted(userns_sysctl),
        apparmor_enabled=detect_apparmor_enabled(apparmor_securityfs, apparmor_module_param),
        distro_id=distro_id.lower() if distro_id else None,
        distro_like=tuple(osr.get("ID_LIKE", "").lower().split()),
        distro_version=osr.get("VERSION_ID") or None,
        has_apt=which("apt-get") is not None,
        libfuse2_package=libfuse2_package_for(osr),
    )


_cache: SystemStatus | None = None
_cache_lock = threading.Lock()


def get_system_status(refresh: bool = False) -> SystemStatus:
    global _cache
    with _cache_lock:
        if _cache is None or refresh:
            _cache = detect_system_status()
        return _cache
