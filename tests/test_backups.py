"""The kept previous version: backup on update, rollback, drop, prune - and what must never happen.

Data safety is the point of these tests: a failed update never loses both versions, going back
swaps the two versions or changes nothing, and nothing outside an app's own backup folder is
ever moved or deleted.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from easy_installer.core import backups, installer, privileged
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    ACTION_ROLLBACK,
    InstallOptions,
    backups_dir,
    drop_backup,
    execute_install,
    plan_install,
    plan_rollback,
    prune_backups,
    rollback,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.reconcile import reconcile_all
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.settings import Settings, save_settings
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import InstallError, NotInstalledError
from easy_installer.helper import ops

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs

pytestmark = [requires_mksquashfs, requires_unsquashfs]

DAY = 24 * 3600


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
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def demo(path: Path, version: str | None, *, name: str = "Demo", stem: str = "demo",
         args: str | None = None, tag: str = "") -> Path:
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\n"
               f"Exec=AppRun {args if args is not None else '--v' + str(version)} %F\n"
               f"Icon={stem}\nCategories=Utility;\n")
    if version:
        desktop += f"X-AppImage-Version={version}\n"
    return make_fake_appimage(path, {
        f"{stem}.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "payload.bin": f"{name} {version} {tag}".encode() * 3,
        f"{stem}.png": make_png(32, 32),
    })


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def tree(root: Path) -> list[str]:
    """Everything below ``root`` (relative, folders with a trailing slash)."""
    return sorted(str(p.relative_to(root)) + ("/" if p.is_dir() and not p.is_symlink() else "")
                  for p in root.rglob("*"))


def snapshot(*roots: Path) -> dict[str, bytes]:
    found = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.name.endswith(".lock"):
                found[str(path)] = path.read_bytes()
    return found


def everything() -> dict[str, bytes]:
    layout = user_layout()
    return snapshot(layout.apps_dir, layout.desktop_dir, layout.icons_dir,
                    layout.registry_path.parent)


def exec_args(app: InstalledApp) -> list[str]:
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))
    return split_exec(entry.get("Exec"))


# ------------------------------------------------------------------------------------------------
# a backup is made when an update replaces a different file
# ------------------------------------------------------------------------------------------------


def test_update_keeps_the_replaced_version(downloads, monkeypatch):
    layout = user_layout()
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    assert v1.previous is None and not backups_dir(layout).exists()
    old_sha, old_size, old_inode = v1.sha256, v1.size, os.stat(v1.appimage_path).st_ino

    # the old version is moved, never copied
    def no_copy(*args, **kwargs):
        raise AssertionError("a backup must not be copied")

    monkeypatch.setattr(installer, "_copy_with_progress", no_copy)
    monkeypatch.setattr(shutil, "copyfile", no_copy)
    monkeypatch.setattr(shutil, "copy2", no_copy)
    with inspect_appimage(demo(downloads / "Demo-1.1-x86_64.AppImage", "1.1")) as info:
        plan = plan_install(info)
        expected = layout.apps_dir / ".easyinstaller-backups" / "demo" / "Demo-1.0.AppImage"
        assert plan.action == "update" and plan.keep_backup is True
        assert plan.backup_target == expected
        assert backups_dir(layout) == layout.apps_dir / ".easyinstaller-backups" == layout.backups_dir
        before = time.time()
        v2 = execute_install(plan)

    assert v2.version == "1.1" and Path(v2.appimage_path) == Path(v1.appimage_path)
    assert tree(layout.apps_dir) == [".easyinstaller-backups/", ".easyinstaller-backups/demo/",
                                     ".easyinstaller-backups/demo/Demo-1.0.AppImage",
                                     "Demo.AppImage"]
    assert sha256(expected) == old_sha and sha256(v2.appimage_path) == v2.sha256 != old_sha
    st = os.lstat(expected)
    assert st.st_ino == old_inode and st.st_nlink == 1      # the very file, under its only name
    assert st.st_mode & 0o777 == 0o755
    assert v2.previous == {"version": "1.0", "path": str(expected), "sha256": old_sha,
                           "size": old_size, "saved_at": v2.previous["saved_at"],
                           "original_filename": v1.original_filename}
    saved = backups.saved_time(v2.previous)
    assert before - 2 <= saved <= time.time() + 2
    assert registry().get("demo") == v2
    assert backups.usable_previous(layout, v2) == v2.previous


def test_only_one_backup_per_app(downloads):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    install(demo(downloads / "Demo-1.1.AppImage", "1.1"))
    v3 = install(demo(downloads / "Demo-1.2.AppImage", "1.2", name="Demo Studio"))   # new name
    folder = backups_dir(layout) / "demo"
    assert [p.name for p in folder.iterdir()] == ["Demo-1.1.AppImage"]
    assert v3.previous["version"] == "1.1" and v3.previous["path"] == str(folder / "Demo-1.1.AppImage")
    assert sorted(p.name for p in layout.apps_dir.iterdir()) == [".easyinstaller-backups",
                                                                 "Demo-Studio.AppImage"]
    # a downgrade keeps the newer version the same way
    v4 = install(demo(downloads / "Demo-1.0b.AppImage", "1.0", tag="again"))
    assert [p.name for p in folder.iterdir()] == ["Demo-Studio-1.2.AppImage"]
    assert v4.previous["version"] == "1.2" and sha256(v4.previous["path"]) == v3.sha256


def test_backup_name_without_a_version_and_name_clashes(downloads):
    layout = user_layout()
    first = install(demo(downloads / "demo-latest.AppImage", None))
    assert first.version is None
    second = install(demo(downloads / "demo-latest.AppImage", None, tag="newer"))
    name = f"Demo-{first.sha256[:8]}.AppImage"
    assert second.previous["path"] == str(backups_dir(layout) / "demo" / name)
    assert second.previous["version"] is None

    # two builds with the same version: the second backup gets a free name first, then the
    # older one goes
    a = install(demo(downloads / "Demo-2.0.AppImage", "2.0", tag="a"))
    b = install(demo(downloads / "Demo-2.0.AppImage", "2.0", tag="b"))
    assert Path(b.previous["path"]).name == "Demo-2.0.AppImage" and b.previous["sha256"] == a.sha256
    blocker = backups_dir(layout) / "demo" / "Demo-2.0.AppImage"
    c = install(demo(downloads / "Demo-2.0.AppImage", "2.0", tag="c"))
    assert Path(c.previous["path"]).name == "Demo-2.0-2.AppImage" and not blocker.exists()
    assert sha256(c.previous["path"]) == b.sha256


@pytest.mark.parametrize("case", ["first-install", "same-file-in-place", "same-content-copy",
                                  "option-off", "settings-off", "old-file-missing",
                                  "old-file-shared"])
def test_when_no_backup_is_made(downloads, case):
    layout = user_layout()
    options = None
    if case == "first-install":
        source = demo(downloads / "Demo-1.0.AppImage", "1.0")
    else:
        v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
        source = demo(downloads / "Demo-1.1.AppImage", "1.1")
        if case == "same-file-in-place":
            source = Path(v1.appimage_path)
        elif case == "same-content-copy":
            source = downloads / "again.AppImage"
            shutil.copyfile(v1.appimage_path, source)
        elif case == "option-off":
            options = InstallOptions(keep_backup=False)
        elif case == "settings-off":
            save_settings(Settings(backup_days=0))
        elif case == "old-file-missing":
            Path(v1.appimage_path).unlink()
        elif case == "old-file-shared":
            registry().put(dataclasses.replace(
                v1, id="other", desktop_path=v1.desktop_path.replace("demo", "other"),
                icon_paths=[]))
            source = demo(downloads / "Demo-1.1.AppImage", "1.1", name="Demo New")
    with inspect_appimage(source) as info:
        plan = plan_install(info, options)
        assert plan.backup_target is None
        assert plan.keep_backup is (case not in ("option-off", "settings-off"))
        app = execute_install(plan)
    assert app.previous is None
    assert not backups_dir(layout).exists()
    if case == "old-file-shared":
        assert Path(v1.appimage_path).is_file()    # the other entry still starts it


def test_update_without_backup_forgets_the_older_one(downloads):
    """``previous`` never describes a version older than the one that was just replaced."""
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-1.1.AppImage", "1.1"))
    assert Path(v2.previous["path"]).is_file()
    v3 = install(demo(downloads / "Demo-1.2.AppImage", "1.2"), InstallOptions(keep_backup=False))
    assert v3.previous is None and not backups_dir(layout).exists()
    assert [p.name for p in layout.apps_dir.iterdir()] == ["Demo.AppImage"]


def test_reinstalling_the_same_file_keeps_the_backup(downloads):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-1.1.AppImage", "1.1"))
    Path(v2.desktop_path).unlink()
    repaired = install(Path(v2.appimage_path), InstallOptions(keep_original=True))   # "Repair"
    assert repaired.previous == v2.previous and Path(v2.previous["path"]).is_file()
    copy = downloads / "same.AppImage"
    shutil.copyfile(v2.appimage_path, copy)
    again = install(copy, InstallOptions(keep_backup=False))     # the same content once more
    assert again.previous == v2.previous and Path(v2.previous["path"]).is_file()


# ------------------------------------------------------------------------------------------------
# a failed update never loses a version
# ------------------------------------------------------------------------------------------------


def _fail(monkeypatch, where: str) -> None:
    if where == "desktop":
        def boom(path, text):
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        monkeypatch.setattr(installer, "_write_desktop_file", boom)
    elif where == "registry":
        def broken_put(self, app):
            raise RuntimeError("registry exploded")
        monkeypatch.setattr(Registry, "put", broken_put)
    elif where == "icon":
        def no_icon(src, dst):
            raise OSError(errno.EIO, "Input/output error", str(dst))
        monkeypatch.setattr(installer, "_copy_icon", no_icon)
    else:  # placing the new file
        def no_rename(src, dst):
            raise OSError(errno.EIO, "Input/output error", str(dst))
        monkeypatch.setattr(installer, "_rename", no_rename)


@pytest.mark.parametrize("where", ["place", "icon", "desktop", "registry"])
@pytest.mark.parametrize("hard_links", [True, False])
@pytest.mark.parametrize("new_name", [False, True])
def test_failed_update_restores_everything(downloads, monkeypatch, where, hard_links, new_name):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-1.1.AppImage", "1.1"))   # there is an older backup (1.0)
    before = everything()
    assert any("Demo-1.0.AppImage" in path for path in before)
    source = demo(downloads / "Demo-1.2.AppImage", "1.2", name="Demo Two" if new_name else "Demo")
    source_bytes = source.read_bytes()

    with monkeypatch.context() as patched:
        if not hard_links:
            def no_links(src, dst):
                raise OSError(errno.EPERM, "Operation not permitted")
            patched.setattr(backups, "_link", no_links)
        with inspect_appimage(source) as info:
            plan = plan_install(info)
            assert plan.backup_target is not None
            _fail(patched, where)
            with pytest.raises((InstallError, RuntimeError)):
                execute_install(plan)

    assert everything() == before                     # files, launcher, icon, registry
    assert registry().get("demo") == v2
    assert source.read_bytes() == source_bytes        # and the download is back where it was
    assert tree(user_layout().backups_dir) == ["demo/", "demo/Demo-1.0.AppImage"]
    # afterwards the update simply works
    v3 = install(source)
    assert v3.version == "1.2" and v3.previous["version"] == "1.1"
    assert sha256(v3.previous["path"]) == v2.sha256


def test_a_killed_update_still_leaves_a_working_consistent_app(downloads, monkeypatch):
    """Power loss after the new file is in place but before the registry is saved: nothing is
    rolled back. One version works, the old one is not lost, and the registry is brought in
    line with the file by the next reconcile."""
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    with inspect_appimage(demo(downloads / "Demo-2.0.AppImage", "2.0")) as info:
        plan = plan_install(info)

        def killed(self, app):
            raise SystemExit("killed")

        with monkeypatch.context() as patched:
            patched.setattr(Registry, "put", killed)
            patched.setattr(installer._Transaction, "rollback", lambda self: None)
            with pytest.raises(SystemExit):
                execute_install(plan)
    layout = user_layout()
    target = Path(v1.appimage_path)
    assert registry().get("demo") == v1                      # still the old entry ...
    assert target.is_file() and sha256(target) != v1.sha256  # ... but the new file is there
    kept = backups_dir(layout) / "demo" / "Demo-1.0.AppImage"
    assert sha256(kept) == v1.sha256                         # and the old version is not lost

    results = reconcile_all([Scope.USER])
    assert [(r.change, r.app.version) for r in results] == [("updated", "2.0")]
    app = registry().get("demo")
    assert app.version == "2.0" and app.sha256 == sha256(target) and app.status() == "ok"
    assert exec_args(app) == [str(target), "--v2.0", "%F"]
    # the version that was already kept is found again: the update ends up complete
    assert app.previous["path"] == str(kept) and app.previous["version"] == "1.0"
    assert app.previous["sha256"] == v1.sha256 == sha256(kept)
    leftovers = [p.name for d in (layout.apps_dir, layout.desktop_dir) for p in d.iterdir()
                 if ".easyinstaller-old-" in p.name or p.name.endswith(".part")]
    assert leftovers == []
    assert rollback("demo", Scope.USER).version == "1.0"


def test_a_killed_rollback_still_leaves_a_working_consistent_app(downloads, monkeypatch):
    """The same for going back: killed after the old version is in place again."""
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    plan = plan_rollback("demo", Scope.USER)
    try:
        def killed(self, app):
            raise SystemExit("killed")

        with monkeypatch.context() as patched:
            patched.setattr(Registry, "put", killed)
            patched.setattr(installer._Transaction, "rollback", lambda self: None)
            with pytest.raises(SystemExit):
                execute_install(plan)
    finally:
        plan.info.cleanup()
    layout = user_layout()
    target = Path(v2.appimage_path)
    assert registry().get("demo") == v2                       # says 2.0, previous = a file ...
    assert not Path(v2.previous["path"]).exists()             # ... that was moved into place
    assert sha256(target) == v1.sha256                        # 1.0 works
    assert backups.usable_previous(layout, v2) is None
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.USER)                          # nothing silly happens meanwhile

    results = reconcile_all([Scope.USER])
    assert [(r.change, r.old_version, r.new_version) for r in results] == [("updated", "2.0", "1.0")]
    app = registry().get("demo")
    assert app.version == "1.0" and app.sha256 == v1.sha256 and app.status() == "ok"
    assert app.previous["version"] == "2.0" and sha256(app.previous["path"]) == v2.sha256
    assert tree(layout.apps_dir) == [".easyinstaller-backups/", ".easyinstaller-backups/demo/",
                                     ".easyinstaller-backups/demo/Demo-2.0.AppImage",
                                     "Demo.AppImage"]
    assert rollback("demo", Scope.USER).version == "2.0"      # and both directions still work


def test_find_unrecorded_only_accepts_the_exact_version(downloads):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    folder = backups_dir(layout) / "demo"
    assert backups.find_unrecorded(layout, v2) is None        # only the recorded 1.0 is there
    same_size = folder / "Other.AppImage"
    same_size.write_bytes(b"x" * v2.size)
    (folder / "link.AppImage").symlink_to(v2.appimage_path)
    (folder / "dir.AppImage").mkdir()
    assert backups.find_unrecorded(layout, v2) is None
    copy = folder / "Demo-2.0.AppImage"
    shutil.copyfile(v2.appimage_path, copy)
    found = backups.find_unrecorded(layout, v2)
    assert found == {"version": "2.0", "path": str(copy), "sha256": v2.sha256, "size": v2.size,
                     "saved_at": found["saved_at"], "original_filename": v2.original_filename}
    assert backups.find_unrecorded(layout, dataclasses.replace(v2, sha256=None)) is None
    assert backups.find_unrecorded(layout, dataclasses.replace(v2, id="other")) is None


def test_no_hard_links_falls_back_to_moving(downloads, monkeypatch):
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    inode = os.stat(v1.appimage_path).st_ino

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(backups, "_link", no_links)
    v2 = install(demo(downloads / "Demo-1.1.AppImage", "1.1"))
    kept = Path(v2.previous["path"])
    assert os.stat(kept).st_ino == inode and sha256(kept) == v1.sha256
    assert sha256(v2.appimage_path) == v2.sha256


def test_backup_folder_on_another_file_system_means_no_backup(downloads, monkeypatch):
    """Nothing is ever copied across file systems for a backup: the update goes on with a note."""
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    layout = user_layout()

    def cross_device(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(backups, "_link", cross_device)
    monkeypatch.setattr(backups, "_move", cross_device)
    with inspect_appimage(demo(downloads / "Demo-1.1.AppImage", "1.1")) as info:
        plan = plan_install(info)
        v2 = execute_install(plan)
    assert v2.version == "1.1" and v2.previous is None
    assert plan.warnings == ["The previous version could not be kept, so you cannot go back to it."]
    assert [p.name for p in layout.apps_dir.iterdir()] == ["Demo.AppImage"]   # no empty folders


def test_an_older_backup_is_not_thrown_away_when_the_newer_one_cannot_be_kept(downloads,
                                                                             monkeypatch):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-1.1.AppImage", "1.1"))

    def impossible(src, dst):
        raise OSError(errno.EACCES, "Permission denied")

    with monkeypatch.context() as patched:
        patched.setattr(backups, "_link", impossible)
        patched.setattr(backups, "_move", impossible)
        v3 = install(demo(downloads / "Demo-1.2.AppImage", "1.2"))
    assert v3.version == "1.2" and v3.previous == v2.previous     # still 1.0: better than nothing
    assert Path(v3.previous["path"]).is_file()
    assert tree(user_layout().backups_dir) == ["demo/", "demo/Demo-1.0.AppImage"]


@pytest.mark.parametrize("planted", ["backups-link", "app-folder-link", "backups-file"])
def test_symlinks_in_the_way_are_never_followed(downloads, planted, isolated_env):
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    layout = user_layout()
    outside = isolated_env / "Documents"
    outside.mkdir()
    (outside / "thesis.odt").write_text("precious")
    root = backups_dir(layout)
    if planted == "backups-link":
        root.symlink_to(outside, target_is_directory=True)
    elif planted == "app-folder-link":
        root.mkdir()
        (root / "demo").symlink_to(outside, target_is_directory=True)
    else:
        root.write_text("a file with the folder's name")
    with inspect_appimage(demo(downloads / "Demo-1.1.AppImage", "1.1")) as info:
        plan = plan_install(info)
        v2 = execute_install(plan)
    assert v2.version == "1.1" and v2.previous is None
    assert any("could not be kept" in w for w in plan.warnings)
    assert sorted(p.name for p in outside.iterdir()) == ["thesis.odt"]
    assert (outside / "thesis.odt").read_text() == "precious"
    # and whatever is recorded there is neither usable nor deletable
    tampered = dataclasses.replace(v2, previous={
        "version": "1.0", "path": str(root / "demo" / "thesis.odt"), "sha256": v1.sha256,
        "size": 1, "saved_at": "2026-01-01T00:00:00Z"})
    registry().put(tampered)
    assert backups.usable_previous(layout, tampered) is None
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.USER)
    drop_backup("demo", Scope.USER)
    uninstall("demo", Scope.USER)
    assert (outside / "thesis.odt").read_text() == "precious"


# ------------------------------------------------------------------------------------------------
# rollback
# ------------------------------------------------------------------------------------------------


def test_rollback_swaps_the_two_versions(downloads):
    layout = user_layout()
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0", args="--one"),
                 InstallOptions(extract_and_run=True))
    v2 = install(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0", args="--two"),
                 InstallOptions(extract_and_run=True))
    assert exec_args(v2)[-2:] == ["--two", "%F"]

    plan = plan_rollback("demo", Scope.USER)
    try:
        assert plan.action == ACTION_ROLLBACK == "rollback"
        assert plan.existing == v2 and plan.version == "1.0" and plan.consumes_backup
        assert plan.info.path == Path(v2.previous["path"])
        assert plan.backup_target == backups_dir(layout) / "demo" / "Demo-2.0.AppImage"
        assert not any("older version" in w for w in plan.warnings)
    finally:
        plan.info.cleanup()

    events = []
    back = rollback("demo", Scope.USER, progress=lambda f, m: events.append(f))
    assert events[-1] == 1.0
    assert back.version == "1.0" and back.sha256 == v1.sha256
    assert sha256(back.appimage_path) == v1.sha256 and back.appimage_path == v1.appimage_path
    assert back.previous["version"] == "2.0" and back.previous["sha256"] == v2.sha256
    assert sha256(back.previous["path"]) == v2.sha256
    assert tree(layout.apps_dir) == [".easyinstaller-backups/", ".easyinstaller-backups/demo/",
                                     ".easyinstaller-backups/demo/Demo-2.0.AppImage",
                                     "Demo.AppImage"]
    # the launcher is the old version's again; the installation's choices are kept
    assert exec_args(back) == ["env", "APPIMAGE_EXTRACT_AND_RUN=1", back.appimage_path, "--one",
                               "%F"]
    assert back.extract_and_run and back.extract_and_run_explicit
    assert back.installed_at == v1.installed_at
    # (PORT-10/CLI-8) the file the kept version was installed from, not the one just left
    assert back.original_filename == v1.original_filename == "Demo-1.0-x86_64.AppImage"
    assert back.previous["original_filename"] == "Demo-2.0-x86_64.AppImage"
    assert back.mtime_ns == os.stat(back.appimage_path).st_mtime_ns
    assert registry().get("demo") == back and back.status() == "ok"

    # going back once more returns to 2.0
    forth = rollback("demo", Scope.USER)
    assert forth.version == "2.0" and sha256(forth.appimage_path) == v2.sha256
    assert forth.original_filename == "Demo-2.0-x86_64.AppImage"
    assert forth.previous["version"] == "1.0" and sha256(forth.previous["path"]) == v1.sha256
    assert [p.name for p in (backups_dir(layout) / "demo").iterdir()] == ["Demo-1.0.AppImage"]
    assert exec_args(forth)[-2:] == ["--two", "%F"]
    # a version kept by 0.2.0 does not tell its file name: none rather than a wrong one
    registry().put(dataclasses.replace(forth, previous={
        k: v for k, v in forth.previous.items() if k != "original_filename"}))
    assert not rollback("demo", Scope.USER).original_filename


def test_backups_work_in_an_oddly_spelled_apps_folder(downloads, isolated_env, monkeypatch):
    """E.g. EASY_INSTALLER_APPS_DIR=/home/u/x/../My Apps/ - recorded and checked as spelled."""
    (isolated_env / "x").mkdir()
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", f"{isolated_env}/x/../My Apps (100%)/")
    layout = user_layout()
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    assert (isolated_env / "My Apps (100%)" / ".easyinstaller-backups" / "demo" /
            "Demo-1.0.AppImage").is_file()
    assert backups.usable_previous(layout, v2) == v2.previous
    back = rollback("demo", Scope.USER)
    assert back.version == "1.0" and sha256(back.appimage_path) == v1.sha256
    assert sha256(back.previous["path"]) == v2.sha256
    assert prune_backups(0) == [dataclasses.replace(back, previous=None)]
    uninstall("demo", Scope.USER)
    assert list((isolated_env / "My Apps (100%)").iterdir()) == []


def test_rollback_to_a_version_with_another_name(downloads):
    layout = user_layout()
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0", name="Demo Studio"))
    assert Path(v2.appimage_path).name == "Demo-Studio.AppImage"
    back = rollback("demo", Scope.USER)
    assert Path(back.appimage_path).name == "Demo.AppImage" and back.name == "Demo"
    assert sha256(back.appimage_path) == v1.sha256
    assert tree(layout.apps_dir) == [".easyinstaller-backups/", ".easyinstaller-backups/demo/",
                                     ".easyinstaller-backups/demo/Demo-Studio-2.0.AppImage",
                                     "Demo.AppImage"]
    assert back.previous["version"] == "2.0"


def test_rollback_of_a_version_whose_file_does_not_tell_it(downloads):
    """E.g. FreeCAD: the version is only in the download's name; the backup record knows it."""
    install(demo(downloads / "Demo-1.0.AppImage", None))
    assert registry().get("demo").version == "1.0"
    install(demo(downloads / "Demo-2.0.AppImage", None, tag="two"))
    back = rollback("demo", Scope.USER)
    assert back.version == "1.0" and back.previous["version"] == "2.0"


