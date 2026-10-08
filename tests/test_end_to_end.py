"""End-to-end tests: inspect → install → launcher/registry checks → uninstall, across all layers.

* (a) read-only inspection of the real sample AppImages (``@pytest.mark.real``) with a table;
* (b) per-user install of COPIES of the real OpenSCAD and T3 Code AppImages through the real
  command line (``python -m easy_installer`` in a subprocess), then uninstall;
* (c) system-wide install through the real client code. ``pkexec`` is replaced by
  :class:`HelperPipe`, which feeds the JSON request to ``helper.main.main()`` in-process with
  ``target_layout()`` pointing at a temporary root, so privileged.py, main.py and ops.py all run;
* (d) update / reinstall / downgrade flows.

conftest.py isolates HOME/XDG_* and disables pkexec; nothing here touches the real home or /.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from easy_installer import cli
from easy_installer.core import installer, privileged
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.imageinfo import hicolor_subdir, probe_image
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import ACTION_DOWNGRADE, ACTION_REINSTALL, ACTION_UPDATE
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import Registry
from easy_installer.core.system_checks import get_system_status
from easy_installer.helper import main as helper_main

from conftest import REAL_APPIMAGES
from fakeappimage import (
    make_fake_appimage,
    make_png,
    make_sample_appimage,
    make_svg,
    requires_mksquashfs,
    requires_unsquashfs,
)

REPO = Path(__file__).resolve().parents[1]
DESKTOP_FILE_VALIDATE = shutil.which("desktop-file-validate")
requires_validate = pytest.mark.skipif(DESKTOP_FILE_VALIDATE is None,
                                       reason="desktop-file-validate is not installed")
needs_squashfs_tools = [requires_mksquashfs, requires_unsquashfs]

#: What the real samples must look like (file name -> expected values), see DESIGN.md intro.
REAL_EXPECTED = {
    "T3-Code-0.0.42-x86_64.AppImage": dict(app_id="t3code", version="0.0.42", icon="png",
                                           is_electron=True, no_sandbox=True),
    "OpenSCAD-2026.03.28-x86_64.AppImage": dict(app_id="openscad", version="2026.03.28", icon="png",
                                                is_electron=False, no_sandbox=False),
    "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage": dict(app_id="org.freecad.FreeCAD", version="1.1.3",
                                                      icon="svg", is_electron=False,
                                                      no_sandbox=False),
}
HASH_LIMIT = 300 * 1024 * 1024


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def real_sample(name: str) -> Path:
    for path in REAL_APPIMAGES:
        if path.name == name:
            return path
    pytest.skip(f"real sample {name} is not available on this machine")


def fingerprint(path: Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode), st.st_ino


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def mode_of(path: Path | str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def assert_valid_desktop_file(path: Path) -> None:
    """desktop-file-validate must not report errors (warnings/hints are tolerated)."""
    if DESKTOP_FILE_VALIDATE is None:
        return
    proc = subprocess.run([DESKTOP_FILE_VALIDATE, str(path)], capture_output=True, text=True,
                          check=False)
    output = proc.stdout + proc.stderr
    errors = [line for line in output.splitlines() if "error:" in line]
    assert proc.returncode == 0 and not errors, f"{path} is invalid:\n{output}"


def glib_argv(exec_value: str) -> list[str]:
    """How GLib (GNOME Shell, Gio.DesktopAppInfo) splits an Exec line started without files.

    Like g_desktop_app_info's expand_application_parameters(): field codes are expanded first
    ("%%" -> "%", file codes -> nothing), then the line is split with g_shell_parse_argv().
    """
    pytest.importorskip("gi")
    from gi.repository import GLib

    expanded, i = [], 0
    while i < len(exec_value):
        if exec_value[i] == "%" and i + 1 < len(exec_value):
            if exec_value[i + 1] == "%":
                expanded.append("%")
            i += 2
            continue
        expanded.append(exec_value[i])
        i += 1
    ok, argv = GLib.shell_parse_argv("".join(expanded))
    assert ok
    return argv


def run_cli_subprocess(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """``python -m easy_installer ARGS`` with the isolated environment of this test."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), LC_ALL="C.UTF-8", LANGUAGE="")
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run([sys.executable, "-m", "easy_installer", *args], env=env,
                          input=stdin, capture_output=True, text=True, timeout=600, check=False)


