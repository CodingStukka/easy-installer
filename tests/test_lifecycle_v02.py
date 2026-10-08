"""The 0.2 features together, end to end through the core API (what the CLI and the window
call): an app's whole life from the first installation to the last trace that is removed.

* an AppImage: install -> update found -> update applied (old version kept) -> the app updates
  itself -> go back -> a second version next to it -> uninstall both, then its data;
* the same for everyone on the computer, through the real helper operations in a temporary root;
* a portable app from an archive;
* a computer that was set up with Easy Installer 0.1.

No network (fake release pages and downloads), no pkexec, everything in the isolated home.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer import __version__
from easy_installer.core import installer, privileged, reconcile, updater
from easy_installer.core.appdata import move_to_trash
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    drop_backup,
    execute_install,
    list_installed,
    plan_install,
    prune_backups,
    removable_data,
    repair,
    rollback,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.portable import inspect_portable, is_portable_archive
from easy_installer.core.reconcile import needs_reconcile, reconcile_all
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.settings import load_settings
from easy_installer.core.system_checks import SystemStatus
from easy_installer.core.updater import (
    apply_update,
    cached_updates,
    check_all_updates,
    check_app_update,
    has_update_source,
)
from easy_installer.core.updates import HttpResponse
from easy_installer.errors import InstallError
from easy_installer.helper import ops

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs
from fakearchive import electron_tree, make_zip

pytestmark = [requires_mksquashfs, requires_unsquashfs]

USER, SYSTEM = Scope.USER.value, Scope.SYSTEM.value
BACKUPS = ".easyinstaller-backups"


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
def quiet_system(monkeypatch):
    reconcile._known_foreign.clear()
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)


@pytest.fixture(autouse=True)
def sys_layout(system_root, monkeypatch):
    """The "for everyone" places live in a temporary root (never the real /opt, /var/lib)."""
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper(sys_layout, monkeypatch):
    """privileged.run_helper -> JSON round trip -> helper.ops (no pkexec, no root)."""
    calls: list[str] = []
    caller = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}

    def run_helper(op, payload, *, timeout=600):
        assert op in privileged.HELPER_OPS
        request = json.loads(json.dumps(payload))
        calls.append(op)
        if op == "install":
            return ops.op_install(request, layout=sys_layout, run_commands=False, **caller)
        if op == "uninstall":
            return ops.op_uninstall(request, layout=sys_layout, run_commands=False)
        if op == "drop-backup":
            return ops.op_drop_backup(request, layout=sys_layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    return calls


@pytest.fixture
def home(isolated_env, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_env / ".local" / "state"))
    for name in (".config", ".local/share", ".cache", ".local/state", "Downloads"):
        (isolated_env / name).mkdir(parents=True, exist_ok=True)
    return isolated_env


# ------------------------------------------------------------------------------------------------
# a fake app with a release page
# ------------------------------------------------------------------------------------------------

UPD_INFO = "gh-releases-zsync|demo-org|demo|latest|Demo-*-x86_64.AppImage.zsync"
API = "https://api.github.com/repos/demo-org/demo/releases/latest"


def demo(folder: Path, version: str, *, embedded_version: bool = True) -> Path:
    desktop = ("[Desktop Entry]\nType=Application\nName=Demo\nName[de]=Demo (de)\n"
               f"Exec=AppRun --v{version} %F\nIcon=demo\nStartupWMClass=demo\nCategories=Utility;\n")
    if embedded_version:
        desktop += f"X-AppImage-Version={version}\n"
    return make_fake_appimage(folder / f"Demo-{version}-x86_64.AppImage", {
        "demo.desktop": desktop, "AppRun": "#!/bin/sh\n", "payload.bin": f"demo {version}".encode() * 9,
        "demo.png": make_png(64, 64, (int(version[0]) * 20, 40, 60, 255)),
    }, upd_info=UPD_INFO)


class ReleasePage:
    """github.com/demo-org/demo: the latest release, the requests made, the downloads."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.latest: Path | None = None
        self.requests = 0
        self.downloads = 0

    def release(self, version: str, **kw) -> Path:
        self.latest = demo(self.folder, version, **kw)
        self.version = version
        return self.latest

    def fetch(self, url, *, headers=None, max_bytes=None) -> HttpResponse:
        assert url == API, f"unexpected request: {url}"
        self.requests += 1
        name = self.latest.name
        body = {"tag_name": f"v{self.version}", "prerelease": False, "draft": False,
                "html_url": f"https://github.com/demo-org/demo/releases/tag/v{self.version}",
                "published_at": "2026-09-20T10:11:12Z",
                "assets": [{"name": name, "state": "uploaded", "size": self.latest.stat().st_size,
                            "digest": "sha256:" + sha256(self.latest),
                            "browser_download_url": "https://github.com/demo-org/demo/releases/"
                                                    f"download/v{self.version}/{name}"}]}
        return HttpResponse(status=200, headers={}, body=json.dumps(body).encode(), url=url)

    def download(self, update, dest_dir, *, progress=None, cancel=None) -> Path:
        self.downloads += 1
        target = Path(dest_dir) / update.filename
        shutil.copyfile(self.latest, target)
        assert sha256(target) == update.sha256
        return target


