from __future__ import annotations

import random

import pytest

from easy_installer.core.desktop_entry import (
    FIELD_CODES,
    DesktopEntry,
    default_locales,
    escape_value,
    exec_program,
    is_valid_key,
    join_exec,
    locale_variants,
    rewrite_exec,
    split_exec,
    unescape_value,
)

from fakeappimage import FREECAD_DESKTOP, OPENSCAD_DESKTOP, SAMPLE_DESKTOP, T3_DESKTOP

MAIN = DesktopEntry.MAIN

# =============================================================================================
# value escaping
# =============================================================================================


@pytest.mark.parametrize("plain, escaped", [
    ("simple", "simple"),
    ("inner spaces stay", "inner spaces stay"),
    (" leading", "\\sleading"),
    ("   three", "\\s\\s\\sthree"),
    ("trailing ", "trailing\\s"),
    ("line\nbreak", "line\\nbreak"),
    ("tab\there", "tab\\there"),
    ("cr\rhere", "cr\\rhere"),
    ("back\\slash", "back\\\\slash"),
    ("semi;colon", "semi;colon"),
    ("\\n literally", "\\\\n literally"),
    ("ünïcødé ✓", "ünïcødé ✓"),
    ("", ""),
])
def test_escape_value(plain, escaped):
    assert escape_value(plain) == escaped
    assert unescape_value(escaped) == plain


@pytest.mark.parametrize("raw, plain", [
    ("\\s", " "),
    ("a\\sb", "a b"),
    ("\\n\\t\\r\\\\", "\n\t\r\\"),
    ("\\\\s", "\\s"),           # escaped backslash followed by "s"
    ("\\x", "\\x"),             # unknown escapes are kept verbatim
    ("\\;", "\\;"),             # "\;" only means something in lists
    ("end\\", "end\\"),         # lone trailing backslash
    ("\\\\\\n", "\\\n"),
])
def test_unescape_value(raw, plain):
    assert unescape_value(raw) == plain


@pytest.mark.parametrize("text", [
    "", " ", "  x  ", "\t\n\r", "a\\b", "\\\\", "\\n", "a;b", "ends with backslash \\", "ümlaut ✓",
    "\\s literal", " \\ ", "x\n y\n",
])
def test_escape_round_trip(text):
    assert unescape_value(escape_value(text)) == text
    entry = DesktopEntry.new()
    entry.set("X-Test", text)
    reparsed = DesktopEntry.parse(entry.to_text())
    assert reparsed.get("X-Test") == text


# =============================================================================================
# lists
# =============================================================================================


@pytest.mark.parametrize("raw, items", [
    ("a;b;c;", ["a", "b", "c"]),
    ("a;b;c", ["a", "b", "c"]),
    ("a\\;b;c;", ["a;b", "c"]),
    ("a\\\\;b;", ["a\\", "b"]),           # escaped backslash, then a real separator
    ("a\\\\\\;b;", ["a\\;b"]),
    ("with\\sspace;tab\\t;", ["with space", "tab\t"]),
    ("a;;b;", ["a", "b"]),
    ("", []),
    (";", []),
    ("single", ["single"]),
])
def test_get_list(raw, items):
    entry = DesktopEntry.parse(f"[Desktop Entry]\nKeywords={raw}\n")
    assert entry.get_list("Keywords") == items


def test_get_list_missing():
    assert DesktopEntry.new().get_list("Categories") == []


@pytest.mark.parametrize("items", [
    ["Development"],
    ["a;b", "c\\d", " lead", "trail ", "multi\nline"],
    ["x-scheme-handler/t3code", "x-scheme-handler/t3code-dev"],
    [],
])
def test_set_list_round_trip(items):
    entry = DesktopEntry.new()
    entry.set_list("MimeType", items)
    raw = entry.get_raw("MimeType")
    assert raw == "" if not items else raw.endswith(";")
    assert DesktopEntry.parse(entry.to_text()).get_list("MimeType") == items


def test_set_list_format():
    entry = DesktopEntry.new()
    entry.set_list("Categories", ["Graphics", "3DGraphics", "semi;colon"])
    assert entry.get_raw("Categories") == "Graphics;3DGraphics;semi\\;colon;"


