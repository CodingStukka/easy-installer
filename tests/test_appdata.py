"""App data clean-up (DESIGN §24). Data loss is the worst possible bug here: most of these tests
are about what must NOT be found and what must NOT be moved."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

from easy_installer.core import appdata
from easy_installer.core.appdata import (
    DataLocation,
    data_hints_for,
    dir_size,
    find_app_data,
    move_to_trash,
)

requires_gio = pytest.mark.skipif(shutil.which("gio") is None, reason="gio is not installed")
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can read everything")


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def make(folder: Path, files: dict[str, bytes | str] | None = None) -> Path:
    """Create ``folder`` with ``files`` (relative name -> content); returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {"settings.ini": b"x" * 10}).items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    return folder


def snapshot(root: Path) -> dict[str, object]:
    """Everything below ``root``: relative path -> content / link target / "dir"."""
    result: dict[str, object] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current, name)
            key = str(path.relative_to(root))
            if path.is_symlink():
                result[key] = ("link", os.readlink(path))
            elif path.is_dir():
                result[key] = "dir"
            else:
                result[key] = path.read_bytes()
    return result


def found(hints, **kwargs) -> dict[str, str]:
    """{path relative to the home: kind} of find_app_data()."""
    home = Path(os.environ["HOME"]).resolve()
    return {str(location.path.relative_to(home)): location.kind
            for location in find_app_data(hints, **kwargs)}