@pytest.fixture
def page(tmp_path) -> ReleasePage:
    folder = tmp_path / "github"
    folder.mkdir()
    return ReleasePage(folder)


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def launcher(app: InstalledApp) -> DesktopEntry:
    return DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))


def exec_args(app: InstalledApp) -> list[str]:
    return split_exec(launcher(app).get("Exec"))


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def app_writes_its_data(home: Path, name: str = "demo") -> dict[str, str]:
    """What the running app leaves in the home folder; returns what may be offered later."""
    for folder in (home / ".config" / name, home / ".cache" / f"{name}-updater",
                   home / ".local" / "share" / name):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "state.json").write_text("{}")
    return {f".config/{name}": "config", f".local/share/{name}": "data",
            f".cache/{name}-updater": "cache"}


def offered(app: InstalledApp) -> dict[str, str]:
    root = Path(os.environ["HOME"]).resolve()
    return {str(location.path.relative_to(root)): location.kind for location in removable_data(app)}


def nothing_left(home: Path, layout=None) -> bool:
    """No app file, kept version, launcher, icon, registry entry or remembered update."""
    layout = layout or user_layout()
    assert names(layout.apps_dir) == []
    assert names(layout.desktop_dir) == []
    assert [p for p in layout.icons_dir.rglob("*") if p.is_file()] == []
    assert Registry(layout.registry_path).all() == []
    assert cached_updates() == {}
    assert names(updater.downloads_dir()) == []
    return True


# ------------------------------------------------------------------------------------------------
# an AppImage, only for me
# ------------------------------------------------------------------------------------------------


