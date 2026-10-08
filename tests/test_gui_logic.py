"""Window logic without a display: what runs in worker threads (start modes, repair, the checks
at start, reconcile, uninstalling with data) and the texts the dialogs show."""

from __future__ import annotations

import dataclasses
import os
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("gi")

from easy_installer.core import appdata, installer, updates  # noqa: E402
from easy_installer.core.appdata import DataLocation  # noqa: E402
from easy_installer.core.desktop_entry import DesktopEntry  # noqa: E402
from easy_installer.core.inspector import inspect_appimage  # noqa: E402
from easy_installer.core.installer import (  # noqa: E402
    InstallOptions,
    execute_install,
    find_installed,
    plan_install,
)
from easy_installer.core.paths import Scope, system_layout, user_layout  # noqa: E402
from easy_installer.core.portable import inspect_portable  # noqa: E402
from easy_installer.core.reconcile import CHANGE_ADOPTED, CHANGE_UPDATED, ReconcileResult  # noqa: E402
from easy_installer.core.registry import InstalledApp, Registry, make_previous  # noqa: E402
from easy_installer.core.settings import load_settings, save_settings  # noqa: E402
from easy_installer.core.system_checks import SystemStatus  # noqa: E402
from easy_installer.core.updater import UpdateCheckResult  # noqa: E402
from easy_installer.core.updates import AvailableUpdate, UpdateCache  # noqa: E402
from easy_installer.errors import (  # noqa: E402
    AuthorizationError,
    HelperError,
    NetworkError,
    UpdateError,
)

from fakeappimage import (  # noqa: E402
    T3_DESKTOP,
    make_fake_appimage,
    make_sample_appimage,
    requires_mksquashfs,
    requires_unsquashfs,
    sample_files,
)
from fakearchive import electron_tree, make_zip  # noqa: E402

window = pytest.importorskip("easy_installer.gui.window")
from easy_installer.gui import app_row, details_dialog, preferences_dialog  # noqa: E402
from easy_installer.gui import uninstall_dialog  # noqa: E402
from easy_installer.gui.common import format_size  # noqa: E402

pytestmark = [requires_mksquashfs, requires_unsquashfs]

FREECAD_SOURCE = {"kind": "github-assets", "owner": "FreeCAD", "repo": "FreeCAD",
                  "release": "latest", "pattern": "FreeCAD*x86_64*.AppImage", "url": None,
                  "prerelease": False, "via": "upd_info"}


def make_status(**overrides) -> SystemStatus:
    base = dict(
        unsquashfs=shutil.which("unsquashfs"), pkexec="/usr/bin/pkexec",
        apparmor_parser="/usr/sbin/apparmor_parser", update_desktop_database=None,
        icon_cache_tool=None, desktop_file_validate=None, libfuse2=True,
        fusermount="/usr/bin/fusermount3", dev_fuse=True, userns_restricted=False,
        apparmor_enabled=True, distro_id="zorin", distro_like=("ubuntu", "debian"),
        distro_version="18", has_apt=True, libfuse2_package="libfuse2t64",
    )
    base.update(overrides)
    return SystemStatus(**base)


@pytest.fixture(autouse=True)
def fuse_works(monkeypatch):
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    """No network, no real system registry (/var/lib/easy-installer), no `gio trash`."""
    def no_network(url, **_kwargs):
        raise AssertionError(f"the network was used: {url}")

    monkeypatch.setattr(updates, "http_get", no_network)
    real_layout_for = installer.layout_for
    monkeypatch.setattr(installer, "layout_for", lambda scope: (
        system_layout(tmp_path / "sysroot") if Scope(scope) is Scope.SYSTEM
        else real_layout_for(scope)))
    monkeypatch.setattr(appdata, "_gio_trash", lambda path: False)


def install_openscad(tmp_path: Path, options: InstallOptions) -> InstalledApp:
    src = make_sample_appimage(tmp_path / "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad")
    info = inspect_appimage(src)
    try:
        return execute_install(plan_install(info, options, status=make_status()))
    finally:
        info.cleanup()


def launcher_exec(app: InstalledApp) -> str:
    return DesktopEntry.parse(Path(app.desktop_path).read_text()).get("Exec") or ""


