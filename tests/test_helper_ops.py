"""Tests for the root-side helper operations (run without root against a fake system root).

Every operation runs with ``layout=system_layout(system_root)``, ``run_commands=False`` (or faked
commands) and the test user as caller. The attacks from the helper's threat model that can be
simulated without root are covered here; the euid switch itself is tested by faking
``os.geteuid``/``os.seteuid``/``os.setegid``/``os.setgroups``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from easy_installer.core import installer, privileged
from easy_installer.core.desktop_entry import DesktopEntry, exec_program
from easy_installer.core.inspector import inspect_appimage
from easy_installer.core.installer import InstallOptions, execute_install, plan_install, uninstall
from easy_installer.core.paths import Scope, system_layout, user_layout
from easy_installer.core.registry import InstalledApp, Registry
from easy_installer.core.sandbox import render_apparmor_profile
from easy_installer.core.system_checks import SystemStatus
from easy_installer.errors import HelperError, InstallError, NotInstalledError
from easy_installer.helper import ops
from easy_installer.helper.ops import (
    ProfileConflictError,
    RequestError,
    SafeDir,
    UnsafePathError,
)

from fakeappimage import (
    ANYTYPE_DESKTOP,
    FREECAD_DESKTOP,
    T3_DESKTOP,
    build_runtime,
    make_fake_appimage,
    make_png,
    make_sample_appimage,
    make_svg,
    make_xpm,
    requires_mksquashfs,
    requires_unsquashfs,
)

ME = {"caller_uid": os.getuid(), "caller_gid": os.getgid()}
IS_ROOT = os.geteuid() == 0
not_root = pytest.mark.skipif(IS_ROOT, reason="permission checks do not apply to root")

T3_FILE = "T3-Code-0.0.42-x86_64.AppImage"
T3_TARGET = "T3-Code-Alpha.AppImage"


# ------------------------------------------------------------------------------------------------
# helpers & fixtures
# ------------------------------------------------------------------------------------------------


def write_appimage(path: Path, payload: bytes = b"squashfs payload " * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_runtime() + payload)
    path.chmod(0o755)
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_manifest(src: Path, icon: Path | None = None, **overrides) -> dict:
    manifest = {
        "app_id": "t3code",
        "name": "T3 Code (Alpha)",
        "version": "0.0.42",
        "comment": "T3 Code desktop build",
        "source_appimage": str(src),
        "sha256": overrides["sha256"] if "sha256" in overrides else sha256(src),
        "embedded_desktop": T3_DESKTOP,
        "embedded_desktop_filename": "t3code.desktop",
        "icon_source": str(icon) if icon is not None else None,
        "extra_args": [],
        "extract_and_run": False,
        "apparmor": False,
        "uninstall_command": None,
        "arch": "x86_64",
        "size": overrides["size"] if "size" in overrides else src.stat().st_size,
        "update_info": None,
        "original_filename": src.name,
        "is_electron": True,
    }
    manifest.update(overrides)
    return manifest


def install(layout, manifest, **kwargs) -> dict:
    kwargs = {**ME, "run_commands": False, **kwargs}
    return ops.op_install(manifest, layout=layout, **kwargs)


def read_desktop(path: Path | str) -> DesktopEntry:
    return DesktopEntry.parse(Path(path).read_text(encoding="utf-8"))


def entries(root: Path) -> list[str]:
    """All paths below ``root`` (files, folders, links), relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def leftovers(layout) -> list[str]:
    """Temporary or backup files the helper might have forgotten."""
    found = []
    for base in (layout.apps_dir, layout.desktop_dir, layout.icons_dir, layout.apparmor_dir):
        if base.exists():
            found += [str(p) for p in base.rglob(".easyinstaller-*")]
    return found


def make_status(**overrides) -> SystemStatus:
    base = dict(
        unsquashfs="/usr/bin/unsquashfs", pkexec="/usr/bin/pkexec",
        apparmor_parser="/usr/sbin/apparmor_parser", update_desktop_database=None,
        icon_cache_tool=None, desktop_file_validate=None, libfuse2=True,
        fusermount="/usr/bin/fusermount3", dev_fuse=True, userns_restricted=False,
        apparmor_enabled=True, distro_id="zorin", distro_like=("ubuntu", "debian"),
        distro_version="18", has_apt=True, libfuse2_package="libfuse2t64",
    )
    base.update(overrides)
    return SystemStatus(**base)


class FakeCommands:
    """Replaces ops.find_tool / ops.run_command; ``fail`` = {(tool name, first arg)}."""

    def __init__(self, fail=(), missing=()):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail = set(fail)
        self.missing = set(missing)
        self.existed: list[bool] = []

    def which(self, name):
        return None if name in self.missing else f"/usr/bin/{name}"

    def run(self, cmd, *, timeout=ops.TOOL_TIMEOUT, env=None):
        self.calls.append((list(cmd), dict(env or {})))
        if len(cmd) > 2 and cmd[-1].startswith("/"):
            self.existed.append(os.path.exists(cmd[-1]))
        key = (Path(cmd[0]).name, cmd[1] if len(cmd) > 1 else None)
        if key in self.fail:
            return ops.CommandResult(1, "simulated failure")
        return ops.CommandResult(0, "")

    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _env in self.calls]


@pytest.fixture
def commands(monkeypatch):
    fake = FakeCommands()
    monkeypatch.setattr(ops, "find_tool", fake.which)
    monkeypatch.setattr(ops, "run_command", fake.run)
    return fake


@pytest.fixture
def layout(system_root):
    return system_layout(system_root)


@pytest.fixture
def work(tmp_path) -> Path:
    """The caller's own files (downloads, inspector work dir)."""
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def secret(tmp_path) -> Path:
    """Stands in for /etc/shadow: must never be read into, written or deleted."""
    s = tmp_path / "outside" / "shadow"
    s.parent.mkdir()
    s.write_text("root:$6$secret\n")
    return s


@pytest.fixture
def t3(work) -> tuple[Path, Path]:
    src = write_appimage(work / T3_FILE)
    icon = work / "t3code.png"
    icon.write_bytes(make_png(512, 512))
    return src, icon


# ------------------------------------------------------------------------------------------------
# install: happy paths
# ------------------------------------------------------------------------------------------------


