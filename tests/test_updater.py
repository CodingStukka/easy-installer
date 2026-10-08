"""core/updater.py: check the installed apps for updates (fake fetchers) and apply an update
(fake downloaders, one loopback server for the real download) - and what must never happen: a
failed, cancelled or wrong download changes nothing that is installed.

Nothing here touches the network.
"""

from __future__ import annotations

import base64
import dataclasses
import functools
import hashlib
import http.server
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from easy_installer.core import inspector, installer, privileged, updater, updates
from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import (
    InstallOptions,
    execute_install,
    plan_install,
    rollback,
    uninstall,
)
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.reconcile import reconcile_app
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import SandboxFix
from easy_installer.core.settings import Settings, save_settings
from easy_installer.core.signature import SignatureInfo
from easy_installer.core.system_checks import SystemStatus
from easy_installer.core.updater import (
    SignerChangedError,
    UpdateCheckResult,
    apply_update,
    cached_updates,
    check_all_updates,
    check_app_update,
    has_update_source,
    update_source_of,
)
from easy_installer.core.updates import AvailableUpdate, HttpResponse, UpdateCache, UpdateSource
from easy_installer.errors import (
    AuthorizationError,
    InstallError,
    NetworkError,
    NotInstalledError,
    UpdateCancelled,
    UpdateError,
)
from easy_installer.helper import ops

from fakeappimage import (
    EM_AARCH64,
    make_fake_appimage,
    make_png,
    requires_mksquashfs,
    requires_unsquashfs,
)
from fakearchive import blender_tree, make_archive

pytestmark = [requires_mksquashfs, requires_unsquashfs]

UPD_INFO = "gh-releases-zsync|demo-org|demo|latest|Demo-*-x86_64.AppImage.zsync"
API = "https://api.github.com/repos/demo-org/demo/releases/latest"
SOURCE = {"kind": "github-assets", "owner": "demo-org", "repo": "demo", "release": "latest",
          "pattern": "Demo-*-x86_64.AppImage", "url": None, "prerelease": False, "via": "upd_info"}
USER = Scope.USER.value
SYSTEM = Scope.SYSTEM.value


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


@pytest.fixture(autouse=True)
def sys_layout(system_root, monkeypatch):
    """The "for everyone" places live in a temporary root (never the real /opt, /var/lib)."""
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def demo(path: Path, version: str | None, *, name: str = "Demo", stem: str = "demo",
         upd_info: str | None = UPD_INFO, tag: str = "", files: dict | None = None,
         **kw) -> Path:
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\nExec=AppRun --v{version} %F\n"
               f"Icon={stem}\nCategories=Utility;\n")
    if version:
        desktop += f"X-AppImage-Version={version}\n"
    return make_fake_appimage(path, {
        f"{stem}.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "payload.bin": f"{name} {version} {tag}".encode() * 3,
        f"{stem}.png": make_png(32, 32), **(files or {}),
    }, upd_info=upd_info, **kw)


def install(path: Path, options: InstallOptions | None = None) -> InstalledApp:
    with inspect_appimage(path) as info:
        return execute_install(plan_install(info, options))


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def snapshot(layout=None) -> dict[str, bytes]:
    """Everything that is installed: app files, kept versions, launchers, icons, registry."""
    layout = layout or user_layout()
    found = {}
    for root in (layout.apps_dir, layout.desktop_dir, layout.icons_dir, layout.registry_dir):
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.name.endswith(".lock"):
                found[str(path)] = path.read_bytes()
    return found


def exec_args(app: InstalledApp) -> list[str]:
    entry = DesktopEntry.parse(Path(app.desktop_path).read_text(encoding="utf-8"))
    return split_exec(entry.get("Exec"))


def no_download_left() -> bool:
    folder = updater.downloads_dir()
    return not folder.exists() or list(folder.iterdir()) == []


