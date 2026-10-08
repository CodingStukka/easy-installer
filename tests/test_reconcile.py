"""core/reconcile.py: apps that replace their own file (self-updaters) stay correctly installed.

A self-updater is simulated the way real ones work: the installed file is overwritten in place
with another fake AppImage (another version, other Exec arguments, another icon), or the new
version is saved under a new name and the old file is deleted.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import installer, privileged, reconcile
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import InstallOptions, execute_install, plan_install
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.reconcile import (
    ReconcileResult,
    needs_reconcile,
    reconcile_all,
    reconcile_app,
)
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import SandboxFix
from easy_installer.core.system_checks import SystemStatus

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs

pytestmark = [requires_mksquashfs, requires_unsquashfs]

LAUNCHER = "/usr/bin/easy-installer"


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


class Machine:
    """What the tests may change about "this computer" (system status, uninstall launcher)."""

    def __init__(self, monkeypatch):
        self.status = make_status()
        self.launcher: str | None = None
        self.helper_calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: self.status)
        monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: self.launcher)
        monkeypatch.setattr(installer, "_mime_tool", lambda: None)
        monkeypatch.setattr(privileged, "run_helper", self.run_helper)

    def run_helper(self, op, payload, *, timeout=600):
        self.helper_calls.append((op, payload))
        raise AssertionError(f"reconcile must never start the administrator helper ({op})")


@pytest.fixture(autouse=True)
def machine(monkeypatch) -> Machine:
    reconcile._known_foreign.clear()
    return Machine(monkeypatch)


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def demo(path: Path, version: str, *, args: str = "%F", color=(10, 20, 30, 255), name: str = "Demo",
         stem: str = "demo", electron: bool = False, icon_size: int = 64) -> Path:
    """A fake app; version, Exec arguments and icon differ between "releases"."""
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\nName[de]={name} (de)\n"
               f"Exec=AppRun {args}\nIcon={stem}\nCategories=Utility;\n"
               f"X-AppImage-Version={version}\n")
    files: dict[str, bytes | str] = {
        f"{stem}.desktop": desktop,
        "AppRun": "#!/bin/sh\n",
        "payload.bin": f"{name} {version}".encode() * (len(version) + 1),
        f"usr/share/icons/hicolor/{icon_size}x{icon_size}/apps/{stem}.png":
            make_png(icon_size, icon_size, color),
    }
    if electron:
        files["chrome-sandbox"] = b"\x7fELF"
        files["resources/app.asar"] = b"asar"
    return make_fake_appimage(path, files)


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def self_update(app: InstalledApp, new_file: Path) -> None:
    """The running app downloads ``new_file`` and writes it over its own file."""
    shutil.copyfile(new_file, app.appimage_path)


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def registry() -> Registry:
    return Registry(user_layout().registry_path)


def launcher(app: InstalledApp) -> DesktopEntry:
    return DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))


def snapshot(*roots: Path) -> dict[str, bytes]:
    """Every file below the folders, with its content (to prove that nothing changed)."""
    found = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith(".lock"):
                found[str(path)] = path.read_bytes()
    return found


# ------------------------------------------------------------------------------------------------
# needs_reconcile: one stat
# ------------------------------------------------------------------------------------------------


def test_installing_records_the_modification_time(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    st = os.stat(app.appimage_path)
    assert app.mtime_ns == st.st_mtime_ns > 0 and app.size == st.st_size
    assert registry().get("demo").mtime_ns == st.st_mtime_ns
    assert needs_reconcile(app) is False
    # a reinstall (repair) of the installed file records it again
    os.utime(app.appimage_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    again = install(Path(app.appimage_path))
    assert again.mtime_ns == st.st_mtime_ns + 5_000_000_000
    # and so does a copy (keep_original) and an update
    newer = install(demo(downloads / "Demo-1.1.AppImage", "1.1"),
                    InstallOptions(keep_original=True))
    assert newer.mtime_ns == os.stat(newer.appimage_path).st_mtime_ns


def test_needs_reconcile(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    path = Path(app.appimage_path)
    st = path.stat()
    assert not needs_reconcile(app)

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))          # same size, another time
    assert needs_reconcile(app)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert not needs_reconcile(app)

    with open(path, "ab") as fh:                                       # another size
        fh.write(b"x")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert needs_reconcile(app)

    # an entry made by 0.1 has no time: only the size counts
    old_entry = dataclasses.replace(app, mtime_ns=0, size=path.stat().st_size)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 99))
    assert not needs_reconcile(old_entry)
    assert needs_reconcile(dataclasses.replace(old_entry, size=1))

    # a missing file, a folder in its place, a portable app: not this function's business
    path.unlink()
    assert not needs_reconcile(app)
    path.mkdir()
    assert not needs_reconcile(app)
    assert not needs_reconcile(dataclasses.replace(app, kind="portable", size=1))
    assert not needs_reconcile(dataclasses.replace(app, appimage_path=""))


# ------------------------------------------------------------------------------------------------
# updated: the app replaced its own file
# ------------------------------------------------------------------------------------------------


def test_self_update_in_place_is_picked_up(downloads, machine):
    machine.launcher = LAUNCHER
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0", args="--old %F"))
    old_icon = Path(v1.icon_paths[0])
    assert old_icon.parent.name == "apps" and "64x64" in str(old_icon)
    assert split_exec(launcher(v1).get("Exec")) == [v1.appimage_path, "--old", "%F"]

    v2_file = demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0", args="--new --fast %U",
                   color=(200, 0, 0, 255), icon_size=128)
    machine.launcher = None                 # e.g. checked by a development build: no launcher
    self_update(v1, v2_file)
    assert needs_reconcile(v1)

    events = []
    result = reconcile_app(v1, progress=lambda f, m: events.append((f, m)))
    assert isinstance(result, ReconcileResult)
    assert (result.change, result.old_version, result.new_version) == ("updated", "1.0", "2.0")
    app = result.app
    assert registry().get("demo") == app and len(registry().load()) == 1

    # registry: version, checksum, size, time
    st = os.stat(v1.appimage_path)
    assert app.version == "2.0" and app.sha256 == sha256(v2_file) != v1.sha256
    assert app.size == st.st_size == v2_file.stat().st_size
    assert app.mtime_ns == st.st_mtime_ns
    assert app.appimage_path == v1.appimage_path and app.desktop_path == v1.desktop_path
    assert app.installed_at == v1.installed_at and app.original_filename == v1.original_filename
    assert not needs_reconcile(app)

    # launcher: new arguments and version, still the same file; the "Uninstall…" action stays
    entry = launcher(app)
    assert split_exec(entry.get("Exec")) == [app.appimage_path, "--new", "--fast", "%U"]
    assert entry.get("X-AppImage-Version") == "2.0" and entry.get("TryExec") == app.appimage_path
    assert entry.get_list("Actions") == ["easyinstaller-uninstall"]
    assert split_exec(entry.get("Exec", "Desktop Action easyinstaller-uninstall")) == \
        [LAUNCHER, "--uninstall", "demo"]

    # icon: the new picture at its new size; the old icon file is gone
    new_icon = Path(app.icon_paths[0])
    assert "128x128" in str(new_icon) and new_icon.read_bytes() == make_png(128, 128, (200, 0, 0, 255))
    assert not old_icon.exists()

    # no backup: the old version no longer exists anywhere
    assert app.previous is None and not user_layout().backups_dir.exists()
    assert sorted(p.name for p in user_layout().apps_dir.iterdir()) == ["Demo.AppImage"]
    assert machine.helper_calls == []
    assert events and events[-1][0] == 1.0

    # nothing to do the next time
    assert reconcile_app(app).change == "unchanged"
    assert [r.change for r in reconcile_all()] == ["unchanged"]


def test_installation_choices_survive_a_self_update(downloads, machine):
    machine.status = make_status(userns_restricted=True)
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0", electron=True),
                 InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX, extract_and_run=True,
                                add_uninstall_action=False))
    assert (v1.sandbox_fix, v1.extract_and_run, v1.extract_and_run_explicit) == \
        ("no-sandbox", True, True)
    machine.launcher = LAUNCHER   # a launcher exists now - but this app was installed without one
    self_update(v1, demo(downloads / "Demo-1.1.AppImage", "1.1", electron=True, args="--x %U"))

    app = reconcile_app(v1).app
    assert app.version == "1.1"
    assert (app.sandbox_fix, app.extract_and_run, app.extract_and_run_explicit) == \
        ("no-sandbox", True, True)
    entry = launcher(app)
    assert split_exec(entry.get("Exec")) == ["env", "APPIMAGE_EXTRACT_AND_RUN=1", app.appimage_path,
                                             "--no-sandbox", "--x", "%U"]
    assert entry.get_list("Actions") == []   # still no "Uninstall…" action
    assert machine.helper_calls == []


def test_self_update_never_asks_for_the_password(downloads, machine, monkeypatch, tmp_path):
    """The new version needs the sandbox permission the old one did not: nobody is there to
    type a password, so it is started without its sandbox instead (Repair can do better)."""
    machine.status = make_status(userns_restricted=True)
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    assert v1.sandbox_fix == "none"
    self_update(v1, demo(downloads / "Demo-2.0.AppImage", "2.0", electron=True, args="%U"))
    result = reconcile_app(v1)
    assert result.change == "updated" and result.app.sandbox_fix == "no-sandbox"
    assert result.app.apparmor_profile is None
    assert "--no-sandbox" in split_exec(launcher(result.app).get("Exec"))
    assert machine.helper_calls == []


def test_existing_sandbox_permission_is_kept_without_the_password(downloads, machine, monkeypatch,
                                                                  tmp_path):
    """The profile is attached to the path, which did not change: nothing to ask for."""
    from easy_installer.core.sandbox import render_apparmor_profile

    machine.status = make_status(userns_restricted=True)
    profiles = tmp_path / "etc" / "apparmor.d"
    profiles.mkdir(parents=True)
    monkeypatch.setattr(installer, "layout_for", lambda scope: dataclasses.replace(
        user_layout(), apparmor_dir=profiles))
    target = user_layout().apps_dir / "Demo.AppImage"
    profile = profiles / "easyinstaller-demo"
    profile.write_text(render_apparmor_profile("demo", os.path.realpath(target)))

    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0", electron=True))
    assert v1.sandbox_fix == "apparmor" and v1.apparmor_profile == str(profile)
    self_update(v1, demo(downloads / "Demo-1.1.AppImage", "1.1", electron=True, args="--n %U"))
    app = reconcile_app(v1).app
    assert app.version == "1.1" and app.sandbox_fix == "apparmor"
    assert app.apparmor_profile == str(profile)
    assert "--no-sandbox" not in split_exec(launcher(app).get("Exec"))
    assert machine.helper_calls == []


def test_unattended_plan_never_starts_the_helper_even_if_the_profile_vanishes(
        downloads, machine, monkeypatch, tmp_path):
    """The permission was there when the plan was made and is gone when it is carried out."""
    from easy_installer.core.sandbox import render_apparmor_profile

    machine.status = make_status(userns_restricted=True)
    profiles = tmp_path / "etc" / "apparmor.d"
    profiles.mkdir(parents=True)
    monkeypatch.setattr(installer, "layout_for", lambda scope: dataclasses.replace(
        user_layout(), apparmor_dir=profiles))
    profile = profiles / "easyinstaller-demo"
    profile.write_text(render_apparmor_profile(
        "demo", os.path.realpath(user_layout().apps_dir / "Demo.AppImage")))
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0", electron=True))
    self_update(v1, demo(downloads / "Demo-1.1.AppImage", "1.1", electron=True))
    with inspect_appimage(v1.appimage_path) as info:
        plan = installer.plan_refresh(info, v1)
        assert plan.unattended and plan.sandbox_fix is SandboxFix.APPARMOR
        assert plan.requires_root is False and plan.in_place and plan.backup_target is None
        profile.unlink()
        app = execute_install(plan)
    assert machine.helper_calls == []
    assert app.version == "1.1" and app.sandbox_fix == "no-sandbox"
    assert "--no-sandbox" in split_exec(launcher(app).get("Exec"))


def test_a_kept_previous_version_survives_a_self_update(downloads):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    backup = Path(v2.previous["path"])
    assert backup.is_file() and v2.previous["version"] == "1.0"
    self_update(v2, demo(downloads / "Demo-3.0.AppImage", "3.0"))
    app = reconcile_app(v2).app
    assert app.version == "3.0"
    assert app.previous == v2.previous and backup.is_file()   # kept, and no new backup
    assert [p.name for p in backup.parent.iterdir()] == [backup.name]


def test_a_kept_copy_that_updates_itself_stays_a_copy(downloads):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    copy = install(demo(downloads / "Demo-2.0.AppImage", "2.0"), InstallOptions(keep_both=True))
    assert copy.id == "demo--2.0" and copy.base_id == "demo" and copy.pinned
    self_update(copy, demo(downloads / "Demo-2.1.AppImage", "2.1", args="--two %F"))
    results = {r.app.id: r for r in reconcile_all([Scope.USER])}
    assert results["demo"].change == "unchanged"
    assert results["demo--2.0"].change == "updated"
    app = results["demo--2.0"].app
    assert (app.id, app.base_id, app.pinned, app.version) == ("demo--2.0", "demo", True, "2.1")
    assert app.appimage_path == copy.appimage_path and app.name == "Demo 2.1"
    assert launcher(app).get("Name[de]") == "Demo (de) 2.1"
    assert registry().get("demo").version == "1.0"


def test_touched_file_is_only_recorded(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    before = snapshot(user_layout().desktop_dir, user_layout().icons_dir)
    st = os.stat(app.appimage_path)
    os.utime(app.appimage_path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    result = reconcile_app(app)
    assert result.change == "recorded" and result.old_version == result.new_version == "1.0"
    assert result.app == dataclasses.replace(app, mtime_ns=st.st_mtime_ns + 7_000_000_000)
    assert registry().get("demo") == result.app
    assert snapshot(user_layout().desktop_dir, user_layout().icons_dir) == before
    assert reconcile_app(result.app).change == "unchanged"


def test_entry_made_by_0_1_gets_its_time_recorded(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    as_0_1 = {key: value for key, value in app.to_dict().items()
              if key not in ("kind", "install_dir", "mtime_ns", "base_id", "pinned", "previous",
                             "update_source", "origin_url", "signature", "data_hints")}
    old = InstalledApp.from_dict(as_0_1)
    assert old.mtime_ns == 0
    registry().put(old)
    desktop_before = Path(app.desktop_path).read_bytes()

    results = reconcile_all()
    assert [(r.app.id, r.change) for r in results] == [("demo", "recorded")]
    stored = registry().get("demo")
    # ... and, on the way, what 0.2 knows about a file (here: the names of its data folders)
    assert stored == dataclasses.replace(old, mtime_ns=os.stat(app.appimage_path).st_mtime_ns,
                                         data_hints=app.data_hints)
    assert app.data_hints == ["Demo", "demo-updater"]
    assert Path(app.desktop_path).read_bytes() == desktop_before
    assert [r.change for r in reconcile_all()] == ["unchanged"]


AS_0_1 = ("kind", "install_dir", "mtime_ns", "base_id", "pinned", "previous", "update_source",
          "origin_url", "signature", "data_hints")
UPD_INFO = "gh-releases-zsync|demo-org|demo|latest|Demo-*-x86_64.AppImage.zsync"
T3_YML = "owner: pingdotgg\nrepo: t3code\nprovider: github\n"


def as_made_by_0_1(app: InstalledApp) -> InstalledApp:
    old = InstalledApp.from_dict({key: value for key, value in app.to_dict().items()
                                  if key not in AS_0_1} | {"installer_version": "0.1.0"})
    Registry(user_layout().registry_path).put(old)
    return old


def test_entry_made_by_0_1_learns_what_0_2_knows_about_its_file(downloads, monkeypatch):
    """Where updates come from, the signature and the data folder names are read from the
    installed file once - without running it and without a new launcher."""
    from easy_installer.core import inspector
    from easy_installer.core.signature import SignatureInfo

    signed = SignatureInfo(status="valid", fingerprint="AB" * 20, signer="Maker", details=None)
    monkeypatch.setattr(inspector, "read_signature", lambda path, elf: signed)
    monkeypatch.setattr(inspector, "read_origin", lambda path: "https://example.org/dl/Demo.AppImage")
    files = {"demo.desktop": "[Desktop Entry]\nType=Application\nName=Demo\nExec=AppRun\n"
                             "Icon=demo\nX-AppImage-Version=1.0\n",
             "AppRun": "#!/bin/sh\n", "demo.png": make_png(32, 32)}
    freecad_like = install(make_fake_appimage(downloads / "Demo-1.0.AppImage", files, upd_info=UPD_INFO))
    electron = install(make_fake_appimage(downloads / "T3-1.0.AppImage", {
        "t3code.desktop": files["demo.desktop"].replace("Demo", "T3 Code").replace("Icon=demo", "Icon=t3"),
        "AppRun": "#!/bin/sh\n", "t3.png": make_png(32, 32), "chrome-sandbox": b"x",
        "resources/app-update.yml": T3_YML}))
    old = [as_made_by_0_1(freecad_like), as_made_by_0_1(electron)]
    assert all(app.update_source is None and app.data_hints == [] and app.signature is None
               for app in old)
    launchers = [Path(app.desktop_path).read_bytes() for app in old]

    assert sorted((r.app.id, r.change) for r in reconcile_all()) == \
        [("demo", "recorded"), ("t3code", "recorded")]
    demo_now, t3_now = registry().get("demo"), registry().get("t3code")
    assert demo_now.update_source["kind"] == "github-assets" and demo_now.update_source["repo"] == "demo"
    assert t3_now.update_source == {"kind": "electron-github", "owner": "pingdotgg", "repo": "t3code",
                                    "release": None, "pattern": None, "url": None,
                                    "prerelease": False, "via": "app-update.yml"}
    assert demo_now.data_hints == ["Demo", "demo-updater"]
    assert t3_now.data_hints == ["T3 Code", "t3code", "t3code-updater"]
    assert demo_now.signature == signed.to_dict()
    assert demo_now.origin_url == "https://example.org/dl/Demo.AppImage"
    assert demo_now.mtime_ns > 0 and demo_now.installer_version == "0.1.0"    # not reinstalled
    assert [Path(app.desktop_path).read_bytes() for app in old] == launchers
    # everything else is exactly what 0.1 recorded
    assert dataclasses.replace(demo_now, mtime_ns=0, update_source=None, data_hints=[],
                               signature=None, origin_url=None) == old[0]

    # once: the file is not looked at again
    def never(*args, **kw):
        raise AssertionError("the file is not inspected again")

    monkeypatch.setattr(reconcile, "inspect_appimage", never)
    assert [r.change for r in reconcile_all()] == ["unchanged", "unchanged"]


def test_details_that_are_recorded_are_never_replaced_by_reconcile(downloads, monkeypatch):
    from easy_installer.core import inspector

    app = install(make_fake_appimage(downloads / "Demo-1.0.AppImage", {
        "demo.desktop": "[Desktop Entry]\nType=Application\nName=Demo\nExec=AppRun\nIcon=demo\n",
        "AppRun": "#!/bin/sh\n"}, upd_info=UPD_INFO))
    mine = {"kind": "zsync-url", "url": "https://example.org/Demo.zsync"}
    kept = dataclasses.replace(app, mtime_ns=0, update_source=mine, data_hints=["demo-data"],
                               origin_url="https://example.org/first")
    registry().put(kept)
    monkeypatch.setattr(inspector, "read_origin", lambda path: "https://example.org/second")
    assert reconcile_app(kept).change == "recorded"
    now = registry().get("demo")
    assert (now.update_source, now.data_hints, now.origin_url) == \
        (mine, ["demo-data"], "https://example.org/first")


def test_details_of_another_app_are_never_recorded(downloads):
    """The file of an entry made by 0.1 was replaced by another app of exactly the same size
    (0.1 recorded no modification time): nothing of that other app gets into the entry."""
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    other = make_fake_appimage(downloads / "Other.AppImage", {
        "other.desktop": "[Desktop Entry]\nType=Application\nName=Other\nExec=AppRun\n",
        "AppRun": "#!/bin/sh\n"}, upd_info=UPD_INFO)
    shutil.copyfile(other, app.appimage_path)
    old = as_made_by_0_1(dataclasses.replace(app, size=os.stat(app.appimage_path).st_size))
    result = reconcile_app(old)
    assert result.change == "recorded"
    assert result.app.update_source is None and result.app.data_hints == []
    assert result.app.name == "Demo"


def test_a_file_that_cannot_be_inspected_still_gets_its_time_recorded(downloads, monkeypatch):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    old = as_made_by_0_1(app)
    from easy_installer.errors import ExtractionError

    def broken(path, **kw):
        raise ExtractionError("The file seems damaged.", details="unsquashfs failed")

    monkeypatch.setattr(reconcile, "inspect_appimage", broken)
    result = reconcile_app(old)
    assert result.change == "recorded" and result.app.mtime_ns > 0
    assert result.app.data_hints == [] and result.app.update_source is None


def test_a_stale_object_is_not_trusted(downloads):
    """A window may hold an older entry: the registry's current one is what counts."""
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"), InstallOptions(keep_backup=False))
    result = reconcile_app(v1)     # v1 says "1.0, other size" - but 2.0 is correctly installed
    assert result.change == "unchanged" and result.app == v2
    assert registry().get("demo") == v2


