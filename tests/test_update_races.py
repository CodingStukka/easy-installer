"""An app that replaced its own file (a self-updater) while an update of Easy Installer was
waiting or downloading: the update is never installed as a silent downgrade or re-install, and
the kept previous version is never labelled with a version it does not contain.

Nothing here touches the network (fake release page and downloader of test_updater).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

import pytest

from easy_installer import cli
from easy_installer.core import installer, reconcile
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import execute_install, plan_install, plan_update, rollback
from easy_installer.core.paths import Scope, user_layout
from easy_installer.core.reconcile import CHANGE_UPDATED, ReconcileResult, reconcile_app
from easy_installer.core.registry import InstalledApp
from easy_installer.core.updater import (
    UpdateNotNeededError,
    apply_update,
    check_app_update,
    is_newer_than_installed,
)
from easy_installer.errors import InstallError, UpdateError

from fakeappimage import requires_mksquashfs, requires_unsquashfs
from test_updater import (  # noqa: F401 - fixtures
    demo,
    downloads,
    feed,
    install,
    publish_v2,
    quiet_system,
    registry,
    sha256,
    snapshot,
    sys_layout,
)

pytestmark = [requires_mksquashfs, requires_unsquashfs]


def version_of(path: str | Path) -> str | None:
    with inspect_appimage(Path(path), compute_hash=False) as info:
        return info.version


def self_update(app: InstalledApp, new_file: Path) -> None:
    """What electron-updater does: the new version takes the old file's place (same name)."""
    os.unlink(app.appimage_path)
    shutil.copyfile(new_file, app.appimage_path)
    os.chmod(app.appimage_path, 0o755)


@pytest.fixture
def v2(downloads, feed) -> InstalledApp:
    """Demo 2.0, updated from 1.0 by Easy Installer: 1.0 is kept."""
    v1 = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    publish_v2(feed, "2.0")
    new = apply_update(v1, check_app_update(v1, fetch=feed.fetch), downloader=feed.download)
    assert new.previous["version"] == "1.0"
    return new


def test_a_stale_offer_is_not_installed_over_a_self_update(v2, feed, downloads):
    """(a) 3.0 is offered; before the click the app installs 3.0 itself."""
    publish_v2(feed, "3.0")
    update = check_app_update(v2, force=True, fetch=feed.fetch)
    assert update is not None and update.version == "3.0"
    self_update(v2, feed.file)
    kept_1_0 = Path(v2.previous["path"])

    with pytest.raises(UpdateNotNeededError, match="now at version 3.0"):
        apply_update(v2, update, downloader=feed.download)
    now = registry().get("demo")
    assert now.version == "3.0" and version_of(now.appimage_path) == "3.0"
    assert now.previous["version"] == "1.0" and kept_1_0.is_file()     # the real 1.0 stays
    assert version_of(kept_1_0) == "1.0"
    assert len(feed.downloads) == 1                                    # nothing downloaded again


