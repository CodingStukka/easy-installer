"""Translations: the German and Dutch catalogs are complete, up to date, safe to format, compile,
follow the house style (informal address, one word per idea in the CLI and the GUI), reach the
CLI (0.2: `update`, `settings`), and agree with the translations that live outside gettext
(launcher, AppStream metainfo and release notes, polkit policy, the "Uninstall…" action of
installed apps)."""

from __future__ import annotations

import gettext
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from easy_installer import __version__
from easy_installer.core.desktop_entry import DesktopEntry
from easy_installer.core.integration import DesktopRenderSpec, render_desktop_entry
from easy_installer.core.paths import Scope

REPO = Path(__file__).resolve().parents[1]
PO_DIR = REPO / "po"
DATA = REPO / "data"
POT = PO_DIR / "easy-installer.pot"
LANGS = PO_DIR.joinpath("LINGUAS").read_text(encoding="utf-8").split()
PLURAL_FORMS = {"de": "nplurals=2; plural=(n != 1);", "nl": "nplurals=2; plural=(n != 1);"}
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

sys.path.insert(0, str(REPO / "tools"))
import msgfmt  # noqa: E402
import xgettext  # noqa: E402

# House style: informal address (German "du", Dutch "je/jij"). German avoids a sentence-initial
# "Sie" meaning "it/they" as well, because readers would take it for the formal "you".
FORMAL = {
    "de": re.compile(r"\b(Sie|Ihr|Ihre|Ihrem|Ihren|Ihrer|Ihres|Ihnen)\b"),
    "nl": re.compile(r"\b(u|uw|U|Uw)\b"),
}
# Labels shown side by side: their access keys (the letter after "_") must differ.
# A dialog's buttons are "side by side" with what stays mapped in the main window behind it (the
# header button, the empty page's button and the banners' buttons): with duplicate access keys GTK
# only moves the focus, so e.g. Alt+I would never press "Install".
MAIN_WINDOW_MNEMONICS = ("Install _App…", "C_hoose AppImage…", "_Make Default", "In_stall",
                         "Update A_ll")
DIALOG_MNEMONICS = [
    ("_Cancel", "_Install"), ("_Cancel", "_Update"), ("_Cancel", "_Reinstall"),
    ("_Cancel", "_Install Anyway"), ("_Done", "_Open App"), ("_Back", "_Close"),
    ("_Cancel", "_Uninstall"), ("_Cancel", "_Remove"), ("_Close",), ("_Install",),
    # 0.2: install dialog (an older file, another signer), update dialog, details alerts
    ("_Cancel", "_Go Back to {version}"), ("_Cancel", "_Update Anyway"),
    ("_Cancel", "_Update", "_What’s New"), ("_Cancel", "_Update Anyway", "_What’s New"),
    ("_Try Again", "_Close"),
    ("_Cancel", "_Go Back"), ("_Cancel", "_Delete"),
]
MNEMONIC_GROUPS = [
    *DIALOG_MNEMONICS,
    *(MAIN_WINDOW_MNEMONICS + group for group in DIALOG_MNEMONICS),
    ("Check for _Updates", "_Check This Computer", "_Preferences", "_Keyboard Shortcuts",
     "_About Easy Installer"),
]
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}|%\([^)]*\)[a-z]|%[a-z%]")
# One word per idea, in the CLI and in the GUI alike: whenever the English text uses the term on
# the left, the translation uses the one on the right (and never an alternative listed below).
UPDATE_WORD = r"(?i)(?<![-\w])updat(e|es|ed|ing)\b(?!-)"   # not update-desktop-database & co.
TERMS = {
    "de": [(r"(?i)previous version", r"(?i)vorherige"),
           (r"(?i)\bportable\b", r"(?i)\bportab"),
           (r"(?i)settings and data", r"Einstellungen und Daten"),
           (UPDATE_WORD, r"(?i)update|aktualisier")],
    "nl": [(r"(?i)previous version", r"(?i)vorige versie"),
           (r"(?i)\bportable\b", r"(?i)\bportable\b"),
           (r"(?i)settings and data", r"(?i)instellingen en gegevens"),
           (UPDATE_WORD, r"(?i)update|bijwerk|bijgewerkt|bij te werken")],
}
AVOIDED_TERMS = {
    "de": re.compile(r"Aktualisierung|frühere(n)? Version", re.IGNORECASE),
    "nl": re.compile(r"draagba|eerdere versie|opwaardering", re.IGNORECASE),
}


def entries(path: Path) -> list[msgfmt.POEntry]:
    return [e for e in msgfmt.read_po(path) if not e.is_header and not e.obsolete]