# =============================================================================================
# round trip fidelity
# =============================================================================================

MESSY = """\
# Preamble comment
  # indented comment

[Desktop Entry]
# comment inside group
Type=Application
Name = Spaced Out
Name[de]=Deutsch
Name[sr@latin]=Latinica
Exec=app --flag="a b" %U
this line is junk
Categories=A;B;

Comment=first
Comment=second wins
X-Weird_Key.Name=kept
Icon=icon  \t

[Desktop Action new-window]
Name=New Window
Exec=app --new-window

[X-Custom Group]
Key=Value
[Desktop Entry]
Duplicate=Group
"""


@pytest.mark.parametrize("text", [MESSY, *SAMPLE_DESKTOP.values(), "[Desktop Entry]\n", "# only a comment\n"])
def test_round_trip_identical(text):
    expected = text if text.endswith("\n") else text + "\n"
    assert DesktopEntry.parse(text).to_text() == expected


def test_round_trip_crlf_and_bom():
    lf = DesktopEntry.parse(MESSY).to_text()
    crlf = "\ufeff" + MESSY.replace("\n", "\r\n")
    assert DesktopEntry.parse(crlf).to_text() == lf


def test_missing_final_newline_is_added():
    text = "[Desktop Entry]\nName=x"
    assert DesktopEntry.parse(text).to_text() == text + "\n"


def test_empty_text():
    entry = DesktopEntry.parse("")
    assert entry.groups() == []
    assert entry.to_text() == ""


def test_edit_keeps_everything_else():
    entry = DesktopEntry.parse(MESSY)
    entry.set("Icon", "new-icon")
    expected = MESSY.replace("Icon=icon  \t\n", "Icon=new-icon\n")
    assert entry.to_text() == expected


# =============================================================================================
# parsing semantics
# =============================================================================================


def test_messy_lookups():
    entry = DesktopEntry.parse(MESSY)
    assert entry.groups() == [MAIN, "Desktop Action new-window", "X-Custom Group"]
    assert entry.has_group("X-Custom Group") and not entry.has_group("Nope")
    assert entry.get("Name") == "Spaced Out"
    assert entry.get("Comment") == "second wins"          # last one wins
    assert entry.get("Icon") == "icon  \t"                 # trailing whitespace is part of the value
    assert entry.get("Duplicate") == "Group"               # duplicate groups are merged for lookups
    assert entry.get("X-Weird_Key.Name") == "kept"
    assert entry.get("Exec", "Desktop Action new-window") == "app --new-window"
    assert entry.get("Key", "X-Custom Group") == "Value"
    assert entry.get("Missing") is None
    assert entry.get("Name", "Nope") is None
    keys = entry.keys()
    assert keys[:4] == ["Type", "Name", "Name[de]", "Name[sr@latin]"]
    assert keys.count("Comment") == 1
    assert "this line is junk" not in " ".join(keys)
    assert entry.keys("Nope") == []


def test_keys_before_first_group_are_ignored():
    entry = DesktopEntry.parse("Name=outside\n[Desktop Entry]\nType=Application\n")
    assert entry.get("Name") is None
    assert entry.keys() == ["Type"]


def test_group_header_with_trailing_whitespace_and_indented_keys():
    entry = DesktopEntry.parse("[Desktop Entry]  \n   Name=Indented\n")
    assert entry.has_group(MAIN)
    assert entry.get("Name") == "Indented"


def test_get_raw_vs_get():
    entry = DesktopEntry.parse("[Desktop Entry]\nComment=\\sHello\\nWorld\n")
    assert entry.get_raw("Comment") == "\\sHello\\nWorld"
    assert entry.get("Comment") == " Hello\nWorld"


@pytest.mark.parametrize("raw, expected", [
    ("true", True), ("True", True), ("1", True), ("false", False), ("0", False),
    ("FALSE", False), ("yes", None), ("", None),
])
def test_get_bool(raw, expected):
    entry = DesktopEntry.parse(f"[Desktop Entry]\nTerminal={raw}\n")
    if expected is None:
        assert entry.get_bool("Terminal") is False
        assert entry.get_bool("Terminal", default=True) is True
    else:
        assert entry.get_bool("Terminal") is expected
    assert DesktopEntry.new().get_bool("Terminal", default=True) is True


