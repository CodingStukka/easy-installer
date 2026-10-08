#!/usr/bin/env python3
"""Compile a gettext .po file into a binary .mo catalog (standard library only).

Usage:
    msgfmt.py INPUT.po -o OUTPUT.mo [--check] [--statistics] [--use-fuzzy]

Supports msgctxt, plural forms (msgid_plural / msgstr[n]), multi-line strings, C escapes and the
header entry. Fuzzy entries are skipped unless --use-fuzzy is given (the header is always kept, as
GNU msgfmt does), untranslated entries and obsolete ("#~") entries are never compiled.

--check reports translations whose Python placeholders do not match the original ("%s",
"%(name)s", "{name}"); a mismatch there would crash the app at runtime, so it is an error.

This module is also the small PO-file library shared by xgettext.py and update_po.py
(POEntry, parse_po, read_po, format_po, header helpers).
"""

from __future__ import annotations

import argparse
import os
import re
import string
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MO_MAGIC = 0x950412DE
CONTEXT_SEPARATOR = "\x04"


# --------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------


@dataclass
class POEntry:
    msgid: str = ""
    msgid_plural: str | None = None
    msgctxt: str | None = None
    msgstr: str = ""
    msgstr_plural: dict[int, str] = field(default_factory=dict)
    translator_comments: list[str] = field(default_factory=list)  # "# text"
    extracted_comments: list[str] = field(default_factory=list)  # "#. text"
    references: list[str] = field(default_factory=list)  # "#: file:line" tokens
    flags: list[str] = field(default_factory=list)  # "#, fuzzy, python-format"
    previous: list[tuple[str, str]] = field(default_factory=list)  # "#| msgid ..." (keyword, value)
    obsolete: bool = False
    lineno: int = 0

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.msgctxt, self.msgid)

    @property
    def is_header(self) -> bool:
        return self.msgid == "" and self.msgctxt is None and not self.obsolete

    @property
    def is_plural(self) -> bool:
        return self.msgid_plural is not None

    @property
    def fuzzy(self) -> bool:
        return "fuzzy" in self.flags

    def plural_forms(self) -> list[str]:
        if not self.msgstr_plural:
            return []
        return [self.msgstr_plural.get(i, "") for i in range(max(self.msgstr_plural) + 1)]

    @property
    def translated(self) -> bool:
        if self.is_plural:
            forms = self.plural_forms()
            return bool(forms) and all(forms)
        return bool(self.msgstr)

    def set_flag(self, flag: str, on: bool = True) -> None:
        if on and flag not in self.flags:
            if flag == "fuzzy":  # GNU tools list it first
                self.flags.insert(0, flag)
            else:
                self.flags.append(flag)
        elif not on and flag in self.flags:
            self.flags.remove(flag)


class POSyntaxError(ValueError):
    def __init__(self, filename: str, lineno: int, message: str):
        super().__init__(f"{filename}:{lineno}: {message}")
        self.filename = filename
        self.lineno = lineno


# --------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
    "\\": "\\", '"': '"', "'": "'", "?": "?",
}
_KEYWORD_RE = re.compile(r"^(msgctxt|msgid_plural|msgid|msgstr)(?:\[(\d+)\])?\s+(\".*\")\s*$")
_STRING_RE = re.compile(r"^(\".*\")\s*$")
_ENTRY_START_RE = re.compile(r"^(msgctxt|msgid)\s")


def unquote(token: str, filename: str = "<po>", lineno: int = 0) -> str:
    """Decode one quoted PO string literal (including the surrounding quotes)."""
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        raise POSyntaxError(filename, lineno, f"expected a quoted string, got {token!r}")
    body = token[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == '"':
            raise POSyntaxError(filename, lineno, "unescaped double quote inside a string")
        if char != "\\":
            out.append(char)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise POSyntaxError(filename, lineno, "string ends with a lone backslash")
        char = body[i]
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
            i += 1
        elif char in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i:]).group(0)  # type: ignore[union-attr]
            out.append(chr(int(digits, 8)))
            i += len(digits)
        elif char == "x":
            match = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 1:])
            if not match:
                raise POSyntaxError(filename, lineno, "\\x escape without hex digits")
            out.append(chr(int(match.group(0), 16)))
            i += 1 + len(match.group(0))
        else:
            raise POSyntaxError(filename, lineno, f"invalid escape sequence \\{char}")
    return "".join(out)