@pytest.fixture
def home(isolated_env, monkeypatch):
    """The isolated home with the usual folders (and a state folder, which conftest leaves out)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_env / ".local" / "state"))
    for name in (".config", ".local/share", ".cache", ".local/state", "Applications", "Documents"):
        (isolated_env / name).mkdir(parents=True, exist_ok=True)
    return isolated_env


@pytest.fixture
def no_gio(tmp_path, monkeypatch):
    """A PATH without gio: move_to_trash() uses its own implementation of the trash."""
    empty = tmp_path / "no-tools"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("gio") is None


def fake_gio(tmp_path: Path, monkeypatch, script: str) -> Path:
    """Put a fake `gio` first on PATH; its arguments are appended to the returned log file."""
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    log = tmp_path / "gio.log"
    tool = directory / "gio"
    tool.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{log}"\n{script}\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    return log


def trash_dir(home: Path) -> Path:
    return home / ".local" / "share" / "Trash"


# --------------------------------------------------------------------------------------------
# data_hints_for
# --------------------------------------------------------------------------------------------

def hints(name=None, app_id=None, desktop_stem=None, wm_class=None, exec_name=None) -> list[str]:
    return data_hints_for(name=name, app_id=app_id, desktop_stem=desktop_stem,
                          wm_class=wm_class, exec_name=exec_name)


def test_hints_for_freecad():
    result = hints("FreeCAD", "org.freecad.FreeCAD", "org.freecad.FreeCAD", "FreeCAD", "AppRun")
    assert result[:2] == ["FreeCAD", "org.freecad.FreeCAD"]
    folded = [hint.casefold() for hint in result]
    assert len(folded) == len(set(folded)), "duplicates (ignoring case)"
    assert "apprun" not in folded and "org" not in folded and "freecad.freecad" not in folded


def test_hints_for_an_electron_app():
    result = hints("T3 Code (Alpha)", "t3code", "t3code", "t3code", "AppRun")
    assert result == ["T3 Code (Alpha)", "t3code", "t3code-updater"]


def test_names_are_never_split_into_words():
    result = hints("My Great Editor (Beta)", "my-great-editor", "my-great-editor", "My Great Editor", "AppRun")
    folded = {hint.casefold() for hint in result}
    assert folded == {"my great editor (beta)", "my-great-editor", "my great editor",
                      "my-great-editor-updater"}
    for word in ("my", "great", "editor", "beta", "(beta)"):
        assert word not in folded


def test_reverse_dns_gives_its_last_component():
    assert "OpenSCAD" in hints(app_id="org.openscad.OpenSCAD")
    assert "krita" in hints(desktop_stem="org.kde.krita")
    assert "Project" in hints(wm_class="io.github.someone.Project")
    # ... but never a middle part, and nothing for names that merely contain dots
    assert hints(app_id="org.freecad.FreeCAD") == ["org.freecad.FreeCAD", "FreeCAD"]
    assert hints(app_id="cool-app-1.2.3") == ["cool-app-1.2.3"]
    assert hints(app_id="app.v1.2.beta2") == ["app.v1.2.beta2", "beta2"]
    assert hints(app_id="my.app") == ["my.app"]
    assert hints(app_id="com.gitbutler.app") == ["com.gitbutler.app"]      # "app" is generic
    assert hints(app_id="a.b..c") == ["a.b..c"]


def test_keep_both_copies_use_the_main_apps_names():
    assert hints(app_id="org.freecad.FreeCAD--1.1.3") == ["org.freecad.FreeCAD", "FreeCAD"]
    assert hints(app_id="t3code--0.0.42") == ["t3code", "t3code-updater"]


def test_executable_name_variants():
    assert hints(exec_name="/opt/UVtools/UVtools.sh") == ["UVtools", "UVtools-updater"]
    assert hints(exec_name="balena-etcher") == ["balena-etcher", "balena-etcher-updater"]
    assert hints(exec_name="bin/freecad.bin") == ["freecad", "freecad-updater"]
    assert hints(exec_name="Tool.AppImage") == ["Tool", "Tool-updater"]
    assert hints(exec_name="AppRun") == []
    assert hints(exec_name=".sh") == []


@pytest.mark.parametrize("value", [
    "", " ", "a", "ab", "t3", "..", ".", "...", ".hidden", "a/b", "../x", "/etc", "a\\b",
    "share", "local", "config", "cache", "state", "Share", "CACHE", "applications", "icons",
    "mime", "fonts", "themes", "autostart", "systemd", "dconf", "gtk-3.0", "gtk-4.0", "pulse",
    "menus", "trash", "Trash", "flatpak", "keyrings", "easy-installer", "Easy-Installer",
    "com.roothirsch.EasyInstaller", "google-chrome", "chromium", "mozilla", "firefox",
    "BraveSoftware", "ssh", "gnupg", "pki", "git", "docker", "AppRun", "app", "Electron",
    "python3", "bash", "claude", "1.2.3", "2026", "---", "a\x00b", "ab\x1b[0m", "ab\nc",
    "evil\u202ename", "x" * 101, "\ud800abc",
])
def test_unusable_names_never_become_hints(value):
    assert hints(name=value) == []
    assert hints(wm_class=value) == []
    if "/" not in value:                  # a program name may be a path; its last part counts
        assert hints(exec_name=value) == []
    assert appdata._clean_hint(value) is None


def test_hints_tolerate_missing_and_odd_values():
    assert hints() == []
    assert data_hints_for(name=5, app_id=None, desktop_stem=["x"], wm_class=b"abc",  # type: ignore[arg-type]
                          exec_name={"a": 1}) == []
    assert hints(name="  Pen  ") == ["Pen"]
    assert hints(name="Pen", wm_class="pen", desktop_stem="PEN") == ["Pen", "PEN-updater"]
    assert hints(name="abc\n") == ["abc"]                # surrounding white space is trimmed


def test_number_of_hints_is_limited():
    assert len(appdata._clean_hints(f"name{i}" for i in range(500))) == appdata.MAX_HINTS


def test_every_denied_name_is_lower_case_and_refused():
    for name in appdata._DENIED_NAMES:
        assert name == name.casefold()
        assert appdata._clean_hint(name) is None and appdata._clean_hint(name.upper()) is None


# --------------------------------------------------------------------------------------------
# find_app_data: what is found
# --------------------------------------------------------------------------------------------

def test_freecad_folders_are_found(home):
    make(home / ".config/FreeCAD", {"user.cfg": b"a" * 100, "Macro/x.py": b"b" * 50})
    make(home / ".local/share/FreeCAD", {"Mod/addon/init.py": b"c" * 7})
    make(home / ".cache/FreeCAD", {"thumb.png": b"d" * 1000})
    wanted = data_hints_for(name="FreeCAD", app_id="org.freecad.FreeCAD",
                            desktop_stem="org.freecad.FreeCAD", wm_class="FreeCAD",
                            exec_name="AppRun")
    result = find_app_data(wanted)
    assert result == [
        DataLocation(home / ".config/FreeCAD", "config", 150, 2),
        DataLocation(home / ".local/share/FreeCAD", "data", 7, 1),
        DataLocation(home / ".cache/FreeCAD", "cache", 1000, 1),
    ]


def test_electron_app_folders_are_found(home):
    make(home / ".config/T3 Code (Alpha)")
    make(home / ".config/t3code")
    make(home / ".cache/t3code-updater", {"pending/T3-Code-0.0.43.AppImage": b"x" * 4096})
    make(home / ".config/T3")                 # somebody else's
    make(home / ".config/Code")               # VS Code!
    wanted = data_hints_for(name="T3 Code (Alpha)", app_id="t3code", desktop_stem="t3code",
                            wm_class="t3code", exec_name="AppRun")
    assert found(wanted) == {".config/T3 Code (Alpha)": "config", ".config/t3code": "config",
                             ".cache/t3code-updater": "cache"}


def test_all_five_kinds_in_order(home):
    for folder in (".config/Krita", ".local/share/krita", ".cache/KRITA", ".local/state/krita",
                   ".krita"):
        make(home / folder)
    result = find_app_data(["krita"])
    assert [location.kind for location in result] == ["config", "data", "cache", "state", "home"]
    assert [location.path.name for location in result] == ["Krita", "krita", "KRITA", "krita", ".krita"]
    assert appdata.KINDS == ("config", "data", "cache", "state", "home")


def test_state_folder_defaults_to_local_state(home, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME")
    make(home / ".local/state/myapp")
    assert found(["myapp"]) == {".local/state/myapp": "state"}


def test_only_exact_names_match(home):
    for name in ("FreeCAD2", "FreeCAD-backup", "MyFreeCAD", "FreeCA", "Free CAD", "FreeCAD.old",
                 "org.freecad.FreeCAD.bak", "freecad "):
        make(home / ".config" / name)
    make(home / ".config/other/FreeCAD")          # not a direct child
    make(home / ".config/FreeCAD")
    make(home / ".local/share/Other/FreeCAD")
    make(home / "FreeCAD")                        # the user's own folder: no leading dot
    make(home / "Documents/FreeCAD")
    make(home / "Documents/.FreeCAD")
    assert found(["freecad", "org.freecad.FreeCAD"]) == {".config/FreeCAD": "config"}


def test_home_folder_needs_the_leading_dot(home):
    make(home / ".FreeCAD")
    make(home / "..FreeCAD")
    make(home / "FreeCAD")
    assert found(["FreeCAD"]) == {".FreeCAD": "home"}


def test_a_single_name_may_be_given_as_a_string(home):
    make(home / ".config/FreeCAD")
    for letter in "FreeCAD":
        make(home / ".config" / (letter * 3))
    assert found("FreeCAD") == {".config/FreeCAD": "config"}


def test_the_given_environment_is_used(home, tmp_path):
    other = tmp_path / "other-home"
    make(other / ".config/myapp")
    make(other / "xdg-data/myapp")
    make(home / ".config/myapp")
    env = {"HOME": str(other), "XDG_DATA_HOME": str(other / "xdg-data")}
    result = find_app_data(["myapp"], env=env)
    assert [(location.path, location.kind) for location in result] == [
        (other / ".config/myapp", "config"), (other / "xdg-data/myapp", "data")]


def test_nothing_to_find(home):
    assert find_app_data(["FreeCAD"]) == []
    assert find_app_data([]) == []
    assert find_app_data(["", None, 5]) == []     # type: ignore[list-item]


# --------------------------------------------------------------------------------------------
# find_app_data: what must never be found
# --------------------------------------------------------------------------------------------

DANGEROUS_HINTS = ["share", "local", "config", "cache", "state", "", "..", ".", "a/b", "../x",
                   "/", "~", "*", "?", "??*", "[a-z]*", ".*", "easy-installer", "applications",
                   "icons", "mime", "Trash", "autostart", "ab", "Applications", ".config",
                   ".local", "local/share", ".local/share", "systemd", "keyrings", "ssh", "gnupg"]


@pytest.fixture
def lived_in_home(home):
    """A home with the folders a real one has - none of them is app data."""
    for folder in (".config/easy-installer", ".config/autostart", ".config/systemd/user",
                   ".config/dconf", ".config/gtk-4.0", ".config/pulse", ".config/ab",
                   ".local/share/applications", ".local/share/icons/hicolor", ".local/share/mime",
                   ".local/share/easy-installer", ".local/share/Trash/files", ".local/share/keyrings",
                   ".local/share/share", ".local/share/local", ".local/share/a/b",
                   ".cache/easy-installer", ".cache/thumbnails", ".cache/cache",
                   ".local/state/state", ".ssh", ".gnupg", ".mozilla/firefox", ".share",
                   ".config/config", ".config/a/b", "Applications/Foo"):
        make(home / folder)
    (home / ".local/share/easy-installer/registry.json").write_text('{"format": 1, "apps": {}}')
    return home


@pytest.mark.parametrize("hint", DANGEROUS_HINTS)
def test_dangerous_hints_find_nothing(lived_in_home, hint):
    before = snapshot(lived_in_home)
    assert find_app_data([hint]) == []
    assert snapshot(lived_in_home) == before


def test_all_dangerous_hints_together_find_nothing(lived_in_home):
    assert find_app_data(DANGEROUS_HINTS) == []


def test_glob_characters_are_plain_characters(home):
    for name in ("Apple", "App", "Apps2", "Appx"):
        make(home / ".config" / name)
    assert found(["App*", "App?", "[A]pple", "A{pp,bc}le", "Ap+le", "^Apple$", "Appl."]) == {}
    make(home / ".config/App*")
    make(home / ".config/[A]pple")
    assert found(["App*", "[A]pple"]) == {".config/App*": "config", ".config/[A]pple": "config"}


def test_own_folders_are_protected_even_without_the_deny_list(lived_in_home, monkeypatch):
    """Second line of defence: the structure, not only the list of names."""
    monkeypatch.setattr(appdata, "_DENIED_NAMES", frozenset())
    monkeypatch.setattr(appdata, "MIN_HINT_LENGTH", 1)
    for hint in ("easy-installer", "applications", "icons", "mime", "Trash", "config", "local",
                 "cache", "share", "state", "Applications"):
        result = found([hint])
        for path in result:
            assert path in (".local/share/share", ".local/share/local", ".cache/cache",
                            ".config/config", ".local/state/state", ".share"), (hint, path)
    assert found(["easy-installer"]) == {}
    assert found(["applications"]) == {} and found(["icons"]) == {} and found(["mime"]) == {}
    assert found(["Trash"]) == {}
    # ~/.config, ~/.cache, ~/.local: the XDG folders themselves (or what contains them)
    assert found(["config"]) == {".config/config": "config"}
    assert found(["cache"]) == {".cache/cache": "cache"}
    assert found(["local"]) == {".local/share/local": "data"}


def test_symlinked_folder_is_never_a_candidate(home, tmp_path):
    target = make(tmp_path / "elsewhere/real-data", {"precious.txt": b"keep me"})
    inside = make(home / "Documents/project")
    (home / ".config/myapp").symlink_to(target)
    (home / ".local/share/myapp").symlink_to(inside)
    (home / ".myapp").symlink_to(home / "Documents")
    (home / ".cache/myapp").symlink_to("/nonexistent")
    assert find_app_data(["myapp"]) == []
    assert (target / "precious.txt").read_bytes() == b"keep me"


def test_files_are_not_candidates(home):
    (home / ".config/myapp").write_text("a settings file, not a folder")
    (home / ".myapp").write_text("x")
    os.mkfifo(home / ".cache/myapp")
    assert find_app_data(["myapp"]) == []


def test_xdg_folder_outside_the_home_is_not_searched(home, tmp_path, monkeypatch):
    outside = tmp_path / "shared-config"
    make(outside / "myapp")
    make(home / ".cache/myapp")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(outside))
    assert found(["myapp"]) == {".cache/myapp": "cache"}
    monkeypatch.setenv("XDG_CONFIG_HOME", "/")
    monkeypatch.setenv("XDG_DATA_HOME", "/usr/share")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert found(["myapp"]) == {".cache/myapp": "cache"}


def test_xdg_folder_linked_to_outside_the_home_is_not_searched(home, tmp_path):
    outside = tmp_path / "other-disk/config"
    make(outside / "myapp")
    (home / ".config").rmdir()
    (home / ".config").symlink_to(outside)
    assert find_app_data(["myapp"]) == []


def test_xdg_folder_linked_inside_the_home_gives_real_paths(home):
    real = make(home / "dotfiles/config/myapp").parent
    (home / ".config").rmdir()
    (home / ".config").symlink_to(real)
    assert [location.path for location in find_app_data(["myapp"])] == [real / "myapp"]


def test_xdg_folder_equal_to_the_home_is_not_searched(home, monkeypatch):
    make(home / "Documents")
    make(home / "myapp")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home.parent))
    assert find_app_data(["Documents", "myapp", "home"]) == []


def test_relative_xdg_values_are_ignored(home, monkeypatch):
    make(home / ".config/myapp")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert found(["myapp"]) == {".config/myapp": "config"}


def test_same_folder_for_two_xdg_variables_is_listed_once(home, monkeypatch):
    make(home / ".config/myapp")
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".config") + "/")
    assert found(["myapp"]) == {".config/myapp": "config"}


def test_home_that_is_a_symlink(home, tmp_path, monkeypatch):
    make(home / ".config/myapp")
    make(home / ".myapp")
    link = tmp_path / "home-link"
    link.symlink_to(home)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, os.environ[name].replace(str(home), str(link)))
    result = find_app_data(["myapp"])
    # real paths inside the real home, never the link
    assert [location.path for location in result] == [home / ".config/myapp", home / ".myapp"]


@pytest.mark.parametrize("value", ["/", "relative/home", "/nonexistent/home", "/etc/passwd"])
def test_unusable_home_finds_nothing(home, value):
    make(home / ".config/myapp")
    env = {"HOME": value, "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
           "XDG_STATE_HOME": str(home / ".local/state")}
    assert find_app_data(["myapp"], env=env) == []


def test_somebody_elses_home_or_folder_is_left_alone(home, monkeypatch):
    make(home / ".config/myapp")
    assert found(["myapp"]) == {".config/myapp": "config"}
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert find_app_data(["myapp"]) == []


def test_folder_of_another_owner_is_skipped(home, monkeypatch):
    make(home / ".config/myapp")
    make(home / ".cache/myapp")
    real_scandir = os.scandir
    foreign = str(home / ".config")

    class Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name, self.path = entry.name, entry.path

        def stat(self, *, follow_symlinks=True):
            st = self._entry.stat(follow_symlinks=follow_symlinks)
            values = list(st)
            values[stat.ST_UID] = st.st_uid + 1
            return os.stat_result(values)

    class Listing:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

        def __iter__(self):
            return (Entry(entry) for entry in self._inner)

    monkeypatch.setattr(os, "scandir", lambda path=".": Listing(path) if str(path) == foreign
                        else real_scandir(path))
    assert found(["myapp"]) == {".cache/myapp": "cache"}


def test_mount_points_are_skipped(home, monkeypatch):
    make(home / ".config/myapp")
    make(home / ".cache/myapp")
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount",
                        lambda path: str(path) == str(home / ".config/myapp") or real_ismount(path))
    assert found(["myapp"]) == {".cache/myapp": "cache"}


def test_exclude_is_respected(home):
    config = make(home / ".config/myapp")
    data = make(home / ".local/share/myapp", {"app/bin/run": b"x"})
    cache = make(home / ".cache/myapp")
    assert len(find_app_data(["myapp"])) == 3
    assert found(["myapp"], exclude=[config]) == {".local/share/myapp": "data", ".cache/myapp": "cache"}
    # a folder that CONTAINS something excluded (e.g. the app's install folder) stays
    assert found(["myapp"], exclude=[data / "app"]) == {".config/myapp": "config", ".cache/myapp": "cache"}
    # ... and so does anything inside an excluded folder
    assert found(["myapp"], exclude=[home / ".cache"]) == {".config/myapp": "config",
                                                          ".local/share/myapp": "data"}
    assert found(["myapp"], exclude=[str(cache), config, data]) == {}
    assert found(["myapp"], exclude=iter([home / "unrelated"])) != {}


def test_exclude_through_a_symlink(home, tmp_path):
    config = make(home / ".config/myapp")
    link = tmp_path / "link-to-config"
    link.symlink_to(config)
    assert find_app_data(["myapp"], exclude=[link]) == []


def test_folder_that_contains_the_apps_folder_is_never_offered(home, monkeypatch):
    apps = make(home / ".local/share/MyApp/apps")
    make(home / ".config/MyApp")
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(apps))
    assert found(["MyApp"]) == {".config/MyApp": "config"}
    # the apps folder itself
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(home / ".local/share/MyApp"))
    assert found(["MyApp"]) == {".config/MyApp": "config"}
    # a hidden apps folder in the home
    hidden = make(home / ".myapp")
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(hidden / "installed"))
    assert found(["MyApp"]) == {".config/MyApp": "config", ".local/share/MyApp": "data"}
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(home / "Applications"))
    assert found(["MyApp"]) == {".config/MyApp": "config", ".local/share/MyApp": "data",
                                ".myapp": "home"}


def write_registry(home: Path, apps: object) -> None:
    folder = home / ".local/share/easy-installer"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "registry.json").write_text(json.dumps({"format": 1, "apps": apps}))


def test_folder_with_an_installed_app_in_it_is_never_offered(home, no_gio):
    """E.g. the apps folder used to be somewhere else: what the registry lists must stay."""
    old_apps = make(home / ".local/share/MyApps", {"Other.AppImage": b"an installed app"})
    portable = make(home / ".config/Suite/portable/Tool", {"tool": b"x"})
    backup = make(home / ".cache/Suite", {"old/Thing-1.0.AppImage": b"previous version"})
    make(home / ".local/state/Suite")
    make(home / ".suite", {"icons/thing.png": b"png"})
    write_registry(home, {
        "other": {"id": "other", "appimage_path": str(old_apps / "Other.AppImage"),
                  "desktop_path": str(home / ".local/share/applications/easyinstaller-other.desktop"),
                  "icon_paths": [str(home / ".suite/icons/thing.png"), 5, None]},
        "tool": {"id": "tool", "kind": "portable", "install_dir": str(portable),
                 "appimage_path": str(portable / "tool"), "icon_paths": "nonsense",
                 "previous": {"path": str(backup / "old/Thing-1.0.AppImage")}},
        "junk": ["not", "an", "app"],
        "more-junk": {"appimage_path": "relative/path", "previous": "x", "install_dir": 7},
    })
    assert found(["MyApps", "Suite"]) == {".local/state/Suite": "state"}
    before = snapshot(home)
    for folder in (old_apps, home / ".config/Suite", backup, home / ".suite",
                   old_apps / "Other.AppImage", portable / "tool"):
        ((_path, error),) = move_to_trash([folder])
        assert error and "protected" in error, folder
    assert snapshot(home) == before


@pytest.mark.parametrize("content", ["", "{", "[]", '{"apps": []}', '{"apps": {"a": 1}}', "null",
                                     '{"format": 1}', "\xff\xfe"])
def test_damaged_registry_is_tolerated_and_not_touched(home, content):
    folder = home / ".local/share/easy-installer"
    folder.mkdir(parents=True)
    (folder / "registry.json").write_bytes(content.encode("latin-1"))
    make(home / ".config/myapp")
    assert found(["myapp"]) == {".config/myapp": "config"}
    assert [path.name for path in folder.iterdir()] == ["registry.json"]
    assert (folder / "registry.json").read_bytes() == content.encode("latin-1")


def test_folder_that_contains_an_xdg_folder_is_never_offered(home, monkeypatch):
    make(home / ".config/myapp/cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".config/myapp/cache"))
    assert find_app_data(["myapp"]) == []


def test_unreadable_base_folder_is_tolerated(home):
    make(home / ".cache/myapp")
    (home / ".config").chmod(0)
    try:
        assert found(["myapp"]) == {".cache/myapp": "cache"}
    finally:
        (home / ".config").chmod(0o755)


def test_search_never_changes_anything(lived_in_home):
    make(lived_in_home / ".config/FreeCAD")
    before = snapshot(lived_in_home)
    find_app_data(["FreeCAD", "org.freecad.FreeCAD"] + DANGEROUS_HINTS)
    assert snapshot(lived_in_home) == before


def test_too_many_entries_in_a_base_folder_are_bounded(home, monkeypatch):
    for index in range(30):
        make(home / ".config" / f"app{index:02d}")
    monkeypatch.setattr(appdata, "MAX_BASE_ENTRIES", 5)
    assert len(find_app_data([f"app{index:02d}" for index in range(20)])) <= 5


# --------------------------------------------------------------------------------------------
# dir_size
# --------------------------------------------------------------------------------------------

def test_dir_size_counts_files(tmp_path):
    root = make(tmp_path / "data", {"a": b"x" * 10, "sub/b": b"y" * 20, "sub/deep/c": b"", "d": b"z"})
    (root / "empty").mkdir()
    assert dir_size(root) == (31, 4)
    assert dir_size(str(root)) == (31, 4)     # type: ignore[arg-type]


def test_dir_size_of_nothing(tmp_path):
    assert dir_size(tmp_path / "missing") == (0, 0)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert dir_size(empty) == (0, 0)
    single = tmp_path / "file"
    single.write_bytes(b"12345")
    assert dir_size(single) == (5, 1)
    assert dir_size(Path("a\0b")) == (0, 0)


def test_dir_size_never_follows_symlinks(tmp_path):
    big = make(tmp_path / "outside", {"huge.bin": b"x" * 100_000})
    root = make(tmp_path / "data", {"a": b"12345"})
    (root / "link-to-folder").symlink_to(big)
    (root / "link-to-file").symlink_to(big / "huge.bin")
    (root / "dangling").symlink_to("/nonexistent/target")
    (root / "loop").symlink_to(root)
    size, files = dir_size(root)
    links = sum(len(os.readlink(root / name)) for name in
                ("link-to-folder", "link-to-file", "dangling", "loop"))
    assert (size, files) == (5 + links, 5)
    # a link itself is one small entry, whatever it points to
    assert dir_size(root / "link-to-folder") == (len(str(big)), 1)


def test_dir_size_counts_hard_links_once(tmp_path):
    root = make(tmp_path / "data", {"a": b"x" * 1000})
    os.link(root / "a", root / "b")
    os.link(root / "a", root / "c")
    assert dir_size(root) == (1000, 3)


@not_root
def test_dir_size_skips_unreadable_folders(tmp_path):
    root = make(tmp_path / "data", {"a": b"x" * 10, "locked/secret": b"y" * 500, "open/b": b"z" * 5})
    (root / "locked").chmod(0)
    try:
        assert dir_size(root) == (15, 2)
    finally:
        (root / "locked").chmod(0o755)


def test_dir_size_ignores_special_files(tmp_path):
    root = make(tmp_path / "data", {"a": b"x" * 10})
    os.mkfifo(root / "pipe")
    assert dir_size(root) == (10, 2)


def test_dir_size_does_not_enter_other_file_systems(tmp_path, monkeypatch):
    root = make(tmp_path / "data", {"a": b"x" * 10, "mounted/big": b"y" * 5000})
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == str(root):          # pretend the root is on another device
            values = list(st)
            values[stat.ST_DEV] = st.st_dev + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "lstat", lstat)
    assert dir_size(root) == (10, 1)


def test_dir_size_of_a_huge_tree_is_bounded_by_entries(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for folder in range(10):
        make(root / f"d{folder}", {f"f{index}": b"x" for index in range(40)})
    assert dir_size(root) == (400, 400)
    monkeypatch.setattr(appdata, "MAX_SCAN_ENTRIES", 100)
    size, files = dir_size(root)
    assert 0 < files <= 100 and size == files
    assert appdata._scan_tree(root)[2] is False
    monkeypatch.setattr(appdata, "MAX_SCAN_ENTRIES", 10_000)
    assert appdata._scan_tree(root) == (400, 400, True)


def test_dir_size_of_a_huge_tree_is_bounded_by_time(tmp_path, monkeypatch):
    root = make(tmp_path / "data", {f"f{index}": b"x" for index in range(1200)})
    monkeypatch.setattr(appdata, "MAX_SCAN_SECONDS", -1.0)      # the time is already up
    started = time.monotonic()
    size, files, complete = appdata._scan_tree(root)
    assert complete is False and files < 1200 and size == files
    assert time.monotonic() - started < 5
    # find_app_data() shares one time budget between all folders and still answers
    assert dir_size(root)[1] < 1200


def test_dir_size_of_a_deep_tree(tmp_path):
    folder = tmp_path / "data"
    for _level in range(150):
        folder = folder / "d"
    make(folder, {"leaf": b"12345"})
    assert dir_size(tmp_path / "data") == (5, 1)


# --------------------------------------------------------------------------------------------
# move_to_trash
# --------------------------------------------------------------------------------------------

def trash_info(home: Path, name: str) -> dict[str, str]:
    text = (trash_dir(home) / "info" / f"{name}.trashinfo").read_text()
    lines = text.splitlines()
    assert lines[0] == "[Trash Info]"
    return dict(line.split("=", 1) for line in lines[1:])


def test_fallback_moves_a_folder_into_the_home_trash(home, no_gio):
    folder = make(home / ".config/My App", {"settings.ini": b"abc", "sub/x": b"1"})
    content = snapshot(folder)
    assert move_to_trash([folder]) == [(folder, None)]
    assert not folder.exists()
    trashed = trash_dir(home) / "files" / "My App"
    assert snapshot(trashed) == content           # moved, complete
    info = trash_info(home, "My App")
    assert info["Path"] == str(home / ".config") + "/My%20App"
    assert unquote(info["Path"]) == str(folder)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", info["DeletionDate"])
    deleted = time.mktime(time.strptime(info["DeletionDate"], "%Y-%m-%dT%H:%M:%S"))
    assert abs(deleted - time.time()) < 120       # local time, now
    for part in (trash_dir(home), trash_dir(home) / "files", trash_dir(home) / "info"):
        assert stat.S_IMODE(part.stat().st_mode) == 0o700


def test_fallback_trashinfo_encodes_odd_names(home, no_gio):
    name = "Ünï cødé %41 #? [x]\nline"
    folder = make(home / ".config" / name)
    assert move_to_trash([folder]) == [(folder, None)]
    info_files = list((trash_dir(home) / "info").iterdir())
    assert len(info_files) == 1
    text = info_files[0].read_bytes()
    assert text.isascii() and text.count(b"\n") == 3, "the info file is three plain lines"
    path_line = text.decode().splitlines()[1]
    assert path_line.startswith("Path=/") and unquote(path_line[5:]) == str(folder)
    assert "%25" in path_line and "%0A" in path_line and " " not in path_line


def test_fallback_keeps_both_when_names_collide(home, no_gio):
    first = make(home / ".config/myapp", {"a": b"config"})
    second = make(home / ".cache/myapp", {"a": b"cache"})
    third = make(home / ".local/share/myapp", {"a": b"data"})
    assert move_to_trash([first, second, third]) == [(first, None), (second, None), (third, None)]
    files = trash_dir(home) / "files"
    assert sorted(path.name for path in files.iterdir()) == ["myapp", "myapp.2", "myapp.3"]
    assert (files / "myapp/a").read_bytes() == b"config"
    assert (files / "myapp.2/a").read_bytes() == b"cache"
    assert (files / "myapp.3/a").read_bytes() == b"data"
    assert unquote(trash_info(home, "myapp.2")["Path"]) == str(second)
    assert unquote(trash_info(home, "myapp.3")["Path"]) == str(third)


def test_fallback_never_overwrites_something_in_the_trash(home, no_gio):
    """An entry without its .trashinfo (left by another program) is not replaced."""
    make(trash_dir(home) / "files/myapp", {"old": b"already in the trash"})
    folder = make(home / ".config/myapp", {"new": b"x"})
    assert move_to_trash([folder]) == [(folder, None)]
    files = trash_dir(home) / "files"
    assert (files / "myapp/old").read_bytes() == b"already in the trash"
    assert (files / "myapp.2/new").read_bytes() == b"x"
    assert [path.name for path in (trash_dir(home) / "info").iterdir()] == ["myapp.2.trashinfo"]


def test_fallback_with_a_very_long_name(home, no_gio):
    folder = make(home / ".config" / ("n" * 250))
    assert move_to_trash([folder]) == [(folder, None)]
    (entry,) = (trash_dir(home) / "files").iterdir()
    assert (trash_dir(home) / "info" / f"{entry.name}.trashinfo").is_file()
    assert unquote(trash_info(home, entry.name)["Path"]) == str(folder)


def test_a_single_file_can_be_trashed(home, no_gio):
    path = home / ".config" / "myapp.conf"
    path.write_text("x")
    assert move_to_trash([path]) == [(path, None)]
    assert (trash_dir(home) / "files/myapp.conf").read_text() == "x"


def test_results_keep_the_order_and_report_each_path(home, no_gio):
    good = make(home / ".config/myapp")
    missing = home / ".config/gone"
    also_good = make(home / ".cache/other")
    result = move_to_trash(iter([good, missing, str(also_good)]))
    assert [path for path, _error in result] == [good, missing, also_good]
    assert [error is None for _path, error in result] == [True, False, True]
    assert "no longer exists" in result[1][1]
    assert move_to_trash([]) == []
    odd = move_to_trash([None, 5, make(home / ".config/third")])     # type: ignore[list-item]
    assert [error is None for _path, error in odd] == [False, False, True]
    assert all(isinstance(path, Path) for path, _error in odd)


@requires_gio
def test_gio_trash_is_used_when_available(home):
    folder = make(home / ".config/My App", {"settings.ini": b"abc"})
    assert move_to_trash([folder]) == [(folder, None)]
    assert not folder.exists()
    assert (trash_dir(home) / "files/My App/settings.ini").read_bytes() == b"abc"
    assert unquote(trash_info(home, "My App")["Path"]) == str(folder)


def test_gio_is_called_with_the_absolute_path(home, tmp_path, monkeypatch):
    log = fake_gio(tmp_path, monkeypatch, 'shift 2; mv "$1" "$1.gone"')
    folder = make(home / ".config/-rf")               # a name that looks like an option
    assert move_to_trash([folder]) == [(folder, None)]
    assert log.read_text().splitlines() == ["trash", "--", str(folder)]


def test_failing_gio_falls_back_to_the_own_implementation(home, tmp_path, monkeypatch):
    fake_gio(tmp_path, monkeypatch, "echo 'gio: Trashing on system internal mounts is not supported' >&2; exit 1")
    folder = make(home / ".config/myapp", {"a": b"1"})
    assert move_to_trash([folder]) == [(folder, None)]
    assert (trash_dir(home) / "files/myapp/a").read_bytes() == b"1"


def test_gio_that_does_nothing_falls_back(home, tmp_path, monkeypatch):
    fake_gio(tmp_path, monkeypatch, "exit 0")
    folder = make(home / ".config/myapp", {"a": b"1"})
    assert move_to_trash([folder]) == [(folder, None)]
    assert not folder.exists() and (trash_dir(home) / "files/myapp/a").is_file()


def test_gio_that_hangs_is_stopped(home, tmp_path, monkeypatch):
    fake_gio(tmp_path, monkeypatch, "exec sleep 30")
    monkeypatch.setattr(appdata, "TRASH_TIMEOUT", 0.3)
    folder = make(home / ".config/myapp", {"a": b"1"})
    started = time.monotonic()
    assert move_to_trash([folder]) == [(folder, None)]
    assert time.monotonic() - started < 10
    assert (trash_dir(home) / "files/myapp/a").is_file()


def test_when_trashing_is_impossible_nothing_is_lost(home, no_gio):
    folder = make(home / ".config/myapp", {"a": b"precious", "sub/b": b"data"})
    before = snapshot(folder)
    trash_dir(home).parent.mkdir(parents=True, exist_ok=True)
    trash_dir(home).write_text("not a folder")        # the trash cannot be created
    ((path, error),) = move_to_trash([folder])
    assert path == folder and error and "left alone" in error
    assert snapshot(folder) == before


@not_root
def test_read_only_trash_reports_failure_and_leaves_the_folder(home, no_gio):
    folder = make(home / ".config/myapp", {"a": b"precious"})
    files = trash_dir(home) / "files"
    info = trash_dir(home) / "info"
    files.mkdir(parents=True)
    info.mkdir()
    files.chmod(0o500)
    try:
        ((_path, error),) = move_to_trash([folder])
    finally:
        files.chmod(0o700)
    assert error and (folder / "a").read_bytes() == b"precious"
    assert list(info.iterdir()) == [], "no info file may stay behind without its folder"


def test_trash_on_another_file_system_is_not_used(home, no_gio, monkeypatch):
    folder = make(home / ".config/myapp", {"a": b"precious"})
    files = trash_dir(home) / "files"
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(files):
            values = list(st)
            values[stat.ST_DEV] = st.st_dev + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    ((_path, error),) = move_to_trash([folder])
    assert error and (folder / "a").read_bytes() == b"precious"
    assert list((trash_dir(home) / "info").iterdir()) == []


def test_nothing_is_ever_deleted(home, no_gio, monkeypatch):
    """Success and failure paths: no remove call ever touches the user's data."""
    removed: list[str] = []
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        removed.append(os.fspath(path))
        return real_unlink(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError(f"must not be called: {args}")

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "remove", forbidden)
    monkeypatch.setattr(os, "rmdir", forbidden)
    monkeypatch.setattr(shutil, "rmtree", forbidden)
    ok = make(home / ".config/myapp", {"a": b"1"})
    make(trash_dir(home) / "files/other")             # forces one retry ("other.2")
    retry = make(home / ".cache/other", {"b": b"2"})
    assert move_to_trash([ok, retry]) == [(ok, None), (retry, None)]
    assert all(name.endswith(".trashinfo") and "/Trash/info/" in name for name in removed), removed
    assert (trash_dir(home) / "files/myapp/a").read_bytes() == b"1"
    assert (trash_dir(home) / "files/other.2/b").read_bytes() == b"2"


