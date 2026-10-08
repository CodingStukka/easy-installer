#!/usr/bin/env python3
"""Extract translatable strings from Python sources into a .pot template (standard library only).

Usage:
    xgettext.py -o po/easy-installer.pot [--files-from po/POTFILES] [FILE.py ...]

Finds calls with literal string arguments (implicit concatenation and "a" + "b" are fine):
    _("text")   N_("text")   gettext("text")
    ngettext("singular", "plural", n)
    pgettext("context", "text")   npgettext("context", "singular", "plural", n)
also as attributes (i18n._("text")). f-strings and non-literal arguments cannot be translated
and are reported as warnings.

Output: "#: file:line" references, "#. Translators: ..." comments taken from a comment block
that starts with "Translators:" right above the call (or above its statement),
"#, python-format" / "#, python-brace-format" flags, messages in order of first appearance.
The file is left untouched when only POT-Creation-Date would change.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import sys
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from msgfmt import (  # noqa: E402
    POEntry,
    brace_placeholders,
    format_po,
    percent_placeholders,
    write_text_if_changed,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_INIT = REPO_ROOT / "src" / "easy_installer" / "__init__.py"

# keyword -> meaning of the positional arguments
KEYWORDS: dict[str, tuple[str, ...]] = {
    "_": ("msgid",),
    "N_": ("msgid",),
    "gettext": ("msgid",),
    "ngettext": ("msgid", "msgid_plural"),
    "pgettext": ("msgctxt", "msgid"),
    "npgettext": ("msgctxt", "msgid", "msgid_plural"),
}
TRANSLATOR_TAG = "translators:"
CREATION_DATE_RE = re.compile(r'^"POT-Creation-Date: .*\\n"$', re.MULTILINE)


@dataclass
class Message:
    msgid: str
    msgid_plural: str | None = None
    msgctxt: str | None = None
    references: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


class Extractor(ast.NodeVisitor):
    def __init__(self, filename: str, source: str):
        self.filename = filename
        self.comment_lines = _comment_only_lines(source)
        self.statement_lines: list[int] = []
        self.found: list[tuple[int, int, Message]] = []
        self.warnings: list[str] = []

    def visit(self, node: ast.AST) -> None:
        is_statement = isinstance(node, ast.stmt)
        if is_statement:
            self.statement_lines.append(node.lineno)
        try:
            if isinstance(node, ast.Call):
                self._call(node)
            self.generic_visit(node)
        finally:
            if is_statement:
                self.statement_lines.pop()

    def _call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name not in KEYWORDS:
            return
        spec = KEYWORDS[name]
        if len(node.args) < len(spec):
            return
        values: dict[str, str] = {}
        for role, arg in zip(spec, node.args):
            value = literal_string(arg)
            if value is None:
                if isinstance(arg, ast.JoinedStr):
                    self.warnings.append(
                        f"{self.filename}:{arg.lineno}: f-string in {name}() cannot be translated; "
                        "use a placeholder and .format() instead"
                    )
                return
            values[role] = value
        if values["msgid"] == "":
            self.warnings.append(f"{self.filename}:{node.lineno}: empty string in {name}() ignored")
            return
        msgid_node = node.args[spec.index("msgid")]
        message = Message(
            msgid=values["msgid"],
            msgid_plural=values.get("msgid_plural"),
            msgctxt=values.get("msgctxt"),
            references=[f"{self.filename}:{msgid_node.lineno}"],
            comments=self._translator_comments(node.lineno),
        )
        self.found.append((msgid_node.lineno, msgid_node.col_offset, message))

    def _translator_comments(self, call_line: int) -> list[str]:
        candidates = [call_line]
        if self.statement_lines and self.statement_lines[-1] != call_line:
            candidates.append(self.statement_lines[-1])
        for line in candidates:
            block: list[str] = []
            current = line - 1
            while current in self.comment_lines:
                block.insert(0, self.comment_lines[current])
                current -= 1
            for i, text in enumerate(block):
                if text.lower().startswith(TRANSLATOR_TAG):
                    return block[i:]
        return []


def literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal_string(node.left), literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _comment_only_lines(source: str) -> dict[int, str]:
    """Line number -> comment text (without '#') for lines that contain only a comment."""
    result: dict[int, str] = {}
    lines = source.splitlines()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                row = token.start[0]
                if lines[row - 1].lstrip().startswith("#"):
                    result[row] = token.string[1:].strip()
    except (tokenize.TokenError, SyntaxError, IndexError):
        pass
    return result


def extract_file(path: Path, display_name: str) -> tuple[list[Message], list[str]]:
    """Raise SyntaxError/OSError/UnicodeDecodeError if the file cannot be read or parsed."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=display_name)
    extractor = Extractor(display_name, source)
    extractor.visit(tree)
    extractor.found.sort(key=lambda item: (item[0], item[1]))
    return [message for _, _, message in extractor.found], extractor.warnings