class Feed:
    """The app's release page: what it publishes, the requests made, and the download."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.file: Path | None = None
        self.name = ""
        self.tag = ""
        self.digest = True
        self.error: BaseException | None = None
        self.status = 200
        self.calls: list[str] = []
        self.downloads: list[tuple[AvailableUpdate, Path]] = []
        self.served: Path | None = None            # what the download really delivers
        self.download_error: BaseException | None = None
        self.during_download = None                # callable(cancel) run in the middle

    def publish(self, file: Path, tag: str, *, name: str | None = None, digest: bool = True) -> Path:
        self.file, self.tag, self.digest = file, tag, digest
        self.name = name or file.name
        return file

    def release(self) -> dict:
        assert self.file is not None
        return {
            "tag_name": self.tag, "prerelease": False, "draft": False,
            "html_url": f"https://github.com/demo-org/demo/releases/tag/{self.tag}",
            "published_at": "2026-09-20T10:11:12Z",
            "assets": [{
                "name": self.name, "state": "uploaded", "size": self.file.stat().st_size,
                "digest": "sha256:" + sha256(self.file) if self.digest else None,
                "browser_download_url":
                    f"https://github.com/demo-org/demo/releases/download/{self.tag}/{self.name}",
            }],
        }

    def fetch(self, url, *, headers=None, max_bytes=None) -> HttpResponse:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        assert url == API, f"unexpected request: {url}"
        if self.file is None or self.status != 200:
            return HttpResponse(status=404 if self.file is None else self.status, headers={},
                                body=b'{"message": "Not Found"}', url=url)
        return HttpResponse(status=200, headers={}, body=json.dumps(self.release()).encode(),
                            url=url)

    def download(self, update, dest_dir, *, progress=None, cancel=None) -> Path:
        self.downloads.append((update, Path(dest_dir)))
        if progress is not None:
            progress(0.5, "Downloading…")
        if self.during_download is not None:
            self.during_download(cancel)
        if self.download_error is not None:
            raise self.download_error
        target = Path(dest_dir) / update.filename
        shutil.copyfile(self.served or self.file, target)
        os.chmod(target, 0o600)                    # as download_update leaves it
        if progress is not None:
            progress(1.0, "Downloading…")
        return target


@pytest.fixture
def feed(tmp_path) -> Feed:
    folder = tmp_path / "published"
    folder.mkdir()
    return Feed(folder)


@pytest.fixture
def v1(downloads) -> InstalledApp:
    return install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))


def publish_v2(feed: Feed, version: str = "2.0", **kw) -> Path:
    return feed.publish(demo(feed.folder / f"Demo-{version}-x86_64.AppImage", version, **kw),
                        f"v{version}")


# ------------------------------------------------------------------------------------------------
# which apps can be checked
# ------------------------------------------------------------------------------------------------


def test_the_inspected_update_source_is_recorded(v1):
    assert v1.update_info == UPD_INFO and v1.update_source == SOURCE
    assert has_update_source(v1)
    source = update_source_of(v1)
    assert source == UpdateSource.from_dict(SOURCE) and source.describe() == "github.com/demo-org/demo"


def test_an_entry_made_by_0_1_is_checked_through_its_update_information(v1):
    old = dataclasses.replace(v1, update_source=None, mtime_ns=0, data_hints=[])
    assert has_update_source(old) and update_source_of(old) == UpdateSource.from_dict(SOURCE)
    broken = dataclasses.replace(old, update_source={"kind": "github-assets", "owner": "../x"})
    assert update_source_of(broken) == UpdateSource.from_dict(SOURCE)     # the fallback again


@pytest.mark.parametrize("changes", [
    {"update_source": None, "update_info": None},
    {"update_source": None, "update_info": "bintray-zsync|a|b|c|d.zsync"},
    {"update_source": None, "update_info": "zsync|http://example.org/Demo.zsync"},
    {"update_source": {"kind": "ftp"}, "update_info": None},
    {"pinned": True},
    {"base_id": "demo", "id": "demo--1.0"},
    {"kind": "portable"},
])
def test_apps_that_are_never_checked(v1, changes):
    app = dataclasses.replace(v1, **changes)
    assert has_update_source(app) is False

    def fetch(url, **kw):
        raise AssertionError("no request is made for such an app")

    assert check_app_update(app, force=True, fetch=fetch) is None
    result = check_all_updates([app], force=True, fetch=fetch)
    assert (dict(result), result.errors, result.checked) == ({}, {}, 0)


# ------------------------------------------------------------------------------------------------
# checking
# ------------------------------------------------------------------------------------------------


def test_check_finds_the_update_and_remembers_it(v1, feed):
    new = publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    assert update == AvailableUpdate(
        version="2.0", filename="Demo-2.0-x86_64.AppImage", size=new.stat().st_size,
        url="https://github.com/demo-org/demo/releases/download/v2.0/Demo-2.0-x86_64.AppImage",
        sha256=sha256(new), release_url="https://github.com/demo-org/demo/releases/tag/v2.0",
        published_at="2026-09-20T10:11:12Z")
    assert feed.calls == [API]
    assert cached_updates() == {(USER, "demo"): update}
    assert UpdateCache().checked_at(USER, "demo") is not None

    # not due again for a day: the answer comes from the cache, without a request
    assert check_app_update(v1, fetch=feed.fetch) == update
    assert feed.calls == [API]
    # unless asked explicitly
    assert check_app_update(v1, force=True, fetch=feed.fetch) == update
    assert feed.calls == [API, API]


def test_up_to_date_is_remembered_too(v1, feed):
    feed.publish(Path(v1.appimage_path), "v1.0", name="Demo-1.0-x86_64.AppImage")
    assert check_app_update(v1, fetch=feed.fetch) is None
    assert check_app_update(v1, fetch=feed.fetch) is None
    assert feed.calls == [API] and cached_updates() == {}
    assert UpdateCache().is_due(USER, "demo", 24) is False


def test_the_interval_comes_from_the_settings(v1, feed, monkeypatch):
    publish_v2(feed)
    seen = []
    real = UpdateCache.is_due

    def is_due(self, scope, app_id, interval_hours):
        seen.append(interval_hours)
        return real(self, scope, app_id, interval_hours)

    monkeypatch.setattr(UpdateCache, "is_due", is_due)
    save_settings(Settings(update_interval_hours=6))
    check_app_update(v1, fetch=feed.fetch)
    check_all_updates(fetch=feed.fetch)
    assert seen == [6, 6]


def test_check_tells_the_source_what_is_installed(v1, monkeypatch):
    seen = {}

    def fake(source, **kw):
        seen.update(kw, source=source)
        return None

    monkeypatch.setattr(updates, "check_for_update", fake)
    assert check_app_update(v1, force=True, fetch="F") is None
    etags = seen.pop("etags")
    assert isinstance(etags, dict)
    assert seen == {
        "source": UpdateSource.from_dict(SOURCE), "current_version": "1.0",
        "current_sha256": v1.sha256, "current_filename": "Demo-1.0-x86_64.AppImage",
        "arch": "x86_64", "fetch": "F", "current_size": v1.size,
    }


def test_a_network_problem_is_raised_and_not_remembered(v1, feed):
    feed.error = NetworkError("The internet cannot be reached.", details="timeout")
    with pytest.raises(NetworkError, match="internet"):
        check_app_update(v1, fetch=feed.fetch)
    assert UpdateCache().is_due(USER, "demo", 24)          # tried again the next time
    feed.error = None
    publish_v2(feed)
    assert check_app_update(v1, fetch=feed.fetch).version == "2.0"


def test_a_rate_limit_is_a_network_problem(v1, feed):
    publish_v2(feed)

    def limited(url, **kw):
        return HttpResponse(status=403, headers={"x-ratelimit-remaining": "0", "retry-after": "600"},
                            body=b"{}", url=url)

    with pytest.raises(NetworkError, match="try again"):
        check_app_update(v1, fetch=limited)
    assert UpdateCache().is_due(USER, "demo", 24)


def test_unusable_update_information_counts_as_checked(v1, feed):
    """The releases are gone (404): asking again at every start would not help."""
    with pytest.raises(UpdateError, match="No update information"):
        check_app_update(v1, fetch=feed.fetch)
    assert UpdateCache().is_due(USER, "demo", 24) is False
    assert check_app_update(v1, fetch=feed.fetch) is None and feed.calls == [API]
    with pytest.raises(UpdateError):
        check_app_update(v1, force=True, fetch=feed.fetch)


def test_check_all_collects_updates_and_errors(downloads, v1, feed):
    publish_v2(feed)
    other_info = "gh-releases-zsync|other-org|other|latest|Other-*.AppImage.zsync"
    other = install(demo(downloads / "Other-1.0.AppImage", "1.0", name="Other", stem="other",
                         upd_info=other_info))
    plain = install(demo(downloads / "Plain-1.0.AppImage", "1.0", name="Plain", stem="plain",
                         upd_info=None))
    assert not has_update_source(plain)
    other_api = "https://api.github.com/repos/other-org/other/releases/latest"

    def fetch(url, **kw):
        if url == other_api:
            raise NetworkError("The server did not answer.", details="503")
        return feed.fetch(url, **kw)

    steps = []
    result = check_all_updates(progress=lambda fraction, text: steps.append((fraction, text)),
                               fetch=fetch)
    assert isinstance(result, UpdateCheckResult)
    assert result.checked == 2
    assert list(result) == [(USER, "demo")] and len(result) == 1
    assert result[(USER, "demo")].version == "2.0" and result.updates == dict(result.items())
    assert list(result.errors) == [(USER, "other")]
    assert isinstance(result.errors[(USER, "other")], NetworkError)
    assert str(result.errors[(USER, "other")]) == "The server did not answer."
    assert steps == [(0.0, "Checking Demo…"), (0.5, "Checking Other…"), (1.0, "Done")]
    assert cached_updates() == {(USER, "demo"): result[(USER, "demo")]}
    assert other.id == "other"

    # only the app whose check failed is asked again
    feed.calls.clear()
    again = check_all_updates(fetch=fetch)
    assert feed.calls == [] and list(again) == [(USER, "demo")] and list(again.errors) == [(USER, "other")]


def test_a_failed_check_keeps_the_update_found_before(v1, feed):
    """UPD-3: a check the source cannot answer is a finished check - but no "up to date"."""
    publish_v2(feed)
    assert check_app_update(v1, force=True, fetch=feed.fetch).version == "2.0"
    feed.status = 404
    with pytest.raises(UpdateError, match="No update information"):
        check_app_update(v1, force=True, fetch=feed.fetch)
    cache = UpdateCache()
    assert cache.failed(USER, "demo") and not cache.is_due(USER, "demo", 24)
    assert cached_updates()[(USER, "demo")].version == "2.0"
    calls = len(feed.calls)
    assert check_app_update(v1, fetch=feed.fetch).version == "2.0"     # the next start
    assert len(feed.calls) == calls
    # an answer again: the failure is forgotten
    feed.status = 200
    assert check_app_update(v1, force=True, fetch=feed.fetch).version == "2.0"
    assert not UpdateCache().failed(USER, "demo")


def test_a_cached_answer_that_cannot_be_used_is_asked_for_again(v1, feed):
    """UPD-1 (one request per address): the retry without If-None-Match is a new request."""
    publish_v2(feed)
    cache = UpdateCache()
    cache.etags[API] = {"etag": '"old"', "body": "{not json", "saved_at": "2026-09-30T00:00:00Z"}
    cache.save()
    seen = []

    def fetch(url, *, headers=None, max_bytes=None):
        seen.append((headers or {}).get("If-None-Match"))
        if (headers or {}).get("If-None-Match"):
            return HttpResponse(status=304, headers={}, body=b"", url=url)
        return feed.fetch(url, headers=headers, max_bytes=max_bytes)

    result = check_all_updates([v1], force=True, fetch=fetch)
    assert result.errors == {} and result[(USER, "demo")].version == "2.0"
    assert seen == ['"old"', None]


FREECAD_INFO = "gh-releases-zsync|demo-org|demo|latest|FreeCAD*x86_64*.AppImage.zsync"


@pytest.mark.parametrize("digest", [True, False])
def test_a_file_without_a_version_replaced_in_place_is_not_recorded_as_the_old_one(
        digest, downloads, feed):
    """UPD-2: FreeCAD's AppImage does not tell its version; only its download name did."""
    old = install(demo(downloads / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", None,
                       name="FreeCAD", stem="freecad", upd_info=FREECAD_INFO, tag="1.1.3"))
    assert old.version == "1.1.3"
    newer = feed.publish(demo(feed.folder / "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage", None,
                              name="FreeCAD", stem="freecad", upd_info=FREECAD_INFO,
                              tag="1.1.4"), "1.1.4", digest=digest)
    update = check_app_update(old, force=True, fetch=feed.fetch)
    assert update.version == "1.1.4"
    os.replace(shutil.copy(newer, downloads / "copy"), old.appimage_path)   # AppImageUpdate -O
    result = reconcile_app(old)
    assert result.change == "updated"
    # the remembered update is this very file: its version is known
    assert result.app.version == "1.1.4" and result.app.sha256 == sha256(newer)
    assert check_app_update(result.app, force=True, fetch=feed.fetch) is None
    # a file that is not the known update: unknown, never the old version's
    other = demo(downloads / "FreeCAD-weekly.AppImage", None, name="FreeCAD", stem="freecad",
                 upd_info=FREECAD_INFO, tag="weekly")
    os.replace(shutil.copy(other, downloads / "copy2"), old.appimage_path)
    assert reconcile_app(result.app).app.version is None


