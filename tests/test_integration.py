from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from easy_installer.core.desktop_entry import DesktopEntry, split_exec
from easy_installer.core.imageinfo import ImageInfo
from easy_installer.core.integration import (
    DESKTOP_PREFIX,
    ID_RE,
    UNINSTALL_ACTION,
    DesktopRenderSpec,
    apparmor_profile_name,
    appimage_target,
    compare_versions,
    derive_app_id,
    desktop_file_name,
    icon_name,
    icon_target,
    name_from_filename,
    parse_version_from_filename,
    render_desktop_entry,
    safe_file_stem,
    sanitize_id,
)
from easy_installer.core.paths import Scope, system_layout, user_layout

from fakeappimage import FREECAD_DESKTOP, OPENSCAD_DESKTOP, SAMPLE_DESKTOP, T3_DESKTOP

MAIN = DesktopEntry.MAIN
UNINSTALL_GROUP = f"Desktop Action {UNINSTALL_ACTION}"

# =============================================================================================
# ids & names
# =============================================================================================


@pytest.mark.parametrize("raw, expected", [
    ("org.freecad.FreeCAD", "org.freecad.FreeCAD"),
    ("t3code", "t3code"),
    ("T3 Code (Alpha)", "T3-Code-Alpha"),
    ("  --weird__name..", "weird__name"),
    ("Ünïcödé Äpp", "Unicode-App"),
    ("a/b\\c", "a-b-c"),
    ("x" * 300, "x" * 128),
    ("日本語", None),
    ("", None),
    ("---", None),
    ("../../etc", "etc"),
])
def test_sanitize_id(raw, expected):
    assert sanitize_id(raw) == expected
    if expected:
        assert ID_RE.match(expected)