def test_rollback_errors_change_nothing(downloads):
    with pytest.raises(NotInstalledError):
        rollback("demo", Scope.USER)
    with pytest.raises(NotInstalledError):
        plan_rollback("demo", Scope.USER)
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    with pytest.raises(InstallError, match="no previous version of Demo"):
        rollback("demo", Scope.USER)
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    kept = Path(v2.previous["path"])

    # the kept file is something else now (another app, or damaged)
    original = kept.read_bytes()
    shutil.copyfile(demo(downloads / "Other.AppImage", "9", name="Other", stem="other"), kept)
    before = everything()
    with pytest.raises(InstallError, match="cannot be used any more"):
        rollback("demo", Scope.USER)
    kept.write_bytes(b"garbage")
    with pytest.raises(InstallError):
        rollback("demo", Scope.USER)
    kept.write_bytes(original)
    assert registry().get("demo") == v2 and sha256(v2.appimage_path) == v2.sha256
    assert {k: v for k, v in before.items() if k != str(kept)} == \
        {k: v for k, v in everything().items() if k != str(kept)}

    # the kept file is gone
    kept.unlink()
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.USER)
    assert registry().get("demo") == v2
    assert v1.sha256 != v2.sha256


@pytest.mark.parametrize("where", ["place", "desktop", "registry"])
@pytest.mark.parametrize("hard_links", [True, False])
def test_failed_rollback_keeps_both_versions(downloads, monkeypatch, where, hard_links):
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    before = everything()
    with monkeypatch.context() as patched:
        if not hard_links:
            def no_links(src, dst):
                raise OSError(errno.EPERM, "Operation not permitted")
            patched.setattr(backups, "_link", no_links)
        plan = plan_rollback("demo", Scope.USER)
        try:
            _fail(patched, where)
            with pytest.raises((InstallError, RuntimeError)):
                execute_install(plan)
        finally:
            plan.info.cleanup()
    assert everything() == before
    assert registry().get("demo") == v2
    assert sha256(v2.appimage_path) == v2.sha256 and sha256(v2.previous["path"]) == v1.sha256
    assert rollback("demo", Scope.USER).version == "1.0"