def test_check_all_survives_a_bug_in_one_check(v1, monkeypatch):
    def boom(source, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(updates, "check_for_update", boom)
    result = check_all_updates(force=True)
    assert result.checked == 1 and dict(result) == {}
    assert isinstance(result.errors[(USER, "demo")], UpdateError)
    assert "RuntimeError: bug" in result.errors[(USER, "demo")].details


def test_check_all_without_apps_makes_no_cache_file(isolated_env):
    result = check_all_updates(force=True)
    assert (dict(result), result.errors, result.checked) == ({}, {}, 0)
    assert cached_updates() == {}
    assert not (isolated_env / ".cache" / "easy-installer").exists()


def test_a_kept_copy_is_never_offered_an_update(downloads, v1, feed):
    """Keep both: the copy is pinned to its version; only the main app is updated."""
    with inspect_appimage(demo(downloads / "Demo-0.9-x86_64.AppImage", "0.9")) as info:
        copy = execute_install(plan_install(info, InstallOptions(keep_both=True)))
    assert copy.pinned and copy.base_id == "demo" and copy.update_source == SOURCE
    assert has_update_source(copy) is False
    publish_v2(feed)
    result = check_all_updates(force=True, fetch=feed.fetch)
    assert list(result) == [(USER, "demo")] and result.checked == 1
    UpdateCache().put(USER, copy.id, result[(USER, "demo")])       # even a cached entry
    assert list(cached_updates()) == [(USER, "demo")]
    with pytest.raises(UpdateError, match="cannot be updated automatically"):
        apply_update(copy, result[(USER, "demo")], downloader=feed.download)
    assert feed.downloads == [] and registry().get(copy.id) == copy


def test_the_same_app_in_both_scopes_is_checked_for_each(downloads, v1, feed, helper):
    for_all = install(demo(downloads / "Demo-1.5-x86_64.AppImage", "1.5"),
                      InstallOptions(scope=Scope.SYSTEM))
    publish_v2(feed)
    result = check_all_updates(fetch=feed.fetch)
    assert sorted(result) == [(SYSTEM, "demo"), (USER, "demo")] and result.checked == 2
    assert for_all.scope is Scope.SYSTEM
    # both installations share one answer of the source (its hourly allowance is small)
    assert feed.calls == [API]
    # ... within one check only: the next forced check asks again
    check_all_updates(force=True, fetch=feed.fetch)
    assert feed.calls == [API, API]


def test_a_failed_request_is_not_shared(downloads, v1, feed, helper):
    install(demo(downloads / "Demo-1.5-x86_64.AppImage", "1.5"), InstallOptions(scope=Scope.SYSTEM))
    publish_v2(feed)
    answers = iter([NetworkError("offline"), None])

    def flaky(url, **kwargs):
        error = next(answers)
        if error is not None:
            feed.calls.append(url)
            raise error
        return feed.fetch(url, **kwargs)

    result = check_all_updates(fetch=flaky)
    assert len(feed.calls) == 2 and len(result.errors) == 1 and len(result.updates) == 1


# ------------------------------------------------------------------------------------------------
# what is remembered stays true
# ------------------------------------------------------------------------------------------------


def test_cached_updates_never_use_the_network(v1, feed, monkeypatch):
    publish_v2(feed)
    check_app_update(v1, fetch=feed.fetch)

    def no_network(*args, **kw):
        raise AssertionError("no request")

    monkeypatch.setattr(updates, "http_get", no_network)
    monkeypatch.setattr(updates, "check_for_update", no_network)
    assert [update.version for update in cached_updates().values()] == ["2.0"]


def test_a_remembered_update_is_dropped_when_the_app_caught_up(downloads, v1, feed):
    new = publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    assert cached_updates() == {(USER, "demo"): update}
    # the app updated itself (the file changed in place), Easy Installer notices it
    shutil.copyfile(new, v1.appimage_path)
    assert reconcile_app(v1).change == "updated"
    assert cached_updates() == {}
    assert UpdateCache().get(USER, "demo") is None          # forgotten, not just hidden


def test_a_remembered_update_is_dropped_when_a_newer_file_is_installed_by_hand(downloads, v1, feed):
    publish_v2(feed)
    check_app_update(v1, fetch=feed.fetch)
    install(demo(downloads / "Demo-3.0-x86_64.AppImage", "3.0"))
    assert cached_updates() == {} and UpdateCache().get(USER, "demo") is None


def test_a_stale_cache_entry_is_not_offered(v1, feed):
    """Whatever the cache file says: an update that is not newer than the app is no update."""
    cache = UpdateCache()
    same = AvailableUpdate(version="1.0", url="https://example.org/Demo-1.0.AppImage",
                           filename="Demo-1.0.AppImage")
    older = dataclasses.replace(same, version="0.5")
    same_file = AvailableUpdate(version=None, url=same.url, filename="x.AppImage", sha256=v1.sha256)
    same_size = AvailableUpdate(version=None, url=same.url, filename="x.AppImage", size=v1.size)
    for update in (same, older, same_file, same_size):
        cache.put(USER, "demo", update)
        assert cached_updates() == {}
        assert check_app_update(v1, fetch=feed.fetch) is None and feed.calls == []
    newer = dataclasses.replace(same, version="1.0.1")
    cache.put(USER, "demo", newer)
    assert cached_updates() == {(USER, "demo"): newer}


def test_uninstalling_forgets_the_remembered_update(v1, feed):
    publish_v2(feed)
    check_app_update(v1, fetch=feed.fetch)
    uninstall("demo", Scope.USER)
    assert cached_updates() == {} and UpdateCache().get(USER, "demo") is None


def test_a_damaged_cache_is_no_problem(v1, feed, isolated_env):
    path = isolated_env / ".cache" / "easy-installer" / "updates.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    assert cached_updates() == {}
    publish_v2(feed)
    assert check_app_update(v1, fetch=feed.fetch).version == "2.0"
    assert list(cached_updates()) == [(USER, "demo")]


# ------------------------------------------------------------------------------------------------
# applying an update
# ------------------------------------------------------------------------------------------------


def test_update_is_applied_with_a_backup_and_can_be_rolled_back(v1, feed):
    new = publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    old_inode = os.stat(v1.appimage_path).st_ino
    steps = []

    v2 = apply_update(v1, update, progress=lambda f, text: steps.append(f), downloader=feed.download)

    assert v2.version == "2.0" and v2.id == "demo" and v2.scope is Scope.USER
    assert v2.appimage_path == v1.appimage_path and sha256(v2.appimage_path) == sha256(new) == v2.sha256
    assert os.stat(v2.appimage_path).st_mode & 0o777 == 0o755
    assert v2.installed_at == v1.installed_at and v2.original_filename == "Demo-2.0-x86_64.AppImage"
    assert exec_args(v2) == [v2.appimage_path, "--v2.0", "%F"]
    assert v2.update_source == SOURCE
    assert v2.origin_url == update.url                      # where this file came from
    assert registry().get("demo") == v2
    # the replaced version is kept (the default of the settings) - the very same file
    kept = Path(v2.previous["path"])
    assert kept == user_layout().apps_dir / ".easyinstaller-backups" / "demo" / "Demo-1.0.AppImage"
    assert v2.previous["version"] == "1.0" and sha256(kept) == v1.sha256
    assert os.stat(kept).st_ino == old_inode
    # the download went through the private cache folder, which is gone again
    assert [(u, d) for u, d in feed.downloads] == [(update, updater.downloads_dir() / "demo")]
    assert no_download_left()
    assert new.is_file()                                    # (the "server's" copy)
    # nothing is remembered about the update any more
    assert cached_updates() == {} and UpdateCache().get(USER, "demo") is None
    # progress never goes backwards and ends at 100 %
    fractions = [f for f in steps if f is not None]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0

    back = rollback("demo", Scope.USER)
    assert back.version == "1.0" and sha256(back.appimage_path) == v1.sha256
    assert back.previous["version"] == "2.0" and sha256(back.previous["path"]) == v2.sha256
    assert exec_args(back) == [back.appimage_path, "--v1.0", "%F"]
    # after going back the update is found again by the next check
    assert check_app_update(back, force=True, fetch=feed.fetch) == update


def test_an_update_that_turns_out_to_be_the_installed_file_is_forgotten(v1, feed):
    """A source without versions and checksums offered the very file that is installed: after
    "updating" it is not offered again and again."""
    same = AvailableUpdate(version=None, url="https://example.org/Demo-latest-x86_64.AppImage",
                           filename="Demo-latest-x86_64.AppImage")
    UpdateCache().put(USER, "demo", same)
    assert cached_updates() == {(USER, "demo"): same}
    feed.file = feed.folder / "same.AppImage"
    shutil.copyfile(v1.appimage_path, feed.file)
    again = apply_update(v1, same, downloader=feed.download)
    assert again.version == "1.0" and again.sha256 == v1.sha256 and again.previous is None
    assert cached_updates() == {} and UpdateCache().get(USER, "demo") is None
    assert no_download_left()


def test_update_without_keeping_the_old_version(v1, feed):
    save_settings(Settings(backup_days=0))
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    v2 = apply_update(v1, update, downloader=feed.download)
    assert v2.version == "2.0" and v2.previous is None
    assert sorted(p.name for p in user_layout().apps_dir.iterdir()) == ["Demo.AppImage"]


def test_notes_that_come_up_while_updating_are_passed_on(v1, feed, monkeypatch):
    """What the user should read after the update (here: the old version could not be kept)
    reaches the caller through ``warnings``; the plan's notes about the app as it was do not."""
    from easy_installer.core import backups

    def cannot_keep(layout, app):
        raise InstallError("The previous version could not be kept.", details="test")

    monkeypatch.setattr(backups, "stash", cannot_keep)
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    notes: list[str] = []
    v2 = apply_update(v1, update, downloader=feed.download, warnings=notes)
    assert v2.version == "2.0" and v2.previous is None
    assert notes == ["The previous version could not be kept, so you cannot go back to it."]


def test_no_notes_for_an_ordinary_update(v1, feed):
    publish_v2(feed)
    notes: list[str] = ["kept"]
    apply_update(v1, check_app_update(v1, fetch=feed.fetch), downloader=feed.download,
                 warnings=notes)
    assert notes == ["kept"]      # only appended to, never replaced


def test_a_broken_signature_of_the_update_is_passed_on(downloads, feed, monkeypatch):
    found = {"Demo-2.0-x86_64.AppImage": SignatureInfo(
        status="invalid", fingerprint=None, signer=None, details="changed")}
    monkeypatch.setattr(inspector, "read_signature", lambda path, elf: found.get(Path(path).name))
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    publish_v2(feed)
    notes: list[str] = []
    new = apply_update(old, check_app_update(old, fetch=feed.fetch), downloader=feed.download,
                       warnings=notes)
    assert new.version == "2.0"
    assert len(notes) == 1 and notes[0].startswith("The signature of this file does not match")


def test_update_keeps_the_choices_of_the_installation(downloads, feed, monkeypatch):
    """Sandbox fix and an explicitly chosen start mode are those of the installed app."""
    restricted = make_status(userns_restricted=True, apparmor_parser=None)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: restricted)
    electron = {"chrome-sandbox": b"\x7fELF", "resources/app.asar": b"asar"}
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0", files=electron),
                  InstallOptions(sandbox_fix=SandboxFix.NO_SANDBOX, extract_and_run=True))
    assert old.sandbox_fix == "no-sandbox" and old.extract_and_run and old.extract_and_run_explicit
    publish_v2(feed, files=electron)
    new = apply_update(old, check_app_update(old, fetch=feed.fetch), downloader=feed.download)
    assert new.version == "2.0"
    assert (new.sandbox_fix, new.extract_and_run, new.extract_and_run_explicit) == \
        ("no-sandbox", True, True)
    assert exec_args(new) == ["env", "APPIMAGE_EXTRACT_AND_RUN=1", new.appimage_path,
                              "--no-sandbox", "--v2.0", "%F"]