# ------------------------------------------------------------------------------------------------
# adopted: the updater saved the new version under another name
# ------------------------------------------------------------------------------------------------


def test_renamed_file_is_adopted(downloads, machine):
    machine.launcher = LAUNCHER
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0", args="--old %F"))
    apps = user_layout().apps_dir
    new_file = apps / "Demo-2.0-x86_64.AppImage"       # written by the app's own updater ...
    shutil.copyfile(demo(downloads / "new.AppImage", "2.0", args="--new %F",
                         color=(0, 200, 0, 255)), new_file)
    os.chmod(new_file, 0o644)
    Path(v1.appimage_path).unlink()                       # ... which then deleted the old file
    assert not needs_reconcile(v1) and v1.status() == "missing-appimage"

    result = reconcile_app(v1)
    assert (result.change, result.old_version, result.new_version) == ("adopted", "1.0", "2.0")
    app = result.app
    assert app.appimage_path == str(new_file) and new_file.is_file()      # it stays where it is
    assert os.stat(new_file).st_mode & 0o777 == 0o755
    assert sorted(p.name for p in apps.iterdir()) == ["Demo-2.0-x86_64.AppImage"]
    assert app.version == "2.0" and app.sha256 == sha256(new_file)
    assert app.mtime_ns == os.stat(new_file).st_mtime_ns and app.status() == "ok"
    assert app.installed_at == v1.installed_at and app.original_filename == v1.original_filename
    entry = launcher(app)
    assert split_exec(entry.get("Exec")) == [str(new_file), "--new", "%F"]
    assert entry.get("TryExec") == str(new_file)
    assert entry.get_list("Actions") == ["easyinstaller-uninstall"]
    assert Path(app.icon_paths[0]).read_bytes() == make_png(64, 64, (0, 200, 0, 255))
    assert registry().get("demo") == app and registry().owner_of(new_file) == "demo"
    assert app.previous is None and not user_layout().backups_dir.exists()
    assert reconcile_app(app).change == "unchanged"

    installer.uninstall("demo", Scope.USER)
    assert list(apps.iterdir()) == []