def test_sample_values():
    t3 = DesktopEntry.parse(T3_DESKTOP)
    assert t3.get("Name") == "T3 Code (Alpha)"
    assert t3.get("Exec") == "AppRun --no-sandbox %U"
    assert t3.get_list("MimeType") == ["x-scheme-handler/t3code", "x-scheme-handler/t3code-dev"]
    assert t3.get_bool("Terminal") is False
    scad = DesktopEntry.parse(OPENSCAD_DESKTOP)
    assert scad.get_list("Keywords") == ["3d", "solid", "geometry", "csg", "model", "stl"]
    assert scad.get("X-AppImage-Version") == "2026.03.28"


# =============================================================================================
# localization
# =============================================================================================

LOCALIZED = """\
[Desktop Entry]
Name=Plain
Name[de]=Deutsch
Name[de_AT]=Österreichisch
Name[sr]=Srpski
Name[sr@latin]=Srpski latinica
Name[sr_RS]=Srpski Srbija
Name[sr_RS@latin]=Srpski Srbija latinica
Comment=Plain comment
"""


@pytest.mark.parametrize("locales, expected", [
    (["de"], "Deutsch"),
    (["de_DE"], "Deutsch"),
    (["de_DE.UTF-8"], "Deutsch"),
    (["de_AT.UTF-8"], "Österreichisch"),
    (["sr_RS@latin"], "Srpski Srbija latinica"),
    (["sr_RS.UTF-8@latin"], "Srpski Srbija latinica"),
    (["sr_ME@latin"], "Srpski latinica"),
    (["sr_RS"], "Srpski Srbija"),
    (["sr@latin"], "Srpski latinica"),
    (["sr_ME"], "Srpski"),
    (["fr_FR"], "Plain"),
    (["fr", "de"], "Deutsch"),
    ([], "Plain"),
    (["C"], "Plain"),
])
def test_get_localized(locales, expected):
    assert DesktopEntry.parse(LOCALIZED).get_localized("Name", locales) == expected


def test_get_localized_falls_back_to_unlocalized_and_none():
    entry = DesktopEntry.parse(LOCALIZED)
    assert entry.get_localized("Comment", ["de"]) == "Plain comment"
    assert entry.get_localized("GenericName", ["de"]) is None


def test_get_localized_real_freecad():
    entry = DesktopEntry.parse(FREECAD_DESKTOP)
    assert entry.get_localized("Comment", ["de_DE.UTF-8"]) == "Feature-basierter parametrischer Modellierer"
    assert entry.get_localized("GenericName", ["ko_KR.UTF-8"]) == "CAD 응용프로그램"
    assert entry.get_localized("Comment", ["nl_NL.UTF-8"]) == "Feature based Parametric Modeler"


@pytest.mark.parametrize("name, variants", [
    ("de", ["de"]),
    ("de_DE", ["de_DE", "de"]),
    ("de_DE.UTF-8", ["de_DE", "de"]),
    ("sr@latin", ["sr@latin", "sr"]),
    ("sr_RS.UTF-8@latin", ["sr_RS@latin", "sr_RS", "sr@latin", "sr"]),
    ("", []),
    ("not a locale!", []),
])
def test_locale_variants(name, variants):
    assert locale_variants(name) == variants


@pytest.mark.parametrize("env, expected", [
    ({"LANG": "de_DE.UTF-8"}, ["de_DE.UTF-8"]),
    ({"LANG": "de_DE.UTF-8", "LC_MESSAGES": "nl_NL.UTF-8"}, ["nl_NL.UTF-8"]),
    ({"LANG": "de_DE.UTF-8", "LC_ALL": "fr_FR.UTF-8"}, ["fr_FR.UTF-8"]),
    ({"LANG": "de_DE.UTF-8", "LANGUAGE": "nl:en"}, ["nl", "en", "de_DE.UTF-8"]),
    ({"LANG": "C", "LANGUAGE": "nl"}, []),        # gettext ignores LANGUAGE in the C locale
    ({"LANG": "C.UTF-8"}, []),
    ({}, []),
])
def test_default_locales(env, expected):
    assert default_locales(env) == expected