def test_a_stale_object_is_not_trusted(downloads, v1, feed):
    """The window may hold an older entry: the update replaces what is installed now."""
    newer = install(demo(downloads / "Demo-1.5-x86_64.AppImage", "1.5"))
    publish_v2(feed)
    update = check_app_update(newer, force=True, fetch=feed.fetch)
    v2 = apply_update(v1, update, downloader=feed.download)
    assert v2.version == "2.0" and v2.previous["version"] == "1.5"


def test_the_announced_version_is_used_when_the_file_does_not_tell(v1, feed):
    """Like FreeCAD: the version is only in the name of the download."""
    feed.publish(demo(feed.folder / "Demo-nightly-x86_64.AppImage", None, tag="three"), "v3.0")
    update = check_app_update(v1, fetch=feed.fetch)
    assert update.version == "3.0" and update.filename == "Demo-nightly-x86_64.AppImage"
    v3 = apply_update(v1, update, downloader=feed.download)
    assert v3.version == "3.0"
    assert check_app_update(v3, force=True, fetch=feed.fetch) is None


def test_an_entry_made_by_0_1_learns_its_update_source_on_the_way(v1, feed):
    old = dataclasses.replace(v1, update_source=None, mtime_ns=0, data_hints=[],
                              installer_version="0.1.0")
    registry().put(old)
    publish_v2(feed)
    update = check_all_updates(fetch=feed.fetch)[(USER, "demo")]
    v2 = apply_update(old, update, downloader=feed.download)
    assert v2.update_source == SOURCE and v2.data_hints == ["Demo", "demo-updater"]
    assert v2.installer_version != "0.1.0" and v2.mtime_ns > 0


