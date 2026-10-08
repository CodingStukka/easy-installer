"""The command line of 0.2 (DESIGN section 27): update, rollback, details, repair, settings,
install --keep-both / --no-backup / --executable and portable archives, uninstall
--delete-data, list with SIZE and UPDATE, and apps that updated themselves.

Nothing here touches the network: GitHub is a fake fetcher and downloads are copies of files
made by the tests.
"""

from __future__ import annotations

import _thread
import dataclasses
import functools
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

import easy_installer.__main__ as entry
from easy_installer import cli
from easy_installer.core import (
    appdata,
    inspector,
    installer,
    portable,
    privileged,
    system_checks,
    updater,
    updates,
)
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.paths import Scope, settings_path, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.settings import load_raw, load_settings, save_raw
from easy_installer.core.signature import SignatureInfo
from easy_installer.core.system_checks import SystemStatus
from easy_installer.core.updates import AvailableUpdate, HttpResponse, UpdateCache
from easy_installer.errors import (
    HelperError,
    InstallError,
    NetworkError,
    UpdateCancelled,
    UpdateError,
)

from fakeappimage import make_fake_appimage, make_png, requires_mksquashfs, requires_unsquashfs
from fakearchive import blender_tree, electron_tree, fake_elf, flat_tree, make_archive, make_tar

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
USER = Scope.USER.value


# ------------------------------------------------------------------------------------------------
# fixtures & helpers
# ------------------------------------------------------------------------------------------------


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


class _NoAdministrator:
    """Stands in for ``privileged.run_helper``: the real helper (pkexec, root) never runs in
    these tests, whatever happens to the environment variables of a test."""

    def __call__(self, op, payload, *, timeout=600):
        raise HelperError("Administrator tasks are turned off in this environment.",
                          details=f"test guard refused helper op {op!r}")


def _never_run_the_helper():
    raise AssertionError("the tests must never start pkexec")


@pytest.fixture(autouse=True)
def no_administrator(monkeypatch, isolated_env):
    monkeypatch.setattr(privileged, "run_helper", _NoAdministrator())
    monkeypatch.setattr(privileged, "helper_command", _never_run_the_helper)
    yield
    # Nothing may have left the temporary home (see conftest.isolated_env).
    assert os.environ["HOME"] == str(isolated_env)
    assert os.environ["EASY_INSTALLER_DISABLE_PKEXEC"] == "1"


@pytest.fixture(autouse=True)
def cli_env(system_root, monkeypatch, no_administrator):
    """Deterministic system status, a temporary "for everyone" root, no uninstall action, no
    MIME database run, no gio (the trash is Easy Installer's own implementation), no network."""
    status = make_status()
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: status)
    monkeypatch.setattr(system_checks, "get_system_status", lambda refresh=False: status)
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "_mime_tool", lambda: None)
    monkeypatch.setattr(appdata, "_gio_trash", lambda path: False)

    def no_network(*args, **kwargs):
        raise AssertionError("the tests must never use the network")

    monkeypatch.setattr(updates, "http_get", no_network)
    monkeypatch.setattr(updates, "download_update", no_network)
    monkeypatch.setattr(updates, "_open_url", no_network)
    return layout


@pytest.fixture
def sys_layout(cli_env):
    return cli_env


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


def run_cli(*args: str, stdin: str | None = None, monkeypatch=None) -> int:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli.main(["easy-installer", *args])


def flat(text: str) -> str:
    """The text with every run of white space (alignment, line breaks) as one space."""
    return " ".join(text.split())


def registry(layout=None) -> Registry:
    return Registry((layout or user_layout()).registry_path)


def sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def demo(path: Path, version: str | None, *, name: str = "Demo", stem: str = "demo",
         owner: str = "demo-org", repo: str | None = None, upd_info: bool = True,
         wm_class: str | None = None) -> Path:
    """A fake AppImage that publishes its updates as GitHub release assets."""
    desktop = (f"[Desktop Entry]\nType=Application\nName={name}\nExec=AppRun %F\nIcon={stem}\n"
               f"Categories=Utility;\n")
    if version:
        desktop += f"X-AppImage-Version={version}\n"
    if wm_class:
        desktop += f"StartupWMClass={wm_class}\n"
    info = f"gh-releases-zsync|{owner}|{repo or stem}|latest|{name}-*-x86_64.AppImage.zsync"
    return make_fake_appimage(path, {
        f"{stem}.desktop": desktop, "AppRun": "#!/bin/sh\n",
        "payload.bin": f"{name} {version}".encode() * 40,
        f"{stem}.png": make_png(32, 32),
    }, upd_info=info if upd_info else None)


def installed(app_id: str, layout=None) -> InstalledApp:
    app = registry(layout).get(app_id)
    assert app is not None, f"{app_id} is not installed"
    return app