def test_get_localized_uses_environment(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "de_AT.UTF-8")
    assert DesktopEntry.parse(LOCALIZED).get_localized("Name") == "Österreichisch"
    monkeypatch.setenv("LANG", "C")
    assert DesktopEntry.parse(LOCALIZED).get_localized("Name") == "Plain"


# =============================================================================================
# modification
# =============================================================================================


def test_new():
    entry = DesktopEntry.new()
    assert entry.groups() == [MAIN]
    assert entry.to_text() == "[Desktop Entry]\n"


def test_set_replaces_in_place_and_escapes():
    entry = DesktopEntry.parse("[Desktop Entry]\nName=Old\nType=Application\n")
    entry.set("Name", " New\nName")
    assert entry.to_text() == "[Desktop Entry]\nName=\\sNew\\nName\nType=Application\n"


def test_set_appends_after_last_key_of_group():
    text = "[Desktop Entry]\nName=A\n# trailing comment\n\n[Desktop Action x]\nName=X\n"
    entry = DesktopEntry.parse(text)
    entry.set("Icon", "i")
    entry.set("Exec", "run", "Desktop Action x")
    assert entry.to_text() == (
        "[Desktop Entry]\nName=A\nIcon=i\n# trailing comment\n\n"
        "[Desktop Action x]\nName=X\nExec=run\n"
    )


def test_set_in_empty_group_goes_right_after_header():
    entry = DesktopEntry.parse("[Desktop Entry]\n\n# c\n")
    entry.set("Type", "Application")
    assert entry.to_text() == "[Desktop Entry]\nType=Application\n\n# c\n"


def test_set_removes_duplicates():
    entry = DesktopEntry.parse("[Desktop Entry]\nName=1\nName=2\n[Desktop Entry]\nName=3\n")
    entry.set("Name", "only")
    assert entry.get("Name") == "only"
    assert entry.to_text().count("Name=") == 1


def test_set_creates_missing_group_separated_by_blank_line():
    entry = DesktopEntry.parse("[Desktop Entry]\nName=A\n")
    entry.set("Name", "Uninstall", "Desktop Action remove")
    assert entry.to_text() == "[Desktop Entry]\nName=A\n\n[Desktop Action remove]\nName=Uninstall\n"


def test_set_localized_key():
    entry = DesktopEntry.new()
    entry.set("Name[de]", "Hallo")
    assert entry.get_localized("Name", ["de"]) == "Hallo"


@pytest.mark.parametrize("key", ["", "A=B", "Line\nBreak", " Name", "#Comment", "[Group]"])
def test_invalid_keys_rejected(key):
    with pytest.raises(ValueError):
        DesktopEntry.new().set(key, "x")


def test_set_raw_rejects_line_breaks():
    with pytest.raises(ValueError):
        DesktopEntry.new().set_raw("Name", "a\nb")
    entry = DesktopEntry.new()
    entry.set_raw("Name", "Raw\\sValue")
    assert entry.get("Name") == "Raw Value"


def test_invalid_group_rejected():
    for group in ("", "a]b", "x\ny"):
        with pytest.raises(ValueError):
            DesktopEntry.new().add_group(group)


def test_remove():
    entry = DesktopEntry.parse(LOCALIZED)
    entry.remove("Name")
    assert entry.get("Name") is None
    assert entry.get("Name[de]") == "Deutsch"
    entry.remove("Name", localized_too=True)
    assert [k for k in entry.keys() if k.startswith("Name")] == []
    assert entry.get("Comment") == "Plain comment"
    entry.remove("DoesNotExist")
    entry.remove("Comment", "No Such Group")


def test_add_and_remove_group():
    entry = DesktopEntry.new()
    entry.add_group("X-Foo")
    entry.add_group("X-Foo")  # idempotent
    assert entry.groups() == [MAIN, "X-Foo"]
    entry.remove_group("X-Foo")
    assert entry.groups() == [MAIN]
    entry.remove_group("X-Foo")


