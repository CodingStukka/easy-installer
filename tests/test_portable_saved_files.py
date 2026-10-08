"""What an app or its user saved inside the folder of a portable app is never lost.

Tor Browser keeps its profile in its folder, VS Code in portable mode its ``data`` folder, games
their saves. Reinstalling, updating, going back and uninstalling replace or remove the folder as
a whole - but what did not come from the archive is taken over into the new folder, and a
folder is only ever deleted for good when nothing in it differs from what was unpacked; else it
goes to the trash.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from easy_installer.core import appfolder, installer
from easy_installer.core.installer import (
    InstallOptions,
    drop_backup,
    execute_install,
    plan_install,
    prune_backups,
    rollback,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.portable import MANIFEST_NAME, inspect_portable
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.settings import Settings, save_settings
from easy_installer.errors import InstallError

from fakearchive import electron_tree, fake_elf, make_archive, make_tar
from test_portable_install import make_status

BACKUPS = ".easyinstaller-backups"
SETTINGS = "data/user-data/User/settings.json"


@pytest.fixture(autouse=True)
def quiet_system(monkeypatch, system_root, tmp_path):
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    # No `gio`: the trash is the FreeDesktop one in the isolated home (deterministic).
    empty = tmp_path / "no-tools"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


@pytest.fixture
def downloads(isolated_env) -> Path:
    folder = isolated_env / "Downloads"
    folder.mkdir()
    return folder


@pytest.fixture
def apps() -> Path:
    return user_layout().apps_dir


def trash(home: Path) -> Path:
    return home / ".local" / "share" / "Trash" / "files"


def vscode(downloads: Path, version: str = "1.90.0", **extra: bytes) -> Path:
    tree = electron_tree("VSCode-linux-x64", program="code")
    tree["files"]["VSCode-linux-x64/resources/app/package.json"] = json.dumps({"version": version})
    for name, content in extra.items():
        tree["files"][f"VSCode-linux-x64/{name}"] = content
    return make_archive(downloads / f"VSCode-linux-x64-{version}.tar.gz", **tree)


def install(archive: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_portable(archive) as info:
        return execute_install(plan_install(info, options))


def install_with_notes(archive: Path) -> tuple[InstalledApp, list[str]]:
    with inspect_portable(archive) as info:
        plan = plan_install(info)
        app = execute_install(plan)
        return app, list(plan.warnings)


def save_settings_file(folder: Path, text: str = '{"editor.fontSize": 18}') -> Path:
    path = folder / SETTINGS
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def anywhere(root: Path, name: str) -> list[Path]:
    return sorted(root.rglob(name))


# ------------------------------------------------------------------------------------------------
# reinstall, update, go back: the new folder gets what was saved
# ------------------------------------------------------------------------------------------------


def test_reinstalling_the_same_archive_keeps_what_the_app_saved(downloads, apps, isolated_env):
    archive = vscode(downloads)
    app = install(archive)
    folder = Path(app.install_dir)
    save_settings_file(folder)
    (folder / "saves").mkdir()
    (folder / "saves" / "slot1.sav").write_text("3 hours of progress")

    with inspect_portable(archive) as info:
        plan = plan_install(info)
        assert plan.action == "reinstall" and plan.backup_target is None
        again = execute_install(plan)

    assert (folder / SETTINGS).read_text() == '{"editor.fontSize": 18}'
    assert (folder / "saves" / "slot1.sav").read_text() == "3 hours of progress"
    assert again.install_dir == app.install_dir
    # a fresh copy otherwise: nothing else is left over
    assert sorted(p.name for p in apps.iterdir()) == ["VSCode"]


@pytest.mark.parametrize("backup_days", [0, 14])
def test_an_update_takes_over_what_the_app_saved(downloads, apps, isolated_env, backup_days):
    save_settings(Settings(backup_days=backup_days))
    folder = Path(install(vscode(downloads, "1.90.0")).install_dir)
    save_settings_file(folder)
    new = install(vscode(downloads, "1.91.0"))
    assert new.version == "1.91.0"
    assert (folder / SETTINGS).read_text() == '{"editor.fontSize": 18}'
    if backup_days:
        kept = Path(new.previous["path"])
        assert kept.is_dir() and not (kept / SETTINGS).exists()     # moved, not copied
    else:
        assert new.previous is None
        assert sorted(p.name for p in apps.iterdir()) == ["VSCode"]
    assert not trash(isolated_env).exists()


def test_going_back_takes_the_saved_files_along(downloads, apps):
    install(vscode(downloads, "1.90.0"))
    folder = apps / "VSCode"
    install(vscode(downloads, "1.91.0"))
    save_settings_file(folder, "made with 1.91")
    back = rollback("vscode", Scope.USER)
    assert back.version == "1.90.0" and (folder / SETTINGS).read_text() == "made with 1.91"
    forth = rollback("vscode", Scope.USER)
    assert forth.version == "1.91.0" and (folder / SETTINGS).read_text() == "made with 1.91"


def test_what_the_new_version_brings_itself_wins_and_the_rest_is_kept(downloads, apps,
                                                                        isolated_env):
    save_settings(Settings(backup_days=0))
    folder = Path(install(vscode(downloads, "1.90.0")).install_dir)
    (folder / "config.ini").write_text("the user's own settings")
    (folder / "notes.txt").write_text("keep me")
    app, notes = install_with_notes(vscode(downloads, "1.91.0", **{"config.ini": b"default"}))
    assert (folder / "config.ini").read_text() == "default"
    assert (folder / "notes.txt").read_text() == "keep me"
    # the old folder still had the user's config.ini: it is in the trash, not deleted
    trashed = trash(isolated_env) / "VSCode-1.90.0"
    assert (trashed / "config.ini").read_text() == "the user's own settings"
    assert any("trash" in note for note in notes)
    assert app.previous is None and sorted(p.name for p in apps.iterdir()) == ["VSCode"]


def test_a_changed_file_of_the_app_goes_to_the_trash_on_reinstall(downloads, apps, isolated_env):
    archive = vscode(downloads)
    folder = Path(install(archive).install_dir)
    (folder / "resources" / "app" / "package.json").write_text("edited by the user")
    again, notes = install_with_notes(archive)
    assert json.loads((folder / "resources" / "app" / "package.json").read_text())
    trashed = trash(isolated_env) / "VSCode-1.90.0"
    assert (trashed / "resources" / "app" / "package.json").read_text() == "edited by the user"
    assert any("trash" in note for note in notes)


def test_a_failed_installation_puts_the_saved_files_back(downloads, apps, monkeypatch):
    folder = Path(install(vscode(downloads, "1.90.0")).install_dir)
    save_settings_file(folder)
    before = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))

    def broken(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(installer, "_write_desktop_file", broken)
    with pytest.raises(InstallError):
        install(vscode(downloads, "1.91.0"))
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) == before
    assert (folder / SETTINGS).read_text() == '{"editor.fontSize": 18}'
    assert sorted(p.name for p in apps.iterdir()) == ["VSCode"]


# ------------------------------------------------------------------------------------------------
# deleting kept versions and uninstalling
# ------------------------------------------------------------------------------------------------


def test_uninstall_moves_a_folder_with_saved_files_to_the_trash(downloads, apps, isolated_env):
    folder = Path(install(vscode(downloads)).install_dir)
    save_settings_file(folder)
    notes = uninstall("vscode", Scope.USER)
    assert not folder.exists()
    assert (trash(isolated_env) / "VSCode" / SETTINGS).is_file()
    assert len(notes) == 1 and "trash" in notes[0]
    info = (trash(isolated_env).parent / "info" / "VSCode.trashinfo").read_text()
    assert f"Path={folder}" in info            # restoring it brings it back where it was


def test_uninstall_deletes_a_folder_that_holds_only_what_was_unpacked(downloads, apps,
                                                                     isolated_env):
    folder = Path(install(vscode(downloads)).install_dir)
    assert (folder / MANIFEST_NAME).is_file()
    assert uninstall("vscode", Scope.USER) == []
    assert not folder.exists() and not trash(isolated_env).exists()


@pytest.mark.parametrize("how", ["prune", "drop", "uninstall"])
def test_a_kept_version_with_saved_files_goes_to_the_trash(downloads, apps, isolated_env, how):
    install(vscode(downloads, "1.90.0"))
    folder = apps / "VSCode"
    (folder / "config.ini").write_text("mine")
    new = install(vscode(downloads, "1.91.0", **{"config.ini": b"default"}))
    kept = Path(new.previous["path"])
    assert (kept / "config.ini").read_text() == "mine"          # it had to stay behind
    if how == "prune":
        assert [app.id for app in prune_backups(0)] == ["vscode"]
    elif how == "drop":
        drop_backup("vscode", Scope.USER)
    else:
        uninstall("vscode", Scope.USER)
    assert not kept.exists()
    assert (trash(isolated_env) / "VSCode-1.90.0" / "config.ini").read_text() == "mine"


def test_a_kept_version_without_saved_files_is_deleted(downloads, apps, isolated_env):
    install(vscode(downloads, "1.90.0"))
    new = install(vscode(downloads, "1.91.0"))
    kept = Path(new.previous["path"])
    assert [app.id for app in prune_backups(0)] == ["vscode"]
    assert not kept.exists() and not trash(isolated_env).exists()


def test_what_cannot_go_to_the_trash_stays(downloads, apps, isolated_env, monkeypatch):
    folder = Path(install(vscode(downloads)).install_dir)
    save_settings_file(folder)
    monkeypatch.setattr(appfolder.appdata, "trash_folder", lambda path: "no trash here")
    notes = uninstall("vscode", Scope.USER)
    assert (folder / SETTINGS).is_file()                 # left where it was
    assert Registry(user_layout().registry_path).get("vscode") is None
    assert len(notes) == 1 and str(folder) in notes[0]


# ------------------------------------------------------------------------------------------------
# folders unpacked by Easy Installer 0.2.0 (no record of what was unpacked)
# ------------------------------------------------------------------------------------------------


def as_made_by_0_2_0(folder: Path) -> None:
    (folder / MANIFEST_NAME).unlink()
    marker = folder / ".easy-installer.json"
    data = json.loads(marker.read_text())
    del data["files_sha256"]
    marker.write_text(json.dumps(data))


def test_reinstall_of_a_folder_from_0_2_0_keeps_what_the_app_saved(downloads, apps,
                                                                   isolated_env):
    archive = vscode(downloads)
    folder = Path(install(archive).install_dir)
    as_made_by_0_2_0(folder)
    save_settings_file(folder)
    install(archive)
    assert (folder / SETTINGS).is_file()
    # what was in the old folder cannot be told apart for sure: it is not deleted
    assert (trash(isolated_env) / "VSCode-1.90.0").is_dir()


def test_a_folder_from_0_2_0_is_never_deleted_for_good(downloads, apps, isolated_env):
    save_settings(Settings(backup_days=0))
    folder = Path(install(vscode(downloads, "1.90.0")).install_dir)
    as_made_by_0_2_0(folder)
    (folder / "saves").mkdir()
    (folder / "saves" / "slot1.sav").write_text("progress")
    install(vscode(downloads, "1.91.0"))
    assert anywhere(isolated_env, "slot1.sav")
    uninstall("vscode", Scope.USER)
    assert anywhere(trash(isolated_env), "slot1.sav")


def test_an_archive_cannot_bring_its_own_record(downloads, apps):
    tree = electron_tree("Evil-linux-x64", program="evil")
    tree["files"]["Evil-linux-x64/" + MANIFEST_NAME] = json.dumps(
        {"format": 1, "entries": {"data": ["d"]}})
    folder = Path(install(make_archive(downloads / "Evil-linux-x64.tar.gz", **tree)).install_dir)
    manifest = appfolder.read_manifest(folder)
    assert manifest is not None and "data" not in manifest.entries
    assert "evil" in manifest.entries


def test_the_record_lists_what_was_unpacked(downloads, apps):
    archive = make_tar(downloads / "Tool-1.0.tar", {"Tool/Tool": fake_elf(), "Tool/doc/a.txt": "a"},
                       modes={"Tool/Tool": 0o755})
    folder = Path(install(archive).install_dir)
    manifest = appfolder.read_manifest(folder)
    assert set(manifest.entries) == {"Tool", "doc", "doc/a.txt"}
    assert appfolder.differences(folder, manifest) == []
    (folder / "doc" / "b.txt").write_text("b")
    (folder / "doc" / "a.txt").write_text("changed")
    assert appfolder.differences(folder, manifest) == [
        appfolder.Difference("doc/a.txt", added=False), appfolder.Difference("doc/b.txt", added=True)]
    assert not appfolder.is_pristine(folder)
    assert os.stat(folder / MANIFEST_NAME).st_mode & 0o777 == 0o644


def test_a_folder_left_by_an_interrupted_update_is_not_deleted_with_saved_files(downloads, apps,
                                                                              isolated_env):
    """A crash after the new folder took the app's place, while the saved files were being
    taken over: the next installation finds the old folder and does not delete what is in it."""
    folder = Path(install(vscode(downloads, "1.90.0")).install_dir)
    save_settings_file(folder)
    leftover = apps / ".VSCode.easyinstaller-old-0123abcd"
    shutil.copytree(folder, leftover, symlinks=True)
    shutil.rmtree(folder / "data")                     # this part went over already
    make_tar(downloads / "Other-1.0.tar", {"Other/Other": fake_elf()}, modes={"Other/Other": 0o755})
    install(downloads / "Other-1.0.tar")
    assert not leftover.exists()
    assert (trash(isolated_env) / "VSCode-1.90.0" / SETTINGS).is_file()