def test_install_creates_files_modes_desktop_and_registry(layout, t3):
    src, icon = t3
    result = install(layout, make_manifest(src, icon))
    assert result["ok"] is True and result["action"] == "install" and result["warnings"] == []

    target = layout.apps_dir / T3_TARGET
    desktop = layout.desktop_dir / "easyinstaller-t3code.desktop"
    icon_path = layout.icons_dir / "512x512" / "apps" / "easyinstaller-t3code.png"
    assert target.read_bytes() == src.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert stat.S_IMODE(desktop.stat().st_mode) == 0o644
    assert stat.S_IMODE(icon_path.stat().st_mode) == 0o644
    assert icon_path.read_bytes() == icon.read_bytes()
    assert src.is_file()  # the helper never deletes the source (the client does)

    entry = read_desktop(desktop)
    assert entry.get("Name") == "T3 Code (Alpha)"
    assert exec_program(entry.get("Exec")) == str(target)
    assert entry.get("Exec").endswith("%U")
    assert entry.get("TryExec") == str(target)
    assert entry.get("Icon") == "easyinstaller-t3code"
    assert entry.get("StartupWMClass") == "t3code"
    assert entry.get("X-EasyInstaller-Id") == "t3code"
    assert entry.get("X-EasyInstaller-Scope") == "system"
    assert entry.get("X-AppImage-Version") == "0.0.42"

    app = InstalledApp.from_dict(result["app"])
    assert Registry(layout.registry_path).get("t3code") == app
    assert app.scope is Scope.SYSTEM
    assert app.appimage_path == str(target)
    assert app.desktop_path == str(desktop)
    assert app.icon_paths == [str(icon_path)]
    assert app.icon_name == "easyinstaller-t3code"
    assert app.sha256 == sha256(src) and app.size == src.stat().st_size
    assert app.sandbox_fix == "none" and app.apparmor_profile is None
    assert app.original_filename == T3_FILE and app.arch == "x86_64"
    assert app.installed_at and app.updated_at
    assert stat.S_IMODE(layout.registry_path.stat().st_mode) == 0o644
    for directory in (layout.apps_dir, layout.desktop_dir, icon_path.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert leftovers(layout) == []


def test_install_without_icon_uses_generic_icon(layout, t3):
    src, _icon = t3
    result = install(layout, make_manifest(src, None))
    app = result["app"]
    assert app["icon_paths"] == [] and app["icon_name"] == "application-x-executable"
    assert read_desktop(app["desktop_path"]).get("Icon") == "application-x-executable"
    assert not layout.icons_dir.exists()


@pytest.mark.parametrize("data, subdir, ext", [
    (make_svg(64, 64).encode(), "scalable", "svg"),
    (make_xpm(16, 16).encode(), "16x16", "xpm"),
    (make_png(100, 80), "64x64", "png"),
])
def test_icon_destination_is_computed_from_the_image(layout, work, t3, data, subdir, ext):
    src, _ = t3
    icon = work / "icon.bin"
    icon.write_bytes(data)
    app = install(layout, make_manifest(src, icon))["app"]
    assert app["icon_paths"] == [str(layout.icons_dir / subdir / "apps" / f"easyinstaller-t3code.{ext}")]


def test_install_without_embedded_desktop_uses_name(layout, t3):
    src, icon = t3
    name = 'Evil" ; $(rm -rf ~) \\n `x` %U'
    app = install(layout, make_manifest(src, icon, embedded_desktop=None,
                                        embedded_desktop_filename=None, name=name))["app"]
    text = Path(app["desktop_path"]).read_text()
    entry = DesktopEntry.parse(text)
    assert entry.get("Name") == name
    assert entry.get("Exec") == app["appimage_path"]
    assert entry.get("Comment") == "T3 Code desktop build"
    assert Path(app["appimage_path"]).name == "Evil-rm-rf-n-x-U.AppImage"
    assert len([line for line in text.splitlines() if line.startswith("Name")]) == 1


@pytest.mark.parametrize("name, stem", [
    ("../../../etc/cron.d/evil", "etc-cron.d-evil"),
    ("/etc/passwd", "etc-passwd"),
    ("..", "App"),
    (".hidden", "hidden"),
    ("x" * 200, "x" * 64),
])
def test_name_cannot_steer_the_destination(layout, system_root, t3, name, stem):
    src, icon = t3
    app = install(layout, make_manifest(src, icon, name=name))["app"]
    assert app["appimage_path"] == str(layout.apps_dir / f"{stem}.AppImage")
    assert not (system_root / "etc").exists()


def test_longest_app_id_fits_every_file_name(layout, work):
    app_id = "a" * 128
    layout.apps_dir.mkdir(parents=True)
    (layout.apps_dir / f"{'x' * 64}.AppImage").write_bytes(b"taken")
    src = write_appimage(work / "x.AppImage")
    icon = work / "x.png"
    icon.write_bytes(make_png(48, 48))
    app = install(layout, make_manifest(src, icon, app_id=app_id, name="x" * 200))["app"]
    assert Path(app["appimage_path"]).name == f"{'x' * 64}-{app_id}.AppImage"
    assert Path(app["desktop_path"]).name == f"easyinstaller-{app_id}.desktop"
    assert ops.op_uninstall({"app_id": app_id}, layout=layout, run_commands=False)["ok"]


def test_multiline_comment_is_escaped(layout, t3):
    src, icon = t3
    app = install(layout, make_manifest(src, icon, embedded_desktop=None,
                                        comment="line one\nExec=/bin/evil"))["app"]
    text = Path(app["desktop_path"]).read_text()
    assert "\nExec=/bin/evil" not in text
    assert read_desktop(app["desktop_path"]).get("Comment") == "line one\nExec=/bin/evil"


def test_no_sandbox_extra_arg(layout, work):
    src = write_appimage(work / "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage")
    manifest = make_manifest(src, None, app_id="org.freecad.FreeCAD", name="FreeCAD",
                             embedded_desktop=FREECAD_DESKTOP,
                             embedded_desktop_filename="org.freecad.FreeCAD.desktop",
                             extra_args=["--no-sandbox", "--no-sandbox"])
    app = install(layout, manifest)["app"]
    entry = read_desktop(app["desktop_path"])
    assert entry.get("Exec") == f"{app['appimage_path']} --no-sandbox - --single-instance %F"
    assert entry.get("Comment[de]") == "Feature-basierter parametrischer Modellierer"
    assert app["sandbox_fix"] == "no-sandbox"


def test_embedded_extras_that_the_renderer_removes_do_not_fail_the_install(layout, t3):
    """What the per-user install accepts must install for everyone, too: localized Icon/Exec in
    X- groups, control characters and line separators in single lines (they are dropped)."""
    src, icon = t3
    desktop = (T3_DESKTOP + "Comment[fr]=Outil\u2028pratique\nComment[ru]=a\x85b\n"
               "Comment[de]=Mojibake \u00e2\x80\x99 Text\nComment[nl]=a\x0cb\nComment[it]=a\x01b\n"
               "Comment[es]=nul\x00\n# note\x7f\n"
               "\n[X-Foo Extra]\nName=Something\nIcon=foo\nIcon[de]=foo-de\nExec=x\nExec[de]=y\n")
    app = install(layout, make_manifest(src, icon, embedded_desktop=desktop))["app"]
    text = Path(app["desktop_path"]).read_text(encoding="utf-8")
    entry = read_desktop(app["desktop_path"])
    for key in ("Comment[fr]", "Comment[ru]", "Comment[de]", "Comment[nl]", "Comment[it]",
                "Comment[es]"):
        assert entry.get(key) is None, key
    assert "Icon[de]" not in text and "Exec[de]" not in text and "note" not in text
    assert entry.get("Name", "X-Foo Extra") == "Something"


def test_extract_and_run_prefix(layout, t3):
    src, icon = t3
    app = install(layout, make_manifest(src, icon, extract_and_run=True))["app"]
    exec_value = read_desktop(app["desktop_path"]).get("Exec")
    assert exec_value.startswith("env APPIMAGE_EXTRACT_AND_RUN=1 ")
    assert exec_program(exec_value) == app["appimage_path"]
    assert app["extract_and_run"] is True
    assert app["extract_and_run_explicit"] is False
    # chosen explicitly (`install --extract-and-run yes`): recorded, so it is never switched back
    app = install(layout, make_manifest(src, icon, extract_and_run=True,
                                        extract_and_run_explicit=True))["app"]
    assert app["extract_and_run"] is True and app["extract_and_run_explicit"] is True


def test_unknown_keys_are_ignored_and_destinations_are_our_own(layout, system_root, t3, secret):
    src, icon = t3
    manifest = make_manifest(src, icon, desktop_path=str(secret), target=str(secret),
                             appimage_path=str(secret), layout="/", apps_dir=str(secret.parent))
    install(layout, manifest)
    assert secret.read_text() == "root:$6$secret\n"
    assert entries(secret.parent) == ["shadow"]
    assert entries(system_root) == [
        "opt", "opt/appimages", f"opt/appimages/{T3_TARGET}", "usr", "usr/local",
        "usr/local/share", "usr/local/share/applications",
        "usr/local/share/applications/easyinstaller-t3code.desktop", "usr/local/share/icons",
        "usr/local/share/icons/hicolor", "usr/local/share/icons/hicolor/512x512",
        "usr/local/share/icons/hicolor/512x512/apps",
        "usr/local/share/icons/hicolor/512x512/apps/easyinstaller-t3code.png", "var", "var/lib",
        "var/lib/easy-installer", "var/lib/easy-installer/registry.json",
        "var/lib/easy-installer/registry.json.lock",
    ]


def test_desktop_injection_from_embedded_entry_is_neutralised(layout, t3):
    src, icon = t3
    embedded = (
        "[Desktop Entry]\n"
        "Name=Evil\n"
        "Exec=env LD_PRELOAD=/tmp/evil.so GIO_EXTRA_MODULES=/tmp/x QT_QPA_PLATFORM=xcb "
        "/usr/bin/evil --flag %U\n"
        "Exec[de]=/tmp/evil-de\n"
        "TryExec=/usr/bin/evil\n"
        "TryExec[de]=/tmp/evil\n"
        "Icon=/etc/shadow\n"
        "Icon[de]=/tmp/evil-icon.svg\n"
        "Type=Link\n"
        "URL=file:///etc/shadow\n"
        "DBusActivatable=true\n"
        "NoDisplay=true\n"
        "Actions=Shell;New;Help;\n"
        "X-EasyInstaller-Id=other\n"
        "X-EasyInstaller-Scope=user\n"
        "Bad Key=value\n"
        "junk line without equals\n"
        "Comment=A demo\rExec=/tmp/evil-cr\n"
        "\n"
        "[Desktop Action Shell]\n"
        "Name=Shell\n"
        "Exec=/bin/sh -c 'rm -rf ~'\n"
        "\n"
        "[Desktop Action New]\n"
        "Name=New Window\n"
        "Exec=env LD_PRELOAD=/tmp/evil.so AppRun --new-window\n"
        "Exec[de]=/tmp/evil-de\n"
        "\n"
        "[Desktop Action Help]\n"
        "Name=Online Help\n"
        "Exec=xdg-open https://example.org/help\n"
        "\n"
        "[Other Group]\n"
        "Exec=/bin/evil\n"
    )
    app = install(layout, make_manifest(src, icon, embedded_desktop=embedded))["app"]
    target = app["appimage_path"]
    text = Path(app["desktop_path"]).read_text(encoding="utf-8")
    entry = read_desktop(app["desktop_path"])
    assert exec_program(entry.get("Exec")) == target
    assert entry.get("Exec").startswith(f"env QT_QPA_PLATFORM=xcb {target} ")
    assert entry.get("TryExec") == target
    assert entry.get("Icon") == "easyinstaller-t3code"
    assert entry.get("Type") == "Application"
    assert entry.get("X-EasyInstaller-Id") == "t3code"
    assert entry.get("X-EasyInstaller-Scope") == "system"
    for key in ("URL", "DBusActivatable", "NoDisplay", "Bad Key", "Exec[de]", "TryExec[de]",
                "Icon[de]"):
        assert entry.get(key) is None
    assert entry.get("Comment") == "T3 Code desktop build"  # the manifest's, not "A demo\r..."
    for needle in ("/tmp/evil", "/tmp/x", "LD_PRELOAD", "GIO_EXTRA_MODULES", "\r", "xdg-open"):
        assert needle not in text
    assert entry.get_list("Actions") == ["New"]
    assert entry.groups() == ["Desktop Entry", "Desktop Action New"]
    assert entry.get("Exec", "Desktop Action New") == f"{target} --new-window"
    assert entry.keys("Desktop Action New") == ["Name", "Exec"]


@pytest.mark.parametrize("launcher", [
    "Exec=env LD_PRELOAD=/tmp/evil.so {target} %U\n",
    "Exec=env GDK_BACKEND=/tmp/x {target} %U\n",
    "Exec={target} %U\nExec[de]=/tmp/evil\n",
    "Exec={target} %U\nIcon[de]=/tmp/evil.svg\n",
    "Exec={target} %U\nComment=A\rExec=/tmp/evil\n",
    "Exec={target} %U\nComment=A\u2028B\n",
    "Exec={target} %U\nComment=A\x01B\n",
    "Exec={target} %U\n\n[X-A\x85B]\nName=A\n",
    "Exec={target} %U\nActions=A;\n\n[Desktop Action A]\nName=A\n"
    "Exec=env PYTHONPATH=/tmp {target}\n",
    "Exec={target} %U\nActions=A;\n\n[Desktop Action A]\nName=A\nTryExec[de]=/tmp/x\n"
    "Exec={target}\n",
])
def test_verify_desktop_rejects_other_code_paths(launcher, tmp_path):
    target = tmp_path / "Demo.AppImage"
    text = (f"[Desktop Entry]\nType=Application\nName=Demo\nTryExec={target}\n"
            f"X-EasyInstaller-Id=demo\n" + launcher.format(target=target))
    with pytest.raises(HelperError):
        ops.verify_desktop(text, app_id="demo", target=target, uninstall_command=None)
    ok = (f"[Desktop Entry]\nType=Application\nName=Demo\nTryExec={target}\n"
          f"X-EasyInstaller-Id=demo\nExec=env APPIMAGE_EXTRACT_AND_RUN=1 {target} %U\n")
    ops.verify_desktop(ok, app_id="demo", target=target, uninstall_command=None)


# ------------------------------------------------------------------------------------------------
# install: updates, collisions, rollback
# ------------------------------------------------------------------------------------------------


def test_update_replaces_files_and_removes_old_ones(layout, work, t3):
    src, icon = t3
    first = install(layout, make_manifest(src, icon))["app"]
    other_src = write_appimage(work / "other.AppImage", b"other")
    install(layout, make_manifest(other_src, None, app_id="other", name="Other"))

    new_src = write_appimage(work / "T3-Code-0.0.43-x86_64.AppImage", b"new version" * 10)
    new_icon = work / "new.svg"
    new_icon.write_text(make_svg(64, 64))
    result = install(layout, make_manifest(new_src, new_icon, version="0.0.43", name="T3 Code"))
    app = result["app"]
    assert result["action"] == "update"
    assert app["appimage_path"] == str(layout.apps_dir / "T3-Code.AppImage")
    assert Path(app["appimage_path"]).read_bytes() == new_src.read_bytes()
    assert not Path(first["appimage_path"]).exists()
    assert not Path(first["icon_paths"][0]).exists()
    assert Path(app["icon_paths"][0]).is_file()
    assert app["installed_at"] == first["installed_at"]
    apps = Registry(layout.registry_path).load()
    assert set(apps) == {"t3code", "other"}
    assert apps["t3code"].version == "0.0.43"
    assert Path(apps["other"].appimage_path).is_file()
    assert leftovers(layout) == []


def test_mime_package_is_installed_validated_and_removed(layout, t3, monkeypatch, tmp_path):
    from easy_installer.core import integration

    monkeypatch.setattr(integration, "HOST_MIME_DIR", tmp_path / "empty-host-mime")
    src, icon = t3
    fcstd = {"type": "application/x-extension-fcstd", "comment": "FreeCAD <files>",
             "globs": ["*.fcstd"], "sub_class_of": []}
    unlisted = {"type": "application/x-unlisted", "comment": None, "globs": ["*.unl"],
                "sub_class_of": []}
    desktop = T3_DESKTOP.replace("MimeType=", "MimeType=application/x-extension-fcstd;")
    app = install(layout, make_manifest(src, icon, embedded_desktop=desktop,
                                        mime_types=[fcstd, unlisted]))["app"]
    package = layout.mime_dir / "packages" / "easyinstaller-t3code.xml"
    assert app["mime_package"] == str(package)
    text = package.read_text()
    assert "application/x-extension-fcstd" in text and "FreeCAD &lt;files&gt;" in text
    assert "x-unlisted" not in text  # not opened by the launcher: not registered
    assert stat.S_IMODE(package.stat().st_mode) == 0o644

    for bad in ([{"type": "a/b", "globs": ["*"]}], [{"type": "../x", "globs": ["*.x"]}],
                [{"type": "a/b", "globs": ["*.x"], "comment": "x\ny"}], "nope",
                [fcstd] * 17,
                # update-mime-database (as root) writes <mime_dir>/<media>/<subtype>.xml
                [{"type": "packages/easyinstaller-victim", "globs": ["*.zzqq"]}],
                [{"type": "mime.cache/x", "globs": ["*.zzqq"]}],
                [{"type": "application/x-a", "globs": ["*.zzqq"], "sub_class_of": ["inode/x"]}]):
        with pytest.raises(RequestError):
            install(layout, make_manifest(src, icon, embedded_desktop=desktop, mime_types=bad))

    # an update without file types removes the package; so does uninstalling
    install(layout, make_manifest(src, icon, embedded_desktop=desktop, mime_types=[fcstd]))
    result = install(layout, make_manifest(src, icon, version="0.0.43", mime_types=[]))
    assert result["app"]["mime_package"] is None and not package.exists()
    install(layout, make_manifest(src, icon, embedded_desktop=desktop, mime_types=[fcstd]))
    assert package.is_file()
    removed = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)["removed"]
    assert str(package) in removed and not package.exists()
    assert ops.recorded_path_allowed(layout, str(package), "mime", "t3code")
    assert not ops.recorded_path_allowed(layout, str(layout.mime_dir / "packages" / "x.xml"),
                                         "mime", "t3code")


def test_blocked_registry_lock_changes_nothing(layout, t3, monkeypatch):
    """Any local user could flock() a readable lock file: the helper must not hang half-done."""
    import fcntl

    src, icon = t3
    install(layout, make_manifest(src, icon))  # creates the registry (and its lock file)
    lock = Path(str(layout.registry_path) + ".lock")
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    before = entries(layout.apps_dir)
    monkeypatch.setattr(ops, "REGISTRY_LOCK_TIMEOUT", 0.3)
    fd = os.open(lock, os.O_RDONLY)  # as root could; other users cannot open a 0600 file
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(InstallError, match="Another installation is running"):
            install(layout, make_manifest(src, icon, version="0.0.43"))
        with pytest.raises(InstallError, match="Another installation is running"):
            ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    finally:
        os.close(fd)
    assert entries(layout.apps_dir) == before and leftovers(layout) == []
    assert Registry(layout.registry_path).get("t3code").version == "0.0.42"


def test_reinstall_same_paths_leaves_no_backups(layout, t3):
    src, icon = t3
    first = install(layout, make_manifest(src, icon))["app"]
    second = install(layout, make_manifest(src, icon))
    assert second["action"] == "update"
    assert second["app"]["appimage_path"] == first["appimage_path"]
    assert sorted(os.listdir(layout.apps_dir)) == [T3_TARGET]
    assert leftovers(layout) == []


def test_reinstall_from_the_installed_file_itself(layout, t3):
    src, icon = t3
    app = install(layout, make_manifest(src, icon))["app"]
    installed = Path(app["appimage_path"])
    again = install(layout, make_manifest(installed, icon))["app"]
    assert again["appimage_path"] == str(installed)
    assert installed.read_bytes() == src.read_bytes()
    assert sorted(os.listdir(layout.apps_dir)) == [T3_TARGET]


def test_foreign_file_at_target_is_not_overwritten(layout, t3):
    src, icon = t3
    layout.apps_dir.mkdir(parents=True)
    foreign = layout.apps_dir / T3_TARGET
    foreign.write_bytes(b"someone else's app")
    (layout.apps_dir / "T3-Code-Alpha-t3code.AppImage").write_bytes(b"also taken")
    app = install(layout, make_manifest(src, icon))["app"]
    assert app["appimage_path"] == str(layout.apps_dir / "T3-Code-Alpha-t3code-2.AppImage")
    assert foreign.read_bytes() == b"someone else's app"


def test_rollback_when_registry_write_fails(layout, t3, monkeypatch):
    src, icon = t3

    def broken_put(self, app):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Registry, "put", broken_put)
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src, icon))
    assert "disk space" in excinfo.value.message
    assert list(layout.apps_dir.iterdir()) == []
    assert list(layout.desktop_dir.iterdir()) == []
    assert list((layout.icons_dir / "512x512" / "apps").iterdir()) == []