def test_an_electron_app_is_updated_through_its_own_feed(downloads, feed):
    """The update source comes from resources/app-update.yml, the check from latest-linux.yml
    (version, sha512), the download from the release."""
    yml = "provider: github\nowner: demo-org\nrepo: demo\n"
    files = {"chrome-sandbox": b"\x7fELF", "resources/app.asar": b"asar",
             "resources/app-update.yml": yml}
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0", files=files, upd_info=None))
    assert old.update_source == {**SOURCE, "kind": "electron-github", "release": None,
                                 "pattern": None, "via": "app-update.yml"}
    new = demo(feed.folder / "Demo-2.0-x86_64.AppImage", "2.0", files=files, upd_info=None)
    feed.file = new
    digest = base64.b64encode(hashlib.sha512(new.read_bytes()).digest()).decode()
    latest = (f"version: 2.0\nfiles:\n  - url: {new.name}\n    sha512: {digest}\n"
              f"    size: {new.stat().st_size}\npath: {new.name}\nsha512: {digest}\n"
              "releaseDate: '2026-09-29T20:28:15.246Z'\n")
    yml_url = "https://github.com/demo-org/demo/releases/latest/download/latest-linux.yml"

    def fetch(url, **kw):
        assert url == yml_url
        return HttpResponse(status=200, headers={}, body=latest.encode(), url=url)

    update = check_app_update(old, fetch=fetch)
    assert update.version == "2.0" and update.sha512 == hashlib.sha512(new.read_bytes()).hexdigest()
    assert update.url == f"https://github.com/demo-org/demo/releases/latest/download/{new.name}"
    done = apply_update(old, update, downloader=feed.download)
    assert done.version == "2.0" and done.previous["version"] == "1.0"
    assert done.update_source == old.update_source


