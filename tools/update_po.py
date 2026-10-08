#!/usr/bin/env python3
"""Merge a new .pot template into existing .po files, like msgmerge (standard library only).

Usage:
    update_po.py [--drop-obsolete] [--no-fuzzy-matching] po/easy-installer.pot po/de.po po/nl.po

For every .po file:
  * existing translations are kept (matched by msgctxt + msgid);
  * messages that are new in the template are added untranslated - or, if a removed message is
    very similar, pre-filled with its translation and marked "fuzzy" (with "#| msgid" showing the
    old text) so a translator only has to review it;
  * messages that no longer exist become obsolete ("#~") entries, or are dropped with
    --drop-obsolete;
  * a missing .po file is created from the template, with Language and Plural-Forms set from the
    file name (e.g. "de.po" -> German plural rules).
Files are only rewritten when something changed.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from msgfmt import (  # noqa: E402
    POEntry,
    POSyntaxError,
    find_header,
    format_po,
    header_get,
    header_set,
    nplurals_of,
    read_po,
    statistics,
    write_text_if_changed,
)

FORMAT_FLAGS = {"python-format", "python-brace-format", "no-python-format", "no-python-brace-format"}
FUZZY_CUTOFF = 0.8

# Plural rules for languages we are likely to get; others keep the template placeholder.
_TWO_FORMS = "nplurals=2; plural=(n != 1);"
_ONE_FORM = "nplurals=1; plural=0;"
PLURAL_FORMS = {
    **{lang: _TWO_FORMS for lang in (
        "bg", "ca", "da", "de", "el", "en", "eo", "es", "et", "eu", "fi", "fy", "gl", "hu", "it",
        "nb", "nl", "nn", "no", "pt", "sv", "tr",
    )},
    **{lang: "nplurals=2; plural=(n > 1);" for lang in ("fr", "pt_BR", "oc")},
    **{lang: _ONE_FORM for lang in ("id", "ja", "ko", "th", "vi", "zh", "zh_CN", "zh_TW")},
    "cs": "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    "sk": "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;",
    "pl": "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    **{lang: "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
                "(n%100<10 || n%100>=20) ? 1 : 2);" for lang in ("ru", "uk", "be", "sr", "hr", "bs")},
}


def plural_forms_for(language: str) -> str | None:
    return PLURAL_FORMS.get(language) or PLURAL_FORMS.get(language.split("_")[0].split("@")[0])


def _template_copy(template: POEntry, nplurals: int) -> POEntry:
    entry = copy.deepcopy(template)
    entry.translator_comments = []
    entry.previous = []
    entry.msgstr = ""
    entry.msgstr_plural = {i: "" for i in range(nplurals)} if entry.is_plural else {}
    entry.flags = [f for f in template.flags if f != "fuzzy"]
    entry.obsolete = False
    return entry


def _copy_translation(target: POEntry, source: POEntry, nplurals: int) -> bool:
    """Copy source's translation into target; return True if it needs review (fuzzy)."""
    if target.is_plural and source.is_plural:
        forms = source.plural_forms()
        target.msgstr_plural = {i: (forms[i] if i < len(forms) else "") for i in range(max(nplurals, 1))}
        return source.msgid_plural != target.msgid_plural
    if target.is_plural:
        target.msgstr_plural = {i: "" for i in range(max(nplurals, 1))}
        target.msgstr_plural[0] = source.msgstr
        return bool(source.msgstr)
    if source.is_plural:
        target.msgstr = source.msgstr_plural.get(0, "")
        return bool(target.msgstr)
    target.msgstr = source.msgstr
    return False


def _has_translation(entry: POEntry) -> bool:
    return bool(entry.msgstr) or any(entry.msgstr_plural.values())


def _previous_of(entry: POEntry) -> list[tuple[str, str]]:
    previous = []
    if entry.msgctxt is not None:
        previous.append(("msgctxt", entry.msgctxt))
    previous.append(("msgid", entry.msgid))
    if entry.msgid_plural is not None:
        previous.append(("msgid_plural", entry.msgid_plural))
    return previous


def merge_header(old: POEntry | None, template: POEntry | None, language: str) -> POEntry:
    header = copy.deepcopy(old if old is not None else template) or POEntry()
    if old is None:
        header.set_flag("fuzzy", False)
        header.translator_comments = [
            f"{language} translation of Easy Installer." if c.startswith("Translation template") else c
            for c in header.translator_comments
            if not c.startswith("FIRST AUTHOR")
        ]
    fields_source = [header]
    for name in ("Project-Id-Version", "POT-Creation-Date"):
        value = header_get([template], name) if template is not None else None
        if value is not None:
            header_set(header, name, value)
    if not (header_get(fields_source, "Language") or "").strip():
        header_set(header, "Language", language)
    content_type = header_get(fields_source, "Content-Type") or ""
    if "charset=" not in content_type or "CHARSET" in content_type:
        header_set(header, "Content-Type", "text/plain; charset=UTF-8")
    plural = header_get(fields_source, "Plural-Forms") or ""
    if "INTEGER" in plural or "nplurals" not in plural:
        known = plural_forms_for(header_get(fields_source, "Language") or language)
        if known:
            header_set(header, "Plural-Forms", known)
        else:
            print(f"update_po.py: warning: unknown plural rules for {language!r}; "
                  "please fill in Plural-Forms", file=sys.stderr)
    return header