def test_rollback_on_failed_update_restores_previous_version(layout, work, t3, monkeypatch):
    src, icon = t3
    first = install(layout, make_manifest(src, icon))["app"]
    old_desktop = Path(first["desktop_path"]).read_text()
    new_src = write_appimage(work / "new.AppImage", b"new")

    def broken_put(self, app):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(Registry, "put", broken_put)
    with pytest.raises(RuntimeError):
        install(layout, make_manifest(new_src, icon, version="0.0.43",
                                      extra_args=["--no-sandbox"]))
    assert Path(first["appimage_path"]).read_bytes() == src.read_bytes()
    assert Path(first["desktop_path"]).read_text() == old_desktop
    assert Path(first["icon_paths"][0]).is_file()
    assert leftovers(layout) == []


def test_directory_in_the_way_rolls_back(layout, t3):
    src, icon = t3
    (layout.desktop_dir / "easyinstaller-t3code.desktop").mkdir(parents=True)
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon))
    assert list(layout.apps_dir.iterdir()) == []
    assert list((layout.icons_dir / "512x512" / "apps").iterdir()) == []
    assert not layout.registry_path.exists()


def test_not_enough_disk_space(layout, t3, monkeypatch):
    src, icon = t3
    monkeypatch.setattr(SafeDir, "free_bytes", lambda self: 1024)
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src, icon))
    assert "disk space" in excinfo.value.message
    assert list(layout.apps_dir.iterdir()) == []


# ------------------------------------------------------------------------------------------------
# install: source validation
# ------------------------------------------------------------------------------------------------


def test_sha256_mismatch_aborts_and_deletes_partial_file(layout, t3):
    src, icon = t3
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src, icon, sha256="0" * 64))
    assert "changed" in excinfo.value.message
    assert list(layout.apps_dir.iterdir()) == []
    assert not (layout.desktop_dir / "easyinstaller-t3code.desktop").exists()
    assert Registry(layout.registry_path).load() == {}


def test_size_mismatch_is_rejected(layout, t3):
    src, icon = t3
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon, size=src.stat().st_size + 1))
    assert not layout.apps_dir.exists() or list(layout.apps_dir.iterdir()) == []


@pytest.mark.parametrize("content", [b"", b"#!/bin/sh\necho hi\n", b"\x7fEL"])
def test_source_must_be_elf(layout, work, content):
    src = work / "fake.AppImage"
    src.write_bytes(content)
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src))
    assert "not an AppImage" in excinfo.value.message


def test_source_symlink_is_refused(layout, t3, work):
    src, icon = t3
    link = work / "link.AppImage"
    link.symlink_to(src)
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(link, icon, sha256=sha256(src)))
    assert "link" in excinfo.value.message
    assert not layout.apps_dir.exists()


def test_symlink_to_root_only_file_is_refused(layout, t3, work):
    src, icon = t3
    link = work / "shadow.AppImage"
    link.symlink_to("/etc/shadow")
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon, source_appimage=str(link)))
    icon_link = work / "shadow.png"
    icon_link.symlink_to("/etc/shadow")
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon_link))


@not_root
def test_unreadable_source_is_refused(layout, t3):
    src, icon = t3
    digest = sha256(src)
    src.chmod(0o000)
    try:
        with pytest.raises(InstallError) as excinfo:
            install(layout, make_manifest(src, icon, sha256=digest, size=src.stat().st_size))
        assert "not allowed to read" in excinfo.value.message
    finally:
        src.chmod(0o644)


@not_root
def test_hard_link_does_not_bypass_read_permission(layout, t3, work):
    src, icon = t3
    digest = sha256(src)
    hidden = work / "private" / "secret.AppImage"
    hidden.parent.mkdir()
    os.link(src, hidden)          # same inode as src
    src.chmod(0o000)              # ... which the caller can no longer read
    try:
        with pytest.raises(InstallError):
            install(layout, make_manifest(hidden, icon, sha256=digest))
    finally:
        src.chmod(0o644)


@pytest.mark.parametrize("path", ["/dev/null", "/dev/zero", "/proc/self/environ", "/proc/self/mem",
                                  "/proc/self/status"])
def test_devices_and_proc_files_are_refused(layout, t3, path):
    src, icon = t3
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon, source_appimage=path))
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, Path(path)))
    assert not layout.apps_dir.exists() or list(layout.apps_dir.iterdir()) == []


def test_fifo_and_directory_sources_are_refused(layout, t3, work):
    src, icon = t3
    fifo = work / "fifo.AppImage"
    os.mkfifo(fifo)
    with pytest.raises(InstallError) as excinfo:  # must not block waiting for a writer
        install(layout, make_manifest(src, icon, source_appimage=str(fifo)))
    assert "not a normal file" in excinfo.value.message
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, fifo))
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, icon, source_appimage=str(work)))


def test_icon_validation(layout, t3, work):
    src, _ = t3
    big = work / "big.png"
    big.write_bytes(make_png(16, 16) + b"\0" * (ops.MAX_ICON_SIZE + 1))
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src, big))
    assert "too large" in excinfo.value.message
    junk = work / "junk.png"
    junk.write_bytes(b"this is not an image")
    with pytest.raises(InstallError) as excinfo:
        install(layout, make_manifest(src, junk))
    assert "icon" in excinfo.value.message
    empty = work / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(InstallError):
        install(layout, make_manifest(src, empty))
    assert not layout.apps_dir.exists() or list(layout.apps_dir.iterdir()) == []


@not_root
def test_unreadable_icon_is_refused(layout, t3):
    src, icon = t3
    icon.chmod(0o000)
    try:
        with pytest.raises(InstallError):
            install(layout, make_manifest(src, icon))
    finally:
        icon.chmod(0o644)


# ------------------------------------------------------------------------------------------------
# install: request validation
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("app_id", [
    "../evil", "a/b", "", ".hidden", "-x", "x\n", "x y", 'x"', "x}", "x" * 129, "ä", 5, None, ["x"],
])
def test_invalid_app_ids(layout, t3, app_id):
    src, icon = t3
    with pytest.raises(RequestError):
        install(layout, make_manifest(src, icon, app_id=app_id))
    assert not layout.apps_dir.exists()


@pytest.mark.parametrize("name", [
    "", "   ", "x" * 201, "a\nExec=/bin/evil", "a\rb", "a\x00b", "a\u2028b", "a\x85b", 42, None,
])
def test_invalid_names(layout, t3, name):
    src, icon = t3
    with pytest.raises(RequestError):
        install(layout, make_manifest(src, icon, name=name))


@pytest.mark.parametrize("key, value", [
    ("source_appimage", 123),
    ("source_appimage", None),
    ("source_appimage", "relative/file.AppImage"),
    ("source_appimage", "/tmp/../etc/passwd"),
    ("source_appimage", "//etc/passwd"),
    ("source_appimage", "/tmp/a\nb"),
    ("source_appimage", "/" + "a" * 5000),
    ("icon_source", {"path": "/x"}),
    ("icon_source", "icon.png"),
    ("sha256", None),
    ("sha256", "abc"),
    ("sha256", "g" * 64),
    ("extra_args", "--no-sandbox"),
    ("extra_args", ["--no-sandbox", "--evil"]),
    ("extra_args", ["--no-sandbox"] * 9),
    ("extra_args", [None]),
    ("extract_and_run", "yes"),
    ("extract_and_run", 1),
    ("extract_and_run_explicit", "yes"),
    ("apparmor", "true"),
    ("is_electron", 0),
    ("size", "5"),
    ("size", True),
    ("size", -1),
    ("size", 1.5),
    ("version", 1.0),
    ("version", "1.0\n[Desktop Entry]"),
    ("version", "x" * 200),
    ("comment", "a\x00b"),
    ("comment", "x" * 3000),
    ("embedded_desktop", ["[Desktop Entry]"]),
    ("embedded_desktop", "[Desktop Entry]\nName=a\ud800\n"),
    ("embedded_desktop", "x" * (ops.MAX_DESKTOP_TEXT + 1)),
    ("embedded_desktop_filename", "../t3code.desktop"),
    ("embedded_desktop_filename", 7),
    ("original_filename", "a/b.AppImage"),
    ("arch", "x86_64; rm -rf /"),
    ("arch", 64),
    ("update_info", "zsync|\nevil"),
])
def test_invalid_request_values(layout, t3, key, value):
    src, icon = t3
    with pytest.raises(RequestError) as excinfo:
        install(layout, make_manifest(src, icon, **{key: value}))
    assert key in (excinfo.value.details or "") or key == "source_appimage"
    assert not layout.apps_dir.exists()


@pytest.mark.parametrize("manifest", [None, [], "install", 42])
def test_request_must_be_an_object(layout, manifest):
    with pytest.raises(RequestError):
        ops.op_install(manifest, layout=layout, run_commands=False, **ME)
    with pytest.raises(RequestError):
        ops.op_uninstall(manifest, layout=layout, run_commands=False)
    with pytest.raises(RequestError):
        ops.op_apparmor_remove(manifest, layout=layout, run_commands=False)
    with pytest.raises(RequestError):
        ops.op_install_fuse(manifest, status=make_status(), run_commands=False)


# ------------------------------------------------------------------------------------------------
# destinations: symlinks, hard links, unsafe folders
# ------------------------------------------------------------------------------------------------


def test_planted_symlink_at_desktop_destination_is_replaced_not_followed(layout, t3, secret):
    src, icon = t3
    layout.desktop_dir.mkdir(parents=True)
    planted = layout.desktop_dir / "easyinstaller-t3code.desktop"
    planted.symlink_to(secret)
    install(layout, make_manifest(src, icon))
    assert secret.read_text() == "root:$6$secret\n"
    assert not planted.is_symlink() and planted.is_file()
    assert read_desktop(planted).get("X-EasyInstaller-Id") == "t3code"


def test_planted_hard_link_at_destination_is_replaced_not_written(layout, t3, secret):
    src, icon = t3
    icon_dir = layout.icons_dir / "512x512" / "apps"
    icon_dir.mkdir(parents=True)
    os.link(secret, icon_dir / "easyinstaller-t3code.png")
    install(layout, make_manifest(src, icon))
    assert secret.read_text() == "root:$6$secret\n"
    assert secret.stat().st_nlink == 1
    assert (icon_dir / "easyinstaller-t3code.png").read_bytes() == icon.read_bytes()


def test_planted_symlink_at_appimage_destination(layout, t3, secret):
    src, icon = t3
    layout.apps_dir.mkdir(parents=True)
    planted = layout.apps_dir / T3_TARGET
    planted.symlink_to(secret)
    app = install(layout, make_manifest(src, icon))["app"]
    assert app["appimage_path"] == str(layout.apps_dir / "T3-Code-Alpha-t3code.AppImage")
    assert planted.is_symlink()
    assert secret.read_text() == "root:$6$secret\n"