class Feed:
    """Fake GitHub: the latest release of each repository, the requests made and the downloads."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.releases: dict[str, tuple[str, Path]] = {}      # "owner/repo" -> (tag, file)
        self.calls: list[str] = []
        self.downloads: list[str] = []
        self.fetch_errors: dict[str, BaseException] = {}      # "owner/repo" -> error
        self.download_errors: dict[str, BaseException] = {}   # file name -> error
        self.during_download = None                           # callable(update, cancel)

    def publish(self, file: Path, tag: str, repo: str = "demo-org/demo") -> Path:
        self.releases[repo] = (tag, file)
        return file

    def fetch(self, url, *, headers=None, max_bytes=None) -> HttpResponse:
        self.calls.append(url)
        prefix, suffix = "https://api.github.com/repos/", "/releases/latest"
        assert url.startswith(prefix) and url.endswith(suffix), url
        repo = url[len(prefix):-len(suffix)]
        if repo in self.fetch_errors:
            raise self.fetch_errors[repo]
        if repo not in self.releases:
            return HttpResponse(status=404, headers={}, body=b'{"message": "Not Found"}', url=url)
        tag, file = self.releases[repo]
        body = {
            "tag_name": tag, "prerelease": False, "draft": False,
            "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "published_at": "2026-09-20T10:11:12Z",
            "assets": [{
                "name": file.name, "state": "uploaded", "size": file.stat().st_size,
                "digest": "sha256:" + sha256(file),
                "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{file.name}",
            }],
        }
        return HttpResponse(status=200, headers={}, body=json.dumps(body).encode(), url=url)

    def download(self, update: AvailableUpdate, dest_dir, *, progress=None, cancel=None,
                 allow_insecure_localhost=False) -> Path:
        self.downloads.append(update.filename)
        if progress is not None:
            progress(None, "Starting the download…")
            progress(0.5, "Downloading… 0.1 MB of 0.2 MB")
        if self.during_download is not None:
            self.during_download(update, cancel)
        if cancel is not None and cancel.is_set():
            raise UpdateCancelled("The download was cancelled.")
        if update.filename in self.download_errors:
            raise self.download_errors[update.filename]
        source = next(file for _tag, file in self.releases.values() if file.name == update.filename)
        target = Path(dest_dir) / update.filename
        shutil.copyfile(source, target)
        if progress is not None:
            progress(1.0, "Downloading… 0.2 MB of 0.2 MB")
        return target


@pytest.fixture
def feed(tmp_path, monkeypatch) -> Feed:
    feed = Feed(tmp_path / "published")
    monkeypatch.setattr(updates, "http_get", feed.fetch)
    monkeypatch.setattr(updates, "download_update", feed.download)
    return feed


def publish(feed: Feed, version: str, *, name: str = "Demo", stem: str = "demo",
            owner: str = "demo-org") -> Path:
    file = demo(feed.folder / f"{name}-{version}-x86_64.AppImage", version, name=name, stem=stem,
                owner=owner)
    return feed.publish(file, f"v{version}", f"{owner}/{stem}")


@pytest.fixture
def v1(downloads) -> InstalledApp:
    """Demo 1.0, installed just for me."""
    assert run_cli("install", "-y", str(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))) == 0
    return installed("demo")


def no_download_left() -> bool:
    folder = updater.downloads_dir()
    return not folder.exists() or list(folder.iterdir()) == []


# ------------------------------------------------------------------------------------------------
# entry point and help
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["update", "rollback", "details", "settings", "repair"])
def test_new_commands_go_to_the_command_line(command, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert entry.is_cli_invocation(["easy-installer", command])
    assert entry.is_cli_invocation(["easy-installer", "-v", command, "x"])


@pytest.mark.parametrize("command, options", [
    ("update", ["--all", "--check", "--user", "--system", "-y", "--json"]),
    ("rollback", ["--user", "--system", "-y"]),
    ("details", ["--user", "--system", "--json"]),
    ("uninstall", ["--delete-data"]),
    ("settings", ["KEY", "VALUE", "backup-days", "check-updates", "update-interval-hours"]),
    ("repair", ["--user", "--system"]),
])
def test_help_of_the_new_commands(command, options, capsys):
    assert cli.main(["easy-installer", command, "--help"]) == 0
    out = capsys.readouterr().out
    for option in options:
        assert option in out


@pytest.mark.parametrize("args", [
    ["update", "demo", "--all"],
    ["update", "--all", "--json"],
    ["update", "--user", "--system"],
    ["rollback"],
    ["details"],
    ["details", "x", "--user", "--system"],
    ["settings", "no-such-setting"],
    ["settings", "backup-days", "many"],
    ["settings", "backup-days", "91"],
    ["settings", "update-interval-hours", "0"],
    ["settings", "check-updates", "maybe"],
    ["repair"],
])
def test_usage_errors(args, capsys):
    assert cli.main(["easy-installer", *args]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err or err.startswith("Error: ")


# ------------------------------------------------------------------------------------------------
# settings
# ------------------------------------------------------------------------------------------------


def test_settings_shows_the_defaults(capsys):
    assert run_cli("settings") == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split()[1] for line in out.splitlines()
             if line.startswith("  ")}
    assert lines == {"check-updates": "yes", "update-interval-hours": "24", "backup-days": "14"}
    assert "settings.json" in out and "easy-installer settings KEY VALUE" in out
    assert run_cli("settings", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "check-updates": True, "update-interval-hours": 24, "backup-days": 14}


def test_settings_change_and_read_back(capsys):
    save_raw({"default_handler_offer_dismissed": True})   # a key of the window stays as it is
    assert run_cli("settings", "backup-days", "7") == 0
    assert "backup-days is now 7." in capsys.readouterr().out
    assert run_cli("settings", "backup-days") == 0
    assert capsys.readouterr().out == "7\n"
    assert run_cli("settings", "check_updates", "no") == 0        # "_" works as well as "-"
    assert run_cli("settings", "Update-Interval-Hours", " 6 ") == 0
    raw = load_raw()
    assert raw == {"default_handler_offer_dismissed": True, "backup_days": 7,
                   "check_updates": False, "update_interval_hours": 6}
    assert load_settings().backup_days == 7
    capsys.readouterr()
    assert run_cli("settings", "check-updates", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"check-updates": False}


def test_settings_json_after_a_change(capsys):
    assert run_cli("settings", "update-interval-hours", "12", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"update-interval-hours": 12}


@pytest.mark.parametrize("key, value, message", [
    ("backup-days", "-1", "backup-days must be a whole number from 0 to 90."),
    ("backup-days", "1.5", "backup-days must be a whole number from 0 to 90."),
    ("update-interval-hours", "721", "update-interval-hours must be a whole number from 1 to 720."),
    ("check-updates", "2", "check-updates must be yes or no."),
])
def test_settings_refuses_values_outside_the_range(key, value, message, capsys):
    assert run_cli("settings", key, value) == 2
    assert f"Error: {message}" in capsys.readouterr().err
    assert load_raw() == {}


def test_settings_unknown_key_lists_the_known_ones(capsys):
    assert run_cli("settings", "colour") == 2
    err = capsys.readouterr().err
    assert 'There is no setting called "colour".' in err
    assert "check-updates, update-interval-hours, backup-days" in err


def test_settings_that_cannot_be_saved(capsys):
    settings_path().mkdir(parents=True)            # a folder where the file should be
    assert run_cli("settings", "backup-days", "3") == 1
    assert "The setting could not be saved." in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------
# list: SIZE, UPDATE (from the cache only), apps that updated themselves
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_list_shows_size_and_a_remembered_update_without_network(v1, capsys):
    capsys.readouterr()
    update = AvailableUpdate(version="2.0", url="https://github.com/demo-org/demo/releases/"
                                                "download/v2.0/Demo-2.0-x86_64.AppImage",
                             filename="Demo-2.0-x86_64.AppImage", size=123)
    UpdateCache().put(USER, "demo", update)
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "ID", "VERSION", "FOR", "SIZE", "UPDATE", "STATUS"]
    row = lines[1].split()
    assert row[:5] == ["Demo", "demo", "1.0", "Only", "me"] and "MB" in row and "2.0" in row
    # one update: the hint names the app (not "--all")
    assert "1 new version is available. To install it, run: easy-installer update demo" \
        in " ".join(out.split())

    assert run_cli("list", "--json") == 0
    [data] = json.loads(capsys.readouterr().out)
    assert data["update"] == update.to_dict() and data["size"] == v1.size


@requires_mksquashfs
@requires_unsquashfs
def test_list_picks_up_an_app_that_updated_itself(v1, downloads, capsys):
    newer = demo(downloads / "new.AppImage", "2.0")
    os.replace(newer, v1.appimage_path)                   # what a self-updater does
    capsys.readouterr()
    assert run_cli("list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("Demo updated itself to version 2.0.")
    assert out[1] == ""
    assert out[2].split()[0] == "NAME" and out[3].split()[:3] == ["Demo", "demo", "2.0"]
    assert installed("demo").version == "2.0"
    assert run_cli("list") == 0                           # said once
    assert "updated itself" not in capsys.readouterr().out


@requires_mksquashfs
@requires_unsquashfs
def test_list_json_stays_json_when_an_app_updated_itself(v1, downloads, capsys):
    os.replace(demo(downloads / "new.AppImage", "2.0"), v1.appimage_path)
    capsys.readouterr()
    assert run_cli("list", "--json") == 0
    assert [a["version"] for a in json.loads(capsys.readouterr().out)] == ["2.0"]


def test_list_offers_repair_for_a_changed_system_app(sys_layout, capsys):
    sys_layout.apps_dir.mkdir(parents=True)
    appimage = sys_layout.apps_dir / "tool.AppImage"
    appimage.write_bytes(b"\x7fELF v1")
    desktop = sys_layout.desktop_dir / "easyinstaller-tool.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Tool\nExec=x\n")
    registry(sys_layout).put(InstalledApp(id="tool", name="Tool", version="1", scope=Scope.SYSTEM,
                                          appimage_path=str(appimage), desktop_path=str(desktop),
                                          size=7, mtime_ns=appimage.stat().st_mtime_ns))
    appimage.write_bytes(b"\x7fELF v2 is larger")
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "Tool (installed for everyone) changed its own app file." in out
    assert "easy-installer repair tool --system" in out


@requires_mksquashfs
@requires_unsquashfs
def test_list_offers_repair_for_a_missing_menu_entry(v1, capsys):
    Path(v1.desktop_path).unlink()
    capsys.readouterr()
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "Menu entry missing" in out
    assert "To make the menu entry of Demo again, run: easy-installer repair demo" in out


@requires_mksquashfs
@requires_unsquashfs
def test_list_deletes_kept_versions_older_than_the_settings_allow(v1, downloads, capsys):
    assert run_cli("install", "-y", str(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))) == 0
    previous = installed("demo").previous
    assert previous and Path(previous["path"]).is_file()
    save_raw({"backup_days": 0})
    assert run_cli("list") == 0
    assert installed("demo").previous is None and not Path(previous["path"]).exists()


# ------------------------------------------------------------------------------------------------
# update: checking
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_update_without_arguments_only_looks(v1, feed, capsys):
    publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update") == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "ID", "FOR", "INSTALLED", "AVAILABLE", "SIZE", "FROM"]
    assert lines[1].split()[:6] == ["Demo", "demo", "Only", "me", "1.0", "2.0"]
    assert lines[1].endswith("github.com/demo-org/demo")
    assert "To install it, run: easy-installer update demo" in out
    assert feed.downloads == [] and installed("demo").version == "1.0"
    assert feed.calls == ["https://api.github.com/repos/demo-org/demo/releases/latest"]


@requires_mksquashfs
@requires_unsquashfs
def test_update_check_asks_again_every_time(v1, feed, capsys):
    publish(feed, "2.0")
    assert run_cli("update", "--check") == 0
    assert run_cli("update", "--check") == 0
    assert len(feed.calls) == 2                           # an explicit check always looks
    assert "To install" not in capsys.readouterr().out    # --check: no hint


@requires_mksquashfs
@requires_unsquashfs
def test_update_right_after_a_check_uses_that_answer(v1, feed, capsys, monkeypatch):
    """``update`` says "To install it, run: easy-installer update demo"; doing that a minute
    later does not ask the source again (its hourly allowance is small). Later it does."""
    new = publish(feed, "2.0")
    assert run_cli("update") == 0
    assert len(feed.calls) == 1
    assert run_cli("update", "demo", "-y") == 0
    assert len(feed.calls) == 1                           # the answer of a minute ago
    assert installed("demo").version == "2.0" and feed.downloads == [new.name]

    # an older answer is not trusted for installing: the source is asked again
    publish(feed, "3.0")
    monkeypatch.setattr(cli, "FRESH_ANSWER_HOURS", 0)
    assert run_cli("update", "demo", "-y") == 0
    assert len(feed.calls) == 2 and installed("demo").version == "3.0"


@requires_mksquashfs
@requires_unsquashfs
def test_update_check_json(v1, feed, capsys):
    new = publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "--check", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [{
        "id": "demo", "scope": "user", "name": "Demo", "installed": "1.0", "available": "2.0",
        "source": "github.com/demo-org/demo", "size": new.stat().st_size,
        "url": f"https://github.com/demo-org/demo/releases/download/v2.0/{new.name}",
        "result": "update-available", "version": "1.0", "error": None, "notes": [],
    }]


@requires_mksquashfs
@requires_unsquashfs
def test_update_when_everything_is_up_to_date(v1, feed, capsys):
    feed.publish(Path(v1.appimage_path), "v1.0")
    capsys.readouterr()
    assert run_cli("update") == 0
    assert "Demo is up to date (version 1.0)." in capsys.readouterr().out
    assert run_cli("update", "--all", "-y") == 0
    assert "Demo is up to date (version 1.0)." in capsys.readouterr().out
    assert feed.downloads == []


@requires_mksquashfs
@requires_unsquashfs
def test_update_names_the_apps_it_cannot_check(v1, downloads, feed, capsys):
    publish(feed, "2.0")
    assert run_cli("install", "-y", str(demo(downloads / "Quiet-1.0-x86_64.AppImage", "1.0",
                                             name="Quiet", stem="quiet", upd_info=False))) == 0
    capsys.readouterr()
    assert run_cli("update") == 0
    out = capsys.readouterr().out
    assert "Not checked, because they do not say where their new versions are published:" in out
    assert "Quiet" in out.split("published:")[1]
    assert run_cli("update", "quiet") == 1
    err_out = capsys.readouterr()
    assert "Quiet: It does not say where its new versions are published" in err_out.out
    # CLI-6: an app from an archive is not checked for another reason - and said so
    assert run_cli("install", "-y", str(make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip",
                                                     **flat_tree()))) == 0
    capsys.readouterr()
    assert run_cli("update") == 0
    out = flat(capsys.readouterr().out)
    assert "do not say where their new versions are published: Quiet" in out
    assert ("Not checked, because they were installed from an archive: UVtools. To update one, "
            "install the archive of its new version: easy-installer install FILE") in out
    assert "UVtools" not in out.split("published:")[1].split("Not checked")[0]


def test_update_when_only_apps_from_archives_are_installed(downloads, capsys):
    assert run_cli("install", "-y", str(make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip",
                                                     **flat_tree()))) == 0
    capsys.readouterr()
    assert run_cli("update") == 0
    out = flat(capsys.readouterr().out)
    assert "None of your apps says" not in out
    assert "Not checked, because they were installed from an archive: UVtools." in out


def test_update_with_nothing_installed(capsys):
    assert run_cli("update") == 0
    assert "No apps installed yet." in capsys.readouterr().out
    assert run_cli("update", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_update_unknown_app(capsys):
    assert run_cli("update", "ghost") == 1
    assert 'No app with the ID "ghost" is installed.' in capsys.readouterr().err


@requires_mksquashfs
@requires_unsquashfs
def test_update_check_network_error(v1, downloads, feed, capsys):
    feed.fetch_errors["demo-org/demo"] = NetworkError("There is no connection to the internet.")
    capsys.readouterr()
    assert run_cli("update") == 1                         # nothing at all could be checked
    out = capsys.readouterr().out
    assert "Could not be checked:" in out
    assert "Demo: There is no connection to the internet." in out
    # another app that can be checked: the command itself worked
    assert run_cli("install", "-y", str(demo(downloads / "Other-1.0-x86_64.AppImage", "1.0",
                                             name="Other", stem="other", owner="other-org"))) == 0
    publish(feed, "1.5", name="Other", stem="other", owner="other-org")
    capsys.readouterr()
    assert run_cli("update") == 0
    out = capsys.readouterr().out
    assert "Other" in out.splitlines()[1] and "Demo: There is no connection" in out


@requires_mksquashfs
@requires_unsquashfs
def test_update_scope_options(v1, feed, capsys):
    publish(feed, "2.0")
    assert run_cli("update", "--system") == 0
    assert feed.calls == []
    assert run_cli("update", "demo", "--system") == 1
    assert "only for you" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------
# update: installing
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_update_all_installs_and_keeps_the_previous_version(v1, feed, capsys):
    new = publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "--all", "-y") == 0
    out = capsys.readouterr().out
    assert "Updating Demo from version 1.0 to 2.0…" in out
    assert "✓ Demo was updated to version 2.0." in out
    assert "The previous version is kept for 14 days. To go back to it, run: " \
           "easy-installer rollback demo" in " ".join(out.split())
    app = installed("demo")
    assert app.version == "2.0" and app.sha256 == sha256(new)
    assert app.previous["version"] == "1.0"
    assert feed.downloads == [new.name] and no_download_left()
    assert "Summary:" not in out                          # one app: no table needed


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer, code", [("y\n", 0), ("\n", 0), ("n\n", 3), ("", 3)])
def test_update_asks_first(answer, code, v1, feed, capsys, monkeypatch):
    publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "demo", stdin=answer, monkeypatch=monkeypatch) == code
    out = capsys.readouterr().out
    assert "Install this update? [Y/n]" in out
    assert installed("demo").version == ("2.0" if code == 0 else "1.0")
    assert feed.downloads == ([] if code else ["Demo-2.0-x86_64.AppImage"])


@requires_mksquashfs
@requires_unsquashfs
def test_update_without_backups(v1, feed, capsys):
    publish(feed, "2.0")
    save_raw({"backup_days": 0})
    assert run_cli("update", "demo", "-y") == 0
    assert "previous version is kept" not in capsys.readouterr().out
    assert installed("demo").previous is None


@requires_mksquashfs
@requires_unsquashfs
def test_update_json(v1, feed, capsys):
    publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "--all", "-y", "--json") == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["result"], row["installed"], row["version"], row["error"]) == \
        ("updated", "1.0", "2.0", None)


@requires_mksquashfs
@requires_unsquashfs
def test_one_failed_update_does_not_stop_the_others(v1, downloads, feed, capsys):
    assert run_cli("install", "-y", str(demo(downloads / "Other-1.0-x86_64.AppImage", "1.0",
                                             name="Other", stem="other", owner="other-org"))) == 0
    before = Path(v1.appimage_path).read_bytes()
    bad = publish(feed, "2.0")
    publish(feed, "1.5", name="Other", stem="other", owner="other-org")
    feed.download_errors[bad.name] = UpdateError("The download is damaged. Please try again later.",
                                                 details="sha256 mismatch")
    capsys.readouterr()
    assert run_cli("update", "--all", "-y") == 1
    out, err = capsys.readouterr()
    assert "Error: The download is damaged. Please try again later." in err
    assert "Demo was not changed." in err and "sha256 mismatch" not in err
    assert "✓ Other was updated to version 1.5." in out
    summary = out.split("Summary:")[1].splitlines()
    assert summary[1].split() == ["NAME", "FOR", "BEFORE", "NOW", "RESULT"]
    assert summary[2].split() == ["Demo", "Only", "me", "1.0", "1.0", "failed"]
    assert summary[3].split() == ["Other", "Only", "me", "1.0", "1.5", "updated"]
    assert installed("demo").version == "1.0" and Path(v1.appimage_path).read_bytes() == before
    assert installed("other").version == "1.5"
    assert no_download_left()


@requires_mksquashfs
@requires_unsquashfs
def test_update_failure_details_with_verbose(v1, feed, capsys):
    bad = publish(feed, "2.0")
    feed.download_errors[bad.name] = UpdateError("The download is damaged.", details="sha256 mismatch")
    assert run_cli("update", "demo", "-y", "-v") == 1
    assert "sha256 mismatch" in capsys.readouterr().err


@requires_mksquashfs
@requires_unsquashfs
def test_update_shows_the_download_progress(v1, feed, capsys, monkeypatch):
    publish(feed, "2.0")
    monkeypatch.setattr(cli, "ProgressLine", functools.partial(cli.ProgressLine, enabled=True))
    capsys.readouterr()
    assert run_cli("update", "demo", "-y") == 0
    err = capsys.readouterr().err
    assert "Checking Demo…" in err
    assert " 50%  Downloading… 0.1 MB of 0.2 MB" in err
    assert "100%  Downloading… 0.2 MB of 0.2 MB" in err
    assert err.endswith("\r\033[K")                       # the status line is gone at the end


@requires_mksquashfs
@requires_unsquashfs
def test_update_id_after_a_failed_check_does_not_say_up_to_date(v1, feed, capsys):
    """CLI-1: the source answers 404; `update` says so - and so does `update demo` after it."""
    capsys.readouterr()
    assert run_cli("update") == 1
    assert "No update information was found for this app." in flat(capsys.readouterr().out)
    assert run_cli("update", "demo") == 1
    out, err = capsys.readouterr()
    assert "up to date" not in out + err
    assert "No update information was found for this app." in flat(out + err)
    assert run_cli("update", "demo", "-y", "--json") == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["result"] == "error" and "No update information" in rows[0]["error"]
    assert run_cli("details", "demo") == 0
    assert "but that check failed" in flat(capsys.readouterr().out)
    assert run_cli("details", "demo", "--json") == 0
    assert json.loads(capsys.readouterr().out)["last_check_failed"] is True


@requires_mksquashfs
@requires_unsquashfs
def test_ctrl_c_during_the_download_cancels_cleanly(v1, downloads, feed, capsys):
    assert run_cli("install", "-y", str(demo(downloads / "Other-1.0-x86_64.AppImage", "1.0",
                                             name="Other", stem="other", owner="other-org"))) == 0
    publish(feed, "2.0")
    publish(feed, "1.5", name="Other", stem="other", owner="other-org")
    before = Path(v1.appimage_path).read_bytes()

    def press_ctrl_c(update, cancel):
        _thread.interrupt_main()                          # like a real Ctrl+C in the terminal
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancel.is_set(), "Ctrl+C must cancel the update, not stop the program"

    feed.during_download = press_ctrl_c
    capsys.readouterr()
    assert run_cli("update", "--all", "-y") == 3
    out, err = capsys.readouterr()
    # CLI-3: said at once (a stalled download no longer keeps the terminal silent)
    assert "Stopping the download…\n" in err
    assert err.index("Stopping the download…") < err.index("Cancelled. Demo was not changed.")
    assert feed.downloads == ["Demo-2.0-x86_64.AppImage"]  # the next one is not started
    summary = out.split("Summary:")[1]
    assert "cancelled" in summary.splitlines()[2] and "cancelled" in summary.splitlines()[3]
    assert installed("demo").version == "1.0" and Path(v1.appimage_path).read_bytes() == before
    assert installed("other").version == "1.0"
    assert no_download_left()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@requires_mksquashfs
@requires_unsquashfs
def test_ctrl_c_during_the_installation_lets_it_finish(v1, downloads, feed, capsys, monkeypatch):
    assert run_cli("install", "-y", str(demo(downloads / "Other-1.0-x86_64.AppImage", "1.0",
                                             name="Other", stem="other", owner="other-org"))) == 0
    publish(feed, "2.0")
    publish(feed, "1.5", name="Other", stem="other", owner="other-org")
    real_execute = installer.execute_install

    def ctrl_c_while_installing(plan, *, progress=None):
        _thread.interrupt_main()
        time.sleep(0.05)                                  # the handler runs meanwhile
        return real_execute(plan, progress=progress)

    monkeypatch.setattr(installer, "execute_install", ctrl_c_while_installing)
    capsys.readouterr()
    assert run_cli("update", "--all", "-y") == 3
    out, err = capsys.readouterr()
    assert "The installation has already started and is finished first…" in err
    assert "Stopping the download" not in err
    assert "✓ Demo was updated to version 2.0." in out
    assert installed("demo").version == "2.0"             # complete, not half replaced
    assert installed("other").version == "1.0" and feed.downloads == ["Demo-2.0-x86_64.AppImage"]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def _signed(app: InstalledApp) -> None:
    registry().put(dataclasses.replace(app, signature=SignatureInfo(
        "valid", FINGERPRINT, "Demo Maker <maker@example.org>").to_dict()))


@requires_mksquashfs
@requires_unsquashfs
def test_update_from_another_signer_is_not_installed_without_asking(v1, feed, capsys):
    _signed(v1)
    publish(feed, "2.0")                                  # the new file is not signed
    capsys.readouterr()
    assert run_cli("update", "demo", "-y") == 1
    err = capsys.readouterr().err
    assert "was signed by its maker, but this update is not signed by the same maker" in err
    assert "To decide yourself, run the update without -y: easy-installer update demo" in err
    assert installed("demo").version == "1.0"


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer, version, code", [("n\n", "1.0", 3), ("y\n", "2.0", 0)])
def test_update_from_another_signer_asks(answer, version, code, v1, feed, capsys, monkeypatch):
    _signed(v1)
    publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "demo", stdin="y\n" + answer, monkeypatch=monkeypatch) == code
    out = capsys.readouterr().out
    assert "Careful:" in out and "Install this update anyway? [y/N]" in out
    assert installed("demo").version == version
    assert no_download_left()
    assert feed.downloads == ["Demo-2.0-x86_64.AppImage"]  # CLI-4: downloaded once, not twice
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@requires_mksquashfs
@requires_unsquashfs
def test_update_of_a_system_app_without_the_administrator(sys_layout, downloads, feed, capsys,
                                                          monkeypatch):
    from easy_installer.helper import ops

    def install_as_root(op, payload, *, timeout=600):
        assert op == "install"
        return ops.op_install(json.loads(json.dumps(payload)), layout=sys_layout,
                              caller_uid=os.getuid(), caller_gid=os.getgid(), run_commands=False)

    monkeypatch.setattr(privileged, "run_helper", install_as_root)
    assert run_cli("install", "-y", "--system",
                   str(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))) == 0
    monkeypatch.setattr(privileged, "run_helper", _NoAdministrator())   # the password is refused
    publish(feed, "2.0")
    capsys.readouterr()
    assert run_cli("update", "--all", "-y") == 1
    out, err = capsys.readouterr()
    assert "You may be asked for your password." in out
    assert "Administrator tasks are turned off" in err
    assert installed("demo", sys_layout).version == "1.0"
    assert no_download_left()


@pytest.mark.parametrize("kind", ["portable", "copy"])
def test_update_explains_apps_that_are_never_updated(kind, capsys, isolated_env):
    layout = user_layout()
    app = InstalledApp(id="tool--1.0" if kind == "copy" else "tool", name="Tool", version="1.0",
                       scope=Scope.USER, appimage_path=str(layout.apps_dir / "x"),
                       desktop_path="", kind="portable" if kind == "portable" else "appimage",
                       base_id="tool" if kind == "copy" else None, pinned=kind == "copy",
                       update_info="gh-releases-zsync|a|b|latest|B-*.AppImage.zsync")
    registry().put(app)
    assert run_cli("update", app.id) == 1
    out = capsys.readouterr().out
    assert "\n    easy-installer install FILE\n" in out or kind == "copy"   # never split
    out = flat(out)
    if kind == "portable":
        assert "Tool: It was installed from an archive and is not updated automatically." in out
        assert "easy-installer install FILE" in out
    else:
        assert "Tool: It is a copy that was kept next to another version" in out


# ------------------------------------------------------------------------------------------------
# rollback
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def v2(v1, downloads) -> InstalledApp:
    """Demo 2.0 installed over 1.0 (which is kept)."""
    assert run_cli("install", "-y", str(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))) == 0
    app = installed("demo")
    assert app.previous and app.previous["version"] == "1.0"
    return app


@requires_mksquashfs
@requires_unsquashfs
def test_rollback(v2, capsys):
    capsys.readouterr()
    assert run_cli("rollback", "demo", "-y") == 0
    out = capsys.readouterr().out
    assert "Ready to go back to the previous version of Demo" in out
    assert "Installed now:  2.0" in out and "Go back to:" in out and "1.0 (kept since " in out
    assert "Version 2.0 is replaced by the kept version 1.0." in out
    assert "✓ Demo is back at version 1.0." in out
    assert "Version 2.0 is kept. To switch back to it, run: easy-installer rollback demo" in out
    app = installed("demo")
    assert app.version == "1.0" and app.previous["version"] == "2.0"
    assert run_cli("details", "demo") == 0                 # CLI-8
    assert "Installed from:    Demo-1.0-x86_64.AppImage" in capsys.readouterr().out
    # and back again
    assert run_cli("rollback", "demo", "-y") == 0
    assert installed("demo").version == "2.0"
    assert installed("demo").original_filename == "Demo-2.0-x86_64.AppImage"


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer", ["n\n", ""])
def test_rollback_asks_first(answer, v2, capsys, monkeypatch):
    before = registry().get("demo")
    assert run_cli("rollback", "demo", stdin=answer, monkeypatch=monkeypatch) == 3
    assert "Proceed? [Y/n]" in capsys.readouterr().out
    assert registry().get("demo") == before


@requires_mksquashfs
@requires_unsquashfs
def test_rollback_without_an_earlier_version(v1, capsys):
    assert run_cli("rollback", "demo", "-y") == 1
    err = capsys.readouterr().err
    assert "There is no previous version of Demo to go back to." in err
    assert "the replaced version is kept for 14 days" in err
    save_raw({"backup_days": 0})
    assert run_cli("rollback", "demo", "-y") == 1
    assert "easy-installer settings backup-days 14" in capsys.readouterr().err


def test_rollback_unknown(capsys):
    assert run_cli("rollback", "ghost") == 1
    assert 'No app with the ID "ghost"' in capsys.readouterr().err


@pytest.mark.parametrize("archive_kind", [".tar.xz", ".zip"])
def test_rollback_of_a_portable_app(archive_kind, downloads, capsys):
    first = make_archive(downloads / f"blender-4.2.0-linux-x64{archive_kind}", **blender_tree())
    second = make_archive(downloads / f"blender-4.3.0-linux-x64{archive_kind}",
                          **blender_tree("blender-4.3.0-linux-x64"))
    assert run_cli("install", "-y", str(first)) == 0
    assert run_cli("install", "-y", str(second)) == 0
    assert installed("blender").version == "4.3.0"
    capsys.readouterr()
    assert run_cli("rollback", "blender", "-y") == 0
    assert "✓ Blender is back at version 4.2.0." in capsys.readouterr().out
    app = installed("blender")
    assert app.version == "4.2.0" and app.previous["version"] == "4.3.0"
    assert Path(app.appimage_path).is_file()
    # PORT-10: installed from the archive of 4.2.0 - also when the record does not say it
    assert app.original_filename == first.name
    registry().put(dataclasses.replace(app, previous={
        k: v for k, v in app.previous.items() if k != "original_filename"}))
    assert run_cli("rollback", "blender", "-y") == 0
    assert installed("blender").original_filename == second.name


# ------------------------------------------------------------------------------------------------
# details
# ------------------------------------------------------------------------------------------------

DETAIL_KEYS = ({f.name for f in dataclasses.fields(InstalledApp)} - {"extra"} | {
    "status", "update", "can_check_updates", "source", "source_homepage", "last_checked",
    "last_check_failed",
    "origin_host", "previous_available", "previous_kept_until", "data", "data_size",
    "data_shared_with"})


@requires_mksquashfs
@requires_unsquashfs
def test_details_shows_everything(v2, capsys, isolated_env):
    registry().put(dataclasses.replace(
        installed("demo"), origin_url="https://github.com/demo-org/demo/releases/download/v2.0/x",
        signature=SignatureInfo("valid", FINGERPRINT, "Demo Maker").to_dict()))
    config = isolated_env / ".config" / "Demo"
    config.mkdir(parents=True)
    (config / "settings.ini").write_bytes(b"x" * 250_000)
    (isolated_env / ".cache" / "demo").mkdir(parents=True)
    UpdateCache().put(USER, "demo", AvailableUpdate(
        version="3.0", url="https://github.com/demo-org/demo/releases/download/v3.0/D.AppImage",
        filename="D.AppImage", size=2_000_000))
    capsys.readouterr()
    assert run_cli("details", "demo") == 0
    out = flat(capsys.readouterr().out)
    for text in ("App ID: demo", "Version: 2.0", "Installed for: Only for me", "Kind: AppImage",
                 "Status: OK", "App file:", "Menu entry:", "App size:",
                 "Installed on:", "Installed from: Demo-2.0-x86_64.AppImage",
                 "Updates: from github.com/demo-org/demo last checked",
                 "New version: 3.0 is available (2,0 MB)" if "2,0 MB" in out else
                 "New version: 3.0 is available (2.0 MB)",
                 "To install it, run: easy-installer update demo",
                 "Downloaded from: github.com https://github.com/demo-org/demo/releases/",
                 "Signature: signed with a key named “Demo Maker” key 0123 4567 89AB",
                 "Previous version: 1.0, kept until ",
                 "To go back to it, run: easy-installer rollback demo",
                 "Settings and data:", "~/.config/Demo", "settings", "~/.cache/demo",
                 "temporary files", "Together:",
                 "easy-installer uninstall demo --delete-data"):
        assert text in out, text

    assert run_cli("details", "demo", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == DETAIL_KEYS
    assert data["source"] == "github.com/demo-org/demo"
    assert data["source_homepage"] == "https://github.com/demo-org/demo"
    assert data["origin_host"] == "github.com" and data["previous_available"] is True
    assert data["update"]["version"] == "3.0"
    assert {(Path(d["path"]).name, d["kind"]) for d in data["data"]} == {
        ("Demo", "config"), ("demo", "cache")}
    assert data["data_size"] >= 250_000 and data["data_shared_with"] == []


@requires_mksquashfs
@requires_unsquashfs
def test_details_of_an_unsigned_app_without_data(v1, capsys):
    capsys.readouterr()
    assert run_cli("details", "demo") == 0
    out = flat(capsys.readouterr().out)
    assert "Signature: not signed (most apps are not)" in out
    assert "No settings or data folders were found." in out
    assert "Previous version:" not in out and "New version:" not in out


@requires_mksquashfs
@requires_unsquashfs
def test_details_reconciles_first(v1, downloads, capsys):
    os.replace(demo(downloads / "new.AppImage", "2.0"), v1.appimage_path)
    capsys.readouterr()
    assert run_cli("details", "demo") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("Demo updated itself to version 2.0.")
    assert "Version: 2.0" in flat(out)


def test_details_of_a_portable_app(downloads, capsys):
    assert run_cli("install", "-y", str(make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz",
                                                     **electron_tree()))) == 0
    capsys.readouterr()
    assert run_cli("details", "myapp") == 0
    out = capsys.readouterr().out
    assert "portable app, unpacked from an archive into its own folder" in out
    assert "App folder:" in out and "Program:" in out and "/MyApp/my-app" in out
    assert "not checked (the app was installed from an archive)" in out
    assert run_cli("details", "myapp", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "portable" and data["can_check_updates"] is False


def test_details_unknown_and_scopes(sys_layout, capsys, isolated_env):
    assert run_cli("details", "ghost") == 1
    assert 'No app with the ID "ghost"' in capsys.readouterr().err
    for layout, scope in ((user_layout(), Scope.USER), (sys_layout, Scope.SYSTEM)):
        registry(layout).put(InstalledApp(id="tool", name="Tool", version=scope.value,
                                          scope=scope, appimage_path="", desktop_path=""))
    assert run_cli("details", "tool", "--json") == 2
    assert "--user or --system" in capsys.readouterr().err
    assert run_cli("details", "tool", "--json", "--system") == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "system"
    assert run_cli("details", "tool") == 0
    out = flat(capsys.readouterr().out)
    assert "Version: user Installed for: Only for me" in out
    assert "Version: system Installed for: Everyone on this computer" in out
    assert "Another installation of this app uses them, too." not in out   # no data here


# ------------------------------------------------------------------------------------------------
# install: keep both, backups, origin, signature
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_install_suggests_keep_both_and_says_what_is_kept(v1, downloads, capsys, monkeypatch):
    capsys.readouterr()
    src = demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0")
    assert run_cli("install", str(src), stdin="n\n", monkeypatch=monkeypatch) == 3
    out = capsys.readouterr().out
    assert "Ready to update Demo" in out
    assert "Previous version:  1.0 is kept for 14 days, so you can go back to it" in out
    assert "To keep version 1.0 and install this one next to it, add --keep-both." in out
    assert run_cli("install", str(src), "--no-backup", stdin="n\n", monkeypatch=monkeypatch) == 3
    out = capsys.readouterr().out
    assert "Previous version:  is replaced and not kept" in out
    assert run_cli("install", "-y", "--no-backup", str(src)) == 0
    assert installed("demo").previous is None


@requires_mksquashfs
@requires_unsquashfs
def test_install_keep_both(v1, downloads, capsys):
    capsys.readouterr()
    assert run_cli("install", "-y", "--keep-both",
                   str(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))) == 0
    out = capsys.readouterr().out
    assert "Ready to install Demo next to the installed version" in out
    assert 'Installed as:' in out and 'a separate app called "Demo 2.0"; version 1.0 stays ' \
                                      'installed as well' in out
    assert "Change:" not in out and "Previous version:" not in out
    assert "easy-installer launch demo--2.0" in out
    assert installed("demo").version == "1.0"
    copy = installed("demo--2.0")
    assert copy.base_id == "demo" and copy.pinned and copy.version == "2.0"
    assert run_cli("list") == 0
    assert "demo--2.0" in capsys.readouterr().out


@requires_mksquashfs
@requires_unsquashfs
def test_install_keep_both_when_it_does_not_apply(downloads, capsys):
    src = demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0")
    assert run_cli("install", "-y", "--keep-both", "--keep", str(src)) == 0
    assert "--keep-both is not needed: there is no other version of this app to keep." in \
        capsys.readouterr().out
    assert run_cli("install", "-y", "--keep-both", str(installed("demo").appimage_path)) == 0
    assert "--keep-both is not used: this very file is already installed." in \
        capsys.readouterr().out
    assert [a.id for a in registry().all()] == ["demo"]


@requires_mksquashfs
@requires_unsquashfs
def test_install_summary_shows_origin_and_signature(downloads, capsys, monkeypatch):
    monkeypatch.setattr(inspector, "read_origin", lambda path: "https://example.org/get/Demo")
    monkeypatch.setattr(inspector, "read_signature",
                        lambda path, elf: SignatureInfo("valid", FINGERPRINT, "Demo Maker"))
    assert run_cli("install", "-y", str(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))) == 0
    out = capsys.readouterr().out
    assert "Downloaded from:  example.org" in out
    assert "Signature:        signed with a key named “Demo Maker”" in out
    assert installed("demo").origin_url == "https://example.org/get/Demo"
    # the next version with the same key: now the key says who made it
    assert run_cli("install", "-y", str(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))) == 0
    assert "Signature: signed by the same maker as the installed version" in \
        flat(capsys.readouterr().out)


@requires_mksquashfs
@requires_unsquashfs
def test_install_summary_without_origin_or_signature(downloads, capsys):
    assert run_cli("install", "-y", str(demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0"))) == 0
    out = capsys.readouterr().out
    assert "Downloaded from:" not in out and "Signature:" not in out   # unsigned is the norm


@requires_mksquashfs
@requires_unsquashfs
def test_install_warns_when_the_signer_changes(v1, downloads, capsys, monkeypatch):
    _signed(v1)
    capsys.readouterr()
    src = demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0")
    assert run_cli("install", str(src), stdin="n\n", monkeypatch=monkeypatch) == 3
    out = capsys.readouterr().out
    notes = out.split("Please note:")[1]
    assert "Careful: the installed version of Demo was signed by its maker" in " ".join(notes.split())


def test_executable_is_only_for_archives(downloads, capsys):
    appimage = downloads / "Tool.AppImage"
    appimage.write_bytes(b"\x7fELF")
    assert run_cli("install", "-y", "--executable", "bin/tool", str(appimage)) == 2
    assert "--executable can only be used for an app that comes as an archive" in \
        capsys.readouterr().err


# ------------------------------------------------------------------------------------------------
# install: portable archives
# ------------------------------------------------------------------------------------------------


def test_install_portable_archive(downloads, capsys, isolated_env):
    src = make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz", **electron_tree())
    assert run_cli("install", "-y", str(src)) == 0
    out = capsys.readouterr().out
    folder = isolated_env / "Applications" / "MyApp"
    assert "Ready to install MyApp" in out
    assert "(unpacked: " in out
    assert f"App folder:     {folder}" in out
    assert "Program:        my-app" in out
    assert "is kept where it is (the app is unpacked from it)" in out
    assert "✓ MyApp is installed." in out and f"{folder / 'my-app'}" in out
    assert src.is_file()                                  # the archive is kept
    app = installed("myapp")
    assert app.kind == "portable" and app.install_dir == str(folder)
    assert Path(app.appimage_path) == folder / "my-app"


def test_install_portable_asks_which_program(downloads, capsys, monkeypatch):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("install", str(src), stdin="3\n2\ny\n", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "UVtools contains several programs that could start it:" in out
    assert "1) UVtools.sh  (script, recommended)" in out
    assert "2) UVtools     (program)" in out
    assert "UVtoolsCmd" not in out.split("Which one")[0]  # clearly worse: not offered
    assert "Please type one of: 1, 2" in out
    assert "Program:        UVtools\n" in out
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools")


def test_install_portable_enter_takes_the_recommended_program(downloads, capsys, monkeypatch):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("install", str(src), stdin="\n\n", monkeypatch=monkeypatch) == 0
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools.sh")


def test_install_portable_without_an_answer_changes_nothing(downloads, capsys, monkeypatch,
                                                            isolated_env):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("install", str(src), stdin="", monkeypatch=monkeypatch) == 3
    assert "Cancelled. Nothing was changed." in capsys.readouterr().err
    assert registry().all() == [] and not (isolated_env / "Applications").exists()


def test_install_portable_with_yes_takes_the_best_program(downloads, capsys):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("install", "-y", str(src)) == 0
    assert "several programs" not in capsys.readouterr().out
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools.sh")


def test_install_portable_update_keeps_the_chosen_program(downloads, capsys, monkeypatch):
    """CLI-2 / PORT-09: the program chosen for the installed version stays (like in the GUI)."""
    first = make_archive(downloads / "UVtools_linux-x64_v6.1.0.zip", **flat_tree())
    assert run_cli("install", "-y", "--executable", "UVtools", str(first)) == 0
    newer = make_archive(downloads / "UVtools_linux-x64_v6.3.0.zip", **flat_tree())
    capsys.readouterr()
    assert run_cli("install", "-y", str(newer)) == 0
    assert "Program:        UVtools\n" in capsys.readouterr().out
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools")
    # asked: it is offered first, and Enter keeps it
    third = make_archive(downloads / "UVtools_linux-x64_v6.4.0.zip", **flat_tree())
    assert run_cli("install", str(third), stdin="\n\n", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "UVtools     (program, started by the installed version)" in out
    assert "UVtools.sh  (script)" in out and "[1-2, Enter = 2]" in out
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools")
    # one that the inspector would not even offer is kept, too (and offered)
    assert run_cli("install", "-y", "--executable", "UVtoolsCmd", str(first)) == 0
    assert run_cli("install", "-y", str(third)) == 0
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtoolsCmd")
    assert run_cli("install", str(newer), stdin="\n\n", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "1) UVtoolsCmd  (program, started by the installed version)" in out
    assert "[1-3, Enter = 1]" in out
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtoolsCmd")
    # --executable still decides
    assert run_cli("install", "-y", "--executable", "UVtools.sh", str(third)) == 0
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtools.sh")


@pytest.mark.parametrize("given", ["UVtoolsCmd", "./UVtoolsCmd"])
def test_install_portable_executable_option(given, downloads, capsys):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("install", "-y", "--executable", given, str(src)) == 0
    assert installed("uvtools").appimage_path.endswith("/UVtools/UVtoolsCmd")


def test_install_portable_executable_with_the_top_folder(downloads, capsys):
    src = make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz", **electron_tree())
    assert run_cli("install", "-y", "--executable", "MyApp-linux-x64/resources/helper-util",
                   str(src)) == 0
    assert installed("myapp").appimage_path.endswith("/MyApp/resources/helper-util")


def test_install_portable_executable_that_is_not_there(downloads, capsys, isolated_env):
    """CLI-5: said before the summary and the question, not after "Proceed?"."""
    src = make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz", **electron_tree())
    assert run_cli("install", "--executable", "bin/nothing", str(src)) == 2
    out, err = capsys.readouterr()
    assert "Ready to install" not in out and "Proceed?" not in out
    assert "There is no program “bin/nothing” in this archive." in err
    assert "Programs that could start the app: my-app" in err
    assert registry().all() == []
    assert not (isolated_env / "Applications" / "MyApp").exists()
    # the plan says so, too (when the program was set some other way)
    with portable.inspect_portable(src) as info:
        info.executable = "bin/nothing"
        with pytest.raises(InstallError, match="was not found in the archive"):
            installer.plan_install(info)


def test_install_portable_program_with_a_name_that_is_not_utf8(downloads, capsys,
                                                                isolated_env):
    """CLI-7: a friendly error, not "Something unexpected went wrong"."""
    src = make_tar(downloads / "Caf-1.0-linux-x64.tar.gz",
                   {"Caf-1.0/caf\udce9": fake_elf(), "Caf-1.0/caf": fake_elf()},
                   modes={"Caf-1.0/caf\udce9": 0o755, "Caf-1.0/caf": 0o755},
                   tar_format=tarfile.GNU_FORMAT)
    assert run_cli("install", "-y", str(src)) == 0          # the other program is taken
    assert installed("caf").appimage_path.endswith("/caf")
    capsys.readouterr()
    assert run_cli("install", "-y", "--executable", "caf\udce9", str(src)) == 1
    err = capsys.readouterr().err
    assert "cannot be started from there" in err and "unexpected" not in err


@pytest.mark.parametrize("option, message", [
    ("--system", "can only be installed just for you"),
    ("--keep-both", "cannot be installed next to it"),
])
def test_install_portable_refusals(option, message, downloads, capsys):
    src = make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz", **electron_tree())
    assert run_cli("install", "-y", option, str(src)) == 1
    assert message in capsys.readouterr().err
    assert registry().all() == []


def test_install_portable_update_keeps_the_old_folder(downloads, capsys):
    assert run_cli("install", "-y", str(make_archive(downloads / "blender-4.2.0-linux-x64.tar.xz",
                                                     **blender_tree()))) == 0
    capsys.readouterr()
    second = make_archive(downloads / "blender-4.3.0-linux-x64.tar.xz",
                          **blender_tree("blender-4.3.0-linux-x64"))
    assert run_cli("install", "-y", str(second)) == 0
    out = capsys.readouterr().out
    assert "Ready to update Blender" in out
    assert "Previous version:  4.2.0 is kept for 14 days" in out
    assert installed("blender").previous["version"] == "4.2.0"


def test_info_of_an_archive(downloads, capsys):
    src = make_archive(downloads / "UVtools_linux-x64_v6.2.0.zip", **flat_tree())
    assert run_cli("info", str(src)) == 0
    out = capsys.readouterr().out
    assert out.startswith("UVtools\n")
    assert "Version:" in out and "6.2.0" in out and "app archive" in out
    assert "UVtools.sh (script, recommended)" in out and "UVtools (program)" in out
    assert run_cli("info", "--json", str(src)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "portable" and data["app_id"] == "uvtools"
    assert data["executable"] == "UVtools.sh"
    assert [c["path"] for c in data["executables"]][:2] == ["UVtools.sh", "UVtools"]
    assert data["installed"] == [] and data["unpacked_size"] > 0
    assert set(data) == {
        "kind", "path", "file_name", "size", "sha256", "app_id", "name", "display_name",
        "version", "comment", "categories", "arch", "host_arch", "top_folder",
        "desktop_filename", "desktop_entry", "executable", "executables", "icon", "is_electron",
        "terminal", "unpacked_size", "file_count", "origin_url", "data_hints", "installed",
        "warnings"}


@requires_mksquashfs
@requires_unsquashfs
def test_info_of_an_appimage_shows_where_updates_come_from(downloads, capsys):
    src = demo(downloads / "Demo-1.0-x86_64.AppImage", "1.0")
    assert run_cli("info", str(src)) == 0
    out = capsys.readouterr().out
    assert "Updates from:        github.com/demo-org/demo" in out
    assert "not signed (most apps are not)" in out
    assert run_cli("info", "--json", str(src)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "appimage" and data["update_source"]["repo"] == "demo"
    assert data["signature"] is None and "Demo" in data["data_hints"]


# ------------------------------------------------------------------------------------------------
# uninstall --delete-data
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def demo_data(isolated_env) -> list[Path]:
    config = isolated_env / ".config" / "Demo"
    config.mkdir(parents=True)
    (config / "settings.ini").write_bytes(b"x" * 1_500_000)
    cache = isolated_env / ".cache" / "demo"
    cache.mkdir(parents=True)
    return [config, cache]


def trashed(home: Path) -> set[str]:
    folder = home / ".local" / "share" / "Trash" / "files"
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_delete_data(v1, demo_data, capsys, isolated_env):
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "--delete-data", "demo") == 0
    out = capsys.readouterr().out
    assert "These settings and data folders of Demo were found:" in out
    assert "~/.config/Demo" in out and ("1,5 MB" in out or "1.5 MB" in out)   # LC_NUMERIC
    assert "~/.cache/demo" in out and "empty" in out
    assert "✓ Demo was uninstalled." in out
    assert "Its settings and data folders were moved to the trash:" in out
    assert "Your personal files and settings were kept." not in out
    assert not any(path.exists() for path in demo_data)
    assert trashed(isolated_env) == {"Demo", "demo"}
    assert registry().get("demo") is None


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answers, data_kept", [("n\ny\n", True), ("y\ny\n", False)])
def test_uninstall_delete_data_asks(answers, data_kept, v1, demo_data, capsys, monkeypatch):
    capsys.readouterr()
    assert run_cli("uninstall", "--delete-data", "demo", stdin=answers,
                   monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Also move these folders to the trash? [y/N]" in out
    assert "Uninstall Demo (Only for me)? [y/N]" in out
    assert all(path.exists() for path in demo_data) is data_kept
    assert ("Your personal files and settings were kept." in out) is data_kept


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_delete_data_cancelled_changes_nothing(v1, demo_data, capsys, monkeypatch):
    assert run_cli("uninstall", "--delete-data", "demo", stdin="y\nn\n",
                   monkeypatch=monkeypatch) == 3
    assert all(path.exists() for path in demo_data) and registry().get("demo") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_delete_data_without_data(v1, capsys):
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "--delete-data", "demo") == 0
    out = capsys.readouterr().out
    assert "No settings or data folders of Demo were found." in out
    assert "Your personal files and settings were kept." in out


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_delete_data_keeps_what_another_installation_uses(v1, downloads, demo_data,
                                                                    capsys):
    assert run_cli("install", "-y", "--keep-both",
                   str(demo(downloads / "Demo-2.0-x86_64.AppImage", "2.0"))) == 0
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "--delete-data", "demo--2.0") == 0
    out = capsys.readouterr().out
    assert "The settings and data of Demo 2.0 are kept, because another installation of it " \
           "still uses them." in " ".join(out.split())
    assert all(path.exists() for path in demo_data)


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_delete_data_reports_what_could_not_be_trashed(v1, demo_data, capsys,
                                                                 monkeypatch):
    def refuse(paths):
        return [(Path(p), "It could not be moved to the trash, so it was left alone.")
                for p in paths]

    monkeypatch.setattr(appdata, "move_to_trash", refuse)
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "--delete-data", "demo") == 1
    out, err = capsys.readouterr()
    assert "✓ Demo was uninstalled." in out
    assert "Error: 2 folders could not be moved to the trash:" in err
    assert "~/.config/Demo: It could not be moved to the trash, so it was left alone." in err
    assert registry().get("demo") is None and all(path.exists() for path in demo_data)


def test_uninstall_delete_data_of_a_portable_app(downloads, capsys, isolated_env):
    assert run_cli("install", "-y", str(make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz",
                                                     **electron_tree()))) == 0
    data = isolated_env / ".config" / "MyApp"
    data.mkdir(parents=True)
    (data / "Preferences").write_text("{}")
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "--delete-data", "myapp") == 0
    assert not data.exists() and not (isolated_env / "Applications" / "MyApp").exists()
    assert "MyApp" in trashed(isolated_env)


def test_uninstall_of_a_portable_app_says_what_happens_to_its_folder(downloads, capsys,
                                                                    monkeypatch, isolated_env):
    """What the app saved in its own folder is not "kept": it goes to the trash (and the
    command says so before and after)."""
    assert run_cli("install", "-y", str(make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz",
                                                     **electron_tree()))) == 0
    folder = isolated_env / "Applications" / "MyApp"
    (folder / "saves").mkdir()
    (folder / "saves" / "slot1.sav").write_text("3 hours of progress")
    capsys.readouterr()
    assert run_cli("uninstall", "myapp", stdin="y\n", monkeypatch=monkeypatch) == 0
    out = flat(capsys.readouterr().out)
    assert "Your personal files and settings are kept." not in out
    assert ("The app will be removed. Anything it saved in its own folder is moved to the "
            "trash. Your other personal files and settings are kept.") in out
    assert "Your other personal files and settings were kept." in out
    assert "The folder of MyApp was moved to the trash, because files were saved in it" in out
    assert not folder.exists() and "MyApp" in trashed(isolated_env)
    trash_file = isolated_env / ".local/share/Trash/files/MyApp/saves/slot1.sav"
    assert trash_file.read_text() == "3 hours of progress"


# ------------------------------------------------------------------------------------------------
# repair
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_repair_makes_the_menu_entry_again(v1, capsys):
    Path(v1.desktop_path).unlink()
    capsys.readouterr()
    assert run_cli("repair", "demo") == 0
    out = capsys.readouterr().out
    assert "✓ Demo was repaired." in out
    entry_text = Path(v1.desktop_path).read_text()
    assert DesktopEntry.parse(entry_text).get("Exec").startswith(v1.appimage_path)


@requires_mksquashfs
@requires_unsquashfs
def test_repair_without_the_app_file(v1, capsys):
    Path(v1.appimage_path).unlink()
    assert run_cli("repair", "demo") == 1
    err = capsys.readouterr().err
    assert "The app file of Demo is missing" in err and "Install the app again" in err


def test_repair_of_a_portable_app(downloads, capsys):
    assert run_cli("install", "-y", str(make_archive(downloads / "MyApp-1.2.3-linux-x64.tar.gz",
                                                     **electron_tree()))) == 0
    app = installed("myapp")
    Path(app.desktop_path).unlink()
    assert run_cli("repair", "myapp") == 0
    assert Path(app.desktop_path).is_file()


def test_repair_of_a_system_app_needs_the_administrator(sys_layout, capsys):
    sys_layout.apps_dir.mkdir(parents=True)
    appimage = sys_layout.apps_dir / "tool.AppImage"
    appimage.write_bytes(b"\x7fELF")
    registry(sys_layout).put(InstalledApp(id="tool", name="Tool", version="1", scope=Scope.SYSTEM,
                                          appimage_path=str(appimage), desktop_path=""))
    assert run_cli("repair", "tool") == 1
    out, err = capsys.readouterr()
    assert "Error: " in err


# ------------------------------------------------------------------------------------------------
# subprocess: the real entry point
# ------------------------------------------------------------------------------------------------


def run_module(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)  # already isolated by conftest (HOME, XDG_*, pkexec disabled)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run([sys.executable, "-m", "easy_installer", *args], capture_output=True,
                          text=True, env=env, timeout=120, cwd=str(REPO))


def test_subprocess_settings():
    # Only commands that neither read the real "for everyone" registry nor could go online:
    # a subprocess cannot be given the fake root and the fake network of the other tests.
    proc = run_module("settings", "backup-days", "30")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(run_module("settings", "--json").stdout)["backup-days"] == 30
    proc = run_module("update", "--help")
    assert proc.returncode == 0 and "--check" in proc.stdout