def test_move_group_to_front():
    entry = DesktopEntry.parse("[X-First]\nA=1\n[Desktop Entry]\nName=N\n")
    entry.move_group_to_front(MAIN)
    assert entry.groups() == [MAIN, "X-First"]
    assert entry.to_text() == "[Desktop Entry]\nName=N\n\n[X-First]\nA=1\n"
    entry.move_group_to_front(MAIN)
    assert entry.to_text() == "[Desktop Entry]\nName=N\n\n[X-First]\nA=1\n"


def test_remove_invalid_lines():
    entry = DesktopEntry.parse("junk before\n# ok\n[Desktop Entry]\nName=A\ngarbage line\nBad_Key=1\nName[de]=B\n")
    entry.remove_invalid_lines()
    assert entry.to_text() == "# ok\n[Desktop Entry]\nName=A\nName[de]=B\n"


@pytest.mark.parametrize("key, valid", [
    ("Name", True), ("Name[de]", True), ("Name[sr@latin]", True), ("X-AppImage-Version", True),
    ("Bad_Key", False), ("Dot.Key", False), ("", False), ("Name[]", False),
    ("Name[de_DE.UTF-8@euro]", True), ("Name[x:y]", False), ("Name[en,de]", False),
    ("Name[de+x]", False), ("Name[de/x]", False),
])
def test_is_valid_key(key, valid):
    assert is_valid_key(key) is valid


def test_copy_is_independent():
    entry = DesktopEntry.parse(T3_DESKTOP)
    clone = entry.copy()
    clone.set("Name", "Changed")
    clone.add_group("X-New")
    assert entry.get("Name") == "T3 Code (Alpha)"
    assert entry.to_text() == T3_DESKTOP


# =============================================================================================
# Exec: split / join
# =============================================================================================


@pytest.mark.parametrize("value, args", [
    ("AppRun --no-sandbox %U", ["AppRun", "--no-sandbox", "%U"]),
    ("openscad %f", ["openscad", "%f"]),
    ("AppRun - --single-instance %F", ["AppRun", "-", "--single-instance", "%F"]),
    ("env FOO=1 app %U", ["env", "FOO=1", "app", "%U"]),
    ('"/home/u/My Apps/app" %U', ["/home/u/My Apps/app", "%U"]),
    ('foo "a \\"quoted\\" word"', ["foo", 'a "quoted" word']),
    ('foo "\\$HOME" "\\`cmd\\`" "back\\\\slash"', ["foo", "$HOME", "`cmd`", "back\\slash"]),
    ('foo "keep \\n as is"', ["foo", "keep \\n as is"]),
    ("foo 100%%", ["foo", "100%"]),
    ('foo "50%%"', ["foo", "50%"]),
    ("foo --file=%f", ["foo", "--file=%f"]),
    ("  foo   bar  ", ["foo", "bar"]),
    ("foo\tbar\nbaz", ["foo", "bar", "baz"]),
    ('foo ""', ["foo", ""]),
    ('foo "a"b"c"', ["foo", "abc"]),
    ("sh -c 'echo %% hi'", ["sh", "-c", "echo % hi"]),
    ("foo a\\ b", ["foo", "a b"]),
    ("", []),
    ("   ", []),
    ("%U", ["%U"]),
])
def test_split_exec(value, args):
    assert split_exec(value) == args


@pytest.mark.parametrize("value", ['foo "unbalanced', "foo 'single", 'a "b\\"', '"'])
def test_split_exec_unbalanced(value):
    with pytest.raises(ValueError):
        split_exec(value)


@pytest.mark.parametrize("args, value", [
    (["AppRun", "--no-sandbox", "%U"], "AppRun --no-sandbox %U"),
    (["/home/u/My Apps/T3.AppImage", "%U"], '"/home/u/My Apps/T3.AppImage" %U'),
    (["foo", "100%"], "foo 100%%"),
    (["foo", "%"], "foo %%"),
    (["foo", 'say "hi"'], 'foo "say \\"hi\\""'),
    (["foo", "$HOME", "`x`", "a\\b"], 'foo "\\$HOME" "\\`x\\`" "a\\\\b"'),
    (["foo", ""], 'foo ""'),
    (["foo", "it's"], 'foo "it\'s"'),
    (["foo", "a=b", "--opt=1", "-"], "foo a=b --opt=1 -"),
    (["foo", "%F"], "foo %F"),
    (["foo", "100% sure"], 'foo "100%% sure"'),
    ([], ""),
])
def test_join_exec(args, value):
    assert join_exec(args) == value