@pytest.mark.parametrize("attr", ["desktop_dir", "apps_dir", "registry_path"])
def test_symlinked_destination_folder_is_refused(layout, t3, tmp_path, attr):
    src, icon = t3
    evil = tmp_path / "evil"
    evil.mkdir()
    directory = getattr(layout, attr)
    if attr == "registry_path":
        directory = directory.parent
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(evil, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        install(layout, make_manifest(src, icon))
    assert list(evil.iterdir()) == []


def test_symlinked_ancestor_is_refused(layout, system_root, t3, tmp_path):
    src, icon = t3
    evil = tmp_path / "evil-usr"
    evil.mkdir()
    (system_root / "usr").symlink_to(evil, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        install(layout, make_manifest(src, icon))
    assert entries(evil) == []


def test_symlinked_icon_size_folder_is_refused(layout, t3, tmp_path):
    src, icon = t3
    evil = tmp_path / "evil-icons"
    evil.mkdir()
    (layout.icons_dir / "512x512").mkdir(parents=True)
    (layout.icons_dir / "512x512" / "apps").symlink_to(evil, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        install(layout, make_manifest(src, icon))
    assert entries(evil) == []


def test_world_writable_folder_is_refused_unless_sticky(layout, t3):
    src, icon = t3
    layout.desktop_dir.mkdir(parents=True)
    layout.desktop_dir.chmod(0o777)
    with pytest.raises(UnsafePathError):
        install(layout, make_manifest(src, icon))
    layout.desktop_dir.chmod(0o1777)
    install(layout, make_manifest(src, icon))
    layout.desktop_dir.chmod(0o755)


def test_registry_file_symlink_is_refused(layout, t3, secret):
    src, icon = t3
    layout.registry_path.parent.mkdir(parents=True)
    layout.registry_path.symlink_to(secret)
    with pytest.raises(UnsafePathError):
        install(layout, make_manifest(src, icon))
    assert secret.read_text() == "root:$6$secret\n"


def test_safedir_rejects_relative_and_unnormalised_paths():
    for bad in ("relative", "/a/../b", "//a", "/a/./b", "/a\nb"):
        with pytest.raises(UnsafePathError):
            SafeDir.open(bad)


def test_safedir_file_names_cannot_escape(layout):
    with SafeDir.open(layout.apps_dir, create=True) as directory:
        for bad in ("../x", "a/b", "..", ".", ""):
            with pytest.raises(UnsafePathError):
                directory.write_file(bad, b"x", 0o644)


# ------------------------------------------------------------------------------------------------
# tampered registry
# ------------------------------------------------------------------------------------------------


def _tampered_app(layout, secret: Path, **overrides) -> InstalledApp:
    values = dict(
        id="t3code", name="T3", version="0.0.1", scope=Scope.SYSTEM,
        appimage_path=str(secret), desktop_path=str(secret.with_name("desktop")),
        icon_paths=[str(secret.with_name("icon.png"))],
        apparmor_profile=str(secret.with_name("profile")),
    )
    values.update(overrides)
    return InstalledApp(**values)


def test_update_with_tampered_registry_deletes_nothing_outside(layout, t3, secret):
    src, icon = t3
    for name in ("desktop", "icon.png", "profile"):
        secret.with_name(name).write_text("keep me")
    layout.registry_path.parent.mkdir(parents=True)
    Registry(layout.registry_path).put(_tampered_app(layout, secret))
    install(layout, make_manifest(src, icon))
    assert secret.read_text() == "root:$6$secret\n"
    assert entries(secret.parent) == ["desktop", "icon.png", "profile", "shadow"]


def test_uninstall_with_tampered_registry(layout, t3, secret):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    other = install(layout, make_manifest(src, None, app_id="other", name="Other"))["app"]
    decoys = [
        layout.desktop_dir / "other-app.desktop",
        layout.icons_dir / "48x48" / "apps" / "easyinstaller-other.png",
        layout.icons_dir / "48x48" / "easyinstaller-t3code.png",
        layout.apparmor_dir / "some-profile",
        layout.apps_dir / "sub" / "x.AppImage",
    ]
    for decoy in decoys:
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text("keep me")
    Registry(layout.registry_path).put(_tampered_app(
        layout, secret,
        appimage_path=str(layout.apps_dir / ".." / "shadow"),
        desktop_path=str(decoys[0]),
        icon_paths=[str(decoys[1]), str(decoys[2]), str(layout.icons_dir / "../../x.png"),
                    str(secret), "relative.png", ""],
        apparmor_profile=str(decoys[3]),
    ))
    Registry(layout.registry_path).put(_tampered_app(
        layout, secret, id="t3code-2", appimage_path=str(decoys[4]), icon_paths=[],
        desktop_path=other["desktop_path"], apparmor_profile=None))
    result = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert result["removed"] == []
    assert len(result["skipped"]) >= 6
    ops.op_uninstall({"app_id": "t3code-2"}, layout=layout, run_commands=False)
    for decoy in decoys:
        assert decoy.read_text() == "keep me"
    assert secret.read_text() == "root:$6$secret\n"
    assert Path(other["desktop_path"]).is_file()
    assert set(Registry(layout.registry_path).load()) == {"other"}


def test_uninstall_does_not_follow_symlinked_parent(layout, t3, tmp_path):
    src, icon = t3
    app = install(layout, make_manifest(src, icon))["app"]
    icon_dir = Path(app["icon_paths"][0]).parent
    outside = tmp_path / "outside-icons"
    outside.mkdir()
    (outside / "easyinstaller-t3code.png").write_text("keep me")
    for child in icon_dir.iterdir():
        child.unlink()
    icon_dir.rmdir()
    icon_dir.symlink_to(outside, target_is_directory=True)
    result = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert app["icon_paths"][0] in result["skipped"]
    assert (outside / "easyinstaller-t3code.png").read_text() == "keep me"
    assert not Path(app["appimage_path"]).exists()


# ------------------------------------------------------------------------------------------------
# uninstall
# ------------------------------------------------------------------------------------------------


def test_uninstall_removes_files_and_entry(layout, t3, commands):
    src, icon = t3
    app = install(layout, make_manifest(src, icon))["app"]
    result = ops.op_uninstall({"app_id": "t3code", "extra": "ignored"}, layout=layout,
                              run_commands=True)
    assert sorted(result["removed"]) == sorted(
        [app["appimage_path"], app["desktop_path"], *app["icon_paths"]])
    for path in result["removed"]:
        assert not os.path.lexists(path)
    assert Registry(layout.registry_path).load() == {}
    assert commands.commands() == [
        ["/usr/bin/update-desktop-database", str(layout.desktop_dir)],
        ["/usr/bin/gtk-update-icon-cache", "-f", "-t", str(layout.icons_dir)],
    ]
    assert src.is_file()


def test_uninstall_with_missing_files(layout, t3):
    src, icon = t3
    app = install(layout, make_manifest(src, icon))["app"]
    os.unlink(app["appimage_path"])
    os.unlink(app["desktop_path"])
    result = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert result["removed"] == app["icon_paths"]
    assert Registry(layout.registry_path).get("t3code") is None


def test_uninstall_unknown_id(layout, t3):
    with pytest.raises(NotInstalledError):
        ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert not layout.registry_path.parent.exists()  # nothing is created for a failed uninstall
    src, icon = t3
    install(layout, make_manifest(src, icon))
    with pytest.raises(NotInstalledError):
        ops.op_uninstall({"app_id": "t3code-other"}, layout=layout, run_commands=False)


@pytest.mark.parametrize("app_id", ["../t3code", "t3code\n", "", None, 1])
def test_uninstall_invalid_id(layout, app_id):
    with pytest.raises(RequestError):
        ops.op_uninstall({"app_id": app_id}, layout=layout, run_commands=False)


def test_cache_tool_failures_are_only_logged(layout, t3, commands):
    commands.fail = {("update-desktop-database", str(layout.desktop_dir)),
                     ("gtk-update-icon-cache", "-f")}
    src, icon = t3
    assert install(layout, make_manifest(src, icon), run_commands=True)["ok"] is True
    assert len(commands.calls) == 2
    assert all(env == {} for _cmd, env in commands.calls)


# ------------------------------------------------------------------------------------------------
# uninstall launcher
# ------------------------------------------------------------------------------------------------


def test_trusted_executable_checks(tmp_path):
    assert ops.is_trusted_executable("/usr/bin/env", name="env") is True
    assert ops.is_trusted_executable("/usr/bin/env", name="easy-installer") is False
    assert ops.is_trusted_executable("usr/bin/env", name="env") is False
    assert ops.is_trusted_executable("/usr/bin/../bin/env", name="env") is False
    assert ops.is_trusted_executable("/usr/bin/does-not-exist", name="does-not-exist") is False
    mine = tmp_path / "easy-installer"
    mine.write_text("#!/bin/sh\n")
    mine.chmod(0o755)
    assert ops.is_trusted_executable(str(mine), name="easy-installer") is False  # not root's
    link = tmp_path / "env"
    link.symlink_to("/usr/bin/env")
    assert ops.is_trusted_executable(str(link), name="env") is False


@pytest.mark.parametrize("command", [
    ["/usr/bin/env", "--uninstall", "other"],
    ["/usr/bin/env", "--remove", "t3code"],
    ["/usr/bin/env", "--uninstall", "t3code", "extra"],
    ["/tmp/easy-installer", "--uninstall", "t3code"],
    ["env", "--uninstall", "t3code"],
    "/usr/bin/env --uninstall t3code",
    [1, 2, 3],
])
def test_untrusted_uninstall_command_is_dropped(layout, t3, monkeypatch, command):
    monkeypatch.setattr(ops, "LAUNCHER_NAME", "env")
    src, icon = t3
    app = install(layout, make_manifest(src, icon, uninstall_command=command))["app"]
    entry = read_desktop(app["desktop_path"])
    assert "easyinstaller-uninstall" not in entry.get_list("Actions")
    assert not entry.has_group("Desktop Action easyinstaller-uninstall")


def test_trusted_uninstall_command_is_used(layout, t3, monkeypatch):
    monkeypatch.setattr(ops, "LAUNCHER_NAME", "env")  # a root-owned stand-in for easy-installer
    src, icon = t3
    app = install(layout, make_manifest(src, icon,
                                        uninstall_command=["/usr/bin/env", "--uninstall", "t3code"]))
    entry = read_desktop(app["app"]["desktop_path"])
    assert "easyinstaller-uninstall" in entry.get_list("Actions")
    group = "Desktop Action easyinstaller-uninstall"
    assert entry.get("Exec", group) == "/usr/bin/env --uninstall t3code"


# ------------------------------------------------------------------------------------------------
# AppArmor profile for system apps (op_install with apparmor=True)
# ------------------------------------------------------------------------------------------------


def electron_manifest(work: Path, **overrides) -> dict:
    src = write_appimage(work / "Anytype-0.55.4.AppImage")
    icon = work / "anytype.png"
    icon.write_bytes(make_png(256, 256))
    values = dict(app_id="anytype", name="Anytype", version="0.55.4", apparmor=True,
                  embedded_desktop=ANYTYPE_DESKTOP, embedded_desktop_filename="anytype.desktop")
    values.update(overrides)
    return make_manifest(src, icon, **values)


def test_install_with_apparmor_profile(layout, work, commands):
    result = install(layout, electron_manifest(work), run_commands=True)
    app = result["app"]
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    assert app["apparmor_profile"] == str(profile)
    assert app["sandbox_fix"] == "apparmor"
    text = profile.read_text()
    assert text == render_apparmor_profile("anytype", app["appimage_path"])
    assert stat.S_IMODE(profile.stat().st_mode) == 0o644
    assert ["/usr/bin/apparmor_parser", "-r", str(profile)] in commands.commands()
    assert "--no-sandbox" not in read_desktop(app["desktop_path"]).get("Exec")


def test_apparmor_failure_falls_back_to_no_sandbox(layout, work, commands):
    commands.fail = {("apparmor_parser", "-r")}
    result = install(layout, electron_manifest(work), run_commands=True)
    app = result["app"]
    assert app["sandbox_fix"] == "no-sandbox" and app["apparmor_profile"] is None
    assert not (layout.apparmor_dir / "easyinstaller-anytype").exists()
    assert "--no-sandbox" in read_desktop(app["desktop_path"]).get("Exec")
    assert result["warnings"]


def test_apparmor_parser_missing_falls_back(layout, work, commands):
    commands.missing = {"apparmor_parser"}
    app = install(layout, electron_manifest(work), run_commands=True)["app"]
    assert app["sandbox_fix"] == "no-sandbox"
    assert not (layout.apparmor_dir / "easyinstaller-anytype").exists()


def test_foreign_profile_is_never_overwritten(layout, work, secret):
    layout.apparmor_dir.mkdir(parents=True)
    foreign = layout.apparmor_dir / "easyinstaller-anytype"
    foreign.write_text("profile something /usr/bin/x {}\n")
    app = install(layout, electron_manifest(work))["app"]
    assert app["sandbox_fix"] == "no-sandbox"
    assert foreign.read_text() == "profile something /usr/bin/x {}\n"
    foreign.unlink()
    foreign.symlink_to(secret)
    app = install(layout, electron_manifest(work))["app"]
    assert app["sandbox_fix"] == "no-sandbox"
    assert foreign.is_symlink() and secret.read_text() == "root:$6$secret\n"


def test_profile_of_a_user_install_is_not_taken_over(layout, work):
    layout.apparmor_dir.mkdir(parents=True)
    user_profile = layout.apparmor_dir / "easyinstaller-anytype"
    text = render_apparmor_profile("anytype", "/home/alice/Applications/Anytype.AppImage")
    user_profile.write_text(text)
    app = install(layout, electron_manifest(work))["app"]
    assert app["sandbox_fix"] == "no-sandbox"
    assert user_profile.read_text() == text


def test_update_without_apparmor_removes_old_profile(layout, work, commands):
    install(layout, electron_manifest(work), run_commands=True)
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    assert profile.is_file()
    commands.calls.clear()
    app = install(layout, electron_manifest(work, apparmor=False, extra_args=["--no-sandbox"]),
                  run_commands=True)["app"]
    assert app["apparmor_profile"] is None
    assert not profile.exists()
    assert ["/usr/bin/apparmor_parser", "-R", str(profile)] in commands.commands()
    assert commands.existed[commands.commands().index(
        ["/usr/bin/apparmor_parser", "-R", str(profile)])] is True


def test_rollback_reverts_new_profile(layout, work, monkeypatch, commands):
    def broken_put(self, app):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Registry, "put", broken_put)
    with pytest.raises(InstallError):
        install(layout, electron_manifest(work), run_commands=True)
    assert not (layout.apparmor_dir / "easyinstaller-anytype").exists()
    assert commands.commands()[-1] == ["/usr/bin/apparmor_parser", "-R",
                                       str(layout.apparmor_dir / "easyinstaller-anytype")]


def test_uninstall_removes_system_profile(layout, work, commands):
    app = install(layout, electron_manifest(work), run_commands=True)["app"]
    result = ops.op_uninstall({"app_id": "anytype"}, layout=layout, run_commands=True)
    assert app["apparmor_profile"] in result["removed"]
    assert not Path(app["apparmor_profile"]).exists()


# ------------------------------------------------------------------------------------------------
# op_apparmor_install / op_apparmor_remove (user-scope apps)
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def home(isolated_env) -> Path:
    return isolated_env


def user_app(home: Path, name: str = "Anytype.AppImage") -> Path:
    return write_appimage(home / "Applications" / name)


def apparmor_install(layout, home, path, app_id="anytype", **kwargs):
    kwargs = {"caller_uid": os.getuid(), "caller_home": home, "run_commands": False, **kwargs}
    return ops.op_apparmor_install({"app_id": app_id, "appimage_path": str(path)},
                                   layout=layout, **kwargs)


def test_apparmor_install_for_user_app(layout, home, commands):
    path = user_app(home)
    result = apparmor_install(layout, home, path, run_commands=True)
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    assert result == {"ok": True, "profile_path": str(profile),
                      "profile_name": "easyinstaller-anytype", "appimage_path": str(path)}
    assert profile.read_text() == render_apparmor_profile("anytype", str(path))
    assert commands.commands() == [["/usr/bin/apparmor_parser", "-r", str(profile)]]
    assert commands.calls[0][1] == {}


def test_apparmor_install_special_characters_are_escaped(layout, home):
    path = user_app(home, 'we"ird {x}*[1]?^.AppImage')
    apparmor_install(layout, home, path)
    text = (layout.apparmor_dir / "easyinstaller-anytype").read_text()
    assert text == render_apparmor_profile("anytype", str(path))
    assert '\\"ird \\{x\\}\\*\\[1\\]\\?\\^.AppImage' in text
    assert ops.profile_attachment(text) == str(path)
    assert len(text.splitlines()) == 10


@pytest.mark.parametrize("app_id", ["x y", "x\n", 'x"', "x}", "../x", "x,userns", ""])
def test_apparmor_profile_name_injection(layout, home, app_id):
    path = user_app(home)
    with pytest.raises(RequestError):
        apparmor_install(layout, home, path, app_id=app_id)
    assert not layout.apparmor_dir.exists()


def test_apparmor_install_path_checks(layout, home, tmp_path, work):
    outside = write_appimage(work / "Outside.AppImage")
    with pytest.raises(InstallError):
        apparmor_install(layout, home, outside)
    with pytest.raises(InstallError):
        apparmor_install(layout, home, "/etc/passwd")
    with pytest.raises(RequestError):
        apparmor_install(layout, home, "relative/App.AppImage")
    with pytest.raises(RequestError):
        apparmor_install(layout, home, f"{home}/../work/Outside.AppImage")
    with pytest.raises(RequestError):
        apparmor_install(layout, home, f"{home}/Applications/a\nb.AppImage")
    link = home / "link.AppImage"
    link.symlink_to(outside)
    with pytest.raises(InstallError):
        apparmor_install(layout, home, link)
    (home / "linkdir").symlink_to(work, target_is_directory=True)
    with pytest.raises(InstallError):   # resolves to a path outside the home folder
        apparmor_install(layout, home, home / "linkdir" / "Outside.AppImage")
    with pytest.raises(InstallError):
        apparmor_install(layout, home, home)
    fifo = home / "fifo.AppImage"
    os.mkfifo(fifo)
    with pytest.raises(InstallError):
        apparmor_install(layout, home, fifo)
    script = home / "script.AppImage"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(InstallError):
        apparmor_install(layout, home, script)
    with pytest.raises(InstallError):   # home "/" would mean "anywhere"
        apparmor_install(layout, Path("/"), outside)
    assert not layout.apparmor_dir.exists()


def test_apparmor_install_requires_ownership(layout, home):
    path = user_app(home)
    with pytest.raises(InstallError) as excinfo:
        apparmor_install(layout, home, path, caller_uid=os.getuid() + 1, caller_gid=os.getgid())
    assert "belong" in excinfo.value.message


def test_apparmor_install_symlinked_home_uses_real_path(layout, tmp_path):
    real_home = tmp_path / "data" / "alice"
    path = user_app(real_home)
    (tmp_path / "homes").mkdir()
    linked_home = tmp_path / "homes" / "alice"
    linked_home.symlink_to(real_home, target_is_directory=True)
    result = apparmor_install(layout, linked_home, linked_home / "Applications" / path.name)
    assert result["appimage_path"] == str(path)
    assert ops.profile_attachment(Path(result["profile_path"]).read_text()) == str(path)


def test_apparmor_install_for_system_app_path(layout, t3):
    src, icon = t3
    app = install(layout, make_manifest(src, icon))["app"]
    result = apparmor_install(layout, Path("/nonexistent-home"), app["appimage_path"],
                              app_id="t3code")
    assert result["appimage_path"] == app["appimage_path"]


def test_apparmor_install_parser_failure_removes_profile(layout, home, commands):
    commands.fail = {("apparmor_parser", "-r")}
    path = user_app(home)
    with pytest.raises(InstallError) as excinfo:
        apparmor_install(layout, home, path, run_commands=True)
    assert "could not be set up" in excinfo.value.message
    assert "simulated failure" in excinfo.value.details
    assert not (layout.apparmor_dir / "easyinstaller-anytype").exists()


def test_apparmor_install_parser_failure_restores_previous_profile(layout, home, commands):
    path = user_app(home)
    apparmor_install(layout, home, path, run_commands=True)
    old = (layout.apparmor_dir / "easyinstaller-anytype").read_text()
    moved = user_app(home, "Anytype-new.AppImage")
    commands.fail = {("apparmor_parser", "-r")}
    with pytest.raises(InstallError):
        apparmor_install(layout, home, moved, run_commands=True)
    assert (layout.apparmor_dir / "easyinstaller-anytype").read_text() == old


def test_apparmor_install_without_apparmor_parser(layout, home, commands):
    commands.missing = {"apparmor_parser"}
    with pytest.raises(InstallError):
        apparmor_install(layout, home, user_app(home), run_commands=True)
    assert not (layout.apparmor_dir / "easyinstaller-anytype").exists()


def test_apparmor_install_same_user_can_replace_own_profile(layout, home):
    apparmor_install(layout, home, user_app(home))
    moved = user_app(home, "Anytype-2.AppImage")
    apparmor_install(layout, home, moved)
    text = (layout.apparmor_dir / "easyinstaller-anytype").read_text()
    assert ops.profile_attachment(text) == str(moved)


def test_apparmor_install_conflicts(layout, home, t3):
    layout.apparmor_dir.mkdir(parents=True)
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    others = render_apparmor_profile("anytype", "/home/bob/Applications/Anytype.AppImage")
    profile.write_text(others)
    with pytest.raises(ProfileConflictError):
        apparmor_install(layout, home, user_app(home))
    assert profile.read_text() == others
    system = render_apparmor_profile("anytype", str(layout.apps_dir / "Anytype.AppImage"))
    profile.write_text(system)
    with pytest.raises(ProfileConflictError):
        apparmor_install(layout, home, user_app(home))
    profile.write_text("# hand-written by the admin\nprofile x {}\n")
    with pytest.raises(ProfileConflictError):
        apparmor_install(layout, home, user_app(home))
    assert profile.read_text() == "# hand-written by the admin\nprofile x {}\n"


def test_apparmor_install_planted_symlink(layout, home, secret):
    layout.apparmor_dir.mkdir(parents=True)
    (layout.apparmor_dir / "easyinstaller-anytype").symlink_to(secret)
    with pytest.raises(ProfileConflictError):
        apparmor_install(layout, home, user_app(home))
    assert secret.read_text() == "root:$6$secret\n"


def test_apparmor_remove(layout, home, commands):
    apparmor_install(layout, home, user_app(home))
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    result = ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout, run_commands=True,
                                    caller_home=home)
    assert result["removed"] is True and result["profile_path"] == str(profile)
    assert not profile.exists()
    assert commands.commands() == [["/usr/bin/apparmor_parser", "-R", str(profile)]]
    assert commands.existed == [True]  # unloaded before the file was deleted
    result = ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout, run_commands=False)
    assert result["removed"] is False