def test_rollback_refuses_when_the_current_version_cannot_be_kept(downloads, monkeypatch):
    """Going back is a swap: if the current version cannot be kept, nothing is changed."""
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    before = everything()

    def impossible(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(backups, "_link", impossible)
    monkeypatch.setattr(backups, "_move", impossible)
    with pytest.raises(InstallError, match="could not be kept, so nothing was changed"):
        rollback("demo", Scope.USER)
    assert everything() == before and registry().get("demo") == v2


# ------------------------------------------------------------------------------------------------
# drop, prune, uninstall
# ------------------------------------------------------------------------------------------------


def test_drop_backup(downloads, isolated_env):
    layout = user_layout()
    with pytest.raises(NotInstalledError):
        drop_backup("demo", Scope.USER)
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    drop_backup("demo", Scope.USER)                       # nothing kept: nothing happens
    assert registry().get("demo") == v1
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    drop_backup("demo", Scope.USER)
    assert registry().get("demo") == dataclasses.replace(v2, previous=None)
    assert [p.name for p in layout.apps_dir.iterdir()] == ["Demo.AppImage"]
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.USER)

    # A record pointing somewhere else is forgotten; the file it names is never deleted.
    precious = isolated_env / "Documents" / "thesis.AppImage"
    precious.parent.mkdir()
    precious.write_text("precious")
    other_backup = backups_dir(layout) / "other" / "Other-1.0.AppImage"
    other_backup.parent.mkdir(parents=True)
    other_backup.write_text("another app's backup")
    for path in (precious, other_backup, backups_dir(layout) / "demo" / ".." / "other" /
                 "Other-1.0.AppImage", Path("relative.AppImage")):
        registry().put(dataclasses.replace(v2, previous={
            "version": "0", "path": str(path), "sha256": None, "size": 1,
            "saved_at": "2026-01-01T00:00:00Z"}))
        assert backups.usable_previous(layout, registry().get("demo")) is None
        drop_backup("demo", Scope.USER)
        assert registry().get("demo").previous is None
        assert precious.read_text() == "precious"
        assert other_backup.read_text() == "another app's backup"