def merge(old_entries: list[POEntry], template_entries: list[POEntry], language: str, *,
          fuzzy_matching: bool = True, keep_obsolete: bool = True) -> list[POEntry]:
    header = merge_header(find_header(old_entries), find_header(template_entries), language)
    nplurals = nplurals_of([header]) or 2

    active = {e.key: e for e in old_entries if not e.obsolete and not e.is_header}
    obsolete = {e.key: e for e in old_entries if e.obsolete}
    used: set[tuple[str | None, str]] = set()
    result = [header]
    unmatched_templates: list[tuple[int, POEntry]] = []

    for template in template_entries:
        if template.is_header or template.obsolete:
            continue
        entry = _template_copy(template, nplurals)
        old = active.get(template.key) or obsolete.get(template.key)
        if old is not None:
            used.add(old.key)
            needs_review = _copy_translation(entry, old, nplurals)
            entry.translator_comments = list(old.translator_comments)
            entry.flags += [f for f in old.flags
                            if f not in FORMAT_FLAGS and f != "fuzzy" and f not in entry.flags]
            if old.fuzzy or needs_review:
                entry.set_flag("fuzzy")
                entry.previous = list(old.previous) or (_previous_of(old) if needs_review else [])
        else:
            unmatched_templates.append((len(result), entry))
        result.append(entry)

    if fuzzy_matching:
        _fuzzy_fill(unmatched_templates, active, used, nplurals)

    if keep_obsolete:
        for old in list(active.values()) + list(obsolete.values()):
            if old.key in used or not _has_translation(old):
                continue
            gone = copy.deepcopy(old)
            gone.obsolete = True
            gone.references = []
            gone.extracted_comments = []
            result.append(gone)
            used.add(old.key)
    return result


def _fuzzy_fill(unmatched: list[tuple[int, POEntry]], active: dict[tuple[str | None, str], POEntry],
                used: set[tuple[str | None, str]], nplurals: int) -> None:
    for _, entry in unmatched:
        candidates = {
            old.msgid: old for key, old in active.items()
            if key not in used and old.msgctxt == entry.msgctxt and _has_translation(old)
        }
        if not candidates:
            continue
        best = difflib.get_close_matches(entry.msgid, list(candidates), n=1, cutoff=FUZZY_CUTOFF)
        if not best:
            continue
        old = candidates[best[0]]
        used.add(old.key)
        _copy_translation(entry, old, nplurals)
        entry.translator_comments = list(old.translator_comments)
        entry.set_flag("fuzzy")
        entry.previous = _previous_of(old)


def update_file(po_path: Path, template_entries: list[POEntry], *, fuzzy_matching: bool,
                keep_obsolete: bool) -> bool:
    language = po_path.stem
    old_entries = read_po(po_path) if po_path.exists() else []
    merged = merge(old_entries, template_entries, language,
                   fuzzy_matching=fuzzy_matching, keep_obsolete=keep_obsolete)
    changed = write_text_if_changed(po_path, format_po(merged))
    translated, fuzzy, untranslated = statistics(merged)
    obsolete = sum(1 for e in merged if e.obsolete)
    state = ("created" if not old_entries else "updated") if changed else "unchanged"
    print(f"update_po.py: {po_path}: {translated} translated, {fuzzy} fuzzy, "
          f"{untranslated} untranslated, {obsolete} obsolete ({state})", file=sys.stderr)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge a .pot template into .po files.")
    parser.add_argument("template", help="the .pot file")
    parser.add_argument("po_files", nargs="+", help=".po files to update (created if missing)")
    parser.add_argument("--drop-obsolete", action="store_true",
                        help="drop messages that are no longer used instead of keeping them as #~")
    parser.add_argument("--no-fuzzy-matching", action="store_true",
                        help="do not pre-fill new messages from similar old ones")
    args = parser.parse_args(argv)

    try:
        template_entries = read_po(args.template)
    except (OSError, POSyntaxError, UnicodeDecodeError, LookupError) as exc:
        print(f"update_po.py: {exc}", file=sys.stderr)
        return 1
    status = 0
    for name in args.po_files:
        try:
            update_file(Path(name), template_entries, fuzzy_matching=not args.no_fuzzy_matching,
                        keep_obsolete=not args.drop_obsolete)
        except (OSError, POSyntaxError, UnicodeDecodeError, LookupError) as exc:
            print(f"update_po.py: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