def files_below(root: Path) -> list[str]:
    """Every non-directory below ``root`` (relative paths)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if not p.is_dir())


def visible(directory: Path) -> list[str]:
    """What a file manager shows in ``directory`` (no hidden entries)."""
    return sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))


def check_launcher(desktop_path: Path, *, app_id: str, appimage: Path, scope: Scope,
                   embedded: DesktopEntry, icon_expected: bool = True) -> DesktopEntry:
    """Everything the launcher must say about an installed app."""
    assert desktop_path.name == f"easyinstaller-{app_id}.desktop"
    assert mode_of(desktop_path) == 0o644
    assert_valid_desktop_file(desktop_path)
    entry = DesktopEntry.parse(desktop_path.read_text(encoding="utf-8"))
    assert entry.get("Type") == "Application"
    assert entry.get("Name") == embedded.get("Name")

    exec_value = entry.get("Exec")
    argv = split_exec(exec_value)
    if "%" in str(appimage):   # (GLib finds argv[0] before it expands "%%": env starts it)
        assert argv[0] == "env" == glib_argv(exec_value)[0]
        argv, exec_value = argv[1:], exec_value[len("env "):]
    assert argv[0] == str(appimage)
    assert glib_argv(exec_value)[0] == str(appimage)          # quoting as GNOME reads it
    embedded_args = split_exec(embedded.get("Exec"))[1:]
    assert argv[1:] == embedded_args                            # arguments and field codes kept
    assert os.access(argv[0], os.X_OK) and mode_of(argv[0]) == 0o755

    assert entry.get("TryExec") == str(appimage)
    if icon_expected:
        assert entry.get("Icon") == f"easyinstaller-{app_id}"
    assert entry.get("StartupWMClass") == embedded.get("StartupWMClass")
    assert entry.get_list("MimeType") == embedded.get_list("MimeType")
    assert entry.get_list("Categories") == embedded.get_list("Categories")
    assert entry.get("X-EasyInstaller-Id") == app_id
    assert entry.get("X-EasyInstaller-Scope") == scope.value
    return entry


def check_icon(icon_path: Path, icons_dir: Path, app_id: str) -> None:
    assert icon_path.is_file()
    assert mode_of(icon_path) == 0o644
    info = probe_image(icon_path)
    assert icon_path.parent == icons_dir / hicolor_subdir(info)
    assert icon_path.name == f"easyinstaller-{app_id}.{info.format}"


# ------------------------------------------------------------------------------------------------
# pkexec stand-in: the real client and the real helper, joined in-process
# ------------------------------------------------------------------------------------------------


class HelperPipe:
    """Replaces ``pkexec <helper> <op>``: stdin JSON → ``helper.main.main()`` → stdout JSON.

    Only the process boundary is simulated: privileged.run_helper builds the request and parses
    the answer exactly as with pkexec, and main.py/ops.py do all their checks. Destinations come
    from ``helper.main.target_layout()``, which points at a temporary root here.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, layout, home: Path):
        self.layout = layout
        self.calls: list[tuple[str, dict]] = []
        monkeypatch.delenv("EASY_INSTALLER_DISABLE_PKEXEC", raising=False)
        monkeypatch.setenv("PKEXEC_UID", str(os.getuid()))
        monkeypatch.setattr(privileged, "helper_command",
                            lambda: ["/usr/bin/pkexec", privileged.HELPER_INSTALLED_PATH])
        # (the simulated helper is this very version: the real file there is never looked at)
        monkeypatch.setattr(privileged, "installed_helper_version",
                            lambda path: privileged.__version__)
        # privileged.py cannot start any real process any more.
        monkeypatch.setattr(privileged, "subprocess",
                            types.SimpleNamespace(Popen=self.popen, PIPE=subprocess.PIPE,
                                                  TimeoutExpired=subprocess.TimeoutExpired))
        monkeypatch.setattr(helper_main, "target_layout", lambda: layout)
        monkeypatch.setattr(helper_main, "RUN_COMMANDS", False)
        real_resolve = helper_main.resolve_caller
        # The caller's home is the fake HOME (the password database knows the real one).
        monkeypatch.setattr(helper_main, "resolve_caller", lambda environ, euid: dataclasses.replace(
            real_resolve(environ, euid), home=home))

    def popen(self, cmd, *, stdin, stdout, stderr):
        assert cmd[:2] == ["/usr/bin/pkexec", privileged.HELPER_INSTALLED_PATH]
        assert stdout == stderr == subprocess.PIPE
        op = cmd[-1]
        request = stdin.read()
        self.calls.append((op, json.loads(request)))
        out = io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(request))
        try:
            with contextlib.redirect_stdout(out):
                code = helper_main.main(["easy-installer-helper", op])
        finally:
            sys.stdin = saved_stdin
        answer = out.getvalue().encode("utf-8")
        return types.SimpleNamespace(returncode=code, kill=lambda: None,
                                     communicate=lambda input=None, timeout=None: (answer, b""))

    @property
    def ops(self) -> list[str]:
        return [op for op, _request in self.calls]


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper_pipe(sys_layout, isolated_env, monkeypatch) -> HelperPipe:
    return HelperPipe(monkeypatch, sys_layout, isolated_env)


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def status_with(**changes):
    return dataclasses.replace(get_system_status(), **changes)


@pytest.fixture(autouse=True)
def hermetic_launcher(isolated_env, monkeypatch):
    """The launcher for the "Uninstall…" action is only ever looked for in the fake HOME.

    A developer machine may have Easy Installer installed (/usr/bin/easy-installer from the
    .deb, or somewhere on PATH): what these tests install must not depend on that.
    """
    monkeypatch.setattr(installer, "_launcher_candidates",
                        lambda: [isolated_env / ".local" / "bin" / installer.LAUNCHER_NAME])


# ------------------------------------------------------------------------------------------------
# (a) inspect every real sample, read-only
# ------------------------------------------------------------------------------------------------