def test_the_whole_life_of_an_appimage(home, page):
    apps = user_layout().apps_dir
    downloads = home / "Downloads"

    # 1. install version 1 (the download is moved into the apps folder)
    v1_file = demo(downloads, "1.0")
    v1_sha = sha256(v1_file)
    v1 = install(v1_file)
    assert (v1.id, v1.version, v1.kind, v1.scope) == ("demo", "1.0", "appimage", Scope.USER)
    assert not v1_file.exists() and names(apps) == ["Demo.AppImage"]
    assert exec_args(v1) == [v1.appimage_path, "--v1.0", "%F"]
    assert v1.update_source["repo"] == "demo" and v1.data_hints == ["Demo", "demo-updater"]
    assert v1.installer_version == __version__ == "0.2.1"
    assert has_update_source(v1) and cached_updates() == {}
    assert [r.change for r in reconcile_all()] == ["unchanged"]
    data = app_writes_its_data(home)

    # 2. look for updates: version 2 is out
    page.release("2.0")
    found = check_all_updates(fetch=page.fetch)
    assert list(found) == [(USER, "demo")] and found.errors == {} and found.checked == 1
    update = found[(USER, "demo")]
    assert (update.version, update.filename) == ("2.0", "Demo-2.0-x86_64.AppImage")
    assert cached_updates() == {(USER, "demo"): update}              # what `list` shows
    assert check_app_update(v1, fetch=page.fetch) == update and page.requests == 1

    # 3. one click: download, check, install - the old version is kept
    v2 = apply_update(v1, update, downloader=page.download)
    assert v2.version == "2.0" and sha256(v2.appimage_path) == sha256(page.latest)
    assert exec_args(v2) == [v2.appimage_path, "--v2.0", "%F"]
    assert launcher(v2).get("X-AppImage-Version") == "2.0"
    kept_v1 = apps / BACKUPS / "demo" / "Demo-1.0.AppImage"
    assert v2.previous["path"] == str(kept_v1) and v2.previous["version"] == "1.0"
    assert sha256(kept_v1) == v1_sha
    assert cached_updates() == {} and page.downloads == 1
    assert names(apps) == [BACKUPS, "Demo.AppImage"]
    assert load_settings().backup_days == 14 and prune_backups(14) == []   # young: it stays

    # 4. the app updates itself to version 3 (it overwrites its own file); Easy Installer
    #    notices it the next time it looks
    v3_file = demo(downloads, "3.0")
    v3_sha = sha256(v3_file)
    shutil.copyfile(v3_file, v2.appimage_path)
    v3_file.unlink()
    assert needs_reconcile(registry().get("demo"))
    [result] = reconcile_all()
    assert (result.change, result.old_version, result.new_version) == ("updated", "2.0", "3.0")
    v3 = result.app
    assert v3.version == "3.0" and v3.sha256 == v3_sha and registry().get("demo") == v3
    assert exec_args(v3) == [v3.appimage_path, "--v3.0", "%F"]
    # no backup is made of a self-update; the kept version stays the one to go back to
    assert v3.previous == v2.previous and kept_v1.is_file()
    assert [r.change for r in reconcile_all()] == ["unchanged"]
    # version 2 is old news now
    assert check_all_updates(force=True, fetch=page.fetch).updates == {}

    # 5. go back to the kept version: the two versions swap
    back = rollback("demo", Scope.USER)
    assert back.version == "1.0" and sha256(back.appimage_path) == v1_sha
    assert exec_args(back) == [back.appimage_path, "--v1.0", "%F"]
    kept_v3 = apps / BACKUPS / "demo" / "Demo-3.0.AppImage"
    assert back.previous["path"] == str(kept_v3) and sha256(kept_v3) == v3_sha
    assert not kept_v1.exists() and back.installed_at == v1.installed_at
    assert [r.change for r in reconcile_all()] == ["unchanged"]

    # 6. install version 2 again - this time next to version 1 ("keep both")
    with inspect_appimage(demo(downloads, "2.0")) as info:
        plan = plan_install(info)
        assert plan.action == "update" and plan.keep_both_available and plan.main_installed == back
        copy_plan = plan_install(info, InstallOptions(keep_both=True))
        assert (copy_plan.app_id, copy_plan.name, copy_plan.action) == ("demo--2.0", "Demo 2.0", "install")
        copy = execute_install(copy_plan)
    assert (copy.base_id, copy.pinned, copy.version) == ("demo", True, "2.0")
    assert launcher(copy).get("Name") == "Demo 2.0" and launcher(copy).get("Name[de]") == "Demo (de) 2.0"
    assert registry().get("demo") == back                        # the main app is untouched
    assert [(a.id, a.version) for a in list_installed()] == [("demo", "1.0"), ("demo--2.0", "2.0")]
    assert names(apps) == [BACKUPS, "Demo-2.0.AppImage", "Demo.AppImage"]

    # 7. updates: offered for the main app (it went back to 1.0), never for the pinned copy
    found = check_all_updates(force=True, fetch=page.fetch)
    assert list(found) == [(USER, "demo")] and found.checked == 1
    assert not has_update_source(copy)

    # 8. uninstall the copy: its data is not offered, the other version still uses it
    assert offered(back) == {} and offered(copy) == {}
    assert uninstall(copy.id, Scope.USER) == []
    assert offered(copy) == {}
    assert names(apps) == [BACKUPS, "Demo.AppImage"] and kept_v3.is_file()

    # 9. uninstall the app: everything of it goes, its data is offered - and only then removed
    assert offered(back) == data
    assert uninstall("demo", Scope.USER) == []
    assert nothing_left(home)
    assert offered(back) == data
    assert all((home / rel / "state.json").is_file() for rel in data)     # still there
    chosen = [location.path for location in removable_data(back)]
    assert [error for _path, error in move_to_trash(chosen)] == [None] * 3
    assert offered(back) == {} and not (home / ".config" / "demo").exists()
    assert len(names(home / ".local" / "share" / "Trash" / "files")) == 3  # trash, not deleted