def test_prune_backups(downloads, monkeypatch):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    fresh = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    install(demo(downloads / "Old-1.0.AppImage", "1.0", name="Old", stem="old"))
    old = install(demo(downloads / "Old-2.0.AppImage", "2.0", name="Old", stem="old"))
    install(demo(downloads / "Gone-1.0.AppImage", "1.0", name="Gone", stem="gone"))
    gone = install(demo(downloads / "Gone-2.0.AppImage", "2.0", name="Gone", stem="gone"))

    def aged(app: InstalledApp, days: float) -> InstalledApp:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * DAY))
        changed = dataclasses.replace(app, previous={**app.previous, "saved_at": stamp})
        registry().put(changed)
        return changed

    fresh = aged(fresh, 13.9)
    old = aged(old, 14.1)
    Path(gone.previous["path"]).unlink()              # deleted by hand
    assert prune_backups(14, [Scope.SYSTEM]) == []    # the helper prunes those
    pruned = prune_backups(14)
    assert sorted(app.id for app in pruned) == ["gone", "old"]
    assert all(app.previous is None for app in pruned)
    assert registry().get("old") == dataclasses.replace(old, previous=None)
    assert registry().get("gone").previous is None
    assert registry().get("demo") == fresh and Path(fresh.previous["path"]).is_file()
    assert tree(backups_dir(layout)) == ["demo/", "demo/Demo-1.0.AppImage"]
    assert prune_backups(14) == []

    # a date that cannot be read: judged by when the file got there (just now: kept)
    odd = dataclasses.replace(fresh, previous={**fresh.previous, "saved_at": "last week"})
    registry().put(odd)
    assert prune_backups(14) == [] and Path(odd.previous["path"]).is_file()
    assert backups.is_expired(odd.previous, 14, now=time.time() + 15 * DAY) is True

    # 0 days ("do not keep previous versions"): everything goes, folders included
    assert [app.id for app in prune_backups(0)] == ["demo"]
    assert not backups_dir(layout).exists()
    assert all(app.previous is None for app in registry().all())
    assert prune_backups(0) == []