@pytest.mark.real
def test_inspect_all_real_samples_read_only(real_appimages, capsys):
    rows = []
    for path in real_appimages:
        before = fingerprint(path)
        size = path.stat().st_size
        with inspect_appimage(path, compute_hash=size <= HASH_LIMIT) as info:
            work_dir = info.work_dir
            rows.append((
                path.name, info.app_id, info.name, info.version or "-", info.arch,
                f"{info.icon_info.format} {info.icon_info.width}x{info.icon_info.height}"
                if info.icon_info else "-",
                "yes" if info.is_electron else "no",
                "yes" if info.exec_has_no_sandbox else "no",
                f"{size / 1e6:.0f} MB",
            ))
            assert info.desktop_entry is not None and info.icon_path is not None
            assert info.arch == "x86_64" and info.appimage_type == 2
            assert (info.sha256 is not None) == (size <= HASH_LIMIT)
            expected = REAL_EXPECTED.get(path.name)
            if expected:
                assert info.app_id == expected["app_id"]
                assert info.version == expected["version"]
                assert info.icon_info.format == expected["icon"]
                assert info.is_electron is expected["is_electron"]
                assert info.exec_has_no_sandbox is expected["no_sandbox"]
        assert not work_dir.exists(), "inspection left its temporary folder behind"
        assert fingerprint(path) == before, f"{path} was modified"

    headers = ("FILE", "ID", "NAME", "VERSION", "ARCH", "ICON", "ELECTRON", "NO-SANDBOX", "SIZE")
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip()
             for row in [headers, *rows]]
    with capsys.disabled():
        print("\n" + "\n".join(lines))


# ------------------------------------------------------------------------------------------------
# (b) per-user install of copies of real AppImages through the command line
# ------------------------------------------------------------------------------------------------


@pytest.mark.real
@pytest.mark.parametrize("sample, stem, wm_class", [
    ("OpenSCAD-2026.03.28-x86_64.AppImage", "OpenSCAD", "org.openscad.openscad"),
    ("T3-Code-0.0.42-x86_64.AppImage", "T3-Code-Alpha", "t3code"),
])
def test_real_user_install_and_uninstall_via_cli(sample, stem, wm_class, isolated_env, downloads):
    original = real_sample(sample)
    before = fingerprint(original)
    copy = downloads / sample
    shutil.copy2(original, copy)
    expected_sha = sha256_of(copy)
    with inspect_appimage(copy, compute_hash=False) as info:
        app_id, embedded = info.app_id, info.desktop_entry.copy()
    assert fingerprint(original) == before

    proc = run_cli_subprocess("install", "-y", str(copy))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is installed." in proc.stdout

    layout = user_layout()
    target = isolated_env / "Applications" / f"{stem}.AppImage"
    assert layout.apps_dir == isolated_env / "Applications"
    assert not copy.exists(), "the downloaded file should have been moved"
    assert target.is_file() and mode_of(target) == 0o755
    assert sha256_of(target) == expected_sha

    app = Registry(layout.registry_path).get(app_id)
    assert app is not None
    assert (app.scope, app.appimage_path, app.version) == (Scope.USER, str(target), info.version)
    assert app.sha256 == expected_sha and app.size == target.stat().st_size
    assert app.original_filename == sample and app.arch == "x86_64"
    assert app.sandbox_fix == "none" and app.apparmor_profile is None
    assert app.extract_and_run is False and app.status() == "ok"
    assert mode_of(layout.registry_path) == 0o644

    desktop = layout.desktop_dir / f"easyinstaller-{app_id}.desktop"
    assert app.desktop_path == str(desktop)
    entry = check_launcher(desktop, app_id=app_id, appimage=target, scope=Scope.USER,
                           embedded=embedded)
    assert entry.get("StartupWMClass") == wm_class
    assert len(app.icon_paths) == 1
    check_icon(Path(app.icon_paths[0]), layout.icons_dir, app_id)
    assert Path(app.icon_paths[0]).parent == layout.icons_dir / "512x512" / "apps"

    proc = run_cli_subprocess("list", "--json")
    assert proc.returncode == 0, proc.stderr
    assert [(a["id"], a["scope"], a["status"]) for a in json.loads(proc.stdout)] == \
        [(app_id, "user", "ok")]

    proc = run_cli_subprocess("uninstall", "-y", app_id)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not target.exists() and not desktop.exists()
    assert not Path(app.icon_paths[0]).exists()
    assert Registry(layout.registry_path).load() == {}
    # Only empty folders, the (now empty) registry and the desktop database's cache remain.
    leftovers = set(files_below(isolated_env))
    allowed = {
        ".local/share/easy-installer/registry.json",
        ".local/share/easy-installer/registry.json.lock",
        ".local/share/applications/mimeinfo.cache",   # written by update-desktop-database
    }
    assert leftovers <= allowed, f"unexpected leftovers: {sorted(leftovers - allowed)}"
    assert fingerprint(original) == before


# ------------------------------------------------------------------------------------------------
# (c) system-wide install through the real client, the real helper and a fake root
# ------------------------------------------------------------------------------------------------