def test_apparmor_remove_keeps_profiles_of_others(layout, home, t3, secret):
    layout.apparmor_dir.mkdir(parents=True)
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    others = render_apparmor_profile("anytype", "/home/bob/Applications/Anytype.AppImage")
    profile.write_text(others)
    assert ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout, run_commands=False,
                                  caller_home=home)["removed"] is False
    assert profile.read_text() == others
    profile.write_text("# not ours\n")
    assert ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout,
                                  run_commands=False)["removed"] is False
    profile.unlink()
    profile.symlink_to(secret)
    assert ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout,
                                  run_commands=False)["removed"] is False
    assert profile.is_symlink() and secret.is_file()


def test_apparmor_remove_keeps_system_app_profile(layout, work):
    app = install(layout, electron_manifest(work))["app"]
    result = ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout, run_commands=False)
    assert result["removed"] is False
    assert Path(app["apparmor_profile"]).is_file()


def test_apparmor_remove_without_caller_home_removes_orphan(layout):
    layout.apparmor_dir.mkdir(parents=True)
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    profile.write_text(render_apparmor_profile("anytype", "/home/x/A.AppImage"))
    assert ops.op_apparmor_remove({"app_id": "anytype"}, layout=layout,
                                  run_commands=False)["removed"] is True


def test_apparmor_remove_invalid_id(layout):
    with pytest.raises(RequestError):
        ops.op_apparmor_remove({"app_id": "../../passwd"}, layout=layout, run_commands=False)


# ------------------------------------------------------------------------------------------------
# install-fuse
# ------------------------------------------------------------------------------------------------


def test_install_fuse_runs_apt_with_fixed_package(commands):
    result = ops.op_install_fuse({"package": "evil; rm -rf /", "args": ["--allow-unauthenticated"]},
                                 status=make_status(), run_commands=True)
    assert result == {"ok": True, "package": "libfuse2t64"}
    [(cmd, env)] = commands.calls
    assert cmd[0] == "/usr/bin/apt-get" and cmd[1:3] == ["install", "-y"]
    assert cmd[-1] == "libfuse2t64"
    assert env == {"DEBIAN_FRONTEND": "noninteractive"}


def test_install_fuse_errors(commands):
    with pytest.raises(HelperError):
        ops.op_install_fuse({}, status=make_status(has_apt=False), run_commands=True)
    with pytest.raises(HelperError):
        ops.op_install_fuse({}, status=make_status(libfuse2_package="fuse; rm -rf /"),
                            run_commands=True)
    assert commands.calls == []
    commands.fail = {("apt-get", "install")}
    with pytest.raises(InstallError) as excinfo:
        ops.op_install_fuse({}, status=make_status(libfuse2_package="libfuse2"), run_commands=True)
    assert "internet" in excinfo.value.message
    commands.missing = {"apt-get"}
    with pytest.raises(HelperError):
        ops.op_install_fuse({}, status=make_status(), run_commands=True)


def test_install_fuse_without_commands_runs_nothing(commands):
    assert ops.op_install_fuse({}, status=make_status(), run_commands=False)["ok"] is True
    assert commands.calls == []


# ------------------------------------------------------------------------------------------------
# commands: safe PATH, minimal environment
# ------------------------------------------------------------------------------------------------


def test_find_tool_ignores_callers_path(tmp_path, monkeypatch):
    fake = tmp_path / "update-desktop-database"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    found = ops.find_tool("update-desktop-database")
    assert found is None or (found.startswith("/") and not found.startswith(str(tmp_path)))
    assert ops.find_tool("env") in ("/usr/bin/env", "/bin/env")


