"""core/settings.py: the preferences file shared by the command line and the window."""

from __future__ import annotations

import json
import os
import stat

import pytest

from easy_installer.core import settings as core_settings
from easy_installer.core.paths import settings_path
from easy_installer.core.settings import Settings, load_settings, save_settings


def read_file() -> dict:
    return json.loads(settings_path().read_text(encoding="utf-8"))


def test_defaults_without_a_file(isolated_env):
    settings = load_settings()
    assert settings == Settings()
    assert (settings.check_updates, settings.update_interval_hours, settings.backup_days) == \
        (True, 24, 14)
    assert settings.extra == {} and settings.keep_backups is True
    assert not settings_path().exists()   # reading never creates anything


def test_round_trip_and_file_format(isolated_env):
    save_settings(Settings(check_updates=False, update_interval_hours=6, backup_days=30))
    assert read_file() == {"check_updates": False, "update_interval_hours": 6, "backup_days": 30}
    assert load_settings() == Settings(check_updates=False, update_interval_hours=6, backup_days=30)
    path = settings_path()
    assert path == isolated_env / ".config" / "easy-installer" / "settings.json"
    assert path.read_text().endswith("\n")
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]   # no temp files left


def test_explicit_path(tmp_path):
    path = tmp_path / "deep" / "prefs.json"
    save_settings(Settings(backup_days=0), path)
    assert load_settings(path).backup_days == 0 and load_settings(path).keep_backups is False
    assert not settings_path().exists()


def test_unknown_keys_are_preserved_verbatim(isolated_env):
    path = settings_path()
    path.parent.mkdir(parents=True)
    original = {"default_handler_offer_dismissed": True, "window": {"width": 760, "tabs": [1, 2]},
                "backup_days": 7}
    path.write_text(json.dumps(original))
    settings = load_settings()
    assert settings.backup_days == 7
    assert settings.extra == {"default_handler_offer_dismissed": True,
                              "window": {"width": 760, "tabs": [1, 2]}}
    settings.check_updates = False
    save_settings(settings)
    assert read_file() == {**original, "check_updates": False, "update_interval_hours": 24}


@pytest.mark.parametrize("raw, expected", [
    ({"check_updates": "yes"}, Settings()),
    ({"check_updates": 0}, Settings()),
    ({"check_updates": None, "backup_days": None}, Settings()),
    ({"update_interval_hours": "12"}, Settings()),
    ({"update_interval_hours": 0}, Settings(update_interval_hours=1)),
    ({"update_interval_hours": -5}, Settings(update_interval_hours=1)),
    ({"update_interval_hours": 10 ** 9}, Settings(update_interval_hours=720)),
    ({"update_interval_hours": 12.0}, Settings(update_interval_hours=12)),
    ({"update_interval_hours": 12.5}, Settings()),
    ({"update_interval_hours": True}, Settings()),
    ({"backup_days": -1}, Settings(backup_days=0)),
    ({"backup_days": 365}, Settings(backup_days=90)),
    ({"backup_days": [14]}, Settings()),
    ({"backup_days": 0}, Settings(backup_days=0)),
])
def test_wrong_types_and_ranges(isolated_env, raw, expected):
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw))
    assert load_settings() == expected


@pytest.mark.parametrize("content", ["{ not json", "[1, 2]", '"text"', "", "\x00\xff", "null",
                                     '{"backup_days": Infinity}', '{"backup_days": NaN}'])
def test_damaged_file_means_defaults(isolated_env, content):
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("latin-1"))
    assert load_settings() == Settings()
    save_settings(Settings(backup_days=3))   # and it can be written again
    assert load_settings().backup_days == 3


def test_values_are_clamped_when_saving(isolated_env):
    save_settings(Settings(update_interval_hours=0, backup_days=1000))
    assert read_file()["update_interval_hours"] == 1 and read_file()["backup_days"] == 90
    save_settings(Settings(update_interval_hours="soon", backup_days=None))  # type: ignore[arg-type]
    assert read_file()["update_interval_hours"] == 24 and read_file()["backup_days"] == 14


def test_saving_does_not_put_back_what_another_window_changed(isolated_env):
    """Preferences are open (loaded) while the main window stores that a banner was closed."""
    from easy_installer.gui import settings as gui_settings

    gui_settings.set_setting("default_handler_offer_dismissed", False)
    gui_settings.set_setting("other", "a")
    prefs = load_settings()
    gui_settings.set_setting("default_handler_offer_dismissed", True)   # meanwhile
    prefs.backup_days = 7
    prefs.extra["other"] = "b"                                          # changed on purpose
    save_settings(prefs)
    assert read_file() == {"default_handler_offer_dismissed": True, "other": "b", "backup_days": 7,
                           "check_updates": True, "update_interval_hours": 24}
    # a key removed from extra is removed from the file; a second save changes nothing more
    del prefs.extra["other"]
    save_settings(prefs)
    save_settings(prefs)
    assert "other" not in read_file() and read_file()["default_handler_offer_dismissed"] is True


def test_known_keys_in_extra_never_win(isolated_env):
    settings = Settings(backup_days=5, extra={"backup_days": 99, "note": "x"})
    save_settings(settings)
    assert read_file()["backup_days"] == 5 and read_file()["note"] == "x"
    assert settings.to_dict()["backup_days"] == 5


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_save_never_raises(isolated_env, caplog):
    config = isolated_env / ".config"
    config.mkdir()
    config.chmod(0o555)
    try:
        save_settings(Settings(backup_days=1))            # cannot create the folder
    finally:
        config.chmod(0o755)
    assert "could not save settings" in caplog.text
    assert not settings_path().exists()

    save_settings(Settings(extra={"bad": object()}))      # not JSON
    assert not settings_path().exists()
    save_settings(Settings(extra={1: "a", "b": 2}))       # keys that cannot be sorted
    assert load_settings() == Settings() or load_settings().extra   # no crash either way


def test_a_failed_save_keeps_the_old_file(isolated_env, monkeypatch):
    save_settings(Settings(backup_days=3))
    before = settings_path().read_text()

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:   # (never monkeypatch.undo(): it ends the isolation)
        patched.setattr(core_settings.os, "replace", boom)
        save_settings(Settings(backup_days=9))
    assert settings_path().read_text() == before
    assert [p.name for p in settings_path().parent.iterdir()] == ["settings.json"]


def test_gui_settings_is_a_wrapper_over_the_same_file(isolated_env):
    from easy_installer.gui import settings as gui_settings

    save_settings(Settings(backup_days=30, check_updates=False))
    assert gui_settings.get_setting(gui_settings.DEFAULT_HANDLER_OFFER_DISMISSED, False) is False
    gui_settings.set_setting(gui_settings.DEFAULT_HANDLER_OFFER_DISMISSED, True)
    assert gui_settings.load_settings() == {
        "backup_days": 30, "check_updates": False, "update_interval_hours": 24,
        "default_handler_offer_dismissed": True}
    settings = load_settings()
    assert settings.backup_days == 30 and settings.check_updates is False
    assert settings.extra == {"default_handler_offer_dismissed": True}

    gui_settings.save_settings({"only": "this"})
    assert gui_settings.load_settings() == {"only": "this"}
    assert load_settings() == Settings(extra={"only": "this"})
    assert core_settings.get_setting("only") == "this"
    assert core_settings.get_setting("missing", 5) == 5
    assert stat.S_ISREG(settings_path().stat().st_mode)