def _system_install_checks(layout, app_id: str, stem: str, embedded: DesktopEntry,
                           expected_sha: str):
    target = layout.apps_dir / f"{stem}.AppImage"
    registry = Registry(layout.registry_path)
    app = registry.get(app_id)
    assert app is not None and app.scope is Scope.SYSTEM
    assert app.appimage_path == str(target)
    assert target.is_file() and mode_of(target) == 0o755 and sha256_of(target) == expected_sha
    assert app.sha256 == expected_sha
    assert mode_of(layout.registry_path) == 0o644
    for directory in (layout.apps_dir, layout.desktop_dir, layout.registry_path.parent):
        assert mode_of(directory) == 0o755
    desktop = layout.desktop_dir / f"easyinstaller-{app_id}.desktop"
    assert app.desktop_path == str(desktop)
    entry = check_launcher(desktop, app_id=app_id, appimage=target, scope=Scope.SYSTEM,
                           embedded=embedded)
    assert len(app.icon_paths) == 1
    check_icon(Path(app.icon_paths[0]), layout.icons_dir, app_id)
    return app, entry


def _system_uninstall_checks(layout, app) -> None:
    assert not Path(app.appimage_path).exists()
    assert not Path(app.desktop_path).exists()
    assert not any(Path(p).exists() for p in app.icon_paths)
    assert Registry(layout.registry_path).load() == {}
    leftovers = set(files_below(layout.apps_dir.parents[1]))
    assert leftovers <= {"var/lib/easy-installer/registry.json",
                         "var/lib/easy-installer/registry.json.lock"}, leftovers


@pytest.mark.real
def test_real_system_install_and_uninstall_via_cli(helper_pipe, sys_layout, downloads, capsys):
    sample = "OpenSCAD-2026.03.28-x86_64.AppImage"
    original = real_sample(sample)
    before = fingerprint(original)
    copy = downloads / sample
    shutil.copy2(original, copy)
    expected_sha = sha256_of(copy)
    with inspect_appimage(copy, compute_hash=False) as info:
        embedded = info.desktop_entry.copy()

    assert cli.main(["easy-installer", "install", "-y", "--system", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "is installed." in out and "--system" in out
    assert helper_pipe.ops == ["install"]
    request = helper_pipe.calls[0][1]
    assert request["source_appimage"] == str(copy) and request["sha256"] == expected_sha
    assert not copy.exists(), "the user's copy is deleted after a system install"

    app, entry = _system_install_checks(sys_layout, "openscad", "OpenSCAD", embedded, expected_sha)
    assert entry.get("StartupWMClass") == "org.openscad.openscad"
    assert Path(app.icon_paths[0]).parent == sys_layout.icons_dir / "512x512" / "apps"

    assert cli.main(["easy-installer", "list", "--json", "--system"]) == 0
    assert [(a["id"], a["status"]) for a in json.loads(capsys.readouterr().out)] == \
        [("openscad", "ok")]
    assert cli.main(["easy-installer", "uninstall", "-y", "--system", "openscad"]) == 0
    assert helper_pipe.ops == ["install", "uninstall"]
    _system_uninstall_checks(sys_layout, app)
    assert fingerprint(original) == before


@pytest.mark.parametrize("kind, filename, app_id, stem", [
    ("t3code", "T3-Code-0.0.42-x86_64.AppImage", "t3code", "T3-Code-Alpha"),
    ("openscad", "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad", "OpenSCAD"),
    ("freecad", "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "org.freecad.FreeCAD", "FreeCAD"),
])
@requires_mksquashfs
@requires_unsquashfs
def test_fake_system_install_and_uninstall_via_cli(kind, filename, app_id, stem, helper_pipe,
                                                   sys_layout, downloads, capsys):
    src = make_sample_appimage(downloads / filename, kind)
    expected_sha = sha256_of(src)
    with inspect_appimage(src, compute_hash=False) as info:
        embedded = info.desktop_entry.copy()

    assert cli.main(["easy-installer", "install", "-y", "--system", str(src)]) == 0
    app, _entry = _system_install_checks(sys_layout, app_id, stem, embedded, expected_sha)
    assert not src.exists()
    capsys.readouterr()

    assert cli.main(["easy-installer", "uninstall", "-y", "--system", app_id]) == 0
    _system_uninstall_checks(sys_layout, app)
    assert helper_pipe.ops == ["install", "uninstall"]


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("staging", [True, False])
def test_system_install_from_network_share(staging, helper_pipe, sys_layout, downloads, capsys,
                                           monkeypatch):
    """gvfs (SMB/SFTP), the document portal, sshfs, ...: only the mount owner may enter them.

    The helper keeps uid 0 as real/saved uid, so the kernel refuses it even with the caller's
    euid. The simulation makes open() fail like fuse_allow_current_process() does.
    """
    from easy_installer.helper import ops

    share = downloads / "gvfs"
    share.mkdir()
    src = make_sample_appimage(share / "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad")
    expected_sha = sha256_of(src)
    real_open = ops._open_nofollow

    def fuse_open(path: str) -> int:
        if Path(path).parent == share:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path)

    monkeypatch.setattr(ops, "_open_nofollow", fuse_open)
    monkeypatch.setattr(installer, "on_private_fuse_mount",
                        lambda p: staging and Path(p).parent == share)
    code = cli.main(["easy-installer", "install", "-y", "--system", str(src)])
    captured = capsys.readouterr()
    if not staging:  # what happened before: a confusing permission error
        assert code == 1 and "You are not allowed to read the file" in captured.err
        return
    assert code == 0, captured.err
    app = Registry(sys_layout.registry_path).get("openscad")
    assert sha256_of(Path(app.appimage_path)) == expected_sha
    assert app.original_filename == src.name
    assert not src.exists()
    assert not any(p.name.startswith("staging-")
                   for p in (downloads.parent / ".cache" / "easy-installer").iterdir())


def _electron_without_no_sandbox(downloads: Path, version: str = "0.55.4") -> Path:
    desktop = (
        "[Desktop Entry]\nName=Anytype\nExec=AppRun --ozone-platform-hint=auto %U\n"
        "Terminal=false\nType=Application\nIcon=anytype\nStartupWMClass=anytype\n"
        f"X-AppImage-Version={version}\nCategories=Utility;\nMimeType=x-scheme-handler/anytype;\n"
    )
    icon = "usr/share/icons/hicolor/256x256/apps/anytype.png"
    return make_fake_appimage(downloads / f"Anytype-{version}.AppImage", {
        "anytype.desktop": desktop, "AppRun": "#!/bin/sh\n", "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": b"asar", icon: make_png(256, 256),
    }, {".DirIcon": icon})


@requires_mksquashfs
@requires_unsquashfs
def test_system_install_with_apparmor_profile(helper_pipe, sys_layout, downloads, capsys,
                                              monkeypatch):
    status = status_with(userns_restricted=True, apparmor_parser="/usr/sbin/apparmor_parser",
                         pkexec="/usr/bin/pkexec", apparmor_enabled=True)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: status)
    src = _electron_without_no_sandbox(downloads)

    assert cli.main(["easy-installer", "install", "-y", "--system", str(src)]) == 0
    assert helper_pipe.calls[0][1]["apparmor"] is True
    app = Registry(sys_layout.registry_path).get("anytype")
    assert app.sandbox_fix == "apparmor"
    profile = sys_layout.apparmor_dir / "easyinstaller-anytype"
    assert app.apparmor_profile == str(profile)
    text = profile.read_text()
    assert f'profile easyinstaller-anytype "{app.appimage_path}" flags=(unconfined)' in text
    assert mode_of(profile) == 0o644
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text())
    assert "--no-sandbox" not in split_exec(entry.get("Exec"))
    assert_valid_desktop_file(Path(app.desktop_path))
    capsys.readouterr()

    assert cli.main(["easy-installer", "uninstall", "-y", "--system", "anytype"]) == 0
    assert not profile.exists()
    _system_uninstall_checks(sys_layout, app)