def test_run_command_uses_minimal_environment(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    result = ops.run_command([ops.find_tool("env")])
    assert result.ok
    assert sorted(line.split("=", 1)[0] for line in result.output.splitlines()) == \
        ["LANG", "LC_ALL", "PATH"]
    assert f"PATH={ops.SAFE_PATH}" in result.output.splitlines()


def test_run_command_failures_do_not_raise(tmp_path):
    assert ops.run_command([str(tmp_path / "missing-tool")]).returncode is None
    assert ops.run_command([ops.find_tool("false")]).ok is False
    slow = ops.run_command([ops.find_tool("sleep"), "5"], timeout=0.2)
    assert slow.returncode is None and "timed out" in slow.output


# ------------------------------------------------------------------------------------------------
# privilege switch (simulated root)
# ------------------------------------------------------------------------------------------------


class FakeRoot:
    """Pretend to be root: records seteuid/setegid/setgroups and every os.open."""

    def __init__(self, monkeypatch, *, fail_seteuid: bool = False):
        self.calls: list[tuple] = []
        self.euid, self.egid, self.groups = 0, 0, [0]
        self.fail_seteuid = fail_seteuid
        real_open = os.open
        monkeypatch.setattr(os, "geteuid", lambda: self.euid)
        monkeypatch.setattr(os, "getegid", lambda: self.egid)
        monkeypatch.setattr(os, "getgroups", lambda: list(self.groups))
        monkeypatch.setattr(os, "setgroups", self.setgroups)
        monkeypatch.setattr(os, "setegid", self.setegid)
        monkeypatch.setattr(os, "seteuid", self.seteuid)

        def recording_open(path, flags, *args, **kwargs):
            self.calls.append(("open", os.fspath(path), self.euid, self.egid))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", recording_open)

    def setgroups(self, groups):
        self.calls.append(("setgroups", list(groups)))
        self.groups = list(groups)

    def setegid(self, gid):
        self.calls.append(("setegid", gid))
        self.egid = gid

    def seteuid(self, uid):
        self.calls.append(("seteuid", uid))
        if self.fail_seteuid and uid != 0:
            raise PermissionError(1, "Operation not permitted")
        self.euid = uid

    def privilege_calls(self):
        return [c for c in self.calls if c[0] != "open"]


def test_privileges_are_switched_around_the_open(monkeypatch, t3):
    src, _ = t3
    fake = FakeRoot(monkeypatch)
    fd, st = ops.open_source(str(src), 4242, 4343)
    os.close(fd)
    assert stat.S_ISREG(st.st_mode)
    assert fake.calls == [
        ("setgroups", [4343]), ("setegid", 4343), ("seteuid", 4242),
        ("open", str(src), 4242, 4343),
        ("seteuid", 0), ("setegid", 0), ("setgroups", [0]),
    ]


def test_privileges_are_restored_when_the_open_fails(monkeypatch, work):
    fake = FakeRoot(monkeypatch)
    with pytest.raises(InstallError):
        ops.open_source(str(work / "missing.AppImage"), 4242, 4343)
    assert fake.privilege_calls() == [
        ("setgroups", [4343]), ("setegid", 4343), ("seteuid", 4242),
        ("seteuid", 0), ("setegid", 0), ("setgroups", [0]),
    ]
    assert (fake.euid, fake.egid, fake.groups) == (0, 0, [0])


def test_privileges_are_restored_when_the_switch_fails(monkeypatch, t3):
    src, _ = t3
    fake = FakeRoot(monkeypatch, fail_seteuid=True)
    with pytest.raises(HelperError) as excinfo:  # fatal, not disguised as "cannot read"
        ops.open_source(str(src), 4242, 4343)
    assert "seteuid" in excinfo.value.details
    assert not any(c[0] == "open" for c in fake.calls)
    assert (fake.euid, fake.egid, fake.groups) == (0, 0, [0])


def test_failure_to_restore_privileges_is_fatal(monkeypatch, t3):
    src, _ = t3
    fake = FakeRoot(monkeypatch)
    real_seteuid = fake.seteuid

    def seteuid(uid):
        if uid == 0:
            raise PermissionError(1, "Operation not permitted")
        real_seteuid(uid)

    monkeypatch.setattr(os, "seteuid", seteuid)
    with pytest.raises(HelperError) as excinfo:
        ops.open_source(str(src), 4242, 4343)
    assert "seteuid(0)" in excinfo.value.details
    # egid and groups were still restored by the outer finally blocks
    assert (fake.egid, fake.groups) == (0, [0])


def test_caller_groups_replace_roots_groups(monkeypatch, t3):
    src, _ = t3
    fake = FakeRoot(monkeypatch)
    fake.groups = [0, 4, 27]
    fd, _st = ops.open_source(str(src), os.getuid(), os.getgid())
    os.close(fd)
    switched = fake.calls[0]
    assert switched[0] == "setgroups" and 0 not in switched[1] and os.getgid() in switched[1]
    assert fake.groups == [0, 4, 27]


def test_install_as_fake_root_opens_sources_as_caller_and_refuses_user_folders(
        monkeypatch, layout, t3):
    src, icon = t3
    fake = FakeRoot(monkeypatch)
    with pytest.raises(UnsafePathError) as excinfo:  # the tmp folders are not owned by root
        ops.op_install(make_manifest(src, icon), layout=layout, caller_uid=4242, caller_gid=4343,
                       run_commands=False)
    assert "owned by uid" in (excinfo.value.details or "")
    opens = [c for c in fake.calls if c[0] == "open"]
    source_opens = [c for c in opens if c[1] in (str(src), str(icon))]
    assert [c[1] for c in source_opens] == [str(src), str(icon)]
    assert all(c[2:] == (4242, 4343) for c in source_opens)
    assert all(c[2] == 0 for c in opens if c not in source_opens)
    assert (fake.euid, fake.egid, fake.groups) == (0, 0, [0])
    assert not layout.apps_dir.exists()


def test_apparmor_install_as_fake_root_checks_as_caller(monkeypatch, layout, home):
    path = user_app(home)
    fake = FakeRoot(monkeypatch)
    with pytest.raises(InstallError):   # file owned by the test user, not by uid 4242
        ops.op_apparmor_install({"app_id": "anytype", "appimage_path": str(path)}, layout=layout,
                                caller_uid=4242, caller_gid=4343, caller_home=home,
                                run_commands=False)
    assert ("open", str(path), 4242, 4343) in fake.calls
    assert (fake.euid, fake.egid, fake.groups) == (0, 0, [0])


def test_no_privilege_calls_when_not_root(monkeypatch, t3):
    if IS_ROOT:
        pytest.skip("running as root")
    src, _ = t3

    def forbidden(*args):
        raise AssertionError("must not change privileges when not running as root")

    for name in ("seteuid", "setegid", "setgroups"):
        monkeypatch.setattr(os, name, forbidden)
    fd, _st = ops.open_source(str(src), os.getuid(), os.getgid())
    os.close(fd)


# ------------------------------------------------------------------------------------------------
# client/helper round trip through the real installer code path
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def helper_via_ops(layout, monkeypatch):
    """privileged.run_helper -> JSON round trip -> ops, against the tmp system layout."""
    calls: list[str] = []

    def run_helper(op, payload, *, timeout=600):
        calls.append(op)
        request = json.loads(json.dumps(payload))  # exactly what travels over stdin
        if op == "install":
            return ops.op_install(request, layout=layout, run_commands=False, **ME)
        if op == "uninstall":
            return ops.op_uninstall(request, layout=layout, run_commands=False)
        raise AssertionError(f"unexpected helper op {op}")

    monkeypatch.setattr(privileged, "run_helper", run_helper)
    monkeypatch.setattr(installer, "layout_for",
                        lambda scope: layout if Scope(scope) is Scope.SYSTEM else user_layout())
    monkeypatch.setattr(installer, "find_uninstall_launcher", lambda scope=None: None)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: make_status())
    return calls


SAMPLES = [
    ("t3code", T3_FILE, "512x512/apps/easyinstaller-t3code.png"),
    ("openscad", "OpenSCAD-2026.03.28-x86_64.AppImage", "256x256/apps/easyinstaller-openscad.png"),
    ("freecad", "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage",
     "scalable/apps/easyinstaller-org.freecad.FreeCAD.svg"),
]


@requires_mksquashfs
@requires_unsquashfs
@pytest.mark.parametrize("kind, filename, icon_rel", SAMPLES)
def test_round_trip_with_real_installer(layout, isolated_env, helper_via_ops, kind, filename,
                                        icon_rel):
    downloads = isolated_env / "Downloads"
    src = make_sample_appimage(downloads / filename, kind)
    original = src.read_bytes()
    with inspect_appimage(src) as info:
        plan = plan_install(info, InstallOptions(scope=Scope.SYSTEM), status=make_status())
        app = execute_install(plan)
        planned_desktop = plan.desktop_text
    assert helper_via_ops == ["install"]
    assert not src.exists()  # moved: the client deletes its own copy after success

    target = Path(app.appimage_path)
    assert target == plan.target_appimage and target.parent == layout.apps_dir
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert Path(app.desktop_path) == plan.desktop_path
    assert stat.S_IMODE(Path(app.desktop_path).stat().st_mode) == 0o644
    # The helper renders the launcher itself - and gets exactly what the client planned.
    assert Path(app.desktop_path).read_text(encoding="utf-8") == planned_desktop
    assert app.icon_paths == [str(layout.icons_dir / icon_rel)]
    assert Path(app.icon_paths[0]) == plan.icon_target
    assert stat.S_IMODE(Path(app.icon_paths[0]).stat().st_mode) == 0o644
    assert Registry(layout.registry_path).get(app.id) == app
    assert app.sha256 == hashlib.sha256(original).hexdigest()
    assert app.original_filename == filename
    assert user_layout().apps_dir.exists() is False

    uninstall(app.id, Scope.SYSTEM)
    assert helper_via_ops == ["install", "uninstall"]
    assert not target.exists() and not Path(app.desktop_path).exists()
    assert not Path(app.icon_paths[0]).exists()
    assert Registry(layout.registry_path).load() == {}


@requires_mksquashfs
@requires_unsquashfs
def test_round_trip_electron_app_with_apparmor(layout, isolated_env, helper_via_ops, monkeypatch):
    downloads = isolated_env / "Downloads"
    icon = "usr/share/icons/hicolor/256x256/apps/anytype.png"
    src = make_fake_appimage(downloads / "Anytype-0.55.4.AppImage", {
        "anytype.desktop": ANYTYPE_DESKTOP, "AppRun": "#!/bin/sh\n", "chrome-sandbox": b"\x7fELF",
        "resources/app.asar": b"asar", icon: make_png(256, 256),
    }, {".DirIcon": icon})
    status = make_status(userns_restricted=True)
    monkeypatch.setattr(installer, "get_system_status", lambda refresh=False: status)
    with inspect_appimage(src) as info:
        plan = plan_install(info, InstallOptions(scope=Scope.SYSTEM, keep_original=True),
                            status=status)
        assert plan.apparmor_profile_text is not None
        app = execute_install(plan)
    assert src.is_file()  # keep_original
    assert app.sandbox_fix == "apparmor"
    profile = layout.apparmor_dir / "easyinstaller-anytype"
    assert app.apparmor_profile == str(profile)
    assert profile.read_text() == plan.apparmor_profile_text
    assert "--no-sandbox" not in read_desktop(app.desktop_path).get("Exec")


@pytest.mark.real
def test_round_trip_with_a_real_appimage(small_real_appimage, layout, isolated_env, helper_via_ops):
    import shutil
    import subprocess

    downloads = isolated_env / "Downloads"
    downloads.mkdir()
    copy = downloads / small_real_appimage.name
    shutil.copyfile(small_real_appimage, copy)
    with inspect_appimage(copy) as info:
        expected_sha = info.sha256
        plan = plan_install(info, InstallOptions(scope=Scope.SYSTEM), status=make_status())
        app = execute_install(plan)
        assert Path(app.desktop_path).read_text(encoding="utf-8") == plan.desktop_text
    assert small_real_appimage.is_file()  # the real sample is never touched
    assert not copy.exists()
    assert app.sha256 == expected_sha and sha256(Path(app.appimage_path)) == expected_sha
    assert stat.S_IMODE(Path(app.appimage_path).stat().st_mode) == 0o755
    validator = shutil.which("desktop-file-validate")
    if validator:
        proc = subprocess.run([validator, app.desktop_path], capture_output=True, text=True)
        assert [line for line in proc.stdout.splitlines() if "error:" in line] == [], proc.stdout
    uninstall(app.id, Scope.SYSTEM)
    assert not Path(app.appimage_path).exists()
    assert Registry(layout.registry_path).load() == {}


# ------------------------------------------------------------------------------------------------
# 0.2: the kept previous version (keep_backup / consume_backup / backup_max_age_days / drop-backup)
# ------------------------------------------------------------------------------------------------

BACKUPS = ".easyinstaller-backups"


def backup_dir(layout, app_id: str = "t3code") -> Path:
    return layout.apps_dir / BACKUPS / app_id


def v2_source(work: Path, payload: bytes = b"version two " * 40) -> Path:
    return write_appimage(work / "T3-Code-0.0.43-x86_64.AppImage", payload)


def update(layout, work, icon, **overrides) -> dict:
    src = overrides.pop("src", None) or v2_source(work)
    values = {"version": "0.0.43", "keep_backup": True, **overrides}
    return install(layout, make_manifest(src, icon, **values))


def reg(layout) -> Registry:
    return Registry(layout.registry_path)


def test_update_with_keep_backup_keeps_the_replaced_version(layout, work, t3):
    src, icon = t3
    first = install(layout, make_manifest(src, icon, keep_backup=True))["app"]
    assert first["previous"] is None and not (layout.apps_dir / BACKUPS).exists()
    assert first["mtime_ns"] == os.stat(first["appimage_path"]).st_mtime_ns > 0
    inode = os.stat(first["appimage_path"]).st_ino

    result = update(layout, work, icon)
    app = result["app"]
    kept = backup_dir(layout) / "T3-Code-Alpha-0.0.42.AppImage"
    assert result["warnings"] == [] and app["version"] == "0.0.43"
    assert app["previous"] == {"version": "0.0.42", "path": str(kept), "sha256": first["sha256"],
                               "size": first["size"], "saved_at": app["previous"]["saved_at"],
                               "original_filename": first["original_filename"]}
    assert kept.read_bytes() == src.read_bytes()
    st = os.lstat(kept)
    assert st.st_ino == inode and st.st_nlink == 1          # the file itself: linked, not copied
    assert stat.S_IMODE(st.st_mode) == 0o755
    assert stat.S_IMODE(kept.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(kept.parent.parent.stat().st_mode) == 0o755
    assert entries(layout.apps_dir) == [BACKUPS, f"{BACKUPS}/t3code",
                                        f"{BACKUPS}/t3code/T3-Code-Alpha-0.0.42.AppImage",
                                        T3_TARGET]
    assert reg(layout).get("t3code").previous == app["previous"]
    assert app["mtime_ns"] == os.stat(app["appimage_path"]).st_mtime_ns

    # one backup per app - also when the new version gets another file name
    third = write_appimage(work / "third.AppImage", b"third " * 30)
    app3 = update(layout, work, icon, src=third, version="0.0.44", name="T3 Code")["app"]
    assert [p.name for p in backup_dir(layout).iterdir()] == ["T3-Code-Alpha-0.0.43.AppImage"]
    assert app3["previous"]["version"] == "0.0.43" and app3["previous"]["sha256"] == app["sha256"]
    assert sorted(os.listdir(layout.apps_dir)) == [BACKUPS, "T3-Code.AppImage"]


def test_no_backup_when_nothing_is_replaced(layout, work, t3):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    # the same file again (also from the installed file itself): nothing to keep
    again = install(layout, make_manifest(src, icon, keep_backup=True))["app"]
    assert again["previous"] is None and not (layout.apps_dir / BACKUPS).exists()
    app = update(layout, work, icon)["app"]
    kept = Path(app["previous"]["path"])
    # a reinstall keeps the recorded backup ...
    same = update(layout, work, icon, keep_backup=False)["app"]
    assert same["previous"] == app["previous"] and kept.is_file()
    # ... an update without keep_backup forgets and deletes it (never an older "previous")
    third = write_appimage(work / "third.AppImage", b"third " * 30)
    gone = update(layout, work, icon, src=third, version="0.0.44", keep_backup=False)["app"]
    assert gone["previous"] is None and sorted(os.listdir(layout.apps_dir)) == [T3_TARGET]
    assert leftovers(layout) == []


def test_repair_of_a_changed_app_file_makes_no_backup(layout, work, t3):
    """"Repair" after a system app file changed in place: the source is the installed file."""
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]
    installed = Path(second["appimage_path"])
    changed = build_runtime() + b"changed in place " * 30
    installed.write_bytes(changed)
    repaired = install(layout, make_manifest(installed, icon, version="0.0.44",
                                             keep_backup=True))["app"]
    assert repaired["version"] == "0.0.44" and installed.read_bytes() == changed
    assert repaired["sha256"] == sha256(installed) != second["sha256"]
    assert repaired["previous"] == second["previous"]          # the 0.0.42 backup, untouched
    assert [p.name for p in backup_dir(layout).iterdir()] == ["T3-Code-Alpha-0.0.42.AppImage"]


