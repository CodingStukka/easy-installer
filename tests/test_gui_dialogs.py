"""The v0.2 install dialog (replace or keep both, origin/signature/update-source lines, portable
archives with a program chooser) and the update dialog.

The first part needs no display: the texts, the choices and the worker functions. The second
part runs both dialogs on a private headless ``gtk4-broadwayd`` display in a subprocess (inside
the isolated HOME/XDG environment of conftest.py) and drives them: it is skipped when Broadway
or PyGObject is unavailable. Updates never touch the network: ``apply_update`` gets a local
"downloader" or is replaced by stand-ins.
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
import types
from pathlib import Path

import pytest

from fakeappimage import requires_mksquashfs, requires_unsquashfs

REPO = Path(__file__).resolve().parents[1]

pytest.importorskip("gi")

from easy_installer.core import installer  # noqa: E402
from easy_installer.core.portable import ExecutableCandidate  # noqa: E402
from easy_installer.core.registry import InstalledApp  # noqa: E402
from easy_installer.core.signature import SignatureInfo  # noqa: E402
from easy_installer.core.updater import SignerChangedError  # noqa: E402
from easy_installer.core.updates import UpdateSource  # noqa: E402
from easy_installer.errors import (  # noqa: E402
    AuthorizationError,
    HelperError,
    NetworkError,
    NotInstalledError,
    UpdateError,
)

dialog_common = pytest.importorskip("easy_installer.gui.dialog_common")
install_dialog = pytest.importorskip("easy_installer.gui.install_dialog")
update_dialog = pytest.importorskip("easy_installer.gui.update_dialog")

FINGERPRINT = "3A1F9C0D5E7B2468ACE013579BDF2468ACE01357"


@pytest.fixture(autouse=True)
def private_system_layout(system_root, monkeypatch):
    """The "for everyone" places live in a temporary root: what is installed system-wide on this
    computer (the real /var/lib/easy-installer registry) must not change what the tests see."""
    from easy_installer.core import installer
    from easy_installer.core.paths import Scope, system_layout, user_layout

    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout
GITHUB = UpdateSource(kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest",
                      pattern="FreeCAD*x86_64*.AppImage", via="upd_info")


# ------------------------------------------------------------------------------------------------
# texts
# ------------------------------------------------------------------------------------------------


def test_origin_text_names_only_the_site():
    assert dialog_common.origin_text(None) is None
    assert dialog_common.origin_text("ftp://example.org/app.AppImage") is None
    assert dialog_common.origin_text(
        "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/FreeCAD.AppImage"
    ) == "Downloaded from github.com"


@pytest.mark.parametrize("value", [
    None, {"status": "unverified", "fingerprint": FINGERPRINT, "signer": "Maker"},
    SignatureInfo("invalid", FINGERPRINT, "Maker", "changed"), {"status": "bogus"}, "valid",
])
def test_only_a_valid_signature_is_shown(value):
    assert dialog_common.signature_text(value) is None


def test_signature_text_and_fingerprint():
    valid = SignatureInfo("valid", FINGERPRINT, "FreeCAD Team", None)
    # SEC-3: the key travels inside the file - its name is only the key's name ...
    assert dialog_common.signature_text(valid) == "Signed with a key named “FreeCAD Team”"
    assert dialog_common.signature_text(valid.to_dict()) == \
        "Signed with a key named “FreeCAD Team”"
    assert dialog_common.signature_text(SignatureInfo("valid", FINGERPRINT, None, None)) \
        == "Signed with a key that has no name"
    # ... only the installed version's key tells who made it
    installed = types.SimpleNamespace(signature=valid.to_dict())
    assert dialog_common.signature_text(valid, installed=installed) == \
        "Signed by the same maker as the installed version"
    other = types.SimpleNamespace(signature=SignatureInfo("valid", "F" * 40, "X", None).to_dict())
    assert dialog_common.signature_text(valid, installed=other) == \
        "Signed with a key named “FreeCAD Team”"
    unverified = types.SimpleNamespace(signature={"status": "unverified",
                                                  "fingerprint": FINGERPRINT})
    assert dialog_common.signature_text(valid, installed=unverified) == \
        "Signed with a key named “FreeCAD Team”"
    assert dialog_common.fingerprint_text(valid) == (
        "Key fingerprint: 3A1F 9C0D 5E7B 2468 ACE0 1357 9BDF 2468 ACE0 1357")
    assert dialog_common.fingerprint_text(None) is None
    assert dialog_common.signature_is_broken(SignatureInfo("invalid", None, None, None))
    assert not dialog_common.signature_is_broken(valid)
    assert not dialog_common.signature_is_broken(None)


def test_update_source_text():
    assert dialog_common.update_source_text(GITHUB) == \
        "Gets updates from github.com/FreeCAD/FreeCAD"
    assert dialog_common.update_source_text(GITHUB.to_dict()) == \
        "Gets updates from github.com/FreeCAD/FreeCAD"
    assert dialog_common.update_source_text(None) is None
    assert dialog_common.update_source_text({"kind": "bintray"}) is None


def test_file_facts():
    info = types.SimpleNamespace(
        origin_url="https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/F.AppImage",
        signature=SignatureInfo("valid", FINGERPRINT, "FreeCAD Team", None),
        update_source=GITHUB)
    facts = dialog_common.file_facts(info)
    assert [f.text for f in facts] == ["Downloaded from github.com",
                                       "Signed with a key named “FreeCAD Team”",
                                       "Gets updates from github.com/FreeCAD/FreeCAD"]
    assert [f.icon for f in facts] == [dialog_common.ICON_ORIGIN, dialog_common.ICON_SIGNED,
                                       dialog_common.ICON_UPDATES]
    assert facts[1].tooltip.startswith("Key fingerprint: 3A1F")
    # A kept copy never gets updates.
    pinned = dialog_common.file_facts(info, pinned=True)
    assert pinned[-1].text == "This copy stays at its version and gets no updates"
    # Unsigned, no origin, no source (e.g. a portable archive): nothing to say.
    assert dialog_common.file_facts(types.SimpleNamespace()) == []
    assert dialog_common.file_facts(types.SimpleNamespace(origin_url=None, signature=None,
                                                          update_source=None)) == []


def test_backup_note_and_version_texts():
    assert dialog_common.backup_note("1.1.3", 14) == \
        "Version 1.1.3 is kept for 14 days, so you can go back to it"
    assert dialog_common.backup_note("1.1.3", 1) == \
        "Version 1.1.3 is kept for 1 day, so you can go back to it"
    assert dialog_common.backup_note(None, 7) == \
        "The current version is kept for 7 days, so you can go back to it"
    assert dialog_common.version_change_text("1.1.3", "1.1.4") == "1.1.3 → 1.1.4"
    assert dialog_common.version_change_label("1.1.3", "1.1.4") == "From version 1.1.3 to 1.1.4"
    assert dialog_common.version_change_text(None, "1.1.4") == "Version 1.1.4"
    assert dialog_common.version_change_text("1.1.3", None) == "A newer version"


def portable(executables, chosen=None):
    candidates = [ExecutableCandidate(relpath, "elf", score) for relpath, score in executables]
    return types.SimpleNamespace(executables=candidates,
                                 executable=chosen or candidates[0].relpath)


def test_program_choices_only_when_several_are_likely():
    # UVtools: the vendor's start script and the program itself score alike.
    uvtools = portable([("UVtools.sh", 75), ("UVtools", 72), ("UVtoolsCmd", 42), ("createdump", -48)])
    assert dialog_common.program_choices(uvtools) == ["UVtools.sh", "UVtools"]
    # balenaEtcher: one clear winner, no question.
    etcher = portable([("balena-etcher", 72), ("resources/etcher-util", 2), ("chrome-sandbox", -48)])
    assert dialog_common.program_choices(etcher) == []
    # A program chosen earlier (the installed version's) is always offered, first.
    kept = portable([("UVtools.sh", 75), ("UVtools", 72), ("UVtoolsCmd", 42)], chosen="UVtoolsCmd")
    assert dialog_common.program_choices(kept) == ["UVtoolsCmd", "UVtools.sh", "UVtools"]
    many = portable([(f"tool{i}", 50) for i in range(20)])
    assert len(dialog_common.program_choices(many)) == dialog_common.MAX_PROGRAM_CHOICES
    assert dialog_common.program_choices(types.SimpleNamespace(executables=[], executable="x")) == []


def test_update_error_titles():
    describe = dialog_common.describe_update_error
    assert describe(NetworkError("offline")).title == "The update could not be downloaded"
    assert describe(UpdateError("bad file")).title == "The update could not be installed"
    assert describe(SignerChangedError("other maker")).title == "The update could not be installed"
    assert describe(HelperError("helper")).title == "The update could not be installed"
    assert describe(NotInstalledError("gone")).title == "The app is not installed"
    assert describe(AuthorizationError("no")).title == "Authentication failed"
    unexpected = describe(ValueError("boom"))
    assert unexpected.title == "Something went wrong" and "ValueError" in unexpected.details
    assert describe(NetworkError("offline", details="DNS")).details == "DNS"


def test_progress_share_and_download_text():
    share = dialog_common.progress_share
    assert share(None, 0.0, 0.7) is None
    assert share(0.35, 0.0, 0.7) == pytest.approx(0.5)
    assert share(0.9, 0.0, 0.7) == 1.0
    assert share(0.5, 0.7, 1.0) == 0.0
    assert share(0.85, 0.7, 1.0) == pytest.approx(0.5)
    assert share(0.5, 1.0, 1.0) == 1.0
    assert dialog_common.download_text(None, 1000) is None
    assert dialog_common.download_text(0.5, None) is None
    assert dialog_common.download_text(0.5, 0) is None
    from gi.repository import GLib

    # GLib formats the sizes for the locale ("1.0 MB", "1,0 MB")
    assert dialog_common.download_text(0.5, 2_000_000) == \
        f"{GLib.format_size(1_000_000)} of {GLib.format_size(2_000_000)}"
    assert dialog_common.download_text(1.7, 2_000_000).startswith(GLib.format_size(2_000_000))


# ------------------------------------------------------------------------------------------------
# the install dialog's decisions (no widgets)
# ------------------------------------------------------------------------------------------------


def plan(action="install", *, version="2.0", installed="1.0", keep_both=False,
         signer_changed=False, main_version=None):
    existing = types.SimpleNamespace(version=installed) if installed is not None else None
    main = types.SimpleNamespace(version=main_version or installed)
    return types.SimpleNamespace(action=action, version=version, existing=existing,
                                 options=types.SimpleNamespace(keep_both=keep_both),
                                 signer_changed=signer_changed, main_installed=main)


@pytest.mark.parametrize("kwargs, label", [
    (dict(action="install", installed=None), "_Install"),
    (dict(action="update"), "_Update"),
    (dict(action="reinstall", version="1.0"), "_Reinstall"),
    (dict(action="downgrade", version="0.9"), "_Go Back to 0.9"),
    (dict(action="update", keep_both=True), "_Install"),
])
def test_install_button_labels(kwargs, label):
    _text, _icon, button = install_dialog.InstallDialog._action_text(plan(**kwargs))
    assert install_dialog.InstallDialog._button_label(plan(**kwargs), button) == label


def test_install_button_says_anyway_when_the_signer_changed():
    dialog = install_dialog.InstallDialog
    update = plan("update", signer_changed=True)
    assert dialog._button_label(update, dialog._action_text(update)[2]) == "_Update Anyway"
    both = plan("install", keep_both=True, signer_changed=True)
    assert dialog._button_label(both, dialog._action_text(both)[2]) == "_Install Anyway"
    older = plan("downgrade", version="0.9", signer_changed=True)
    assert dialog._button_label(older, dialog._action_text(older)[2]) == "_Install Anyway"


def test_action_texts():
    dialog = install_dialog.InstallDialog
    assert dialog._action_text(plan("update"))[0] == \
        "Version 1.0 is installed and will be replaced by 2.0"
    assert dialog._action_text(plan("update", keep_both=True))[0] is None
    assert dialog._action_text(plan("install", installed=None))[0] is None
    assert "older version 0.9" in dialog._action_text(plan("downgrade", version="0.9"))[0]


@pytest.mark.parametrize("version, installed, title, subtitle", [
    ("2.0", "1.0", "Replace version 1.0", "This newer version takes its place"),
    ("0.9", "1.0", "Replace version 1.0", "This older version takes its place"),
    ("1.0", "1.0", "Replace version 1.0", "This version takes its place"),
    (None, "1.0", "Replace version 1.0", "This version takes its place"),
    ("2.0", None, "Replace the installed version", "This version takes its place"),
])
def test_replace_choice_texts(version, installed, title, subtitle):
    choice = plan("update", version=version, installed=installed, keep_both=True)
    choice.main_installed = types.SimpleNamespace(version=installed)
    assert install_dialog.InstallDialog._replace_texts(choice) == (title, subtitle)


def test_a_newer_archive_keeps_the_program_chosen_for_the_installed_version(monkeypatch, tmp_path):
    folder = tmp_path / "Applications" / "UVtools"
    installed = InstalledApp.from_dict({
        "id": "uvtools", "name": "UVtools", "version": "6.2.0", "scope": "user",
        "appimage_path": str(folder / "UVtools"), "desktop_path": "", "icon_paths": [],
        "icon_name": "", "kind": "portable", "install_dir": str(folder)})
    monkeypatch.setattr(installer, "find_installed", lambda app_id: [installed])
    info = types.SimpleNamespace(app_id="uvtools", executables=[
        ExecutableCandidate("UVtools.sh", "script", 75), ExecutableCandidate("UVtools", "elf", 72)])
    assert install_dialog.installed_program(info) == "UVtools"
    info.executables = [ExecutableCandidate("UVtools.sh", "script", 75)]
    assert install_dialog.installed_program(info) is None
    monkeypatch.setattr(installer, "find_installed", lambda app_id: [])
    assert install_dialog.installed_program(info) is None


def test_inspect_worker_reads_archives_as_portable_apps(tmp_path):
    from fakearchive import flat_tree, make_archive

    archive = make_archive(tmp_path / "UVtools_linux-x64_v6.2.0.zip", **flat_tree("UVtools"))
    seen = []
    info, status, plan_, backup_days = install_dialog.InstallDialog._inspect_worker(
        archive, threading.Event(), lambda fraction, message: seen.append(message))
    try:
        assert plan_.kind == "portable" and plan_.scope.value == "user"
        assert info.name == "UVtools" and info.version == "6.2.0"
        assert backup_days == 14
        assert dialog_common.program_choices(info) == ["UVtools.sh", "UVtools"]
        assert seen, "no progress while reading the archive"
    finally:
        info.cleanup()
    assert not info.work_dir.exists()


def test_inspect_worker_stops_when_the_dialog_was_closed(tmp_path):
    from fakearchive import flat_tree, make_archive

    archive = make_archive(tmp_path / "UVtools.zip", **flat_tree("UVtools"))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(install_dialog._Cancelled):
        install_dialog.InstallDialog._inspect_worker(archive, cancel, lambda *a: None)


def test_update_worker_passes_the_choices_to_apply_update(monkeypatch):
    calls = []
    monkeypatch.setattr(update_dialog, "apply_update",
                        lambda app, update, **kw: calls.append((app, update, kw)) or "new")
    progress, cancel = object(), threading.Event()
    assert update_dialog._run_update("app", "update", progress, cancel, True) == "new"
    assert calls == [("app", "update", {"progress": progress, "cancel": cancel,
                                        "allow_signer_change": True})]
    notes: list[str] = []
    assert update_dialog._run_update("app", "update", progress, cancel, False, notes) == "new"
    assert calls[-1] == ("app", "update", {"progress": progress, "cancel": cancel,
                                           "allow_signer_change": False, "warnings": notes})


# ------------------------------------------------------------------------------------------------
# both dialogs on a headless Broadway display
# ------------------------------------------------------------------------------------------------

CHILD = textwrap.dedent(r'''
    import dataclasses, functools, os, shutil, sys, threading, time, traceback
    from pathlib import Path

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("SKIP", exc)
        sys.exit(77)

    from fakeappimage import make_fake_appimage, make_png
    from fakearchive import flat_tree, make_archive
    from easy_installer.core import updater
    from easy_installer.core.inspector import inspect_appimage
    from easy_installer.core.installer import InstallOptions, execute_install, find_installed, plan_install
    from easy_installer.core.paths import Scope, user_layout
    from easy_installer.core.registry import Registry
    from easy_installer.core.signature import SignatureInfo
    from easy_installer.core.updates import AvailableUpdate
    from easy_installer.errors import (AuthorizationError, NetworkError, NotInstalledError,
                                       UpdateCancelled)
    from easy_installer.gui import install_dialog, update_dialog
    from easy_installer.gui.install_dialog import InstallDialog
    from easy_installer.gui.update_dialog import UpdateDialog

    home = Path(os.environ["HOME"])
    downloads = home / "Downloads"
    downloads.mkdir()
    seed = home / "seed"

    # The "for everyone" places live in a temporary root: apps installed system-wide on this
    # computer must not change what the dialogs are expected to show.
    from easy_installer.core import installer, paths
    installer.layout_for = lambda scope: (paths.system_layout(home / "sysroot")
                                          if Scope(scope) is Scope.SYSTEM else paths.user_layout())
    UPD = "gh-releases-zsync|demo-org|demo|latest|Demo-*-x86_64.AppImage.zsync"

    def demo(path, version, tag=""):
        desktop = ("[Desktop Entry]\nType=Application\nName=Demo\nExec=AppRun %U\nIcon=demo\n"
                   f"Categories=Utility;\nX-AppImage-Version={version}\n")
        return make_fake_appimage(path, {"demo.desktop": desktop, "AppRun": "#!/bin/sh\n",
                                         "payload.bin": f"Demo {version} {tag}".encode() * 3,
                                         "demo.png": make_png(32, 32)}, upd_info=UPD)

    def install(path):
        info = inspect_appimage(path)
        try:
            return execute_install(plan_install(info, InstallOptions(keep_original=True)))
        finally:
            info.cleanup()

    registry = Registry(user_layout().registry_path)
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

    def dialogs(win):
        return [win.get_dialogs().get_item(i) for i in range(win.get_dialogs().get_n_items())]

    def close_dialog(win, dialog):
        # libadwaita 1.5 drops close requests that arrive before a freshly presented dialog was
        # painted; Broadway without a browser paints only about once a second -> retry.
        deadline = time.monotonic() + 30
        while dialog in dialogs(win):
            assert time.monotonic() < deadline, "dialog did not close"
            dialog.force_close()
            frames(win)

    def open_install(win, path):
        dialog = InstallDialog(path)
        installed = []
        dialog.connect("installed", lambda d, app: installed.append(app))
        dialog.present(win)
        dialog.start()
        wait_until(lambda: dialog.page in ("confirm", "error"), "confirm page")
        assert dialog.page == "confirm", dialog._error_page.get_description()
        return dialog, installed

    def run_install(dialog):
        dialog.install()
        wait_until(lambda: dialog.page in ("done", "error"), "installation")
        assert dialog.page == "done", dialog._error_page.get_description()

    def install_dialog_scenarios(win):
        # --- replace or keep both ---------------------------------------------------------
        v1 = install(demo(seed / "Demo-1.0-x86_64.AppImage", "1.0"))
        v2 = demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0")
        try:
            os.setxattr(v2, "user.xdg.origin.url",
                        b"https://github.com/demo-org/demo/releases/download/v2.0/Demo.AppImage")
            origin = True
        except OSError:
            origin = False
        dialog, installed = open_install(win, v2)
        work_dir = dialog.info.work_dir
        assert dialog.plan.keep_both_available and not dialog.plan.options.keep_both
        assert dialog._choice_group.get_visible() and dialog._replace_check.get_active()
        assert dialog._replace_row.get_title() == "Replace version 1.0"
        assert dialog._replace_row.get_subtitle() == "This newer version takes its place"
        assert not dialog._action_group.get_visible()
        assert dialog._install_button.get_label() == "_Update"
        assert dialog.get_title() == "Update App"
        facts = dialog._facts.texts
        assert facts[0] == "Version 1.0 is kept for 14 days, so you can go back to it", facts
        assert "Gets updates from github.com/demo-org/demo" in facts, facts
        assert ("Downloaded from github.com" in facts) == origin, facts
        assert not dialog._trust_group.get_visible()

        dialog.set_keep_both(True)
        assert dialog.plan.options.keep_both and dialog.plan.backup_target is None
        assert dialog._install_button.get_label() == "_Install"
        assert dialog.get_title() == "Install App"
        facts = dialog._facts.texts
        assert not any("kept for" in f for f in facts), facts
        assert facts[-1] == "This copy stays at its version and gets no updates", facts
        dialog.set_keep_both(False)
        assert not dialog.plan.options.keep_both and dialog.plan.backup_target is not None
        dialog.set_keep_both(True)

        run_install(dialog)
        assert dialog._done_page.get_title() == "Demo 2.0 is installed"
        assert installed and installed[0].pinned and installed[0].base_id == "demo"
        ids = sorted(a.id for a in find_installed("demo") + find_installed(installed[0].id))
        assert ids == ["demo", installed[0].id], ids
        assert find_installed("demo")[0].version == "1.0"
        close_dialog(win, dialog)
        assert not work_dir.exists(), "the work dir was not removed"

        # --- another signer than the installed version's ----------------------------------
        signed = dataclasses.replace(registry.get("demo"), signature=SignatureInfo(
            "valid", "3A1F9C0D5E7B2468ACE013579BDF2468ACE01357", "Demo Maker", None).to_dict())
        registry.put(signed)
        v3 = demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0")
        dialog, _installed = open_install(win, v3)
        assert dialog.plan.signer_changed
        assert dialog._trust_group.get_visible() and len(dialog._trust_rows) == 1
        assert dialog._trust_rows[0].get_title() == dialog.plan.warnings[0]
        assert dialog._trust_rows[0].get_title() not in [r.get_title() for r in dialog._warning_rows]
        assert dialog._install_button.get_label() == "_Update Anyway"
        assert dialog._install_button.has_css_class("destructive-action")
        assert not dialog._install_button.has_css_class("suggested-action")
        close_dialog(win, dialog)

        # --- a portable archive: program chooser, only for me -----------------------------
        archive = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree("UVtools"))
        dialog, installed = open_install(win, archive)
        assert dialog.is_portable and dialog.plan.kind == "portable"
        assert dialog.program_choices == ["UVtools.sh", "UVtools"]
        assert dialog._program_group.get_visible()
        assert not dialog._system_row.get_sensitive()
        assert dialog._system_row.get_subtitle() == "Not possible for apps that come as an archive"
        assert dialog._keep_row.get_title() == "Keep the archive" and dialog._keep_row.get_active()
        assert not dialog._sandbox_row.get_visible()
        assert dialog._target_label.get_label().startswith("The app will be unpacked to ")
        assert dialog.plan.target_appimage.name == "UVtools.sh"
        dialog.set_program("UVtools")
        assert dialog.info.executable == "UVtools"
        assert dialog.plan.target_appimage.name == "UVtools"
        dialog.set_scope(Scope.SYSTEM)   # not possible: stays "only for me"
        assert dialog.plan.scope is Scope.USER and dialog._user_check.get_active()
        run_install(dialog)
        app = installed[0]
        assert app.kind == "portable" and app.appimage_path.endswith("/UVtools/UVtools")
        assert archive.is_file(), "the archive must be kept"
        close_dialog(win, dialog)

        # A newer archive keeps the program chosen for the installed version.
        tree = flat_tree("UVtools")
        tree["files"]["NEWS.txt"] = "6.3.0\n"
        tree["modes"]["NEWS.txt"] = 0o644
        newer = make_archive(downloads / "UVtools_linux-x64_v6.3.0.zip", **tree)
        dialog, _installed = open_install(win, newer)
        assert dialog.plan.action == "update" and not dialog.plan.keep_both_available
        assert dialog.info.executable == "UVtools"
        assert dialog._program_row.get_selected() == dialog.program_choices.index("UVtools")
        assert dialog._action_group.get_visible()
        assert dialog._facts.texts[0].startswith("Version 6.2.0 is kept for 14 days")
        close_dialog(win, dialog)

    def update_dialog_scenarios(win):
        old = install(demo(seed / "Demo-4.0-x86_64.AppImage", "4.0", "u"))
        old = find_installed("demo")[0]
        served = demo(home / "server" / "Demo-5.0-x86_64.AppImage", "5.0", "u")
        update = AvailableUpdate(
            version="5.0", url="https://github.com/demo-org/demo/releases/download/v5.0/"
                               "Demo-5.0-x86_64.AppImage",
            filename="Demo-5.0-x86_64.AppImage", size=served.stat().st_size,
            release_url="https://github.com/demo-org/demo/releases/tag/v5.0")
        real_apply = update_dialog.apply_update
        gate = threading.Event()
        downloads_seen = []

        def download(update, folder, *, progress=None, cancel=None):
            downloads_seen.append(Path(folder))
            progress(0.5, "")
            while not gate.is_set():
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled("The download was cancelled.")
                time.sleep(0.01)
            target = Path(folder) / update.filename
            shutil.copyfile(served, target)
            return target

        # --- confirm → download → install → done; on_done once, after closing --------------
        done = []
        dialog = UpdateDialog(win, old, update, on_done=done.append)
        dialog.present(win)   # the constructor presented it already: harmless
        assert dialogs(win).count(dialog) == 1
        assert dialog.page == "confirm" and not dialog.busy
        assert dialog._name_label.get_label() == "Demo"
        assert dialog._versions_label.get_label() == "4.0 → 5.0"
        facts = dialog._facts.texts
        assert facts[1:] == ["From github.com/demo-org/demo",
                             "Version 4.0 is kept for 14 days, so you can go back to it"], facts
        assert facts[0].endswith(" to download"), facts
        assert dialog._notes_button.get_visible()
        assert dialog._update_button.get_label() == "_Update"
        update_dialog.apply_update = functools.partial(updater.apply_update, downloader=download)
        try:
            dialog.start_update()
            assert dialog.busy and dialog.page == "download"
            wait_until(lambda: dialog._download_bar.get_fraction() >= 0.5, "download progress")
            assert " of " in dialog._download_label.get_label(), dialog._download_label.get_label()
            assert dialog._header.get_show_end_title_buttons()
            gate.set()
            wait_until(lambda: dialog.page in ("done", "error"), "update")
        finally:
            update_dialog.apply_update = real_apply
        assert dialog.page == "done", dialog._error_page.get_description()
        assert dialog.new_app.version == "5.0" and dialog.new_app.previous["version"] == "4.0"
        assert dialog._done_page.get_title() == "Demo was updated"
        assert not dialog._done_notes.get_visible()     # nothing came up while updating
        assert done == []
        close_dialog(win, dialog)
        wait_until(lambda: done, "on_done")
        frames(win, 2)
        assert len(done) == 1 and done[0].version == "5.0"
        assert not downloads_seen[-1].exists(), "download folder left behind"

        current = find_installed("demo")[0]
        newer = dataclasses.replace(update, version="6.0", release_url=None)

        # --- Cancel while downloading: the dialog closes itself, on_done(None) --------------
        gate.clear()
        done.clear()
        update_dialog.apply_update = functools.partial(updater.apply_update, downloader=download)
        try:
            dialog = UpdateDialog(win, current, newer, autostart=True, on_done=done.append)
            assert dialog.page == "download" and dialog.busy
            assert not dialog._notes_button.get_visible()
            wait_until(lambda: dialog._download_bar.get_fraction() >= 0.5, "download progress")
            dialog.close()   # Escape / Ctrl+W while downloading = Cancel
            assert dialog.cancelling and not dialog._cancel_button.get_sensitive()
            wait_until(lambda: done, "the cancelled dialog to close")
        finally:
            update_dialog.apply_update = real_apply
        assert done == [None] and dialog not in dialogs(win)
        assert find_installed("demo")[0].version == "5.0"
        assert not downloads_seen[-1].exists()

        # --- installing cannot be closed; a force-close waits for the result ----------------
        hold = threading.Event()
        calls = []

        def installing(app, update, *, progress=None, cancel=None, allow_signer_change=False,
                       warnings=None):
            calls.append(allow_signer_change)
            progress(0.9, "Copying the app…")
            hold.wait(30)
            return dataclasses.replace(app, version=update.version)

        done.clear()
        update_dialog.apply_update = installing
        try:
            dialog = UpdateDialog(win, current, newer, autostart=True, on_done=done.append)
            wait_until(lambda: dialog.page == "install", "install page")
            assert dialog._install_label.get_label() == "Copying the app…"
            assert not dialog._header.get_show_end_title_buttons()
            toasts = []
            dialog.add_toast = toasts.append
            frames(win, 2)
            dialog.close()
            assert toasts == ["Please wait until the update is finished"]
            assert dialog in dialogs(win)
            dialog.cancel_update()   # too late: ignored
            assert not dialog.cancelling
            dialog.force_close()     # e.g. the app quits
            wait_until(lambda: dialog not in dialogs(win), "force close")
            frames(win)
            assert done == [], "on_done before the installation ended"
            hold.set()
            wait_until(lambda: done, "on_done after the installation")
        finally:
            hold.set()
            update_dialog.apply_update = real_apply
        assert len(done) == 1 and done[0].version == "6.0"

        # --- notes that came up while updating are shown on the done page -------------------
        def with_note(app, update, *, progress=None, cancel=None, allow_signer_change=False,
                      warnings=None):
            warnings.append("The previous version could not be kept, so you cannot go back to it.")
            return dataclasses.replace(app, version=update.version)

        update_dialog.apply_update = with_note
        try:
            dialog = UpdateDialog(win, current, newer, autostart=True)
            wait_until(lambda: dialog.page == "done", "update with a note")
        finally:
            update_dialog.apply_update = real_apply
        assert dialog._done_notes.get_visible()
        assert [row.get_title() for row in dialog._done_note_rows] == [
            "The previous version could not be kept, so you cannot go back to it."]
        close_dialog(win, dialog)

        # --- password prompt refused → back to confirm with a toast -------------------------
        def refused(app, update, **kw):
            raise AuthorizationError("Authentication was cancelled.")

        done.clear()
        update_dialog.apply_update = refused
        try:
            dialog = UpdateDialog(win, current, newer, on_done=done.append)
            toasts = []
            dialog.add_toast = toasts.append
            dialog.start_update()
            wait_until(lambda: not dialog.busy, "refused password")
            assert dialog.page == "confirm" and toasts == ["Authentication was cancelled."]
        finally:
            update_dialog.apply_update = real_apply

        # --- another signer → confirm with a warning, then "Update Anyway" ------------------
        calls.clear()

        def resigned(app, update, *, progress=None, cancel=None, allow_signer_change=False,
                     warnings=None):
            calls.append(allow_signer_change)
            if not allow_signer_change:
                raise updater.SignerChangedError("other maker")
            raise NetworkError("The internet could not be reached.", details="offline")

        update_dialog.apply_update = resigned
        try:
            dialog.start_update()
            wait_until(lambda: not dialog.busy, "signer check")
            assert dialog.page == "confirm" and dialog._warnings_group.get_visible()
            assert dialog._warning_row.get_title().startswith("Careful: the installed version of Demo")
            assert dialog._update_button.get_label() == "_Update Anyway"
            assert dialog._update_button.has_css_class("destructive-action")
            dialog.start_update()
            wait_until(lambda: dialog.page == "error", "network error")
        finally:
            update_dialog.apply_update = real_apply
        assert calls == [False, True]
        assert dialog._error_page.get_title() == "The update could not be downloaded"
        assert dialog._error_note.get_label() == "Demo 5.0 is still installed and works as before."
        assert dialog._retry_button.get_visible() and dialog._details_expander.get_visible()
        dialog._on_retry_clicked()
        assert dialog.page == "confirm"
        assert done == []
        close_dialog(win, dialog)
        wait_until(lambda: done == [None], "on_done after closing")

        # --- the app was uninstalled meanwhile: no "Try Again" ------------------------------
        def gone(app, update, **kw):
            raise NotInstalledError("This app is not installed.")

        update_dialog.apply_update = gone
        try:
            dialog = UpdateDialog(win, current, newer, autostart=True)
            wait_until(lambda: dialog.page == "error", "not installed")
        finally:
            update_dialog.apply_update = real_apply
        assert not dialog._retry_button.get_visible() and not dialog._error_note.get_visible()
        close_dialog(win, dialog)

        # --- system-wide apps say that the password is needed -------------------------------
        dialog = UpdateDialog(None, dataclasses.replace(current, scope=Scope.SYSTEM), newer)
        assert dialog not in dialogs(win)   # without a window it is not presented
        assert dialog._facts.texts[-1].startswith("Asks for your password"), dialog._facts.texts
        dialog.present(win)
        wait_until(lambda: dialog in dialogs(win), "present")
        close_dialog(win, dialog)

    def scenario():
        win = Adw.ApplicationWindow(application=app, default_width=760, default_height=640)
        win.present()
        # Broadway without a browser paints about once a second: skip the dialog animations.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        wait_until(lambda: win.get_mapped(), "window")
        install_dialog_scenarios(win)
        update_dialog_scenarios(win)
        result["rc"] = 0

    def run():
        try:
            scenario()
        except BaseException:
            traceback.print_exc()
        finally:
            app.quit()
        return False

    app = Adw.Application(application_id="com.roothirsch.EasyInstaller.DialogTest",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", lambda *_a: GLib.idle_add(run))
    app.hold()
    app.run([sys.argv[0]])
    sys.exit(result["rc"])
''')


def _free_display(runtime: Path) -> int | None:
    for display in range(140, 200):
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
def broadway(tmp_path, isolated_env):
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
               PYTHONDONTWRITEBYTECODE="1",
               XDG_STATE_HOME=str(isolated_env / ".local" / "state"))
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
            proc.wait()


@pytest.mark.gui
@requires_mksquashfs
@requires_unsquashfs
def test_install_and_update_dialogs(broadway, tmp_path):
    env = {**broadway, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tests")])}
    script = tmp_path / "gui_dialogs_child.py"
    script.write_text(CHILD)
    cmd = [sys.executable, str(script)]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--", *cmd]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240,
                          cwd=tmp_path, check=False)
    if proc.returncode == 77:
        pytest.skip(proc.stdout.strip())
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