@requires_mksquashfs
@requires_unsquashfs
def test_system_apparmor_conflict_falls_back_and_warns(helper_pipe, sys_layout, downloads, capsys,
                                                       monkeypatch):
    """The helper's English warnings reach the user (installer appends them to plan.warnings)."""
    status = status_with(userns_restricted=True, apparmor_parser="/usr/sbin/apparmor_parser",
                         pkexec="/usr/bin/pkexec", apparmor_enabled=True)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: status)
    sys_layout.apparmor_dir.mkdir(parents=True)
    foreign = sys_layout.apparmor_dir / "easyinstaller-anytype"
    foreign.write_text("# somebody else's profile\n")
    src = _electron_without_no_sandbox(downloads)

    assert cli.main(["easy-installer", "install", "-y", "--system", str(src)]) == 0
    out = " ".join(capsys.readouterr().out.split())   # the CLI wraps long notes
    after = out.split("is installed.")[1]
    assert "could not get the special permission it needs, so it will be started without its " \
           "built-in security sandbox." in after
    app = Registry(sys_layout.registry_path).get("anytype")
    assert app.sandbox_fix == "no-sandbox" and app.apparmor_profile is None
    assert foreign.read_text() == "# somebody else's profile\n"
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text())
    assert split_exec(entry.get("Exec"))[:2] == [app.appimage_path, "--no-sandbox"]