def test_an_explicit_extract_and_run_choice_is_kept(tmp_path):
    """`install --extract-and-run yes` although FUSE works: the window must not undo it."""
    app = install_openscad(tmp_path, InstallOptions(extract_and_run=True))
    assert app.extract_and_run and app.extract_and_run_explicit
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in launcher_exec(app)

    # what MainWindow._on_status_loaded runs on every start
    assert window.speed_up_apps(make_status(), system=False) == []
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in launcher_exec(app)
    stored = Registry(user_layout().registry_path).get("openscad")
    assert stored.extract_and_run and stored.extract_and_run_explicit

    # Repair keeps the choice, too
    repaired = window.repair_app(stored)
    assert repaired.extract_and_run and repaired.extract_and_run_explicit
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in launcher_exec(repaired)


def test_automatic_extract_and_run_is_switched_back(tmp_path):
    no_fuse = make_status(libfuse2=False, dev_fuse=False)
    src = make_sample_appimage(tmp_path / "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad")
    info = inspect_appimage(src)
    try:
        app = execute_install(plan_install(info, InstallOptions(), status=no_fuse))
    finally:
        info.cleanup()
    assert app.extract_and_run and not app.extract_and_run_explicit
    assert [a.id for a in window.speed_up_apps(make_status(), system=False)] == ["openscad"]
    assert "APPIMAGE_EXTRACT_AND_RUN" not in launcher_exec(find_installed("openscad")[0])


def test_system_apps_with_an_explicit_start_mode_are_not_repaired(monkeypatch, tmp_path):
    app = install_openscad(tmp_path, InstallOptions())
    system_apps = [dataclasses.replace(app, id=f"app{i}", scope=Scope.SYSTEM, extract_and_run=True,
                                       extract_and_run_explicit=explicit)
                   for i, explicit in enumerate((True, False))]
    repaired = []
    monkeypatch.setattr(window, "list_installed", lambda scopes=None: list(system_apps))
    monkeypatch.setattr(window, "update_start_modes", lambda status: [])
    monkeypatch.setattr(window, "repair_app", lambda a: repaired.append(a.id) or a)
    window.speed_up_apps(make_status(), system=True)
    assert repaired == ["app1"]


def test_a_cancelled_password_prompt_stops_speeding_up_system_apps(monkeypatch, tmp_path):
    """After installing FUSE each system app is repaired with the helper: one Cancel is enough."""
    app = install_openscad(tmp_path, InstallOptions())
    system_apps = [dataclasses.replace(app, id=f"app{i}", scope=Scope.SYSTEM, extract_and_run=True)
                   for i in range(3)]
    prompts = []

    def repair(a):
        prompts.append(a.id)
        raise AuthorizationError("Authentication was cancelled.")

    monkeypatch.setattr(window, "list_installed", lambda scopes=None: list(system_apps))
    monkeypatch.setattr(window, "update_start_modes", lambda status: [])
    monkeypatch.setattr(window, "repair_app", repair)
    assert window.speed_up_apps(make_status(), system=True) == []
    assert prompts == ["app0"]

    # other failures only concern that one app
    prompts.clear()

    def fail(a):
        prompts.append(a.id)
        raise HelperError("The administrator task failed unexpectedly.")

    monkeypatch.setattr(window, "repair_app", fail)
    window.speed_up_apps(make_status(), system=True)
    assert prompts == ["app0", "app1", "app2"]


# ------------------------------------------------------------------------------------------------
# helpers for the 0.2 tests
# ------------------------------------------------------------------------------------------------