PROTECTED = [".", ".config", ".local", ".local/share", ".cache", ".local/state", "Applications",
             ".local/share/easy-installer", ".config/easy-installer", ".cache/easy-installer",
             ".local/share/applications", ".local/share/icons", ".local/share/icons/hicolor",
             ".local/share/mime", ".local/share/Trash", ".local/share/Trash/files",
             ".local/share/Trash/files/old", "Applications/Foo",
             ".local/share/easy-installer/registry.json"]


@pytest.mark.parametrize("relative", PROTECTED)
@pytest.mark.parametrize("with_gio", [False, True], ids=["fallback", "gio"])
def test_protected_folders_are_refused(lived_in_home, tmp_path, monkeypatch, relative, with_gio):
    make(lived_in_home / ".local/share/Trash/files/old")
    if with_gio:
        log = fake_gio(tmp_path, monkeypatch, 'shift 2; mv "$1" "$1.gone"')
    else:
        empty = tmp_path / "no-tools"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        log = tmp_path / "gio.log"
    before = snapshot(lived_in_home)
    target = lived_in_home / relative
    ((path, error),) = move_to_trash([target])
    assert path == target and error and "protected" in error
    assert snapshot(lived_in_home) == before
    assert not log.exists(), "gio must not even be asked"


@pytest.mark.parametrize("path", ["/", "/etc", "/usr/share", "/tmp", "relative/name", "",
                                  "/nonexistent/thing", "a\0b"])