def test_a_stale_offer_is_never_a_silent_downgrade(v2, feed, downloads):
    """(b) 2.5 was offered; meanwhile the app updated itself to 3.0 (already reconciled)."""
    publish_v2(feed, "2.5")
    update = check_app_update(v2, force=True, fetch=feed.fetch)
    self_update(v2, demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0"))
    assert reconcile_app(v2).change == CHANGE_UPDATED
    assert not is_newer_than_installed(update, registry().get("demo"))
    before = snapshot()
    with pytest.raises(UpdateNotNeededError):
        apply_update(v2, update, downloader=feed.download)
    assert snapshot() == before and registry().get("demo").version == "3.0"


def test_a_self_update_during_the_download_is_kept_under_its_real_version(downloads, feed):
    """DATA-2: the app replaces its file (1.0 -> 2.0) while Easy Installer downloads 3.0."""
    v09 = install(demo(downloads / "Demo-0.9-x86_64.AppImage", "0.9"))
    publish_v2(feed, "1.0")
    v1 = apply_update(v09, check_app_update(v09, fetch=feed.fetch), downloader=feed.download)
    assert v1.previous["version"] == "0.9"

    publish_v2(feed, "3.0")
    update = check_app_update(v1, force=True, fetch=feed.fetch)
    self_updated = demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0")
    feed.during_download = lambda cancel: self_update(v1, self_updated)
    new = apply_update(v1, update, downloader=feed.download)

    assert new.version == "3.0" and version_of(new.appimage_path) == "3.0"
    # what was replaced is 2.0 - and it is kept as 2.0, with 2.0's checksum
    kept = Path(new.previous["path"])
    assert new.previous["version"] == "2.0" and version_of(kept) == "2.0"
    assert new.previous["sha256"] == sha256(self_updated) == sha256(kept)
    back = rollback("demo", Scope.USER)
    assert back.version == "2.0" and version_of(back.appimage_path) == "2.0"


def test_a_self_update_to_the_offered_version_during_the_download(v2, feed, downloads):
    """The app installs the very version that is being downloaded: nothing is installed twice."""
    publish_v2(feed, "3.0")
    update = check_app_update(v2, force=True, fetch=feed.fetch)
    feed.during_download = lambda cancel: self_update(v2, feed.file)
    with pytest.raises(UpdateNotNeededError, match="now at version 3.0"):
        apply_update(v2, update, downloader=feed.download)
    now = registry().get("demo")
    assert now.version == "3.0" and now.previous["version"] == "1.0"
    assert version_of(now.previous["path"]) == "1.0"


def test_a_download_older_than_the_installed_version_is_refused(v2, feed, downloads):
    """The source announces 3.0, but its file is 1.5: never a silent downgrade."""
    publish_v2(feed, "3.0")
    update = check_app_update(v2, force=True, fetch=feed.fetch)
    feed.served = demo(downloads / "Demo-1.5-x86_64.AppImage", "1.5")
    update = dataclasses.replace(update, sha256=None, size=None)   # not checked by the download
    before = snapshot()
    with pytest.raises(UpdateError, match="older than the installed version 2.0"):
        apply_update(v2, update, downloader=feed.download)
    assert snapshot() == before


def test_a_file_that_changes_between_planning_and_installing(v2, downloads):
    """The plan keeps the installed file as version 2.0; before it runs, the file changes."""
    newer = demo(downloads / "Demo-4.0-x86_64.AppImage", "4.0")
    with inspect_appimage(newer) as info:
        plan = plan_install(info)
        assert plan.backup_target is not None
        self_update(v2, demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0"))
        before = snapshot()
        with pytest.raises(InstallError, match="changed its own file in the meantime"):
            execute_install(plan)
        assert snapshot() == before
        # planned again, the changed file is not kept under the old version's name
        again = plan_install(info)
        assert again.backup_target is None
        assert any("changed its own file" in warning for warning in again.warnings)
        new = execute_install(again)
    assert new.version == "4.0" and new.previous["version"] == "1.0"


def test_plan_update_with_a_changed_file_does_not_label_it(v2, downloads):
    self_update(v2, demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0"))
    with inspect_appimage(demo(downloads / "Demo-4.0-x86_64.AppImage", "4.0")) as info:
        plan = plan_update(info, v2)
        assert plan.backup_target is None


# ------------------------------------------------------------------------------------------------
# "updated itself" is only said when the app went forward (UPD-5)
# ------------------------------------------------------------------------------------------------


def result(old: str, new: str) -> ReconcileResult:
    app = InstalledApp(id="t3code", name="T3 Code (Alpha)", version=new, scope=Scope.USER,
                       appimage_path="/x", desktop_path="/y")
    return ReconcileResult(app=app, change=reconcile.CHANGE_UPDATED, old_version=old,
                           new_version=new)


def test_an_app_that_went_back_a_version_is_not_announced_as_updated():
    good, text = cli._reconcile_note(result("0.0.45", "0.0.44"))
    assert not good and text == "T3 Code (Alpha) went back to version 0.0.44 by itself."
    good, text = cli._reconcile_note(result("0.0.43", "0.0.44"))
    assert good and text == "T3 Code (Alpha) updated itself to version 0.0.44."


def test_going_back_after_a_self_update_keeps_what_the_app_has_now(v2, downloads, capsys):
    """The app updated itself to 3.0 (not looked at yet): going back to 1.0 keeps 3.0 as 3.0."""
    three = demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0")
    self_update(v2, three)
    with pytest.raises(InstallError, match="changed its own file"):
        rollback("demo", Scope.USER)            # (the core never labels it 2.0)
    assert cli.main(["easy-installer", "rollback", "demo", "-y"]) == 0
    assert "Demo updated itself to version 3.0." in capsys.readouterr().out
    back = registry().get("demo")
    assert back.version == "1.0" and version_of(back.appimage_path) == "1.0"
    assert back.previous["version"] == "3.0" and version_of(back.previous["path"]) == "3.0"


def test_the_cli_says_an_update_is_no_longer_needed(v2, feed, capsys, monkeypatch):
    """`update --all` uses answers of the last minutes: the app updated itself meanwhile."""
    from easy_installer.core import updates

    publish_v2(feed, "3.0")
    monkeypatch.setattr(updates, "http_get", feed.fetch)
    monkeypatch.setattr(updates, "download_update", feed.download)
    feed.during_download = lambda cancel: self_update(v2, feed.file)   # e.g. it was quit
    capsys.readouterr()
    assert cli.main(["easy-installer", "update", "--all", "-y"]) == 0
    out, err = capsys.readouterr()
    assert "✓ Demo is now at version 3.0, so this update is no longer needed." in out
    assert "Error" not in err and registry().get("demo").version == "3.0"
    assert registry().get("demo").previous["version"] == "1.0"


def test_going_back_in_the_window_keeps_what_the_app_has_now(v2, downloads):
    pytest.importorskip("gi")
    from easy_installer.gui import window

    self_update(v2, demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0"))
    back = window.go_back(v2)
    assert back.version == "1.0" and version_of(back.appimage_path) == "1.0"
    assert back.previous["version"] == "3.0" and version_of(back.previous["path"]) == "3.0"