def install_file(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    info = inspect_portable(path) if path.suffix == ".zip" else inspect_appimage(path)
    try:
        return execute_install(plan_install(info, options or InstallOptions(),
                                            status=make_status()))
    finally:
        info.cleanup()


def freecad_file(folder: Path, version: str) -> Path:
    """FreeCAD samples whose content differs per version (so an update keeps a backup)."""
    files, symlinks, kwargs = sample_files("freecad")
    files = {**files, "usr/share/freecad/VERSION": version}
    return make_fake_appimage(folder / f"FreeCAD_{version}-Linux-x86_64-py311.AppImage", files,
                              symlinks, **kwargs)


def t3_file(folder: Path, version: str, name: str | None = None) -> Path:
    files, symlinks, kwargs = sample_files("t3code")
    files = {**files, "t3code.desktop": T3_DESKTOP.replace("0.0.42", version)}
    return make_fake_appimage(folder / (name or f"T3-Code-{version}-x86_64.AppImage"), files,
                              symlinks, **kwargs)


def etcher_zip(folder: Path) -> Path:
    tree = electron_tree("balenaEtcher-linux-x64", program="balena-etcher")
    return make_zip(folder / "balenaEtcher-linux-x64-2.1.4.zip", tree["files"], tree["symlinks"],
                    tree["modes"])


def an_update(version: str | None = "1.1.4", **overrides) -> AvailableUpdate:
    values = dict(version=version, url="https://github.com/FreeCAD/FreeCAD/releases/download/"
                  "1.1.4/FreeCAD_1.1.4-Linux-x86_64-py311.AppImage",
                  filename="FreeCAD_1.1.4-Linux-x86_64-py311.AppImage", size=861_400_000,
                  published_at="2026-09-24T09:30:00Z")
    values.update(overrides)
    return AvailableUpdate(**values)


def sample_app(**overrides) -> InstalledApp:
    values = dict(id="org.freecad.FreeCAD", name="FreeCAD", version="1.1.3", scope=Scope.USER,
                  appimage_path="/nonexistent/FreeCAD.AppImage",
                  desktop_path="/nonexistent/easyinstaller-org.freecad.FreeCAD.desktop",
                  size=820_000_000)
    values.update(overrides)
    return InstalledApp(**values)


def set_settings(**values) -> None:
    prefs = load_settings()
    for key, value in values.items():
        setattr(prefs, key, value)
    save_settings(prefs)


# ------------------------------------------------------------------------------------------------
# repair (installer.repair: kept copies and portable apps, too)
# ------------------------------------------------------------------------------------------------


def test_repair_works_for_a_kept_copy_and_a_portable_app(tmp_path):
    install_file(freecad_file(tmp_path, "1.1.2"))
    copy = install_file(freecad_file(tmp_path, "1.1.3"), InstallOptions(keep_both=True))
    assert copy.base_id == "org.freecad.FreeCAD" and copy.id != copy.base_id
    Path(copy.desktop_path).unlink()
    repaired = window.repair_app(copy)
    assert repaired.id == copy.id and repaired.pinned
    assert Path(repaired.desktop_path).is_file()

    portable = install_file(etcher_zip(tmp_path))
    assert portable.kind == "portable"
    Path(portable.desktop_path).unlink()
    repaired = window.repair_app(portable)
    assert repaired.kind == "portable" and Path(repaired.desktop_path).is_file()
    assert repaired.install_dir == portable.install_dir


# ------------------------------------------------------------------------------------------------
# start-up maintenance: prune, reconcile, remembered updates, is a check due?
# ------------------------------------------------------------------------------------------------


def test_startup_maintenance_reconciles_prunes_and_reads_the_cache(tmp_path):
    install_file(freecad_file(tmp_path, "1.1.2"))
    freecad = install_file(freecad_file(tmp_path, "1.1.3"))
    assert freecad.previous and Path(freecad.previous["path"]).is_file()
    t3 = install_file(t3_file(tmp_path, "0.0.42"))

    # T3 Code updates itself (it replaces its own file)
    (tmp_path / "new").mkdir()
    new_file = t3_file(tmp_path / "new", "0.0.44", name="T3.AppImage")
    time.sleep(0.01)
    shutil.copyfile(new_file, t3.appimage_path)
    # FreeCAD has an update source; one update was found by an earlier check
    registry = Registry(user_layout().registry_path)
    registry.put(dataclasses.replace(registry.get("org.freecad.FreeCAD"),
                                     update_source=FREECAD_SOURCE))
    UpdateCache().put("user", "org.freecad.FreeCAD", an_update())
    set_settings(backup_days=0)   # "keep the previous version: off"

    report = window.startup_maintenance()

    changes = {r.app.id: r.change for r in report.results}
    assert changes["t3code"] == CHANGE_UPDATED
    assert window.self_update_text(report.results) == "T3 Code (Alpha) was updated to 0.0.44"
    assert find_installed("t3code")[0].version == "0.0.44"
    assert [a.id for a in report.pruned] == ["org.freecad.FreeCAD"]
    assert find_installed("org.freecad.FreeCAD")[0].previous is None
    assert not Path(freecad.previous["path"]).exists()
    assert set(report.updates) == {("user", "org.freecad.FreeCAD")}
    # checked moments ago: nothing is due
    assert report.check_due is False


def test_startup_maintenance_says_when_a_check_is_due(tmp_path):
    app = install_file(freecad_file(tmp_path, "1.1.3"))
    registry = Registry(user_layout().registry_path)
    registry.put(dataclasses.replace(app, update_source=FREECAD_SOURCE))
    assert window.startup_maintenance().check_due is True
    set_settings(check_updates=False)
    assert window.startup_maintenance().check_due is False
    set_settings(check_updates=True)
    UpdateCache().put("user", app.id, None)   # checked just now: up to date
    assert window.startup_maintenance().check_due is False


def test_updates_due_only_counts_apps_that_can_be_checked(tmp_path):
    cache = UpdateCache(tmp_path / "updates.json")
    portable = sample_app(id="etcher", kind="portable", update_source=FREECAD_SOURCE)
    copy = sample_app(id="org.freecad.FreeCAD--1.1.2", base_id="org.freecad.FreeCAD",
                      pinned=True, update_source=FREECAD_SOURCE)
    plain = sample_app(id="openscad")
    assert window.updates_due([portable, copy, plain], 24, cache) is False
    checkable = sample_app(update_source=FREECAD_SOURCE)
    assert window.updates_due([checkable], 24, cache) is True
    cache.put("user", checkable.id, None)
    assert window.updates_due([checkable], 24, cache) is False
    # a 0.1 entry: the update information of its file counts
    old = sample_app(id="old", update_info="gh-releases-zsync|a|b|latest|b*.AppImage.zsync")
    assert window.updates_due([old], 24, cache) is True


def test_reconcile_apps_never_stops_at_one_broken_app(monkeypatch):
    calls = []

    def fake(app):
        calls.append(app.id)
        if app.id == "bad":
            raise RuntimeError("boom")
        return ReconcileResult(app=app, change="unchanged", old_version=None, new_version=None)

    monkeypatch.setattr(window, "reconcile_app", fake)
    results = window.reconcile_apps([sample_app(id="bad"), sample_app(id="good")])
    assert calls == ["bad", "good"] and [r.app.id for r in results] == ["good"]


def test_self_update_texts():
    def result(change, old, new, name="T3 Code"):
        return ReconcileResult(app=sample_app(name=name), change=change, old_version=old,
                               new_version=new)

    assert window.self_update_text([]) is None
    assert window.self_update_text([result("unchanged", "1", "1")]) is None
    assert window.self_update_text([result(CHANGE_UPDATED, "0.0.42", "0.0.44")]) == \
        "T3 Code was updated to 0.0.44"
    assert window.self_update_text([result(CHANGE_UPDATED, None, None)]) == "T3 Code was updated"
    # adopted without a new version: nothing worth a message
    assert window.self_update_text([result(CHANGE_ADOPTED, "1.0", "1.0")]) is None
    assert window.self_update_text([result(CHANGE_UPDATED, "1", "2"),
                                    result(CHANGE_ADOPTED, "1", "3", name="Anytype")]) == \
        "2 apps were updated"
    # UPD-5: an app that replaced itself with an older version was not "updated"
    assert window.self_update_text([result(CHANGE_UPDATED, "0.0.45", "0.0.44")]) == \
        "T3 Code went back to version 0.0.44 by itself"
    assert window.self_update_text([result(CHANGE_UPDATED, "1", "2", name="Pen"),
                                    result(CHANGE_UPDATED, "3", "2"),
                                    result(CHANGE_ADOPTED, "5", "4", name="Anytype")]) == \
        "Pen was updated to 2 · 2 apps went back to an older version by itself"


# ------------------------------------------------------------------------------------------------
# update checks: what is kept, what is said
# ------------------------------------------------------------------------------------------------


def test_merge_updates_keeps_what_was_known_for_apps_that_could_not_be_checked():
    a, b, c = ("user", "a"), ("user", "b"), ("system", "c")
    known = {a: an_update("1"), b: an_update("2"), c: an_update("3")}
    result = UpdateCheckResult(updates={a: an_update("1.5")},
                               errors={b: NetworkError("offline")}, checked=3)
    merged = window.merge_updates(known, result)
    assert merged == {a: an_update("1.5"), b: an_update("2")}   # c is up to date now


def test_check_result_texts():
    ok = UpdateCheckResult(updates={}, errors={}, checked=3)
    assert window.check_result_text(ok) == "All apps are up to date"
    assert window.check_result_text(UpdateCheckResult()) == \
        "None of your apps tells where to find updates"
    one = UpdateCheckResult(updates={("user", "a"): an_update()}, checked=2)
    assert window.check_result_text(one) == "1 update is available"
    two = UpdateCheckResult(updates={("user", "a"): an_update(), ("user", "b"): an_update()},
                            errors={("user", "c"): UpdateError("broken")}, checked=4)
    assert window.check_result_text(two) == "2 updates are available · 1 app could not be checked"
    offline = UpdateCheckResult(errors={("user", "a"): NetworkError("No connection."),
                                        ("user", "b"): NetworkError("No connection.")}, checked=2)
    assert window.check_result_text(offline) == "No connection."
    broken = UpdateCheckResult(errors={("user", "a"): UpdateError("x")}, checked=1)
    assert window.check_result_text(broken) == "Updates could not be checked"


# ------------------------------------------------------------------------------------------------
# rows
# ------------------------------------------------------------------------------------------------


def test_row_subtitle_shows_version_scope_and_size():
    app = sample_app()
    size = format_size(820_000_000)
    assert app_row.app_subtitle(app, "ok") == f"Version 1.1.3 · Only for me · {size}"
    assert app_row.app_subtitle(dataclasses.replace(app, version=None, size=0,
                                                    scope=Scope.SYSTEM), "ok") == \
        "Everyone on this computer"
    portable = dataclasses.replace(app, kind="portable")
    assert app_row.app_subtitle(portable, "ok") == f"Version 1.1.3 · Only for me · {size}"
    assert app_row.app_subtitle(portable, "ok", portable_text=True) == \
        f"Portable app · Version 1.1.3 · Only for me · {size}"
    assert app_row.app_subtitle(app, "missing-appimage") == "App file is missing"
    assert app_row.app_subtitle(app, "missing-launcher") == "Missing from the app menu"


def test_update_button_texts():
    app = sample_app()
    assert app_row.update_label(an_update()) == "Update to 1.1.4"
    assert app_row.update_label(an_update(), compact=True) == "Update"
    assert app_row.update_label(an_update(None)) == "Update"
    assert app_row.update_tooltip(app, an_update()) == "Update FreeCAD to version 1.1.4"
    assert app_row.update_tooltip(app, an_update(None)) == "Install the new version of FreeCAD"


# ------------------------------------------------------------------------------------------------
# uninstall dialog
# ------------------------------------------------------------------------------------------------


def test_uninstall_texts():
    app = sample_app()
    assert uninstall_dialog.uninstall_heading(app) == "Remove FreeCAD?"   # its file is missing
    body = uninstall_dialog.uninstall_body(app)
    assert body.startswith("The app file is already gone.")

    existing = Path(__file__)
    ok = sample_app(appimage_path=str(existing), desktop_path=str(existing))
    assert uninstall_dialog.uninstall_heading(ok) == "Uninstall FreeCAD?"
    assert uninstall_dialog.uninstall_body(ok) == \
        "The app will be removed. Your personal files and settings are kept."
    assert uninstall_dialog.uninstall_body(ok, offers_data=True) == \
        "The app will be removed. Your personal files are kept."

    # a portable app may keep what it saves in its own folder: that is not "kept"
    portable = dataclasses.replace(ok, kind="portable", install_dir=str(existing.parent))
    assert uninstall_dialog.uninstall_body(portable) == (
        "The app will be removed. Anything it saved in its own folder is moved to the trash. "
        "Your other personal files and settings are kept.")
    assert uninstall_dialog.uninstall_body(portable, offers_data=True) == (
        "The app will be removed. Anything it saved in its own folder is moved to the trash. "
        "Your other personal files are kept.")

    kept = dataclasses.replace(ok, previous=make_previous(version="1.1.2", path="/x", sha256=None,
                                                          size=1))
    assert "The previous version that was kept is removed, too." in \
        uninstall_dialog.uninstall_body(kept)
    system = dataclasses.replace(ok, scope=Scope.SYSTEM)
    assert "asked for your password" in uninstall_dialog.uninstall_body(system)
    profile = dataclasses.replace(ok, apparmor_profile="/etc/apparmor.d/easyinstaller-x")
    assert "special permission" in uninstall_dialog.uninstall_body(profile)

    # what stays installed
    assert "installed for everyone on this computer. That copy stays installed." in \
        uninstall_dialog.uninstall_body(ok, [system])
    assert "installed just for you. That copy stays installed." in \
        uninstall_dialog.uninstall_body(system, [ok])
    copy = dataclasses.replace(ok, id="org.freecad.FreeCAD--1.1.2",
                               base_id="org.freecad.FreeCAD")
    assert "The other installed versions of this app stay installed." in \
        uninstall_dialog.uninstall_body(ok, [copy])


def test_data_texts():
    config = DataLocation(path=Path("/h/.config/FreeCAD"), kind="config", size=60_000,
                          file_count=2)
    cache = DataLocation(path=Path("/h/.cache/FreeCAD"), kind="cache", size=31_000_000,
                         file_count=1)
    assert uninstall_dialog.location_subtitle(config) == f"Settings · {format_size(60_000)}"
    assert uninstall_dialog.kind_label("cache") == "Temporary files"
    assert uninstall_dialog.kind_label("something-else") == "App data"
    assert uninstall_dialog.data_summary([]) == "No settings or data found"
    assert uninstall_dialog.data_summary([config]) == f"{format_size(60_000)} in 1 folder"
    assert uninstall_dialog.data_summary([config, cache]) == \
        f"{format_size(31_060_000)} in 2 folders"


def test_uninstall_and_trash_moves_the_ticked_folders_after_the_uninstall(tmp_path, isolated_env):
    app = install_file(freecad_file(tmp_path, "1.1.3"))
    config = isolated_env / ".config" / "FreeCAD"
    config.mkdir(parents=True)
    (config / "user.cfg").write_text("settings")
    locations = installer.removable_data(app)
    assert [loc.path for loc in locations] == [config]

    outcome = uninstall_dialog.uninstall_and_trash(app.id, Scope.USER, [config])
    assert not find_installed(app.id)
    assert outcome.trashed == [config] and not config.exists()
    assert outcome.trash_failures == [] and outcome.messages == []
    trashed = Path(os.environ["XDG_DATA_HOME"]) / "Trash" / "files" / "FreeCAD" / "user.cfg"
    assert trashed.read_text() == "settings"
    assert uninstall_dialog.result_toast_text(app, outcome, False) == \
        "FreeCAD was uninstalled, its settings and data are in the trash"


def test_nothing_is_trashed_when_the_uninstall_fails(tmp_path, isolated_env, monkeypatch):
    config = isolated_env / ".config" / "FreeCAD"
    config.mkdir(parents=True)

    def refused(app_id, scope, *, progress=None, keep_permission=False):
        raise AuthorizationError("Authentication was cancelled.")

    monkeypatch.setattr(uninstall_dialog, "uninstall", refused)
    with pytest.raises(AuthorizationError):
        uninstall_dialog.uninstall_and_trash("org.freecad.FreeCAD", Scope.USER, [config])
    assert config.is_dir()


def test_trash_failures_are_reported_in_full(tmp_path, isolated_env, monkeypatch):
    monkeypatch.setattr(uninstall_dialog, "uninstall",
                        lambda app_id, scope, **kw: ["A note."])
    # the home folder itself is never moved to the trash
    outcome = uninstall_dialog.uninstall_and_trash("x", Scope.USER, [isolated_env])
    assert outcome.trashed == [] and [p for p, _reason in outcome.trash_failures] == [isolated_env]
    assert isolated_env.is_dir()
    assert outcome.messages[0] == "A note."
    assert outcome.messages[1].startswith("~: ")
    app = sample_app()
    assert uninstall_dialog.done_text(app, True) == "FreeCAD was removed"
    assert uninstall_dialog.result_toast_text(app, uninstall_dialog.UninstallOutcome(), False) == \
        "FreeCAD was uninstalled"


# ------------------------------------------------------------------------------------------------
# details dialog
# ------------------------------------------------------------------------------------------------


def test_signature_texts():
    app = sample_app()
    assert details_dialog.signature_state(app) == (details_dialog.SIGNATURE_NONE,
                                                   "Not signed (most apps are not)")
    valid = dataclasses.replace(app, signature={
        "status": "valid", "signer": "FreeCAD Team",
        "fingerprint": "7A9C4E2B1F3D5A6C8E0B2D4F6A8C0E2B4D6F8A1C"})
    state, text = details_dialog.signature_state(valid)
    assert state == details_dialog.SIGNATURE_VALID
    assert text == ("Signed with a key named “FreeCAD Team”\n"
                    "Key 7A9C 4E2B 1F3D 5A6C 8E0B 2D4F 6A8C 0E2B 4D6F 8A1C")
    anonymous = dataclasses.replace(app, signature={"status": "valid"})
    assert details_dialog.signature_state(anonymous)[1] == "Signed with a key that has no name"
    invalid = dataclasses.replace(app, signature={"status": "invalid", "details": "Changed."})
    assert details_dialog.signature_state(invalid) == (details_dialog.SIGNATURE_INVALID,
                                                       "Changed.")
    unverified = dataclasses.replace(app, signature={"status": "unverified"})
    assert details_dialog.signature_state(unverified)[0] == details_dialog.SIGNATURE_UNVERIFIED
    # anything else in the registry is "not signed", never a crash
    odd = dataclasses.replace(app, signature={"status": "great"})
    assert details_dialog.signature_state(odd)[0] == details_dialog.SIGNATURE_NONE
    assert details_dialog.format_fingerprint(None) is None


def test_details_texts():
    app = sample_app(origin_url="https://github.com/FreeCAD/FreeCAD/releases/download/x.AppImage")
    assert details_dialog.origin_text(app) == "github.com"
    assert details_dialog.origin_text(sample_app()) == "Not known"
    assert details_dialog.kind_text(app) == "AppImage"
    portable = sample_app(kind="portable", install_dir="/h/Applications/Etcher",
                          original_filename="balenaEtcher-linux-x64-2.1.4.zip")
    assert details_dialog.kind_text(portable) == \
        "Portable app, unpacked from balenaEtcher-linux-x64-2.1.4.zip"
    assert details_dialog.location_of(portable) == "/h/Applications/Etcher"
    assert details_dialog.location_of(app) == app.appimage_path

    assert details_dialog.updates_text(portable).startswith("Portable apps are not checked")
    copy = sample_app(base_id="org.freecad", pinned=True, update_source=FREECAD_SOURCE)
    assert details_dialog.updates_text(copy) == \
        "This copy stays at version 1.1.3. It is never updated."
    assert details_dialog.updates_text(app).startswith("This app does not say where")
    with_source = sample_app(update_source=FREECAD_SOURCE)
    assert details_dialog.updates_text(with_source) == "From github.com/FreeCAD/FreeCAD"

    update = an_update()
    assert details_dialog.update_text(update) == "Version 1.1.4 is available"
    assert details_dialog.update_text(an_update(None)) == "A new version is available"
    assert details_dialog.update_subtitle(update).endswith(
        f" · Download size {format_size(861_400_000)}")
    assert details_dialog.update_subtitle(an_update(size=None, published_at=None)) is None


def test_previous_version_texts():
    now = time.time()
    saved = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 86400))
    previous = make_previous(version="1.1.2", path="/x", sha256=None, size=810_000_000,
                             saved_at=saved)
    until = details_dialog.kept_until(previous, 14, now=now)
    expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 11 * 86400))
    assert until == details_dialog.format_date(expected)
    assert details_dialog.kept_until(previous, 0) is None
    assert details_dialog.previous_title(previous) == "Version 1.1.2"
    assert details_dialog.go_back_text(previous) == "Go back to version 1.1.2"
    assert details_dialog.go_back_question(previous) == "Go back to version 1.1.2?"
    assert details_dialog.delete_backup_heading(previous) == "Delete version 1.1.2?"
    assert details_dialog.previous_subtitle(previous, 14).startswith("Kept until ")
    assert details_dialog.previous_subtitle(previous, 14).endswith(format_size(810_000_000))
    assert details_dialog.previous_subtitle(previous, 0).startswith(
        "Deleted the next time Easy Installer starts")
    # DATA-3: an app for everyone: the helper deletes it, at the next installation for everyone
    for days, moment in ((0, None), (14, now + 20 * 86400)):
        assert details_dialog.previous_subtitle(previous, days, system=True, now=moment).startswith(
            "Deleted the next time an app is installed or updated for everyone")
    assert details_dialog.previous_subtitle(previous, 14, system=True).startswith("Kept until ")
    unknown = make_previous(version=None, path="/x", sha256=None, size=0, saved_at="garbage")
    assert details_dialog.previous_title(unknown) == "Previous version"
    assert details_dialog.go_back_text(unknown) == "Go back to the previous version"
    assert details_dialog.go_back_question(unknown) == "Go back to the previous version?"
    assert details_dialog.delete_backup_heading(unknown) == "Delete the previous version?"
    assert details_dialog.previous_subtitle(unknown, 14) == "Kept on this computer"
    # long past its date (not pruned yet): never a date in the past
    old = make_previous(version="1", path="/x", sha256=None, size=0,
                        saved_at="2020-01-01T00:00:00Z")
    assert details_dialog.kept_until(old, 7, now=now) == details_dialog.format_date(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))