# ------------------------------------------------------------------------------------------------
# launcher details: quoting, uninstall action
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_user_install_quotes_paths_and_adds_uninstall_action(isolated_env, downloads, monkeypatch,
                                                             capsys):
    apps_dir = isolated_env / "My Apps (100% mine)"
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(apps_dir))
    launcher = isolated_env / ".local" / "bin" / "easy-installer"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(installer, "_transient_roots", lambda: [])   # the fake HOME is in /tmp
    src = make_sample_appimage(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")
    with inspect_appimage(src, compute_hash=False) as info:
        embedded = info.desktop_entry.copy()

    assert cli.main(["easy-installer", "install", "-y", str(src)]) == 0
    capsys.readouterr()
    app = Registry(user_layout().registry_path).get("org.freecad.FreeCAD")
    target = apps_dir / "FreeCAD.AppImage"
    assert app.appimage_path == str(target)
    entry = check_launcher(Path(app.desktop_path), app_id=app.id, appimage=target,
                           scope=Scope.USER, embedded=embedded)
    raw_exec = entry.get_raw("Exec")
    assert raw_exec.startswith('env "') and "%%" in raw_exec   # quoted, literal % escaped
    pytest.importorskip("gi")
    from gi.repository import Gio
    assert Gio.DesktopAppInfo.new_from_filename(app.desktop_path) is not None   # GLib loads it
    assert entry.get("Comment[de]") == embedded.get("Comment[de]")
    assert entry.get_list("Actions") == ["easyinstaller-uninstall"]
    group = "Desktop Action easyinstaller-uninstall"
    assert split_exec(entry.get("Exec", group)) == [str(launcher), "--uninstall", app.id]
    assert entry.get("Name[de]", group) == "Deinstallieren…"
    assert Path(app.icon_paths[0]).parent == user_layout().icons_dir / "scalable" / "apps"

    assert cli.main(["easy-installer", "uninstall", "-y", app.id]) == 0
    assert not target.exists() and not Path(app.desktop_path).exists()


# ------------------------------------------------------------------------------------------------
# (d) update, reinstall and downgrade
# ------------------------------------------------------------------------------------------------


def _demo(downloads: Path, version: str, *, name: str = "Demo", svg_icon: bool = False) -> Path:
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\nExec=demo %F\nIcon=demo\n"
               f"Categories=Utility;\nX-AppImage-Version={version}\n")
    files: dict[str, bytes | str] = {"demo.desktop": desktop, "usr/bin/demo": b"\x7fELF demo"}
    if svg_icon:
        files["usr/share/icons/hicolor/scalable/apps/demo.svg"] = make_svg(48, 48)
    else:
        files["usr/share/icons/hicolor/128x128/apps/demo.png"] = make_png(128, 128)
    return make_fake_appimage(downloads / f"Demo-{version}-x86_64.AppImage", files,
                              {"AppRun": "usr/bin/demo"})


def _plan(path: Path, scope: Scope = Scope.USER):
    info = inspect_appimage(path)
    return info, installer.plan_install(info, installer.InstallOptions(scope=scope))


@requires_mksquashfs
@requires_unsquashfs
def test_user_update_replaces_old_files_and_detects_downgrade(isolated_env, downloads, capsys):
    layout = user_layout()
    assert cli.main(["easy-installer", "install", "-y", str(_demo(downloads, "1.0"))]) == 0
    v1 = Registry(layout.registry_path).get("demo")
    assert v1.version == "1.0" and Path(v1.icon_paths[0]).suffix == ".png"

    # 1.1 changes the name (-> new file name) and the icon format (-> new icon path).
    newer = _demo(downloads, "1.1", name="Demo Studio", svg_icon=True)
    info, plan = _plan(newer)
    with info:
        assert plan.action == ACTION_UPDATE and plan.existing.version == "1.0"
        app = installer.execute_install(plan)
    assert app.version == "1.1" and app.installed_at == v1.installed_at
    apps = Registry(layout.registry_path).load()
    assert list(apps) == ["demo"]
    assert app.appimage_path == str(layout.apps_dir / "Demo-Studio.AppImage")
    assert not Path(v1.appimage_path).exists(), "the old AppImage must be removed"
    assert not Path(v1.icon_paths[0]).exists(), "the old icon must be removed"
    assert Path(app.icon_paths[0]).suffix == ".svg" and Path(app.icon_paths[0]).is_file()
    assert visible(layout.apps_dir) == ["Demo-Studio.AppImage"]
    assert files_below(layout.apps_dir) == [".easyinstaller-backups/demo/Demo-1.0.AppImage",
                                            "Demo-Studio.AppImage"]   # 1.0 is kept for a while
    assert [p.name for p in layout.desktop_dir.glob("easyinstaller-*.desktop")] == \
        ["easyinstaller-demo.desktop"]
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text())
    assert entry.get("X-AppImage-Version") == "1.1" and entry.get("Name") == "Demo Studio"
    assert_valid_desktop_file(Path(app.desktop_path))

    # The same version again is a reinstall; an older one is a downgrade (with a warning).
    same = _demo(downloads, "1.1", name="Demo Studio", svg_icon=True)
    info, plan = _plan(same)
    with info:
        assert plan.action == ACTION_REINSTALL
    older = _demo(downloads, "1.0")
    info, plan = _plan(older)
    with info:
        assert plan.action == ACTION_DOWNGRADE
        assert any("newer version (1.1)" in w for w in plan.warnings)
    capsys.readouterr()
    assert cli.main(["easy-installer", "install", "-y", str(older)]) == 0
    out = capsys.readouterr().out
    assert "Ready to install an older version of Demo" in out
    assert "Version 1.1 is installed and will be replaced by the older version 1.0." in out
    app = Registry(layout.registry_path).get("demo")
    assert app.version == "1.0" and len(Registry(layout.registry_path).load()) == 1
    assert visible(layout.apps_dir) == ["Demo.AppImage"]
    # one backup per app: now the replaced 1.1
    assert files_below(layout.apps_dir) == [".easyinstaller-backups/demo/Demo-Studio-1.1.AppImage",
                                            "Demo.AppImage"]


