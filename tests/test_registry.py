from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest

from easy_installer.core.paths import Scope, user_layout
from easy_installer.core.registry import InstalledApp, Registry, utc_now


def make_app(tmp_path, app_id="org.example.App", name="Example", **kw) -> InstalledApp:
    defaults = dict(
        id=app_id,
        name=name,
        version="1.0",
        scope=Scope.USER,
        appimage_path=str(tmp_path / f"{app_id}.AppImage"),
        desktop_path=str(tmp_path / f"easyinstaller-{app_id}.desktop"),
        icon_paths=[str(tmp_path / f"easyinstaller-{app_id}.png")],
        icon_name=f"easyinstaller-{app_id}",
        apparmor_profile=None,
        sandbox_fix="none",
        extract_and_run=False,
        sha256="ab" * 32,
        size=1234,
        arch="x86_64",
        update_info=None,
        original_filename=f"{name}-1.0-x86_64.AppImage",
        comment="An example",
        installed_at="2026-09-29T15:04:05Z",
        updated_at="2026-09-29T15:04:05Z",
        installer_version="0.1.0",
    )
    defaults.update(kw)
    return InstalledApp(**defaults)


def test_utc_now_format():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_now())


def test_roundtrip_dict(tmp_path):
    app = make_app(tmp_path)
    data = app.to_dict()
    assert data["scope"] == "user"
    json.dumps(data)  # serialisable
    assert InstalledApp.from_dict(data) == app


def test_from_dict_tolerant():
    app = InstalledApp.from_dict({
        "id": "x", "name": "X", "scope": "system", "appimage_path": "/opt/appimages/X.AppImage",
        "desktop_path": "/usr/local/share/applications/easyinstaller-x.desktop",
        "some_future_key": {"nested": True},
    })
    assert app.scope is Scope.SYSTEM
    assert app.version is None
    assert app.icon_paths == []
    assert app.sandbox_fix == "none"
    assert app.size == 0
    assert app.extract_and_run is False


def test_from_dict_rejects_garbage():
    with pytest.raises(ValueError):
        InstalledApp.from_dict({"name": "no id"})
    with pytest.raises(ValueError):
        InstalledApp.from_dict({"id": "x", "scope": "galaxy"})
    with pytest.raises(ValueError):
        InstalledApp.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_status_and_icon_path(tmp_path):
    app = make_app(tmp_path)
    assert app.status() == "missing-appimage"
    assert app.icon_path is None
    open(app.appimage_path, "wb").close()
    assert app.status() == "missing-launcher"
    open(app.desktop_path, "w").close()
    assert app.status() == "ok"
    open(app.icon_paths[0], "wb").close()
    assert app.icon_path == app.icon_paths[0]


def test_missing_registry_is_empty(tmp_path):
    reg = Registry(tmp_path / "nope" / "registry.json")
    assert reg.load() == {}
    assert reg.all() == []
    assert reg.get("x") is None
    assert not (tmp_path / "nope").exists()  # reading never creates anything