def test_paths_outside_the_home_are_refused(home, tmp_path, monkeypatch, path):
    log = fake_gio(tmp_path, monkeypatch, "exit 0")
    ((_path, error),) = move_to_trash([Path(path)])
    assert error
    assert not log.exists()


def test_folder_outside_the_home_is_refused(home, tmp_path, no_gio):
    outside = make(tmp_path / "outside/myapp", {"a": b"x"})
    ((_path, error),) = move_to_trash([outside])
    assert error and "protected" in error and (outside / "a").exists()
    # ... also when it is reached through a link inside the home
    (home / ".config/link").symlink_to(outside.parent)
    ((_path, error),) = move_to_trash([home / ".config/link/myapp"])
    assert error and (outside / "a").exists()


def test_symlinks_are_refused(home, tmp_path, no_gio):
    target = make(home / "Documents/real", {"a": b"x"})
    link = home / ".config/myapp"
    link.symlink_to(target)
    ((_path, error),) = move_to_trash([link])
    assert error and "protected" in error
    assert link.is_symlink() and (target / "a").exists()


def test_home_itself_and_its_parents_are_refused(home, no_gio):
    for path in (home, home.parent, Path(str(home) + "/"), home / ".config/..", home / "."):
        ((_path, error),) = move_to_trash([path])
        assert error, path
    assert home.is_dir() and (home / ".config").is_dir()