def test_update_then_delete_the_old_version(home, page):
    """The other ways out of "a previous version is kept"."""
    apps = user_layout().apps_dir
    v1 = install(demo(home / "Downloads", "1.0"))
    page.release("2.0")
    v2 = apply_update(v1, check_app_update(v1, fetch=page.fetch), downloader=page.download)
    assert Path(v2.previous["path"]).is_file()
    drop_backup("demo", Scope.USER)                        # "Delete old version"
    assert registry().get("demo").previous is None and names(apps) == ["Demo.AppImage"]
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.USER)

    page.release("3.0")
    v3 = apply_update(registry().get("demo"), check_app_update(v2, force=True, fetch=page.fetch),
                      downloader=page.download)
    assert v3.previous["version"] == "2.0"
    assert [a.id for a in prune_backups(0)] == ["demo"]    # "keep the previous version: off"
    assert registry().get("demo").previous is None and names(apps) == ["Demo.AppImage"]
    uninstall("demo", Scope.USER)
    assert nothing_left(home)


# ------------------------------------------------------------------------------------------------
# the same app for everyone on this computer
# ------------------------------------------------------------------------------------------------


def test_the_whole_life_of_a_system_wide_app(home, page, sys_layout, helper):
    apps = sys_layout.apps_dir
    v1 = install(demo(home / "Downloads", "1.0"), InstallOptions(scope=Scope.SYSTEM))
    assert v1.scope is Scope.SYSTEM and names(apps) == ["Demo.AppImage"]
    assert v1.update_source["repo"] == "demo" and v1.data_hints == ["Demo", "demo-updater"]
    mine = install(demo(home / "Downloads", "1.0"))        # ... and once more only for me
    data = app_writes_its_data(home)

    page.release("2.0")
    found = check_all_updates(fetch=page.fetch)
    # both installations are checked; they share one answer of the release page
    assert sorted(found) == [(SYSTEM, "demo"), (USER, "demo")] and page.requests == 1

    v2 = apply_update(v1, found[(SYSTEM, "demo")], downloader=page.download)
    assert v2.scope is Scope.SYSTEM and v2.version == "2.0"
    assert v2.previous["path"] == str(apps / BACKUPS / "demo" / "Demo-1.0.AppImage")
    assert registry().get("demo") == mine                  # my own copy is a separate app
    assert list(cached_updates()) == [(USER, "demo")]

    # a system-wide app that changed itself is reported, not touched
    shutil.copyfile(demo(home / "Downloads", "3.0"), v2.appimage_path)
    changes = {(r.app.scope.value, r.change) for r in reconcile_all()}
    assert changes == {(USER, "unchanged"), (SYSTEM, "needs-admin")}
    fixed = repair("demo", Scope.SYSTEM)                   # "Repair", with the password
    assert fixed.version == "3.0" and exec_args(fixed) == [fixed.appimage_path, "--v3.0", "%F"]
    assert {(r.app.scope.value, r.change) for r in reconcile_all()} == \
        {(USER, "unchanged"), (SYSTEM, "unchanged")}

    back = rollback("demo", Scope.SYSTEM)
    assert back.version == "1.0" and back.previous["version"] == "3.0"

    # data is only offered once the last installation goes
    assert offered(back) == {} and offered(mine) == {}
    uninstall("demo", Scope.SYSTEM)
    assert names(apps) == [] and registry(sys_layout).all() == []
    assert offered(back) == {}                             # mine is still installed ...
    assert offered(mine) == data                           # ... and is the last one now
    uninstall("demo", Scope.USER)
    assert offered(mine) == data and offered(back) == data
    assert nothing_left(home) and nothing_left(home, sys_layout)
    assert helper.count("install") == 4 and helper.count("uninstall") == 1


# ------------------------------------------------------------------------------------------------
# a portable app from an archive
# ------------------------------------------------------------------------------------------------


def etcher(folder: Path, version: str) -> Path:
    tree = electron_tree("balenaEtcher-linux-x64", program="balena-etcher")
    tree["files"]["balenaEtcher-linux-x64/resources/version.txt"] = version
    return make_zip(folder / f"balenaEtcher-linux-x64-{version}.zip", **tree)


def install_archive(archive: Path, options: InstallOptions | None = None) -> InstalledApp:
    assert is_portable_archive(archive)
    with inspect_portable(archive) as info:
        return execute_install(plan_install(info, options))