def forms(entry: msgfmt.POEntry) -> list[str]:
    return entry.plural_forms() if entry.is_plural else [entry.msgstr]


def mnemonic(label: str) -> str | None:
    """The access key of a GTK label ("_Install" -> "i"), ignoring placeholders."""
    text = _PLACEHOLDER_RE.sub("", label).replace("__", "")
    index = text.find("_")
    return text[index + 1].casefold() if 0 <= index < len(text) - 1 else None


def underscores(text: str) -> int:
    return _PLACEHOLDER_RE.sub("", text).replace("__", "").count("_")


@pytest.fixture(scope="module")
def localedir(tmp_path_factory) -> Path:
    """All catalogs compiled with tools/msgfmt.py --check into a private locale dir."""
    root = tmp_path_factory.mktemp("locale")
    for lang in LANGS:
        mo = root / lang / "LC_MESSAGES" / "easy-installer.mo"
        mo.parent.mkdir(parents=True)
        assert msgfmt.main(["--check", str(PO_DIR / f"{lang}.po"), "-o", str(mo)]) == 0, lang
    return root


def catalog(localedir: Path, lang: str) -> gettext.GNUTranslations:
    return gettext.translation("easy-installer", localedir=str(localedir), languages=[lang])


# ------------------------------------------------------------------------------------------------
# template
# ------------------------------------------------------------------------------------------------


def test_potfiles_lists_every_source_file():
    listed = set((PO_DIR / "POTFILES").read_text(encoding="utf-8").split())
    sources = {str(p.relative_to(REPO)) for p in (REPO / "src" / "easy_installer").rglob("*.py")
               if p.stat().st_size and "__pycache__" not in p.parts}
    assert sources - listed == set(), "run `make po` (po/POTFILES is incomplete)"
    assert all((REPO / name).is_file() for name in listed), "po/POTFILES lists a missing file"


def test_pot_is_up_to_date_with_the_sources(tmp_path, capsys):
    sources = (PO_DIR / "POTFILES").read_text(encoding="utf-8").split()
    fresh = tmp_path / "fresh.pot"
    assert xgettext.main(["-o", str(fresh), "-D", str(REPO), *sources]) == 0

    def messages(path: Path) -> dict:
        return {e.key: (e.msgid_plural, sorted(e.flags)) for e in entries(path)}

    assert messages(fresh) == messages(POT), "po/easy-installer.pot is outdated: run `make po`"
    # f-strings or other non-literal arguments of _() cannot be translated.
    warnings = [line for line in capsys.readouterr().err.splitlines() if "warning" in line]
    assert warnings == []


# ------------------------------------------------------------------------------------------------
# catalogs
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_header(lang):
    po = msgfmt.read_po(PO_DIR / f"{lang}.po")
    header = msgfmt.find_header(po)
    assert header is not None and not header.fuzzy
    assert msgfmt.header_get(po, "Language") == lang
    assert msgfmt.header_get(po, "Plural-Forms") == PLURAL_FORMS[lang]
    assert "charset=UTF-8" in (msgfmt.header_get(po, "Content-Type") or "")


@pytest.mark.parametrize("lang", LANGS)
def test_every_message_is_translated(lang):
    pot = {e.key: e for e in entries(POT)}
    po = {e.key: e for e in entries(PO_DIR / f"{lang}.po")}
    assert po.keys() == pot.keys(), "po file and template differ: run `make po`"
    assert not [e for e in msgfmt.read_po(PO_DIR / f"{lang}.po") if e.obsolete], "obsolete entries"
    for key, entry in po.items():
        assert not entry.fuzzy, f"{lang}: fuzzy translation of {entry.msgid!r}"
        assert entry.msgid_plural == pot[key].msgid_plural
        assert entry.translated and all(f.strip() for f in forms(entry)), \
            f"{lang}: untranslated {entry.msgid!r}"
        if entry.is_plural:
            assert len(entry.plural_forms()) == 2, f"{lang}: {entry.msgid!r} needs 2 plural forms"