# ------------------------------------------------------------------------------------------------
# what must never happen: a bad download changes something
# ------------------------------------------------------------------------------------------------


def untouched(before: dict[str, bytes], app: InstalledApp) -> bool:
    assert snapshot() == before
    assert registry().get(app.id) == app
    assert no_download_left()
    return True


def test_wrong_app_in_the_download(downloads, v1, feed):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    feed.served = demo(feed.folder / "other.AppImage", "9.9", name="Other", stem="other")
    before = snapshot()
    with pytest.raises(UpdateError, match="different app, not Demo") as caught:
        apply_update(v1, update, downloader=feed.download)
    assert "'other', expected 'demo'" in caught.value.details
    assert untouched(before, v1)
    assert cached_updates() == {(USER, "demo"): update}     # still offered (the next try may work)


def test_the_download_is_not_an_app_at_all(v1, feed):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    before = snapshot()
    for content in (b"<html>Sign in to continue</html>", b"", b"\x7fELF" + b"\0" * 100):
        feed.served = feed.folder / "garbage"
        feed.served.write_bytes(content)
        with pytest.raises(UpdateError, match="not a working app"):
            apply_update(v1, update, downloader=feed.download)
        assert untouched(before, v1)
    # a download that was cut off in the payload
    from easy_installer.core.elf import read_elf_info

    whole = feed.file.read_bytes()
    feed.served.write_bytes(whole[: read_elf_info(feed.file).payload_offset + 200])
    with pytest.raises(UpdateError, match="not a working app"):
        apply_update(v1, update, downloader=feed.download)
    assert untouched(before, v1)


def test_update_for_another_kind_of_computer(v1, feed):
    publish_v2(feed, machine=EM_AARCH64)
    update = AvailableUpdate(version="2.0", url="https://example.org/Demo-2.0-x86_64.AppImage",
                             filename="Demo-2.0-x86_64.AppImage")
    before = snapshot()
    with pytest.raises(UpdateError, match=r"different kind of computer \(aarch64\)"):
        apply_update(v1, update, downloader=feed.download)
    assert untouched(before, v1)


def test_network_error_during_the_download(v1, feed):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    feed.download_error = NetworkError("The download was interrupted.", details="reset")
    before = snapshot()
    with pytest.raises(NetworkError, match="interrupted"):
        apply_update(v1, update, downloader=feed.download)
    assert untouched(before, v1)
    assert cached_updates() == {(USER, "demo"): update}


def test_a_partial_file_left_by_the_downloader_is_removed(v1, feed):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)

    def leaves_a_part(update, dest_dir, **kw):
        (Path(dest_dir) / "Demo-2.0-x86_64.AppImage.part").write_bytes(b"half")
        raise NetworkError("The download was interrupted.")

    before = snapshot()
    with pytest.raises(NetworkError):
        apply_update(v1, update, downloader=leaves_a_part)
    assert untouched(before, v1)


def test_leftovers_of_an_interrupted_update_are_cleared_first(v1, feed):
    stale = updater.downloads_dir() / "demo"
    stale.mkdir(parents=True)
    (stale / "Demo-2.0-x86_64.AppImage.part").write_bytes(b"old")
    (stale / "Demo-2.0-x86_64.AppImage").write_bytes(b"not what is downloaded now")
    publish_v2(feed)
    v2 = apply_update(v1, check_app_update(v1, fetch=feed.fetch), downloader=feed.download)
    assert v2.version == "2.0" and sha256(v2.appimage_path) == sha256(feed.file)
    assert no_download_left()


@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_cancel(v1, feed, when):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    cancel = threading.Event()
    if when == "before":
        cancel.set()
    elif when == "during":
        def stop(event):
            event.set()
            raise UpdateCancelled("The download was cancelled.")

        feed.during_download = stop
    else:
        feed.during_download = lambda event: event.set()     # ... and the file still arrives
    before = snapshot()
    with pytest.raises(UpdateCancelled, match="cancelled"):
        apply_update(v1, update, cancel=cancel, downloader=feed.download)
    assert untouched(before, v1)
    assert len(feed.downloads) == (0 if when == "before" else 1)
    assert cached_updates() == {(USER, "demo"): update}
    # the same update can be started again
    cancel.clear()
    feed.during_download = None
    assert apply_update(v1, update, cancel=cancel, downloader=feed.download).version == "2.0"


def test_a_downloader_that_answers_with_a_file_from_elsewhere_is_refused(v1, feed, tmp_path):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    before = snapshot()
    with pytest.raises(UpdateError, match="could not be downloaded"):
        apply_update(v1, update, downloader=lambda *a, **kw: feed.file)
    assert untouched(before, v1) and feed.file.is_file()


def test_update_of_an_app_that_is_gone(v1, feed):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    uninstall("demo", Scope.USER)
    with pytest.raises(NotInstalledError):
        apply_update(v1, update, downloader=feed.download)
    assert feed.downloads == [] and no_download_left()


def test_update_of_a_portable_app_is_refused(downloads, feed):
    archive = make_archive(downloads / "blender-4.2.0-linux-x64.tar.gz", **blender_tree())
    from easy_installer.core.portable import inspect_portable

    with inspect_portable(archive) as info:
        app = execute_install(plan_install(info))
    assert app.kind == "portable" and not has_update_source(app)
    update = AvailableUpdate(version="9", url="https://example.org/x.AppImage", filename="x.AppImage")
    with pytest.raises(UpdateError, match="cannot be updated automatically"):
        apply_update(app, update, downloader=feed.download)
    assert feed.downloads == []


def test_install_failure_leaves_the_old_version(v1, feed, monkeypatch):
    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    before = snapshot()

    def full_disk(path, text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(installer, "_write_desktop_file", full_disk)
    from easy_installer.errors import InstallError

    with pytest.raises(InstallError, match="free disk space"):
        apply_update(v1, update, downloader=feed.download)
    assert untouched(before, v1)
    assert cached_updates() == {(USER, "demo"): update}


def test_an_unusable_app_id_never_becomes_a_path(v1, isolated_env):
    evil = dataclasses.replace(v1, id="../../evil")
    with pytest.raises(UpdateError):
        updater._download_dir(evil)
    assert updater._download_dir(v1) == isolated_env / ".cache" / "easy-installer" / "downloads" / "demo"


# ------------------------------------------------------------------------------------------------
# the real download (loopback server): checksum mismatch, cancel
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def server(feed):
    """Serves the feed's folder on 127.0.0.1 (the only place a test may download from)."""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(feed.folder))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