@pytest.mark.parametrize("hard_links", [True, False])
def test_failed_update_with_backup_restores_everything(layout, work, t3, monkeypatch, hard_links):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]                 # an older backup exists
    before = {p: (layout.apps_dir / p).read_bytes() for p in entries(layout.apps_dir)
              if (layout.apps_dir / p).is_file()}
    third = write_appimage(work / "third.AppImage", b"third " * 30)

    def broken_put(self, app):
        raise RuntimeError("registry exploded")

    with monkeypatch.context() as patched:
        patched.setattr(Registry, "put", broken_put)
        if not hard_links:
            real_link = os.link

            def no_links(src, dst, **kwargs):
                raise OSError(1, "Operation not permitted")

            patched.setattr(ops.os, "link", no_links)
            assert real_link is not no_links
        with pytest.raises(RuntimeError):
            update(layout, work, icon, src=third, version="0.0.44", name="T3 Code")
    after = {p: (layout.apps_dir / p).read_bytes() for p in entries(layout.apps_dir)
             if (layout.apps_dir / p).is_file()}
    assert after == before
    assert reg(layout).get("t3code").to_dict() == second
    assert Path(second["previous"]["path"]).is_file()


def test_update_without_hard_links_moves_the_old_version(layout, work, t3, monkeypatch):
    src, icon = t3
    first = install(layout, make_manifest(src, icon))["app"]
    inode = os.stat(first["appimage_path"]).st_ino

    def no_links(src, dst, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(ops.os, "link", no_links)
    app = update(layout, work, icon)["app"]
    kept = Path(app["previous"]["path"])
    assert os.stat(kept).st_ino == inode and kept.read_bytes() == src.read_bytes()
    assert Path(app["appimage_path"]).read_bytes() == v2_source(work).read_bytes()


def test_older_backup_stays_when_the_newer_one_cannot_be_kept(layout, work, t3, monkeypatch):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]

    def impossible(*args, **kwargs):
        raise OSError(13, "Permission denied")

    third = write_appimage(work / "third.AppImage", b"third " * 30)
    with monkeypatch.context() as patched:
        patched.setattr(ops.os, "link", impossible)
        real_rename = os.rename
        patched.setattr(ops.os, "rename", lambda a, b, **kw: impossible()
                        if kw.get("src_dir_fd") != kw.get("dst_dir_fd") else real_rename(a, b, **kw))
        result = update(layout, work, icon, src=third, version="0.0.44")
    assert result["app"]["version"] == "0.0.44" and len(result["warnings"]) == 1
    assert result["app"]["previous"] == second["previous"]        # still 0.0.42
    assert Path(second["previous"]["path"]).read_bytes() == src.read_bytes()
    assert [p.name for p in backup_dir(layout).iterdir()] == ["T3-Code-Alpha-0.0.42.AppImage"]


def test_backup_that_cannot_be_kept_is_only_a_warning(layout, work, t3, tmp_path):
    """A planted link where the backups folder should be: nothing is written through it."""
    src, icon = t3
    install(layout, make_manifest(src, icon))
    evil = tmp_path / "evil"
    evil.mkdir()
    (layout.apps_dir / BACKUPS).symlink_to(evil, target_is_directory=True)
    result = update(layout, work, icon)
    assert result["app"]["version"] == "0.0.43" and result["app"]["previous"] is None
    assert result["warnings"] == [
        "The previous version could not be kept, so you cannot go back to it."]
    assert list(evil.iterdir()) == []

    # the app file of another entry, too, is never moved away
    (layout.apps_dir / BACKUPS).unlink()
    shared = reg(layout).get("t3code")
    import dataclasses
    reg(layout).put(dataclasses.replace(shared, id="other", name="Other", previous=None,
                                        desktop_path=str(layout.desktop_dir / "easyinstaller-other.desktop"),
                                        icon_paths=[]))
    third = write_appimage(work / "third.AppImage", b"third " * 30)
    result = update(layout, work, icon, src=third, version="0.0.44", name="T3 Code")
    assert result["app"]["previous"] is None and len(result["warnings"]) == 1
    assert Path(shared.appimage_path).is_file() and not (layout.apps_dir / BACKUPS).exists()
    # ... and it only goes with the last entry that uses it
    other = reg(layout).get("other")
    reg(layout).put(dataclasses.replace(other, id="other2", desktop_path=str(
        layout.desktop_dir / "easyinstaller-other2.desktop")))
    result = ops.op_uninstall({"app_id": "other"}, layout=layout, run_commands=False)
    assert shared.appimage_path in result["skipped"] and Path(shared.appimage_path).is_file()
    result = ops.op_uninstall({"app_id": "other2"}, layout=layout, run_commands=False)
    assert shared.appimage_path in result["removed"] and not Path(shared.appimage_path).exists()
    assert set(reg(layout).load()) == {"t3code"}


# -- consume_backup (going back) ------------------------------------------------------------------


def test_consume_backup_swaps_the_two_versions(layout, work, t3):
    src, icon = t3
    first = install(layout, make_manifest(src, icon))["app"]
    second = update(layout, work, icon)["app"]
    kept = Path(second["previous"]["path"])

    result = install(layout, make_manifest(kept, icon, consume_backup=True, keep_backup=True))
    app = result["app"]
    assert result["warnings"] == [] and app["version"] == "0.0.42"
    assert app["sha256"] == first["sha256"]
    assert Path(app["appimage_path"]).read_bytes() == src.read_bytes()
    assert not kept.exists()                                   # used up by the helper itself
    now_kept = backup_dir(layout) / "T3-Code-Alpha-0.0.43.AppImage"
    assert app["previous"]["path"] == str(now_kept) and app["previous"]["version"] == "0.0.43"
    assert app["previous"]["sha256"] == second["sha256"]
    assert now_kept.read_bytes() == v2_source(work).read_bytes()
    assert [p.name for p in backup_dir(layout).iterdir()] == [now_kept.name]
    assert leftovers(layout) == [str(layout.apps_dir / BACKUPS)]   # (the folder matches the glob)

    # without keep_backup the kept version simply replaces the current one
    back = install(layout, make_manifest(now_kept, icon, version="0.0.43", consume_backup=True))
    assert back["app"]["previous"] is None and sorted(os.listdir(layout.apps_dir)) == [T3_TARGET]
    assert Path(back["app"]["appimage_path"]).read_bytes() == v2_source(work).read_bytes()


def test_consume_backup_must_be_this_apps_recorded_backup(layout, work, t3, secret):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]
    kept = Path(second["previous"]["path"])
    other_src = write_appimage(work / "other.AppImage", b"other one " * 20)
    install(layout, make_manifest(other_src, None, app_id="other", name="Other"))
    other_new = write_appimage(work / "other2.AppImage", b"other two " * 20)
    other = install(layout, make_manifest(other_new, None, app_id="other", name="Other",
                                          version="2", keep_backup=True))["app"]
    other_kept = Path(other["previous"]["path"])
    assert other_kept.parent == backup_dir(layout, "other")
    stray = write_appimage(backup_dir(layout) / "Stray-1.0.AppImage", b"stray")   # not recorded
    outside = write_appimage(work / "outside.AppImage", b"outside")
    before = {p: (layout.apps_dir / p).read_bytes() for p in entries(layout.apps_dir)
              if (layout.apps_dir / p).is_file()}
    registry_before = layout.registry_path.read_bytes()

    def refused(source: Path, **overrides) -> None:
        with pytest.raises(RequestError) as excinfo:
            install(layout, make_manifest(source, icon, consume_backup=True, keep_backup=True,
                                          **overrides))
        assert "consume_backup" in excinfo.value.details
        assert {p: (layout.apps_dir / p).read_bytes() for p in entries(layout.apps_dir)
                if (layout.apps_dir / p).is_file()} == before
        assert layout.registry_path.read_bytes() == registry_before

    refused(other_kept)                                   # another app's backup
    refused(stray)                                        # in the right folder, but not recorded
    refused(outside)                                      # outside the layout
    refused(src)
    refused(Path(second["appimage_path"]))                # the installed file itself
    refused(kept, app_id="other", name="Other")           # this app's backup for another app
    refused(kept, app_id="unknown", name="Unknown")       # no such app: nothing recorded
    assert other_kept.is_file() and kept.is_file() and outside.is_file()

    # A tampered registry cannot point the helper at a file elsewhere - not even at a real app.
    app = reg(layout).get("t3code")
    import dataclasses
    for path in (outside, secret, other_kept, Path(other["appimage_path"]),
                 backup_dir(layout) / ".." / "other" / other_kept.name,
                 backup_dir(layout) / "sub" / "x.AppImage"):
        reg(layout).put(dataclasses.replace(app, previous={**app.previous, "path": str(path)}))
        registry_before = layout.registry_path.read_bytes()
        digest = sha256(path) if path.is_file() else "0" * 64
        with pytest.raises((RequestError, InstallError)):
            install(layout, make_manifest(path, icon, consume_backup=True, keep_backup=True,
                                          sha256=digest, size=None))
        assert layout.registry_path.read_bytes() == registry_before
        assert secret.read_text() == "root:$6$secret\n"
        assert other_kept.is_file() and outside.is_file() and Path(other["appimage_path"]).is_file()


def test_consume_backup_fails_whole_when_the_current_version_cannot_be_kept(layout, work, t3,
                                                                            monkeypatch):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]
    kept = Path(second["previous"]["path"])
    registry_before = layout.registry_path.read_bytes()

    def impossible(*args, **kwargs):
        raise OSError(18, "Invalid cross-device link")

    with monkeypatch.context() as patched:
        patched.setattr(ops.os, "link", impossible)
        real_rename = os.rename

        def rename(src, dst, **kwargs):
            if kwargs.get("src_dir_fd") != kwargs.get("dst_dir_fd"):
                raise OSError(18, "Invalid cross-device link")
            return real_rename(src, dst, **kwargs)

        patched.setattr(ops.os, "rename", rename)
        with pytest.raises(InstallError, match="could not be kept, so nothing was changed"):
            install(layout, make_manifest(kept, icon, consume_backup=True, keep_backup=True))
    assert layout.registry_path.read_bytes() == registry_before
    assert kept.read_bytes() == src.read_bytes()
    assert Path(second["appimage_path"]).read_bytes() == v2_source(work).read_bytes()
    assert sorted(p.name for p in backup_dir(layout).iterdir()) == [kept.name]


# -- drop-backup, uninstall, pruning ----------------------------------------------------------------


def test_drop_backup(layout, work, t3):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    assert ops.op_drop_backup({"app_id": "t3code"}, layout=layout, run_commands=False) == \
        {"ok": True, "removed": [], "skipped": []}                 # nothing kept
    second = update(layout, work, icon)["app"]
    kept = second["previous"]["path"]
    result = ops.op_drop_backup({"app_id": "t3code", "extra": "ignored"}, layout=layout,
                                run_commands=False)
    assert result == {"ok": True, "removed": [kept], "skipped": []}
    assert reg(layout).get("t3code").previous is None
    assert {**reg(layout).get("t3code").to_dict(), "previous": second["previous"]} == second
    assert sorted(os.listdir(layout.apps_dir)) == [T3_TARGET]

    with pytest.raises(NotInstalledError):
        ops.op_drop_backup({"app_id": "unknown"}, layout=layout, run_commands=False)
    for bad in ("../evil", "", None, 5, "a/b", ["t3code"]):
        with pytest.raises(RequestError):
            ops.op_drop_backup({"app_id": bad}, layout=layout, run_commands=False)
    with pytest.raises(RequestError):
        ops.op_drop_backup(["t3code"], layout=layout, run_commands=False)