def merge_messages(messages: list[Message]) -> tuple[list[Message], list[str]]:
    merged: dict[tuple[str | None, str], Message] = {}
    warnings: list[str] = []
    for message in messages:
        key = (message.msgctxt, message.msgid)
        existing = merged.get(key)
        if existing is None:
            merged[key] = Message(message.msgid, message.msgid_plural, message.msgctxt,
                                  list(message.references), list(message.comments))
            continue
        for ref in message.references:
            if ref not in existing.references:
                existing.references.append(ref)
        for comment in message.comments:
            if comment not in existing.comments:
                existing.comments.append(comment)
        if message.msgid_plural is not None:
            if existing.msgid_plural is None:
                existing.msgid_plural = message.msgid_plural
            elif existing.msgid_plural != message.msgid_plural:
                warnings.append(
                    f"{message.references[0]}: {message.msgid!r} is used with two different plural "
                    f"forms; keeping {existing.msgid_plural!r}"
                )
    return list(merged.values()), warnings


def format_flags(message: Message) -> list[str]:
    texts = [message.msgid] + ([message.msgid_plural] if message.msgid_plural is not None else [])
    flags = []
    if any(any(percent_placeholders(t)) for t in texts):
        flags.append("python-format")
    if any(brace_placeholders(t) for t in texts):
        flags.append("python-brace-format")
    return flags


def to_entry(message: Message) -> POEntry:
    return POEntry(
        msgid=message.msgid,
        msgid_plural=message.msgid_plural,
        msgctxt=message.msgctxt,
        msgstr_plural={0: "", 1: ""} if message.msgid_plural is not None else {},
        extracted_comments=list(message.comments),
        references=list(message.references),
        flags=format_flags(message),
    )


def read_version() -> str:
    try:
        tree = ast.parse(PACKAGE_INIT.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = literal_string(node.value)
            if value:
                return value
    return ""


def creation_date() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        return time.strftime("%Y-%m-%d %H:%M+0000", time.gmtime(int(epoch)))
    return time.strftime("%Y-%m-%d %H:%M%z")


def header_entry(package: str, version: str, holder: str) -> POEntry:
    year = time.strftime("%Y")
    fields = [
        ("Project-Id-Version", f"{package} {version}".strip()),
        ("Report-Msgid-Bugs-To", ""),
        ("POT-Creation-Date", creation_date()),
        ("PO-Revision-Date", "YEAR-MO-DA HO:MI+ZONE"),
        ("Last-Translator", "FULL NAME <EMAIL@ADDRESS>"),
        ("Language-Team", "LANGUAGE <LL@li.org>"),
        ("Language", ""),
        ("MIME-Version", "1.0"),
        ("Content-Type", "text/plain; charset=UTF-8"),
        ("Content-Transfer-Encoding", "8bit"),
        ("Plural-Forms", "nplurals=INTEGER; plural=EXPRESSION;"),
    ]
    return POEntry(
        msgid="",
        msgstr="".join(f"{name}: {value}\n" for name, value in fields),
        translator_comments=[
            "Translation template for Easy Installer.",
            f"Copyright (C) {year} {holder}",
            f"This file is distributed under the same license as the {package} package.",
            "FIRST AUTHOR <EMAIL@ADDRESS>, YEAR.",
            "",
        ],
        flags=["fuzzy"],
    )


def read_file_list(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract translatable strings into a .pot file.")
    parser.add_argument("files", nargs="*", help="Python files (relative to --directory)")
    parser.add_argument("-o", "--output", required=True, help="output .pot file")
    parser.add_argument("-f", "--files-from", help="file with one source path per line (e.g. po/POTFILES)")
    parser.add_argument("-D", "--directory", default=".", help="base directory of the source paths")
    parser.add_argument("--package-name", default="easy-installer")
    parser.add_argument("--package-version", default=None, help="default: __version__ of the package")
    parser.add_argument("--copyright-holder", default="roothirsch")
    args = parser.parse_args(argv)

    names = list(args.files)
    if args.files_from:
        try:
            names += read_file_list(Path(args.files_from))
        except OSError as exc:
            print(f"xgettext.py: {exc}", file=sys.stderr)
            return 1
    if not names:
        parser.error("no input files (give files or --files-from)")

    base = Path(args.directory)
    messages: list[Message] = []
    failed = False
    for name in dict.fromkeys(names):
        path = base / name
        if not path.is_file():
            print(f"xgettext.py: warning: {name} does not exist, skipped", file=sys.stderr)
            continue
        try:
            found, warnings = extract_file(path, name)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"xgettext.py: error: {name}: {exc}", file=sys.stderr)
            failed = True
            continue
        for warning in warnings:
            print(f"xgettext.py: warning: {warning}", file=sys.stderr)
        messages += found
    if failed:
        return 1

    unique, warnings = merge_messages(messages)
    for warning in warnings:
        print(f"xgettext.py: warning: {warning}", file=sys.stderr)
    version = args.package_version if args.package_version is not None else read_version()
    entries = [header_entry(args.package_name, version, args.copyright_holder)]
    entries += [to_entry(message) for message in unique]
    output = Path(args.output)
    written = write_text_if_changed(output, format_po(entries), ignore=CREATION_DATE_RE)
    state = "written" if written else "unchanged"
    print(f"xgettext.py: {output}: {len(unique)} messages ({state})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