def test_adoption_needs_exactly_one_matching_file(downloads):
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    other = install(demo(downloads / "Other-1.0.AppImage", "1.0", name="Other", stem="other"))
    apps = user_layout().apps_dir
    Path(v1.appimage_path).unlink()
    before = registry().get("demo")

    # nothing else there / only other apps' files / things that are no AppImages
    assert reconcile_app(v1).change == "missing"
    shutil.copyfile(other.appimage_path, apps / "Unregistered-Other.AppImage")
    (apps / "notes.AppImage").write_text("not an app")
    (apps / "Readme.txt").write_text("hello")
    hidden = apps / ".Demo-hidden.AppImage"
    shutil.copyfile(demo(downloads / "h.AppImage", "5.0"), hidden)
    (apps / "Link.AppImage").symlink_to(hidden)
    (apps / "Folder.AppImage").mkdir()
    assert reconcile_app(v1).change == "missing"

    # two candidates: which one is it? - nobody can tell
    shutil.copyfile(demo(downloads / "a.AppImage", "2.0"), apps / "Demo-2.0.AppImage")
    shutil.copyfile(demo(downloads / "b.AppImage", "3.0"), apps / "Demo-3.0.AppImage")
    result = reconcile_app(v1)
    assert result.change == "missing" and result.app == before
    assert registry().get("demo") == before and registry().get("other") == other

    # one left: adopted; the registered file of another app is never a candidate
    (apps / "Demo-2.0.AppImage").unlink()
    result = reconcile_app(v1)
    assert result.change == "adopted" and result.app.version == "3.0"
    assert result.app.appimage_path == str(apps / "Demo-3.0.AppImage")
    assert registry().get("other") == other and Path(other.appimage_path).is_file()