@requires_mksquashfs
@requires_unsquashfs
def test_system_update_replaces_old_files(helper_pipe, sys_layout, downloads, capsys):
    assert cli.main(["easy-installer", "install", "-y", "--system",
                     str(_demo(downloads, "1.0"))]) == 0
    v1 = Registry(sys_layout.registry_path).get("demo")
    newer = _demo(downloads, "1.1", name="Demo Studio", svg_icon=True)
    capsys.readouterr()
    assert cli.main(["easy-installer", "install", "-y", "--system", str(newer)]) == 0
    assert "Ready to update Demo Studio" in capsys.readouterr().out
    apps = Registry(sys_layout.registry_path).load()
    assert list(apps) == ["demo"] and apps["demo"].version == "1.1"
    assert apps["demo"].installed_at == v1.installed_at
    assert not Path(v1.appimage_path).exists() and not Path(v1.icon_paths[0]).exists()
    assert visible(sys_layout.apps_dir) == ["Demo-Studio.AppImage"]
    assert mode_of(apps["demo"].appimage_path) == 0o755
    # the helper keeps the replaced version (as the settings say) in its own backup folder
    backup = sys_layout.apps_dir / ".easyinstaller-backups" / "demo" / "Demo-1.0.AppImage"
    assert apps["demo"].previous["path"] == str(backup) and apps["demo"].previous["version"] == "1.0"
    assert sha256_of(backup) == v1.sha256 and mode_of(backup.parent) == 0o755
    assert helper_pipe.calls[1][1]["keep_backup"] is True

    older = _demo(downloads, "1.0")
    info, plan = _plan(older, Scope.SYSTEM)
    with info:
        assert plan.action == ACTION_DOWNGRADE and plan.requires_root
    assert helper_pipe.ops == ["install", "install"]


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.skipif(shutil.which("update-mime-database") is None,
                    reason="update-mime-database (shared-mime-info) is not installed")
def test_app_file_types_are_recognised_after_install(isolated_env, downloads):
    """FreeCAD's *.FCStd is only defined by its own MIME package: after installing, GIO (the
    file manager) knows the type, so FreeCAD is offered for such files; after uninstalling not."""
    script = ("import gi; gi.require_version('Gio', '2.0'); from gi.repository import Gio; "
              "print(Gio.content_type_guess('model.FCStd', None)[0])")

    def guess() -> str:
        env = {**os.environ, "XDG_DATA_DIRS": "/usr/share"}
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=env, timeout=60).stdout.strip()

    if guess() == "application/x-extension-fcstd":
        pytest.skip("this computer already knows the FreeCAD file type")
    src = make_sample_appimage(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")
    assert cli.main(["easy-installer", "install", "-y", str(src)]) == 0
    assert guess() == "application/x-extension-fcstd"
    assert cli.main(["easy-installer", "uninstall", "-y", "org.freecad.FreeCAD"]) == 0
    assert guess() != "application/x-extension-fcstd"


# ------------------------------------------------------------------------------------------------
# (e) 0.2: self-updating apps, the kept previous version, "keep both" - across all layers
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_self_updating_app_is_picked_up_and_stays_valid(isolated_env, downloads, capsys):
    """Install with the command line, let the "app" replace its own file, reconcile."""
    from easy_installer.core.reconcile import needs_reconcile, reconcile_all

    layout = user_layout()
    assert cli.main(["easy-installer", "install", "-y", str(_demo(downloads, "1.0"))]) == 0
    v1 = Registry(layout.registry_path).get("demo")
    assert v1.mtime_ns == os.stat(v1.appimage_path).st_mtime_ns and not needs_reconcile(v1)

    # The running app downloads 1.1 (other icon format) and writes it over its own file.
    newer = _demo(downloads, "1.1", svg_icon=True)
    shutil.copyfile(newer, v1.appimage_path)
    assert needs_reconcile(v1)
    results = reconcile_all()
    assert [(r.app.id, r.change, r.old_version, r.new_version) for r in results] == \
        [("demo", "updated", "1.0", "1.1")]
    app = Registry(layout.registry_path).get("demo")
    assert app == results[0].app and app.version == "1.1" and app.sha256 == sha256_of(newer)
    assert app.size == newer.stat().st_size and app.status() == "ok" and app.previous is None
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text())
    assert entry.get("X-AppImage-Version") == "1.1" and entry.get("Icon") == "easyinstaller-demo"
    assert_valid_desktop_file(Path(app.desktop_path))
    assert Path(app.icon_paths[0]).suffix == ".svg" and not Path(v1.icon_paths[0]).exists()
    check_icon(Path(app.icon_paths[0]), layout.icons_dir, "demo")
    assert visible(layout.apps_dir) == ["Demo.AppImage"] and not layout.backups_dir.exists()

    capsys.readouterr()
    assert cli.main(["easy-installer", "list", "--json"]) == 0
    assert [(a["id"], a["version"], a["status"]) for a in json.loads(capsys.readouterr().out)] == \
        [("demo", "1.1", "ok")]
    assert cli.main(["easy-installer", "uninstall", "-y", "demo"]) == 0
    assert files_below(layout.apps_dir) == []