@pytest.mark.parametrize("char", list(" \t\n\"'\\><~|&;$*?#()`"))
def test_join_exec_quotes_every_reserved_char(char):
    arg = f"a{char}b"
    joined = join_exec(["prog", arg])
    assert joined.startswith('prog "') and joined.endswith('"')
    assert split_exec(joined) == ["prog", arg]


@pytest.mark.parametrize("code", sorted(FIELD_CODES))
def test_field_codes_stay_separate_unquoted_tokens(code):
    assert join_exec(["app", code]) == f"app {code}"
    assert split_exec(f"app --x {code}") == ["app", "--x", code]


def test_split_join_random_round_trip():
    rng = random.Random(42)
    alphabet = "ab Z9/._-=%\"'\\$`;&|<>~*?#()\t\näß"
    for _ in range(2000):
        args = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
                for _ in range(rng.randint(1, 5))]
        args = [a for a in args if a not in FIELD_CODES and a.replace("%%", "%") not in FIELD_CODES] or ["x"]
        joined = join_exec(args)
        assert split_exec(joined) == args, joined
        # and through the desktop-file escaping layer
        entry = DesktopEntry.new()
        entry.set("Exec", joined)
        assert split_exec(DesktopEntry.parse(entry.to_text()).get("Exec")) == args


@pytest.mark.parametrize("value", [
    "AppRun --no-sandbox %U",
    "openscad %f",
    "AppRun - --single-instance %F",
    '"/home/u/My Apps/T3 Code.AppImage" --no-sandbox %U',
    'foo "a \\"b\\"" 100%% %F',
])
def test_join_split_canonical_round_trip(value):
    assert join_exec(split_exec(value)) == value


def test_backslash_in_arg_is_double_escaped_in_the_file():
    entry = DesktopEntry.new()
    entry.set("Exec", join_exec(["prog", "a\\b"]))
    assert entry.get_raw("Exec") == 'prog "a\\\\\\\\b"'   # four backslashes in the file
    assert split_exec(entry.get("Exec")) == ["prog", "a\\b"]


@pytest.mark.parametrize("value, program", [
    ("AppRun %U", "AppRun"),
    ("env A=1 B=2 /usr/bin/app %U", "/usr/bin/app"),
    ('"/opt/My App/app"', "/opt/My App/app"),
    ("env A=1", None),
    ("", None),
    ('broken "', None),
])
def test_exec_program(value, program):
    assert exec_program(value) == program


# =============================================================================================
# rewrite_exec
# =============================================================================================

T3_TARGET = "/home/u/Applications/T3-Code-Alpha.AppImage"
SPACED = "/home/u/My Apps/T3 Code.AppImage"