def test_folder_containing_the_apps_folder_is_refused(home, no_gio, monkeypatch):
    folder = make(home / ".myapp/installed")
    monkeypatch.setenv("EASY_INSTALLER_APPS_DIR", str(folder))
    ((_path, error),) = move_to_trash([home / ".myapp"])
    assert error and "protected" in error and folder.is_dir()


def test_somebody_elses_folder_is_refused(home, no_gio, monkeypatch):
    folder = make(home / ".config/myapp")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    ((_path, error),) = move_to_trash([folder])
    assert error and folder.is_dir()


def test_trash_works_when_home_is_a_symlink(home, tmp_path, no_gio, monkeypatch):
    folder = make(home / ".config/myapp", {"a": b"1"})
    link = tmp_path / "home-link"
    link.symlink_to(home)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, os.environ[name].replace(str(home), str(link)))
    through_link = link / ".config/myapp"
    assert move_to_trash([through_link]) == [(through_link, None)]
    assert not folder.exists()
    assert (trash_dir(home) / "files/myapp/a").read_bytes() == b"1"
    assert unquote(trash_info(home, "myapp")["Path"]) == str(folder)      # the real path


def test_one_odd_path_does_not_stop_the_others(home, no_gio, monkeypatch):
    first = make(home / ".config/first")
    second = make(home / ".config/second")
    real = appdata._spec_trash

    def flaky(path):
        if path.name == "first":
            raise RuntimeError("boom")
        return real(path)

    monkeypatch.setattr(appdata, "_spec_trash", flaky)
    result = move_to_trash([first, second])
    assert result[0][1] and first.is_dir()
    assert result[1] == (second, None) and not second.exists()