def test_reconcile_all_reports_missing_and_adopts(downloads):
    gone = install(demo(downloads / "Gone-1.0.AppImage", "1.0", name="Gone", stem="gone"))
    moved = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    Path(gone.appimage_path).unlink()
    os.rename(moved.appimage_path, user_layout().apps_dir / "demo-latest.AppImage")
    results = {r.app.id: r.change for r in reconcile_all()}
    assert results == {"gone": "missing", "demo": "adopted"}
    assert registry().get("gone") == gone


# ------------------------------------------------------------------------------------------------
# foreign: the file is something else now
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("what", ["other-app", "garbage", "truncated"])
def test_foreign_file_changes_nothing(downloads, what, monkeypatch):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    target = Path(app.appimage_path)
    if what == "other-app":
        shutil.copyfile(demo(downloads / "Other.AppImage", "9.0", name="Other", stem="other"),
                        target)
    elif what == "garbage":
        target.write_bytes(b"<html>404 Not Found</html>" * 10)
    else:
        target.write_bytes(target.read_bytes()[:700])
    layout = user_layout()
    before = snapshot(layout.apps_dir, layout.desktop_dir, layout.icons_dir,
                      layout.registry_path.parent)
    assert needs_reconcile(app)

    result = reconcile_app(app)
    assert result.change == "foreign" and result.app == app
    assert result.old_version == result.new_version == "1.0"
    assert snapshot(layout.apps_dir, layout.desktop_dir, layout.icons_dir,
                    layout.registry_path.parent) == before
    assert registry().get("demo") == app

    # The same wrong file is not read again on every check (window focus, ...) ...
    calls = []
    real_inspect = reconcile.inspect_appimage
    monkeypatch.setattr(reconcile, "inspect_appimage",
                        lambda *a, **kw: calls.append(a) or real_inspect(*a, **kw))
    assert reconcile_app(app).change == "foreign" and calls == []
    assert [r.change for r in reconcile_all()] == ["foreign"] and calls == []
    # ... but it is once it changes again: here, the real update arrives
    shutil.copyfile(demo(downloads / "Demo-2.0.AppImage", "2.0"), target)
    result = reconcile_app(app)
    assert result.change == "updated" and result.app.version == "2.0" and len(calls) == 1