@pytest.mark.parametrize("value, kwargs, expected", [
    # the three real Exec lines
    ("AppRun --no-sandbox %U", {}, f"{T3_TARGET} --no-sandbox %U"),
    ("AppRun --no-sandbox %U", {"extra_args": ("--no-sandbox",)}, f"{T3_TARGET} --no-sandbox %U"),
    ("openscad %f", {}, f"{T3_TARGET} %f"),
    ("openscad %f", {"extra_args": ("--no-sandbox",)}, f"{T3_TARGET} --no-sandbox %f"),
    ("AppRun - --single-instance %F", {}, f"{T3_TARGET} - --single-instance %F"),
    ("AppRun --ozone-platform-hint=auto %U", {"extra_args": ("--no-sandbox",)},
     f"{T3_TARGET} --no-sandbox --ozone-platform-hint=auto %U"),
    # env handling
    ("env FOO=1 app %U", {}, f"env FOO=1 {T3_TARGET} %U"),
    ("env FOO=1 app %U", {"env": {"APPIMAGE_EXTRACT_AND_RUN": "1"}},
     f"env FOO=1 APPIMAGE_EXTRACT_AND_RUN=1 {T3_TARGET} %U"),
    ("env FOO=1 app %U", {"env": {"FOO": "2"}}, f"env FOO=2 {T3_TARGET} %U"),
    ("AppRun %U", {"env": {"APPIMAGE_EXTRACT_AND_RUN": "1"}, "extra_args": ("--no-sandbox",)},
     f"env APPIMAGE_EXTRACT_AND_RUN=1 {T3_TARGET} --no-sandbox %U"),
    ("env FOO=1", {}, f"env FOO=1 {T3_TARGET}"),
    # empty / unparseable
    ("", {}, T3_TARGET),
    ("", {"extra_args": ("--no-sandbox",)}, f"{T3_TARGET} --no-sandbox"),
    ("", {"env": {"APPIMAGE_EXTRACT_AND_RUN": "1"}}, f"env APPIMAGE_EXTRACT_AND_RUN=1 {T3_TARGET}"),
    ('app "unbalanced %U', {}, T3_TARGET),
    ("   ", {}, T3_TARGET),
    # the tail is kept verbatim
    ('app --title="Hello World" --file=%f', {}, f'{T3_TARGET} --title="Hello World" --file=%f'),
    ("  AppRun   %U  ", {}, f"{T3_TARGET} %U"),
    ("AppRun %U", {"extra_args": ("--a", "--a", "--b")}, f"{T3_TARGET} --a --b %U"),
    ('"/usr/lib/my app/run" 100%% %F', {}, f"{T3_TARGET} 100%% %F"),
])
def test_rewrite_exec(value, kwargs, expected):
    assert rewrite_exec(value, T3_TARGET, **kwargs) == expected


def test_rewrite_exec_path_with_spaces():
    result = rewrite_exec("AppRun --no-sandbox %U", SPACED, extra_args=("--no-sandbox",))
    assert result == f'"{SPACED}" --no-sandbox %U'
    assert split_exec(result) == [SPACED, "--no-sandbox", "%U"]
    entry = DesktopEntry.new()
    entry.set("Exec", result)
    assert split_exec(DesktopEntry.parse(entry.to_text()).get("Exec"))[0] == SPACED


def test_rewrite_exec_path_with_special_characters():
    path = '/home/u/100% "cool" $apps/x`y`.AppImage'
    result = rewrite_exec("AppRun %U", path)
    # GLib looks argv[0] up before it expands "%%": a program with "%" is started by env
    assert split_exec(result) == ["env", path, "%U"]
    assert "%%" in result and exec_program(result) == path
    assert split_exec(rewrite_exec("AppRun %U", "/apps/x.AppImage")) == ["/apps/x.AppImage", "%U"]
    assert split_exec(rewrite_exec("AppRun %U", path, env={"A": "1"})) == ["env", "A=1", path, "%U"]


def test_rewrite_exec_keep_env_filters_embedded_assignments():
    value = "env LD_PRELOAD=/tmp/evil.so QT_QPA_PLATFORM=xcb AppRun %U"
    result = rewrite_exec(value, "/apps/App.AppImage", env={"APPIMAGE_EXTRACT_AND_RUN": "1"},
                          keep_env=lambda name, val: name == "QT_QPA_PLATFORM")
    assert result == "env QT_QPA_PLATFORM=xcb APPIMAGE_EXTRACT_AND_RUN=1 /apps/App.AppImage %U"
    assert rewrite_exec(value, "/apps/App.AppImage", keep_env=lambda n, v: False) == \
        "/apps/App.AppImage %U"


def test_exec_env():
    from easy_installer.core.desktop_entry import exec_env

    assert exec_env("env A=1 B=x=y prog --flag C=3") == [("A", "1"), ("B", "x=y")]
    assert exec_env("prog A=1") == []
    assert exec_env('env "unbalanced') is None
    assert exec_env("env A=1") == [("A", "1")]


def test_parse_only_skips_ascii_whitespace_like_glib():
    entry = DesktopEntry.parse("[Desktop Entry]\n \n Name=Foo\n\tExec=foo\n \n")
    assert entry.get("Name") is None  # an NBSP-indented key is not a key for GLib either
    assert entry.get("Exec") == "foo"
    entry.remove_invalid_lines()
    assert entry.to_text() == "[Desktop Entry]\n\tExec=foo\n \n"