class _Parser:
    def __init__(self, filename: str):
        self.filename = filename
        self.entries: list[POEntry] = []
        self.current = POEntry()
        self.seen_msgid = False
        self.seen_msgstr = False
        self.section: tuple[str, int | None] | None = None  # field that continuation lines extend
        self.in_previous = False

    def error(self, lineno: int, message: str) -> POSyntaxError:
        return POSyntaxError(self.filename, lineno, message)

    def finish(self, lineno: int) -> None:
        if self.seen_msgid:
            if not self.seen_msgstr:
                raise self.error(lineno, f"entry starting at line {self.current.lineno} has no msgstr")
            if self.current.is_plural and not self.current.msgstr_plural:
                raise self.error(lineno, "plural entry needs msgstr[N] lines")
            self.entries.append(self.current)
        self.current = POEntry()
        self.seen_msgid = self.seen_msgstr = False
        self.section = None

    def start_comment(self, lineno: int) -> None:
        if self.seen_msgstr:
            self.finish(lineno)
        elif self.seen_msgid:
            raise self.error(lineno, "comment between msgid and msgstr")

    def feed(self, lineno: int, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            if self.seen_msgstr:
                self.finish(lineno)
            return
        obsolete = stripped.startswith("#~")
        if obsolete:
            rest = stripped[2:]
            if rest.startswith("|"):
                self.start_comment(lineno)
                self.previous_line(lineno, rest[1:].strip())
                return
            stripped = rest.strip()
            if not stripped:
                return
        elif stripped.startswith("#"):
            self.comment_line(lineno, stripped)
            return
        if _ENTRY_START_RE.match(stripped) and self.seen_msgstr:
            self.finish(lineno)
        if self.seen_msgid or self.current.msgctxt is not None:
            if self.current.obsolete != obsolete:
                raise self.error(lineno, "obsolete (#~) and normal lines mixed in one entry")
        else:
            self.current.obsolete = obsolete
        self.keyword_line(lineno, stripped)

    def comment_line(self, lineno: int, stripped: str) -> None:
        self.start_comment(lineno)
        kind, text = stripped[1:2], stripped[2:].strip()
        entry = self.current
        if kind == ".":
            entry.extracted_comments.append(text)
        elif kind == ":":
            entry.references.extend(text.split())
        elif kind == ",":
            entry.flags.extend(f.strip() for f in text.split(",") if f.strip())
        elif kind == "|":
            self.previous_line(lineno, text)
        else:
            body = stripped[1:]
            entry.translator_comments.append(body[1:] if body.startswith(" ") else body)
        if not entry.lineno:
            entry.lineno = lineno

    def previous_line(self, lineno: int, text: str) -> None:
        match = _KEYWORD_RE.match(text)
        if match:
            self.current.previous.append((match.group(1), unquote(match.group(3), self.filename, lineno)))
        elif _STRING_RE.match(text) and self.current.previous:
            keyword, value = self.current.previous[-1]
            self.current.previous[-1] = (keyword, value + unquote(text, self.filename, lineno))
        else:
            raise self.error(lineno, f"cannot parse previous-message comment: {text!r}")

    def keyword_line(self, lineno: int, text: str) -> None:
        match = _KEYWORD_RE.match(text)
        if not match:
            string_match = _STRING_RE.match(text)
            if not string_match:
                raise self.error(lineno, f"syntax error: {text!r}")
            if self.section is None:
                raise self.error(lineno, "string continuation without a keyword")
            self.append(self.section, unquote(string_match.group(1), self.filename, lineno))
            return
        keyword, index, token = match.group(1), match.group(2), match.group(3)
        value = unquote(token, self.filename, lineno)
        entry = self.current
        if not entry.lineno:
            entry.lineno = lineno
        if keyword == "msgctxt":
            if self.seen_msgid or entry.msgctxt is not None:
                raise self.error(lineno, "misplaced msgctxt")
            entry.msgctxt = value
            self.section = ("msgctxt", None)
        elif keyword == "msgid":
            if self.seen_msgid:
                raise self.error(lineno, "duplicate msgid in one entry")
            entry.msgid = value
            self.seen_msgid = True
            self.section = ("msgid", None)
        elif keyword == "msgid_plural":
            if not self.seen_msgid or self.seen_msgstr or entry.is_plural:
                raise self.error(lineno, "misplaced msgid_plural")
            entry.msgid_plural = value
            self.section = ("msgid_plural", None)
        else:  # msgstr
            if not self.seen_msgid:
                raise self.error(lineno, "msgstr without msgid")
            if index is None:
                if entry.is_plural:
                    raise self.error(lineno, "plural entry needs msgstr[N], not msgstr")
                if self.seen_msgstr:
                    raise self.error(lineno, "duplicate msgstr")
                entry.msgstr = value
                self.section = ("msgstr", None)
            else:
                if not entry.is_plural:
                    raise self.error(lineno, "msgstr[N] without msgid_plural")
                n = int(index)
                if n in entry.msgstr_plural:
                    raise self.error(lineno, f"duplicate msgstr[{n}]")
                entry.msgstr_plural[n] = value
                self.section = ("msgstr", n)
            self.seen_msgstr = True

    def append(self, section: tuple[str, int | None], value: str) -> None:
        name, index = section
        entry = self.current
        if name == "msgstr" and index is not None:
            entry.msgstr_plural[index] += value
        else:
            setattr(entry, name, getattr(entry, name) + value)


def parse_po(text: str, filename: str = "<po>") -> list[POEntry]:
    """Parse PO text into entries (header included, obsolete entries flagged)."""
    if text.startswith("﻿"):
        text = text[1:]
    parser = _Parser(filename)
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        parser.feed(lineno, line)
    parser.finish(len(lines) + 1)
    return parser.entries


def read_po(path: str | os.PathLike[str]) -> list[POEntry]:
    """Read a PO file, honouring the charset declared in its header (UTF-8 by default)."""
    raw = Path(path).read_bytes()
    try:
        entries = parse_po(raw.decode("utf-8"), str(path))
        decoded_as = "utf-8"
    except UnicodeDecodeError:
        entries = parse_po(raw.decode("latin-1"), str(path))
        decoded_as = "latin-1"
    charset = catalog_charset(entries)
    if charset.lower().replace("_", "-") not in (decoded_as, "utf8", "ascii", "us-ascii"):
        entries = parse_po(raw.decode(charset), str(path))
    return entries


# --------------------------------------------------------------------------------------------
# Header helpers
# --------------------------------------------------------------------------------------------


def find_header(entries: list[POEntry]) -> POEntry | None:
    return next((e for e in entries if e.is_header), None)


def parse_header(msgstr: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for line in msgstr.split("\n"):
        if ":" in line:
            name, _, value = line.partition(":")
            fields.append((name.strip(), value.strip()))
    return fields


def build_header(fields: list[tuple[str, str]]) -> str:
    return "".join(f"{name}: {value}\n" for name, value in fields)


def header_get(entries: list[POEntry], name: str) -> str | None:
    header = find_header(entries)
    if header is None:
        return None
    for field_name, value in parse_header(header.msgstr):
        if field_name.lower() == name.lower():
            return value
    return None


def header_set(header: POEntry, name: str, value: str) -> None:
    fields = parse_header(header.msgstr)
    for i, (field_name, _) in enumerate(fields):
        if field_name.lower() == name.lower():
            fields[i] = (field_name, value)
            break
    else:
        fields.append((name, value))
    header.msgstr = build_header(fields)


def catalog_charset(entries: list[POEntry]) -> str:
    content_type = header_get(entries, "Content-Type") or ""
    match = re.search(r"charset=([A-Za-z0-9_.:-]+)", content_type)
    if not match or match.group(1).upper() == "CHARSET":
        return "UTF-8"
    return match.group(1)


def nplurals_of(entries: list[POEntry]) -> int | None:
    match = re.search(r"nplurals\s*=\s*(\d+)", header_get(entries, "Plural-Forms") or "")
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------------------------
# Writing PO text (used by xgettext.py / update_po.py)
# --------------------------------------------------------------------------------------------

_QUOTE_MAP = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r",
              "\a": "\\a", "\b": "\\b", "\f": "\\f", "\v": "\\v"}


def quote(value: str) -> str:
    out = []
    for char in value:
        if char in _QUOTE_MAP:
            out.append(_QUOTE_MAP[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\{ord(char):03o}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def format_string(keyword: str, value: str, prefix: str = "") -> list[str]:
    """Format one keyword; multi-line values are split after each embedded newline (no wrapping)."""
    pieces = value.splitlines(keepends=True)
    if len(pieces) <= 1:
        return [f"{prefix}{keyword} {quote(value)}"]
    return [f'{prefix}{keyword} ""'] + [f"{prefix}{quote(piece)}" for piece in pieces]


def _wrap_references(references: list[str], width: int = 79) -> list[str]:
    lines: list[str] = []
    current = "#:"
    for ref in references:
        if len(current) + 1 + len(ref) > width and current != "#:":
            lines.append(current)
            current = "#:"
        current += " " + ref
    if current != "#:":
        lines.append(current)
    return lines


def format_entry(entry: POEntry) -> str:
    lines = [f"# {c}" if c else "#" for c in entry.translator_comments]
    if not entry.obsolete:
        lines += [f"#. {c}" if c else "#." for c in entry.extracted_comments]
        lines += _wrap_references(entry.references)
    if entry.flags:
        lines.append("#, " + ", ".join(entry.flags))
    previous_prefix = "#~| " if entry.obsolete else "#| "
    for keyword, value in entry.previous:
        lines += format_string(keyword, value, previous_prefix)
    prefix = "#~ " if entry.obsolete else ""
    if entry.msgctxt is not None:
        lines += format_string("msgctxt", entry.msgctxt, prefix)
    lines += format_string("msgid", entry.msgid, prefix)
    if entry.is_plural:
        lines += format_string("msgid_plural", entry.msgid_plural or "", prefix)
        forms = entry.plural_forms() or ["", ""]
        for i, form in enumerate(forms):
            lines += format_string(f"msgstr[{i}]", form, prefix)
    else:
        lines += format_string("msgstr", entry.msgstr, prefix)
    return "\n".join(lines) + "\n"


def format_po(entries: list[POEntry]) -> str:
    return "\n".join(format_entry(e) for e in entries)


def write_text_if_changed(path: Path, text: str, *, ignore: re.Pattern[str] | None = None) -> bool:
    """Atomically write `text` unless the file already has it (optionally ignoring lines
    matching `ignore`, e.g. timestamps). Returns True if the file was written."""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if ignore is not None:
            if ignore.sub("", old) == ignore.sub("", text):
                return False
        elif old == text:
            return False
    atomic_write(path, text.encode("utf-8"))
    return True


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------------------------
# Placeholder checks
# --------------------------------------------------------------------------------------------

# No space flag: "100% sure" must not count as the directive "% s".
PERCENT_RE = re.compile(
    r"%(?:\((?P<name>[^)]*)\))?[#0+-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?(?P<conv>[diouxXeEfFgGcrsa%])"
)


def percent_placeholders(text: str) -> tuple[list[str], set[str]]:
    """Return (positional conversion types, named placeholders) of a %-format string."""
    positional: list[str] = []
    named: set[str] = set()
    for match in PERCENT_RE.finditer(text):
        if match.group("conv") == "%":
            continue
        if match.group("name") is not None:
            named.add(match.group("name"))
        else:
            positional.append(match.group("conv"))
    return positional, named


def brace_placeholders(text: str) -> set[str] | None:
    """Field names of a str.format() string ("" for "{}"), None if it is not a valid one."""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return None
    return {re.split(r"[.\[]", name, maxsplit=1)[0] for _, name, _, _ in parsed if name is not None}


def check_entry(entry: POEntry) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one translated entry.

    Errors are mismatches that make Python's formatting raise at runtime: a different sequence
    of positional %-placeholders, or named placeholders the original does not have.
    """
    errors: list[str] = []
    warnings: list[str] = []
    sources = [entry.msgid] + ([entry.msgid_plural or ""] if entry.is_plural else [])
    targets = [t for t in (entry.plural_forms() if entry.is_plural else [entry.msgstr]) if t]
    for target in targets:
        if sources[0].endswith("\n") != target.endswith("\n"):
            warnings.append("translation and original differ in a trailing newline")
        if "python-format" in entry.flags:
            _check_percent(entry, sources, target, errors, warnings)
        if "python-brace-format" in entry.flags:
            _check_brace(entry, sources, target, errors, warnings)
    return errors, warnings


def _check_percent(entry: POEntry, sources: list[str], target: str,
                   errors: list[str], warnings: list[str]) -> None:
    parsed = [percent_placeholders(source) for source in sources]
    allowed_positional = [positional for positional, _ in parsed]
    source_named: set[str] = set().union(*(named for _, named in parsed))
    positional, named = percent_placeholders(target)
    if positional not in allowed_positional:
        errors.append(f"%-placeholders {positional} do not match the original {allowed_positional[-1]}")
    if named - source_named:
        errors.append(f"unknown placeholder(s) {sorted(named - source_named)} in translation")
    if not entry.is_plural and source_named - named:
        warnings.append(f"placeholder(s) {sorted(source_named - named)} missing in translation")


def _check_brace(entry: POEntry, sources: list[str], target: str,
                 errors: list[str], warnings: list[str]) -> None:
    source_fields: set[str] = set()
    for source in sources:
        source_fields |= brace_placeholders(source) or set()
    fields = brace_placeholders(target)
    if fields is None:
        errors.append("translation has unbalanced { } braces")
        return
    if fields - source_fields:
        errors.append(f"unknown placeholder(s) {sorted(fields - source_fields)} in translation")
    if not entry.is_plural and source_fields - fields:
        warnings.append(f"placeholder(s) {sorted(source_fields - fields)} missing in translation")


# --------------------------------------------------------------------------------------------
# Compiling
# --------------------------------------------------------------------------------------------


def build_catalog(entries: list[POEntry], *, use_fuzzy: bool = False) -> dict[str, str]:
    catalog: dict[str, str] = {}
    for entry in entries:
        if entry.obsolete:
            continue
        if entry.is_header:
            if entry.msgstr:
                catalog[""] = entry.msgstr
            continue
        if (entry.fuzzy and not use_fuzzy) or not entry.translated:
            continue
        key = entry.msgid
        if entry.msgctxt is not None:
            key = entry.msgctxt + CONTEXT_SEPARATOR + key
        if entry.is_plural:
            key += "\0" + (entry.msgid_plural or "")
            catalog[key] = "\0".join(entry.plural_forms())
        else:
            catalog[key] = entry.msgstr
    return catalog


def compile_mo(entries: list[POEntry], *, use_fuzzy: bool = False) -> bytes:
    """Build GNU .mo data (little endian, no hash table - gettext falls back to binary search)."""
    charset = catalog_charset(entries)
    catalog = {
        k.encode(charset): v.encode(charset)
        for k, v in build_catalog(entries, use_fuzzy=use_fuzzy).items()
    }
    keys = sorted(catalog)
    count = len(keys)
    originals_table = 7 * 4
    translations_table = originals_table + count * 8
    data_start = translations_table + count * 8
    ids = b""
    strs = b""
    orig_index: list[int] = []
    trans_index: list[tuple[int, int]] = []
    for key in keys:
        orig_index += [len(key), data_start + len(ids)]
        ids += key + b"\0"
        trans_index.append((len(catalog[key]), len(strs)))
        strs += catalog[key] + b"\0"
    strs_start = data_start + len(ids)
    trans_flat: list[int] = []
    for length, offset in trans_index:
        trans_flat += [length, strs_start + offset]
    header = struct.pack("<7I", MO_MAGIC, 0, count, originals_table, translations_table, 0, 0)
    return (
        header
        + struct.pack(f"<{len(orig_index)}I", *orig_index)
        + struct.pack(f"<{len(trans_flat)}I", *trans_flat)
        + ids
        + strs
    )


def statistics(entries: list[POEntry]) -> tuple[int, int, int]:
    translated = fuzzy = untranslated = 0
    for entry in entries:
        if entry.obsolete or entry.is_header:
            continue
        if entry.fuzzy and entry.translated:
            fuzzy += 1
        elif entry.translated:
            translated += 1
        else:
            untranslated += 1
    return translated, fuzzy, untranslated


def run_checks(entries: list[POEntry], filename: str, *, use_fuzzy: bool = False) -> int:
    """Print problems to stderr; return the number of errors."""
    error_count = 0
    header = find_header(entries)
    if header is None:
        print(f"{filename}: warning: no header entry", file=sys.stderr)
    elif any(e.is_plural and not e.obsolete for e in entries) and nplurals_of(entries) is None:
        print(f"{filename}: error: plural messages but no valid Plural-Forms header", file=sys.stderr)
        error_count += 1
    nplurals = nplurals_of(entries)
    seen: set[tuple[str | None, str]] = set()
    for entry in entries:
        if entry.obsolete or entry.is_header:
            continue
        where = f"{filename}:{entry.lineno}"
        if entry.key in seen:
            print(f"{where}: error: duplicate message {entry.msgid!r}", file=sys.stderr)
            error_count += 1
        seen.add(entry.key)
        # Fuzzy entries are only skipped when they are not compiled (--use-fuzzy compiles them).
        if not entry.translated or (entry.fuzzy and not use_fuzzy):
            continue
        if entry.is_plural and nplurals is not None and len(entry.plural_forms()) != nplurals:
            print(f"{where}: error: {len(entry.plural_forms())} plural forms, header says {nplurals}",
                  file=sys.stderr)
            error_count += 1
        errors, warnings = check_entry(entry)
        for message in errors:
            print(f"{where}: error: {message}", file=sys.stderr)
        for message in warnings:
            print(f"{where}: warning: {message}", file=sys.stderr)
        error_count += len(errors)
    return error_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a .po file into a .mo file.")
    parser.add_argument("input", help="input .po file")
    parser.add_argument("-o", "--output", required=True, help="output .mo file")
    parser.add_argument("-c", "--check", action="store_true",
                        help="check placeholders and plural forms; fail on errors")
    parser.add_argument("--statistics", action="store_true", help="print translation statistics")
    parser.add_argument("-f", "--use-fuzzy", action="store_true", help="also compile fuzzy entries")
    args = parser.parse_args(argv)

    try:
        entries = read_po(args.input)
    except (OSError, POSyntaxError, UnicodeDecodeError, LookupError) as exc:
        print(f"msgfmt.py: {exc}", file=sys.stderr)
        return 1
    if args.check and run_checks(entries, args.input, use_fuzzy=args.use_fuzzy):
        print(f"msgfmt.py: {args.input}: not compiled because of the errors above", file=sys.stderr)
        return 1
    try:
        atomic_write(Path(args.output), compile_mo(entries, use_fuzzy=args.use_fuzzy))
    except (OSError, UnicodeEncodeError) as exc:
        print(f"msgfmt.py: {exc}", file=sys.stderr)
        return 1
    if args.statistics:
        translated, fuzzy, untranslated = statistics(entries)
        print(f"{args.input}: {translated} translated, {fuzzy} fuzzy, {untranslated} untranslated",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