def test_prune_removes_old_unreferenced_files_only(downloads):
    """Backups nobody refers to (an interrupted update, a registry rewritten by version 0.1)
    go once they are older than the limit - and nothing else does."""
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    root = backups_dir(layout)
    orphan = root / "demo" / "Demo-0.9.AppImage"
    orphan.write_bytes(b"left behind")
    orphan_dir = root / "uninstalled-app"
    orphan_dir.mkdir()
    (orphan_dir / "X-1.0.AppImage").write_bytes(b"x")
    (orphan_dir / "X-folder").mkdir()
    (orphan_dir / "X-folder" / "file").write_text("portable app backup")
    stray = root / "not an id!"
    stray.mkdir()
    (stray / "keep.txt").write_text("not ours")
    (root / "note.txt").write_text("not ours either")

    assert backups.prune(layout, registry(), 14) == []              # all younger than 14 days
    assert orphan.is_file() and (orphan_dir / "X-folder" / "file").is_file()

    later = time.time() + 15 * DAY
    changed = backups.prune(layout, registry(), 14, now=later)
    assert [app.id for app in changed] == ["demo"]                  # its own backup is old now too
    assert tree(root) == ["not an id!/", "not an id!/keep.txt", "note.txt"]
    assert registry().get("demo") == dataclasses.replace(v2, previous=None)