LOCAL_DOWNLOAD = functools.partial(updates.download_update, allow_insecure_localhost=True)


def local_update(server: str, file: Path, **changes) -> AvailableUpdate:
    update = AvailableUpdate(version="2.0", url=f"{server}/{file.name}", filename=file.name,
                             size=file.stat().st_size, sha256=sha256(file))
    return dataclasses.replace(update, **changes)


def test_real_download_and_install(v1, feed, server):
    new = publish_v2(feed)
    v2 = apply_update(v1, local_update(server, new), downloader=LOCAL_DOWNLOAD)
    assert v2.version == "2.0" and sha256(v2.appimage_path) == sha256(new)
    assert v2.previous["version"] == "1.0" and no_download_left()
    assert v2.origin_url == f"{server}/{new.name}"


def test_checksum_mismatch(v1, feed, server):
    new = publish_v2(feed)
    before = snapshot()
    with pytest.raises(UpdateError) as caught:
        apply_update(v1, local_update(server, new, sha256="0" * 64), downloader=LOCAL_DOWNLOAD)
    assert "sha256" in (caught.value.details or "")
    assert untouched(before, v1)
    # a file of another size than announced
    with pytest.raises(UpdateError):
        apply_update(v1, local_update(server, new, size=new.stat().st_size + 1, sha256=None),
                     downloader=LOCAL_DOWNLOAD)
    assert untouched(before, v1)


def test_download_that_does_not_exist(v1, feed, server):
    new = publish_v2(feed)
    gone = dataclasses.replace(local_update(server, new), url=f"{server}/gone.AppImage")
    before = snapshot()
    with pytest.raises((UpdateError, NetworkError)):
        apply_update(v1, gone, downloader=LOCAL_DOWNLOAD)
    assert untouched(before, v1)


def test_cancel_during_the_real_download(v1, feed, server):
    new = publish_v2(feed)
    cancel = threading.Event()

    def progress(fraction, text):
        cancel.set()                      # "Cancel" is clicked as soon as something happens

    before = snapshot()
    with pytest.raises(UpdateCancelled):
        apply_update(v1, local_update(server, new), progress=progress, cancel=cancel,
                     downloader=LOCAL_DOWNLOAD)
    assert untouched(before, v1)


def test_an_address_that_is_not_secure_is_refused_by_default(v1, feed, server):
    new = publish_v2(feed)
    before = snapshot()
    with pytest.raises(UpdateError, match="not secure"):
        apply_update(v1, local_update(server, new))          # the default downloader
    assert untouched(before, v1)


# ------------------------------------------------------------------------------------------------
# signature continuity (DESIGN section 25)
# ------------------------------------------------------------------------------------------------

MAKER = "AB" * 20
SOMEONE = "CD" * 20


def signed(fingerprint: str = MAKER, status: str = "valid") -> SignatureInfo:
    return SignatureInfo(status=status, fingerprint=fingerprint, signer="Demo Maker <m@example.org>",
                         details=None)


@pytest.fixture
def signatures(monkeypatch) -> dict[str, SignatureInfo]:
    """File name -> the signature the inspector finds in it (none: not signed)."""
    found: dict[str, SignatureInfo] = {}
    monkeypatch.setattr(inspector, "read_signature", lambda path, elf: found.get(Path(path).name))
    return found


@pytest.mark.parametrize("new_signature", [None, signed(SOMEONE), signed(MAKER, "invalid"),
                                           signed(MAKER, "unverified")])
def test_update_by_another_signer_is_refused(downloads, feed, signatures, new_signature):
    signatures["Demo-1.0-x86_64.AppImage"] = signed()
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    assert old.signature == signed().to_dict()
    publish_v2(feed)
    if new_signature is not None:
        signatures["Demo-2.0-x86_64.AppImage"] = new_signature
    update = check_app_update(old, fetch=feed.fetch)
    before = snapshot()
    with pytest.raises(SignerChangedError, match="not signed by the same maker") as caught:
        apply_update(old, update, downloader=feed.download)
    assert isinstance(caught.value, UpdateError)
    assert untouched(before, old)

    # the user was asked and wants it anyway
    new = apply_update(old, update, downloader=feed.download, allow_signer_change=True)
    assert new.version == "2.0"
    assert new.signature == (new_signature.to_dict() if new_signature is not None else None)


def test_update_by_the_same_signer(downloads, feed, signatures):
    signatures["Demo-1.0-x86_64.AppImage"] = signed()
    signatures["Demo-2.0-x86_64.AppImage"] = signed()
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    publish_v2(feed)
    new = apply_update(old, check_app_update(old, fetch=feed.fetch), downloader=feed.download)
    assert new.version == "2.0" and new.signature == signed().to_dict()


@pytest.mark.parametrize("old_signature", [None, signed(MAKER, "unverified"), signed(MAKER, "invalid")])
def test_nothing_to_continue_without_a_valid_signature(downloads, feed, signatures, old_signature):
    """Unsigned is the norm: an update of an app that is not (validly) signed needs no question."""
    if old_signature is not None:
        signatures["Demo-1.0-x86_64.AppImage"] = old_signature
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    publish_v2(feed)
    signatures["Demo-2.0-x86_64.AppImage"] = signed(SOMEONE)
    new = apply_update(old, check_app_update(old, fetch=feed.fetch), downloader=feed.download)
    assert new.version == "2.0" and new.signature["fingerprint"] == SOMEONE