# ------------------------------------------------------------------------------------------------
# system scope: nothing is changed without the administrator
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


def system_app(layout, source: Path, **values) -> InstalledApp:
    """A system-wide installation as the helper leaves it (made by hand: no root here)."""
    target = layout.apps_dir / "Demo.AppImage"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    layout.desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop = layout.desktop_dir / "easyinstaller-demo.desktop"
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Demo\n"
                       f"Exec={target} %F\nX-AppImage-Version=1.0\n")
    st = target.stat()
    app = InstalledApp(id="demo", name="Demo", version="1.0", scope=Scope.SYSTEM,
                       appimage_path=str(target), desktop_path=str(desktop),
                       sha256=sha256(target), size=st.st_size,
                       **{"mtime_ns": st.st_mtime_ns, **values})
    Registry(layout.registry_path).put(app)
    return app


def test_system_app_that_changed_needs_the_administrator(downloads, sys_layout, system_root,
                                                         machine):
    app = system_app(sys_layout, demo(downloads / "Demo-1.0.AppImage", "1.0"))
    assert reconcile_app(app).change == "unchanged"

    shutil.copyfile(demo(downloads / "Demo-2.0.AppImage", "2.0", args="--new %F"),
                    app.appimage_path)
    before = snapshot(system_root)
    result = reconcile_app(app)
    assert result.change == "needs-admin" and result.app == app
    assert result.old_version == result.new_version == "1.0"
    assert snapshot(system_root) == before            # registry and launcher untouched
    assert machine.helper_calls == []                 # and no password prompt
    assert [(r.app.scope, r.change) for r in reconcile_all()] == [(Scope.SYSTEM, "needs-admin")]
    assert reconcile_all([Scope.USER]) == []

    # a missing system app is reported, never "adopted"
    shutil.copyfile(app.appimage_path, sys_layout.apps_dir / "Demo-2.0.AppImage")
    Path(app.appimage_path).unlink()
    assert reconcile_app(app).change == "missing"
    assert Registry(sys_layout.registry_path).get("demo") == app