@pytest.mark.parametrize("lang", LANGS)
def test_placeholders_match(lang):
    for entry in entries(PO_DIR / f"{lang}.po"):
        sources = [entry.msgid] + ([entry.msgid_plural] if entry.is_plural else [])
        expected_brace = set().union(*(msgfmt.brace_placeholders(s) or set() for s in sources))
        expected_percent = [msgfmt.percent_placeholders(s) for s in sources]
        for form in forms(entry):
            where = f"{lang}: {entry.msgid!r} -> {form!r}"
            if "python-brace-format" in entry.flags:
                assert msgfmt.brace_placeholders(form) == expected_brace, where
                form.format(**{name: "x" for name in expected_brace})  # must not raise
            else:
                assert msgfmt.brace_placeholders(form) in (set(), None), where
            positional, named = msgfmt.percent_placeholders(form)
            assert (positional, named) in expected_percent, where
            assert form.endswith("\n") == entry.msgid.endswith("\n"), where
        errors, _warnings = msgfmt.check_entry(entry)
        assert errors == [], (lang, entry.msgid, errors)


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_compiles_and_translates(lang, localedir):
    translations = catalog(localedir, lang)
    for entry in entries(PO_DIR / f"{lang}.po"):
        if entry.is_plural:
            assert translations.ngettext(entry.msgid, entry.msgid_plural, 1) == entry.plural_forms()[0]
            assert translations.ngettext(entry.msgid, entry.msgid_plural, 5) == entry.plural_forms()[1]
        else:
            assert translations.gettext(entry.msgid) == entry.msgstr
    assert translations.gettext("{name} is installed").format(name="X").startswith("X ")


@pytest.mark.parametrize("lang", LANGS)
def test_house_style(lang):
    for entry in entries(PO_DIR / f"{lang}.po"):
        for form in forms(entry):
            where = f"{lang}: {entry.msgid!r} -> {form!r}"
            assert not FORMAL[lang].search(form), f"use the informal address: {where}"
            assert "..." not in form or "..." in entry.msgid, f"use '…': {where}"
            if entry.msgid.endswith("…") or entry.msgid.endswith("…]"):
                assert re.search(r"\S…\]?$", form), f"ellipsis without a space before it: {where}"
            assert underscores(form) == underscores(entry.msgid), f"one access key: {where}"


@pytest.mark.parametrize("lang", LANGS)
def test_terminology_is_consistent(lang):
    """"previous version", "portable app", "settings and data" and "update" are always
    translated with the same word (the CLI and the GUI say the same thing)."""
    for entry in entries(PO_DIR / f"{lang}.po"):
        source = " ".join(filter(None, (entry.msgid, entry.msgid_plural))).replace("_", "")
        for form in forms(entry):
            text = form.replace("_", "")
            where = f"{lang}: {entry.msgid!r} -> {form!r}"
            assert not AVOIDED_TERMS[lang].search(text), where
            for english, translated in TERMS[lang]:
                if re.search(english, source):
                    assert re.search(translated, text), (translated, where)


@pytest.mark.parametrize("lang", LANGS)
def test_access_keys_do_not_collide(lang, localedir):
    translations = catalog(localedir, lang)
    for group in MNEMONIC_GROUPS:
        keys = [mnemonic(translations.gettext(label)) for label in group]
        assert None not in keys and len(set(keys)) == len(keys), \
            (lang, [translations.gettext(label) for label in group])