def test_the_whole_life_of_a_portable_app(home, page):
    apps = user_layout().apps_dir
    folder = apps / "balenaEtcher"

    # 1. install from the archive: unpacked into its own folder, the archive stays
    archive_1 = etcher(home / "Downloads", "2.1.4")
    v1 = install_archive(archive_1)
    assert (v1.kind, v1.version, v1.scope) == ("portable", "2.1.4", Scope.USER)
    assert v1.install_dir == str(folder) and v1.appimage_path == str(folder / "balena-etcher")
    assert archive_1.is_file() and names(apps) == ["balenaEtcher"]
    assert exec_args(v1) == [str(folder / "balena-etcher")] and launcher(v1).get("Path") == str(folder)
    assert os.access(folder / "balena-etcher", os.X_OK)
    assert (folder / "resources" / "version.txt").read_text() == "2.1.4"
    assert not (folder / "MyApp").exists()         # the archive's link to its build machine
    data = app_writes_its_data(home, "balena-etcher")

    # 2. no update checks and no reconcile for portable apps
    assert not has_update_source(v1)
    found = check_all_updates(force=True, fetch=page.fetch)
    assert (found.updates, found.errors, found.checked, page.requests) == ({}, {}, 0, 0)
    (folder / "balena-etcher").write_bytes((folder / "balena-etcher").read_bytes() + b"patched")
    assert [r.change for r in reconcile_all()] == ["unchanged"] and registry().get(v1.id) == v1

    # 3. a newer archive replaces the folder; the old folder is kept as the previous version
    v2 = install_archive(etcher(home / "Downloads", "2.2.0"))
    assert v2.version == "2.2.0" and v2.install_dir == v1.install_dir
    kept = Path(v2.previous["path"])
    assert kept == apps / BACKUPS / v1.id / "balenaEtcher-2.1.4" and kept.is_dir()
    assert (kept / "resources" / "version.txt").read_text() == "2.1.4"
    assert (kept / "balena-etcher").read_bytes().endswith(b"patched")     # as it was
    assert (folder / "resources" / "version.txt").read_text() == "2.2.0"
    assert names(apps) == [BACKUPS, "balenaEtcher"]

    # 4. go back; the launcher is made again after someone deleted it
    back = rollback(v1.id, Scope.USER)
    assert back.version == "2.1.4" and back.previous["version"] == "2.2.0"
    assert (folder / "resources" / "version.txt").read_text() == "2.1.4"
    assert launcher(back).get("X-AppImage-Version") == "2.1.4"
    Path(back.desktop_path).unlink()
    assert registry().get(v1.id).status() == "missing-launcher"
    repaired = repair(v1.id, Scope.USER)
    assert repaired.status() == "ok" and repaired.previous == back.previous
    assert exec_args(repaired) == [str(folder / "balena-etcher")]

    # 5. uninstall: folder, kept version, launcher - then, if wanted, the data. Its program was
    #    changed in step 2, so the folder is not deleted for good: it goes to the trash.
    assert offered(repaired) == data
    notes = uninstall(v1.id, Scope.USER)
    assert len(notes) == 1 and "balenaEtcher was moved to the trash" in notes[0]
    assert nothing_left(home) and archive_1.is_file()
    assert offered(repaired) == data
    results = move_to_trash([location.path for location in removable_data(repaired)])
    assert [error for _path, error in results] == [None] * 3 and offered(repaired) == {}


# ------------------------------------------------------------------------------------------------
# a computer that was set up with Easy Installer 0.1
# ------------------------------------------------------------------------------------------------

T3_YML = "owner: pingdotgg\nrepo: t3code\nprovider: github\nupdaterCacheDirName: t3code-updater\n"
T3_FEED = "https://github.com/pingdotgg/t3code/releases/latest/download/latest-linux.yml"


def t3(path: Path, version: str) -> Path:
    desktop = ("[Desktop Entry]\nName=T3 Code (Alpha)\nExec=AppRun --no-sandbox %U\nTerminal=false\n"
               f"Type=Application\nIcon=t3code\nStartupWMClass=t3code\nX-AppImage-Version={version}\n"
               "Categories=Development;\n")
    return make_fake_appimage(path, {
        "t3code.desktop": desktop, "AppRun": "#!/bin/sh\n", "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": f"asar {version}", "resources/app-update.yml": T3_YML,
        "t3code.png": make_png(64, 64)})