def test_system_entry_made_by_0_1_is_left_alone(downloads, sys_layout, system_root):
    app = system_app(sys_layout, demo(downloads / "Demo-1.0.AppImage", "1.0"), mtime_ns=0)
    before = snapshot(system_root)
    assert reconcile_app(app).change == "unchanged"   # nobody may write the system registry
    assert snapshot(system_root) == before


# ------------------------------------------------------------------------------------------------
# robustness
# ------------------------------------------------------------------------------------------------


def test_one_broken_app_never_stops_the_others(downloads, monkeypatch):
    a = install(demo(downloads / "A-1.0.AppImage", "1.0", name="Aaa", stem="aaa"))
    b = install(demo(downloads / "B-1.0.AppImage", "1.0", name="Bbb", stem="bbb"))
    c = install(demo(downloads / "C-1.0.AppImage", "1.0", name="Ccc", stem="ccc"))
    self_update(a, demo(downloads / "A-2.0.AppImage", "2.0", name="Aaa", stem="aaa"))
    self_update(b, demo(downloads / "B-2.0.AppImage", "2.0", name="Bbb", stem="bbb"))
    self_update(c, demo(downloads / "C-2.0.AppImage", "2.0", name="Ccc", stem="ccc"))

    real = reconcile._reconcile
    seen = []

    def flaky(app, report):
        seen.append(app.id)
        if app.id == "aaa":
            raise RuntimeError("a bug triggered by this one file")
        if app.id == "bbb":
            raise OSError(5, "Input/output error")
        return real(app, report)

    monkeypatch.setattr(reconcile, "_reconcile", flaky)
    results = reconcile_all()
    assert seen == ["aaa", "bbb", "ccc"]
    assert [(r.app.id, r.change, r.app.version) for r in results] == [
        ("aaa", "unchanged", "1.0"), ("bbb", "unchanged", "1.0"), ("ccc", "updated", "2.0")]
    # reconcile_app itself never raises for the expected kinds of trouble
    assert reconcile_app(b).change == "unchanged"
    with pytest.raises(RuntimeError):
        reconcile_app(a)