def test_plan_install_warns_when_the_signer_changes(downloads, signatures):
    signatures["Demo-1.0-x86_64.AppImage"] = signed()
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))
    # a file from somewhere, not signed: the first warning says so, prominently
    with inspect_appimage(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0")) as info:
        plan = plan_install(info)
        assert plan.signer_changed is True and plan.action == "update"
        assert plan.warnings[0].startswith("Careful: the installed version of Demo was signed")
        copy = plan_install(info, InstallOptions(keep_both=True))     # next to it: the same doubt
        assert copy.signer_changed is True and copy.warnings[0] == plan.warnings[0]
    # the same maker: no warning
    signatures["Demo-2.1-x86_64.AppImage"] = signed()
    with inspect_appimage(demo(downloads / "Demo-2.1-x86_64.AppImage", "2.1")) as info:
        plan = plan_install(info)
        assert plan.signer_changed is False
        assert not any("signed" in warning for warning in plan.warnings)
    # the installed file itself (repair), even when gpg is no longer there to check it
    signatures.clear()
    with inspect_appimage(old.appimage_path) as info:
        assert plan_install(info, InstallOptions(keep_original=True)).signer_changed is False
    # a first installation is never a change of signer
    with inspect_appimage(demo(downloads / "New-1.0.AppImage", "1.0", name="New", stem="new")) as info:
        assert plan_install(info).signer_changed is False


def test_plan_install_warns_about_a_broken_signature(downloads, signatures):
    signatures["Demo-1.0-x86_64.AppImage"] = signed(MAKER, "invalid")
    with inspect_appimage(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0")) as info:
        plan = plan_install(info)
    assert plan.signer_changed is False
    assert plan.warnings[0].startswith("The signature of this file does not match")


# ------------------------------------------------------------------------------------------------
# system scope: the real client code and the real helper operations in a temporary root
# ------------------------------------------------------------------------------------------------


class Calls(list):
    """The helper requests made, as (op, payload)."""

    fail_with = None


@pytest.fixture
def helper(sys_layout, monkeypatch):
    """privileged.run_helper -> JSON round trip -> helper.ops (no pkexec, no root);
    ``helper.fail_with(error)`` makes the next requests fail like a refused password prompt."""
    calls = Calls()
    caller = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}
    state: dict = {"error": None}

    def run_helper(op, payload, *, timeout=600):
        assert op in privileged.HELPER_OPS
        request = json.loads(json.dumps(payload))      # exactly what travels over stdin
        calls.append((op, request))
        if state["error"] is not None:
            raise state["error"]
        if op == "install":
            return ops.op_install(request, layout=sys_layout, run_commands=False, **caller)
        if op == "uninstall":
            return ops.op_uninstall(request, layout=sys_layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    calls.fail_with = lambda error: state.update(error=error)
    return calls


def test_system_app_is_updated_through_the_helper(downloads, feed, sys_layout, helper):
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"),
                  InstallOptions(scope=Scope.SYSTEM))
    assert old.scope is Scope.SYSTEM and old.update_source == SOURCE and has_update_source(old)
    new = publish_v2(feed)
    update = check_all_updates(fetch=feed.fetch)[(SYSTEM, "demo")]
    assert cached_updates() == {(SYSTEM, "demo"): update}

    done = apply_update(old, update, downloader=feed.download)

    op, manifest = helper[-1]
    assert op == "install" and manifest["app_id"] == "demo" and manifest["version"] == "2.0"
    assert manifest["keep_backup"] is True and manifest["consume_backup"] is False
    assert manifest["source_appimage"] == str(updater.downloads_dir() / "demo" / new.name)
    assert manifest["update_source"] == SOURCE and manifest["origin_url"] == update.url
    assert done.scope is Scope.SYSTEM and done.version == "2.0"
    assert done.appimage_path == old.appimage_path and sha256(done.appimage_path) == sha256(new)
    assert done.update_source == SOURCE and done.origin_url == update.url
    kept = sys_layout.apps_dir / ".easyinstaller-backups" / "demo" / "Demo-1.0.AppImage"
    assert done.previous["path"] == str(kept) and sha256(kept) == old.sha256
    assert registry(sys_layout).get("demo") == done
    assert registry().get("demo") is None                   # nothing was installed "only for me"
    assert no_download_left() and cached_updates() == {}

    back = rollback("demo", Scope.SYSTEM)
    assert back.version == "1.0" and back.previous["version"] == "2.0"
    assert check_app_update(back, force=True, fetch=feed.fetch) == update


def test_refused_password_leaves_the_system_app_untouched(downloads, feed, sys_layout, helper):
    old = install(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"),
                  InstallOptions(scope=Scope.SYSTEM))
    publish_v2(feed)
    update = check_app_update(old, fetch=feed.fetch)
    before = snapshot(sys_layout)
    helper.fail_with(AuthorizationError("The password prompt was cancelled."))
    with pytest.raises(AuthorizationError):
        apply_update(old, update, downloader=feed.download)
    assert snapshot(sys_layout) == before and registry(sys_layout).get("demo") == old
    assert no_download_left()
    assert cached_updates() == {(SYSTEM, "demo"): update}   # still offered


# ------------------------------------------------------------------------------------------------
# what an update that was stopped hard leaves in the cache (UPD-6)
# ------------------------------------------------------------------------------------------------


def _leftover(app_id: str, age: float) -> Path:
    folder = updater.downloads_dir() / app_id
    folder.mkdir(parents=True)
    part = folder / "Demo-2.0-x86_64.AppImage.part"
    part.write_bytes(b"x" * 5_000_000)
    old = time.time() - age
    for path in (part, folder):
        os.utime(path, (old, old))
    return folder


def test_an_unfinished_download_is_removed_at_the_next_start_and_on_uninstall(v1):
    from easy_installer.core import downloads
    pytest.importorskip("gi")
    from easy_installer.gui import window

    folder = _leftover("demo", age=3600)
    fresh = _leftover("other", age=0)          # maybe still being written (0.2.0 holds no lock)
    window.startup_maintenance()
    assert not folder.exists() and fresh.exists()
    # one that a running update holds stays, however old
    held = downloads.hold(updater.downloads_dir() / "busy")
    try:
        os.utime(updater.downloads_dir() / "busy", (0, 0))
        assert downloads.remove_leftovers() == [] and (updater.downloads_dir() / "busy").is_dir()
        with pytest.raises(downloads.FolderInUse):
            downloads.hold(updater.downloads_dir() / "busy")
    finally:
        downloads.release(updater.downloads_dir() / "busy", held)
    # uninstalling the app removes its leftover
    shutil.rmtree(fresh)
    folder = _leftover("demo", age=3600)
    uninstall("demo", Scope.USER)
    assert not folder.exists()


def test_an_update_of_an_app_that_is_already_being_updated_is_refused(v1, feed):
    from easy_installer.core import downloads

    publish_v2(feed)
    update = check_app_update(v1, fetch=feed.fetch)
    held = downloads.hold(updater.downloads_dir() / "demo")
    try:
        with pytest.raises(UpdateError, match="already being updated"):
            apply_update(v1, update, downloader=feed.download)
        assert (updater.downloads_dir() / "demo").is_dir()      # the other one's folder stays
    finally:
        downloads.release(updater.downloads_dir() / "demo", held)
    assert apply_update(v1, update, downloader=feed.download).version == "2.0"
    assert no_download_left()
