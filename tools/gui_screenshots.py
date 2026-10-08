#!/usr/bin/env python3
"""Render Easy Installer's GUI headlessly and save PNG screenshots (developer tool).

    .venv/bin/python tools/gui_screenshots.py [--out build/screenshots] [--backend broadway] [--language de]

(``EASY_INSTALLER_LOCALEDIR=<dir>`` renders with the catalogs in that folder instead of
build/locale, e.g. to try out translations before they go into po/.)

The tool starts its own ``gtk4-broadwayd`` on a free display, runs the app in a throw-away
HOME/XDG environment seeded with a few *fake* AppImages (tests/fakeappimage.py) that are installed
with the real core installer, drives the UI programmatically and captures the window's widget tree
offscreen (Gtk.WidgetPaintable → Gsk.CairoRenderer → PNG). Adw.Dialogs render inside their parent
window, so the window captures include them.

Scenes: the 0.1 window and install dialog (01-15), the 0.2 main window, details, uninstall with
data and preferences (16-25), and the 0.2 install and update dialogs (30-42: replace or keep
both, origin/signature/update-source facts, a changed signer, a portable archive with the
program chooser; update confirm, download, install, done, error, changed signer, dark mode,
an app installed for everyone), and the review scenes (43-47: an update that is no longer
needed, an app uninstalled while its update ran, no uninstall while it is busy, archives that
are an installer or an AppImage). Updates never touch the network: checks are answered by fakes
and the update dialog's download copies a local fake file.

Nothing is ever written to the real HOME: every path Easy Installer uses is redirected into a
temporary directory (HOME, XDG_*_HOME, EASY_INSTALLER_APPS_DIR), GSettings uses the in-memory
backend, the app runs on a private D-Bus session (if ``dbus-run-session`` exists) and pkexec is
disabled. The real screen is never used by default: only ``--backend x11`` does, and
``--backend auto`` when ``gtk4-broadwayd`` cannot be started at all (never after a Broadway run
failed - a window would appear on the user's screen and be driven there).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"
DEFAULT_OUT = REPO / "build" / "screenshots"
CHILD_TIMEOUT = 300

SHOTS = (
    "01-empty.png", "02-list.png", "03-install-loading.png", "04-install-confirm.png",
    "05-install-update.png", "06-install-done.png", "07-uninstall-confirm.png",
    "07b-uninstall-without-password.png", "07c-uninstall-notes.png",
    "08-dark-list.png", "09-install-electron-sandbox.png", "10-install-error.png",
    "10b-install-error-latin1-name.png",
    "11-system-check.png", "12-install-progress.png", "13-drop-overlay.png",
    "14-about.png", "15-shortcuts.png",
    # 0.2: updates, details, uninstall with data, preferences
    "16-list-update.png", "17-updates-banner.png", "18-checking.png", "18b-check-result.png",
    "19-details.png", "19b-details-bottom.png", "19c-details-dark.png",
    "20-details-portable.png", "21-uninstall-data.png", "22-preferences.png",
    "23-go-back-confirm.png", "24-list-narrow.png", "25-details-check-now.png",
    # 0.2: install and update dialogs
    "30-install-keep-both.png", "30b-install-keep-both-end.png", "31-install-keep-both-chosen.png",
    "32-install-trust.png", "33-install-signer-changed.png", "34-install-portable.png",
    "34b-install-portable-end.png", "35-update-confirm.png", "36-update-downloading.png",
    "37-update-installing.png", "38-update-done.png", "38b-update-done-note.png",
    "39-update-error.png", "40-update-signer-changed.png", "41-update-confirm-dark.png",
    "42-update-system-confirm.png",
    # 0.2 review: an update no longer needed, an app uninstalled meanwhile, no uninstall while
    # busy, archives that are no app
    "43-update-not-needed.png", "44-update-app-gone.png", "45-uninstall-busy.png",
    "46-install-archive-installer.png", "47-install-archive-appimage.png",
)


# ================================================================================================
# parent: environment, display server, child process
# ================================================================================================

def ui_locale(language: str) -> str:
    """A UTF-8 locale under which GTK/libadwaita honour LANGUAGE for their own texts.

    glibc ignores LANGUAGE in the C locales, so "Troubleshooting", "Cancel", ... would stay
    English in German screenshots. Prefer an installed locale of the language itself, then any
    installed non-C UTF-8 locale; C.UTF-8 only for English or if nothing else is installed.
    """
    if language.split("_")[0] in ("", "en", "C"):
        return "C.UTF-8"
    try:
        installed = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10,
                                   check=False).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return "C.UTF-8"
    utf8 = [name for name in installed if name.lower().endswith((".utf8", ".utf-8"))
            and not name.startswith(("C.", "POSIX"))]
    own = [name for name in utf8 if name.split("_")[0].split(".")[0] == language.split("_")[0]]
    preferred = [name for name in utf8 if name.startswith("en_US")]
    return (own or preferred or utf8 or ["C.UTF-8"])[0]


def isolated_env(root: Path, language: str = "en") -> dict[str, str]:
    home = root / "home"
    for sub in ("", ".local/share", ".config", ".cache", "Downloads"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    # GLib ignores launchers whose program is not on PATH; a stub makes our own launcher (and
    # with it the "open AppImages with Easy Installer" banner) visible. It is never executed.
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "easy-installer"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("XDG_DATA_", "XDG_CONFIG_", "XDG_CACHE_", "XDG_STATE_",
                                "XDG_RUNTIME_", "EASY_INSTALLER_", "GDK_", "GTK_", "BROADWAY_",
                                "DBUS_", "SSH_AUTH_", "GNOME_KEYRING"))}
    # A catalog to try out (e.g. new translations before they are in po/) may be passed on.
    if os.environ.get("EASY_INSTALLER_LOCALEDIR"):
        env["EASY_INSTALLER_LOCALEDIR"] = os.environ["EASY_INSTALLER_LOCALEDIR"]
    env.update({
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        # No gvfs / portals / keyring daemons: nothing may reach into the real session.
        "GIO_USE_VFS": "local",
        "GDK_DEBUG": "no-portals",
        "ADW_DISABLE_PORTAL": "1",
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DOWNLOAD_DIR": str(home / "Downloads"),
        "EASY_INSTALLER_APPS_DIR": str(home / "Applications"),
        "EASY_INSTALLER_DISABLE_PKEXEC": "1",
        # apps installed for everyone on this machine stay out of the pictures
        "EASY_INSTALLER_SYSTEM_ROOT": str(root / "sysroot"),
        "GSETTINGS_BACKEND": "memory",
        "NO_AT_BRIDGE": "1",
        "GTK_A11Y": "none",
        "LANGUAGE": language,
        "LC_ALL": ui_locale(language),
        "PATH": os.pathsep.join([str(bindir), os.environ.get("PATH", "/usr/bin:/bin")]),
        "PYTHONPATH": os.pathsep.join([str(SRC), str(TESTS)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def start_broadway(env: dict[str, str], log_path: Path) -> tuple[subprocess.Popen, int] | None:
    tool = shutil.which("gtk4-broadwayd")
    if tool is None:
        print("gtk4-broadwayd not found", file=sys.stderr)
        return None
    runtime = Path(env["XDG_RUNTIME_DIR"])
    for display in range(9, 64):
        # gtk4-broadwayd :N listens on $XDG_RUNTIME_DIR/broadway<N+1>.socket and port 8080+N.
        if (runtime / f"broadway{display + 1}.socket").exists() or not _port_free(8080 + display):
            continue
        with open(log_path, "ab") as log:
            proc = subprocess.Popen([tool, f":{display}"], stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if (runtime / f"broadway{display + 1}.socket").exists():
                return proc, display
            time.sleep(0.05)
        stop_process(proc)
    return None


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_child(env: dict[str, str], out: Path) -> int:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--out", str(out)]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--", *cmd]
    try:
        return subprocess.run(cmd, env=env, timeout=CHILD_TIMEOUT, check=False).returncode
    except subprocess.TimeoutExpired:
        print("screenshot run timed out", file=sys.stderr)
        return 124


def parent_main(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in SHOTS:
        (out / name).unlink(missing_ok=True)
    root = Path(tempfile.mkdtemp(prefix="easy-installer-shots-"))
    used = None
    try:
        env = isolated_env(root, args.language)
        rc = 1
        broadway_ran = False
        if args.backend in ("auto", "broadway"):
            started = start_broadway(env, root / "broadwayd.log")
            if started is None:
                print("could not start gtk4-broadwayd", file=sys.stderr)
            else:
                proc, display = started
                broadway_ran = True
                headless = {k: v for k, v in env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
                try:
                    print(f"rendering with Broadway on :{display}")
                    rc = run_child({**headless, "GDK_BACKEND": "broadway",
                                    "BROADWAY_DISPLAY": f":{display}"}, out)
                    used = f"broadway :{display}"
                finally:
                    stop_process(proc)
        # The real screen only when asked for; a failed Broadway run is reported, not repeated there.
        use_x11 = args.backend == "x11" or (args.backend == "auto" and not broadway_ran)
        if use_x11 and os.environ.get("DISPLAY"):
            print("using the X11 display (a window will appear on the screen)")
            rc = run_child({**env, "GDK_BACKEND": "x11", "DISPLAY": os.environ["DISPLAY"]}, out)
            used = "x11"
        if rc != 0:
            print(f"screenshot run failed (exit code {rc})", file=sys.stderr)
            return rc
        print(f"backend: {used}")
        for name in SHOTS:
            path = out / name
            print(f"  {'ok     ' if path.is_file() else 'MISSING'} {path}")
        return 0 if all((out / n).is_file() for n in SHOTS) else 1
    finally:
        if args.keep:
            print(f"kept temporary environment in {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


# ================================================================================================
# child: seed fake apps, drive the UI, capture
# ================================================================================================

def icon_svg(top: str, bottom: str, glyph: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
        '</linearGradient></defs>'
        '<rect x="10" y="12" width="108" height="108" rx="26" fill="#000" fill-opacity="0.18"/>'
        '<rect x="10" y="8" width="108" height="108" rx="26" fill="url(#bg)"/>'
        f"{glyph}</svg>\n"
    )


GLYPH_CUBE = ('<path d="M64 30 L94 46 L94 80 L64 96 L34 80 L34 46 Z" fill="#fff" fill-opacity="0.95"/>'
              '<path d="M64 62 L94 46 M64 62 L34 46 M64 62 L64 96" stroke="#e8a317" '
              'stroke-width="4" fill="none"/>')
GLYPH_GEAR = ('<circle cx="64" cy="62" r="30" fill="none" stroke="#fff" stroke-width="12" '
              'stroke-dasharray="10 6"/><circle cx="64" cy="62" r="12" fill="#fff"/>')
GLYPH_CODE = ('<path d="M50 40 L28 62 L50 84 M78 40 L100 62 L78 84" stroke="#fff" stroke-width="10" '
              'stroke-linecap="round" stroke-linejoin="round" fill="none"/>')
GLYPH_BOAT = ('<path d="M28 74 L100 74 L88 94 L40 94 Z" fill="#fff"/>'
              '<path d="M62 30 L62 70 L36 70 Z" fill="#fff" fill-opacity="0.9"/>'
              '<path d="M68 38 L68 70 L90 70 Z" fill="#fff" fill-opacity="0.7"/>')
GLYPH_PEN = ('<path d="M40 92 L46 70 L80 36 L94 50 L60 84 Z" fill="#fff"/>'
             '<path d="M40 92 L46 70 L60 84 Z" fill="#fff" fill-opacity="0.6"/>')
GLYPH_DISK = ('<circle cx="64" cy="62" r="32" fill="#fff"/><circle cx="64" cy="62" r="10" '
              'fill="#1f9e93"/><path d="M64 30 A32 32 0 0 1 96 62" stroke="#1f9e93" '
              'stroke-width="6" fill="none"/>')
GLYPH_NOTES = ('<rect x="38" y="30" width="52" height="66" rx="8" fill="#fff"/>'
               '<path d="M48 48 H80 M48 60 H80 M48 72 H70" stroke="#6a4bc4" stroke-width="6" '
               'stroke-linecap="round"/>')


def desktop(name: str, icon: str, version: str | None, comment: str, exec_line: str,
            categories: str) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={name}", f"Comment={comment}",
             f"Exec={exec_line}", f"Icon={icon}", f"Categories={categories}", "Terminal=false"]
    if version:
        lines.append(f"X-AppImage-Version={version}")
    return "\n".join(lines) + "\n"


def build_fake(staging: Path, filename: str, stem: str, name: str, version: str | None,
               comment: str, colors: tuple[str, str], glyph: str, *, electron: bool = False,
               no_sandbox: bool = False, upd_info: str = "",
               extra: dict[str, bytes | str] | None = None) -> Path:
    from fakeappimage import make_fake_appimage

    exec_line = "AppRun --no-sandbox %U" if no_sandbox else "AppRun %U"
    icon_rel = f"usr/share/icons/hicolor/scalable/apps/{stem}.svg"
    files: dict[str, bytes | str] = {
        f"{stem}.desktop": desktop(name, stem, version, comment, exec_line, "Utility;"),
        "AppRun": "#!/bin/sh\nexit 0\n",
        icon_rel: icon_svg(*colors, glyph),
    }
    if electron:
        files["chrome-sandbox"] = b"\x7fELF fake chrome-sandbox"
        files["resources/app.asar"] = b"fake asar"
    files.update(extra or {})
    return make_fake_appimage(staging / filename, files, {".DirIcon": icon_rel}, upd_info=upd_info)


def build_portable(staging: Path) -> Path:
    """A zip like balenaEtcher's: one top folder with an Electron program and its helpers."""
    from fakearchive import electron_tree, make_zip

    tree = electron_tree("balenaEtcher-linux-x64", program="balena-etcher")
    files = dict(tree["files"])
    files["balenaEtcher-linux-x64/balena-etcher.svg"] = icon_svg(
        "#5fd4c9", "#1f9e93", GLYPH_DISK)
    return make_zip(staging / "balenaEtcher-linux-x64-2.1.4.zip", files, tree["symlinks"],
                    tree["modes"])