def test_upgrade_from_0_1(home, page):
    """The registry file of 0.1 keeps working as it is; what 0.2 adds is learned on the way."""
    from test_registry import V01_REGISTRY

    v01_keys = set(json.loads(V01_REGISTRY)["apps"]["t3code"])
    downloads = home / "Downloads"
    # FreeCAD-like: update information in the file, the version only in the download's name
    install(demo(downloads, "1.0", embedded_version=False))
    install(t3(downloads / "T3-Code-0.0.42-x86_64.AppImage", "0.0.42"))
    path = user_layout().registry_path
    written = json.loads(path.read_text())
    for entry in written["apps"].values():
        for key in set(entry) - v01_keys:
            del entry[key]
        entry["installer_version"] = "0.1.0"
        assert set(entry) == v01_keys                  # exactly what 0.1 wrote
    path.write_text(json.dumps(written, indent=2) + "\n")
    launchers = {p.name: p.read_bytes() for p in user_layout().desktop_dir.iterdir()}

    # it loads; an app with update information can be checked right away
    old = {app.id: app for app in list_installed()}
    assert sorted(old) == ["demo", "t3code"] and old["demo"].version == "1.0"
    assert all(app.mtime_ns == 0 and app.update_source is None and app.data_hints == []
               for app in old.values())
    assert has_update_source(old["demo"]) and not has_update_source(old["t3code"])
    assert offered(old["t3code"]) == {}                # nothing there yet - and no crash

    # the first start of 0.2 looks at each file once
    assert sorted((r.app.id, r.change) for r in reconcile_all()) == \
        [("demo", "recorded"), ("t3code", "recorded")]
    now = {app.id: app for app in list_installed()}
    assert now["t3code"].update_source["kind"] == "electron-github" and has_update_source(now["t3code"])
    assert now["t3code"].data_hints == ["T3 Code (Alpha)", "t3code", "t3code-updater"]
    assert all(app.mtime_ns > 0 and app.installer_version == "0.1.0" for app in now.values())
    assert {p.name: p.read_bytes() for p in user_layout().desktop_dir.iterdir()} == launchers
    # every key 0.1 knows is still there, unchanged
    stored = json.loads(path.read_text())
    assert stored["format"] == 1
    for app_id, entry in written["apps"].items():
        assert {key: stored["apps"][app_id][key] for key in v01_keys} == entry

    # both apps are checked, each through its own kind of source
    page.release("2.0", embedded_version=False)
    new_t3 = t3(page.folder / "T3-Code-0.0.44-x86_64.AppImage", "0.0.44")
    digest = base64.b64encode(hashlib.sha512(new_t3.read_bytes()).digest()).decode()
    latest_yml = (f"version: 0.0.44\nfiles:\n  - url: {new_t3.name}\n    sha512: {digest}\n"
                  f"    size: {new_t3.stat().st_size}\npath: {new_t3.name}\nsha512: {digest}\n")

    def fetch(url, **kw):
        if url == T3_FEED:
            return HttpResponse(status=200, headers={}, body=latest_yml.encode(), url=url)
        return page.fetch(url, **kw)

    found = check_all_updates(fetch=fetch)
    assert {key: update.version for key, update in found.items()} == \
        {(USER, "demo"): "2.0", (USER, "t3code"): "0.0.44"} and found.errors == {}

    # the update of an entry made by 0.1: the version comes from the announced release
    demo_2 = apply_update(now["demo"], found[(USER, "demo")], downloader=page.download)
    assert demo_2.version == "2.0" and demo_2.previous["version"] == "1.0"
    assert demo_2.installer_version == __version__ and demo_2.update_source["repo"] == "demo"

    def download_t3(update, dest_dir, **kw):
        assert update.sha512 == hashlib.sha512(new_t3.read_bytes()).hexdigest()
        return Path(shutil.copyfile(new_t3, Path(dest_dir) / update.filename))

    t3_2 = apply_update(now["t3code"], found[(USER, "t3code")], downloader=download_t3)
    assert t3_2.version == "0.0.44" and t3_2.previous["version"] == "0.0.42"
    assert exec_args(t3_2) == [t3_2.appimage_path, "--no-sandbox", "%U"]
    assert cached_updates() == {}

    assert rollback("demo", Scope.USER).version == "1.0"
    for app_id in ("demo", "t3code"):
        uninstall(app_id, Scope.USER)
    assert nothing_left(home)