def test_drop_backup_without_a_registry(layout):
    with pytest.raises(NotInstalledError):
        ops.op_drop_backup({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert not layout.registry_path.parent.exists()


def test_tampered_previous_path_is_never_deleted(layout, work, t3, secret, tmp_path):
    """drop-backup, uninstall and an update only delete a backup in the app's own folder."""
    import dataclasses

    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]
    real_backup = Path(second["previous"]["path"])
    other_src = write_appimage(work / "other.AppImage", b"other one " * 20)
    other = install(layout, make_manifest(other_src, None, app_id="other", name="Other"))["app"]
    decoy = write_appimage(backup_dir(layout, "other") / "Other-1.0.AppImage", b"other backup")
    targets = [secret, Path(other["appimage_path"]), decoy, layout.registry_path,
               backup_dir(layout) / ".." / "other" / "Other-1.0.AppImage",
               backup_dir(layout), layout.apps_dir / BACKUPS / "t3code.AppImage",
               Path("relative.AppImage")]
    app = reg(layout).get("t3code")

    def tamper(path) -> None:
        reg(layout).put(dataclasses.replace(app, previous={**app.previous, "path": str(path)}))

    def untouched() -> None:
        assert secret.read_text() == "root:$6$secret\n"
        assert Path(other["appimage_path"]).read_bytes() == other_src.read_bytes()
        assert decoy.is_file() and layout.registry_path.is_file()

    for index, path in enumerate(targets):
        tamper(path)
        result = ops.op_drop_backup({"app_id": "t3code"}, layout=layout, run_commands=False)
        # (the real kept version, now recorded nowhere, is t3code's own: it goes - SEC-1)
        assert result["removed"] == ([str(real_backup)] if index == 0 else [])
        assert result["skipped"] == [str(path)]
        assert reg(layout).get("t3code").previous is None      # forgotten, not deleted
        untouched()
    assert not real_backup.exists()

    # an update that replaces the file: the tampered "older backup" is not deleted either
    tamper(secret)
    third = write_appimage(work / "third.AppImage", b"third " * 30)
    update(layout, work, icon, src=third, version="0.0.44")
    untouched()

    # uninstall
    tamper(decoy)
    result = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert str(decoy) in result["skipped"]
    untouched()

    # a symlinked backup folder is not followed
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    (outside / "Other-1.0.AppImage").write_text("keep me")
    decoy.unlink()
    backup_dir(layout, "other").rmdir()
    backup_dir(layout, "other").symlink_to(outside, target_is_directory=True)
    other_app = reg(layout).get("other")
    reg(layout).put(dataclasses.replace(other_app, previous={
        "version": "1", "path": str(backup_dir(layout, "other") / "Other-1.0.AppImage"),
        "sha256": None, "size": 1, "saved_at": "2026-01-01T00:00:00Z"}))
    result = ops.op_drop_backup({"app_id": "other"}, layout=layout, run_commands=False)
    assert result["removed"] == [] and len(result["skipped"]) == 1
    assert (outside / "Other-1.0.AppImage").read_text() == "keep me"


def test_recorded_backup_paths(layout):
    folder = backup_dir(layout)
    allowed = ops.recorded_path_allowed
    assert allowed(layout, str(folder / "T3-Code-Alpha-0.0.42.AppImage"), "backup", "t3code")
    for path in (folder / "x.txt", folder / ".hidden.AppImage", folder / "sub" / "x.AppImage",
                 folder.parent / "x.AppImage", layout.apps_dir / "x.AppImage",
                 folder / ".." / "t3code" / "x.AppImage", Path("/etc/passwd")):
        assert not allowed(layout, str(path), "backup", "t3code"), path
    assert not allowed(layout, str(folder / "x.AppImage"), "backup", "other")
    assert not allowed(layout, str(folder / "x.AppImage"), "backup", "../t3code")
    assert not allowed(layout, str(folder / "x.AppImage"), "appimage", "t3code")
    assert not allowed(layout, None, "backup", "t3code")


def test_uninstall_removes_the_backup(layout, work, t3):
    src, icon = t3
    install(layout, make_manifest(src, icon))
    second = update(layout, work, icon)["app"]
    result = ops.op_uninstall({"app_id": "t3code"}, layout=layout, run_commands=False)
    assert second["previous"]["path"] in result["removed"]
    assert list(layout.apps_dir.iterdir()) == []
    assert leftovers(layout) == []


def test_old_backups_are_pruned_during_an_install(layout, work, t3, secret):
    import dataclasses
    import time

    src, icon = t3

    def with_backup(app_id: str, age_days: float | None, saved_at: str | None = None) -> Path:
        one = write_appimage(work / f"{app_id}-1.AppImage", f"{app_id} one ".encode() * 20)
        two = write_appimage(work / f"{app_id}-2.AppImage", f"{app_id} two ".encode() * 20)
        install(layout, make_manifest(one, None, app_id=app_id, name=app_id.title(), version="1"))
        install(layout, make_manifest(two, None, app_id=app_id, name=app_id.title(), version="2",
                                      keep_backup=True))
        app = reg(layout).get(app_id)
        if saved_at is None:
            saved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(time.time() - age_days * 86400))
        reg(layout).put(dataclasses.replace(app, previous={**app.previous, "saved_at": saved_at}))
        return Path(app.previous["path"])

    old = with_backup("old", 31)
    young = with_backup("young", 29)
    undated = with_backup("undated", None, saved_at="some day")
    tampered = with_backup("tampered", 400)
    app = reg(layout).get("tampered")
    reg(layout).put(dataclasses.replace(app, previous={**app.previous, "path": str(secret)}))

    install(layout, make_manifest(src, icon))                       # no pruning asked for
    assert old.is_file()
    install(layout, make_manifest(src, icon, backup_max_age_days=30))
    assert not old.exists() and reg(layout).get("old").previous is None
    assert not old.parent.exists()                                  # its empty folder is gone
    assert young.is_file() and reg(layout).get("young").previous["path"] == str(young)
    assert undated.is_file() and reg(layout).get("undated").previous is not None
    assert secret.read_text() == "root:$6$secret\n"
    assert reg(layout).get("tampered").previous is None             # forgotten, never deleted
    assert tampered.is_file()

    # the number of days is clamped to 0..90: a huge number still means 90 days, and 0 (backups
    # are switched off, DATA-3) means that every kept version goes
    ancient = with_backup("ancient", 91)
    today = with_backup("today", 0.5)
    install(layout, make_manifest(src, icon, backup_max_age_days=10 ** 6))
    assert young.is_file() and today.is_file() and not ancient.exists()
    install(layout, make_manifest(src, icon, backup_max_age_days=1))
    assert not young.exists() and today.is_file()
    assert reg(layout).get("today").previous is not None
    install(layout, make_manifest(src, icon, backup_max_age_days=0))
    assert not today.exists() and reg(layout).get("today").previous is None
    request = ops.InstallRequest.from_manifest(make_manifest(src, icon, backup_max_age_days=0))
    assert request.backup_max_age_days == 0
    request = ops.InstallRequest.from_manifest(make_manifest(src, icon, backup_max_age_days=-5))
    assert request.backup_max_age_days == 0
    request = ops.InstallRequest.from_manifest(make_manifest(src, icon, backup_max_age_days=1000))
    assert request.backup_max_age_days == 90
    assert ops.InstallRequest.from_manifest(make_manifest(src, icon)).backup_max_age_days is None


# ------------------------------------------------------------------------------------------------
# 0.2: keep both (variant id, base_id, pinned) and the values only stored for the client
# ------------------------------------------------------------------------------------------------

UPDATE_SOURCE = {"kind": "github-assets", "owner": "FreeCAD", "repo": "FreeCAD",
                 "release": "latest", "pattern": "FreeCAD*x86_64*.AppImage", "url": None,
                 "prerelease": False, "via": "upd_info"}
SIGNATURE = {"status": "unverified", "fingerprint": "ABCD1234" * 5, "signer": None,
             "details": "gpg: Signature made ...\n\tusing RSA key"}


def test_stored_values_end_up_in_the_registry(layout, t3):
    src, icon = t3
    manifest = make_manifest(
        src, icon, app_id="t3code--0.0.42", name="T3 Code (Alpha) 0.0.42", base_id="t3code",
        pinned=True, update_source=UPDATE_SOURCE, origin_url="https://example.org/dl/T3.AppImage",
        signature=SIGNATURE, data_hints=["t3code", "T3 Code", "t3code"],
        mtime_ns=1, previous={"path": "/etc/passwd"}, kind="portable", install_dir="/etc")
    app = install(layout, manifest)["app"]
    assert (app["id"], app["base_id"], app["pinned"]) == ("t3code--0.0.42", "t3code", True)
    assert app["update_source"] == UPDATE_SOURCE and app["signature"] == SIGNATURE
    assert app["origin_url"] == "https://example.org/dl/T3.AppImage"
    assert app["data_hints"] == ["t3code", "T3 Code"]
    # never taken from the request: measured or decided by the helper
    assert app["mtime_ns"] == os.stat(app["appimage_path"]).st_mtime_ns != 1
    assert app["previous"] is None and app["kind"] == "appimage" and app["install_dir"] is None
    stored = reg(layout).get("t3code--0.0.42")
    assert stored.to_dict() == app and stored.main_id == "t3code"
    assert app["appimage_path"] == str(layout.apps_dir / "T3-Code-Alpha-0.0.42.AppImage")
    assert read_desktop(app["desktop_path"]).get("X-EasyInstaller-Id") == "t3code--0.0.42"

    plain = install(layout, make_manifest(src, icon))["app"]
    assert (plain["base_id"], plain["pinned"], plain["update_source"], plain["origin_url"],
            plain["signature"], plain["data_hints"]) == (None, False, None, None, None, [])
    # empty values mean "nothing"
    empty = install(layout, make_manifest(src, icon, update_source={}, signature=None,
                                          origin_url="", data_hints=[], base_id=None,
                                          pinned=None))["app"]
    assert (empty["update_source"], empty["origin_url"], empty["data_hints"]) == (None, None, [])


@pytest.mark.parametrize("key, value", [
    ("base_id", "../evil"),
    ("base_id", "a/b"),
    ("base_id", ""),
    ("base_id", "x\n"),
    ("base_id", "x" * 129),
    ("base_id", 5),
    ("base_id", ["t3code"]),
    ("base_id", {"id": "t3code"}),
    ("base_id", "t3code"),                       # its own id: not a copy of itself
    ("pinned", "yes"),
    ("pinned", 1),
    ("keep_backup", "true"),
    ("keep_backup", 1),
    ("consume_backup", "true"),
    ("consume_backup", 0),
    ("backup_max_age_days", "14"),
    ("backup_max_age_days", True),
    ("backup_max_age_days", 1.5),
    ("backup_max_age_days", [14]),
    ("update_source", "github"),
    ("update_source", ["github"]),
    ("update_source", 7),
    ("update_source", {"kind": {"nested": True}}),
    ("update_source", {"kind": ["a"]}),
    ("update_source", {"kind": 1.5}),
    ("update_source", {"kind": 2 ** 60}),
    ("update_source", {"kind": "x" * 5000}),
    ("update_source", {"kind": "a\x00b"}),
    ("update_source", {"kind": "a\rb"}),
    ("update_source", {"kind": "a\x1b[31m"}),
    ("update_source", {"bad key": "x"}),
    ("update_source", {"": "x"}),
    ("update_source", {"../x": "x"}),
    ("update_source", {"k" * 65: "x"}),
    ("update_source", {f"key{n}": "x" for n in range(17)}),
    ("signature", "valid"),
    ("signature", {"status": {"really": "valid"}}),
    ("signature", {"status": "x" * 5000}),
    ("signature", {"details": "a\x07b"}),
    ("origin_url", 5),
    ("origin_url", ["https://example.org"]),
    ("origin_url", "ftp://example.org/x"),
    ("origin_url", "file:///etc/passwd"),
    ("origin_url", "javascript:alert(1)"),
    ("origin_url", "https://"),
    ("origin_url", "https://example.org/\nExec=evil"),
    ("origin_url", "https://example.org/" + "a" * 3000),
    ("data_hints", "t3code"),
    ("data_hints", {"a": 1}),
    ("data_hints", ["ok", 5]),
    ("data_hints", ["ok", None]),
    ("data_hints", [""]),
    ("data_hints", ["   "]),
    ("data_hints", ["a/b"]),
    ("data_hints", [".."]),
    ("data_hints", ["."]),
    ("data_hints", ["a\nb"]),
    ("data_hints", ["a\x00b"]),
    ("data_hints", ["x" * 129]),
    ("data_hints", [f"hint{n}" for n in range(33)]),
])
def test_invalid_stored_values_are_refused(layout, t3, key, value):
    src, icon = t3
    with pytest.raises(RequestError) as excinfo:
        install(layout, make_manifest(src, icon, **{key: value}))
    assert key in (excinfo.value.details or "")
    assert not layout.apps_dir.exists() and not layout.registry_path.exists()


def test_a_giant_request_value_is_refused_cheaply(layout, t3):
    src, icon = t3
    giant = {"kind": "x" * (1 << 20)}
    for key in ("update_source", "signature"):
        with pytest.raises(RequestError) as excinfo:
            install(layout, make_manifest(src, icon, **{key: giant}))
        assert len(excinfo.value.details) < 300          # and the answer does not echo it back
    with pytest.raises(RequestError) as excinfo:
        install(layout, make_manifest(src, icon, data_hints=["h" * (1 << 20)]))
    assert len(excinfo.value.details) < 300
    with pytest.raises(RequestError):
        install(layout, make_manifest(src, icon, origin_url="https://e.org/" + "a" * (1 << 20)))
    assert not layout.apps_dir.exists()


def test_client_side_cleaning_always_passes_the_helper(layout, t3):
    """installer.build_manifest() sends only what the helper accepts (storable_*)."""
    src, icon = t3
    messy_dict = {"status": "valid", "details": "line 1\r\nline 2\x00\x1b[0m\ttab", "n": 5,
                  "big": 2 ** 70, "ratio": 1.5, "nested": {"a": 1}, "list": [1], "bad key": "x",
                  "": "x", "flag": True, "none": None, "long": "y" * 10000, 7: "seven",
                  **{f"k{n}": n for n in range(40)}}
    cleaned = ops.storable_dict(messy_dict)
    assert cleaned["details"] == "line 1\nline 2[0m\ttab" and cleaned["n"] == 5
    assert cleaned["flag"] is True and cleaned["none"] is None and len(cleaned["long"]) == 4096
    assert not {"big", "ratio", "nested", "list", "bad key", "", 7} & set(cleaned)
    assert len(cleaned) == ops.MAX_STORED_KEYS
    assert ops.storable_dict(None) is None and ops.storable_dict("x") is None
    assert ops.storable_dict({"nested": {}}) is None

    assert ops.storable_url("https://example.org/a b") == "https://example.org/a b"
    for bad in (None, 5, "ftp://x", "https://", "https://x/\n", "https://x/" + "a" * 3000):
        assert ops.storable_url(bad) is None
    hints = ops.storable_hints(["ok", "ok", "a/b", "..", "", 5, None, "x" * 200, "fine name",
                                "ctl\x01", *[f"h{n}" for n in range(50)]])
    assert hints[:2] == ["ok", "fine name"] and len(hints) == ops.MAX_DATA_HINTS
    assert ops.storable_hints("ok") == [] and ops.storable_hints(None) == []

    app = install(layout, make_manifest(src, icon, update_source=cleaned, signature=cleaned,
                                        origin_url=ops.storable_url("https://example.org/x"),
                                        data_hints=hints))["app"]
    assert app["update_source"] == cleaned and app["data_hints"] == hints