def test_prune_does_not_wait_for_a_running_installation(downloads):
    import fcntl

    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    layout = user_layout()
    fd = os.open(str(layout.registry_path) + ".lock", os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert backups.prune(layout, registry(), 0, lock_timeout=0.3) == []
    finally:
        os.close(fd)
    assert registry().get("demo") == v2 and Path(v2.previous["path"]).is_file()


def test_prune_without_anything_creates_nothing(isolated_env):
    assert prune_backups(14) == []
    assert not user_layout().registry_path.parent.exists()


def test_uninstall_removes_the_backup(downloads):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    install(demo(downloads / "Other-1.0.AppImage", "1.0", name="Other", stem="other"))
    other = install(demo(downloads / "Other-2.0.AppImage", "2.0", name="Other", stem="other"))
    (backups_dir(layout) / "demo" / "orphan.AppImage").write_bytes(b"x")
    uninstall("demo", Scope.USER)
    assert tree(backups_dir(layout)) == ["other/", "other/Other-1.0.AppImage"]
    assert registry().get("other") == other
    uninstall("other", Scope.USER)
    assert list(layout.apps_dir.iterdir()) == []       # the backups folder itself is gone, too


def test_backups_of_kept_copies_are_independent(downloads):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    install(demo(downloads / "Demo-1.1.AppImage", "1.1"))                       # backup of demo
    copy = install(demo(downloads / "Demo-2.0.AppImage", "2.0"), InstallOptions(keep_both=True))
    shutil.copyfile(copy.appimage_path, downloads / "copy.AppImage")
    assert copy.previous is None
    # replace the copy's file by another build of the same version: the copy gets its own backup
    rebuilt = install(demo(downloads / "Demo-2.0.AppImage", "2.0", tag="rebuilt"),
                      InstallOptions(keep_both=True))
    assert rebuilt.id == copy.id == "demo--2.0"
    assert tree(backups_dir(layout)) == ["demo--2.0/", "demo--2.0/Demo-2.0-2.0.AppImage",
                                         "demo/", "demo/Demo-1.0.AppImage"]
    main = registry().get("demo")
    assert main.previous["version"] == "1.0" and rebuilt.previous["sha256"] == copy.sha256

    back = rollback("demo--2.0", Scope.USER)
    assert back.sha256 == copy.sha256 and (back.base_id, back.pinned) == ("demo", True)
    assert registry().get("demo") == main and Path(main.previous["path"]).is_file()
    drop_backup("demo", Scope.USER)
    # (the rebuilt 2.0 was kept next to the first 2.0 for a moment, hence the number)
    assert tree(backups_dir(layout)) == ["demo--2.0/", "demo--2.0/Demo-2.0-2.0-2.AppImage"]
    assert sha256(back.previous["path"]) == rebuilt.sha256
    uninstall("demo--2.0", Scope.USER)
    assert not backups_dir(layout).exists()
    assert registry().get("demo").previous is None


def test_leftover_cleanup_never_touches_the_backups_folder(downloads):
    """Names that look like an interrupted installation's leftovers are left alone in there."""
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    folder = backups_dir(layout) / "demo"
    lookalikes = [folder / ".Demo-1.0.AppImage.part",
                  folder / ".Demo-1.0.AppImage.easyinstaller-old-0badc0de",
                  backups_dir(layout) / ".Demo.AppImage.easyinstaller-old-12345678",
                  backups_dir(layout) / ".Demo.AppImage.part"]
    for path in lookalikes:
        path.write_bytes(b"keep")
    installer._clean_leftovers([layout.apps_dir, backups_dir(layout), folder, layout.desktop_dir],
                               registry())
    install(demo(downloads / "Other-1.0.AppImage", "1.0", name="Other", stem="other"))
    uninstall("other", Scope.USER)
    install(Path(v2.appimage_path))   # a repair of the app itself
    assert all(path.read_bytes() == b"keep" for path in lookalikes)
    assert (folder / "Demo-1.0.AppImage").is_file()
    # even an app whose (tampered) app file path lies in there does not get them "cleaned"
    registry().put(dataclasses.replace(v2, id="odd", appimage_path=str(folder / "x.AppImage"),
                                       desktop_path=str(folder / "easyinstaller-odd.desktop"),
                                       icon_paths=[], previous=None))
    uninstall("odd", Scope.USER)
    assert all(path.read_bytes() == b"keep" for path in lookalikes)


# ------------------------------------------------------------------------------------------------
# the file helpers: files and folders, never through symbolic links
# ------------------------------------------------------------------------------------------------


def portable_app(layout, isolated_env, **values) -> InstalledApp:
    folder = layout.apps_dir / "Tool"
    (folder / "bin").mkdir(parents=True)
    (folder / "bin" / "tool").write_bytes(b"\x7fELF")
    (folder / "data.txt").write_text("app data")
    base = dict(id="tool", name="Tool", version="3.1", scope=Scope.USER, kind="portable",
                install_dir=str(folder), appimage_path=str(folder / "bin" / "tool"),
                desktop_path=str(layout.desktop_dir / "easyinstaller-tool.desktop"), size=5)
    base.update(values)
    return InstalledApp(**base)


def test_a_folder_can_be_kept_and_taken_back(isolated_env):
    """Portable apps (a later step) are folders: the same helpers move and delete them."""
    layout = user_layout()
    app = portable_app(layout, isolated_env)
    assert backups.backed_up_path(app) == Path(app.install_dir)
    assert backups.planned_backup_path(layout, app) == backups_dir(layout) / "tool" / "Tool-3.1"

    stash = backups.stash(layout, app)
    assert stash.linked is False and stash.path == backups_dir(layout) / "tool" / "Tool-3.1"
    assert not Path(app.install_dir).exists()
    assert (stash.path / "bin" / "tool").read_bytes() == b"\x7fELF"
    assert stash.record["path"] == str(stash.path) and stash.record["version"] == "3.1"
    kept = dataclasses.replace(app, previous=stash.record)
    assert backups.usable_previous(layout, kept) == stash.record

    stash.undo()                                           # the installation failed
    assert (Path(app.install_dir) / "data.txt").read_text() == "app data"
    assert not backups_dir(layout).exists()

    stash = backups.stash(layout, app)                     # and once more, to delete it
    precious = isolated_env / "Documents"
    precious.mkdir()
    (precious / "thesis.odt").write_text("precious")
    (stash.path / "link-to-documents").symlink_to(precious, target_is_directory=True)
    (stash.path / "link-to-file").symlink_to(precious / "thesis.odt")
    assert backups.remove_backup(layout, "tool", stash.path) is True
    assert not backups_dir(layout).exists()
    assert (precious / "thesis.odt").read_text() == "precious"


def test_remove_backup_only_inside_the_apps_own_folder(downloads, isolated_env):
    layout = user_layout()
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"))
    folder = backups_dir(layout) / "demo"
    kept = Path(v2.previous["path"])
    precious = isolated_env / "precious.txt"
    precious.write_text("precious")
    (backups_dir(layout) / "other").mkdir()
    foreign = backups_dir(layout) / "other" / "Other.AppImage"
    foreign.write_text("other app")

    refused = [precious, foreign, folder, backups_dir(layout), layout.apps_dir / "Demo.AppImage",
               folder / "sub" / "x.AppImage", folder / ".." / "demo" / kept.name,
               Path("demo") / kept.name, "", None, 5, str(kept) + "/", folder / "."]
    for path in refused:
        assert backups.is_backup_path(layout, "demo", path) is False, path
        assert backups.remove_backup(layout, "demo", path) is False
    assert backups.is_backup_path(layout, "other", kept) is False
    assert backups.is_backup_path(layout, "../x", kept) is False
    assert backups.remove_backup(layout, "other", kept) is False
    assert kept.is_file() and precious.read_text() == "precious" and foreign.is_file()
    assert Path(v2.appimage_path).is_file()

    # a symbolic link planted as "the backup" is removed as a link
    link = folder / "Linked.AppImage"
    link.symlink_to(precious)
    assert backups.is_backup_path(layout, "demo", link)
    assert backups.usable_previous(layout, dataclasses.replace(
        v2, previous={**v2.previous, "path": str(link)})) is None     # not a file or folder
    assert backups.remove_backup(layout, "demo", link) is True
    assert not link.is_symlink() and precious.read_text() == "precious"
    assert backups.remove_backup(layout, "demo", folder / "missing.AppImage") is False
    assert backups.remove_backup(layout, "demo", kept) is True
    assert not folder.exists() and (backups_dir(layout) / "other").is_dir()

    with pytest.raises(ValueError):
        backups.app_backup_dir(layout, "../evil")


def test_stash_refuses_odd_things(downloads, isolated_env):
    layout = user_layout()
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"))
    target = Path(v1.appimage_path)
    real = isolated_env / "real.AppImage"
    os.rename(target, real)
    target.symlink_to(real)                       # the app file is a link now
    with pytest.raises(InstallError, match="could not be kept"):
        backups.stash(layout, v1)
    target.unlink()
    with pytest.raises(OSError):
        backups.stash(layout, v1)                 # missing
    assert real.is_file() and not backups_dir(layout).exists()   # nothing was created either


# ------------------------------------------------------------------------------------------------
# system scope: the real client code and the real helper operations, in a temporary root
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def helper(sys_layout, monkeypatch):
    """privileged.run_helper -> JSON round trip -> helper.ops (no pkexec, no root)."""
    calls: list[tuple[str, dict]] = []
    caller = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}

    def run_helper(op, payload, *, timeout=600):
        assert op in privileged.HELPER_OPS
        request = json.loads(json.dumps(payload))      # exactly what travels over stdin
        calls.append((op, request))
        if op == "install":
            return ops.op_install(request, layout=sys_layout, run_commands=False, **caller)
        if op == "uninstall":
            return ops.op_uninstall(request, layout=sys_layout, run_commands=False)
        if op == "drop-backup":
            return ops.op_drop_backup(request, layout=sys_layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    return calls


SYSTEM = InstallOptions(scope=Scope.SYSTEM)


def test_system_update_rollback_and_drop(downloads, sys_layout, helper):
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0", args="--one"), SYSTEM)
    assert helper[-1][1]["keep_backup"] is True and helper[-1][1]["backup_max_age_days"] == 14
    assert v1.previous is None and v1.mtime_ns == os.stat(v1.appimage_path).st_mtime_ns > 0
    inode = os.stat(v1.appimage_path).st_ino

    with inspect_appimage(demo(downloads / "Demo-2.0.AppImage", "2.0", args="--two")) as info:
        plan = plan_install(info, SYSTEM)
        kept = sys_layout.apps_dir / ".easyinstaller-backups" / "demo" / "Demo-1.0.AppImage"
        assert plan.backup_target == kept
        v2 = execute_install(plan)
    assert v2.previous["path"] == str(kept) and v2.previous["version"] == "1.0"
    assert v2.previous["sha256"] == v1.sha256 and sha256(kept) == v1.sha256
    assert os.stat(kept).st_ino == inode and os.stat(kept).st_nlink == 1    # moved, not copied
    assert registry(sys_layout).get("demo") == v2

    # going back: the client inspects the kept file, the helper swaps the two versions
    plan = plan_rollback("demo", Scope.SYSTEM)
    try:
        assert plan.action == "rollback" and plan.requires_root and plan.consumes_backup
    finally:
        plan.info.cleanup()
    back = rollback("demo", Scope.SYSTEM)
    op, manifest = helper[-1]
    assert op == "install" and manifest["consume_backup"] is True and manifest["keep_backup"] is True
    assert manifest["source_appimage"] == str(kept) and manifest["version"] == "1.0"
    assert manifest["original_filename"] == "Demo-1.0.AppImage"   # (PORT-10)
    assert back.version == "1.0" and sha256(back.appimage_path) == v1.sha256
    assert back.previous["version"] == "2.0" and sha256(back.previous["path"]) == v2.sha256
    assert tree(sys_layout.apps_dir) == [".easyinstaller-backups/", ".easyinstaller-backups/demo/",
                                         ".easyinstaller-backups/demo/Demo-2.0.AppImage",
                                         "Demo.AppImage"]
    assert exec_args(back) == [back.appimage_path, "--one", "%F"]
    assert kept.exists() is False            # used up by the helper, not deleted by the client
    assert back.original_filename == "Demo-1.0.AppImage"
    # a version kept by 0.2.0 (no file name recorded): the helper is never sent ""
    registry(sys_layout).put(dataclasses.replace(back, previous={
        k: v for k, v in back.previous.items() if k != "original_filename"}))
    forth = rollback("demo", Scope.SYSTEM)
    assert helper[-1][1]["original_filename"] is None and not forth.original_filename
    assert forth.version == "2.0"
    back = rollback("demo", Scope.SYSTEM)
    assert back.version == "1.0" and back.original_filename == "Demo-1.0.AppImage"

    # delete the kept version: only with the administrator helper
    drop_backup("demo", Scope.SYSTEM)
    assert helper[-1] == ("drop-backup", {"app_id": "demo"})
    assert registry(sys_layout).get("demo").previous is None
    assert [p.name for p in sys_layout.apps_dir.iterdir()] == ["Demo.AppImage"]
    calls = len(helper)
    drop_backup("demo", Scope.SYSTEM)        # nothing kept: no password prompt for nothing
    assert len(helper) == calls
    with pytest.raises(InstallError, match="no previous version"):
        rollback("demo", Scope.SYSTEM)
    with pytest.raises(NotInstalledError):
        drop_backup("nope", Scope.SYSTEM)
    assert len(helper) == calls


def test_system_uninstall_removes_the_backup(downloads, sys_layout, helper):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"), SYSTEM)
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"), SYSTEM)
    assert Path(v2.previous["path"]).is_file()
    uninstall("demo", Scope.SYSTEM)
    assert list(sys_layout.apps_dir.iterdir()) == []


def test_system_update_without_backup(downloads, sys_layout, helper):
    save_settings(Settings(backup_days=0))
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"), SYSTEM)
    v2 = install(demo(downloads / "Demo-2.0.AppImage", "2.0"), SYSTEM)
    assert helper[-1][1]["keep_backup"] is False and helper[-1][1]["backup_max_age_days"] == 0
    assert v2.previous is None
    assert [p.name for p in sys_layout.apps_dir.iterdir()] == ["Demo.AppImage"]


def test_a_killed_system_update_leaves_no_backup_behind_for_ever(downloads, sys_layout, helper):
    """SEC-1: the helper is killed after keeping 1.0 (a hard link) and placing 2.0, before the
    registry is saved: nothing records that link; the helper itself must remove it later."""
    v1 = install(demo(downloads / "Demo-1.0.AppImage", "1.0"), SYSTEM)
    folder = sys_layout.apps_dir / ".easyinstaller-backups" / "demo"
    folder.mkdir(parents=True)
    os.link(v1.appimage_path, folder / "Demo-1.0.AppImage")      # what the kill left behind
    two = demo(downloads / "Demo-2.0.AppImage", "2.0")
    os.unlink(v1.appimage_path)
    shutil.copyfile(two, v1.appimage_path)
    os.chmod(v1.appimage_path, 0o755)
    assert registry(sys_layout).get("demo") == v1        # the registry still says 1.0
    # the file on disk is 2.0 now, but the registry says 1.0: it is not kept as "1.0"
    v3 = install(demo(downloads / "Demo-3.0.AppImage", "3.0"), SYSTEM)
    assert v3.version == "3.0" and v3.previous is None
    assert [p.name for p in folder.iterdir()] == ["Demo-1.0.AppImage"]   # the orphan
    uninstall("demo", Scope.SYSTEM)
    assert registry(sys_layout).all() == []
    assert not (sys_layout.apps_dir / ".easyinstaller-backups").exists()


def test_the_helper_prunes_backups_nobody_records(downloads, sys_layout, helper):
    install(demo(downloads / "Demo-1.0.AppImage", "1.0"), SYSTEM)
    folder = sys_layout.apps_dir / ".easyinstaller-backups" / "demo"
    folder.mkdir(parents=True)
    orphan = folder / "Demo-0.9.AppImage"
    orphan.write_bytes(b"old")
    old = time.time() - 30 * 86400
    os.utime(orphan, (old, old))
    with ops.open_registry(sys_layout, create=False).locked(timeout=5):
        assert ops.prune_backups(sys_layout, ops.open_registry(sys_layout, create=False), 14) == []
    assert orphan.exists()          # (ctime: just made - a kill may have happened a moment ago)
    with ops.open_registry(sys_layout, create=False).locked(timeout=5):
        registry_now = ops.open_registry(sys_layout, create=False)
        assert ops.remove_unrecorded_backups(sys_layout, registry_now, "demo",
                                             cutoff=time.time() + 1) == [str(orphan)]
    assert not orphan.exists() and not folder.exists()


def test_drop_backup_is_a_helper_operation_the_client_may_run():
    assert installer.DROP_BACKUP_OP == "drop-backup" in privileged.HELPER_OPS
    from easy_installer.helper import main as helper_main

    assert "drop-backup" in helper_main.OPS


def test_layout_paths(isolated_env, system_root):
    from easy_installer.core import paths

    layout = user_layout()
    assert paths.BACKUPS_DIR_NAME == ".easyinstaller-backups"
    assert paths.backups_dir(layout) == layout.backups_dir == \
        isolated_env / "Applications" / ".easyinstaller-backups"
    assert system_layout(system_root).backups_dir == \
        system_root / "opt" / "appimages" / ".easyinstaller-backups"
    assert backups.backups_dir(layout) == installer.backups_dir(layout) == layout.backups_dir
    assert paths.cache_dir() == isolated_env / ".cache" / "easy-installer"
    assert paths.state_home() == isolated_env / ".local" / "state"
    env = {"HOME": "/home/u", "XDG_STATE_HOME": "/state", "XDG_CACHE_HOME": "relative"}
    assert paths.state_home(env) == Path("/state")
    assert paths.cache_dir(env) == Path("/home/u/.cache/easy-installer")   # relative: ignored


# ------------------------------------------------------------------------------------------------
# DATA-5: a crash between the two renames of an update or a rollback
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["update", "rollback"])
def test_a_crash_between_the_two_renames_leaves_no_missing_app(operation, downloads, monkeypatch):
    from easy_installer.core.reconcile import reconcile_all

    class Killed(BaseException):
        pass

    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    v2 = install(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))
    v3 = demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0")
    content = Path(v2.appimage_path).read_bytes()

    def killed(src, dst):    # right after the installed file was set aside: nothing is undone
        raise Killed()

    with monkeypatch.context() as patched, pytest.raises(Killed):
        patched.setattr(installer, "_rename", killed)
        patched.setattr(installer._Transaction, "rollback", lambda self: None)
        if operation == "update":
            install(v3)
        else:
            rollback("demo", Scope.USER)
    assert not Path(v2.appimage_path).exists()            # set aside, under a hidden name
    # the next start: put back, nothing is missing
    assert [r.change for r in reconcile_all()] == ["unchanged"]
    app = registry().get("demo")
    assert app.status() == "ok" and Path(app.appimage_path).read_bytes() == content
    assert app.version == "2.0" and backups.usable_previous(user_layout(), app)["version"] == "1.0"
    assert sha256(app.previous["path"]) == v1.sha256
    assert not any(".easyinstaller-old-" in p.name for p in user_layout().apps_dir.iterdir())


def test_repair_puts_back_a_file_an_interrupted_update_set_aside(downloads):
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    content = Path(v1.appimage_path).read_bytes()
    aside = Path(v1.appimage_path).with_name(".Demo.AppImage.easyinstaller-old-0123abcd")
    os.rename(v1.appimage_path, aside)
    assert registry().get("demo").status() == "missing-appimage"
    repaired = installer.repair("demo", Scope.USER)
    assert Path(repaired.appimage_path).read_bytes() == content and not aside.exists()