@pytest.mark.parametrize("desktop, name, filename, expected", [
    ("org.freecad.FreeCAD.desktop", "FreeCAD", "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "org.freecad.FreeCAD"),
    ("t3code.desktop", "T3 Code (Alpha)", "T3-Code-0.0.42-x86_64.AppImage", "t3code"),
    ("openscad.desktop", "OpenSCAD", "OpenSCAD-2026.03.28-x86_64.AppImage", "openscad"),
    ("AppRun.desktop", "T3 Code (Alpha)", "x.AppImage", "t3-code-alpha"),
    ("appimage.desktop", "My App", "x.AppImage", "my-app"),
    ("app.desktop", None, "Cool_Tool-2.5.1-x86_64.AppImage", "cool-tool"),
    ("default.desktop", "日本語", "Pen-linux-x86_64.AppImage", "app-77710aedc7"),  # the name wins
    (None, None, "Pen-linux-x86_64.AppImage", "pen"),
    (None, "", "日本語.AppImage", "app-77710aedc7"),
    ("日本語.desktop", None, "", "app-77710aedc7"),
    ("usr/share/applications/org.example.Tool.desktop", None, "", "org.example.Tool"),
])
def test_derive_app_id(desktop, name, filename, expected):
    assert derive_app_id(desktop, name, filename) == expected


def test_derive_app_id_never_shares_a_constant_fallback():
    """Two unrelated apps without any ASCII id must not become "updates" of each other."""
    calc = derive_app_id("калькулятор.desktop", "Калькулятор", "Калькулятор-1.0-x86_64.AppImage")
    editor = derive_app_id("редактор.desktop", "Редактор", "Редактор-2.0-x86_64.AppImage")
    wechat = derive_app_id("微信.desktop", "微信", "微信.AppImage")
    only_version = derive_app_id(None, None, "1.0.AppImage")
    only_arch = derive_app_id(None, None, "x86_64.AppImage")
    ids = [calc, editor, wechat, only_version, only_arch]
    assert len(set(ids)) == len(ids)
    assert all(ID_RE.fullmatch(i) and i.startswith("app-") for i in ids)
    # stable: the next version of the calculator is still the same app
    assert derive_app_id("калькулятор.desktop", "Калькулятор",
                         "Калькулятор-1.1-x86_64.AppImage") == calc
    # without a desktop entry the name from the file name is used (version and arch removed)
    assert derive_app_id(None, None, "Калькулятор-1.0-x86_64.AppImage") == \
        derive_app_id(None, None, "Калькулятор-2.0-x86_64.AppImage")


@pytest.mark.parametrize("desktop, name", [
    ("калькулятор.desktop", "Калькулятор"), ("AppRun.desktop", "Калькулятор"), (None, "微信"),
    ("калькулятор.desktop", None),
])
def test_derive_app_id_does_not_depend_on_the_installed_file_name(desktop, name):
    """Installed, the file is called e.g. "App.AppImage" (safe_file_stem): opening it again
    (Repair, "install it again") must still be the same app, not a second one."""
    downloaded = derive_app_id(desktop, name, "calc-1.0-x86_64.AppImage")
    assert downloaded.startswith("app-")
    assert derive_app_id(desktop, name, safe_file_stem(name or "") + ".AppImage") == downloaded


@pytest.mark.parametrize("filename, version", [
    ("FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "1.1.3"),
    ("OpenSCAD-2026.03.28-x86_64.AppImage", "2026.03.28"),
    ("T3-Code-0.0.42-x86_64.AppImage", "0.0.42"),
    ("winboat-0.9.0-x86_64.AppImage", "0.9.0"),
    ("Anytype-0.55.4.AppImage", "0.55.4"),
    ("Pen-linux-x86_64.AppImage", None),
    ("App-v1.2-x86_64.AppImage", "1.2"),
    ("app-1.0.0-beta.2-x86_64.AppImage", "1.0.0-beta.2"),
    ("app-2.0rc1.AppImage", "2.0rc1"),
    ("tool_x86_64.AppImage", None),
    ("tool-12-x86_64.AppImage", None),
    ("GTK4.14-demo.AppImage", None),
    ("/some/dir-9.9/app-3.4.5.appimage", "3.4.5"),
])
def test_parse_version_from_filename(filename, version):
    assert parse_version_from_filename(filename) == version


@pytest.mark.parametrize("filename, name", [
    ("T3-Code-0.0.42-x86_64.AppImage", "T3 Code"),
    ("FreeCAD_1.1.3-Linux-x86_64-py311.AppImage", "FreeCAD"),
    ("Pen-linux-x86_64.AppImage", "Pen"),
    ("Cool_Tool-2.5.1-x86_64.AppImage", "Cool Tool"),
    ("1.2.3-app.AppImage", "app"),
    ("x86_64.AppImage", ""),
])
def test_name_from_filename(filename, name):
    assert name_from_filename(filename) == name


@pytest.mark.parametrize("a, b, result", [
    ("1.0", "1.0", 0),
    ("1.0", "1.0.0", 0),
    ("1.1.3", "1.1.4", -1),
    ("1.10", "1.9", 1),
    ("0.0.42", "0.0.9", 1),
    ("2026.03.28", "2026.3.29", -1),
    ("1.0-beta", "1.0", -1),
    ("1.0rc1", "1.0", -1),
    ("1.0.1", "1.0rc1", 1),
    ("1.0-beta.2", "1.0-beta.10", -1),
    ("1.0a", "1.0b", -1),
    ("1.0-1", "1.0", 1),
    ("V1.2", "v1.2", 0),
    ("v1.3.0", "1.2.0", 1),
    ("V2.0", "1.9", 1),
    ("v1.2", "1.2", 0),
    ("v1.0.0", "1.0.0", 0),
    ("v1.2.3", "v1.0.0", 1),
    ("version2", "1.0", -1),
    (None, None, 0),
    (None, "0.1", -1),
    ("0.1", None, 1),
])
def test_compare_versions(a, b, result):
    assert compare_versions(a, b) == result
    assert compare_versions(b, a) == -result


@pytest.mark.parametrize("name, stem", [
    ("T3 Code (Alpha)", "T3-Code-Alpha"),
    ("FreeCAD", "FreeCAD"),
    ("OpenSCAD", "OpenSCAD"),
    ("Anytype (XWayland)", "Anytype-XWayland"),
    ("Ünïcödé Äpp!", "Unicode-App"),
    ("..hidden", "hidden"),
    ("a/b/../c", "a-b-..-c"),
    ("日本語", "App"),
    ("", "App"),
    ("x" * 100, "x" * 64),
    ("v1.2 release", "v1.2-release"),
])
def test_safe_file_stem(name, stem):
    assert safe_file_stem(name) == stem
    assert "/" not in stem and len(stem) <= 64 and not stem.startswith(".")


def test_derived_names():
    assert desktop_file_name("t3code") == "easyinstaller-t3code.desktop"
    assert icon_name("org.freecad.FreeCAD") == "easyinstaller-org.freecad.FreeCAD"
    assert apparmor_profile_name("t3code") == "easyinstaller-t3code"
    assert DESKTOP_PREFIX == "easyinstaller-"
    for bad in ("", "../x", "a/b", "-x", "x" * 129, "a b"):
        with pytest.raises(ValueError):
            desktop_file_name(bad)
        with pytest.raises(ValueError):
            icon_name(bad)


# =============================================================================================
# target paths
# =============================================================================================


def test_appimage_target_free_slot(isolated_env):
    layout = user_layout()
    target = appimage_target(layout, "t3code", "T3 Code (Alpha)", lambda p: None)
    assert target == layout.apps_dir / "T3-Code-Alpha.AppImage"


def test_appimage_target_conflicts(isolated_env, tmp_path):
    layout = user_layout()
    layout.apps_dir.mkdir(parents=True)
    existing = layout.apps_dir / "T3-Code-Alpha.AppImage"
    existing.write_bytes(b"x")
    owners = {existing: "t3code"}
    owner_of = owners.get
    # owned by the same app -> reuse (update in place)
    assert appimage_target(layout, "t3code", "T3 Code (Alpha)", owner_of) == existing
    # owned by another app or unknown -> suffixed with the id
    assert appimage_target(layout, "other", "T3 Code (Alpha)", owner_of) == \
        layout.apps_dir / "T3-Code-Alpha-other.AppImage"
    assert appimage_target(layout, "other", "T3 Code (Alpha)", lambda p: None) == \
        layout.apps_dir / "T3-Code-Alpha-other.AppImage"
    # the file is the source itself -> keep it
    assert appimage_target(layout, "other", "T3 Code (Alpha)", lambda p: None, source=existing) == existing
    # a dangling symlink counts as occupied
    (layout.apps_dir / "Dangling.AppImage").symlink_to(tmp_path / "nowhere")
    assert appimage_target(layout, "d", "Dangling", lambda p: None).name == "Dangling-d.AppImage"


def test_appimage_target_rejects_bad_id(isolated_env):
    with pytest.raises(ValueError):
        appimage_target(user_layout(), "../evil", "x", lambda p: None)


def test_icon_target(system_root):
    layout = system_layout(system_root)
    assert icon_target(layout, "t3code", ImageInfo("png", 512, 512)) == \
        layout.icons_dir / "512x512/apps/easyinstaller-t3code.png"
    assert icon_target(layout, "org.freecad.FreeCAD", ImageInfo("svg", None, None)) == \
        layout.icons_dir / "scalable/apps/easyinstaller-org.freecad.FreeCAD.svg"
    assert icon_target(layout, "x", ImageInfo("xpm", 30, 30)) == layout.icons_dir / "24x24/apps/easyinstaller-x.xpm"
    assert str(icon_target(layout, "x", ImageInfo("png", 1, 1))).startswith(str(system_root))


# =============================================================================================
# render_desktop_entry
# =============================================================================================

TARGET = Path("/home/tester/Applications/T3-Code-Alpha.AppImage")
SPACED = Path("/home/tester/My Apps/T3 Code (Alpha).AppImage")
UNINSTALL = ("/usr/bin/easy-installer", "--uninstall", "t3code")


def spec_for(text: str | None, *, app_id="t3code", stem="t3code", path=TARGET, **overrides) -> DesktopRenderSpec:
    kwargs = dict(
        app_id=app_id,
        name="Fallback Name",
        appimage_path=path,
        icon_name=f"easyinstaller-{app_id}",
        embedded=DesktopEntry.parse(text) if text is not None else None,
        embedded_stem=stem,
        comment="Fallback comment",
        version="0.0.42",
        scope=Scope.USER,
    )
    kwargs.update(overrides)
    return DesktopRenderSpec(**kwargs)


def render(text: str | None, **kw) -> DesktopEntry:
    return DesktopEntry.parse(render_desktop_entry(spec_for(text, **kw)))


def validate(text: str, tmp_path: Path, name: str = "easyinstaller-test.desktop") -> None:
    tool = shutil.which("desktop-file-validate")
    if tool is None:
        return
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    proc = subprocess.run([tool, str(path)], capture_output=True, text=True)
    errors = [line for line in (proc.stdout + proc.stderr).splitlines() if ": error:" in line]
    assert not errors, "\n".join(errors) + "\n\n" + text
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_t3(tmp_path):
    spec = spec_for(T3_DESKTOP, extra_args=("--no-sandbox",), uninstall_command=UNINSTALL)
    text = render_desktop_entry(spec)
    validate(text, tmp_path)
    entry = DesktopEntry.parse(text)
    assert entry.groups()[0] == MAIN
    assert entry.get("Type") == "Application"
    assert entry.get("Name") == "T3 Code (Alpha)"
    assert entry.get("Exec") == f"{TARGET} --no-sandbox %U"
    assert entry.get("TryExec") == str(TARGET)
    assert entry.get("Icon") == "easyinstaller-t3code"
    assert entry.get("StartupWMClass") == "t3code"
    assert entry.get_list("MimeType") == ["x-scheme-handler/t3code", "x-scheme-handler/t3code-dev"]
    assert entry.get_list("Categories") == ["Development"]
    assert entry.get("Comment") == "T3 Code desktop build"
    assert entry.get("Terminal") == "false"
    assert entry.get("X-EasyInstaller-Id") == "t3code"
    assert entry.get("X-EasyInstaller-Scope") == "user"
    assert entry.get("X-AppImage-Version") == "0.0.42"
    assert entry.get_list("Actions") == [UNINSTALL_ACTION]
    assert entry.get("Name", UNINSTALL_GROUP) == "Uninstall with Easy Installer…"
    assert entry.get("Name[de]", UNINSTALL_GROUP) == "Mit Easy Installer deinstallieren…"
    assert entry.get("Name[nl]", UNINSTALL_GROUP) == "Verwijderen met Easy Installer…"
    assert entry.get("Icon", UNINSTALL_GROUP) == "user-trash-symbolic"
    assert entry.get("Exec", UNINSTALL_GROUP) == "/usr/bin/easy-installer --uninstall t3code"
    # the embedded entry itself is untouched
    assert spec.embedded.to_text() == T3_DESKTOP


def test_openscad(tmp_path):
    text = render_desktop_entry(spec_for(OPENSCAD_DESKTOP, app_id="openscad", stem="openscad",
                                         version="2026.03.28", uninstall_command=UNINSTALL))
    validate(text, tmp_path)
    entry = DesktopEntry.parse(text)
    assert entry.get("Exec") == f"{TARGET} %f"
    assert entry.get("Version") == "1.0"
    assert entry.get("StartupWMClass") == "org.openscad.openscad"   # kept, not replaced by the stem
    assert entry.get_list("Keywords") == ["3d", "solid", "geometry", "csg", "model", "stl"]
    assert entry.get("X-AppImage-Version") == "2026.03.28"


def test_freecad(tmp_path):
    text = render_desktop_entry(spec_for(FREECAD_DESKTOP, app_id="org.freecad.FreeCAD",
                                         stem="org.freecad.FreeCAD", version="1.1.3", scope=Scope.SYSTEM))
    validate(text, tmp_path)
    entry = DesktopEntry.parse(text)
    assert entry.get("Exec") == f"{TARGET} - --single-instance %F"
    assert entry.get("Comment[de]") == "Feature-basierter parametrischer Modellierer"
    assert entry.get("GenericName[ko]") == "CAD 응용프로그램"
    assert entry.get("StartupNotify") == "true"
    assert entry.get("X-EasyInstaller-Scope") == "system"
    assert entry.get("X-AppImage-Version") == "1.1.3"
    assert entry.get("Actions") is None
    assert not any(g.startswith("Desktop Action") for g in entry.groups())


@pytest.mark.parametrize("filename", sorted(SAMPLE_DESKTOP))
@pytest.mark.parametrize("variant", ["plain", "all-options"])
def test_every_sample_validates(tmp_path, filename, variant):
    stem = filename[: -len(".desktop")]
    options = {}
    if variant == "all-options":
        options = dict(path=SPACED, extra_args=("--no-sandbox",), extract_and_run=True,
                       uninstall_command=("/usr/bin/easy-installer", "--uninstall", stem), scope=Scope.SYSTEM,
                       icon_name=None)
    text = render_desktop_entry(spec_for(SAMPLE_DESKTOP[filename], app_id=stem, stem=stem, **options))
    validate(text, tmp_path, f"easyinstaller-{stem}.desktop")
    entry = DesktopEntry.parse(text)
    args = split_exec(entry.get("Exec"))
    if variant == "all-options":
        assert args[:3] == ["env", "APPIMAGE_EXTRACT_AND_RUN=1", str(SPACED)]
        assert args.count("--no-sandbox") == 1
        assert entry.get("Icon") == "application-x-executable"
    else:
        assert args[0] == str(TARGET)
    for key in ("NoDisplay", "Hidden", "DBusActivatable"):
        assert entry.get(key) is None


def test_path_with_spaces_and_special_characters(tmp_path):
    path = Path('/home/tester/My "Apps"/100% $weird`name`.AppImage')
    text = render_desktop_entry(spec_for(T3_DESKTOP, path=path, extra_args=("--no-sandbox",)))
    validate(text, tmp_path)
    entry = DesktopEntry.parse(text)
    assert split_exec(entry.get("Exec")) == ["env", str(path), "--no-sandbox", "%U"]
    assert entry.get("TryExec") == str(path)


def test_without_embedded_entry(tmp_path):
    text = render_desktop_entry(spec_for(None, stem=None, version=None, icon_name=None))
    validate(text, tmp_path)
    entry = DesktopEntry.parse(text)
    assert entry.groups() == [MAIN]
    assert entry.get("Type") == "Application"
    assert entry.get("Name") == "Fallback Name"
    assert entry.get("Comment") == "Fallback comment"
    assert entry.get("Exec") == str(TARGET)          # no %U appended
    assert entry.get("Icon") == "application-x-executable"
    assert entry.get_list("Categories") == ["Utility"]
    assert entry.get("StartupWMClass") is None
    assert entry.get("X-AppImage-Version") is None


def test_embedded_without_exec_and_main_group_problems(tmp_path):
    text = "[X-First]\nFoo=bar\n[Desktop Entry]\nName=NoExec\nType=Application\n[Custom Group]\nA=1\n"
    out = render_desktop_entry(spec_for(text, extra_args=("--no-sandbox",), extract_and_run=True))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.groups() == [MAIN, "X-First"]
    assert entry.get("Exec") == f"env APPIMAGE_EXTRACT_AND_RUN=1 {TARGET} --no-sandbox"


def test_embedded_without_main_group_is_ignored(tmp_path):
    out = render_desktop_entry(spec_for("[Something]\nName=x\n"))
    validate(out, tmp_path)
    assert DesktopEntry.parse(out).get("Name") == "Fallback Name"


def test_removed_and_defaulted_keys(tmp_path):
    text = (
        "[Desktop Entry]\nType=Application\nName=Thing\nExec=thing %U\nNoDisplay=true\nHidden=false\n"
        "DBusActivatable=true\nX-AppImage-Integrate=false\nX-AppImageLauncher-Version=1\n"
        "X-GNOME-Autostart-enabled=true\nX-GNOME-AutostartDelay=5\nImplements=org.example.X;\n"
        "TryExec=thing\nVersion=0.9.0\nComment=\nX-Keep-Me=yes\nPath=/tmp\nKeywords[de]=Ding;\n"
        "this is junk\nBad_Key=1\n"
    )
    out = render_desktop_entry(spec_for(text))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    for key in ("NoDisplay", "Hidden", "DBusActivatable", "X-AppImage-Integrate", "X-AppImageLauncher-Version",
                "X-GNOME-Autostart-enabled", "X-GNOME-AutostartDelay", "Implements", "Version", "Bad_Key"):
        assert entry.get(key) is None, key
    assert entry.get("TryExec") == str(TARGET)
    assert entry.get("Comment") == "Fallback comment"
    assert entry.get_list("Categories") == ["Utility"]
    assert entry.get("StartupWMClass") == "t3code"
    assert entry.get("X-Keep-Me") == "yes"
    assert entry.get("Path") == "/tmp"
    assert entry.get("Keywords[de]") is None   # localized key without "Keywords" is invalid
    assert "junk" not in out


def test_actions_are_rewritten_or_dropped(tmp_path):
    text = (
        "[Desktop Entry]\nType=Application\nName=Multi\nExec=AppRun %U\nIcon=multi\n"
        "Actions=new-window;private;host-tool;broken;missing-group;no-exec;\n\n"
        "[Desktop Action new-window]\nName=New Window\nExec=AppRun --new-window\nIcon=multi\n\n"
        "[Desktop Action private]\nName=Private\nExec=usr/bin/multi --private %u\nIcon=other-icon\n\n"
        "[Desktop Action host-tool]\nName=Host\nExec=/usr/bin/gnome-calculator\n\n"
        "[Desktop Action broken]\nName=Broken\nExec=AppRun \"unbalanced\n\n"
        "[Desktop Action no-exec]\nName=No Exec\n\n"
        "[Desktop Action unlisted]\nName=Unlisted\nExec=AppRun --unlisted\n"
    )
    out = render_desktop_entry(spec_for(text, extra_args=("--no-sandbox",), extract_and_run=True,
                                        uninstall_command=UNINSTALL))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get_list("Actions") == ["new-window", "private", UNINSTALL_ACTION]
    assert entry.groups() == [MAIN, "Desktop Action new-window", "Desktop Action private", UNINSTALL_GROUP]
    assert split_exec(entry.get("Exec", "Desktop Action new-window")) == [
        "env", "APPIMAGE_EXTRACT_AND_RUN=1", str(TARGET), "--no-sandbox", "--new-window"]
    assert entry.get("Exec", "Desktop Action private") == \
        f"env APPIMAGE_EXTRACT_AND_RUN=1 {TARGET} --no-sandbox --private %u"
    assert entry.get("Icon", "Desktop Action new-window") == "easyinstaller-t3code"   # was the main icon
    assert entry.get("Icon", "Desktop Action private") == "other-icon"


def test_existing_uninstall_action_is_replaced_not_duplicated(tmp_path):
    first = render_desktop_entry(spec_for(T3_DESKTOP, uninstall_command=UNINSTALL))
    second = render_desktop_entry(spec_for(first, uninstall_command=("/usr/local/bin/easy-installer",
                                                                     "--uninstall", "t3code")))
    validate(second, tmp_path)
    entry = DesktopEntry.parse(second)
    assert entry.get_list("Actions") == [UNINSTALL_ACTION]
    assert entry.groups().count(UNINSTALL_GROUP) == 1
    assert entry.get("Exec", UNINSTALL_GROUP).startswith("/usr/local/bin/easy-installer")
    third = DesktopEntry.parse(render_desktop_entry(spec_for(second)))
    assert third.get("Actions") is None and not third.has_group(UNINSTALL_GROUP)


def test_rerender_is_stable(tmp_path):
    spec = spec_for(T3_DESKTOP, extra_args=("--no-sandbox",), uninstall_command=UNINSTALL)
    once = render_desktop_entry(spec)
    twice = render_desktop_entry(spec_for(once, extra_args=("--no-sandbox",), uninstall_command=UNINSTALL))
    assert once == twice


def test_orphan_localized_keys_are_dropped(tmp_path):
    text = ("[Desktop Entry]\nName=A\nExec=a\nGenericName[de]=Programm\nKeywords=k;\nKeywords[de]=d;\n"
            "Actions=x;\n[Desktop Action x]\nName=X\nName[de]=Ix\nExec=a --x\nComment[de]=nur deutsch\n")
    out = render_desktop_entry(spec_for(text, comment=None))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get("GenericName[de]") is None
    assert entry.get("Keywords[de]") == "d;"
    assert entry.get("Name[de]", "Desktop Action x") == "Ix"
    assert entry.get("Comment[de]", "Desktop Action x") is None


@pytest.mark.parametrize("text", [
    "[Desktop Entry]\nType=Link\nName=L\nURL=https://example.org\n",
    "[Desktop Entry]\nType=Directory\nName=D\n",
    "[Desktop Entry]\nName=N\nExec=x\nOnlyShowIn=KDE;\nNotShowIn=GNOME;\n",
])
def test_other_types_and_visibility_restrictions(tmp_path, text):
    out = render_desktop_entry(spec_for(text))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get("Type") == "Application"
    for key in ("URL", "OnlyShowIn", "NotShowIn"):
        assert entry.get(key) is None


def test_empty_name_uses_spec_name_and_scope_string(tmp_path):
    out = render_desktop_entry(spec_for("[Desktop Entry]\nName=\nExec=x\n", scope="system"))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get("Name") == "Fallback Name"
    assert entry.get("X-EasyInstaller-Scope") == "system"


# ---------------------------------------------------------------------------------------------
# launchers GLib can read (what the app menu uses) and nothing that runs other code
# ---------------------------------------------------------------------------------------------


def glib_keyfile(text: str):
    """Load ``text`` like GIO does; raises GLib.Error if the app menu would reject it."""
    gi = pytest.importorskip("gi")
    from gi.repository import GLib

    keyfile = GLib.KeyFile()
    keyfile.load_from_data(text, len(text.encode("utf-8")), GLib.KeyFileFlags.NONE)
    return keyfile


@pytest.mark.parametrize("junk", [
    "\u00a0",                     # a stray no-break space (copy-pasted from a web page)
    "\u2028", "\u0085", "\u001c", "\u3000", "\u2003",
    "\u00a0Name=Indented with NBSP",
    "Comment=A\rExec=/tmp/evil-cr",  # a lone CR: universal-newline parsers see a second Exec
    # other control characters (GLib rejects some; C1 is what double-encoded text contains)
    "Comment[fr]=Outil\u2028pratique", "Comment=a\x0cb", "Comment=a\x01b", "Comment=a\x7fb",
    "Comment=Mojibake \u00e2\x80\x99", "Comment[es]=nul\x00", "# comment\x1f",
    # locale suffixes GLib rejects (it then refuses the whole file)
    "Name[x:y]=O", "Name[en,de]=O", "Name[de+x]=O", "Name[de/x]=O",
])
def test_unicode_whitespace_lines_are_dropped(junk, tmp_path):
    text = f"[Desktop Entry]\nType=Application\nName=Foo\n{junk}\nExec=foo %U\nIcon=foo\n"
    out = render_desktop_entry(spec_for(text))
    keyfile = glib_keyfile(out)
    assert keyfile.get_string(MAIN, "Name") == "Foo"
    assert keyfile.get_string(MAIN, "Exec") == f"{TARGET} %U"
    assert junk not in out and "\r" not in out
    validate(out, tmp_path)


@pytest.mark.parametrize("group", [
    "X-Foo\tBar", "X-Foo\rBar", "X-Foo\x1cBar", "X-Foo\u2028Bar", "X-Foo\x01Bar", "X-Foo\x7fBar",
    "X-Foo\x85Bar", "Desktop Action a\x0bb",
])
def test_groups_whose_names_glib_rejects_are_dropped(group, tmp_path):
    """GLib refuses the whole file for a group name with a control character (the app would be
    missing from the menu), and other parsers split a line at U+2028 & co."""
    text = (f"[Desktop Entry]\nType=Application\nName=Foo\nExec=foo %U\nActions=a\x0bb;\n\n"
            f"[{group}]\nName=A\nExec=foo --a\n")
    out = render_desktop_entry(spec_for(text))
    glib_keyfile(out)
    assert group not in out
    assert DesktopEntry.parse(out).groups() == [MAIN]
    validate(out, tmp_path)


def test_unicode_space_after_group_header_is_not_a_group():
    """GLib only allows spaces/tabs after "]", so such a file has no [Desktop Entry] at all."""
    entry = DesktopEntry.parse("[Desktop Entry]\u00a0\nName=Foo\nExec=foo\n")
    assert not entry.has_group(MAIN)
    assert DesktopEntry.parse("  [Desktop Entry] \t\n Name=Foo\n").get("Name") == "Foo"


@pytest.mark.parametrize("raw, shown", [
    ("Foo\\xBar", "Foo\\xBar"),
    ("A\\;B", "A\\;B"),
    ("Trailing\\", "Trailing\\"),
    ("C:\\Program", "C:\\Program"),
    ("Foo\\sBar", "Foo Bar"),
])
def test_unknown_escapes_become_literal_backslashes(raw, shown, tmp_path):
    text = (f"[Desktop Entry]\nType=Application\nName={raw}\nName[de]={raw}\nExec=foo\n"
            f"Keywords=a\\;b;c;\nActions=x;\n\n[Desktop Action x]\nName={raw}\nExec=foo --x\n")
    out = render_desktop_entry(spec_for(text))
    keyfile = glib_keyfile(out)
    assert keyfile.get_string(MAIN, "Name") == shown
    assert keyfile.get_locale_string(MAIN, "Name", "de") == shown
    assert keyfile.get_string("Desktop Action x", "Name") == shown
    assert keyfile.get_string_list(MAIN, "Keywords") == ["a;b", "c"]
    assert DesktopEntry.parse(out).get("Name") == shown


def test_localized_exec_tryexec_and_icon_are_removed(tmp_path):
    text = ("[Desktop Entry]\nType=Application\nName=Foo\nExec=foo %U\nExec[de]=/tmp/evil-de\n"
            "TryExec=foo\nTryExec[de]=/tmp/evil\nIcon=foo\nIcon[de]=foo-de\n"
            "Icon[nl]=/usr/share/foo.png\nActions=x;\n\n[Desktop Action x]\nName=X\n"
            "Exec=foo --x\nExec[de]=/tmp/evil\nIcon=foo\nIcon[de]=/tmp/evil.svg\n"
            "\n[X-Foo Extra]\nName=Something\nIcon=foo\nIcon[de]=foo-de\nExec=x\nExec[de]=y\n"
            "TryExec=x\nTryExec[nl]=z\n")
    out = render_desktop_entry(spec_for(text))
    validate(out, tmp_path)
    keyfile = glib_keyfile(out)
    for locale in ("de", "nl", "C"):
        assert keyfile.get_locale_string(MAIN, "Icon", locale) == "easyinstaller-t3code"
        assert keyfile.get_locale_string(MAIN, "Exec", locale) == f"{TARGET} %U"
    entry = DesktopEntry.parse(out)
    for group in entry.groups():
        assert not [k for k in entry.keys(group) if k.startswith(("Exec[", "TryExec[", "Icon["))]
    assert entry.get("Icon", "Desktop Action x") == "easyinstaller-t3code"
    assert entry.keys("X-Foo Extra") == ["Name", "Icon", "Exec", "TryExec"]


def test_actions_with_the_main_programs_absolute_path_are_kept(tmp_path):
    """Repacked .deb AppImages: the main Exec (taken to be the app) often is an absolute path."""
    text = ("[Desktop Entry]\nType=Application\nName=Brave\nExec=/usr/bin/brave-browser-stable %U\n"
            "Actions=new-window;new-private-window;other;\n\n"
            "[Desktop Action new-window]\nName=New Window\nExec=/usr/bin/brave-browser-stable\n\n"
            "[Desktop Action new-private-window]\nName=New Private Window\n"
            "Exec=/usr/bin/brave-browser-stable --incognito\n\n"
            "[Desktop Action other]\nName=Other\nExec=/usr/bin/xdg-open https://example.org\n")
    out = render_desktop_entry(spec_for(text, stem="brave-browser"))
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get("Exec") == f"{TARGET} %U"
    assert entry.get_list("Actions") == ["new-window", "new-private-window"]
    assert entry.get("Exec", "Desktop Action new-private-window") == f"{TARGET} --incognito"
    assert "xdg-open" not in out


def test_actions_running_other_host_programs_are_dropped(tmp_path):
    text = (
        "[Desktop Entry]\nType=Application\nName=Foo\nExec=foo %U\n"
        "Actions=help;shell;new;same;apprun;relative;\n\n"
        "[Desktop Action help]\nName=Online Help\nExec=xdg-open https://example.org/help\n\n"
        "[Desktop Action shell]\nName=Shell\nExec=sh -c \"echo hi\"\n\n"
        "[Desktop Action new]\nName=New\nExec=t3code --new-window\n\n"
        "[Desktop Action same]\nName=Same\nExec=foo --same\n\n"
        "[Desktop Action apprun]\nName=AppRun\nExec=AppRun --x\n\n"
        "[Desktop Action relative]\nName=Relative\nExec=usr/bin/foo --rel\n"
    )
    out = render_desktop_entry(spec_for(text))  # the embedded desktop file is "t3code.desktop"
    validate(out, tmp_path)
    entry = DesktopEntry.parse(out)
    assert entry.get_list("Actions") == ["new", "same", "apprun", "relative"]
    assert "xdg-open" not in out and "echo hi" not in out
    assert entry.get("Exec", "Desktop Action new") == f"{TARGET} --new-window"


def test_embedded_env_only_keeps_harmless_variables(tmp_path):
    text = ("[Desktop Entry]\nType=Application\nName=Foo\n"
            "Exec=env LD_PRELOAD=/tmp/evil.so QT_QPA_PLATFORM=xcb LD_LIBRARY_PATH=lib "
            "GDK_BACKEND=/tmp/x PYTHONPATH=x AppRun %U\n")
    out = render_desktop_entry(spec_for(text, extract_and_run=True))
    validate(out, tmp_path)
    assert DesktopEntry.parse(out).get("Exec") == \
        f"env QT_QPA_PLATFORM=xcb APPIMAGE_EXTRACT_AND_RUN=1 {TARGET} %U"


# ---------------------------------------------------------------------------------------------
# MIME packages
# ---------------------------------------------------------------------------------------------


def test_parse_mime_package_of_real_samples():
    from fakeappimage import FREECAD_MIME_XML, OPENSCAD_MIME_XML
    from easy_installer.core.integration import MimeTypeDef, parse_mime_package

    assert parse_mime_package(OPENSCAD_MIME_XML) == [
        MimeTypeDef("application/x-openscad", "OpenSCAD Model", ("*.scad",))]
    assert parse_mime_package(FREECAD_MIME_XML) == [
        MimeTypeDef("application/x-extension-fcstd", "FreeCAD document files", ("*.fcstd",))]


def test_parse_mime_package_is_strict_and_safe():
    from easy_installer.core.integration import parse_mime_package

    ns = 'xmlns="http://www.freedesktop.org/standards/shared-mime-info"'
    text = (f'<mime-info {ns}>'
            '<mime-type type="application/x-a"><comment xml:lang="de">Deutsch</comment>'
            '<comment>A files</comment><glob pattern="*"/><glob pattern="*.a"/>'
            '<glob pattern="README"/><glob pattern="*.B" case-sensitive="true"/>'
            '<sub-class-of type="text/plain"/><magic><match type="string" value="A"/></magic>'
            '</mime-type><mime-type type="not a type"><glob pattern="*.x"/></mime-type>'
            '</mime-info>')
    [definition] = parse_mime_package(text)
    assert definition.type == "application/x-a" and definition.comment == "A files"
    assert definition.globs == ("*.a",) and definition.sub_class_of == ("text/plain",)
    for bad in ("", "<mime-info", "not xml at all",
                '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                f'<mime-info {ns}><mime-type type="a/b"><comment>&e;</comment></mime-type></mime-info>'):
        assert parse_mime_package(bad) == []


@pytest.mark.parametrize("mime", [
    "packages/easyinstaller-victim",   # would overwrite <mime_dir>/packages/easyinstaller-victim.xml
    "mime.cache/x",                    # would make <mime_dir>/mime.cache a folder
    "types/x", "globs2/x", "inode/directory", "x-scheme-handler/foo", "x-content/image-dcf",
    "x-epoc/x-sisx-app", "weird/x-weird", "Application/x-a", "APPLICATION/x-a",
])
def test_mime_types_must_be_real_media_types(mime):
    """update-mime-database writes <mime_dir>/<media>/<subtype>.xml for every type (as root)."""
    from easy_installer.core.integration import MimeTypeDef, parse_mime_package

    with pytest.raises(ValueError):
        MimeTypeDef.from_dict({"type": mime, "globs": ["*.zzqq"]})
    with pytest.raises(ValueError):
        MimeTypeDef.from_dict({"type": "application/x-ok", "globs": ["*.zzqq"],
                               "sub_class_of": [mime]})
    ns = 'xmlns="http://www.freedesktop.org/standards/shared-mime-info"'
    assert parse_mime_package(f'<mime-info {ns}><mime-type type="{mime}">'
                              '<glob pattern="*.zzqq"/></mime-type></mime-info>') == []


@pytest.mark.parametrize("mime", [
    "application/x-extension-fcstd", "chemical/x-pdb", "model/x-openscad", "font/x-foo",
    "text/x-foo", "image/x-foo", "audio/x-foo", "video/x-foo", "message/x-foo",
    "multipart/x-foo", "application/vnd.ms-excel.sheet.macroEnabled.12",
])
def test_real_media_types_are_accepted(mime):
    from easy_installer.core.integration import MimeTypeDef

    assert MimeTypeDef.from_dict({"type": mime, "globs": ["*.zzqq"],
                                  "sub_class_of": ["text/plain"]}).type == mime


def test_select_mime_types_only_adds_what_the_host_lacks():
    from easy_installer.core.integration import HostMimeDatabase, MimeTypeDef, select_mime_types

    defs = [MimeTypeDef("application/x-openscad", "OpenSCAD Model", ("*.scad",)),
            MimeTypeDef("application/x-new", None, ("*.new", "*.txt")),
            MimeTypeDef("application/x-unused", None, ("*.unused",)),
            MimeTypeDef("application/x-clash", None, ("*.TXT",))]
    host = HostMimeDatabase(types=frozenset({"application/x-openscad"}),
                            globs=frozenset({"*.txt"}))
    wanted = ["application/x-openscad", "application/x-new", "application/x-clash",
              "x-scheme-handler/x-new"]
    assert select_mime_types(defs, wanted, host) == [
        MimeTypeDef("application/x-new", None, ("*.new",))]


def test_select_mime_types_never_takes_over_files_a_host_pattern_matches():
    """xdgmime prefers simple "*.ext" globs over full globs, and longer suffixes over shorter
    ones: "*.1" would take man pages from "*.[1-9]", "*.min.js" JavaScript from "*.js"."""
    from easy_installer.core.integration import HostMimeDatabase, MimeTypeDef, select_mime_types

    host = HostMimeDatabase(globs=frozenset({
        "*.[1-9]", "*.so.[0-9]*", "*.anim[1-9j]", "[0-9][0-9][0-9].vdr", "*.js", "*.gz",
        "readme*", "makefile.*", "todo.txt"}))
    taken = ("*.1", "*.8", "*.so.6", "*.so.1", "*.anim3", "*.vdr", "*.min.js", "*.JS", "*.tar.gz")
    free = ("*.zzqq", "*.fcstd", "*.txt", "*.jsx", "*.gzip", "*.10")
    defs = [MimeTypeDef("application/x-hostile", None, taken[:8]),
            MimeTypeDef("application/x-fine", None, taken[8:] + free[:5]),
            MimeTypeDef("application/x-ten", None, free[5:])]
    wanted = ["application/x-hostile", "application/x-fine", "application/x-ten"]
    assert select_mime_types(defs, wanted, host) == [
        MimeTypeDef("application/x-fine", None, free[:5]),
        MimeTypeDef("application/x-ten", None, free[5:])]


def test_select_mime_types_with_the_real_host_database():
    from easy_installer.core.integration import (
        HOST_MIME_DIR, MimeTypeDef, host_mime_database, select_mime_types)

    if not (HOST_MIME_DIR / "globs2").is_file():
        pytest.skip("no shared-mime-info database on this computer")
    defs = [MimeTypeDef("application/x-hostile", None,
                        ("*.1", "*.8", "*.so.6", "*.min.js", "*.tar.gz", "*.txt", "*.zzqq"))]
    assert select_mime_types(defs, ["application/x-hostile"], host_mime_database()) == [
        MimeTypeDef("application/x-hostile", None, ("*.zzqq",))]


def test_render_mime_package_escapes_and_is_readable_by_update_mime_database(tmp_path):
    from easy_installer.core.integration import (
        MimeTypeDef, parse_mime_package, render_mime_package)

    defs = [MimeTypeDef("application/x-new", 'A <"new"> & odd type', ("*.new",), ("text/plain",))]
    text = render_mime_package("demo", defs)
    assert parse_mime_package(text) == defs
    tool = shutil.which("update-mime-database")
    if tool:
        (tmp_path / "mime" / "packages").mkdir(parents=True)
        (tmp_path / "mime" / "packages" / "easyinstaller-demo.xml").write_text(text)
        proc = subprocess.run([tool, str(tmp_path / "mime")], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "application/x-new" in (tmp_path / "mime" / "types").read_text()
