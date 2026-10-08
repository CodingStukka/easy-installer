"""Round-trip ``.desktop`` file parser/writer and ``Exec`` command line quoting.

The parser keeps every line of the original file (comments, blank lines, unknown or invalid
lines, group and key order), so ``DesktopEntry.parse(text).to_text()`` reproduces ``text``
(apart from a leading BOM, CRLF line endings and a missing final newline).

Value escaping follows the Desktop Entry Specification: ``\\s \\n \\t \\r \\\\`` in string values,
plus ``\\;`` inside lists. ``Exec`` values have a second level of quoting (double quotes with
``\\" \\` \\$ \\\\`` escapes and ``%%`` for a literal percent sign), handled by :func:`split_exec`
and :func:`join_exec`.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

# --------------------------------------------------------------------------------------------
# Value escaping
# --------------------------------------------------------------------------------------------

_UNESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


def escape_value(s: str) -> str:
    """Escape a string for use as a desktop entry value (inverse of :func:`unescape_value`)."""
    out = (
        s.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    # Leading (and, defensively, trailing) spaces would be stripped by parsers.
    stripped = out.lstrip(" ")
    out = "\\s" * (len(out) - len(stripped)) + stripped
    body = out.rstrip(" ")
    return body + "\\s" * (len(out) - len(body))


def _unescape(raw: str, *, list_mode: bool) -> list[str]:
    """Unescape ``raw``; in list mode split at unescaped ``;`` (dropping empty elements)."""
    items: list[str] = []
    buf: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in _UNESCAPES:
                buf.append(_UNESCAPES[nxt])
            elif list_mode and nxt == ";":
                buf.append(";")
            else:  # unknown escape: keep verbatim
                buf.append(c + nxt)
            i += 2
            continue
        if list_mode and c == ";":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    items.append("".join(buf))
    if list_mode:
        return [item for item in items if item != ""]
    return items


def unescape_value(s: str) -> str:
    return _unescape(s, list_mode=False)[0]


# --------------------------------------------------------------------------------------------
# Locale matching
# --------------------------------------------------------------------------------------------

_LOCALE_RE = re.compile(r"^([A-Za-z]+)(?:_([A-Za-z0-9]+))?(?:\.[^@]*)?(?:@(.+))?$")


def locale_variants(locale_name: str) -> list[str]:
    """``de_DE.UTF-8@euro`` -> ``["de_DE@euro", "de_DE", "de@euro", "de"]`` (spec order)."""
    match = _LOCALE_RE.match(locale_name.strip())
    if not match:
        return []
    lang, country, modifier = match.groups()
    variants = []
    if country and modifier:
        variants.append(f"{lang}_{country}@{modifier}")
    if country:
        variants.append(f"{lang}_{country}")
    if modifier:
        variants.append(f"{lang}@{modifier}")
    variants.append(lang)
    return variants


def default_locales(env: Mapping[str, str] | None = None) -> list[str]:
    """Locale names from ``LANGUAGE`` / ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG`` (gettext order)."""
    env = os.environ if env is None else env
    effective = ""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            effective = env[var]
            break
    if effective.split(".")[0] in ("", "C", "POSIX"):
        return []
    names = [x for x in env.get("LANGUAGE", "").split(":") if x]
    names.append(effective)
    return [n for n in dict.fromkeys(names) if n.split(".")[0] not in ("C", "POSIX")]


# --------------------------------------------------------------------------------------------
# DesktopEntry
# --------------------------------------------------------------------------------------------

#: What GLib's key file parser skips (g_ascii_isspace); Unicode spaces such as U+00A0 are content.
ASCII_WHITESPACE = " \t\n\v\f\r"
#: Characters no launcher line may contain: control characters other than TAB (and the LF that
#: ends a line), the Unicode line/paragraph separators and lone surrogates (bytes that are not
#: UTF-8). GLib rejects some of them (in group names: the whole file); others - a lone CR, VT,
#: FF, U+001C-1E, NEL, U+2028 - are line breaks to Python's splitlines() and other "universal
#: newline" parsers (pyxdg), so the line would mean different things to different parsers. C1
#: characters are what text encoded twice (mojibake) contains. remove_invalid_lines() drops such
#: lines and groups.
INVALID_CHARS_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
#: Group names: not even a TAB (g_key_file_is_group_name() refuses all ASCII control characters).
_INVALID_GROUP_CHARS_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")
_GROUP_RE = re.compile(r"^\[([^\[\]]+)\][ \t]*$")
_ENTRY_RE = re.compile(r"^([^\s=\[\]#][^\s=\[\]]*(?:\[[^\s\[\]=]+\])?)[ \t]*=[ \t]*(.*)$")
#: Like GLib's g_key_file_is_key_name() for the locale: letters, digits and "-_.@" (ASCII only).
_VALID_KEY_RE = re.compile(r"^[A-Za-z0-9-]+(?:\[[A-Za-z0-9_.@-]+\])?$")


@dataclass
class _Line:
    raw: str
    key: str | None = None     # set for "Key=value" lines
    value: str | None = None   # raw (escaped) value


@dataclass
class _Group:
    name: str
    header: str
    lines: list[_Line] = field(default_factory=list)


def _entry_line(key: str, raw_value: str) -> _Line:
    return _Line(f"{key}={raw_value}", key, raw_value)


def _check_key(key: str) -> None:
    if not key or "=" in key or "\n" in key or "\r" in key or key != key.strip() or key.startswith(("#", "[")):
        raise ValueError(f"invalid desktop entry key: {key!r}")


def _check_group(group: str) -> None:
    if not group or any(c in group for c in "[]\n\r"):
        raise ValueError(f"invalid desktop entry group name: {group!r}")


def is_valid_key(key: str) -> bool:
    """True if ``key`` (with optional ``[locale]``) only uses characters allowed by the spec."""
    return bool(_VALID_KEY_RE.match(key))


class DesktopEntry:
    MAIN = "Desktop Entry"

    def __init__(self) -> None:
        self._preamble: list[_Line] = []
        self._groups: list[_Group] = []

    # -- construction ---------------------------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "DesktopEntry":
        entry = cls()
        if text.startswith("﻿"):
            text = text[1:]
        text = text.replace("\r\n", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        current: list[_Line] = entry._preamble
        for raw in lines:
            stripped = raw.strip(ASCII_WHITESPACE)
            group_match = _GROUP_RE.match(stripped) if stripped.startswith("[") else None
            if group_match:
                group = _Group(group_match.group(1), raw)
                entry._groups.append(group)
                current = group.lines
                continue
            if not stripped or stripped.startswith("#") or current is entry._preamble:
                current.append(_Line(raw))
                continue
            match = _ENTRY_RE.match(raw.lstrip(ASCII_WHITESPACE))
            if match:
                current.append(_Line(raw, match.group(1), match.group(2)))
            else:
                current.append(_Line(raw))  # junk, kept verbatim
        return entry

    @classmethod
    def new(cls) -> "DesktopEntry":
        entry = cls()
        entry.add_group(cls.MAIN)
        return entry

    def copy(self) -> "DesktopEntry":
        return copy.deepcopy(self)

    # -- lookups --------------------------------------------------------------------------

    def _groups_named(self, group: str) -> list[_Group]:
        return [g for g in self._groups if g.name == group]

    def groups(self) -> list[str]:
        return list(dict.fromkeys(g.name for g in self._groups))

    def has_group(self, group: str) -> bool:
        return any(g.name == group for g in self._groups)

    def keys(self, group: str = MAIN) -> list[str]:
        keys = (line.key for g in self._groups_named(group) for line in g.lines if line.key)
        return list(dict.fromkeys(keys))

    def get_raw(self, key: str, group: str = MAIN) -> str | None:
        value = None
        for g in self._groups_named(group):
            for line in g.lines:
                if line.key == key:
                    value = line.value  # last one wins, like GLib
        return value

    def get(self, key: str, group: str = MAIN) -> str | None:
        raw = self.get_raw(key, group)
        return None if raw is None else unescape_value(raw)

    def get_localized(self, key: str, locales: Sequence[str] | None = None,
                      group: str = MAIN) -> str | None:
        if locales is None:
            locales = default_locales()
        for locale_name in locales:
            for variant in locale_variants(locale_name):
                value = self.get(f"{key}[{variant}]", group)
                if value is not None:
                    return value
        return self.get(key, group)

    def get_list(self, key: str, group: str = MAIN) -> list[str]:
        raw = self.get_raw(key, group)
        return [] if raw is None else _unescape(raw, list_mode=True)

    def get_bool(self, key: str, group: str = MAIN, default: bool = False) -> bool:
        raw = self.get_raw(key, group)
        if raw is None:
            return default
        value = raw.strip().lower()
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
        return default

    # -- modification ---------------------------------------------------------------------

    def set(self, key: str, value: str, group: str = MAIN) -> None:
        self.set_raw(key, escape_value(value), group)

    def set_raw(self, key: str, raw: str, group: str = MAIN) -> None:
        _check_key(key)
        if "\n" in raw or "\r" in raw:
            raise ValueError("raw desktop entry values must not contain line breaks")
        replaced = False
        for g in self._groups_named(group):
            kept: list[_Line] = []
            for line in g.lines:
                if line.key == key:
                    if replaced:
                        continue  # drop duplicates
                    kept.append(_entry_line(key, raw))
                    replaced = True
                else:
                    kept.append(line)
            g.lines = kept
        if replaced:
            return
        if not self.has_group(group):
            self.add_group(group)
        target = self._groups_named(group)[0]
        last_key = max((i for i, line in enumerate(target.lines) if line.key), default=-1)
        target.lines.insert(last_key + 1, _entry_line(key, raw))

    def set_list(self, key: str, values: Sequence[str], group: str = MAIN) -> None:
        raw = "".join(escape_value(v).replace(";", "\\;") + ";" for v in values)
        self.set_raw(key, raw, group)

    def remove(self, key: str, group: str = MAIN, *, localized_too: bool = False) -> None:
        def matches(line_key: str | None) -> bool:
            if line_key is None:
                return False
            return line_key == key or (localized_too and line_key.startswith(key + "["))

        for g in self._groups_named(group):
            g.lines = [line for line in g.lines if not matches(line.key)]

    def add_group(self, group: str) -> None:
        _check_group(group)
        if self.has_group(group):
            return
        last_lines = self._groups[-1].lines if self._groups else self._preamble
        has_content = bool(self._groups) or bool(self._preamble)
        if has_content:
            tail = last_lines[-1].raw if last_lines else (self._groups[-1].header if self._groups else "")
            if tail.strip():
                last_lines.append(_Line(""))
        self._groups.append(_Group(group, f"[{group}]"))

    def remove_group(self, group: str) -> None:
        self._groups = [g for g in self._groups if g.name != group]

    def move_group_to_front(self, group: str) -> None:
        """Make ``group`` the first group (the spec requires ``[Desktop Entry]`` to come first)."""
        if not self._groups or self._groups[0].name == group:
            return
        first = [g for g in self._groups if g.name == group]
        if not first:
            return
        rest = [g for g in self._groups if g.name != group]
        if not first[-1].lines or first[-1].lines[-1].raw.strip():
            first[-1].lines.append(_Line(""))
        self._groups = first + rest

    def remove_invalid_lines(self) -> None:
        """Drop junk lines, keys and groups that GLib or other parsers would not read alike.

        Like GLib, only ASCII whitespace counts as blank. Lines (also comments) and whole groups
        whose name contains a character of :data:`INVALID_CHARS_RE` (a lone CR, U+2028, other
        control characters; in group names also TAB) are dropped as well.
        """
        def keep(line: _Line) -> bool:
            if INVALID_CHARS_RE.search(line.raw):
                return False
            if line.key is not None:
                return is_valid_key(line.key)
            stripped = line.raw.strip(ASCII_WHITESPACE)
            return not stripped or stripped.startswith("#")

        self._preamble = [line for line in self._preamble if keep(line) and line.key is None]
        self._groups = [g for g in self._groups if not _INVALID_GROUP_CHARS_RE.search(g.name)]
        for g in self._groups:
            g.header = f"[{g.name}]"
            g.lines = [line for line in g.lines if keep(line)]

    # -- output ---------------------------------------------------------------------------

    def to_text(self) -> str:
        out = [line.raw for line in self._preamble]
        for g in self._groups:
            out.append(g.header)
            out.extend(line.raw for line in g.lines)
        return "\n".join(out) + "\n" if out else ""

    def __repr__(self) -> str:
        return f"<DesktopEntry groups={self.groups()!r}>"


# --------------------------------------------------------------------------------------------
# Exec quoting
# --------------------------------------------------------------------------------------------

FIELD_CODES = frozenset({"%f", "%F", "%u", "%U", "%i", "%c", "%k"})
DEPRECATED_FIELD_CODES = frozenset({"%d", "%D", "%n", "%N", "%v", "%m"})
_ALL_FIELD_CODES = FIELD_CODES | DEPRECATED_FIELD_CODES
_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_QUOTED_ESCAPES = frozenset('"`$\\')
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WHITESPACE = " \t\n"


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


def _tokenize(value: str) -> list[_Token]:
    """Split an (already string-unescaped) Exec value into arguments, remembering their spans.

    Double quotes allow the escapes ``\\" \\` \\$ \\\\``; single quotes and backslashes outside of
    quotes are accepted like GLib's ``g_shell_parse_argv`` does. ``%%`` becomes ``%``; field
    codes are kept as they are.
    """
    tokens: list[_Token] = []
    i, n = 0, len(value)
    while i < n:
        while i < n and value[i] in _WHITESPACE:
            i += 1
        if i >= n:
            break
        start = i
        buf: list[str] = []
        while i < n and value[i] not in _WHITESPACE:
            c = value[i]
            if c == '"':
                i += 1
                while True:
                    if i >= n:
                        raise ValueError(f"unbalanced double quote in Exec value: {value!r}")
                    c = value[i]
                    if c == '"':
                        i += 1
                        break
                    if c == "\\" and i + 1 < n and value[i + 1] in _QUOTED_ESCAPES:
                        buf.append(value[i + 1])
                        i += 2
                    elif c == "%" and i + 1 < n and value[i + 1] == "%":
                        buf.append("%")
                        i += 2
                    else:
                        buf.append(c)
                        i += 1
            elif c == "'":
                end = value.find("'", i + 1)
                if end < 0:
                    raise ValueError(f"unbalanced single quote in Exec value: {value!r}")
                buf.append(value[i + 1:end].replace("%%", "%"))
                i = end + 1
            elif c == "\\":
                if i + 1 < n:
                    buf.append(value[i + 1])
                    i += 2
                else:
                    buf.append("\\")
                    i += 1
            elif c == "%" and i + 1 < n and value[i + 1] == "%":
                buf.append("%")
                i += 2
            else:
                buf.append(c)
                i += 1
        tokens.append(_Token("".join(buf), start, i))
    return tokens


def split_exec(value: str) -> list[str]:
    """Split an unescaped ``Exec`` value into arguments. Raises ValueError on unbalanced quotes."""
    return [t.value for t in _tokenize(value)]


def quote_exec_arg(arg: str) -> str:
    if arg in _ALL_FIELD_CODES:
        return arg
    text = arg.replace("%", "%%")
    if text == "":
        return '""'
    if any(c in _RESERVED for c in text):
        return '"' + "".join("\\" + c if c in _QUOTED_ESCAPES else c for c in text) + '"'
    return text


def join_exec(args: Sequence[str]) -> str:
    """Inverse of :func:`split_exec`: build an (unescaped) ``Exec`` value from literal arguments."""
    return " ".join(quote_exec_arg(a) for a in args)


def exec_env(value: str) -> list[tuple[str, str]] | None:
    """The ``VAR=value`` assignments of a leading ``env`` (None if the value cannot be parsed)."""
    try:
        tokens = _tokenize(value or "")
    except ValueError:
        return None
    if not tokens or tokens[0].value != "env":
        return []
    index = _program_index(tokens)
    assignments = tokens[1:index if index is not None else len(tokens)]
    return [tuple(t.value.partition("=")[::2]) for t in assignments]  # type: ignore[misc]


def exec_program(value: str) -> str | None:
    """The program of an Exec value, skipping a leading ``env VAR=value ...``; None if unknown."""
    try:
        tokens = _tokenize(value or "")
    except ValueError:
        return None
    index = _program_index(tokens)
    return tokens[index].value if index is not None else None


def _program_index(tokens: Sequence[_Token]) -> int | None:
    index = 0
    if tokens and tokens[0].value == "env":
        index = 1
        while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index].value):
            index += 1
    return index if index < len(tokens) else None


def rewrite_exec(value: str, program: str, *, extra_args: Sequence[str] = (),
                 env: Mapping[str, str] | None = None,
                 keep_env: Callable[[str, str], bool] | None = None) -> str:
    """Replace the program of an ``Exec`` value, keeping its arguments and field codes.

    The arguments after the program are kept verbatim (including their original quoting).
    Assignments of a leading ``env VAR=value`` are kept (only those ``keep_env(name, value)``
    accepts, if given) and merged with ``env``; ``extra_args`` that are not yet present are
    inserted right after the program. A program whose path contains ``%`` is started through
    ``env`` (else GLib does not load the launcher).
    """
    try:
        tokens = _tokenize(value or "")
    except ValueError:
        tokens = []
    env_items: dict[str, str] = {}
    tail = ""
    existing: list[str] = []
    index = _program_index(tokens)
    if tokens and tokens[0].value == "env":
        for token in tokens[1:index if index is not None else len(tokens)]:
            name, _sep, val = token.value.partition("=")
            if keep_env is None or keep_env(name, val):
                env_items[name] = val
    if index is not None:
        tail = value[tokens[index].end:].strip(_WHITESPACE)
        existing = [t.value for t in tokens[index + 1:]]
    if env:
        env_items.update(env)
    missing = [a for a in dict.fromkeys(extra_args) if a not in existing]
    head: list[str] = []
    if env_items:
        head = ["env", *(f"{k}={v}" for k, v in env_items.items())]
    elif "%" in program:
        # GLib looks the program up before it expands field codes, i.e. as ".../App 100%%",
        # and would refuse to load the launcher: let ``env`` start it.
        head = ["env"]
    command = join_exec([*head, program, *missing])
    return f"{command} {tail}" if tail else command
