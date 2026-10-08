"""CLI and entry point tests (in-process and via ``python -m easy_installer``) with fake AppImages."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

import easy_installer.__main__ as entry
from easy_installer import cli
from easy_installer.core import inspector, installer, privileged, system_checks
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import AuthorizationError, HelperError, InstallError

from fakeappimage import (
    EM_AARCH64,
    T3_DESKTOP,
    make_fake_appimage,
    make_png,
    make_sample_appimage,
    payload_offset_of,
    requires_mksquashfs,
    requires_unsquashfs,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

OPENSCAD_FILE = "OpenSCAD-2026.03.28-x86_64.AppImage"
T3_FILE = "T3-Code-0.0.42-x86_64.AppImage"

INFO_KEYS = {
    "path", "file_name", "size", "sha256", "app_id", "name", "display_name", "version", "comment",
    "categories", "appimage_type", "arch", "host_arch", "desktop_filename", "desktop_entry", "icon",
    "is_electron", "exec_has_no_sandbox", "terminal", "update_info", "elf", "needs_sandbox_fix",
    "can_run_directly", "installed", "warnings",
    # 0.2
    "kind", "update_source", "origin_url", "signature", "data_hints",
}
ELF_KEYS = {"bits", "little_endian", "machine", "arch", "payload_offset", "appimage_type",
            "has_interp", "sections"}
APP_KEYS = {f.name for f in dataclasses.fields(InstalledApp)} - {"extra"} | {"status", "update"}


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


@pytest.fixture
def status_box(monkeypatch):
    """Deterministic system status for the core and the CLI; replace ``box[0]`` to change it."""
    box = [make_status()]
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: box[0])
    monkeypatch.setattr(system_checks, "get_system_status", lambda refresh=False: box[0])
    return box


@pytest.fixture
def sys_layout(system_root, monkeypatch):
    """System scope points into a temporary root instead of /."""
    layout = system_layout(system_root)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    return layout


def _never_start_pkexec():
    raise AssertionError("the tests must never start pkexec")


@pytest.fixture(autouse=True)
def cli_env(status_box, sys_layout, monkeypatch):
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    # A safety net that does not depend on EASY_INSTALLER_DISABLE_PKEXEC: the real helper
    # (pkexec, root, the real /opt) is never started, whatever a test does to the environment.
    monkeypatch.setattr(privileged, "helper_command", _never_start_pkexec)
    return sys_layout


@pytest.fixture
def downloads(isolated_env) -> Path:
    d = isolated_env / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """A private temp dir for work directories, so leftovers can be detected."""
    d = tmp_path / "private-tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def run_cli(*args: str, stdin: str | None = None, monkeypatch=None) -> int:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli.main(["easy-installer", *args])


def openscad(downloads: Path, name: str = OPENSCAD_FILE) -> Path:
    return make_sample_appimage(downloads / name, "openscad")


def t3(downloads: Path, *, version: str = "0.0.42", exec_line: str | None = None,
       name: str | None = None) -> Path:
    desktop = T3_DESKTOP.replace("X-AppImage-Version=0.0.42", f"X-AppImage-Version={version}")
    if exec_line is not None:
        desktop = desktop.replace("Exec=AppRun --no-sandbox %U", f"Exec={exec_line}")
    icon = "usr/share/icons/hicolor/512x512/apps/t3code.png"
    return make_fake_appimage(downloads / (name or f"T3-Code-{version}-x86_64.AppImage"), {
        "t3code.desktop": desktop,
        "AppRun": "#!/bin/sh\n",
        "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": f"asar {version}",
        icon: make_png(512, 512),
    }, {".DirIcon": icon})


def user_registry() -> Registry:
    return Registry(user_layout().registry_path)


def put_system_app(layout, app_id: str = "t3code", name: str = "T3 Code (Alpha)",
                   version: str = "0.0.40") -> InstalledApp:
    """Register a (fake) system-wide installation directly in the temporary system registry."""
    layout.apps_dir.mkdir(parents=True, exist_ok=True)
    layout.desktop_dir.mkdir(parents=True, exist_ok=True)
    appimage = layout.apps_dir / f"{app_id}.AppImage"
    appimage.write_bytes(b"\x7fELF fake")
    desktop = layout.desktop_dir / f"easyinstaller-{app_id}.desktop"
    desktop.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec={appimage} %U\n")
    st = appimage.stat()  # recorded like the helper does: reconcile finds nothing changed
    app = InstalledApp(id=app_id, name=name, version=version, scope=Scope.SYSTEM,
                       appimage_path=str(appimage), desktop_path=str(desktop),
                       size=st.st_size, mtime_ns=st.st_mtime_ns)
    Registry(layout.registry_path).put(app)
    return app


class HelperRecorder:
    """Stands in for privileged.run_helper; simulates the system side in the temporary root."""

    def __init__(self, layout):
        self.layout = layout
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, op: str, payload: dict, *, timeout=600) -> dict:
        payload = json.loads(json.dumps(payload))  # must survive the JSON channel
        self.calls.append((op, payload))
        registry = Registry(self.layout.registry_path)
        if op == "install":
            app = InstalledApp(
                id=payload["app_id"], name=payload["name"], version=payload["version"],
                scope=Scope.SYSTEM,
                appimage_path=str(self.layout.apps_dir / f"{payload['app_id']}.AppImage"),
                desktop_path=str(self.layout.desktop_dir / f"easyinstaller-{payload['app_id']}.desktop"),
                extract_and_run=payload["extract_and_run"], size=payload["size"])
            registry.put(app)
            return {"ok": True, "app": app.to_dict()}
        if op == "uninstall":
            registry.remove(payload["app_id"])
            return {"ok": True}
        raise HelperError(f"unexpected op {op}")


def route_to_helper_ops(layout):
    """A run_helper that executes the real root-side operations against ``layout``."""
    ops = pytest.importorskip("easy_installer.helper.ops",
                              reason="easy_installer.helper.ops is not available yet")

    def run_helper(op: str, payload: dict, *, timeout=600) -> dict:
        payload = json.loads(json.dumps(payload))
        try:
            if op == "install":
                result = ops.op_install(payload, layout=layout, caller_uid=os.getuid(),
                                        caller_gid=os.getgid(), run_commands=False)
            elif op == "uninstall":
                result = ops.op_uninstall(payload, layout=layout, run_commands=False)
            elif op == "apparmor-install":
                result = ops.op_apparmor_install(payload, layout=layout, caller_uid=os.getuid(),
                                                 caller_home=Path(os.environ["HOME"]),
                                                 run_commands=False)
            elif op == "apparmor-remove":
                result = ops.op_apparmor_remove(payload, layout=layout, run_commands=False)
            else:
                raise AssertionError(f"unexpected helper op {op}")
        except HelperError:
            raise
        except Exception as exc:  # like helper/main.py: every failure becomes ok=false
            raise HelperError(str(exc)) from exc
        if result.get("ok") is not True:
            raise HelperError(str(result.get("error") or "helper failed"))
        return result

    return run_helper


# ------------------------------------------------------------------------------------------------
# __main__ dispatch
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_gui(monkeypatch):
    calls: list[list[str]] = []
    module = types.ModuleType(entry.GUI_MODULE)
    module.run = lambda argv: calls.append(list(argv)) or 0
    monkeypatch.setitem(sys.modules, entry.GUI_MODULE, module)
    monkeypatch.setattr(entry, "gui_problem", lambda: None)
    return calls


@pytest.mark.parametrize("command", ["install", "info", "list", "uninstall", "launch", "check",
                                     "update", "rollback", "details", "settings", "repair",
                                     "--help", "-h", "--version"])
def test_main_dispatches_cli(command, monkeypatch, fake_gui):
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 7)
    assert entry.main(["easy-installer", command, "x"]) == 7
    assert seen == [["easy-installer", command, "x"]]
    assert fake_gui == []


def test_main_verbose_before_command_is_cli(monkeypatch, fake_gui):
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    assert entry.main(["easy-installer", "-v", "list"]) == 0
    assert seen == [["easy-installer", "-v", "list"]]


@pytest.mark.parametrize("args", [[], ["MyApp.AppImage"], ["--uninstall", "t3code"],
                                  ["a.AppImage", "b.AppImage"], ["/tmp/Tool"], ["./tool"],
                                  ["file:///home/u/Downloads/Tool"], ["--uninstall=t3code"],
                                  ["--", "x"], ["-v", "App.appimage"]])
def test_main_dispatches_gui(args, monkeypatch, fake_gui):
    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("CLI must not run"))
    assert entry.main(["easy-installer", *args]) == 0
    assert fake_gui == [["easy-installer", *args]]


def test_main_existing_file_without_extension_opens_the_window(monkeypatch, fake_gui, tmp_path):
    (tmp_path / "remove").write_bytes(b"\x7fELF")  # a file really called like a command typo
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("CLI must not run"))
    assert entry.main(["easy-installer", "remove"]) == 0


@pytest.mark.parametrize("args", [["remove", "openscad"], ["help"], ["upgrade", "x"], ["ls"],
                                  ["--list"], ["-v", "remove", "x"]])
def test_main_sends_unknown_commands_to_the_cli(args, monkeypatch, fake_gui, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 2)
    assert entry.main(["easy-installer", *args]) == 2
    assert seen == [["easy-installer", *args]] and fake_gui == []


def test_subprocess_typo_is_a_usage_error_not_an_install_dialog():
    proc = run_module("remove", "openscad", drop=("DISPLAY", "WAYLAND_DISPLAY"))
    assert proc.returncode == 2
    assert "invalid choice: 'remove'" in proc.stderr
    assert "install remove" not in proc.stderr


def test_main_uses_sys_argv(monkeypatch, fake_gui):
    monkeypatch.setattr(sys, "argv", ["easy-installer", "file.AppImage"])
    assert entry.main() == 0
    assert fake_gui == [["easy-installer", "file.AppImage"]]


def test_gui_return_code_is_passed_on(monkeypatch, fake_gui):
    sys.modules[entry.GUI_MODULE].run = lambda argv: 5
    assert entry.main(["easy-installer"]) == 5


def test_gui_unavailable_friendly_message(monkeypatch, capsys):
    monkeypatch.setattr(entry, "gui_problem", lambda: "No graphical desktop was found.")
    assert entry.main(["easy-installer", "/tmp/My App.AppImage"]) == 1
    err = capsys.readouterr().err
    assert "No graphical desktop was found." in err
    assert "easy-installer install '/tmp/My App.AppImage'" in err
    assert "easy-installer --help" in err


def test_gui_unavailable_uninstall_suggestion(monkeypatch, capsys):
    monkeypatch.setattr(entry, "gui_problem", lambda: "missing")
    assert entry.main(["easy-installer", "--uninstall", "t3code"]) == 1
    assert "easy-installer uninstall t3code" in capsys.readouterr().err


def test_gui_import_failure_is_friendly(monkeypatch, capsys):
    monkeypatch.setattr(entry, "gui_problem", lambda: None)
    monkeypatch.setitem(sys.modules, entry.GUI_MODULE, None)  # makes the import fail
    assert entry.main(["easy-installer"]) == 1
    err = capsys.readouterr().err
    assert "cannot open its window" in err
    assert "easy-installer --help" in err


def test_gui_problem_without_gi(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    assert "GTK 4" in entry.gui_problem()


# ------------------------------------------------------------------------------------------------
# argument parsing, --version, --help, usage errors
# ------------------------------------------------------------------------------------------------


def test_version(capsys):
    assert entry.main(["easy-installer", "--version"]) == 0
    assert capsys.readouterr().out == "Easy Installer 0.2.2\n"


def test_help(capsys):
    assert cli.main(["easy-installer", "--help"]) == 0
    out = capsys.readouterr().out
    for command in ("install", "info", "list", "uninstall", "launch", "check", "update",
                    "rollback", "details", "settings", "repair"):
        assert command in out


def test_install_help_lists_options(capsys):
    assert cli.main(["easy-installer", "install", "-h"]) == 0
    out = capsys.readouterr().out
    for option in ("--system", "--keep", "--sandbox-fix", "--extract-and-run",
                   "--allow-foreign-arch", "-y", "--keep-both", "--no-backup", "--executable"):
        assert option in out


@pytest.mark.parametrize("args", [
    [],
    ["bogus"],
    ["install"],
    ["install", "x.AppImage", "--sandbox-fix", "maybe"],
    ["install", "x.AppImage", "--extract-and-run", "sometimes"],
    ["list", "--user", "--system"],
    ["uninstall", "x", "--user", "--system"],
    ["info"],
    ["launch"],
    ["check", "--bogus"],
])
def test_usage_errors_exit_2(args, capsys):
    assert cli.main(["easy-installer", *args]) == 2
    assert "usage:" in capsys.readouterr().err


def test_install_options_mapping():
    parser = cli.build_parser()
    args = parser.parse_args(["install", "f", "--system", "--keep", "--sandbox-fix", "no-sandbox",
                              "--extract-and-run", "yes", "--allow-foreign-arch", "-y"])
    options = cli.install_options(args)
    assert options.scope is Scope.SYSTEM
    assert options.keep_original is True
    assert options.sandbox_fix is cli.SandboxFix.NO_SANDBOX
    assert options.extract_and_run is True
    assert options.allow_foreign_arch is True
    defaults = cli.install_options(parser.parse_args(["install", "f"]))
    assert defaults.scope is Scope.USER and defaults.sandbox_fix is None
    assert defaults.extract_and_run is None and not defaults.keep_original


# ------------------------------------------------------------------------------------------------
# info
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_info_human(downloads, capsys):
    src = openscad(downloads)
    assert run_cli("info", str(src)) == 0
    out = capsys.readouterr().out
    assert "OpenSCAD" in out
    assert "2026.03.28" in out
    assert "openscad" in out
    assert "openscad.desktop" in out
    assert "PNG" in out
    assert src.exists()  # info never moves anything


@requires_mksquashfs
@requires_unsquashfs
def test_info_json_schema(downloads, capsys):
    src = t3(downloads)
    assert run_cli("info", "--json", str(src)) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == INFO_KEYS
    assert set(data["elf"]) == ELF_KEYS
    assert data["app_id"] == "t3code"
    assert data["name"] == "T3 Code (Alpha)"
    assert data["version"] == "0.0.42"
    assert data["is_electron"] is True
    assert data["exec_has_no_sandbox"] is True
    assert data["categories"] == ["Development"]
    assert data["elf"]["payload_offset"] == payload_offset_of(src)
    assert data["path"] == str(src.resolve())
    assert data["size"] == src.stat().st_size
    assert len(data["sha256"]) == 64
    assert data["icon"] == {"path_in_appimage": "usr/share/icons/hicolor/512x512/apps/t3code.png",
                            "format": "png", "width": 512, "height": 512}
    assert data["desktop_entry"].startswith("[Desktop Entry]")
    assert data["installed"] == []
    assert data["can_run_directly"] is True
    assert data["needs_sandbox_fix"] is False
    json.dumps(data)  # no bytes anywhere


@requires_mksquashfs
@requires_unsquashfs
def test_info_json_reports_installation_and_sandbox(downloads, capsys, status_box):
    status_box[0] = make_status(userns_restricted=True, dev_fuse=False)
    src = t3(downloads, exec_line="AppRun %U")
    assert run_cli("install", "-y", "--keep", "--sandbox-fix", "no-sandbox", str(src)) == 0
    capsys.readouterr()
    assert run_cli("info", "--json", str(src)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["needs_sandbox_fix"] is True
    assert data["can_run_directly"] is False
    assert [a["id"] for a in data["installed"]] == ["t3code"]
    assert data["installed"][0]["scope"] == "user"


@requires_mksquashfs
@requires_unsquashfs
def test_info_cleans_work_dir(downloads, private_tmp):
    src = openscad(downloads)
    assert run_cli("info", str(src)) == 0
    assert list(private_tmp.iterdir()) == []


def test_info_not_an_appimage(tmp_path, capsys):
    bogus = tmp_path / "notes.AppImage"
    bogus.write_text("hello")
    assert run_cli("info", str(bogus)) == 1
    assert capsys.readouterr().err.startswith("Error: This file is not an AppImage.")


def test_info_missing_file(tmp_path, capsys):
    assert run_cli("info", str(tmp_path / "nope.AppImage")) == 1
    err = capsys.readouterr().err
    assert "Error: The file could not be found." in err
    assert "Details" not in err


def test_info_missing_file_verbose_shows_details(tmp_path, capsys):
    assert run_cli("info", "-v", str(tmp_path / "nope.AppImage")) == 1
    err = capsys.readouterr().err
    assert "Details:" in err
    assert "nope.AppImage" in err


# ------------------------------------------------------------------------------------------------
# install
# ------------------------------------------------------------------------------------------------

@requires_mksquashfs
@requires_unsquashfs
def test_install_yes(downloads, capsys, isolated_env):
    src = openscad(downloads)
    assert run_cli("install", "-y", str(src)) == 0
    out = capsys.readouterr().out
    target = isolated_env / "Applications" / "OpenSCAD.AppImage"
    assert target.is_file() and os.access(target, os.X_OK)
    assert not src.exists()  # moved, like the article
    desktop = user_layout().desktop_dir / "easyinstaller-openscad.desktop"
    assert desktop.is_file()
    app = user_registry().get("openscad")
    assert app is not None and app.version == "2026.03.28"
    # summary
    assert "Ready to install OpenSCAD" in out
    assert "2026.03.28" in out
    assert " MB" in out
    assert "Only for me" in out
    assert str(target) in out
    # done
    assert "OpenSCAD is installed." in out
    assert "app menu" in out
    assert "easy-installer launch openscad" in out
    assert "easy-installer uninstall openscad" in out
    assert "Proceed?" not in out


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer", ["y\n", "\n", "yes\n", "maybe\ny\n"])
def test_install_confirm_yes(answer, downloads, capsys, monkeypatch):
    src = openscad(downloads)
    assert run_cli("install", str(src), stdin=answer, monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Proceed? [Y/n]" in out
    assert user_registry().get("openscad") is not None


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer", ["n\n", "no\n", ""])
def test_install_confirm_no_or_eof_cancels(answer, downloads, capsys, monkeypatch, private_tmp,
                                           isolated_env):
    src = openscad(downloads)
    assert run_cli("install", str(src), stdin=answer, monkeypatch=monkeypatch) == 3
    captured = capsys.readouterr()
    assert "Cancelled" in captured.err
    assert src.exists()
    assert not (isolated_env / "Applications").exists()
    assert user_registry().all() == []
    assert list(private_tmp.iterdir()) == []  # inspection work dir removed


@requires_mksquashfs
@requires_unsquashfs
def test_install_keep(downloads, capsys, isolated_env):
    src = openscad(downloads)
    assert run_cli("install", "-y", "--keep", str(src)) == 0
    assert src.exists()
    assert (isolated_env / "Applications" / "OpenSCAD.AppImage").is_file()
    assert "is kept where it is" in capsys.readouterr().out


@requires_mksquashfs
@requires_unsquashfs
def test_install_update_notice(downloads, capsys):
    assert run_cli("install", "-y", str(t3(downloads, version="0.0.41"))) == 0
    capsys.readouterr()
    assert run_cli("install", "-y", str(t3(downloads, version="0.0.42"))) == 0
    out = capsys.readouterr().out
    assert "Ready to update T3 Code (Alpha)" in out
    assert "Version 0.0.41 is installed and will be replaced by 0.0.42." in out
    assert user_registry().get("t3code").version == "0.0.42"


@requires_mksquashfs
@requires_unsquashfs
def test_install_downgrade_notice(downloads, capsys):
    assert run_cli("install", "-y", str(t3(downloads, version="0.0.42"))) == 0
    capsys.readouterr()
    assert run_cli("install", "-y", str(t3(downloads, version="0.0.40"))) == 0
    out = capsys.readouterr().out
    assert "older version of T3 Code (Alpha)" in out
    assert "replaced by the older version 0.0.40" in out
    assert "Please note:" in out


@requires_mksquashfs
@requires_unsquashfs
def test_install_foreign_arch(downloads, capsys):
    src = make_sample_appimage(downloads / "Arm-1.0-aarch64.AppImage", "openscad",
                               machine=EM_AARCH64)
    assert run_cli("install", "-y", str(src)) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ")
    assert "--allow-foreign-arch" in err
    assert src.exists()
    assert run_cli("install", "-y", "--allow-foreign-arch", str(src)) == 0
    assert "aarch64" in capsys.readouterr().out


@requires_mksquashfs
@requires_unsquashfs
def test_install_extract_and_run(downloads, capsys):
    src = openscad(downloads)
    assert run_cli("install", "-y", "--extract-and-run", "yes", str(src)) == 0
    assert "unpacks itself on every start" in capsys.readouterr().out
    app = user_registry().get("openscad")
    assert app.extract_and_run is True
    exec_value = DesktopEntry.parse(Path(app.desktop_path).read_text()).get("Exec")
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in exec_value


@requires_mksquashfs
@requires_unsquashfs
def test_install_automatic_extract_and_run_without_fuse(downloads, capsys, status_box):
    status_box[0] = make_status(dev_fuse=False)
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    out = capsys.readouterr().out
    assert "FUSE" in out
    assert user_registry().get("openscad").extract_and_run is True


@requires_mksquashfs
@requires_unsquashfs
def test_install_sandbox_no_sandbox(downloads, capsys, status_box):
    status_box[0] = make_status(userns_restricted=True)
    src = t3(downloads, exec_line="AppRun %U")
    assert run_cli("install", "-y", "--sandbox-fix", "no-sandbox", str(src)) == 0
    out = capsys.readouterr().out
    assert "without its security sandbox" in out
    app = user_registry().get("t3code")
    assert app.sandbox_fix == "no-sandbox"
    assert "--no-sandbox" in DesktopEntry.parse(Path(app.desktop_path).read_text()).get("Exec")


@requires_mksquashfs
@requires_unsquashfs
def test_install_sandbox_apparmor_falls_back_when_helper_disabled(downloads, capsys, status_box):
    # conftest disables pkexec: the helper fails, the core falls back to --no-sandbox.
    status_box[0] = make_status(userns_restricted=True)
    src = t3(downloads, exec_line="AppRun %U")
    assert run_cli("install", "-y", str(src)) == 0
    out = capsys.readouterr().out
    assert "system permission" in out  # announced in the summary
    assert "could not get the special permission" in out  # reported afterwards
    assert user_registry().get("t3code").sandbox_fix == "no-sandbox"


@requires_mksquashfs
@requires_unsquashfs
def test_install_ctrl_c_during_inspection(downloads, capsys, monkeypatch, private_tmp):
    src = openscad(downloads)
    real_inspect = inspector.inspect_appimage

    def interrupted(path, **kw):
        progress = kw["progress"]

        def boom(fraction, message):
            if fraction is not None and fraction >= 1.0:
                raise KeyboardInterrupt
            progress(fraction, message)

        return real_inspect(path, **{**kw, "progress": boom})

    monkeypatch.setattr(inspector, "inspect_appimage", interrupted)
    assert run_cli("install", "-y", str(src)) == 3
    assert "Cancelled." in capsys.readouterr().err
    assert src.exists()
    assert list(private_tmp.iterdir()) == []


@requires_mksquashfs
@requires_unsquashfs
def test_install_ctrl_c_during_copy_rolls_back(downloads, capsys, monkeypatch, private_tmp,
                                               isolated_env):
    src = openscad(downloads)
    real_copy = installer._copy_icon

    def interrupted_copy(*args, **kwargs):
        real_copy(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_copy_icon", interrupted_copy)
    assert run_cli("install", "-y", str(src)) == 3
    assert "Cancelled." in capsys.readouterr().err
    assert src.exists()  # moved back
    assert not (isolated_env / "Applications" / "OpenSCAD.AppImage").exists()
    assert user_registry().all() == []
    assert list(private_tmp.iterdir()) == []


@requires_mksquashfs
@requires_unsquashfs
def test_install_ctrl_c_at_prompt(downloads, capsys, monkeypatch, private_tmp):
    src = openscad(downloads)

    def ctrl_c(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", ctrl_c)
    assert run_cli("install", str(src)) == 3
    assert "Cancelled." in capsys.readouterr().err
    assert src.exists()
    assert list(private_tmp.iterdir()) == []


@requires_mksquashfs
@requires_unsquashfs
def test_install_error_from_core(downloads, capsys, monkeypatch):
    def fail(plan, *, progress=None):
        raise InstallError("There is not enough free disk space.", details="ENOSPC details")

    monkeypatch.setattr(installer, "execute_install", fail)
    assert run_cli("install", "-y", str(openscad(downloads))) == 1
    err = capsys.readouterr().err
    assert "Error: There is not enough free disk space." in err
    assert "ENOSPC" not in err
    assert run_cli("install", "-y", "--verbose", str(openscad(downloads, "Other-1.0.AppImage"))) == 1
    assert "ENOSPC details" in capsys.readouterr().err


# -- system scope ------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_install_system_uses_helper(downloads, capsys, monkeypatch, sys_layout):
    helper = HelperRecorder(sys_layout)
    monkeypatch.setattr(privileged, "run_helper", helper)
    src = openscad(downloads)
    assert run_cli("install", "-y", "--system", str(src)) == 0
    out = capsys.readouterr().out
    assert [op for op, _payload in helper.calls] == ["install"]
    manifest = helper.calls[0][1]
    assert manifest["app_id"] == "openscad"
    assert manifest["source_appimage"] == str(src)
    assert "Everyone on this computer" in out
    assert "password" in out
    assert "easy-installer uninstall openscad --system" in out
    assert not src.exists()  # the client removes the user's original after success
    assert Registry(sys_layout.registry_path).get("openscad") is not None
    assert user_registry().all() == []


@requires_mksquashfs
@requires_unsquashfs
def test_install_system_with_pkexec_disabled(downloads, capsys):
    src = openscad(downloads)
    assert run_cli("install", "-y", "--system", str(src)) == 1
    assert "Error: Administrator tasks are turned off" in capsys.readouterr().err
    assert src.exists()


@requires_mksquashfs
@requires_unsquashfs
def test_install_system_password_dismissed_is_cancel(downloads, capsys, monkeypatch):
    def dismissed(op, payload, *, timeout=600):
        raise AuthorizationError("Authentication was cancelled.", details="exit code: 126")

    monkeypatch.setattr(privileged, "run_helper", dismissed)
    src = openscad(downloads)
    assert run_cli("install", "-y", "--system", str(src)) == 3
    assert "Authentication was cancelled." in capsys.readouterr().err
    assert src.exists()


@requires_mksquashfs
@requires_unsquashfs
def test_install_system_not_authorized_is_error(downloads, capsys, monkeypatch):
    def refused(op, payload, *, timeout=600):
        raise AuthorizationError("You are not allowed to do this.", details="exit code: 127")

    monkeypatch.setattr(privileged, "run_helper", refused)
    assert run_cli("install", "-y", "--system", str(openscad(downloads))) == 1


@requires_mksquashfs
@requires_unsquashfs
def test_install_and_uninstall_system_via_helper_ops(downloads, capsys, monkeypatch, sys_layout):
    monkeypatch.setattr(privileged, "run_helper", route_to_helper_ops(sys_layout))
    src = openscad(downloads)
    assert run_cli("install", "-y", "--system", str(src)) == 0
    capsys.readouterr()
    app = Registry(sys_layout.registry_path).get("openscad")
    assert app is not None and app.scope is Scope.SYSTEM
    assert Path(app.appimage_path).is_file()
    assert Path(app.appimage_path).is_relative_to(sys_layout.apps_dir)

    assert run_cli("list", "--json", "--system") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(a["id"], a["scope"], a["status"]) for a in listed] == [("openscad", "system", "ok")]

    assert run_cli("uninstall", "-y", "--system", "openscad") == 0
    assert "was uninstalled" in capsys.readouterr().out
    assert Registry(sys_layout.registry_path).get("openscad") is None
    assert not Path(app.appimage_path).exists()


# ------------------------------------------------------------------------------------------------
# list
# ------------------------------------------------------------------------------------------------


def test_list_empty(capsys):
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "No apps installed yet." in out
    assert "easy-installer install" in out


def test_list_empty_json(capsys):
    assert run_cli("list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


@requires_mksquashfs
@requires_unsquashfs
def test_list_table_and_json(downloads, capsys, sys_layout):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    put_system_app(sys_layout)
    capsys.readouterr()

    assert run_cli("list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["NAME", "ID", "VERSION", "FOR", "SIZE", "UPDATE", "STATUS"]
    assert any(line.split()[:3] == ["OpenSCAD", "openscad", "2026.03.28"] and "Only me" in line
               and " MB" in line and line.rstrip().endswith("OK") for line in out)
    assert any("T3 Code (Alpha)" in line and "t3code" in line and "Everyone" in line for line in out)

    assert run_cli("list", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert [(a["id"], a["scope"]) for a in data] == [("openscad", "user"), ("t3code", "system")]
    for app in data:
        assert set(app) == APP_KEYS
        assert app["status"] == "ok"
        InstalledApp.from_dict(app)  # round-trips

    assert run_cli("list", "--json", "--user") == 0
    assert [a["id"] for a in json.loads(capsys.readouterr().out)] == ["openscad"]
    assert run_cli("list", "--json", "--system") == 0
    assert [a["id"] for a in json.loads(capsys.readouterr().out)] == ["t3code"]


@requires_mksquashfs
@requires_unsquashfs
def test_list_shows_problems(downloads, capsys):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    capsys.readouterr()
    Path(user_registry().get("openscad").appimage_path).unlink()
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "App file missing" in out
    assert "installing them again" in out
    assert run_cli("list", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "missing-appimage"


def test_list_error_is_reported(monkeypatch, capsys):
    def broken(scopes=()):
        raise InstallError("The list could not be read.", details="technical cause")

    monkeypatch.setattr(installer, "list_installed", broken)
    assert run_cli("list") == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: The list could not be read.")
    assert "technical cause" not in err
    assert run_cli("list", "-v") == 1
    assert "technical cause" in capsys.readouterr().err
    assert cli.main(["easy-installer", "-v", "list"]) == 1
    assert "technical cause" in capsys.readouterr().err


def test_unexpected_exception_is_friendly(monkeypatch, capsys):
    def bug(scopes=()):
        raise ZeroDivisionError("oops")

    monkeypatch.setattr(installer, "list_installed", bug)
    assert run_cli("list") == 1
    err = capsys.readouterr().err
    assert "Error: Something unexpected went wrong." in err
    assert "Traceback" not in err
    assert run_cli("list", "-v") == 1
    assert "ZeroDivisionError" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------
# uninstall
# ------------------------------------------------------------------------------------------------


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_yes(downloads, capsys, isolated_env):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    app = user_registry().get("openscad")
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "openscad") == 0
    out = capsys.readouterr().out
    assert "OpenSCAD was uninstalled." in out
    assert "personal files" in out
    assert user_registry().get("openscad") is None
    assert not Path(app.appimage_path).exists()
    assert not Path(app.desktop_path).exists()


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("answer, code", [("y\n", 0), ("n\n", 3), ("\n", 3), ("", 3)])
def test_uninstall_confirm(answer, code, downloads, capsys, monkeypatch):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    capsys.readouterr()
    assert run_cli("uninstall", "openscad", stdin=answer, monkeypatch=monkeypatch) == code
    out = capsys.readouterr().out
    assert "Uninstall OpenSCAD" in out and "[y/N]" in out
    assert (user_registry().get("openscad") is None) == (code == 0)


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("failure", ["cancelled", "helper"])
def test_uninstall_user_app_with_apparmor_profile(failure, downloads, capsys, monkeypatch):
    """Per-user Electron apps on Ubuntu 24.04 have a root-owned profile: removing it asks."""
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    app = user_registry().get("openscad")
    user_registry().put(dataclasses.replace(app, apparmor_profile="/etc/apparmor.d/easyinstaller-openscad",
                                            sandbox_fix="apparmor"))
    calls = []

    def run_helper(op, payload, *, timeout=600):
        calls.append(op)
        if failure == "cancelled":
            raise AuthorizationError("Authentication was cancelled.",
                                     details="command: pkexec ...\nexit code: 126")
        raise HelperError("The administrator task failed unexpectedly.",
                          details="command: pkexec ...\nexit code: 1\nstderr: boom")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    capsys.readouterr()
    code = run_cli("uninstall", "openscad", stdin="y\n", monkeypatch=monkeypatch)
    out, err = capsys.readouterr()
    assert calls == ["apparmor-remove"]
    assert "You may be asked for your password." in out  # before the password dialog appears
    assert "exit code" not in out + err and "WARNING" not in err
    if failure == "cancelled":  # nothing was changed; the uninstall can simply be repeated
        assert code == 3
        assert user_registry().get("openscad") is not None and Path(app.appimage_path).is_file()
        assert "--keep-permission" in err  # the way out without an administrator password
    else:
        assert code == 0
        assert user_registry().get("openscad") is None and not Path(app.appimage_path).exists()
        assert "The special permission that OpenSCAD needed could not be removed." in out


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("broken", [False, True])
def test_uninstall_without_administrator_password(broken, downloads, capsys, monkeypatch):
    """A standard user (or an ssh session without a polkit agent) cannot authorize removing the
    profile an administrator allowed: the app itself must still be removable."""
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    app = user_registry().get("openscad")
    user_registry().put(dataclasses.replace(app, apparmor_profile="/etc/apparmor.d/easyinstaller-openscad",
                                            sandbox_fix="apparmor"))
    if broken:
        Path(app.appimage_path).unlink()
    calls = []

    def run_helper(op, payload, *, timeout=600):
        calls.append(op)
        raise AuthorizationError(
            "You are not allowed to do this, or the password was not accepted.",
            details="command: pkexec ...\nexit code: 127\nstderr: No authentication agent found.")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "openscad") == 1
    err = capsys.readouterr().err
    assert "--keep-permission" in err and user_registry().get("openscad") is not None

    assert run_cli("uninstall", "-y", "--keep-permission", "openscad") == 0
    out = capsys.readouterr().out
    assert calls == ["apparmor-remove"]  # not asked again
    assert "You may be asked for your password." not in out
    assert "OpenSCAD was uninstalled." in out
    assert "The special permission that OpenSCAD needed was left on this computer" in out
    assert user_registry().get("openscad") is None
    assert not Path(app.appimage_path).exists() and not Path(app.desktop_path).exists()


@requires_mksquashfs
@requires_unsquashfs
def test_install_file_without_version_is_not_called_older(downloads, capsys, monkeypatch):
    def foo(name: str, tag: str) -> Path:
        desktop = "[Desktop Entry]\nType=Application\nName=Foo\nExec=foo %U\n"
        return make_fake_appimage(downloads / name, {"foo.desktop": desktop,
                                                     "usr/bin/foo": b"\x7fELF " + tag.encode()},
                                  {"AppRun": "usr/bin/foo"})

    assert run_cli("install", "-y", str(foo("Foo-1.2.AppImage", "a"))) == 0
    capsys.readouterr()
    assert run_cli("install", str(foo("Foo-x86_64.AppImage", "b")), stdin="n\n",
                   monkeypatch=monkeypatch) == 3
    out = capsys.readouterr().out
    assert "Ready to update Foo" in out
    assert "The installed copy of this app will be replaced by this file." in out
    assert "None" not in out and "older" not in out


def test_uninstall_unknown(capsys):
    assert run_cli("uninstall", "-y", "nothing") == 1
    err = capsys.readouterr().err
    assert 'Error: No app with the ID "nothing" is installed.' in err
    assert "easy-installer list" in err


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_unknown_suggests_close_ids(downloads, capsys):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "OpenScad") == 1
    assert "Did you mean: openscad?" in capsys.readouterr().err
    assert user_registry().get("openscad") is not None


def test_uninstall_wrong_scope_flag(capsys, sys_layout):
    put_system_app(sys_layout)
    assert run_cli("uninstall", "-y", "--user", "t3code") == 1
    assert "--system" in capsys.readouterr().err
    assert Registry(sys_layout.registry_path).get("t3code") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_both_scopes_with_yes_needs_flag(downloads, capsys, sys_layout):
    assert run_cli("install", "-y", str(t3(downloads))) == 0
    put_system_app(sys_layout)
    capsys.readouterr()
    assert run_cli("uninstall", "-y", "t3code") == 2
    err = capsys.readouterr().err
    assert "both" in err and "--user" in err and "--system" in err
    assert user_registry().get("t3code") is not None
    assert Registry(sys_layout.registry_path).get("t3code") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_both_scopes_asks_which(downloads, capsys, monkeypatch, sys_layout):
    assert run_cli("install", "-y", str(t3(downloads))) == 0
    put_system_app(sys_layout)
    helper = HelperRecorder(sys_layout)
    monkeypatch.setattr(privileged, "run_helper", helper)
    capsys.readouterr()

    assert run_cli("uninstall", "t3code", stdin="3\n2\ny\n", monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert "installed twice" in out
    assert "1) Only for me (version 0.0.42)" in out
    assert "2) Everyone on this computer (version 0.0.40)" in out
    assert "Please type one of" in out
    assert helper.calls == [("uninstall", {"app_id": "t3code"})]
    assert Registry(sys_layout.registry_path).get("t3code") is None
    assert user_registry().get("t3code") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_both_scopes_choose_user(downloads, capsys, monkeypatch, sys_layout):
    assert run_cli("install", "-y", str(t3(downloads))) == 0
    put_system_app(sys_layout)
    monkeypatch.setattr(privileged, "run_helper", lambda *a, **k: pytest.fail("no helper needed"))
    capsys.readouterr()
    assert run_cli("uninstall", "t3code", stdin="1\ny\n", monkeypatch=monkeypatch) == 0
    assert user_registry().get("t3code") is None
    assert Registry(sys_layout.registry_path).get("t3code") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_both_scopes_cancel_choice(downloads, capsys, monkeypatch, sys_layout):
    assert run_cli("install", "-y", str(t3(downloads))) == 0
    put_system_app(sys_layout)
    capsys.readouterr()
    assert run_cli("uninstall", "t3code", stdin="\n", monkeypatch=monkeypatch) == 3
    assert user_registry().get("t3code") is not None


@requires_mksquashfs
@requires_unsquashfs
def test_uninstall_scope_flags_select(downloads, capsys, monkeypatch, sys_layout):
    assert run_cli("install", "-y", str(t3(downloads))) == 0
    put_system_app(sys_layout)
    helper = HelperRecorder(sys_layout)
    monkeypatch.setattr(privileged, "run_helper", helper)
    assert run_cli("uninstall", "-y", "--system", "t3code") == 0
    assert helper.calls == [("uninstall", {"app_id": "t3code"})]
    assert user_registry().get("t3code") is not None
    assert run_cli("uninstall", "-y", "--user", "t3code") == 0
    assert user_registry().get("t3code") is None


def test_uninstall_system_with_pkexec_disabled(capsys, sys_layout):
    put_system_app(sys_layout)
    assert run_cli("uninstall", "-y", "t3code") == 1
    assert "Administrator tasks are turned off" in capsys.readouterr().err
    assert Registry(sys_layout.registry_path).get("t3code") is not None


def test_uninstall_system_via_helper_ops(capsys, monkeypatch, sys_layout, downloads):
    run_helper = route_to_helper_ops(sys_layout)
    if not (shutil.which("mksquashfs") and shutil.which("unsquashfs")):
        pytest.skip("squashfs-tools are not installed")
    monkeypatch.setattr(privileged, "run_helper", run_helper)
    assert run_cli("install", "-y", "--system", str(t3(downloads))) == 0
    assert run_cli("install", "-y", str(t3(downloads, version="0.0.43"))) == 0
    capsys.readouterr()
    assert run_cli("uninstall", "t3code", stdin="2\ny\n", monkeypatch=monkeypatch) == 0
    assert Registry(sys_layout.registry_path).get("t3code") is None
    assert user_registry().get("t3code") is not None


# ------------------------------------------------------------------------------------------------
# launch
# ------------------------------------------------------------------------------------------------


_REAL_POPEN = subprocess.Popen


class PopenRecorder:
    """Records detached starts (``start_new_session=True``, i.e. `launch`) instead of running them.

    Everything else (unsquashfs & co. used by install) goes to the real Popen.
    """

    def __init__(self, error: OSError | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.error = error

    def __call__(self, argv, *args, **kwargs):
        if not kwargs.get("start_new_session"):
            return _REAL_POPEN(argv, *args, **kwargs)
        if self.error is not None:
            raise self.error
        self.calls.append((list(argv), kwargs))
        return types.SimpleNamespace(pid=4242)


@pytest.fixture
def popen(monkeypatch):
    recorder = PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    return recorder


def assert_detached(kwargs):
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL


@requires_mksquashfs
@requires_unsquashfs
def test_launch_plain(downloads, capsys, popen, isolated_env):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    capsys.readouterr()
    assert run_cli("launch", "openscad") == 0
    assert "Starting OpenSCAD" in capsys.readouterr().out
    [(argv, kwargs)] = popen.calls
    assert argv == [str(isolated_env / "Applications" / "OpenSCAD.AppImage")]  # "%f" dropped
    assert_detached(kwargs)
    assert "APPIMAGE_EXTRACT_AND_RUN" not in kwargs["env"]
    assert kwargs["env"]["HOME"] == str(isolated_env)
    assert kwargs["cwd"] == str(isolated_env)


@requires_mksquashfs
@requires_unsquashfs
def test_launch_keeps_embedded_arguments(downloads, capsys, popen):
    assert run_cli("install", "-y", str(t3(downloads))) == 0  # Exec=AppRun --no-sandbox %U
    app = user_registry().get("t3code")
    assert run_cli("launch", "t3code") == 0
    [(argv, kwargs)] = popen.calls
    assert argv == [app.appimage_path, "--no-sandbox"]


@requires_mksquashfs
@requires_unsquashfs
def test_launch_extract_and_run(downloads, capsys, popen):
    assert run_cli("install", "-y", "--extract-and-run", "yes", str(openscad(downloads))) == 0
    app = user_registry().get("openscad")
    assert run_cli("launch", "openscad") == 0
    [(argv, kwargs)] = popen.calls
    assert argv == [app.appimage_path]
    assert kwargs["env"]["APPIMAGE_EXTRACT_AND_RUN"] == "1"
    assert_detached(kwargs)


@requires_mksquashfs
@requires_unsquashfs
def test_launch_no_sandbox_fix(downloads, capsys, popen, status_box):
    status_box[0] = make_status(userns_restricted=True)
    src = t3(downloads, exec_line="AppRun %U")
    assert run_cli("install", "-y", "--sandbox-fix", "no-sandbox", str(src)) == 0
    app = user_registry().get("t3code")
    assert run_cli("launch", "t3code") == 0
    assert popen.calls[0][0] == [app.appimage_path, "--no-sandbox"]


@requires_mksquashfs
@requires_unsquashfs
def test_launch_without_launcher_uses_registry(downloads, capsys, popen, status_box):
    status_box[0] = make_status(userns_restricted=True)
    src = t3(downloads, exec_line="AppRun %U")
    assert run_cli("install", "-y", "--sandbox-fix", "no-sandbox", "--extract-and-run", "yes",
                   str(src)) == 0
    app = user_registry().get("t3code")
    Path(app.desktop_path).unlink()
    assert run_cli("launch", "t3code") == 0
    [(argv, kwargs)] = popen.calls
    assert argv == [app.appimage_path, "--no-sandbox"]
    assert kwargs["env"]["APPIMAGE_EXTRACT_AND_RUN"] == "1"


@requires_mksquashfs
@requires_unsquashfs
def test_launch_ignores_launcher_pointing_elsewhere(downloads, capsys, popen):
    assert run_cli("install", "-y", str(openscad(downloads))) == 0
    app = user_registry().get("openscad")
    Path(app.desktop_path).write_text("[Desktop Entry]\nType=Application\nName=X\nExec=/bin/sh -c evil\n")
    assert run_cli("launch", "openscad") == 0
    assert popen.calls[0][0] == [app.appimage_path]


def test_launch_prefers_user_scope(capsys, popen, sys_layout, isolated_env):
    put_system_app(sys_layout, app_id="t3code")
    user = user_layout()
    user.apps_dir.mkdir(parents=True)
    appimage = user.apps_dir / "T3.AppImage"
    appimage.write_bytes(b"\x7fELF")
    user_registry().put(InstalledApp(id="t3code", name="T3", version="1", scope=Scope.USER,
                                     appimage_path=str(appimage), desktop_path=""))
    assert run_cli("launch", "t3code") == 0
    assert popen.calls[0][0] == [str(appimage)]


def test_launch_system_app(capsys, popen, sys_layout):
    app = put_system_app(sys_layout)
    assert run_cli("launch", "t3code") == 0
    assert popen.calls[0][0] == [app.appimage_path]  # "%U" dropped


def test_launch_missing_appimage(capsys, popen, sys_layout):
    app = put_system_app(sys_layout)
    Path(app.appimage_path).unlink()
    assert run_cli("launch", "t3code") == 1
    err = capsys.readouterr().err
    assert "is missing" in err and "again" in err
    assert popen.calls == []


def test_launch_unknown(capsys, popen):
    assert run_cli("launch", "ghost") == 1
    assert 'No app with the ID "ghost"' in capsys.readouterr().err
    assert popen.calls == []


def test_launch_popen_failure(capsys, monkeypatch, sys_layout):
    put_system_app(sys_layout)
    monkeypatch.setattr(subprocess, "Popen", PopenRecorder(error=PermissionError(13, "denied")))
    assert run_cli("launch", "t3code") == 1
    assert "could not be started" in capsys.readouterr().err


@pytest.mark.parametrize("value, expected", [
    ("/opt/a.AppImage %U", ({}, ["/opt/a.AppImage"])),
    ("env APPIMAGE_EXTRACT_AND_RUN=1 /opt/a.AppImage --no-sandbox %U",
     ({"APPIMAGE_EXTRACT_AND_RUN": "1"}, ["/opt/a.AppImage", "--no-sandbox"])),
    ('"/opt/My App.AppImage" - --single-instance %F',
     ({}, ["/opt/My App.AppImage", "-", "--single-instance"])),
    ("/opt/a.AppImage %i %c %k", ({}, ["/opt/a.AppImage"])),
    ('/opt/a.AppImage "unbalanced', None),
    ("", None),
    ("env A=1", None),
])
def test_parse_exec_command(value, expected):
    assert cli.parse_exec_command(value) == expected


# ------------------------------------------------------------------------------------------------
# check
# ------------------------------------------------------------------------------------------------


def test_check_all_good(capsys):
    assert run_cli("check") == 0
    out = capsys.readouterr().out
    assert "Computer:" in out
    assert "✓" in out
    assert "unsquashfs" in out and "libfuse2" in out and "/dev/fuse" in out and "pkexec" in out
    # the optional cache tools are missing in make_status()
    assert "Everything important is ready" in out
    assert "sudo apt install desktop-file-utils" in out


def test_check_reports_problems_with_hints(capsys, status_box):
    status_box[0] = make_status(unsquashfs=None, libfuse2=False, fusermount=None, dev_fuse=False,
                                pkexec=None, userns_restricted=True)
    assert run_cli("check") == 0
    out = capsys.readouterr().out
    assert "✗" in out
    assert "sudo apt install squashfs-tools" in out
    assert "sudo apt install libfuse2t64" in out
    assert "sudo apt install fuse3" in out
    assert "sudo modprobe fuse" in out
    assert "sudo apt install pkexec" in out
    assert "without their sandbox" in out
    assert "problems found" in out


def test_check_hints_without_apt(capsys, status_box):
    status_box[0] = make_status(unsquashfs=None, has_apt=False, libfuse2_package="libfuse2")
    assert run_cli("check") == 0
    out = capsys.readouterr().out
    assert "sudo apt" not in out.split("unsquashfs")[1].splitlines()[1]
    assert 'install the package "squashfs-tools"' in out
    assert "1 problem found" in out


def test_check_restricted_userns_with_apparmor_fix(capsys, status_box):
    status_box[0] = make_status(userns_restricted=True, update_desktop_database="/usr/bin/u",
                                icon_cache_tool="/usr/bin/g")
    assert run_cli("check") == 0
    out = capsys.readouterr().out
    assert "adds a permission" in out
    assert "Everything is ready" in out


def test_check_json(capsys, status_box):
    status_box[0] = make_status(libfuse2=False)
    assert run_cli("check", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"ready", "problems", "computer", "checks", "status"}
    assert data["ready"] is False and data["problems"] == 1
    ids = [c["id"] for c in data["checks"]]
    assert ids == ["unsquashfs", "libfuse2", "fusermount", "dev_fuse", "userns", "apparmor",
                   "pkexec", "update_desktop_database", "icon_cache"]
    for check in data["checks"]:
        assert set(check) == {"id", "ok", "label", "detail", "hint", "important"}
    libfuse = data["checks"][1]
    assert libfuse["ok"] is False and "libfuse2t64" in libfuse["hint"]
    assert data["status"]["libfuse2_package"] == "libfuse2t64"
    assert data["status"]["distro_like"] == ["ubuntu", "debian"]
    assert set(data["computer"]) == {"name", "id", "like", "version", "host_arch"}


# ------------------------------------------------------------------------------------------------
# progress line and helpers
# ------------------------------------------------------------------------------------------------


def test_progress_line_draws_and_clears():
    stream = io.StringIO()
    progress = cli.ProgressLine(stream, enabled=True)
    progress(0.5, "Copying the app…")
    progress(0.5, "Copying the app…")  # unchanged: not redrawn
    progress(None, "Adding the app to the menu…")
    progress.clear()
    text = stream.getvalue()
    assert text.count("Copying the app…") == 1
    assert " 50%" in text
    assert "Adding the app to the menu…" in text
    assert text.endswith("\r\033[K")


def test_progress_line_silent_when_not_a_terminal():
    stream = io.StringIO()
    progress = cli.ProgressLine(stream)
    progress(0.5, "x")
    progress.clear()
    assert stream.getvalue() == ""


def test_text_width_counts_wide_characters():
    assert cli._text_width("abc") == 3
    assert cli._text_width("형상") == 4


def test_logging_is_restored(capsys):
    import logging

    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    assert run_cli("list", "-vv") == 0
    assert (list(root.handlers), root.level) == before


# ------------------------------------------------------------------------------------------------
# subprocess: the real entry point
# ------------------------------------------------------------------------------------------------


def run_module(*args: str, stdin: str | None = None, env_update: dict | None = None,
               drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    env = dict(os.environ)  # already isolated by conftest (HOME, XDG_*, pkexec disabled)
    env["PYTHONPATH"] = str(SRC)
    for key in drop:
        env.pop(key, None)
    env.update(env_update or {})
    return subprocess.run([sys.executable, "-m", "easy_installer", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=120, cwd=str(REPO))


@pytest.mark.parametrize("args", [["check"], ["--help"], ["--version"], ["list"],
                                  ["install", "--help"]])
def test_subprocess_reader_that_quits_early(args):
    """`easy-installer check | true`: no "Exception ignored ... BrokenPipeError", no exit 120."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader is already gone
    try:
        proc = subprocess.run([sys.executable, "-m", "easy_installer", *args], stdout=write_end,
                              stderr=subprocess.PIPE, text=True, env=env, timeout=120,
                              cwd=str(REPO))
    finally:
        os.close(write_end)
    assert "Exception ignored" not in proc.stderr and "BrokenPipeError" not in proc.stderr
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
@pytest.mark.parametrize("args", [["check"], ["--help"], ["list", "--json"], ["list"]])
def test_subprocess_output_that_cannot_be_written(args):
    """`easy-installer list --json > /media/full-disk/apps.json`: a friendly error, exit 1."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    with open("/dev/full", "w") as full:
        proc = subprocess.run([sys.executable, "-m", "easy_installer", *args], stdout=full,
                              stderr=subprocess.PIPE, text=True, env=env, timeout=120,
                              cwd=str(REPO))
    assert "Exception ignored" not in proc.stderr and "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 1, proc.stderr
    assert "The output could not be written" in proc.stderr


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("json_output", [False, True])
def test_info_of_a_file_name_that_is_not_utf8(json_output, downloads, capsys):
    src = openscad(downloads, name=os.fsdecode(b"Caf\xe9-Tool.AppImage"))
    assert run_cli("info", *(["--json"] if json_output else []), str(src)) == 0
    out = capsys.readouterr().out
    assert "Caf\ufffd-Tool.AppImage" in out
    if json_output:
        data = json.loads(out)
        assert data["file_name"] == "Caf\ufffd-Tool.AppImage"
        assert data["path"].endswith("/Caf\ufffd-Tool.AppImage")


@requires_mksquashfs
@requires_unsquashfs
def test_subprocess_closed_stdin_means_no_answer(downloads):
    src = openscad(downloads)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run([sys.executable, "-m", "easy_installer", "install", str(src)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                          timeout=120, cwd=str(REPO), preexec_fn=lambda: os.close(0))
    assert proc.returncode == 3, proc.stderr
    assert "No answer was given. Add -y to skip the question." in proc.stderr
    assert "Something unexpected" not in proc.stderr
    assert src.is_file()


def test_ask_without_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.confirm("Proceed? [Y/n] ", default=True) is False

    def lost(prompt):
        raise RuntimeError("input(): lost sys.stdin")

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr("builtins.input", lost)
    assert cli.confirm("Proceed? [Y/n] ", default=True) is False
    assert capsys.readouterr().err.count("No answer was given") == 2


def test_subprocess_version():
    proc = run_module("--version")
    assert proc.returncode == 0
    assert proc.stdout == "Easy Installer 0.2.2\n"


def test_subprocess_usage_error():
    proc = run_module("install")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


def test_subprocess_gui_without_display_is_friendly(tmp_path):
    proc = run_module(drop=("DISPLAY", "WAYLAND_DISPLAY", "GDK_BACKEND", "BROADWAY_DISPLAY"),
                      env_update={"XDG_RUNTIME_DIR": str(tmp_path)})
    assert proc.returncode == 1
    assert "easy-installer --help" in proc.stderr
    assert "Traceback" not in proc.stderr


@requires_mksquashfs
@requires_unsquashfs
def test_subprocess_full_cycle(downloads, isolated_env):
    src = openscad(downloads)
    info = run_module("info", "--json", str(src))
    assert info.returncode == 0, info.stderr
    assert json.loads(info.stdout)["app_id"] == "openscad"

    cancelled = run_module("install", str(src), stdin="n\n")
    assert cancelled.returncode == 3
    assert "Proceed? [Y/n]" in cancelled.stdout
    assert src.exists()

    installed = run_module("install", str(src), stdin="y\n")
    assert installed.returncode == 0, installed.stderr
    assert "OpenSCAD is installed." in installed.stdout
    assert not src.exists()
    assert (isolated_env / "Applications" / "OpenSCAD.AppImage").is_file()

    listed = run_module("list", "--json", "--user")
    assert listed.returncode == 0
    assert [(a["id"], a["status"]) for a in json.loads(listed.stdout)] == [("openscad", "ok")]

    table = run_module("list", "--user")
    assert "OpenSCAD" in table.stdout

    removed = run_module("uninstall", "-y", "--user", "openscad")
    assert removed.returncode == 0, removed.stderr
    assert json.loads(run_module("list", "--json", "--user").stdout) == []


def test_subprocess_check():
    proc = run_module("check")
    assert proc.returncode == 0
    assert "unsquashfs" in proc.stdout
    assert "✓" in proc.stdout or "✗" in proc.stdout
    proc = run_module("check", "--json")
    assert proc.returncode == 0
    assert "checks" in json.loads(proc.stdout)


def test_subprocess_error_exit_code(tmp_path):
    proc = run_module("info", str(tmp_path / "missing.AppImage"))
    assert proc.returncode == 1
    assert proc.stderr.startswith("Error: ")


@requires_mksquashfs
@requires_unsquashfs
def test_subprocess_ctrl_c_at_prompt(downloads, tmp_path):
    src = openscad(downloads)
    work_tmp = tmp_path / "proc-tmp"
    work_tmp.mkdir()
    env = dict(os.environ, PYTHONPATH=str(SRC), TMPDIR=str(work_tmp))
    proc = subprocess.Popen([sys.executable, "-m", "easy_installer", "install", str(src)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=str(REPO))
    try:
        seen = b""
        deadline = time.monotonic() + 60
        while b"[Y/n]" not in seen:
            assert time.monotonic() < deadline, seen
            ready, _w, _x = select.select([proc.stdout], [], [], 1.0)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                assert chunk, "process ended before asking"
                seen += chunk
        assert any(work_tmp.iterdir())  # the inspection work dir exists while asking
        proc.send_signal(signal.SIGINT)
        _out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 3
    assert b"Cancelled." in err
    assert b"Traceback" not in err
    assert src.exists()
    assert list(work_tmp.iterdir()) == []