def test_failed_refresh_leaves_everything_as_it_was(downloads, monkeypatch):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    self_update(app, demo(downloads / "Demo-2.0.AppImage", "2.0", args="--new %F"))
    layout = user_layout()
    before = snapshot(layout.apps_dir, layout.desktop_dir, layout.icons_dir,
                      layout.registry_path.parent)

    def full(path, text):
        raise OSError(28, "No space left on device", str(path))

    with monkeypatch.context() as patched:
        patched.setattr(installer, "_write_desktop_file", full)
        result = reconcile_app(app)
    assert result.change == "unchanged" and result.app == app
    assert snapshot(layout.apps_dir, layout.desktop_dir, layout.icons_dir,
                    layout.registry_path.parent) == before
    assert os.stat(app.appimage_path).st_mode & 0o777 == 0o755
    assert reconcile_app(app).change == "updated"     # and it works the next time


def test_file_that_is_still_being_written_is_tried_again_later(downloads, monkeypatch):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    newer = demo(downloads / "Demo-2.0.AppImage", "2.0")
    self_update(app, newer)
    real_hash = installer._file_sha256

    def hash_while_the_updater_writes(path):
        digest = real_hash(path)
        with open(path, "ab") as fh:        # the download goes on
            fh.write(b"more")
        return digest

    with monkeypatch.context() as patched:
        patched.setattr(installer, "_file_sha256", hash_while_the_updater_writes)
        result = reconcile_app(app)
    assert result.change == "unchanged" and registry().get("demo") == app
    shutil.copyfile(newer, app.appimage_path)   # the updater is done
    result = reconcile_app(app)
    assert result.change == "updated" and result.app.sha256 == sha256(newer)


def test_reconcile_does_not_wait_long_for_a_running_installation(downloads, monkeypatch):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    self_update(app, demo(downloads / "Demo-2.0.AppImage", "2.0"))
    monkeypatch.setattr(installer, "UNATTENDED_LOCK_TIMEOUT", 0.3)
    lock = Path(str(user_layout().registry_path) + ".lock")
    fd = os.open(lock, os.O_RDONLY)             # another installation (e.g. at its password prompt)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = reconcile_app(app)
    finally:
        os.close(fd)
    assert result.change == "unchanged" and registry().get("demo") == app
    assert reconcile_app(app).change == "updated"


def test_other_kinds_of_apps_are_left_alone(downloads):
    app = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    portable = dataclasses.replace(app, kind="portable", size=1)
    registry().put(portable)
    assert reconcile_app(portable).change == "unchanged"
    assert registry().get("demo") == portable