@requires_mksquashfs
@requires_unsquashfs
def test_user_update_rollback_and_uninstall(isolated_env, downloads, capsys):
    layout = user_layout()
    assert cli.main(["easy-installer", "install", "-y", str(_demo(downloads, "1.0"))]) == 0
    v1 = Registry(layout.registry_path).get("demo")
    assert cli.main(["easy-installer", "install", "-y",
                     str(_demo(downloads, "1.1", svg_icon=True))]) == 0
    v2 = Registry(layout.registry_path).get("demo")
    assert v2.previous["version"] == "1.0" and sha256_of(Path(v2.previous["path"])) == v1.sha256

    back = installer.rollback("demo", Scope.USER)
    assert back.version == "1.0" and sha256_of(Path(back.appimage_path)) == v1.sha256
    assert back.previous["version"] == "1.1" and sha256_of(Path(back.previous["path"])) == v2.sha256
    entry = DesktopEntry.parse(Path(back.desktop_path).read_text())
    assert entry.get("X-AppImage-Version") == "1.0"
    assert_valid_desktop_file(Path(back.desktop_path))
    assert Path(back.icon_paths[0]).suffix == ".png" and not Path(v2.icon_paths[0]).exists()
    assert files_below(layout.apps_dir) == [".easyinstaller-backups/demo/Demo-1.1.AppImage",
                                            "Demo.AppImage"]

    assert cli.main(["easy-installer", "uninstall", "-y", "demo"]) == 0
    assert list(layout.apps_dir.iterdir()) == []       # the kept version went with the app
    leftovers = set(files_below(isolated_env)) - {str(p.relative_to(isolated_env))
                                                 for p in downloads.rglob("*")}
    assert leftovers <= {".local/share/easy-installer/registry.json",
                         ".local/share/easy-installer/registry.json.lock",
                         ".local/share/applications/mimeinfo.cache"}, leftovers


@requires_mksquashfs
@requires_unsquashfs
def test_system_rollback_keep_both_and_drop_backup_through_the_real_helper(
        helper_pipe, sys_layout, downloads, capsys):
    """privileged.run_helper -> JSON -> helper.main -> ops, for every new helper request."""
    registry = Registry(sys_layout.registry_path)
    assert cli.main(["easy-installer", "install", "-y", "--system",
                     str(_demo(downloads, "1.0"))]) == 0
    v1 = registry.get("demo")
    assert cli.main(["easy-installer", "install", "-y", "--system",
                     str(_demo(downloads, "1.1"))]) == 0
    v2 = registry.get("demo")
    kept = Path(v2.previous["path"])
    assert kept.parent == sys_layout.apps_dir / ".easyinstaller-backups" / "demo"

    # going back: inspected by the client (no root needed to read it), swapped by the helper
    back = installer.rollback("demo", Scope.SYSTEM)
    request = helper_pipe.calls[-1][1]
    assert helper_pipe.ops[-1] == "install" and request["consume_backup"] is True
    assert request["source_appimage"] == str(kept)
    assert back.version == "1.0" and sha256_of(Path(back.appimage_path)) == v1.sha256
    assert not kept.exists() and sha256_of(Path(back.previous["path"])) == v2.sha256
    assert registry.get("demo") == back
    assert_valid_desktop_file(Path(back.desktop_path))

    # keep both: 2.0 next to the installed 1.0
    newest = _demo(downloads, "2.0")
    info = inspect_appimage(newest)
    with info:
        plan = installer.plan_install(info, installer.InstallOptions(scope=Scope.SYSTEM,
                                                                     keep_both=True))
        assert plan.keep_both_available and plan.app_id == "demo--2.0"
        copy = installer.execute_install(plan)
    assert (copy.id, copy.name, copy.base_id, copy.pinned) == ("demo--2.0", "Demo 2.0", "demo", True)
    assert registry.get("demo") == back and len(registry.load()) == 2
    assert DesktopEntry.parse(Path(copy.desktop_path).read_text()).get("Name") == "Demo 2.0"
    assert_valid_desktop_file(Path(copy.desktop_path))
    assert visible(sys_layout.apps_dir) == ["Demo-2.0.AppImage", "Demo.AppImage"]

    # the kept version is deleted by the helper only
    installer.drop_backup("demo", Scope.SYSTEM)
    assert helper_pipe.calls[-1] == ("drop-backup", {"app_id": "demo",
                                                     "client_version": privileged.__version__})
    assert registry.get("demo").previous is None
    assert files_below(sys_layout.apps_dir) == ["Demo-2.0.AppImage", "Demo.AppImage"]

    capsys.readouterr()
    assert cli.main(["easy-installer", "uninstall", "-y", "--system", "demo"]) == 0
    assert [a.id for a in registry.all()] == ["demo--2.0"]
    assert installer.uninstall("demo--2.0", Scope.SYSTEM) == []
    assert files_below(sys_layout.apps_dir) == []