# --------------------------------------------------------------------------------------------
# the whole way: find, then trash what was ticked
# --------------------------------------------------------------------------------------------

def test_find_then_trash(lived_in_home, no_gio):
    home = lived_in_home
    make(home / ".config/FreeCAD", {"user.cfg": b"a"})
    make(home / ".local/share/FreeCAD", {"Mod/x": b"b"})
    make(home / ".cache/FreeCAD", {"c": b"c"})
    make(home / ".config/FreeCAD-backup", {"keep": b"mine"})
    others = snapshot(home)
    locations = find_app_data(data_hints_for(
        name="FreeCAD", app_id="org.freecad.FreeCAD", desktop_stem="org.freecad.FreeCAD",
        wm_class="FreeCAD", exec_name="AppRun"))
    assert len(locations) == 3
    ticked = [location.path for location in locations if location.kind != "data"]
    assert all(error is None for _path, error in move_to_trash(ticked))
    assert not (home / ".config/FreeCAD").exists() and not (home / ".cache/FreeCAD").exists()
    assert (home / ".local/share/FreeCAD/Mod/x").read_bytes() == b"b"     # not ticked
    after = snapshot(home)
    lost = {name for name in others if name not in after
            and not name.startswith((".config/FreeCAD/", ".cache/FreeCAD/"))
            and name not in (".config/FreeCAD", ".cache/FreeCAD")}
    assert lost == set(), "only the ticked folders may have moved"
    trashed = trash_dir(home) / "files"
    assert (trashed / "FreeCAD/user.cfg").read_bytes() == b"a"
    assert (trashed / "FreeCAD.2/c").read_bytes() == b"c"


def test_module_has_no_way_to_delete_or_to_run_a_shell():
    source = Path(appdata.__file__).read_text()
    assert "shell=True" not in source and "os.system" not in source
    assert "rmtree" not in source and "os.remove" not in source and "os.rmdir" not in source
    # the only unlink is the one for our own ".trashinfo" reservation
    assert source.count("os.unlink(") == 2 and source.count("os.unlink(info_path)") == 2