@pytest.mark.parametrize("lang", LANGS)
def test_cli_prints_translations(lang, localedir, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG"))}
    env.update({"LANGUAGE": lang, "LANG": "C.UTF-8", "EASY_INSTALLER_LOCALEDIR": str(localedir),
                "PYTHONPATH": str(REPO / "src")})
    proc = subprocess.run([sys.executable, "-m", "easy_installer", "--help"], env=env,
                          cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    translations = catalog(localedir, lang)
    assert proc.stdout.startswith(translations.gettext("usage: "))
    assert translations.gettext("check whether this computer is ready for AppImages") in proc.stdout
    assert translations.gettext("look for new versions of your apps and install them") \
        in proc.stdout


def run_cli(lang: str, localedir: Path, cwd: Path, *args: str) -> str:
    """stdout of `easy-installer ARGS` in ``lang`` (HOME and XDG dirs are the test's own), with
    runs of white space (argparse wraps lines) joined into one space."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG"))}
    env.update({"LANGUAGE": lang, "LANG": "C.UTF-8", "EASY_INSTALLER_LOCALEDIR": str(localedir),
                "PYTHONPATH": str(REPO / "src"), "NO_COLOR": "1"})
    proc = subprocess.run([sys.executable, "-m", "easy_installer", *args], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return " ".join(proc.stdout.split())


@pytest.mark.parametrize("lang", LANGS)
def test_cli_update_and_settings_are_translated(lang, localedir, tmp_path):
    """0.2 commands: the help of `update` and the list of settings (no network, no apps)."""
    translations = catalog(localedir, lang)

    def tr(text: str) -> str:
        return " ".join(translations.gettext(text).split())

    out = run_cli(lang, localedir, tmp_path, "update", "--help")
    for text in ("Look for new versions of the installed apps and install them. Without an app "
                 "ID or --all, it only shows which new versions there are.",
                 "update every app that has a new version", "do not ask before updating"):
        assert tr(text) in out, (lang, text)
    out = run_cli(lang, localedir, tmp_path, "settings")
    assert out.startswith(tr("Settings") + " " + tr("(saved in {path})").split("{")[0])
    assert f"check-updates {tr('yes')} " in out
    for text in ("look for new versions of your apps automatically (yes or no)",
                 "hours between two automatic checks for new versions"):
        assert tr(text) in out, (lang, text)


# ------------------------------------------------------------------------------------------------
# translations outside gettext
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_uninstall_action_matches_catalog(lang, localedir, tmp_path):
    spec = DesktopRenderSpec(
        app_id="demo", name="Demo", appimage_path=tmp_path / "Demo.AppImage", icon_name=None,
        embedded=None, embedded_stem=None, comment=None, version=None, scope=Scope.USER,
        uninstall_command=("/usr/bin/easy-installer", "--uninstall", "demo"))
    entry = DesktopEntry.parse(render_desktop_entry(spec))
    group = "Desktop Action easyinstaller-uninstall"
    assert entry.get("Name", group) == "Uninstall with Easy Installer…"
    assert entry.get(f"Name[{lang}]", group) == catalog(localedir, lang).gettext(
        "Uninstall with Easy Installer…")


@pytest.mark.parametrize("lang", LANGS)
def test_launcher_and_metainfo_match_catalog(lang, localedir):
    desktop = DesktopEntry.parse((DATA / "com.roothirsch.EasyInstaller.desktop").read_text("utf-8"))
    for key in ("Name", "GenericName", "Comment", "Keywords"):
        assert desktop.get(f"{key}[{lang}]"), f"launcher lacks {key}[{lang}]"
    comment = desktop.get(f"Comment[{lang}]")
    about = catalog(localedir, lang).gettext(
        "Install downloaded AppImage apps with one click. Easy Installer puts them in a safe place, "
        "adds them to your app menu and search, finds updates for them, and removes them again "
        "when you no longer need them.")
    assert about.startswith(comment + "."), "launcher Comment and About text disagree"

    component = ET.parse(DATA / "com.roothirsch.EasyInstaller.metainfo.xml").getroot()
    summaries = {s.get(XML_LANG): s.text for s in component.findall("summary")}
    assert summaries[lang] == comment
    description = component.find("description")
    english = [e for e in description.iter() if e.tag in ("p", "li") and e.get(XML_LANG) is None]
    localized = [e for e in description.iter() if e.tag in ("p", "li") and e.get(XML_LANG) == lang]
    assert len(localized) == len(english), f"metainfo description is not fully translated to {lang}"
    for element in localized:
        assert not FORMAL[lang].search(" ".join(element.text.split())), element.text

    # The notes of the newest release are shown by software centers, too.
    newest = component.find("releases").find("release")
    assert newest.get("version") == __version__
    notes = newest.find("description")
    english = [e for e in notes.iter() if e.tag in ("p", "li") and e.get(XML_LANG) is None]
    localized = [e for e in notes.iter() if e.tag in ("p", "li") and e.get(XML_LANG) == lang]
    assert english and len(localized) == len(english), f"release notes lack {lang}"
    for element in localized:
        assert not FORMAL[lang].search(" ".join(element.text.split())), element.text

    # Every English keyword has a counterpart (search in GNOME / the app grid).
    assert len(desktop.get_list(f"Keywords[{lang}]")) >= len(desktop.get_list("Keywords"))


@pytest.mark.parametrize("lang", LANGS)
def test_polkit_policy_is_translated(lang):
    action = ET.parse(DATA / "com.roothirsch.EasyInstaller.policy").getroot().find("action")
    for tag in ("description", "message"):
        texts = {e.get(XML_LANG): e.text for e in action.findall(tag)}
        assert texts.get(lang), f"policy {tag} lacks {lang}"
        assert not FORMAL[lang].search(texts[lang]), texts[lang]


def test_msgfmt_checks_fuzzy_entries_it_compiles(tmp_path, capsys):
    """update_po.py pre-fills fuzzy entries whose placeholders may be stale; --use-fuzzy compiles
    them, so --check must check them too (a wrong placeholder raises KeyError at runtime)."""
    po = tmp_path / "t.po"
    po.write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        "#, fuzzy, python-brace-format\n"
        'msgid "Version {old} is installed and will be replaced by {latest}."\n'
        'msgstr "Version {old} ist installiert und wird durch {new} ersetzt."\n',
        encoding="utf-8")
    mo = tmp_path / "t.mo"
    assert msgfmt.main(["--check", str(po), "-o", str(mo)]) == 0  # fuzzy is not compiled
    assert msgfmt.main(["--check", "--use-fuzzy", str(po), "-o", str(mo)]) == 1
    assert "unknown placeholder(s) ['new']" in capsys.readouterr().err
