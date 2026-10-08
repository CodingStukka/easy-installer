"""GUI smoke tests.

The display tests run the real application in a subprocess on a private ``gtk4-broadwayd`` display
(headless), inside the isolated HOME/XDG environment of conftest.py (plus a private system root and
no network), and drive it programmatically:

* main window with installed apps, install dialog (confirm → install → done, error page,
  authentication cancelled, system scope without pkexec), uninstall, --uninstall of an unknown
  id, system check and About dialogs;
* 0.2: remembered updates and the quiet automatic check at start, "Check for Updates", compact
  rows, Update All through the update dialog, details (Check Now, going back, deleting the kept
  version), uninstalling with the settings folder, preferences, reconcile on focus and a changed
  app for everyone that needs a Repair.

They are skipped when Broadway or PyGObject is unavailable.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fakeappimage import requires_mksquashfs, requires_unsquashfs

REPO = Path(__file__).resolve().parents[1]

gi = pytest.importorskip("gi")


# ------------------------------------------------------------------------------------------------
# display-free parts
# ------------------------------------------------------------------------------------------------


def _run_main_loop(until, timeout: float = 5.0) -> None:
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "main loop timed out"
        context.iteration(False) or time.sleep(0.005)


def test_run_in_thread_delivers_result_on_main_thread():
    from easy_installer.gui.async_utils import run_in_thread

    got = {}

    def callback(result, error):
        got.update(result=result, error=error, thread=threading.current_thread())

    run_in_thread(lambda a, b=0: a + b, callback, 40, b=2)
    _run_main_loop(lambda: "result" in got)
    assert got["result"] == 42
    assert got["error"] is None
    assert got["thread"] is threading.main_thread()


def test_run_in_thread_delivers_errors():
    from easy_installer.errors import InstallError
    from easy_installer.gui.async_utils import format_exception, run_in_thread

    got = {}

    def fail():
        raise InstallError("Disk full", details="ENOSPC")

    run_in_thread(fail, lambda result, error: got.update(result=result, error=error))
    _run_main_loop(lambda: "error" in got)
    assert got["result"] is None
    assert isinstance(got["error"], InstallError)
    assert "fail" in format_exception(got["error"])


def test_main_loop_progress_coalesces_and_closes():
    from easy_installer.gui.async_utils import main_loop_progress

    seen = []
    progress = main_loop_progress(lambda fraction, message: seen.append((fraction, message)))
    worker = threading.Thread(target=lambda: [progress(i / 100, f"step {i}") for i in range(101)])
    worker.start()
    worker.join()
    _run_main_loop(lambda: bool(seen) and seen[-1][1] == "step 100")
    assert 1 <= len(seen) <= 101
    assert seen[-1] == (1.0, "step 100")

    progress.close()
    progress(0.5, "late")
    _run_main_loop(lambda: True)
    assert seen[-1] == (1.0, "step 100")


def test_settings_round_trip_and_corrupt_file(isolated_env):
    from easy_installer.core.paths import settings_path
    from easy_installer.gui import settings

    assert settings.get_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED, False) is False
    settings.set_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED, True)
    assert settings.get_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED) is True
    path = settings_path()
    assert path.is_file() and str(path).startswith(str(isolated_env))

    path.write_text("{not json")
    assert settings.load_settings() == {}


# ------------------------------------------------------------------------------------------------
# the real window on a headless Broadway display
# ------------------------------------------------------------------------------------------------

CHILD = textwrap.dedent(r'''
    import os, sys, time, traceback
    from pathlib import Path

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("SKIP", exc)
        sys.exit(77)

    from fakeappimage import make_sample_appimage
    from easy_installer.core import updates as core_updates
    from easy_installer.errors import NetworkError

    # Never the network: the window checks the FreeCAD sample's update source at start.
    network_calls = []
    def offline(url, **kwargs):
        network_calls.append(url)
        raise NetworkError("There is no internet connection right now.")
    core_updates.http_get = offline

    from easy_installer.core.inspector import inspect_appimage
    from easy_installer.core.installer import (
        InstallOptions, execute_install, find_installed, plan_install)
    from easy_installer.core.paths import Scope
    from easy_installer.core.registry import InstalledApp
    from easy_installer.errors import AuthorizationError
    from easy_installer.gui import install_dialog
    from easy_installer.gui.app_row import AppRow
    from easy_installer.gui.application import EasyInstallerApp
    from easy_installer.gui.window import MainWindow

    home = Path(os.environ["HOME"])
    downloads = home / "Downloads"
    downloads.mkdir()

    # A private system root: apps installed for everyone on this machine (/var/lib/easy-installer)
    # must neither show up in the window nor be touched.
    from easy_installer.core import installer as core_installer
    from easy_installer.core.paths import system_layout
    _layout_for = core_installer.layout_for
    core_installer.layout_for = lambda scope: (system_layout(home / "sysroot")
                                               if Scope(scope) is Scope.SYSTEM
                                               else _layout_for(scope))

    # Two installed apps: one fine, one whose AppImage disappeared.
    for name, kind in (("T3-Code-0.0.42-x86_64.AppImage", "t3code"),
                       ("FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "freecad")):
        info = inspect_appimage(make_sample_appimage(home / "seed" / name, kind))
        try:
            execute_install(plan_install(info, InstallOptions()))
        finally:
            info.cleanup()
    freecad = find_installed("org.freecad.FreeCAD")[0]
    Path(freecad.appimage_path).unlink()

    # Our own launcher must be findable for the "open AppImages with…" banner; GLib also wants
    # its program on PATH (a stub that is never run).
    import shutil
    apps_dir = Path(os.environ["XDG_DATA_HOME"]) / "applications"
    shutil.copy(Path(os.environ["REPO"]) / "data" / "com.roothirsch.EasyInstaller.desktop", apps_dir)
    stub_dir = home / "bin"
    stub_dir.mkdir()
    (stub_dir / "easy-installer").write_text("#!/bin/sh\nexit 0\n")
    (stub_dir / "easy-installer").chmod(0o755)
    os.environ["PATH"] = str(stub_dir) + os.pathsep + os.environ["PATH"]

    openscad = make_sample_appimage(downloads / "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad")
    not_an_appimage = downloads / "notes.txt"
    not_an_appimage.write_text("hello")

    context = GLib.MainContext.default()
    result = {"rc": 1}

    def wait_until(predicate, what, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for " + what)
            if not context.iteration(False):
                time.sleep(0.005)

    def open_dialog(win, path, page):
        win.install_files([path])
        wait_until(lambda: win.current_dialog is not None, "dialog")
        dialog = win.current_dialog
        wait_until(lambda: dialog.page == page, "page " + page)
        return dialog

    def frames(win, n=1):
        clock = win.get_frame_clock()
        for _ in range(n):
            start = clock.get_frame_counter()
            win.queue_draw()
            wait_until(lambda: clock.get_frame_counter() > start, "a frame", timeout=10)

    def close_dialog(win, dialog, force=False):
        # libadwaita 1.5 drops close requests that arrive before a freshly presented dialog was
        # painted; Broadway without a browser paints only about once a second -> retry.
        deadline = time.monotonic() + 30
        while dialog in [win.get_dialogs().get_item(i)
                         for i in range(win.get_dialogs().get_n_items())]:
            assert time.monotonic() < deadline, "dialog did not close"
            dialog.force_close() if force else dialog.close()
            frames(win)

    def scenario():
        wait_until(lambda: any(isinstance(w, MainWindow) for w in app.get_windows()), "window")
        win = next(w for w in app.get_windows() if isinstance(w, MainWindow))
        # Broadway without a browser paints about once a second: skip the dialog animations.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

        # command line: `easy-installer --uninstall does-not-exist notes.txt`
        wait_until(lambda: win.current_dialog is not None
                   and win.current_dialog.page == "error", "dialog for the file argument")
        dialogs = [win.get_dialogs().get_item(i) for i in range(win.get_dialogs().get_n_items())]
        alerts = [d for d in dialogs if isinstance(d, Adw.AlertDialog)]
        assert [a.get_heading() for a in alerts] == ["Nothing to uninstall"], dialogs
        close_dialog(win, alerts[0])
        close_dialog(win, win.current_dialog)
        wait_until(lambda: win.current_dialog is None, "command line dialogs")

        from easy_installer.gui.application import split_verbosity
        assert split_verbosity(["ei", "-vv", "a.AppImage", "--", "-v"]) == (
            ["ei", "a.AppImage", "--", "-v"], 2)

        # list with an ok row and a "missing" row
        assert [r.app.id for r in win.rows] == ["org.freecad.FreeCAD", "t3code"], win.rows
        assert [r.status for r in win.rows] == ["missing-appimage", "ok"]

        # banner: make Easy Installer the default for AppImages
        assert win._handler_banner.get_revealed()
        win._on_make_default(win._handler_banner)
        default = Gio.AppInfo.get_default_for_type("application/vnd.appimage", False)
        assert default is not None and default.get_id() == "com.roothirsch.EasyInstaller.desktop"
        assert not win._handler_banner.get_revealed()
        win._handler_banner.set_revealed(True)
        win._on_dismiss_handler_banner(None)
        from easy_installer.gui import settings
        assert settings.get_setting(settings.DEFAULT_HANDLER_OFFER_DISMISSED) is True
        assert not win._handler_banner.get_revealed()

        # FUSE banner: only with apt; installing fails here (pkexec disabled) -> friendly alert
        import dataclasses
        from easy_installer.core.system_checks import get_system_status
        real = get_system_status()
        win.status = dataclasses.replace(real, libfuse2=False, has_apt=False)
        win._update_banners()
        assert win._fuse_banner.get_revealed() and not win._fuse_banner.get_button_label()
        win.status = dataclasses.replace(real, libfuse2=False, has_apt=True)
        win._update_banners()
        assert win._fuse_banner.get_button_label()
        win.install_fuse()
        wait_until(lambda: isinstance(win.get_visible_dialog(), Adw.AlertDialog), "fuse alert")
        close_dialog(win, win.get_visible_dialog())
        win.status = real
        win._update_banners()
        assert win._fuse_banner.get_revealed() == (not real.libfuse2)

        # standalone rows for every status
        for app_obj in (freecad, find_installed("t3code")[0]):
            AppRow(app_obj)
        broken = find_installed("t3code")[0]
        Path(broken.desktop_path).rename(broken.desktop_path + ".bak")
        assert AppRow(broken).status == "missing-launcher"
        Path(broken.desktop_path + ".bak").rename(broken.desktop_path)

        # install dialog: confirm page, scope switching, install, done
        dialog = open_dialog(win, openscad, "confirm")
        work_dir = dialog.info.work_dir
        assert dialog.plan.action == "install"
        dialog.set_scope(Scope.SYSTEM)
        assert dialog.plan.layout.scope is Scope.SYSTEM
        dialog.set_scope(Scope.USER)
        assert dialog.plan.layout.scope is Scope.USER
        dialog.expand_options(True)
        dialog.install()
        wait_until(lambda: dialog.page in ("done", "error"), "installation")
        assert dialog.page == "done", dialog._error_page.get_description()
        assert dialog.installed_app.id == "openscad"
        close_dialog(win, dialog)
        wait_until(lambda: win.current_dialog is None, "dialog cleanup")
        assert not work_dir.exists(), "work dir was not cleaned up"
        assert [r.app.id for r in win.rows] == ["org.freecad.FreeCAD", "openscad", "t3code"]

        # same file again (now inside ~/Applications): reinstall, in place
        installed = find_installed("openscad")[0]
        dialog = open_dialog(win, installed.appimage_path, "confirm")
        assert dialog.plan.action == "reinstall" and dialog.plan.in_place
        # authentication cancelled -> back to the confirmation page
        real_execute = install_dialog.execute_install
        def cancelled(plan, progress=None):
            raise AuthorizationError("Authentication was cancelled.")
        install_dialog.execute_install = cancelled
        try:
            dialog.install()
            wait_until(lambda: dialog.page == "confirm" and not dialog.busy, "auth cancel")
        finally:
            install_dialog.execute_install = real_execute
        # everyone on this computer: pkexec is disabled in tests -> friendly error + Back
        dialog.set_scope(Scope.SYSTEM)
        dialog.install()
        wait_until(lambda: dialog.page == "error", "helper error")
        assert dialog._back_button.get_visible()
        dialog._on_back_clicked()
        assert dialog.page == "confirm"
        close_dialog(win, dialog)
        wait_until(lambda: win.current_dialog is None, "dialog cleanup")

        # not an AppImage -> error page; two files queue one after the other
        win.install_files([not_an_appimage, openscad])
        wait_until(lambda: win.current_dialog is not None, "queued dialog")
        dialog = win.current_dialog
        wait_until(lambda: dialog.page == "error", "error page")
        assert len(win.queued_files) == 1
        close_dialog(win, dialog)
        wait_until(lambda: win.current_dialog is not None and win.current_dialog is not dialog,
                   "next queued dialog")
        close_dialog(win, win.current_dialog)
        wait_until(lambda: win.current_dialog is None, "queue done")

        # uninstall with confirmation
        target = find_installed("openscad")[0]
        alert = win.confirm_uninstall(target)
        win._on_uninstall_response(alert, "uninstall", target)
        close_dialog(win, alert, force=True)
        wait_until(lambda: not find_installed("openscad") and not win.is_busy, "uninstall")
        assert [r.app.id for r in win.rows] == ["org.freecad.FreeCAD", "t3code"]

        # Repair: the launcher of T3 Code disappeared (e.g. Linux Mint's own "Uninstall" deleted
        # it): its row and, once per session, a toast also offer to uninstall the rest
        offered = []
        real_toast = win.add_toast
        win.add_toast = lambda text, **kw: (offered.append((text, kw.get("button_label")))
                                            or real_toast(text, **kw))
        t3 = find_installed("t3code")[0]
        Path(t3.desktop_path).unlink()
        win.reload()
        row = next(r for r in win.rows if r.app.id == "t3code")
        assert row.status == "missing-launcher"
        assert row._uninstall_button is not None
        assert row._uninstall_button.get_label() == "Uninstall…"
        assert row._uninstall_button.get_action_name() == "row.uninstall"
        assert offered == [("T3 Code (Alpha) is no longer in the app menu", "Uninstall…")], offered
        win.reload(force=True)
        assert len(offered) == 1, offered
        win.add_toast = real_toast
        win.repair(row.app)
        wait_until(lambda: not win.is_busy, "repair")
        assert Path(t3.desktop_path).is_file()
        assert next(r for r in win.rows if r.app.id == "t3code").status == "ok"

        # Remove: FreeCAD's AppImage is gone
        freecad_app = find_installed("org.freecad.FreeCAD")[0]
        alert = win.confirm_uninstall(freecad_app)
        assert alert.get_heading() == "Remove FreeCAD?"
        win._on_uninstall_response(alert, "uninstall", freecad_app)
        close_dialog(win, alert, force=True)
        wait_until(lambda: not find_installed("org.freecad.FreeCAD") and not win.is_busy,
                   "remove")
        assert not Path(freecad_app.desktop_path).exists()

        # Ctrl+W closes the topmost dialog first
        win.show_about()
        wait_until(lambda: win.get_visible_dialog() is not None, "about")
        frames(win, 3)
        assert win.activate_action("win.close", None)
        frames(win)
        assert win.get_visible_dialog() is None, "Ctrl+W did not close the dialog"
        assert win.get_visible(), "Ctrl+W closed the window instead of the dialog"

        win.show_system_check()
        wait_until(lambda: win.get_visible_dialog() is not None, "system check")
        close_dialog(win, win.get_visible_dialog())

        win.set_drop_overlay_visible(True)
        win.set_drop_overlay_visible(False)

        # --- review findings -------------------------------------------------------------
        toasts = []
        real_add_toast = win.add_toast
        win.add_toast = lambda text, **kw: toasts.append(text) or real_add_toast(text, **kw)

        # Banners that wrap (narrow window) push the list down instead of covering it.
        win.set_visible(False)  # a new default size only applies to a window being mapped
        win.set_default_size(360, 640)
        win.present()
        win.status = dataclasses.replace(real, libfuse2=False, has_apt=True)
        win._update_banners()
        win._handler_banner.set_revealed(True)
        wait_until(lambda: 0 < win.get_width() <= 400, "narrow window")
        frames(win, 3)
        def bottom(widget):
            ok, rect = widget.compute_bounds(win)
            assert ok
            return rect.get_y() + rect.get_height()
        def top(widget):
            ok, rect = widget.compute_bounds(win)
            assert ok
            return rect.get_y()
        assert top(win._stack) >= bottom(win._fuse_banner) - 1, (
            top(win._stack), bottom(win._fuse_banner))
        assert bottom(win._handler_banner) <= top(win._fuse_banner) + 1
        win._handler_banner.set_revealed(False)
        win.status = real
        win._update_banners()

        # Normal uninstall says "uninstalled" (the file is gone afterwards in any case).
        info = inspect_appimage(make_sample_appimage(home / "seed" / "Other.AppImage", "openscad"))
        try:
            execute_install(plan_install(info, InstallOptions()))
        finally:
            info.cleanup()
        target = find_installed("openscad")[0]
        alert = win.confirm_uninstall(target)
        assert "password" not in alert.get_body()
        win._on_uninstall_response(alert, "uninstall", target)
        close_dialog(win, alert, force=True)
        wait_until(lambda: not find_installed("openscad") and not win.is_busy, "uninstall 2")
        assert "OpenSCAD was uninstalled" in toasts, toasts

        # A per-user app with an AppArmor profile asks for the password; a copy for everyone stays.
        from easy_installer.gui import uninstall_dialog
        t3_app = find_installed("t3code")[0]
        with_profile = dataclasses.replace(t3_app, apparmor_profile="/etc/apparmor.d/x")
        system_copy = dataclasses.replace(t3_app, scope=Scope.SYSTEM)
        real_others = uninstall_dialog.other_installations
        uninstall_dialog.other_installations = lambda app_obj: [system_copy]
        try:
            alert = win.confirm_uninstall(with_profile)
        finally:
            uninstall_dialog.other_installations = real_others
        body = alert.get_body()
        assert "asked for your password" in body and "That copy stays installed" in body, body
        close_dialog(win, alert, force=True)

        # Without the administrator's password (a standard user) the app can still be
        # uninstalled; notes are shown in full, not in a one-line toast.
        uninstall_calls = []
        note = ("The special permission that T3 Code (Alpha) needed was left on this computer, "
                "because only an administrator can remove it.")
        def fake_uninstall(app_id, scope, *, progress=None, keep_permission=False):
            uninstall_calls.append(keep_permission)
            if not keep_permission:
                raise AuthorizationError("Authentication was cancelled.")
            return [note]
        real_uninstall = uninstall_dialog.uninstall
        uninstall_dialog.uninstall = fake_uninstall
        try:
            win._on_uninstall_response(None, "uninstall", with_profile)
            wait_until(lambda: isinstance(win.get_visible_dialog(), Adw.AlertDialog)
                       and not win.is_busy, "offer to keep the permission")
            offer = win.get_visible_dialog()
            assert offer.get_heading() == "Uninstall T3 Code (Alpha) without the password?"
            assert "Authentication was cancelled." in offer.get_body()
            assert "stays on this computer" in offer.get_body()
            win._on_keep_permission_response(offer, "uninstall", with_profile, False)
            close_dialog(win, offer, force=True)
            wait_until(lambda: not win.is_busy and isinstance(win.get_visible_dialog(),
                                                              Adw.AlertDialog), "notes")
        finally:
            uninstall_dialog.uninstall = real_uninstall
        assert uninstall_calls == [False, True]
        notes_alert = win.get_visible_dialog()
        assert notes_alert.get_heading() == "T3 Code (Alpha) was uninstalled"
        assert notes_alert.get_body() == note
        close_dialog(win, notes_alert)

        # File names that are not UTF-8 (e.g. from an old Windows zip) are shown, not fatal.
        latin1 = make_sample_appimage(downloads / os.fsdecode(b"Caf\xe9-Tool.AppImage"), "openscad")
        dialog = open_dialog(win, latin1, "confirm")
        assert dialog._loading_file.get_label() == "Caf�-Tool.AppImage"
        close_dialog(win, dialog)
        wait_until(lambda: win.current_dialog is None, "dialog cleanup (latin-1)")
        latin1_text = downloads / os.fsdecode(b"Caf\xe9-notes.txt")
        latin1_text.write_text("hello")
        dialog = open_dialog(win, latin1_text, "error")
        assert dialog._error_file.get_label() == "File: Caf�-notes.txt"
        close_dialog(win, dialog)
        wait_until(lambda: win.current_dialog is None, "dialog cleanup (latin-1 error)")

        # The install dialog never gets wider than the window; toasts stay above its buttons.
        long_name = downloads / "Kdenlive Video Editor Nightly Preview Build With A Long Name.AppImage"
        make_sample_appimage(long_name, "openscad")
        dialog = open_dialog(win, long_name, "confirm")
        real_execute = install_dialog.execute_install
        def cancelled(plan, progress=None):
            raise AuthorizationError("Authentication was cancelled.")
        install_dialog.execute_install = cancelled
        try:
            dialog.info.display_name = "Kdenlive Video Editor Nightly Preview Build " * 3
            dialog.install()
            wait_until(lambda: dialog.page == "confirm" and not dialog.busy, "auth cancel 2")
        finally:
            install_dialog.execute_install = real_execute
        minimum, _natural, _b1, _b2 = dialog._stack.measure(Gtk.Orientation.HORIZONTAL, -1)
        assert minimum <= 360, minimum
        frames(win, 2)
        assert bottom(dialog._toasts) <= top(dialog._install_button) + 1

        # Files dropped while a dialog is open are acknowledged in that dialog.
        dialog_toasts = []
        dialog.add_toast = lambda text: dialog_toasts.append(text)
        win.install_files([openscad])
        assert dialog_toasts == ["“OpenSCAD-2026.03.28-x86_64.AppImage” will be installed after "
                                 "this app"], dialog_toasts

        # "Only for me" says when it needs the password (sandbox permission on Ubuntu 24.04+).
        from easy_installer.core.sandbox import SandboxFix
        dialog.plan = dataclasses.replace(dialog.plan, requires_root=True,
                                          sandbox_fix=SandboxFix.APPARMOR)
        dialog._refresh_confirm()
        assert "password" in dialog._user_row.get_subtitle()

        # An alert on top of the install dialog: a close request (Alt+F4) closes only the alert,
        # so the files waiting behind the install dialog must stay queued.
        assert len(win.queued_files) == 1
        alert = win.request_uninstall("does-not-exist")
        wait_until(lambda: win.get_visible_dialog() is alert, "alert over the install dialog")
        deadline = time.monotonic() + 30
        while alert in [win.get_dialogs().get_item(i) for i in range(win.get_dialogs().get_n_items())]:
            assert time.monotonic() < deadline, "alert did not close"
            win.close()
            frames(win)
        assert win.current_dialog is dialog and len(win.queued_files) == 1, win.queued_files

        # A close request (Alt+F4) closes the open dialog - and opens no queued file after it.
        assert len(win.queued_files) == 1
        win.close()
        wait_until(lambda: win.current_dialog is None, "dialog closed by the close request")
        frames(win, 2)
        assert win.current_dialog is None and win.queued_files == [] and win.get_visible()

        # Quit with more files waiting: the app quits, the next file is not opened.
        win.install_files([openscad, not_an_appimage])
        wait_until(lambda: win.current_dialog is not None, "dialog before quitting")
        assert len(win.queued_files) == 1
        opened = []
        real_show_next = win._show_next_dialog
        win._show_next_dialog = lambda: opened.append(True) or real_show_next()
        app.activate_action("quit", None)
        wait_until(lambda: not any(isinstance(w, MainWindow) for w in app.get_windows()), "quit")
        frames_done = time.monotonic() + 1
        wait_until(lambda: time.monotonic() > frames_done, "idle")
        assert opened == [] and win.queued_files == []
        result["rc"] = 0

    def run():
        try:
            scenario()
        except BaseException:
            traceback.print_exc()
        finally:
            app.quit()
        return False

    app = EasyInstallerApp(flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.idle_add(run)
    app.run([sys.argv[0], "--uninstall", "does-not-exist", str(not_an_appimage)])
    sys.exit(result["rc"])
''')



# The 0.2 main window: updates (automatic check at start, "Check for Updates", Update All through
# the update dialog), details (Check Now, going back, deleting the kept version), uninstalling
# with the app's settings and data, preferences, reconcile when the window gets the focus, and a
# changed system-wide app that needs a Repair. No network, no real system registry.
CHILD_V02 = textwrap.dedent(r"""
    import dataclasses, os, shutil, sys, time, traceback
    from pathlib import Path

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("SKIP", exc)
        sys.exit(77)

    from fakeappimage import T3_DESKTOP, make_fake_appimage, make_sample_appimage, sample_files
    from fakearchive import electron_tree, make_zip
    from easy_installer.core import installer as core_installer, updates as core_updates
    from easy_installer.core.paths import Scope, system_layout
    from easy_installer.errors import NetworkError

    home = Path(os.environ["HOME"])
    sysroot = home / "sysroot"
    _layout_for = core_installer.layout_for
    core_installer.layout_for = lambda scope: (system_layout(sysroot)
                                               if Scope(scope) is Scope.SYSTEM
                                               else _layout_for(scope))
    network_calls = []
    def offline(url, **kwargs):
        network_calls.append(url)
        raise NetworkError("There is no internet connection right now.")
    core_updates.http_get = offline

    from easy_installer.core.inspector import inspect_appimage
    from easy_installer.core.installer import (
        InstallOptions, execute_install, find_installed, plan_install)
    from easy_installer.core.portable import inspect_portable
    from easy_installer.core.registry import InstalledApp, Registry
    from easy_installer.core.settings import load_settings
    from easy_installer.core.updater import UpdateCheckResult
    from easy_installer.core.updates import AvailableUpdate, UpdateCache
    from easy_installer.gui import details_dialog, update_dialog
    from easy_installer.gui import window as window_module
    from easy_installer.gui.application import EasyInstallerApp
    from easy_installer.gui.window import MainWindow

    seed = home / "seed"
    seed.mkdir()

    def install(path, options=None):
        info = inspect_portable(path) if path.suffix == ".zip" else inspect_appimage(path)
        try:
            return execute_install(plan_install(info, options or InstallOptions()))
        finally:
            info.cleanup()

    def freecad_file(version):
        files, symlinks, kwargs = sample_files("freecad")
        files = {**files, "usr/share/freecad/VERSION": version}
        return make_fake_appimage(seed / f"FreeCAD_{version}-Linux-x86_64-py311.AppImage",
                                  files, symlinks, **kwargs)

    def t3_file(version, folder=seed):
        files, symlinks, kwargs = sample_files("t3code")
        files = {**files, "t3code.desktop": T3_DESKTOP.replace("0.0.42", version)}
        return make_fake_appimage(folder / f"T3-Code-{version}-x86_64.AppImage", files,
                                  symlinks, **kwargs)

    install(freecad_file("1.1.2"))
    install(freecad_file("1.1.3"))          # keeps 1.1.2 as the previous version
    t3 = install(t3_file("0.0.42"))
    registry = Registry(core_installer.layout_for(Scope.USER).registry_path)
    registry.put(dataclasses.replace(t3, update_source={
        "kind": "electron-github", "owner": "pingdotgg", "repo": "t3code"}))
    tree = electron_tree("balenaEtcher-linux-x64", program="balena-etcher")
    install(make_zip(seed / "balenaEtcher-linux-x64-2.1.4.zip", tree["files"], tree["symlinks"],
                     tree["modes"]))
    config = home / ".config" / "FreeCAD"
    config.mkdir(parents=True)
    (config / "user.cfg").write_text("settings")

    # An app for everyone whose file changed since (as if it had updated itself).
    system = system_layout(sysroot)
    system.apps_dir.mkdir(parents=True)
    system.desktop_dir.mkdir(parents=True)
    scad = make_sample_appimage(system.apps_dir / "OpenSCAD.AppImage", "openscad")
    (system.desktop_dir / "easyinstaller-openscad.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=OpenSCAD\nExec=" + str(scad) + "\n")
    Registry(system.registry_path).put(InstalledApp(
        id="openscad", name="OpenSCAD", version="2021.01", scope=Scope.SYSTEM,
        appimage_path=str(scad), desktop_path=str(system.desktop_dir /
                                                   "easyinstaller-openscad.desktop"),
        size=1, mtime_ns=1))

    # An update of T3 Code found by an earlier check (so no check of T3 Code is due).
    t3_update = AvailableUpdate(
        version="0.0.44", url="https://github.com/pingdotgg/t3code/releases/download/v0.0.44/"
        "T3-Code-0.0.44-x86_64.AppImage", filename="T3-Code-0.0.44-x86_64.AppImage", size=1000)
    freecad_update = AvailableUpdate(
        version="1.1.4", url="https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/"
        "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage",
        filename="FreeCAD_1.1.4-Linux-x86_64-py311.AppImage", size=2000)
    UpdateCache().put("user", "t3code", t3_update)

    toasts = []
    real_add_toast = MainWindow.add_toast
    def recording_add_toast(self, text, **kwargs):
        toasts.append(text)
        return real_add_toast(self, text, **kwargs)
    MainWindow.add_toast = recording_add_toast

    context = GLib.MainContext.default()
    result = {"rc": 1}

    def wait_until(predicate, what, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for " + what)
            if not context.iteration(False):
                time.sleep(0.005)

    def frames(win, n=1):
        clock = win.get_frame_clock()
        for _ in range(n):
            start = clock.get_frame_counter()
            win.queue_draw()
            wait_until(lambda: clock.get_frame_counter() > start, "a frame", timeout=10)

    def open_dialogs(win):
        return [win.get_dialogs().get_item(i) for i in range(win.get_dialogs().get_n_items())]

    def close_dialog(win, dialog, force=False):
        deadline = time.monotonic() + 30
        while dialog in open_dialogs(win):
            assert time.monotonic() < deadline, "dialog did not close"
            dialog.force_close() if force else dialog.close()
            frames(win)

    def row(win, app_id):
        return next(r for r in win.rows if r.app.id == app_id)

    def scenario():
        wait_until(lambda: any(isinstance(w, MainWindow) for w in app.get_windows()), "window")
        win = next(w for w in app.get_windows() if isinstance(w, MainWindow))
        gtk_settings = Gtk.Settings.get_default()
        gtk_settings.set_property("gtk-enable-animations", False)
        if gtk_settings.get_property("gtk-xft-dpi") <= 0:
            # Broadway reports no DPI: "sp" sizes (the window's breakpoint) would be 0 px.
            gtk_settings.set_property("gtk-xft-dpi", 96 * 1024)

        # --- start: reconcile, remembered updates, automatic check (quietly offline) ---------
        wait_until(lambda: not win.maintenance_running and not win.checking, "start-up checks")
        ids = [r.app.id for r in win.rows]
        assert ids == ["balenaetcher", "org.freecad.FreeCAD", "openscad", "t3code"], ids
        assert network_calls and all("FreeCAD" in url for url in network_calls), network_calls
        assert not any("internet" in t or "could not" in t for t in toasts), toasts
        assert row(win, "t3code").update == t3_update
        assert row(win, "org.freecad.FreeCAD").update is None
        assert not win.updates_banner_revealed          # one update: its row is enough
        etcher = row(win, "balenaetcher")
        assert etcher.app.kind == "portable" and etcher.update is None
        assert etcher._tag.get_visible()
        # the system-wide app changed: a Repair is offered
        assert "OpenSCAD changed since it was installed" in toasts, toasts
        assert row(win, "openscad").needs_repair and row(win, "openscad").can_repair
        win._reconcile_changed_apps()      # only OpenSCAD differs, and it waits for a Repair
        assert not win.reconciling
        # a row that is busy stays busy when the list is rebuilt
        busy_app = row(win, "t3code").app
        win._set_busy(busy_app, True)
        win.reload(force=True)
        assert row(win, "t3code").busy and win.is_busy
        win._set_busy(busy_app, False)
        assert not row(win, "t3code").busy and not win.is_busy
        # the keyboard focus goes back to the app's row when its details close
        dialog = win.show_details(row(win, "t3code").app)
        wait_until(lambda: dialog.locations is not None, "details")
        win.reload(force=True)             # e.g. something changed meanwhile
        close_dialog(win, dialog)
        wait_until(lambda: win.get_focus() is row(win, "t3code"), "focus back on the row")

        # --- "Check for Updates" -------------------------------------------------------------
        found = {("user", "t3code"): t3_update, ("user", "org.freecad.FreeCAD"): freecad_update}
        window_module.check_all_updates = lambda apps=None, *, force=False, progress=None, \
            fetch=None: UpdateCheckResult(updates=dict(found), errors={}, checked=2)
        win.activate_action("win.check-updates", None)
        wait_until(lambda: not win.checking and "2 updates are available" in toasts, "check")
        assert win.updates_banner_revealed
        assert row(win, "org.freecad.FreeCAD").update == freecad_update
        # offline: the reason is said, what was known stays
        window_module.check_all_updates = real_check_all_updates
        win.check_for_updates()
        wait_until(lambda: not win.checking, "offline check")
        assert toasts[-1] == "There is no internet connection right now.", toasts
        assert win.updates_banner_revealed

        # --- narrow window: compact rows -----------------------------------------------------
        win.set_visible(False)
        win.set_default_size(360, 640)
        win.present()
        wait_until(lambda: win.compact, "narrow window")
        t3_row = row(win, "t3code")
        assert t3_row.compact and not t3_row._main_button.get_visible()
        assert t3_row._update_button.get_icon_name() == "software-update-available-symbolic"
        assert t3_row._update_button.get_tooltip_text() == "Update T3 Code (Alpha) to version 0.0.44"
        assert not row(win, "balenaetcher")._tag.get_visible()
        assert row(win, "balenaetcher").get_subtitle().startswith("Portable app · ")
        # a long translation of "Install App…" would be cut off: only the icon is shown
        assert win._install_button.get_icon_name() == "list-add-symbolic"
        win.set_visible(False)
        win.set_default_size(760, 640)
        win.present()
        wait_until(lambda: not win.compact, "wide window")
        assert row(win, "t3code")._update_button.get_label() == "Update to 0.0.44"
        assert win._install_button.get_child() is win._install_content

        # --- Update All: one update dialog after the other -----------------------------------
        applied = []
        def fake_apply(app_obj, update, *, progress=None, cancel=None,
                       allow_signer_change=False, downloader=None, warnings=None):
            applied.append(app_obj.id)
            progress(0.5, "Downloading…")
            return dataclasses.replace(app_obj, version=update.version)
        update_dialog.apply_update = fake_apply
        win.update_all()
        for expected in ("org.freecad.FreeCAD", "t3code"):
            wait_until(lambda: isinstance(win.get_visible_dialog(), update_dialog.UpdateDialog)
                       and win.get_visible_dialog().page == update_dialog.PAGE_DONE,
                       "update of " + expected)
            dialog = win.get_visible_dialog()
            assert dialog.app.id == expected
            assert not win.updates_banner_revealed      # hidden while the queue runs
            close_dialog(win, dialog)
        wait_until(lambda: not win.queued_updates and win.get_visible_dialog() is None,
                   "update queue")
        assert applied == ["org.freecad.FreeCAD", "t3code"], applied
        assert all(r.update is None for r in win.rows)
        assert not win.updates_banner_revealed

        # --- details: data size, Check Now, go back, delete the kept version ------------------
        freecad = row(win, "org.freecad.FreeCAD").app
        dialog = win.show_details(freecad)
        wait_until(lambda: dialog.locations is not None, "data size")
        assert [loc.path for loc in dialog.locations] == [config]
        win.remember_update(freecad, freecad_update)
        dialog.refresh(freecad)
        assert dialog.update_button.get_visible()
        details_dialog.check_app_update = lambda app_obj, force=False, fetch=None: None
        checked = []
        dialog.connect("checked", lambda *_a: checked.append(True))
        dialog.check_now()
        wait_until(lambda: checked, "check now")
        assert win.update_for(freecad) is None       # the window learned it, too
        assert dialog._check_state == ("current", None)

        dialog._on_go_back_response(None, "go-back")
        wait_until(lambda: not win.is_busy and dialog.app.version == "1.1.2", "going back")
        assert find_installed("org.freecad.FreeCAD")[0].previous["version"] == "1.1.3"
        assert "FreeCAD is back at version 1.1.2" in toasts, toasts
        dialog._on_delete_backup_response(None, "delete")
        wait_until(lambda: not win.is_busy and dialog.app.previous is None, "deleting")
        assert details_dialog.usable_previous(dialog.app) is None
        assert "The previous version of FreeCAD was deleted" in toasts, toasts
        close_dialog(win, dialog)

        dialog = win.show_details(row(win, "balenaetcher").app)
        wait_until(lambda: dialog.locations is not None, "portable details")
        assert not hasattr(dialog, "check_button")
        close_dialog(win, dialog)

        # --- uninstall with the settings folder ticked ---------------------------------------
        freecad = row(win, "org.freecad.FreeCAD").app
        alert = win.confirm_uninstall(freecad)
        wait_until(lambda: alert.data_ready, "data folders")
        assert [loc.path for loc in alert.locations] == [config]
        assert alert.selected_data == []                 # nothing is ticked beforehand
        alert.set_data_selected(config)
        win._on_uninstall_response(alert, "uninstall", freecad)
        close_dialog(win, alert, force=True)
        wait_until(lambda: not win.is_busy and not find_installed("org.freecad.FreeCAD"),
                   "uninstall")
        assert not config.exists()
        trash = Path(os.environ["XDG_DATA_HOME"]) / "Trash" / "files" / "FreeCAD" / "user.cfg"
        assert trash.read_text() == "settings"
        assert "FreeCAD was uninstalled, its settings and data are in the trash" in toasts

        # --- preferences ----------------------------------------------------------------------
        prefs = win.show_preferences()
        prefs.check_row.set_active(False)
        prefs.backup_row.set_selected(0)
        assert load_settings().check_updates is False and load_settings().backup_days == 0
        close_dialog(win, prefs)

        # --- T3 Code replaced its own file: noticed when the window gets the focus -----------
        t3_now = row(win, "t3code").app
        newer = t3_file("0.0.44", folder=home)
        time.sleep(0.01)
        shutil.copyfile(newer, t3_now.appimage_path)
        win._reconcile_changed_apps()
        wait_until(lambda: not win.reconciling and row(win, "t3code").app.version == "0.0.44",
                   "reconcile")
        assert "T3 Code (Alpha) was updated to 0.0.44" in toasts, toasts
        result["rc"] = 0

    def run():
        try:
            scenario()
        except BaseException:
            traceback.print_exc()
        finally:
            app.quit()
        return False

    real_check_all_updates = window_module.check_all_updates
    app = EasyInstallerApp(flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.idle_add(run)
    app.run([sys.argv[0]])
    sys.exit(result["rc"])
""")

# Review findings of the main window (GUI-1, GUI-2, GUI-4 to GUI-8): stale updates are never
# installed, Update All goes on after a failure, Enter keeps working on rows with many apps, no
# uninstall while an app is busy, the focus stays with its row, the banner's access key does
# nothing behind a dialog, and the automatic check obeys the preferences of the moment.
CHILD_REVIEW = textwrap.dedent(r"""
    import dataclasses, os, sys, threading, time, traceback
    from pathlib import Path

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("SKIP", exc)
        sys.exit(77)

    from easy_installer.core import installer as core_installer, updates as core_updates
    from easy_installer.core.paths import Scope, system_layout, user_layout
    from easy_installer.errors import InstallError, NetworkError, UpdateCancelled, UpdateError

    home = Path(os.environ["HOME"])
    sysroot = home / "sysroot"
    _layout_for = core_installer.layout_for
    core_installer.layout_for = lambda scope: (system_layout(sysroot)
                                               if Scope(scope) is Scope.SYSTEM
                                               else _layout_for(scope))
    def offline(url, **kwargs):
        raise NetworkError("There is no internet connection right now.")
    core_updates.http_get = offline

    from easy_installer.core.registry import InstalledApp, Registry
    from easy_installer.core.updater import UpdateCheckResult, UpdateNotNeededError
    from easy_installer.core.updates import AvailableUpdate, UpdateCache
    from easy_installer.gui import update_dialog
    from easy_installer.gui import window as window_module
    from easy_installer.gui.application import EasyInstallerApp
    from easy_installer.gui.details_dialog import DetailsDialog
    from easy_installer.gui.window import MainWindow, StartupReport

    layout = user_layout()
    layout.apps_dir.mkdir(parents=True)
    layout.desktop_dir.mkdir(parents=True)
    registry = Registry(layout.registry_path)
    SOURCE = {"kind": "github-assets", "owner": "demo-org", "repo": "demo", "release": "latest",
              "pattern": "Demo-*-x86_64.AppImage", "url": None, "prerelease": False,
              "via": "upd_info"}

    def fake_app(app_id, name, version):
        # (registry entries with plain files: nothing here is ever started or inspected)
        path = layout.apps_dir / f"{name}.AppImage"
        path.write_bytes(b"not started " + app_id.encode())
        desktop = layout.desktop_dir / f"easyinstaller-{app_id}.desktop"
        desktop.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={path}\n")
        st = path.stat()
        app = InstalledApp(id=app_id, name=name, version=version, scope=Scope.USER,
                           appimage_path=str(path), desktop_path=str(desktop), size=st.st_size,
                           mtime_ns=st.st_mtime_ns, sha256=(app_id * 64)[:64],
                           update_source=dict(SOURCE))
        registry.put(app)
        return app

    for app_id, name, version in (("alpha", "Alpha", "1.0"), ("bravo", "Bravo", "1.0"),
                                  ("charlie", "Charlie", "1.0"), ("delta", "Delta", "1.0"),
                                  ("echo", "Echo", "1.0"), ("freecad", "FreeCAD", "1.1.3"),
                                  ("t3code", "T3 Code", "0.0.42")):
        fake_app(app_id, name, version)
    t3_update = AvailableUpdate(version="0.0.44", url="https://example.org/T3-0.0.44.AppImage",
                                filename="T3-0.0.44.AppImage", size=1000)
    fc_update = AvailableUpdate(version="1.1.4", url="https://example.org/FC-1.1.4.AppImage",
                                filename="FC-1.1.4.AppImage", size=2000)
    cache = UpdateCache()
    cache.put("user", "t3code", t3_update)
    cache.put("user", "freecad", fc_update)

    toasts = []
    real_add_toast = MainWindow.add_toast
    def recording_add_toast(self, text, **kwargs):
        toasts.append(text)
        return real_add_toast(self, text, **kwargs)
    MainWindow.add_toast = recording_add_toast

    context = GLib.MainContext.default()
    result = {"rc": 1}

    def wait_until(predicate, what, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for " + what)
            if not context.iteration(False):
                time.sleep(0.005)

    def frames(win, n=1):
        clock = win.get_frame_clock()
        for _ in range(n):
            start = clock.get_frame_counter()
            win.queue_draw()
            wait_until(lambda: clock.get_frame_counter() > start, "a frame", timeout=10)

    def open_dialogs(win):
        return [win.get_dialogs().get_item(i) for i in range(win.get_dialogs().get_n_items())]

    def close_dialog(win, dialog, force=False):
        deadline = time.monotonic() + 30
        while dialog in open_dialogs(win):
            assert time.monotonic() < deadline, "dialog did not close"
            dialog.force_close() if force else dialog.close()
            frames(win)

    def row(win, app_id):
        return next(r for r in win.rows if r.app.id == app_id)

    def update_dialog_on(win, page):
        dialog = win.get_visible_dialog()
        return dialog if isinstance(dialog, update_dialog.UpdateDialog) \
            and dialog.page == page else None

    applied = []
    plan = {}          # app id -> what the fake update does
    def fake_apply(app_obj, update, *, progress=None, cancel=None, allow_signer_change=False,
                   downloader=None, warnings=None):
        applied.append((app_obj.id, app_obj.version, update.version))
        progress(0.3, "Downloading…")
        action = plan.get(app_obj.id)
        if callable(action):
            return action(app_obj, update, cancel)
        if isinstance(action, BaseException):
            raise action
        return dataclasses.replace(app_obj, version=update.version)
    update_dialog.apply_update = fake_apply

    def scenario():
        wait_until(lambda: any(isinstance(w, MainWindow) for w in app.get_windows()), "window")
        win = next(w for w in app.get_windows() if isinstance(w, MainWindow))
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        wait_until(lambda: not win.maintenance_running and not win.checking, "start-up checks")
        assert [r.app.id for r in win.rows] == ["alpha", "bravo", "charlie", "delta", "echo",
                                                "freecad", "t3code"]
        assert row(win, "t3code").update == t3_update and win.updates_banner_revealed

        # --- GUI-2: with more than 6 apps, Enter and Space stay with the focused row --------
        assert win._search.get_visible()
        assert win._search.get_key_capture_widget() is None
        charlie = row(win, "charlie")
        charlie.grab_focus()
        assert win.get_focus() is charlie
        none = Gdk.ModifierType(0)
        for key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_space,
                    Gdk.KEY_Tab, Gdk.KEY_Down, Gdk.KEY_Menu, Gdk.KEY_Escape):
            assert not win.starts_search(key, none), key
        assert not win.starts_search(Gdk.KEY_r, Gdk.ModifierType.CONTROL_MASK)
        assert win.starts_search(Gdk.KEY_z, none) and win.starts_search(Gdk.KEY_adiaeresis, none)
        forwarded = []
        class Controller:
            def forward(self, widget):
                forwarded.append(widget)
                return True
        assert not win._on_type_to_search(Controller(), Gdk.KEY_Return, 0, none)
        assert forwarded == [] and win.get_focus() is charlie
        assert win._on_type_to_search(Controller(), Gdk.KEY_z, 0, none)
        assert forwarded == [win._search.get_delegate()]
        assert win.get_focus() is win._search.get_delegate()
        assert not win.starts_search(Gdk.KEY_y, none)       # typing goes on in the entry itself
        # activating the focused row opens its details (what Enter does)
        charlie.grab_focus()
        win.activate_focus() if hasattr(win, "activate_focus") else win.emit("activate-focus")
        wait_until(lambda: isinstance(win.get_visible_dialog(), DetailsDialog), "details")
        details = win.get_visible_dialog()
        assert details.app.id == "charlie"

        # --- GUI-7: the banner's access key does nothing behind the details -----------------
        win._on_update_all_clicked(win._updates_banner)
        assert not win.queued_updates and win.get_visible_dialog() is details
        assert not any(isinstance(d, update_dialog.UpdateDialog) for d in open_dialogs(win))
        win._on_make_default(win._handler_banner)
        assert "AppImages now open with Easy Installer" not in toasts
        close_dialog(win, details)
        wait_until(lambda: win.get_visible_dialog() is None, "details closed")

        # --- GUI-5: a background result that changes nothing keeps rows and focus -----------
        charlie = row(win, "charlie")
        charlie.grab_focus()
        win._on_updates_checked(UpdateCheckResult(
            updates={("user", "t3code"): t3_update, ("user", "freecad"): fc_update},
            errors={}, checked=7), None)
        assert row(win, "charlie") is charlie and win.get_focus() is charlie
        win._on_apps_reconciled([], None)
        assert row(win, "charlie") is charlie and win.get_focus() is charlie
        # a rebuild (something changed) gives the focus to the same app's new row
        win.reload(force=True)
        assert row(win, "charlie") is not charlie and win.get_focus() is row(win, "charlie")

        # --- GUI-8: the automatic check follows the preferences of the moment ---------------
        checks = []
        real_check = window_module.check_all_updates
        window_module.check_all_updates = lambda apps=None, *, force=False, progress=None, \
            fetch=None: checks.append(force) or UpdateCheckResult(updates={}, errors={},
                                                                   checked=0)
        win._auto_check = False                 # switched off while the start-up checks ran
        win._on_maintenance_done(StartupReport(check_due=True), None)
        assert not win.checking and checks == []
        win._auto_check = True                  # switched on meanwhile
        real_due = window_module.updates_due
        window_module.updates_due = lambda apps, hours, cache=None: True
        win._on_maintenance_done(StartupReport(check_due=False), None)
        wait_until(lambda: not win.checking and checks == [False], "quiet check")
        window_module.updates_due = real_due
        window_module.check_all_updates = real_check
        win.remember_update(row(win, "t3code").app, t3_update)
        win.remember_update(row(win, "freecad").app, fc_update)
        assert win.updates_banner_revealed

        # --- GUI-6: one update that fails does not stop Update All ---------------------------
        plan["freecad"] = UpdateError("The downloaded file is not a working app. Nothing was "
                                      "changed.")
        win._on_update_all_clicked(win._updates_banner)
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_ERROR) is not None,
                   "FreeCAD fails")
        dialog = win.get_visible_dialog()
        assert dialog.app.id == "freecad" and dialog._retry_button.get_visible()
        close_dialog(win, dialog)
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_DONE) is not None,
                   "T3 Code is updated after all")
        assert win.get_visible_dialog().app.id == "t3code"
        close_dialog(win, win.get_visible_dialog())
        wait_until(lambda: not win.is_busy and win.get_visible_dialog() is None, "queue done")
        assert [a[0] for a in applied] == ["freecad", "t3code"], applied
        assert row(win, "freecad").update == fc_update and row(win, "t3code").update is None
        win.remember_update(row(win, "t3code").app, t3_update)   # (the fake changed nothing)
        assert win.updates_banner_revealed

        # ... but Cancel stops it
        applied.clear()
        started = threading.Event()
        def wait_for_cancel(app_obj, update, cancel):
            started.set()
            cancel.wait(20)
            raise UpdateCancelled("The download was cancelled.")
        plan["freecad"] = wait_for_cancel
        win.update_all()
        wait_until(lambda: started.is_set() and update_dialog_on(
            win, update_dialog.PAGE_DOWNLOAD) is not None, "FreeCAD downloads")
        dialog = win.get_visible_dialog()
        dialog.cancel_update()
        wait_until(lambda: dialog not in open_dialogs(win) and not win.queued_updates
                   and "t3code" not in [d.app.id for d in open_dialogs(win)
                                        if isinstance(d, update_dialog.UpdateDialog)],
                   "cancelled")
        frames(win, 2)
        assert [a[0] for a in applied] == ["freecad"] and win.updates_banner_revealed

        # --- GUI-1: an update found earlier is never installed over a newer version ----------
        applied.clear()
        release = threading.Event()
        def hold(app_obj, update, cancel):
            release.wait(20)
            return dataclasses.replace(app_obj, version=update.version)
        plan["freecad"] = hold
        win.update_all()
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_DOWNLOAD) is not None,
                   "FreeCAD downloads again")
        assert [(a.id, u.version) for a, u in win.queued_updates] == [("t3code", "0.0.44")]
        # meanwhile T3 Code updates itself to 0.0.45 (and the window learns it on focus)
        t3 = registry.get("t3code")
        registry.put(dataclasses.replace(t3, version="0.0.45", sha256="5" * 64))
        win.reload()
        assert row(win, "t3code").update is None
        # --- GUI-4: "Uninstall…" from FreeCAD's menu entry while it is being updated ---------
        alert = win.request_uninstall("freecad")
        assert alert.get_heading() == "FreeCAD cannot be uninstalled right now", alert.get_heading()
        close_dialog(win, alert, force=True)
        win._on_uninstall_response(None, "uninstall", row(win, "freecad").app)
        assert registry.get("freecad") is not None
        assert "Please wait until Easy Installer has finished with FreeCAD" in toasts, toasts
        release.set()
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_DONE) is not None, "FreeCAD")
        close_dialog(win, win.get_visible_dialog())
        wait_until(lambda: not win.queued_updates and not win.is_busy, "queue done again")
        frames(win, 2)
        assert [a[0] for a in applied] == ["freecad"], applied     # T3 Code was not touched
        assert "T3 Code is already up to date" in toasts, toasts
        assert not any(isinstance(d, update_dialog.UpdateDialog) for d in open_dialogs(win))
        # the details' Update button of a stale update does the same
        assert not win.start_update(row(win, "t3code").app, t3_update)
        assert [a[0] for a in applied] == ["freecad"]

        # --- the core finds out itself (the file changed, nobody looked yet) ----------------
        plan["freecad"] = UpdateNotNeededError(
            "FreeCAD is now at version 1.1.5, so this update is no longer needed.")
        assert win.start_update(row(win, "freecad").app, fc_update, autostart=True)
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_DONE) is not None,
                   "not needed")
        dialog = win.get_visible_dialog()
        assert dialog._done_page.get_title() == "FreeCAD is up to date"
        assert "no longer needed" in dialog._done_page.get_description()
        assert not dialog._open_button.get_visible() and dialog.new_app is None
        close_dialog(win, dialog)
        wait_until(lambda: not win.is_busy, "not needed closed")

        # --- GUI-4: the app was uninstalled while its update downloaded ----------------------
        def gone(app_obj, update, cancel):
            registry.remove("freecad")
            raise InstallError("Another installation changed the installed apps in the "
                               "meantime. Please try again.")
        plan["freecad"] = gone
        assert win.start_update(row(win, "freecad").app, fc_update, autostart=True)
        wait_until(lambda: update_dialog_on(win, update_dialog.PAGE_ERROR) is not None, "gone")
        dialog = win.get_visible_dialog()
        assert dialog._error_page.get_description() == \
            "FreeCAD is no longer installed, so it was not updated."
        assert not dialog._retry_button.get_visible() and not dialog._error_note.get_visible()
        close_dialog(win, dialog)
        result["rc"] = 0

    def run():
        try:
            scenario()
        except BaseException:
            traceback.print_exc()
        finally:
            app.quit()
        return False

    app = EasyInstallerApp(flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.idle_add(run)
    app.run([sys.argv[0]])
    sys.exit(result["rc"])
""")

def _free_display(runtime: Path) -> int | None:
    for display in range(30, 90):
        if (runtime / f"broadway{display + 1}.socket").exists():
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", 8080 + display))
            except OSError:
                continue
        return display
    return None


@pytest.fixture
def broadway(tmp_path):
    """A private gtk4-broadwayd; yields the environment for GTK clients."""
    tool = shutil.which("gtk4-broadwayd")
    if tool is None:
        pytest.skip("gtk4-broadwayd is not installed")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GDK_", "GTK_", "BROADWAY_", "DBUS_", "WAYLAND_"))}
    env.update(XDG_RUNTIME_DIR=str(runtime), GSETTINGS_BACKEND="memory", GIO_USE_VFS="local",
               GDK_DEBUG="no-portals", ADW_DISABLE_PORTAL="1", NO_AT_BRIDGE="1", GTK_A11Y="none",
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("DISPLAY", None)
    display = _free_display(runtime)
    if display is None:
        pytest.skip("no free Broadway display")
    proc = subprocess.Popen([tool, f":{display}"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not (runtime / f"broadway{display + 1}.socket").exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.skip("gtk4-broadwayd could not be started")
            time.sleep(0.05)
        yield {**env, "GDK_BACKEND": "broadway", "BROADWAY_DISPLAY": f":{display}"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.gui
@requires_mksquashfs
@requires_unsquashfs
def test_window_and_install_dialog(broadway, tmp_path):
    env = {**broadway, "REPO": str(REPO),
           "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tests")])}
    script = tmp_path / "gui_smoke_child.py"
    script.write_text(CHILD)
    cmd = [sys.executable, str(script)]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--", *cmd]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180,
                          cwd=tmp_path, check=False)
    if proc.returncode == 77:
        pytest.skip(proc.stdout.strip())
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@pytest.mark.gui
@requires_mksquashfs
@requires_unsquashfs
def test_updates_details_and_uninstall_with_data(broadway, tmp_path):
    env = {**broadway, "REPO": str(REPO),
           "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tests")])}
    script = tmp_path / "gui_smoke_v02_child.py"
    script.write_text(CHILD_V02)
    cmd = [sys.executable, str(script)]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--", *cmd]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240,
                          cwd=tmp_path, check=False)
    if proc.returncode == 77:
        pytest.skip(proc.stdout.strip())
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


@pytest.mark.gui
def test_review_findings_of_the_main_window(broadway, tmp_path):
    env = {**broadway, "REPO": str(REPO),
           "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tests")])}
    script = tmp_path / "gui_review_child.py"
    script.write_text(CHILD_REVIEW)
    cmd = [sys.executable, str(script)]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--", *cmd]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240,
                          cwd=tmp_path, check=False)
    if proc.returncode == 77:
        pytest.skip(proc.stdout.strip())
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