def test_put_get_remove(tmp_path):
    path = tmp_path / "data" / "registry.json"
    reg = Registry(path)
    a = make_app(tmp_path, "b.app", "beta")
    b = make_app(tmp_path, "a.app", "Alpha")
    reg.put(a)
    reg.put(b)
    assert reg.get("b.app") == a
    assert [x.id for x in reg.all()] == ["a.app", "b.app"]  # sorted by name, case-insensitive
    raw = json.loads(path.read_text())
    assert raw["format"] == 1
    assert set(raw["apps"]) == {"a.app", "b.app"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    reg.remove("b.app")
    assert reg.get("b.app") is None
    reg.remove("does-not-exist")  # no error
    assert [x.id for x in Registry(path).all()] == ["a.app"]
    # no temp files left behind
    leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_put_replaces_existing(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    reg.put(make_app(tmp_path, version="1.0"))
    reg.put(make_app(tmp_path, version="2.0"))
    assert len(reg.load()) == 1
    assert reg.get("org.example.App").version == "2.0"


def test_owner_of(tmp_path):
    reg = Registry(tmp_path / "registry.json")
    app = make_app(tmp_path)
    reg.put(app)
    assert reg.owner_of(tmp_path / "org.example.App.AppImage") == "org.example.App"
    # non-normalised spelling of the same path
    assert reg.owner_of(tmp_path / "sub" / ".." / "org.example.App.AppImage") == "org.example.App"
    assert reg.owner_of(tmp_path / "other.AppImage") is None


def test_owner_of_through_symlinked_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    reg = Registry(tmp_path / "registry.json")
    reg.put(make_app(real, appimage_path=str(real / "X.AppImage")))
    (real / "X.AppImage").write_bytes(b"x")
    assert reg.owner_of(link / "X.AppImage") == "org.example.App"


def test_corrupt_registry_is_quarantined(tmp_path, caplog):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not json")
    reg = Registry(path)
    assert reg.load() == {}
    assert not path.exists()
    assert (tmp_path / "registry.json.corrupt-1").read_text() == "{ this is not json"
    assert "corrupt" in caplog.text
    # a second corruption gets the next number
    path.write_text("[1, 2, 3]")
    assert reg.load() == {}
    assert (tmp_path / "registry.json.corrupt-2").exists()
    # and writing afterwards works
    reg.put(make_app(tmp_path))
    assert reg.get("org.example.App") is not None


def test_invalid_entries_are_skipped(tmp_path):
    path = tmp_path / "registry.json"
    good = make_app(tmp_path).to_dict()
    path.write_text(json.dumps({"format": 1, "apps": {
        "org.example.App": good,
        "broken": {"id": "broken", "scope": "nowhere"},
        "keyonly": {"name": "Uses key as id", "scope": "user"},
    }}))
    apps = Registry(path).load()
    assert set(apps) == {"org.example.App", "keyonly"}


def test_concurrent_puts_do_not_lose_updates(tmp_path):
    path = tmp_path / "registry.json"
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            Registry(path).put(make_app(tmp_path, f"app{n}", f"App {n}"))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(Registry(path).load()) == 12


def test_user_layout_registry_path(isolated_env):
    reg = Registry(user_layout().registry_path)
    reg.put(make_app(isolated_env))
    assert (isolated_env / ".local/share/easy-installer/registry.json").is_file()
    assert os.path.exists(str(reg.path) + ".lock")


def test_unreadable_registry_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    reg = Registry(path)
    reg.put(make_app(tmp_path, "keep.me", "Keep"))
    original = path.read_text()

    real_read_bytes = type(path).read_bytes

    def denied(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(type(path), "read_bytes", denied)
    assert reg.load() == {}  # readers degrade gracefully
    with pytest.raises(PermissionError):
        reg.put(make_app(tmp_path, "new.app", "New"))
    monkeypatch.undo()
    assert path.read_text() == original


def _hold_lock(path) -> int:
    """Another process's view: any readable lock file can be flock()ed via O_RDONLY."""
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_lock_file_is_private_and_waiting_is_bounded(tmp_path):
    from easy_installer.errors import InstallError

    path = tmp_path / "registry.json"
    lock = tmp_path / "registry.json.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)  # made by an older version
    reg = Registry(path)
    reg.put(make_app(tmp_path))
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    holder = _hold_lock(lock)
    try:
        waited = []
        with pytest.raises(InstallError, match="Another installation is running"):
            with reg.locked(timeout=0.3, on_wait=lambda: waited.append(True)):
                pass  # pragma: no cover
        assert waited == [True]
    finally:
        os.close(holder)
    with reg.locked(timeout=0.3):
        reg.put(make_app(tmp_path, "second", "Second"))  # re-entrant: no deadlock
    assert set(reg.load()) == {"org.example.App", "second"}


def test_paths_that_are_not_utf8_are_stored_and_read_back(tmp_path):
    path = tmp_path / "registry.json"
    odd = os.fsdecode(b"/home/u/Caf\xe9.AppImage")
    Registry(path).put(make_app(tmp_path, appimage_path=odd))
    assert Registry(path).get("org.example.App").appimage_path == odd
    json.loads(path.read_text(encoding="utf-8"))  # still valid UTF-8 JSON


# ------------------------------------------------------------------------------------------------
# 0.2: new optional fields, full compatibility with registries written by 0.1
# ------------------------------------------------------------------------------------------------

#: A registry.json exactly as Easy Installer 0.1 wrote it on a real computer (home folder
#: renamed): three per-user apps, one with its own file types, none of the 0.2 keys.
V01_REGISTRY = """{
  "format": 1,
  "apps": {
    "org.freecad.FreeCAD": {
      "id": "org.freecad.FreeCAD",
      "name": "FreeCAD",
      "version": "1.1.3",
      "scope": "user",
      "appimage_path": "/home/user/Applications/FreeCAD.AppImage",
      "desktop_path": "/home/user/.local/share/applications/easyinstaller-org.freecad.FreeCAD.desktop",
      "icon_paths": [
        "/home/user/.local/share/icons/hicolor/scalable/apps/easyinstaller-org.freecad.FreeCAD.svg"
      ],
      "icon_name": "easyinstaller-org.freecad.FreeCAD",
      "apparmor_profile": null,
      "sandbox_fix": "none",
      "extract_and_run": false,
      "extract_and_run_explicit": false,
      "sha256": "3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef",
      "size": 820795896,
      "arch": "x86_64",
      "update_info": "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync",
      "original_filename": "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage",
      "comment": "Feature based Parametric Modeler",
      "installed_at": "2026-09-30T07:33:37Z",
      "updated_at": "2026-09-30T07:33:37Z",
      "installer_version": "0.1.0",
      "mime_package": "/home/user/.local/share/mime/packages/easyinstaller-org.freecad.FreeCAD.xml"
    },
    "pen": {
      "id": "pen",
      "name": "Pen",
      "version": "1.2.8",
      "scope": "user",
      "appimage_path": "/home/user/Applications/Pen.AppImage",
      "desktop_path": "/home/user/.local/share/applications/easyinstaller-pen.desktop",
      "icon_paths": [
        "/home/user/.local/share/icons/hicolor/512x512/apps/easyinstaller-pen.png"
      ],
      "icon_name": "easyinstaller-pen",
      "apparmor_profile": null,
      "sandbox_fix": "none",
      "extract_and_run": false,
      "extract_and_run_explicit": false,
      "sha256": "f37d9363b8b9e3e109409c4d95713f859c3bfbd62f5d32e3ffa2667fbd1d7036",
      "size": 406525521,
      "arch": "x86_64",
      "update_info": null,
      "original_filename": "Pen-linux-x86_64.AppImage",
      "comment": "Desktop app for pen.dev",
      "installed_at": "2026-09-30T07:34:45Z",
      "updated_at": "2026-09-30T07:34:45Z",
      "installer_version": "0.1.0",
      "mime_package": null
    },
    "t3code": {
      "id": "t3code",
      "name": "T3 Code (Alpha)",
      "version": "0.0.42",
      "scope": "user",
      "appimage_path": "/home/user/Applications/T3-Code-Alpha.AppImage",
      "desktop_path": "/home/user/.local/share/applications/easyinstaller-t3code.desktop",
      "icon_paths": [
        "/home/user/.local/share/icons/hicolor/512x512/apps/easyinstaller-t3code.png"
      ],
      "icon_name": "easyinstaller-t3code",
      "apparmor_profile": null,
      "sandbox_fix": "none",
      "extract_and_run": false,
      "extract_and_run_explicit": false,
      "sha256": "8dc1fccdabc2ed3a59a3944cc772ef11931b9351401c0963ed305d5f96e3cdf4",
      "size": 150326305,
      "arch": "x86_64",
      "update_info": null,
      "original_filename": "T3-Code-0.0.42-x86_64.AppImage",
      "comment": "T3 Code desktop build",
      "installed_at": "2026-09-30T08:35:53Z",
      "updated_at": "2026-09-30T08:35:53Z",
      "installer_version": "0.1.0",
      "mime_package": null
    }
  }
}
"""

NEW_DEFAULTS = {
    "kind": "appimage", "install_dir": None, "mtime_ns": 0, "base_id": None, "pinned": False,
    "previous": None, "update_source": None, "origin_url": None, "signature": None,
    "data_hints": [],
}


def test_registry_written_by_0_1_loads_and_round_trips(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(V01_REGISTRY, encoding="utf-8")
    original = json.loads(V01_REGISTRY)
    registry = Registry(path)
    apps = registry.load()
    assert list(apps) == ["org.freecad.FreeCAD", "pen", "t3code"]
    assert path.read_text(encoding="utf-8") == V01_REGISTRY   # reading changes nothing
    assert not list(tmp_path.glob("registry.json.corrupt-*"))

    freecad = apps["org.freecad.FreeCAD"]
    assert (freecad.name, freecad.version, freecad.scope) == ("FreeCAD", "1.1.3", Scope.USER)
    assert freecad.size == 820795896 and freecad.update_info.startswith("gh-releases-zsync|")
    assert freecad.mime_package.endswith("easyinstaller-org.freecad.FreeCAD.xml")
    assert [a.name for a in registry.all()] == ["FreeCAD", "Pen", "T3 Code (Alpha)"]
    for app_id, app in apps.items():
        # every 0.2 field has its default ...
        assert {key: getattr(app, key) for key in NEW_DEFAULTS} == NEW_DEFAULTS
        assert app.main_id == app_id and app.exec_path == app.appimage_path
        # ... and everything 0.1 stored comes back unchanged
        data = app.to_dict()
        assert {key: data[key] for key in original["apps"][app_id]} == original["apps"][app_id]
        assert set(data) == set(original["apps"][app_id]) | set(NEW_DEFAULTS)
        assert InstalledApp.from_dict(data) == app

    # Writing (as 0.2 does on the first reconcile) keeps the format and every old value.
    t3 = apps["t3code"]
    registry.put(dataclasses.replace(t3, mtime_ns=1_790_000_000_123_456_789))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["format"] == 1 and list(written["apps"]) == list(original["apps"])
    for app_id, old in original["apps"].items():
        assert {key: written["apps"][app_id][key] for key in old} == old
    assert written["apps"]["t3code"]["mtime_ns"] == 1_790_000_000_123_456_789
    assert Registry(path).get("pen") == apps["pen"]

    # 0.1 (still installed next to 0.2) reads the new file: it ignores the keys it does not
    # know - simulated by dropping them - and the entries are the same apps.
    for app_id, entry in written["apps"].items():
        as_0_1 = {key: value for key, value in entry.items() if key in original["apps"][app_id]}
        assert as_0_1 == original["apps"][app_id]


def test_new_fields_round_trip(tmp_path):
    previous = {"version": "1.0", "path": str(tmp_path / ".easyinstaller-backups/x/X-1.0.AppImage"),
                "sha256": "cd" * 32, "size": 99, "saved_at": "2026-09-30T10:00:00Z"}
    source = {"kind": "github-assets", "owner": "FreeCAD", "repo": "FreeCAD", "release": "latest",
              "pattern": "FreeCAD*x86_64*.AppImage", "url": None, "prerelease": False,
              "via": "upd_info"}
    signature = {"status": "unverified", "fingerprint": "ABCD" * 10, "signer": None, "details": None}
    app = make_app(tmp_path, "x--1.1", "X 1.1", kind="portable", install_dir=str(tmp_path / "X"),
                   mtime_ns=1_790_000_000_000_000_001, base_id="x", pinned=True, previous=previous,
                   update_source=source, origin_url="https://example.org/X.AppImage",
                   signature=signature, data_hints=["x", "X App"])
    data = app.to_dict()
    json.dumps(data)
    assert InstalledApp.from_dict(data) == app
    assert app.main_id == "x" and app.exec_path == app.appimage_path
    registry = Registry(tmp_path / "registry.json")
    registry.put(app)
    loaded = registry.get("x--1.1")
    assert loaded == app and loaded.mtime_ns == 1_790_000_000_000_000_001   # no float rounding
    # to_dict()/from_dict() copy: changing one never changes the other
    data["previous"]["path"] = "/elsewhere"
    data["update_source"]["owner"] = "evil"
    data["data_hints"].append("more")
    assert app.previous == previous and app.update_source == source
    assert app.data_hints == ["x", "X App"]
    again = InstalledApp.from_dict(app.to_dict())
    again.update_source["owner"] = "changed"
    assert app.update_source["owner"] == "FreeCAD"


@pytest.mark.parametrize("values, expected", [
    ({"kind": None}, {"kind": "appimage"}),
    ({"kind": ""}, {"kind": "appimage"}),
    ({"kind": 7}, {"kind": "appimage"}),
    ({"kind": "portable"}, {"kind": "portable"}),
    ({"mtime_ns": "12"}, {"mtime_ns": 12}),
    ({"mtime_ns": -5}, {"mtime_ns": 0}),
    ({"mtime_ns": "soon"}, {"mtime_ns": 0}),
    ({"mtime_ns": None}, {"mtime_ns": 0}),
    ({"base_id": ""}, {"base_id": None}),
    ({"pinned": "yes"}, {"pinned": False}),
    ({"pinned": 1}, {"pinned": False}),
    ({"pinned": True}, {"pinned": True}),
    ({"previous": "a path"}, {"previous": None}),
    ({"previous": {}}, {"previous": None}),
    ({"previous": {"version": "1.0"}}, {"previous": None}),            # no path: no backup
    ({"previous": {"path": 5}}, {"previous": None}),
    ({"previous": {"path": "/b/X.AppImage", "size": "x", "extra": 1}},
     {"previous": {"version": None, "path": "/b/X.AppImage", "sha256": None, "size": 0,
                   "saved_at": ""}}),
    ({"update_source": ["github"]}, {"update_source": None}),
    ({"update_source": {}}, {"update_source": None}),
    ({"signature": "valid"}, {"signature": None}),
    ({"origin_url": ""}, {"origin_url": None}),
    ({"data_hints": "x"}, {"data_hints": []}),
    ({"data_hints": ["a", 5, "", None, "b"]}, {"data_hints": ["a", "b"]}),
    ({"install_dir": ""}, {"install_dir": None}),
])
def test_new_fields_are_read_tolerantly(values, expected):
    app = InstalledApp.from_dict({"id": "x", "name": "X", "scope": "user",
                                  "appimage_path": "/a/X.AppImage", "desktop_path": "/d", **values})
    assert {key: getattr(app, key) for key in expected} == expected


def test_numbers_json_cannot_really_hold_do_not_break_loading(tmp_path):
    path = tmp_path / "registry.json"
    entry = make_app(tmp_path).to_dict()
    text = json.dumps({"format": 1, "apps": {"org.example.App": entry}})
    path.write_text(text.replace('"size": 1234', '"size": Infinity')
                    .replace('"mtime_ns": 0', '"mtime_ns": NaN'))
    app = Registry(path).get("org.example.App")
    assert app is not None and app.size == 0 and app.mtime_ns == 0


def test_status_never_reports_changed(tmp_path):
    """"changed" is what core.reconcile finds out; status() stays the cheap 0.1 check."""
    from easy_installer.core import registry as registry_module

    app = make_app(tmp_path, size=1, mtime_ns=1)
    Path(app.appimage_path).write_bytes(b"another size and time")
    Path(app.desktop_path).write_text("")
    assert app.status() == "ok"
    assert registry_module.STATUS_CHANGED == "changed"


def test_make_previous_record(tmp_path):
    from easy_installer.core.registry import make_previous

    record = make_previous(version="", path=tmp_path / "b" / "X.AppImage", sha256=None, size=7,
                           saved_at="2026-09-30T10:00:00Z")
    assert record == {"version": None, "path": str(tmp_path / "b" / "X.AppImage"), "sha256": None,
                      "size": 7, "saved_at": "2026-09-30T10:00:00Z"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
                        make_previous(version="1", path="/x", sha256="ab", size=1)["saved_at"])
    assert InstalledApp.from_dict({**make_app(tmp_path).to_dict(), "previous": record}).previous \
        == record


def test_keys_of_a_newer_version_survive_a_rewrite(tmp_path):
    """PKG-1: an older writer (e.g. an older helper next to a newer program) must not destroy
    what a newer version recorded: keys it does not know are written back as they are."""
    path = tmp_path / "registry.json"
    registry = Registry(path)
    registry.put(make_app(tmp_path, "demo", "Demo"))
    registry.put(make_app(tmp_path, "other", "Other"))
    data = json.loads(path.read_text())
    entries = data["apps"]
    demo = entries["demo"] if isinstance(entries, dict) else next(
        e for e in entries if e["id"] == "demo")
    demo["future_field"] = {"from": "0.3", "values": [1, 2]}
    demo["previous"] = None
    path.write_text(json.dumps(data))
    loaded = Registry(path).get("demo")
    assert loaded.extra == {"future_field": {"from": "0.3", "values": [1, 2]}}
    Registry(path).remove("other")                       # rewrites the whole file
    again = json.loads(path.read_text())["apps"]
    demo = again["demo"] if isinstance(again, dict) else again[0]
    assert demo["future_field"] == {"from": "0.3", "values": [1, 2]}
    # (what this version knows is never taken from there)
    assert dataclasses.replace(loaded, extra={"name": "x"}).to_dict()["name"] == "Demo"