def set_origin(path: Path, url: str) -> None:
    """What a browser notes on a downloaded file (``user.xdg.origin.url``)."""
    try:
        os.setxattr(path, "user.xdg.origin.url", url.encode())
    except OSError as exc:  # e.g. a tmpfs without user xattrs
        print(f"cannot set the origin of {path.name}: {exc}", flush=True)


def fill_folder(folder: Path, files: dict[str, int]) -> None:
    """Settings and data an app left behind (sizes in bytes; content is irrelevant)."""
    for name, size in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)
            fh.write(b"x")


def child_main(out: Path) -> int:
    import dataclasses
    import threading

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Graphene, Gsk, Gtk

    import functools

    from easy_installer.core import inspector, system_checks, updater, updates
    from easy_installer.core.inspector import inspect_appimage
    from easy_installer.core.installer import InstallOptions, execute_install, plan_install
    from easy_installer.core.paths import Scope, user_layout
    from easy_installer.core.portable import inspect_portable
    from easy_installer.core.registry import Registry
    from easy_installer.core.signature import SignatureInfo
    from easy_installer.core.updater import UpdateCheckResult
    from easy_installer.core.updates import AvailableUpdate
    from easy_installer.errors import (
        AuthorizationError,
        InstallError,
        NetworkError,
        UpdateCancelled,
    )
    from easy_installer.gui import details_dialog, install_dialog, update_dialog
    from easy_installer.gui import window as window_module
    from easy_installer.gui.application import EasyInstallerApp
    from easy_installer.gui.uninstall_dialog import UninstallOutcome
    from easy_installer.gui.window import MainWindow
    from easy_installer.i18n import _

    # Never the network: every update check below is answered by these fakes.
    def offline(url, **_kwargs):
        raise NetworkError("There is no internet connection right now.", details=url)

    updates.http_get = offline
    fake_updates: dict[tuple[str, str], AvailableUpdate] = {}
    check_gate = threading.Event()
    check_gate.set()

    def fake_check_all(apps=None, *, force=False, progress=None, fetch=None):
        check_gate.wait(60)
        return UpdateCheckResult(updates=dict(fake_updates), errors={}, checked=2)

    def fake_check_app(app, *, force=False, fetch=None):
        time.sleep(0.2)
        return fake_updates.get((Scope(app.scope).value, app.id))

    window_module.check_all_updates = fake_check_all
    details_dialog.check_app_update = fake_check_app

    def available(version: str, filename: str, repo: str, size: int) -> AvailableUpdate:
        return AvailableUpdate(
            version=version, filename=filename, size=size, sha256=None, sha512=None,
            url=f"https://github.com/{repo}/releases/download/{version}/{filename}",
            release_url=f"https://github.com/{repo}/releases/tag/{version}",
            published_at="2026-09-24T09:30:00Z")

    freecad_update = available("1.1.4", "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage",
                               "FreeCAD/FreeCAD", 861_400_000)
    t3_update = available("0.0.44", "T3-Code-0.0.44-x86_64.AppImage", "pingdotgg/t3code",
                          158_300_000)

    home = Path(os.environ["HOME"])
    assert str(home).startswith(tempfile.gettempdir()), "refusing to run outside a temp HOME"
    # Apps installed for everyone on this machine (/var/lib/easy-installer) stay out of the
    # pictures: a private, empty system root.
    from easy_installer.core import installer as core_installer
    from easy_installer.core.paths import system_layout

    real_layout_for = core_installer.layout_for
    core_installer.layout_for = lambda scope: (system_layout(home.parent / "sysroot")
                                               if Scope(scope) is Scope.SYSTEM
                                               else real_layout_for(scope))
    staging = home.parent / "staging"
    downloads = home / "Downloads"
    staging.mkdir(exist_ok=True)

    # Our own launcher, so that the "make default" banner can be offered (isolated data dir).
    apps_dir = Path(os.environ["XDG_DATA_HOME"]) / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "data" / "com.roothirsch.EasyInstaller.desktop", apps_dir)

    print("building fake AppImages…", flush=True)
    freecad_upd = "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"
    t3_feed = {"resources/app-update.yml": "provider: github\nowner: pingdotgg\nrepo: t3code\n"}
    seed = [
        build_fake(staging, "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad-nightly", "OpenSCAD",
                   "2026.03.28", "The programmers solid 3D CAD modeller", ("#f7c948", "#e8a317"),
                   GLYPH_CUBE),
        # 1.1.2 first: updating it to 1.1.3 keeps 1.1.2 as the previous version.
        build_fake(staging, "FreeCAD_1.1.2-Linux-x86_64-py311.AppImage", "org.freecad.FreeCAD",
                   "FreeCAD", None, "Feature based parametric modeler", ("#e0443e", "#a3201d"),
                   GLYPH_GEAR, upd_info=freecad_upd, extra={"usr/share/freecad/VERSION": "1.1.2"}),
        build_fake(staging, "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "org.freecad.FreeCAD",
                   "FreeCAD", None, "Feature based parametric modeler", ("#e0443e", "#a3201d"),
                   GLYPH_GEAR, upd_info=freecad_upd, extra={"usr/share/freecad/VERSION": "1.1.3"}),
        build_fake(staging, "T3-Code-0.0.42-x86_64.AppImage", "t3code", "T3 Code (Alpha)",
                   "0.0.42", "T3 Code desktop build", ("#3a3f4b", "#1d2027"), GLYPH_CODE,
                   electron=True, no_sandbox=True, extra=t3_feed),
        build_fake(staging, "winboat-0.9.0-x86_64.AppImage", "winboat", "WinBoat", "0.9.0",
                   "Windows for Penguins", ("#3d8fe0", "#1c5fae"), GLYPH_BOAT, electron=True,
                   no_sandbox=True),
    ]
    pen = build_fake(downloads, "Pen-1.2.8-x86_64.AppImage", "pen", "Pen", "1.2.8",
                     "Sketch, draw and write notes by hand", ("#33b28a", "#1d7d5f"), GLYPH_PEN)
    openscad_new = build_fake(downloads, "OpenSCAD-2026.04.15-x86_64.AppImage",
                              "openscad-nightly", "OpenSCAD", "2026.04.15",
                              "The programmers solid 3D CAD modeller", ("#f7c948", "#e8a317"),
                              GLYPH_CUBE)
    anytype = build_fake(downloads, "Anytype-0.55.4.AppImage", "anytype", "Anytype", "0.55.4",
                         "Your personal knowledge workspace", ("#8a6be0", "#5b3cb8"), GLYPH_NOTES,
                         electron=True)
    broken = downloads / "holiday-photos.zip"
    broken.write_bytes(b"PK\x03\x04 definitely not an AppImage")
    # A name that is not UTF-8 (e.g. unpacked from an old Windows zip): shown with U+FFFD.
    latin1 = downloads / os.fsdecode(b"Caf\xe9-Rezepte.zip")
    latin1.write_bytes(b"PK\x03\x04 not an AppImage either")

    context = GLib.MainContext.default()
    state: dict[str, object] = {"rc": 1}

    def iterate() -> None:
        if not context.iteration(False):
            time.sleep(0.01)

    def wait_until(predicate, timeout: float = 30.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {what}")
            iterate()

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            iterate()

    def settle(window: Gtk.Window, frames: int = 2) -> None:
        """Let the window lay out and paint ``frames`` new frames (Broadway: ~1 frame/s).

        queue_draw() drops the window's cached render node until the next frame is painted, so
        it is only called right before waiting for that frame.
        """
        clock = window.get_frame_clock()
        for _ in range(frames):
            start = clock.get_frame_counter()
            window.queue_draw()
            wait_until(lambda: clock.get_frame_counter() > start, timeout=15, what="a frame")
        pump(0.05)

    def render_node(window: Gtk.Window, width: int, height: int):
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        return snapshot.to_node()

    def capture(window: Gtk.Window, name: str) -> None:
        settle(window)
        width, height = window.get_width(), window.get_height()
        node = render_node(window, width, height)
        for _attempt in range(5):
            if node is not None:
                break
            settle(window, 1)
            node = render_node(window, width, height)
        if node is None:
            raise RuntimeError(f"nothing rendered for {name}")
        renderer = Gsk.CairoRenderer()
        renderer.realize(None)
        try:
            texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
        finally:
            renderer.unrealize()
        texture.save_to_png(str(out / name))
        print(f"captured {name} ({width}×{height})", flush=True)

    def seed_installs() -> None:
        for path in seed:
            info = inspect_appimage(path)
            try:
                execute_install(plan_install(info, InstallOptions(scope=Scope.USER)))
            finally:
                info.cleanup()
        # One app whose file was deleted behind our back → "App file is missing".
        (home / "Applications" / "WinBoat.AppImage").unlink()
        # What a real download and a signed AppImage would have recorded.
        registry = Registry(user_layout().registry_path)
        freecad = registry.get("org.freecad.FreeCAD")
        registry.put(dataclasses.replace(
            freecad,
            origin_url="https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/"
                       "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage",
            signature={"status": "valid", "signer": "FreeCAD Release Team",
                       "fingerprint": "7A9C4E2B1F3D5A6C8E0B2D4F6A8C0E2B4D6F8A1C",
                       "details": None}))
        # Settings and data FreeCAD keeps in the home folder.
        fill_folder(home / ".config" / "FreeCAD", {"user.cfg": 48_000, "system.cfg": 12_000})
        fill_folder(home / ".local" / "share" / "FreeCAD", {"Macro/gears.FCMacro": 180_000,
                                                          "Mod/README": 2_400_000})
        fill_folder(home / ".cache" / "FreeCAD", {"Cache/thumbs.db": 31_000_000})

    def seed_portable() -> None:
        info = inspect_portable(build_portable(staging))
        try:
            execute_install(plan_install(info, InstallOptions(scope=Scope.USER)))
        finally:
            info.cleanup()

    def scenario() -> bool:
        try:
            run_scenario()
            state["rc"] = 0
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            app.quit()
        return GLib.SOURCE_REMOVE

    def open_dialog(win: MainWindow, path: Path, page: str) -> install_dialog.InstallDialog:
        win.install_files([path])
        wait_until(lambda: win.current_dialog is not None, what="install dialog")
        dialog = win.current_dialog
        wait_until(lambda: dialog.page == page, what=f"page {page}")
        return dialog

    def close_dialog(win: MainWindow, dialog: Adw.Dialog) -> None:
        dialog.close()
        wait_until(lambda: win.get_visible_dialog() is None, what="dialog to close")

    def scroll_to_end(dialog: install_dialog.InstallDialog) -> None:
        scrolled = dialog._stack.get_child_by_name(install_dialog.PAGE_CONFIRM)
        pump(0.3)
        adj = scrolled.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def run_scenario() -> None:
        wait_until(lambda: any(isinstance(w, MainWindow) for w in app.get_windows()),
                   what="main window")
        win = next(w for w in app.get_windows() if isinstance(w, MainWindow))
        gtk_settings = Gtk.Settings.get_default()
        gtk_settings.set_property("gtk-enable-animations", False)
        if gtk_settings.get_property("gtk-xft-dpi") <= 0:
            # Broadway reports no DPI (-1); libadwaita's "sp" units (Adw.Clamp sizes) would
            # collapse to 0 px. Use what an X11/Wayland session reports at 100 % scale.
            gtk_settings.set_property("gtk-xft-dpi", 96 * 1024)
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        wait_until(lambda: win.get_mapped(), what="window to map")

        capture(win, "01-empty.png")

        win._on_dismiss_handler_banner(None)  # one banner screenshot is enough
        seed_installs()
        win.reload()
        capture(win, "02-list.png")

        # Loading page: hold the inspection until the page was captured.
        gate = threading.Event()
        real_inspect = install_dialog.inspect_appimage

        def slow_inspect(*args, **kwargs):
            gate.wait(30)
            return real_inspect(*args, **kwargs)

        install_dialog.inspect_appimage = slow_inspect
        try:
            win.install_files([pen])
            wait_until(lambda: win.current_dialog is not None, what="install dialog")
            dialog = win.current_dialog
            capture(win, "03-install-loading.png")
        finally:
            gate.set()
            install_dialog.inspect_appimage = real_inspect
        wait_until(lambda: dialog.page == install_dialog.PAGE_CONFIRM, what="confirm page")
        capture(win, "04-install-confirm.png")

        dialog.install()
        wait_until(lambda: dialog.page in (install_dialog.PAGE_DONE, install_dialog.PAGE_ERROR),
                   what="installation")
        capture(win, "06-install-done.png")
        close_dialog(win, dialog)

        dialog = open_dialog(win, openscad_new, install_dialog.PAGE_CONFIRM)
        capture(win, "05-install-update.png")
        close_dialog(win, dialog)

        t3 = next(r.app for r in win.rows if r.app.id == "t3code")
        alert = win.confirm_uninstall(t3)
        wait_until(lambda: alert.data_ready, what="the search for settings and data")
        capture(win, "07-uninstall-confirm.png")
        close_dialog(win, alert)

        # A per-user app with its sandbox permission, and no administrator password given.
        t3_with_profile = dataclasses.replace(
            t3, apparmor_profile="/etc/apparmor.d/easyinstaller-t3code")
        alert = win._offer_uninstall_keeping_permission(
            t3_with_profile, AuthorizationError(_("Authentication was cancelled.")), False)
        capture(win, "07b-uninstall-without-password.png")
        close_dialog(win, alert)
        win._on_uninstalled(t3_with_profile, None, False, UninstallOutcome(notes=[_(
            "The special permission that {name} needed was left on this computer, because only "
            "an administrator can remove it.").format(name=t3.name)]))
        wait_until(lambda: win.get_visible_dialog() is not None, what="notes alert")
        capture(win, "07c-uninstall-notes.png")
        close_dialog(win, win.get_visible_dialog())

        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        win.reload(force=True)
        capture(win, "08-dark-list.png")
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        # Electron app on a system that restricts user namespaces (Ubuntu 24.04+).
        real_status = system_checks.get_system_status()
        system_checks._cache = dataclasses.replace(
            real_status, userns_restricted=True, apparmor_enabled=True,
            apparmor_parser=real_status.apparmor_parser or "/usr/sbin/apparmor_parser",
            pkexec=real_status.pkexec or "/usr/bin/pkexec")
        dialog = open_dialog(win, anytype, install_dialog.PAGE_CONFIRM)
        dialog.expand_options(True)
        settle(win)
        scroll_to_end(dialog)
        capture(win, "09-install-electron-sandbox.png")
        close_dialog(win, dialog)
        system_checks._cache = real_status

        dialog = open_dialog(win, broken, install_dialog.PAGE_ERROR)
        capture(win, "10-install-error.png")
        close_dialog(win, dialog)

        dialog = open_dialog(win, latin1, install_dialog.PAGE_ERROR)
        capture(win, "10b-install-error-latin1-name.png")
        close_dialog(win, dialog)

        win.show_system_check()
        wait_until(lambda: win.get_visible_dialog() is not None, what="system check")
        capture(win, "11-system-check.png")
        close_dialog(win, win.get_visible_dialog())

        # Progress page: hold the installation half-way.
        gate = threading.Event()
        real_execute = install_dialog.execute_install

        def slow_execute(plan, progress=None):
            progress(0.45, _("Copying the app…"))
            gate.wait(30)
            return real_execute(plan, progress=progress)

        install_dialog.execute_install = slow_execute
        try:
            dialog = open_dialog(win, openscad_new, install_dialog.PAGE_CONFIRM)
            dialog.install()
            wait_until(lambda: dialog.page == install_dialog.PAGE_PROGRESS, what="progress")
            capture(win, "12-install-progress.png")
        finally:
            gate.set()
            install_dialog.execute_install = real_execute
        wait_until(lambda: dialog.page == install_dialog.PAGE_DONE, what="update to finish")
        close_dialog(win, dialog)

        win.set_drop_overlay_visible(True)
        capture(win, "13-drop-overlay.png")
        win.set_drop_overlay_visible(False)

        about = win.show_about()
        wait_until(lambda: win.get_visible_dialog() is not None, what="about dialog")
        capture(win, "14-about.png")
        close_dialog(win, about)

        win.lookup_action("show-help-overlay").activate(None)

        def shortcuts_window() -> Gtk.ShortcutsWindow | None:
            return next((w for w in Gtk.Window.list_toplevels()
                         if isinstance(w, Gtk.ShortcutsWindow) and w.get_mapped()), None)

        wait_until(lambda: shortcuts_window() is not None, what="shortcuts window")
        shortcuts = shortcuts_window()
        capture(shortcuts, "15-shortcuts.png")
        shortcuts.close()

        run_v02_scenes(win, style)
        run_dialog_scenes(win, style)

    def row_of(win: MainWindow, app_id: str):
        return next(r for r in win.rows if r.app.id == app_id)

    def check_updates(win: MainWindow, found: dict) -> None:
        fake_updates.clear()
        fake_updates.update(found)
        win.check_for_updates()
        wait_until(lambda: not win.checking, what="update check")

    def run_v02_scenes(win: MainWindow, style: Adw.StyleManager) -> None:
        seed_portable()
        win.reload(force=True)

        # One update: its row offers it.
        check_updates(win, {("user", "org.freecad.FreeCAD"): freecad_update})
        wait_until(lambda: row_of(win, "org.freecad.FreeCAD").update is not None, what="row")
        pump(5.5)   # let the result toast go
        capture(win, "16-list-update.png")

        # Two updates: "Update All" banner.
        check_updates(win, {("user", "org.freecad.FreeCAD"): freecad_update,
                            ("user", "t3code"): t3_update})
        wait_until(lambda: win.updates_banner_revealed, what="updates banner")
        pump(5.5)
        capture(win, "17-updates-banner.png")

        # Checking: the header shows a spinner.
        check_gate.clear()
        try:
            win.check_for_updates()
            wait_until(lambda: win.checking, what="checking")
            capture(win, "18-checking.png")
        finally:
            check_gate.set()
        wait_until(lambda: not win.checking, what="update check")
        capture(win, "18b-check-result.png")
        pump(5.5)

        # Details of an AppImage with a kept previous version and data folders.
        freecad = row_of(win, "org.freecad.FreeCAD").app
        dialog = win.show_details(freecad)
        wait_until(lambda: dialog.locations is not None, what="data sizes")
        dialog.data_row.set_expanded(True)
        capture(win, "19-details.png")
        adjustment = dialog.page.get_first_child().get_vadjustment() \
            if isinstance(dialog.page.get_first_child(), Gtk.ScrolledWindow) else None
        if adjustment is not None:
            pump(0.3)
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        capture(win, "19b-details-bottom.png")
        confirm = dialog._on_go_back_clicked()
        capture(win, "23-go-back-confirm.png")
        close_top(win, confirm)
        close_top(win, dialog)

        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        dialog = win.show_details(freecad)
        wait_until(lambda: dialog.locations is not None, what="data sizes")
        capture(win, "19c-details-dark.png")
        close_top(win, dialog)
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        # "Check Now" in the details of an app that is up to date.
        t3 = row_of(win, "t3code").app
        fake_updates.pop(("user", "t3code"), None)
        dialog = win.show_details(t3)
        wait_until(lambda: dialog.locations is not None, what="data sizes")
        checked = []
        dialog.connect("checked", lambda *_a: checked.append(True))
        dialog.check_now()
        wait_until(lambda: bool(checked), what="check now")
        capture(win, "25-details-check-now.png")
        close_top(win, dialog)

        # A portable app.
        portable = next(r.app for r in win.rows if r.app.kind == "portable")
        dialog = win.show_details(portable)
        wait_until(lambda: dialog.locations is not None, what="data sizes")
        capture(win, "20-details-portable.png")
        close_top(win, dialog)

        # Uninstall with the settings and data folders offered (one of them ticked).
        alert = win.confirm_uninstall(freecad)
        wait_until(lambda: alert.data_ready, what="the search for settings and data")
        if alert.locations:
            alert.set_data_selected(alert.locations[0].path)
        capture(win, "21-uninstall-data.png")
        close_top(win, alert)

        prefs = win.show_preferences()
        capture(win, "22-preferences.png")
        close_top(win, prefs)

        # A narrow window: compact rows.
        check_updates(win, {("user", "org.freecad.FreeCAD"): freecad_update,
                            ("user", "t3code"): t3_update})
        pump(5.5)
        win.set_visible(False)
        win.set_default_size(360, 640)
        win.present()
        wait_until(lambda: 0 < win.get_width() <= 400 and win.compact, what="narrow window")
        capture(win, "24-list-narrow.png")
        win.set_visible(False)
        win.set_default_size(760, 640)
        win.present()
        wait_until(lambda: win.get_width() >= 700, what="wide window")

    # --------------------------------------------------------------------------------------------
    # 0.2: install and update dialogs (on top of the real main window)
    # --------------------------------------------------------------------------------------------

    FREECAD_KEY = "7A9C4E2B1F3D5A6C8E0B2D4F6A8C0E2B4D6F8A1C"
    signatures = {
        "FreeCAD": SignatureInfo("valid", FREECAD_KEY, "FreeCAD Release Team", None),
        "Pen-": SignatureInfo("valid", "D4C3B2A1908F7E6D5C4B3A29180F7E6D5C4B3A29", "Pen Team",
                              None),
    }

    def fake_signature(path, elf):
        """What gpg would say about the fake files (they carry no real signature)."""
        name = Path(path).name
        return next((info for prefix, info in signatures.items() if name.startswith(prefix)),
                    None)

    def open_install(win: MainWindow, path: Path) -> install_dialog.InstallDialog:
        dialog = open_dialog(win, path, install_dialog.PAGE_CONFIRM)
        return dialog

    def scroll_confirm(dialog: install_dialog.InstallDialog, *, end: bool) -> None:
        scrolled = dialog._stack.get_child_by_name(install_dialog.PAGE_CONFIRM)
        pump(0.3)
        adj = scrolled.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size() if end else 0)

    def run_dialog_scenes(win: MainWindow, style: Adw.StyleManager) -> None:
        inspector.read_signature = fake_signature
        freecad_upd = "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"
        pen_feed = {"resources/app-update.yml":
                    "provider: github\nowner: highagency\nrepo: pen-desktop-releases\n"}
        t3_feed = {"resources/app-update.yml": "provider: github\nowner: pingdotgg\nrepo: t3code\n"}

        def freecad_file(folder: Path, version: str) -> Path:
            return build_fake(folder, f"FreeCAD_{version}-Linux-x86_64-py311.AppImage",
                              "org.freecad.FreeCAD", "FreeCAD", None,
                              "Feature based parametric modeler", ("#e0443e", "#a3201d"),
                              GLYPH_GEAR, upd_info=freecad_upd,
                              extra={"usr/share/freecad/VERSION": version})

        freecad_new = freecad_file(downloads, "1.1.4")
        set_origin(freecad_new, "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/"
                                "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage")
        server = staging / "server"
        server.mkdir(exist_ok=True)
        freecad_served = freecad_file(server, "1.1.4")
        pen_new = build_fake(downloads, "Pen-1.2.9-x86_64.AppImage", "pen", "Pen", "1.2.9",
                             "Sketch, draw and write notes by hand", ("#33b28a", "#1d7d5f"),
                             GLYPH_PEN, electron=True, extra=pen_feed)
        set_origin(pen_new, "https://github.com/highagency/pen-desktop-releases/releases/"
                            "download/v1.2.9/Pen-1.2.9-x86_64.AppImage")
        t3_new = build_fake(downloads, "T3-Code-0.0.44-x86_64.AppImage", "t3code",
                            "T3 Code (Alpha)", "0.0.44", "T3 Code desktop build",
                            ("#3a3f4b", "#1d2027"), GLYPH_CODE, electron=True, no_sandbox=True,
                            extra={**t3_feed, "payload.bin": b"unsigned rebuild"})
        from fakearchive import flat_tree, make_archive

        uvtools = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree("UVtools"))
        # The installed T3 Code carries a valid signature: the unsigned 0.0.44 is "another signer".
        registry = Registry(user_layout().registry_path)
        t3 = registry.get("t3code")
        registry.put(dataclasses.replace(t3, signature=SignatureInfo(
            "valid", "3A1F9C0D5E7B2468ACE013579BDF2468ACE01357", "T3 Tools", None).to_dict()))
        win.reload(force=True)

        # --- install dialog: replace or keep both, with the facts about the file ---------------
        dialog = open_install(win, freecad_new)
        assert dialog.plan.keep_both_available, "no keep-both choice"
        assert not dialog.plan.signer_changed, "the same maker signed both"
        capture(win, "30-install-keep-both.png")
        scroll_confirm(dialog, end=True)
        capture(win, "30b-install-keep-both-end.png")
        dialog.set_keep_both(True)
        assert dialog.plan.options.keep_both
        scroll_confirm(dialog, end=False)
        capture(win, "31-install-keep-both-chosen.png")
        close_top(win, dialog)

        dialog = open_install(win, pen_new)
        capture(win, "32-install-trust.png")
        close_top(win, dialog)

        dialog = open_install(win, t3_new)
        assert dialog.plan.signer_changed, "no signer change"
        capture(win, "33-install-signer-changed.png")
        close_top(win, dialog)

        dialog = open_install(win, uvtools)
        assert dialog.is_portable and len(dialog.program_choices) > 1, dialog.program_choices
        capture(win, "34-install-portable.png")
        dialog.expand_options(True)
        scroll_confirm(dialog, end=True)
        capture(win, "34b-install-portable-end.png")
        close_top(win, dialog)

        # --- update dialog: confirm → download → install → done -------------------------------
        freecad = registry.get("org.freecad.FreeCAD")
        update = available("1.1.4", "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage",
                           "FreeCAD/FreeCAD", freecad_served.stat().st_size)
        real_apply = update_dialog.apply_update
        gate = threading.Event()

        def copy_download(update, folder, *, progress=None, cancel=None):
            progress(0.42, "")
            deadline = time.monotonic() + 60
            while not gate.is_set():
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled(_("The download was cancelled."))
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            target = Path(folder) / update.filename
            shutil.copyfile(freecad_served, target)
            return target

        assert win.start_update(freecad, update)
        wait_until(lambda: isinstance(win.get_visible_dialog(), update_dialog.UpdateDialog),
                   what="update dialog")
        dialog = win.get_visible_dialog()
        capture(win, "35-update-confirm.png")
        update_dialog.apply_update = functools.partial(updater.apply_update,
                                                       downloader=copy_download)
        try:
            dialog.start_update()
            wait_until(lambda: dialog._download_bar.get_fraction() > 0.3, what="download")
            capture(win, "36-update-downloading.png")
            gate.set()
            wait_until(lambda: dialog.page in (update_dialog.PAGE_DONE, update_dialog.PAGE_ERROR),
                       what="update to finish")
        finally:
            gate.set()
            update_dialog.apply_update = real_apply
        if dialog.page != update_dialog.PAGE_DONE:
            raise RuntimeError(dialog._error_page.get_description())
        capture(win, "38-update-done.png")
        close_top(win, dialog)
        wait_until(lambda: registry.get("org.freecad.FreeCAD").version == "1.1.4",
                   what="FreeCAD 1.1.4 in the registry")

        installed = registry.get("org.freecad.FreeCAD")
        newer = available("1.2.0", "FreeCAD_1.2.0-Linux-x86_64-py311.AppImage",
                          "FreeCAD/FreeCAD", 874_000_000)

        # Installing: a stand-in holds the installation half-way.
        hold = threading.Event()

        def installing(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                       warnings=None):
            progress(0.86, _("Copying the app…"))
            hold.wait(60)
            raise UpdateCancelled(_("The download was cancelled."))

        update_dialog.apply_update = installing
        try:
            dialog = update_dialog.UpdateDialog(win, installed, newer, autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_INSTALL, what="install page")
            capture(win, "37-update-installing.png")
        finally:
            hold.set()
            update_dialog.apply_update = real_apply
        wait_until(lambda: not dialog.busy, what="stand-in to end")
        close_top(win, dialog)

        # Done, with a note that came up while installing.
        def with_note(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                      warnings=None):
            progress(0.9, _("Copying the app…"))
            if warnings is not None:
                warnings.append(_("The previous version could not be kept, so you cannot go "
                                  "back to it."))
            return dataclasses.replace(app_, version=update_.version)

        update_dialog.apply_update = with_note
        try:
            dialog = update_dialog.UpdateDialog(win, installed, newer, autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_DONE, what="done page")
            capture(win, "38b-update-done-note.png")
        finally:
            update_dialog.apply_update = real_apply
        close_top(win, dialog)

        def offline(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                    warnings=None):
            progress(None, "")
            raise NetworkError(
                _("The internet could not be reached. Please check your connection and try "
                  "again."), details="URLError: [Errno -3] Temporary failure in name resolution")

        update_dialog.apply_update = offline
        try:
            dialog = update_dialog.UpdateDialog(win, installed, newer, autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_ERROR, what="error page")
            capture(win, "39-update-error.png")
        finally:
            update_dialog.apply_update = real_apply
        close_top(win, dialog)

        def resigned(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                     warnings=None):
            if not allow_signer_change:
                raise updater.SignerChangedError("changed", details="test")
            raise UpdateCancelled("cancelled")

        update_dialog.apply_update = resigned
        try:
            dialog = update_dialog.UpdateDialog(win, registry.get("t3code"), t3_update,
                                                autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_CONFIRM and not dialog.busy,
                       what="signer warning")
            capture(win, "40-update-signer-changed.png")
        finally:
            update_dialog.apply_update = real_apply
        close_top(win, dialog)

        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        dialog = update_dialog.UpdateDialog(win, installed, newer)
        capture(win, "41-update-confirm-dark.png")
        close_top(win, dialog)
        style.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        system_app = dataclasses.replace(installed, scope=Scope.SYSTEM)
        dialog = update_dialog.UpdateDialog(win, system_app,
                                            dataclasses.replace(newer, release_url=None))
        capture(win, "42-update-system-confirm.png")
        close_top(win, dialog)

        # --- review scenes ----------------------------------------------------------------------
        def not_needed(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                       warnings=None):
            progress(0.5, "")
            raise updater.UpdateNotNeededError(_(
                "{name} is now at version {version}, so this update is no longer needed.").format(
                    name=app_.name, version="1.2.0"))

        update_dialog.apply_update = not_needed
        try:
            dialog = update_dialog.UpdateDialog(win, installed, newer, autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_DONE, what="not needed")
            capture(win, "43-update-not-needed.png")
        finally:
            update_dialog.apply_update = real_apply
        close_top(win, dialog)

        def uninstalled(app_, update_, *, progress=None, cancel=None, allow_signer_change=False,
                        warnings=None):
            progress(0.5, "")
            raise InstallError(_("Another installation changed the installed apps in the "
                                 "meantime. Please try again."), details="test")

        real_find = update_dialog.find_installed
        update_dialog.apply_update = uninstalled
        update_dialog.find_installed = lambda app_id: []
        try:
            dialog = update_dialog.UpdateDialog(win, installed, newer, autostart=True)
            wait_until(lambda: dialog.page == update_dialog.PAGE_ERROR, what="gone")
            capture(win, "44-update-app-gone.png")
        finally:
            update_dialog.apply_update = real_apply
            update_dialog.find_installed = real_find
        close_top(win, dialog)

        win._set_busy(installed, True)
        try:
            alert = win.request_uninstall(installed.id)
            capture(win, "45-uninstall-busy.png")
            close_top(win, alert)
        finally:
            win._set_busy(installed, False)

        from fakearchive import fake_elf, fake_script, make_zip

        driver = make_zip(downloads / "cnijfilter2-6.60-1-deb.zip", {
            "cnijfilter2-6.60-1-deb/install.sh": fake_script(),
            "cnijfilter2-6.60-1-deb/packages/cnijfilter2_6.60-1_amd64.deb": b"!<arch>\n"},
            modes={"cnijfilter2-6.60-1-deb/install.sh": 0o755})
        dialog = open_dialog(win, driver, install_dialog.PAGE_ERROR)
        capture(win, "46-install-archive-installer.png")
        close_top(win, dialog)
        wait_until(lambda: win.current_dialog is None, what="installer dialog cleanup")
        appimage = bytearray(fake_elf(size=4096))
        appimage[8:11] = b"AI\x02"
        zipped = make_zip(downloads / "winboat-linux.zip",
                          {"winboat-0.9.0-x86_64.AppImage": bytes(appimage)},
                          modes={"winboat-0.9.0-x86_64.AppImage": 0o755})
        dialog = open_dialog(win, zipped, install_dialog.PAGE_ERROR)
        capture(win, "47-install-archive-appimage.png")
        close_top(win, dialog)
        wait_until(lambda: win.current_dialog is None, what="appimage zip dialog cleanup")

    def close_top(win: MainWindow, dialog: Adw.Dialog) -> None:
        dialogs = win.get_dialogs()
        deadline = time.monotonic() + 30
        while dialog in [dialogs.get_item(i) for i in range(dialogs.get_n_items())]:
            if time.monotonic() > deadline:
                raise TimeoutError("dialog did not close")
            dialog.close()
            settle(win, 1)

    app = EasyInstallerApp(flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.idle_add(scenario)
    app.run([sys.argv[0]])
    return int(state["rc"])


def _default_language() -> str:
    """``LANGUAGE=de tools/gui_screenshots.py`` renders German screenshots, like ``--language de``."""
    return os.environ.get("LANGUAGE", "").split(":")[0].strip() or "en"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory for the PNGs")
    parser.add_argument("--backend", choices=("auto", "broadway", "x11"), default="broadway",
                        help="display backend (default: broadway, headless; x11 opens windows "
                             "on the screen; auto: Broadway, or X11 if gtk4-broadwayd cannot "
                             "be started)")
    parser.add_argument("--language", default=_default_language(),
                        help="UI language, e.g. de or nl (default: the first entry of $LANGUAGE, "
                             "else en; uses build/locale, run `make mo` first)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary HOME (for debugging)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child_main(Path(args.out))
    return parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