def test_details_find_the_data_also_while_another_version_is_installed(tmp_path, isolated_env):
    install_file(freecad_file(tmp_path, "1.1.2"))
    copy = install_file(freecad_file(tmp_path, "1.1.3"), InstallOptions(keep_both=True))
    main = next(a for a in find_installed("org.freecad.FreeCAD"))
    data = isolated_env / ".local" / "share" / "FreeCAD"
    data.mkdir(parents=True)
    (data / "macro.FCMacro").write_text("x" * 1000)
    # nothing is offered for removal while the kept copy remains ...
    assert installer.removable_data(main) == []
    # ... but the details show the folders and their size
    found = details_dialog.app_data_locations(main)
    assert [(loc.path, loc.size) for loc in found] == [(data, 1000)]
    assert details_dialog.app_data_locations(copy)[0].path == data
    # the apps folder itself is never "data"
    assert all("Applications" not in str(loc.path) for loc in found)


def test_last_checked_text(tmp_path):
    app = sample_app(update_source=FREECAD_SOURCE)
    cache = UpdateCache(tmp_path / "u.json")
    assert details_dialog.last_checked_text(app, cache) is None
    cache.put("user", app.id, None)
    assert details_dialog.last_checked_text(app, cache).startswith("Last checked on ")


# ------------------------------------------------------------------------------------------------
# preferences
# ------------------------------------------------------------------------------------------------


def test_preference_choices_and_texts():
    assert preferences_dialog.backup_choices(14) == [0, 7, 14, 30]
    # a value set with the command line is offered, not changed
    assert preferences_dialog.backup_choices(90) == [0, 7, 14, 30, 90]
    assert preferences_dialog.backup_choices(3) == [0, 3, 7, 14, 30]
    assert preferences_dialog.backup_label(0) == "Off"
    assert preferences_dialog.backup_label(1) == "1 day"
    assert preferences_dialog.backup_label(14) == "14 days"
    assert preferences_dialog.interval_text(24) == "once a day"
    assert preferences_dialog.interval_text(48) == "every 2 days"
    assert preferences_dialog.interval_text(6) == "every 6 hours"
    assert preferences_dialog.interval_text(1) == "once an hour"
    assert "not kept" in preferences_dialog.backup_subtitle(0)
    assert "go back" in preferences_dialog.backup_subtitle(7)
    prefs = load_settings()
    assert "once a day" in preferences_dialog.check_updates_subtitle(prefs)
