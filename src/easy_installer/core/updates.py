"""Update sources, update checks and downloads.

Pure module: it knows nothing about the registry or the installer (``core/updater.py`` puts the
pieces together). What an update source is comes from the app itself: the AppImage ``.upd_info``
section or electron-builder's ``resources/app-update.yml``. Only the hosts named there are ever
contacted, only over https, without tokens and without telemetry.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import email.utils
import errno
import fnmatch
import hashlib
import hmac
import http.client
import json
import locale
import logging
import os
import posixpath
import re
import shutil
import socket
import ssl
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .. import __version__
from ..errors import EasyInstallerError, NetworkError, UpdateCancelled, UpdateError
from ..i18n import _, ngettext
from .integration import compare_versions, normalize_version, parse_version_from_filename
from .paths import cache_home

log = logging.getLogger(__name__)

T = TypeVar("T")
ProgressCallback = Callable[[float | None, str], None]

KIND_GITHUB_ASSETS = "github-assets"
KIND_ELECTRON_GITHUB = "electron-github"
KIND_ELECTRON_GENERIC = "electron-generic"
KIND_ZSYNC_URL = "zsync-url"
SOURCE_KINDS = (KIND_GITHUB_ASSETS, KIND_ELECTRON_GITHUB, KIND_ELECTRON_GENERIC, KIND_ZSYNC_URL)
VIA_UPD_INFO = "upd_info"
VIA_APP_UPDATE_YML = "app-update.yml"

RELEASE_LATEST = "latest"
RELEASE_LATEST_PRE = "latest-pre"
RELEASE_LATEST_ALL = "latest-all"

USER_AGENT = f"EasyInstaller/{__version__}"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
GITHUB_API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
#: How many releases are looked at for "latest-pre" / "latest-all" / Electron pre-release channels.
RELEASES_PER_PAGE = 20

#: Connect timeout and the longest pause between two reads.
METADATA_TIMEOUT = 20.0
#: A server that keeps sending a few bytes every some seconds must not block a check forever.
METADATA_DEADLINE = 90.0
DOWNLOAD_TIMEOUT = 30.0
MAX_REDIRECTS = 8

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_YML_BYTES = 1024 * 1024
MAX_ZSYNC_HEADER_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_UPDATE_INFO_LENGTH = 2048
MAX_URL_LENGTH = 2048
MAX_FILENAME_LENGTH = 200
MAX_DOWNLOAD_BYTES = 16 * 1024 ** 3
CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL = 0.1

MAX_YAML_LINES = 5000
MAX_YAML_ITEMS = 500
MAX_ASSETS = 2000

CACHE_FORMAT = 1
CACHE_FILE_NAME = "updates.json"
CACHE_DIR_MODE = 0o700
CACHE_FILE_MODE = 0o600
#: Answers larger than this are not remembered for "304 Not Modified" (the cache stays small).
MAX_CACHED_BODY = 256 * 1024
MAX_ETAGS = 100
MAX_ETAG_LENGTH = 256

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
APPIMAGE_SUFFIX = ".appimage"

_GITHUB_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/@-]{0,127}\Z")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+\Z")
_ARCH_RE = re.compile(
    r"(?<![A-Za-z0-9])(x86[_-]64|amd64|x64|aarch64|arm64|armv7l|armv7|armhf|i[3-6]86|ia32)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_ARCH_ALIASES = {
    "x86_64": "x86_64", "x86-64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64",
    "armhf": "armhf", "armv7l": "armhf", "armv7": "armhf",
    "i386": "i686", "i486": "i686", "i586": "i686", "i686": "i686", "ia32": "i686",
}
#: electron-builder's update file per CPU: "latest-linux.yml" (x64), "latest-linux-arm64.yml", ...
_ELECTRON_ARCH_SUFFIX = {"x86_64": "", "aarch64": "-arm64", "armhf": "-arm", "i686": "-ia32"}


# ------------------------------------------------------------------------------------------------
# errors and small helpers
# ------------------------------------------------------------------------------------------------

def _unreadable(details: str | None = None) -> UpdateError:
    return UpdateError(_("The update information of this app could not be read."), details)


def _no_update_info(details: str | None = None) -> UpdateError:
    return UpdateError(_("No update information was found for this app."), details)


def _insecure(details: str | None = None) -> UpdateError:
    return UpdateError(
        _("The update would come from an address that is not secure, so Easy Installer did not "
          "use it."), details)


def _noop_progress(fraction: float | None, message: str) -> None:
    pass


def utc_timestamp(when: datetime | None = None) -> str:
    """ISO-8601 UTC with second precision ("2026-09-30T08:15:00Z"), like the registry's dates."""
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CACHE_HOME/easy-installer`` (update cache, downloads)."""
    return cache_home(env) / "easy-installer"


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 for ch in text)


def _clean_text(value: Any, max_length: int) -> str | None:
    """``value`` if it is a short one-line text, else None."""
    if not isinstance(value, str) or _has_control(value):
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    return value


def _format_size(size: int) -> str:
    megabytes = size / 1_000_000
    if 0 < megabytes < 0.1:
        megabytes = 0.1
    return _("{size} MB").format(size=locale.format_string("%.1f", megabytes))


# ------------------------------------------------------------------------------------------------
# URLs and digests
# ------------------------------------------------------------------------------------------------

def is_secure_url(url: Any, *, allow_insecure_localhost: bool = False) -> bool:
    """True for an https URL with a host and without credentials. Plain http is accepted only
    for 127.0.0.1/localhost and only with ``allow_insecure_localhost`` (tests)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return False
    if not url.isascii() or "\\" in url or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port   # raises ValueError for a port that is not a number
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    scheme = parts.scheme.lower()
    if scheme == "https":
        return True
    return scheme == "http" and allow_insecure_localhost and host.lower() in LOCAL_HOSTS


def _require_secure_url(url: Any, allow_insecure_localhost: bool = False) -> str:
    if not is_secure_url(url, allow_insecure_localhost=allow_insecure_localhost):
        raise _insecure(f"refused address: {url!r}")
    return url


def _url_host(url: str | None) -> str:
    try:
        return urllib.parse.urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def normalize_hex_digest(value: Any, size: int) -> str | None:
    """Lower-case hex of a ``size``-byte digest, or None if ``value`` is not one."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) != size * 2 or not _HEX_RE.match(value):
        return None
    return value.lower()


def base64_to_hex(value: Any, size: int | None = None) -> str | None:
    """Strictly decode a base64 digest (electron-builder's ``sha512``) to lower-case hex.
    None if it is not valid base64 or not ``size`` bytes long."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value.strip().encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or (size is not None and len(raw) != size):
        return None
    return raw.hex()


def hex_to_base64(value: str) -> str:
    """Inverse of :func:`base64_to_hex`; raises ValueError for text that is not hex."""
    return base64.b64encode(bytes.fromhex(value)).decode("ascii")


def digests_equal(a: str | None, b: str | None) -> bool:
    """Compare two hex digests in constant time, ignoring case. Missing digests never match."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.strip().lower().encode("utf-8"), b.strip().lower().encode("utf-8"))


# ------------------------------------------------------------------------------------------------
# file names and CPU architectures
# ------------------------------------------------------------------------------------------------

def _is_appimage_name(name: str) -> bool:
    return len(name) > len(APPIMAGE_SUFFIX) and name.lower().endswith(APPIMAGE_SUFFIX)


def safe_download_name(filename: str) -> str:
    """The file name a download is stored under: no folders, only ``[A-Za-z0-9._+-]``, never
    hidden, at most 200 characters. Raises UpdateError if it is not an ``.AppImage`` name."""
    name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._+-]", "_", name).lstrip(".-")
    if len(name) > MAX_FILENAME_LENGTH:
        name = name[: MAX_FILENAME_LENGTH - len(APPIMAGE_SUFFIX)] + name[-len(APPIMAGE_SUFFIX):]
    if not _is_appimage_name(name) or not name[: -len(APPIMAGE_SUFFIX)].strip("._"):
        raise UpdateError(_("The update is not an AppImage file, so Easy Installer cannot install it."),
                          details=f"file name: {filename!r}")
    return name


def _comparable_name(filename: str | None) -> str | None:
    if not filename:
        return None
    try:
        return safe_download_name(filename)
    except UpdateError:
        return str(filename)


def _canonical_arch(arch: str | None) -> str | None:
    return _ARCH_ALIASES.get((arch or "").strip().lower())


def _name_archs(name: str) -> set[str]:
    return {_ARCH_ALIASES[match.lower()] for match in _ARCH_RE.findall(name)}


def _arch_fits(name: str, arch: str | None) -> bool:
    """False only if the name clearly says that the file is for another CPU."""
    archs = _name_archs(name)
    return not archs or arch is None or arch in archs


def _without_version(name: str) -> str:
    version = parse_version_from_filename(name)
    return (name.replace(version, "", 1) if version else name).casefold()


def _name_tokens(name: str) -> set[str]:
    stem = _ARCH_RE.sub(" ", _without_version(name))
    return {token for token in re.split(r"[-_.\s]+", stem) if token and token != "appimage"}


def _closeness(name: str, current_filename: str | None, arch: str | None) -> tuple[int, int, int]:
    archs = _name_archs(name)
    arch_score = 2 if arch and arch in archs else (1 if not archs else 0)
    if not current_filename:
        return (0, arch_score, 0)
    same_scheme = int(_without_version(name) == _without_version(current_filename))
    mine, theirs = _name_tokens(current_filename), _name_tokens(name)
    return (same_scheme, arch_score, len(mine & theirs) - len(mine ^ theirs))


def _closest(names: Sequence[str], current_filename: str | None, arch: str | None) -> int | None:
    """Index of the name that fits the installed file best (the first one on a tie)."""
    best: tuple[tuple[int, int, int], int] | None = None
    for index, name in enumerate(names):
        key = (_closeness(name, current_filename, arch), -index)
        if best is None or key > best:
            best = key
    return -best[1] if best else None


# ------------------------------------------------------------------------------------------------
# update sources
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class UpdateSource:
    """Where an app publishes its new versions."""

    kind: str
    owner: str | None = None
    repo: str | None = None
    #: github-assets: "latest" | "latest-pre" | "latest-all" | a tag name
    release: str | None = None
    #: github-assets: glob of the AppImage asset (the ".zsync" suffix removed)
    pattern: str | None = None
    #: electron-generic: base URL; zsync-url: URL of the .zsync file
    url: str | None = None
    #: electron-github: the app follows pre-releases (releaseType == "prerelease")
    prerelease: bool = False
    #: "upd_info" | "app-update.yml"
    via: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "owner": self.owner, "repo": self.repo, "release": self.release,
                "pattern": self.pattern, "url": self.url, "prerelease": self.prerelease,
                "via": self.via}

    @classmethod
    def from_dict(cls, d: Any) -> UpdateSource | None:
        """The source stored in a registry entry; None if it is missing or not valid."""
        if not isinstance(d, Mapping):
            return None
        texts = {key: d.get(key) for key in ("kind", "owner", "repo", "release", "pattern", "url")}
        if any(value is not None and not isinstance(value, str) for value in texts.values()):
            return None
        if not isinstance(texts["kind"], str) or not isinstance(d.get("prerelease", False), bool):
            return None
        via = d.get("via", "")
        source = cls(prerelease=d.get("prerelease", False),
                     via=via if isinstance(via, str) and len(via) <= 32 and not _has_control(via) else "",
                     **texts)
        return source if source.is_valid() else None

    def is_valid(self) -> bool:
        """Every field has the form its kind needs (names that fit into a URL, https only)."""
        if self.kind in (KIND_GITHUB_ASSETS, KIND_ELECTRON_GITHUB):
            if not _valid_github_repo(self.owner, self.repo):
                return False
            if self.kind == KIND_ELECTRON_GITHUB:
                return True
            return _valid_release(self.release) and _valid_pattern(self.pattern)
        if self.kind in (KIND_ELECTRON_GENERIC, KIND_ZSYNC_URL):
            return is_secure_url(self.url) and "${" not in (self.url or "")
        return False

    def describe(self) -> str:
        """Short text for "from …": "github.com/FreeCAD/FreeCAD" or the host of the URL."""
        if self.kind in (KIND_GITHUB_ASSETS, KIND_ELECTRON_GITHUB):
            return f"github.com/{self.owner}/{self.repo}"
        return _url_host(self.url)

    def homepage(self) -> str | None:
        if self.kind in (KIND_GITHUB_ASSETS, KIND_ELECTRON_GITHUB) and self.owner and self.repo:
            return f"{GITHUB_WEB}/{self.owner}/{self.repo}"
        return None


def _valid_github_repo(owner: str | None, repo: str | None) -> bool:
    if not isinstance(owner, str) or not isinstance(repo, str):
        return False
    return bool(_GITHUB_OWNER_RE.match(owner) and _GITHUB_REPO_RE.match(repo)
                and repo not in (".", ".."))


def _valid_release(release: str | None) -> bool:
    return isinstance(release, str) and bool(_RELEASE_RE.match(release)) and ".." not in release


def _valid_pattern(pattern: str | None) -> bool:
    if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
        return False
    return pattern.isprintable() and "/" not in pattern and "\\" not in pattern and "|" not in pattern


def parse_update_info(text: str | None) -> UpdateSource | None:
    """The AppImage ``.upd_info`` section: ``gh-releases-zsync|owner|repo|release|glob.zsync`` or
    ``zsync|https://…/x.zsync``. Anything else (bintray, pling, http://, malformed) gives None."""
    if not isinstance(text, str):
        return None
    text = text.strip("\x00 \t\r\n")
    if not text or len(text) > MAX_UPDATE_INFO_LENGTH or _has_control(text):
        return None
    parts = text.split("|")
    if any(not part or part != part.strip() for part in parts):
        return None
    source: UpdateSource | None = None
    if parts[0] == "gh-releases-zsync" and len(parts) == 5 and parts[4].endswith(".zsync"):
        source = UpdateSource(kind=KIND_GITHUB_ASSETS, owner=parts[1], repo=parts[2],
                              release=parts[3], pattern=parts[4][: -len(".zsync")], via=VIA_UPD_INFO)
    elif parts[0] == "zsync" and len(parts) == 2:
        source = UpdateSource(kind=KIND_ZSYNC_URL, url=parts[1], via=VIA_UPD_INFO)
    return source if source is not None and source.is_valid() else None


def parse_electron_update_config(text: str | bytes | None) -> UpdateSource | None:
    """electron-builder's ``resources/app-update.yml``: provider ``github`` (owner, repo,
    releaseType) or ``generic`` (an https url). Other providers, GitHub Enterprise hosts, private
    repositories and special channels give None - the app then only updates itself."""
    data = _load_config(text)
    if data is None:
        return None
    provider = data.get("provider")
    channel = data.get("channel")
    if channel not in (None, "", "latest") or _yaml_true(data.get("private")):
        return None
    source: UpdateSource | None = None
    if provider == "github":
        source = _electron_github_source(data)
    elif provider == "generic" and isinstance(data.get("url"), str):
        source = UpdateSource(kind=KIND_ELECTRON_GENERIC, url=data["url"].strip(), via=VIA_APP_UPDATE_YML)
    return source if source is not None and source.is_valid() else None


def _load_config(text: str | bytes | None) -> dict[str, Any] | None:
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(text, str) or len(text) > MAX_CONFIG_BYTES:
        return None
    try:
        return parse_simple_yaml(text)
    except ValueError as exc:
        log.debug("app-update.yml not understood: %s", exc)
        return None


def _yaml_true(value: Any) -> bool:
    return isinstance(value, str) and value.lower() in ("true", "yes", "on")


def _electron_github_source(data: Mapping[str, Any]) -> UpdateSource | None:
    owner, repo = data.get("owner"), data.get("repo")
    if data.get("host") not in (None, "", "github.com"):
        return None
    if not isinstance(repo, str):
        return None
    if owner in (None, "") and repo.count("/") == 1:
        owner, repo = repo.split("/")
    release_type = data.get("releaseType")
    if not isinstance(owner, str) or release_type not in (None, "", "release", "prerelease"):
        return None   # "draft" releases are not public
    return UpdateSource(kind=KIND_ELECTRON_GITHUB, owner=owner, repo=repo,
                        prerelease=release_type == "prerelease", via=VIA_APP_UPDATE_YML)


def choose_source(update_info: str | None, electron_config: str | bytes | None) -> UpdateSource | None:
    """Prefer the Electron source (it publishes version and sha512), else ``.upd_info``."""
    return parse_electron_update_config(electron_config) or parse_update_info(update_info)


# ------------------------------------------------------------------------------------------------
# a small, strict YAML subset (what electron-builder writes) - there is no PyYAML here
# ------------------------------------------------------------------------------------------------

class SimpleYamlError(ValueError):
    """The text uses YAML that :func:`parse_simple_yaml` does not support."""


_YAML_ENTRY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:(?:[ \t]+(.*))?\Z")
_YAML_BLOCK_SCALAR_RE = re.compile(r"[|>](?:[+-][1-9]?|[1-9][+-]?)?(?:[ \t]+#.*)?\Z")
_YAML_COMMENT_RE = re.compile(r"[ \t]#")
_YAML_NULLS = frozenset({"null", "Null", "NULL", "~"})
_YAML_INDICATORS = frozenset("[]{}&*!|>%@`,#")
_YAML_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f",
                 "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\"}
_YAML_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the YAML subset of ``app-update.yml`` / ``latest-linux.yml``: top-level ``key: value``
    lines, plain or quoted one-line scalars (returned as ``str``, ``null``/``~`` as None), comments,
    one level of lists (of scalars or flat mappings) or flat mappings under a key, and block
    scalars (``key: |``) at the top level. Everything else - flow collections, anchors, aliases,
    tags, deeper nesting, tabs as indentation, duplicate keys - raises :class:`SimpleYamlError`."""
    return _SimpleYaml(text).parse()


def _yaml_entry(content: str) -> tuple[str, str | None]:
    match = _YAML_ENTRY_RE.match(content)
    if not match:
        raise SimpleYamlError(f"not a 'key: value' line: {content[:60]!r}")
    rest = (match.group(2) or "").strip()
    return match.group(1), (rest if rest and not rest.startswith("#") else None)


def _yaml_after_quote(rest: str) -> None:
    if rest.strip() and not (rest[0] in " \t" and rest.lstrip().startswith("#")):
        raise SimpleYamlError(f"unexpected text after a quoted value: {rest[:40]!r}")


def _yaml_single_quoted(text: str) -> str:
    parts: list[str] = []
    start = 1
    while True:
        end = text.find("'", start)
        if end < 0:
            raise SimpleYamlError("missing closing quote")
        parts.append(text[start:end])
        if text[end + 1: end + 2] != "'":
            _yaml_after_quote(text[end + 1:])
            return "".join(parts)
        parts.append("'")
        start = end + 2


def _yaml_escape(text: str, index: int) -> tuple[str, int]:
    """The character for the escape whose letter is at ``index``, and the index of its last char."""
    letter = text[index: index + 1]
    if letter in _YAML_ESCAPES:
        return _YAML_ESCAPES[letter], index
    width = _YAML_HEX_ESCAPES.get(letter)
    digits = text[index + 1: index + 1 + width] if width else ""
    if not width or len(digits) != width or not _HEX_RE.match(digits):
        raise SimpleYamlError(f"unsupported escape: \\{letter}")
    code = int(digits, 16)
    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        raise SimpleYamlError("escape is not a character")
    return chr(code), index + width


def _yaml_double_quoted(text: str) -> str:
    parts: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == '"':
            _yaml_after_quote(text[index + 1:])
            return "".join(parts)
        if char == "\\":
            char, index = _yaml_escape(text, index + 1)
        parts.append(char)
        index += 1
    raise SimpleYamlError("missing closing quote")


def _yaml_scalar(text: str) -> str | None:
    text = text.strip()
    if text.startswith("'"):
        return _yaml_single_quoted(text)
    if text.startswith('"'):
        return _yaml_double_quoted(text)
    comment = _YAML_COMMENT_RE.search(text)
    if comment:
        text = text[: comment.start()].rstrip()
    if not text or text[0] in _YAML_INDICATORS or text[:2] in ("- ", "? ", ": ") or text in ("-", "?", ":"):
        raise SimpleYamlError(f"unsupported value: {text[:40]!r}")
    if ": " in text or ":\t" in text or text.endswith(":"):
        raise SimpleYamlError(f"nested mapping in a value: {text[:40]!r}")
    return None if text in _YAML_NULLS else text


def _yaml_flat(rest: str | None) -> str | None:
    return None if rest is None else _yaml_scalar(rest)


def _is_list_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _SimpleYaml:
    def __init__(self, text: str) -> None:
        if not isinstance(text, str):
            raise SimpleYamlError("not text")
        if len(text) > MAX_YML_BYTES:
            raise SimpleYamlError("too large")
        if text.startswith("\ufeff"):
            text = text[1:]
        self.lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
        if len(self.lines) > MAX_YAML_LINES:
            raise SimpleYamlError("too many lines")
        self.pos = 0

    def parse(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        line = self._peek()
        if line == (0, "---"):
            self.pos += 1
        while (line := self._peek()) is not None:
            indent, content = line
            if indent:
                raise SimpleYamlError(f"unexpected indentation in line {self.pos + 1}")
            if content == "...":
                self.pos += 1
                if self._peek() is not None:
                    raise SimpleYamlError("text after the end of the document")
                break
            key, rest = _yaml_entry(content)
            if key in result:
                raise SimpleYamlError(f"duplicate key: {key}")
            self.pos += 1
            result[key] = self._value(rest)
        return result

    def _peek(self) -> tuple[int, str] | None:
        """The next line that is not blank or a comment as (indentation, content)."""
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            stripped = line.lstrip(" ")
            if not line.strip() or stripped.startswith("#"):
                self.pos += 1
                continue
            if stripped.startswith("\t"):
                raise SimpleYamlError(f"tab used as indentation in line {self.pos + 1}")
            return len(line) - len(stripped), stripped.rstrip()
        return None

    def _value(self, rest: str | None) -> Any:
        if rest is None:
            return self._block()
        if _YAML_BLOCK_SCALAR_RE.match(rest):
            return self._block_scalar()
        return _yaml_scalar(rest)

    def _block(self) -> Any:
        """What follows a top-level ``key:`` line: a list, a flat mapping, or nothing (null)."""
        line = self._peek()
        if line is None:
            return None
        indent, content = line
        if _is_list_item(content):
            return self._list(indent)
        return self._mapping(indent) if indent else None

    def _list(self, list_indent: int) -> list[Any]:
        items: list[Any] = []
        while (line := self._peek()) is not None:
            indent, content = line
            if indent < list_indent or (indent == list_indent and not _is_list_item(content)):
                break
            if indent > list_indent or len(items) >= MAX_YAML_ITEMS:
                raise SimpleYamlError(f"unsupported list structure in line {self.pos + 1}")
            body = content[1:]
            item_indent = indent + 1 + len(body) - len(body.lstrip(" "))
            body = body.strip()
            if not body:
                raise SimpleYamlError(f"empty list item in line {self.pos + 1}")
            self.pos += 1
            if _YAML_ENTRY_RE.match(body):
                items.append(self._item_mapping(body, item_indent))
            else:
                items.append(_yaml_scalar(body))
        return items

    def _item_mapping(self, first: str, item_indent: int) -> dict[str, str | None]:
        key, rest = _yaml_entry(first)
        item = {key: _yaml_flat(rest)}
        while (line := self._peek()) is not None and line[0] == item_indent:
            key, rest = _yaml_entry(line[1])
            if key in item:
                raise SimpleYamlError(f"duplicate key: {key}")
            self.pos += 1
            item[key] = _yaml_flat(rest)
        return item

    def _mapping(self, map_indent: int) -> dict[str, str | None]:
        mapping: dict[str, str | None] = {}
        while (line := self._peek()) is not None and line[0] >= map_indent:
            indent, content = line
            key, rest = _yaml_entry(content)
            if indent > map_indent or key in mapping or len(mapping) >= MAX_YAML_ITEMS:
                raise SimpleYamlError(f"unsupported mapping in line {self.pos + 1}")
            self.pos += 1
            mapping[key] = _yaml_flat(rest)
        return mapping

    def _block_scalar(self) -> str:
        """``key: |`` / ``key: >`` (release notes): the indented lines, roughly as written."""
        collected: list[str] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip() and not line.startswith(" "):
                break
            collected.append(line if line.strip() else "")
            self.pos += 1
        return textwrap.dedent("\n".join(collected)).strip("\n")


# ------------------------------------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpResponse:
    """Answer of a :data:`Fetcher`. HTTP error statuses are returned, not raised."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    #: the final URL (after redirects)
    url: str
    #: the body was cut off at ``max_bytes``
    truncated: bool = False
    #: the addresses the request was redirected to, in order (the last one is ``url``)
    redirects: tuple[str, ...] = ()

    def header(self, name: str) -> str | None:
        """Header value, looked up without regard to case."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return None


#: ``fetch(url, *, headers=None, max_bytes=...)``; the default is :func:`http_get`.
Fetcher = Callable[..., HttpResponse]

_ssl_context: ssl.SSLContext | None = None
_ssl_lock = threading.Lock()


def _default_ssl_context() -> ssl.SSLContext:
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context()
        return _ssl_context


class _SecureRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to addresses that :func:`is_secure_url` accepts."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_insecure_localhost: bool) -> None:
        self._allow_insecure_localhost = allow_insecure_localhost
        self.followed: list[str] = []

    def _refuse(self, req, fp, newurl):
        with contextlib.suppress(Exception):
            fp.close()
        raise _insecure(f"redirect from {req.full_url!r} to {newurl!r}")

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib itself answers "file:" & co. with a plain HTTP error; say what it is instead.
        location = headers.get("location") or headers.get("uri") or ""
        scheme = location.split(":", 1)[0].strip().lower() if _SCHEME_RE.match(location.strip()) else ""
        if scheme not in ("", "http", "https"):
            self._refuse(req, fp, location)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # newurl is absolute and percent-encoded here
        if not is_secure_url(newurl, allow_insecure_localhost=self._allow_insecure_localhost):
            self._refuse(req, fp, newurl)
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None:
            self.followed.append(newurl)
        return request


def _build_opener(url: str, allow_insecure_localhost: bool,
                  redirects: _SecureRedirectHandler | None = None) -> urllib.request.OpenerDirector:
    """Only http(s): no file:, ftp: or data: handlers; loopback addresses never use a proxy."""
    local = _url_host(url).lower() in LOCAL_HOSTS
    opener = urllib.request.OpenerDirector()
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({}) if local else urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=_default_ssl_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        redirects or _SecureRedirectHandler(allow_insecure_localhost),
        urllib.request.HTTPErrorProcessor(),
    ]
    if allow_insecure_localhost:
        handlers.append(urllib.request.HTTPHandler())
    for handler in handlers:
        opener.add_handler(handler)
    return opener


def _network_error(exc: BaseException, url: str) -> NetworkError:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    details = f"{url}: {type(reason).__name__}: {reason}"
    if isinstance(reason, TimeoutError):
        return NetworkError(_("The server did not answer in time. Please try again later."), details)
    if isinstance(reason, ssl.SSLError):
        return NetworkError(_("A secure connection to {host} could not be set up.").format(
            host=_url_host(url) or "?"), details)
    return NetworkError(
        _("The internet could not be reached. Please check your connection and try again."), details)


def _open_url(url: str, headers: Mapping[str, str] | None, timeout: float,
              allow_insecure_localhost: bool) -> tuple[Any, tuple[str, ...]]:
    """GET ``url``; returns the response object - also for HTTP error statuses - and the
    addresses it was redirected to."""
    _require_secure_url(url, allow_insecure_localhost)
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    redirects = _SecureRedirectHandler(allow_insecure_localhost)
    try:
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        opener = _build_opener(url, allow_insecure_localhost, redirects)
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except EasyInstallerError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise _network_error(exc, url) from exc
    return response, tuple(redirects.followed)


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    return int(status if status is not None else response.getcode())


def _response_headers(response: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in response.headers.items()}


def _read_limited(response: Any, limit: int, deadline: float) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read1", None) or response.read
    while total <= limit:
        if time.monotonic() > deadline:
            raise TimeoutError("the answer took too long")
        chunk = read(min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    body = b"".join(chunks)
    return body[:limit], len(body) > limit


def http_get(url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int = MAX_JSON_BYTES,
             timeout: float = METADATA_TIMEOUT, allow_insecure_localhost: bool = False) -> HttpResponse:
    """GET a small document. https only (see :func:`is_secure_url`), redirects only to https, at
    most ``max_bytes`` of the body (``truncated`` tells if there was more). HTTP statuses such as
    304/403/404 are returned; an unreachable server, a timeout or a broken connection raise
    NetworkError, an insecure address or redirect UpdateError."""
    deadline = time.monotonic() + max(METADATA_DEADLINE, timeout)
    response, redirects = _open_url(url, headers, timeout, allow_insecure_localhost)
    try:
        status = _response_status(response)
        limit = max_bytes if status < 400 else min(max_bytes, MAX_ERROR_BODY_BYTES)
        body, truncated = _read_limited(response, max(0, limit), deadline)
        return HttpResponse(status=status, headers=_response_headers(response), body=body,
                            url=response.geturl() or url, truncated=truncated, redirects=redirects)
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise _network_error(exc, url) from exc
    finally:
        with contextlib.suppress(Exception):
            response.close()


def _retry_after_seconds(response: HttpResponse) -> int | None:
    retry = (response.header("retry-after") or "").strip()
    if retry.isdigit():
        return int(retry)
    reset = (response.header("x-ratelimit-reset") or "").strip()
    if reset.isdigit():
        return max(0, int(reset) - int(time.time()))
    return None


def _is_rate_limited(response: HttpResponse) -> bool:
    if response.status == 429:
        return True
    if response.status != 403:
        return False
    remaining = (response.header("x-ratelimit-remaining") or "").strip()
    # GitHub's "secondary" limit may come without either header; its message says it.
    return remaining == "0" or response.header("retry-after") is not None \
        or b"rate limit" in response.body[:2000].lower()


def _rate_limit_error(response: HttpResponse, details: str) -> NetworkError:
    seconds = _retry_after_seconds(response)
    if seconds is None or seconds > 24 * 3600:
        return NetworkError(
            _("Too many update checks were made in a short time. Please try again later."), details)
    minutes = max(1, -(-seconds // 60))
    return NetworkError(ngettext(
        "Too many update checks were made in a short time. Please try again in about {minutes} minute.",
        "Too many update checks were made in a short time. Please try again in about {minutes} minutes.",
        minutes).format(minutes=minutes), details)


def _status_details(response: HttpResponse, url: str) -> str:
    text = response.body[:300].decode("utf-8", "replace").strip()
    return f"{url}: HTTP {response.status}" + (f": {text}" if text else "")


def _raise_for_status(response: HttpResponse, url: str, ok: tuple[int, ...] = (200,)) -> None:
    status = response.status
    if status in ok:
        return
    details = _status_details(response, url)
    if _is_rate_limited(response):
        raise _rate_limit_error(response, details)
    if status in (404, 410):
        raise _no_update_info(details)
    if status >= 500 or status == 408:
        raise NetworkError(
            _("The update server has a problem right now. Please try again later."), details)
    if status in (401, 403, 451):
        raise UpdateError(
            _("The update server did not allow Easy Installer to look for updates."), details)
    raise UpdateError(_("The update server gave an answer that Easy Installer does not understand."),
                      details)


# ------------------------------------------------------------------------------------------------
# conditional requests (ETag)
# ------------------------------------------------------------------------------------------------

def _cached_entry(etags: dict | None, url: str) -> dict | None:
    entry = etags.get(url) if isinstance(etags, dict) else None
    if not isinstance(entry, dict):
        return None
    etag, body = entry.get("etag"), entry.get("body")
    if not _valid_etag(etag) or not isinstance(body, str):
        return None
    return entry


def _valid_etag(etag: Any) -> bool:
    return (isinstance(etag, str) and 0 < len(etag) <= MAX_ETAG_LENGTH and etag.isascii()
            and etag.isprintable())


def _remember(etags: dict | None, url: str, response: HttpResponse, text: str) -> None:
    if not isinstance(etags, dict):
        return
    etag = (response.header("etag") or "").strip()
    if not _valid_etag(etag) or len(text) > MAX_CACHED_BODY:
        etags.pop(url, None)
        return
    etags[url] = {"etag": etag, "body": text, "saved_at": utc_timestamp()}
    if len(etags) > MAX_ETAGS:
        oldest = sorted(etags, key=lambda key: str((etags[key] or {}).get("saved_at", ""))
                        if isinstance(etags[key], dict) else "")
        for key in oldest[: len(etags) - MAX_ETAGS]:
            etags.pop(key, None)


def _as_response(answer: Any, url: str) -> HttpResponse:
    """What a fetcher returned as a well-formed HttpResponse (fetchers of tests may be sloppy)."""
    try:
        body = answer.body or b""
        return HttpResponse(
            status=int(answer.status), headers=dict(answer.headers or {}),
            body=body.encode("utf-8") if isinstance(body, str) else bytes(body),
            url=str(getattr(answer, "url", "") or ""), truncated=bool(getattr(answer, "truncated", False)),
            redirects=tuple(r for r in (getattr(answer, "redirects", None) or ()) if isinstance(r, str)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _unreadable(f"{url}: unusable answer: {type(exc).__name__}: {exc}") from exc


def _fetch_document(url: str, *, fetch: Fetcher, etags: dict | None, max_bytes: int,
                    reduce: Callable[[bytes], str], load: Callable[[str], T],
                    headers: Mapping[str, str] | None = None,
                    partial: bool = False) -> tuple[T, HttpResponse]:
    """GET ``url`` and return ``load(reduce(body))`` plus the answer it came from. ``reduce`` turns
    the body into the (small) text that is remembered next to the ETag; after "304 Not Modified"
    that text is loaded again. ``partial``: only the first ``max_bytes`` are wanted (a Range
    request may be answered by 206 or by the whole file)."""
    cached = _cached_entry(etags, url)
    request_headers = dict(headers or {})
    if cached is not None:
        request_headers["If-None-Match"] = cached["etag"]
    response = _as_response(fetch(url, headers=request_headers, max_bytes=max_bytes), url)
    if response.url and not is_secure_url(response.url):
        raise _insecure(f"{url!r} was answered by {response.url!r}")
    if response.status == 304 and cached is not None:
        try:
            return load(cached["body"]), response
        except (EasyInstallerError, ValueError) as exc:
            log.warning("cached answer for %s is unusable (%s); asking again", url, exc)
            etags.pop(url, None)   # type: ignore[union-attr]
            return _fetch_document(url, fetch=fetch, etags=etags, max_bytes=max_bytes,
                                   reduce=reduce, load=load, headers=headers, partial=partial)
    _raise_for_status(response, url, ok=(200, 206) if partial else (200,))
    body = response.body
    if response.truncated or len(body) > max_bytes:
        if not partial:
            raise _unreadable(f"{url}: answer larger than {max_bytes} bytes")
        body = body[:max_bytes]
    text = reduce(body)
    try:
        value = load(text)
    except ValueError as exc:
        raise _unreadable(f"{url}: {exc}") from exc
    _remember(etags, url, response, text)
    return value, response


def _decode(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unreadable(f"not UTF-8: {exc}") from exc


# ------------------------------------------------------------------------------------------------
# available updates
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AvailableUpdate:
    """A newer file that can be downloaded."""

    version: str | None
    url: str
    filename: str
    size: int | None = None
    #: lower-case hex, when the source publishes them
    sha256: str | None = None
    sha512: str | None = None
    release_url: str | None = None
    published_at: str | None = None
    #: lower-case hex; the only checksum a .zsync file has
    sha1: str | None = None

    def to_dict(self) -> dict:
        return {"version": self.version, "url": self.url, "filename": self.filename,
                "size": self.size, "sha256": self.sha256, "sha512": self.sha512,
                "release_url": self.release_url, "published_at": self.published_at,
                "sha1": self.sha1}

    @classmethod
    def from_dict(cls, d: Any) -> AvailableUpdate | None:
        """Tolerant of missing optional keys; None if the address or file name is unusable."""
        if not isinstance(d, Mapping):
            return None
        url, filename = d.get("url"), _clean_text(d.get("filename"), 255)
        if not is_secure_url(url, allow_insecure_localhost=True) or filename is None:
            return None
        if "/" in filename or "\\" in filename:
            return None
        size = d.get("size")
        release_url = d.get("release_url")
        return cls(
            version=normalize_version(_clean_text(d.get("version"), 64)),
            url=url, filename=filename,
            size=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            sha256=normalize_hex_digest(d.get("sha256"), 32),
            sha512=normalize_hex_digest(d.get("sha512"), 64),
            release_url=release_url if is_secure_url(release_url) else None,
            published_at=_clean_text(d.get("published_at"), 64),
            sha1=normalize_hex_digest(d.get("sha1"), 20),
        )


@dataclass(frozen=True)
class _Installed:
    """What is known about the installed file."""

    version: str | None
    sha256: str | None
    filename: str | None
    arch: str | None
    size: int | None
    sha1: str | None


def _is_newer(update: AvailableUpdate, installed: _Installed) -> bool:
    if digests_equal(update.sha256, installed.sha256):
        return False   # the very same file, whatever the version texts say
    if update.version and installed.version:
        return compare_versions(update.version, installed.version) > 0
    if update.sha256 and installed.sha256:
        return True
    if not installed.filename:
        return False   # nothing to compare with: do not offer the same file again and again
    return _comparable_name(update.filename) != _comparable_name(installed.filename)


def _zsync_is_newer(update: AvailableUpdate, installed: _Installed) -> bool:
    if update.version and installed.version:
        return compare_versions(update.version, installed.version) > 0
    if update.sha1 and installed.sha1:
        return not digests_equal(update.sha1, installed.sha1)
    if update.size is not None and installed.size:
        return update.size != installed.size
    if not installed.filename:
        return False
    return _comparable_name(update.filename) != _comparable_name(installed.filename)


def _url_filename(url: str) -> str:
    return posixpath.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path))


def _join_file_url(base_url: str, reference: str) -> str:
    """The https URL of a file named in update information, relative to that document."""
    if _has_control(reference) or "\\" in reference:
        raise _insecure(f"refused file address: {reference!r}")
    if _SCHEME_RE.match(reference):
        url = reference
    else:
        url = urllib.parse.urljoin(base_url, urllib.parse.quote(reference, safe="/%+@~!$&'()*,;=:?#[]-._"))
    return _require_secure_url(url)


# --- GitHub releases -----------------------------------------------------------------------------

def _slim_asset(asset: Any) -> dict | None:
    if not isinstance(asset, dict) or asset.get("state") not in (None, "uploaded"):
        return None
    name, url = asset.get("name"), asset.get("browser_download_url")
    if not isinstance(name, str) or not isinstance(url, str):
        return None
    if not (_is_appimage_name(name) or name.lower().endswith(".yml")):
        return None
    size, digest = asset.get("size"), asset.get("digest")
    return {"name": name, "browser_download_url": url,
            "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            "digest": digest if isinstance(digest, str) else None}


def _slim_release(release: Any) -> dict | None:
    """Only what update checks need (release notes and other assets are dropped)."""
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        return None
    assets = [slim for slim in map(_slim_asset, release["assets"][:MAX_ASSETS]) if slim is not None]

    def text(key: str, limit: int) -> str | None:
        return _clean_text(release.get(key), limit)

    return {"tag_name": text("tag_name", 200), "prerelease": release.get("prerelease") is True,
            "draft": release.get("draft") is True, "html_url": text("html_url", MAX_URL_LENGTH),
            "published_at": text("published_at", 64), "assets": assets}


def _reduce_github(body: bytes) -> str:
    try:
        data = json.loads(_decode(body))
    except (ValueError, RecursionError) as exc:
        raise _unreadable(f"not JSON: {exc}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise _unreadable("unexpected JSON document")
    releases = [_slim_release(release) for release in data[:RELEASES_PER_PAGE * 5]]
    if any(release is None for release in releases):
        raise _unreadable("a release without assets list")
    return json.dumps(releases, separators=(",", ":"))


def _load_releases(text: str) -> list[dict]:
    data = json.loads(text)
    releases = [_slim_release(release) for release in data] if isinstance(data, list) else [None]
    if any(release is None for release in releases):
        raise ValueError("not a list of releases")
    return [release for release in releases if release is not None and not release["draft"]]


def _github_releases(url: str, fetch: Fetcher, etags: dict | None) -> list[dict]:
    return _fetch_document(url, fetch=fetch, etags=etags, max_bytes=MAX_JSON_BYTES,
                           reduce=_reduce_github, load=_load_releases, headers=GITHUB_API_HEADERS)[0]


def _github_api_url(source: UpdateSource, release: str) -> str:
    base = f"{GITHUB_API}/repos/{source.owner}/{source.repo}/releases"
    if release == RELEASE_LATEST:
        return f"{base}/latest"
    if release in (RELEASE_LATEST_PRE, RELEASE_LATEST_ALL):
        return f"{base}?per_page={RELEASES_PER_PAGE}"
    return f"{base}/tags/{urllib.parse.quote(release, safe='')}"


def _asset_sha256(asset: Mapping[str, Any]) -> str | None:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        return normalize_hex_digest(digest[len("sha256:"):], 32)
    return None


def _version_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    version = parse_version_from_filename(tag)
    if version:
        return normalize_version(version)
    version = normalize_version(tag)
    if version and version[0].isdigit() and len(version) <= 64 and not re.search(r"\s", version):
        return version
    return None


def _pick_asset(release: Mapping[str, Any], pattern: str, installed: _Installed) -> dict | None:
    assets = [asset for asset in release["assets"]
              if _is_appimage_name(asset["name"]) and fnmatch.fnmatchcase(asset["name"], pattern)
              and _arch_fits(asset["name"], installed.arch)
              and is_secure_url(asset["browser_download_url"])
              and "/" not in asset["name"] and not _has_control(asset["name"])]
    index = _closest([asset["name"] for asset in assets], installed.filename, installed.arch)
    return assets[index] if index is not None else None


def _releases_in_order(releases: list[dict], release: str) -> list[dict]:
    """"latest-pre": pre-releases first (newest first), then the others; else as listed (for
    equal versions the first one wins)."""
    if release != RELEASE_LATEST_PRE:
        return releases
    return [r for r in releases if r["prerelease"]] + [r for r in releases if not r["prerelease"]]


def _check_github_assets(source: UpdateSource, installed: _Installed, fetch: Fetcher,
                         etags: dict | None) -> AvailableUpdate | None:
    release_name = source.release or RELEASE_LATEST
    url = _github_api_url(source, release_name)
    releases = _github_releases(url, fetch, etags)
    if not releases:
        raise _no_update_info(f"{url}: no releases")
    found: list[AvailableUpdate] = []
    for release in _releases_in_order(releases, release_name):
        asset = _pick_asset(release, source.pattern or "*", installed)
        if asset is None:
            continue
        name = asset["name"]
        version = normalize_version(parse_version_from_filename(name)) or _version_from_tag(release["tag_name"])
        found.append(AvailableUpdate(
            version=version, url=asset["browser_download_url"], filename=name, size=asset["size"],
            sha256=_asset_sha256(asset), sha512=None,
            release_url=release["html_url"] if is_secure_url(release["html_url"]) else None,
            published_at=release["published_at"]))
        if release_name not in (RELEASE_LATEST_PRE, RELEASE_LATEST_ALL):
            break
    if not found:
        log.info("%s: no asset matches %r for %s", source.describe(), source.pattern,
                 installed.arch)
        return None
    if not all(update.version for update in found):
        return found[0]        # versions unknown: the order of the list decides
    # Among several releases the newest version wins: an older pre-release (or a backport
    # published last) must not hide a newer release. Equal versions: the first one listed.
    best = found[0]
    for update in found[1:]:
        if compare_versions(update.version, best.version) > 0:   # type: ignore[arg-type]
            best = update
    return best


# --- electron-builder (latest-linux.yml) ---------------------------------------------------------

@dataclass(frozen=True)
class _YmlFile:
    url: str
    sha512: str | None
    sha256: str | None
    size: int | None


@dataclass(frozen=True)
class _LatestYml:
    version: str
    files: tuple[_YmlFile, ...]
    release_date: str | None


def _yml_file(entry: Mapping[str, Any]) -> _YmlFile | None:
    """One AppImage of ``files`` (other files - .deb, .rpm, ... - are skipped unchecked); raises
    ValueError for a checksum or size that is not what it should be."""
    url = entry.get("url") or entry.get("path")
    if not isinstance(url, str) or not _is_appimage_name(url.strip().split("?", 1)[0]):
        return None
    sha512 = entry.get("sha512")
    if sha512 is not None and base64_to_hex(sha512, 64) is None:
        raise ValueError(f"sha512 of {url!r} is not a base64 SHA-512")
    sha256 = entry.get("sha2")
    if sha256 is not None and normalize_hex_digest(sha256, 32) is None:
        raise ValueError(f"sha2 of {url!r} is not a hex SHA-256")
    size = entry.get("size")
    if size is not None and not (isinstance(size, str) and size.isascii() and size.isdigit()
                                 and len(size) <= 15):
        raise ValueError(f"size of {url!r} is not a number")
    return _YmlFile(url=url.strip(), sha512=base64_to_hex(sha512, 64),
                    sha256=normalize_hex_digest(sha256, 32), size=int(size) if size is not None else None)


def _parse_latest_yml(text: str) -> _LatestYml:
    """``latest-linux.yml``; raises ValueError if it is not what electron-builder writes."""
    data = parse_simple_yaml(text)
    version = normalize_version(_clean_text(data.get("version"), 64))
    if version is None or re.search(r"\s", version):
        raise ValueError("no usable version")
    entries = data.get("files")
    if entries is None:
        entries = [data]   # old format: only top-level path + sha512
    if not isinstance(entries, list):
        raise ValueError("files is not a list")
    files = [file for file in (_yml_file(entry) for entry in entries if isinstance(entry, Mapping))
             if file is not None]
    return _LatestYml(version=version, files=tuple(files),
                      release_date=_clean_text(data.get("releaseDate"), 64))


def _pick_yml_file(info: _LatestYml, installed: _Installed) -> _YmlFile | None:
    files = [file for file in info.files if _arch_fits(file.url, installed.arch)]
    index = _closest([file.url for file in files], installed.filename, installed.arch)
    return files[index] if index is not None else None


def _latest_yml(url: str, fetch: Fetcher, etags: dict | None) -> tuple[_LatestYml, HttpResponse]:
    return _fetch_document(url, fetch=fetch, etags=etags, max_bytes=MAX_YML_BYTES,
                           reduce=_decode, load=_parse_latest_yml)


def _pinned_release(source: UpdateSource, yml_name: str, response: HttpResponse) -> tuple[str, str] | None:
    """GitHub answers ``releases/latest/download/<file>`` with a redirect to
    ``releases/download/<tag>/<file>``. Returns that address and the release page, so the update
    keeps pointing at this very release when a newer one comes out before it is downloaded."""
    prefix = f"{GITHUB_WEB}/{source.owner}/{source.repo}/releases/download/".lower()
    for url in response.redirects:
        tag = url[len(prefix): -len(yml_name) - 1]
        if (url.lower().startswith(prefix) and url.endswith(f"/{yml_name}") and "/" not in tag
                and tag not in ("", ".", "..") and "?" not in url and "#" not in url and is_secure_url(url)):
            return url, f"{GITHUB_WEB}/{source.owner}/{source.repo}/releases/tag/{tag}"
    return None


def _electron_prerelease(source: UpdateSource, yml_name: str, fetch: Fetcher,
                         etags: dict | None) -> tuple[dict, str]:
    """The newest published release (pre-releases included) that carries ``yml_name``."""
    url = f"{GITHUB_API}/repos/{source.owner}/{source.repo}/releases?per_page={RELEASES_PER_PAGE}"
    for release in _github_releases(url, fetch, etags):
        for asset in release["assets"]:
            if asset["name"] == yml_name and is_secure_url(asset["browser_download_url"]):
                return release, asset["browser_download_url"]
    raise _no_update_info(f"{url}: no release with {yml_name}")


def _check_electron(source: UpdateSource, installed: _Installed, fetch: Fetcher,
                    etags: dict | None) -> AvailableUpdate | None:
    yml_name = f"latest-linux{_ELECTRON_ARCH_SUFFIX.get(installed.arch or 'x86_64', '')}.yml"
    release: dict | None = None
    release_url: str | None = None
    if source.kind == KIND_ELECTRON_GENERIC:
        yml_url = urllib.parse.urljoin((source.url or "").rstrip("/") + "/", yml_name)
    elif source.prerelease:
        release, yml_url = _electron_prerelease(source, yml_name, fetch, etags)
        release_url = release["html_url"] if is_secure_url(release["html_url"]) else None
    else:
        yml_url = f"{GITHUB_WEB}/{source.owner}/{source.repo}/releases/latest/download/{yml_name}"
        release_url = f"{GITHUB_WEB}/{source.owner}/{source.repo}/releases/latest"
    info, response = _latest_yml(yml_url, fetch, etags)
    if source.kind == KIND_ELECTRON_GITHUB and release is None:
        yml_url, release_url = _pinned_release(source, yml_name, response) or (yml_url, release_url)
    chosen = _pick_yml_file(info, installed)
    if chosen is None:
        log.info("%s: %s lists no AppImage for %s", source.describe(), yml_name, installed.arch)
        return None
    url = _join_file_url(yml_url, chosen.url)
    filename = _url_filename(url)
    if not _is_appimage_name(filename) or _has_control(filename):
        raise _unreadable(f"{yml_url}: unusable file name {filename!r}")
    sha256 = chosen.sha256
    if release is not None:
        twin = next((asset for asset in release["assets"] if asset["name"] == filename), None)
        sha256 = sha256 or (_asset_sha256(twin) if twin else None)
    return AvailableUpdate(
        version=info.version, url=url, filename=filename, size=chosen.size, sha256=sha256,
        sha512=chosen.sha512, release_url=release_url,
        published_at=info.release_date or (release["published_at"] if release else None))


# --- plain zsync files ---------------------------------------------------------------------------

def _reduce_zsync(body: bytes) -> str:
    """The text header of a .zsync file (everything before the first empty line)."""
    head, separator, _rest = body.replace(b"\r\n", b"\n").partition(b"\n\n")
    if not separator or not head.startswith(b"zsync:"):
        raise _unreadable("not a .zsync file")
    return _decode(head)


def _parse_zsync_header(text: str) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in text.split("\n"):
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise ValueError(f"not a header line: {line[:60]!r}")
        header.setdefault(key.strip().lower(), value.strip())   # several "URL" lines: the first
    if "url" not in header and "filename" not in header:
        raise ValueError("neither URL nor Filename")
    return header


def _zsync_published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return utc_timestamp(when)


def _check_zsync(source: UpdateSource, installed: _Installed, fetch: Fetcher,
                 etags: dict | None) -> AvailableUpdate | None:
    zsync_url = source.url or ""
    header, _response = _fetch_document(
        zsync_url, fetch=fetch, etags=etags, max_bytes=MAX_ZSYNC_HEADER_BYTES, reduce=_reduce_zsync,
        load=_parse_zsync_header, headers={"Range": f"bytes=0-{MAX_ZSYNC_HEADER_BYTES - 1}"},
        partial=True)
    url = _join_file_url(zsync_url, header.get("url") or header.get("filename") or "")
    names = [name for name in (header.get("filename"), _url_filename(url))
             if name and _is_appimage_name(name) and "/" not in name and not _has_control(name)]
    if not names:
        raise _unreadable(f"{zsync_url}: the file is not an AppImage")
    if not _arch_fits(names[0], installed.arch):
        log.info("%s: %s is for another CPU", zsync_url, names[0])
        return None
    length = header.get("length", "")
    return AvailableUpdate(
        version=normalize_version(parse_version_from_filename(names[0])), url=url, filename=names[0],
        size=int(length) if length.isascii() and length.isdigit() and len(length) <= 15 else None,
        sha256=None, sha512=None, release_url=None,
        published_at=_zsync_published_at(header.get("mtime")),
        sha1=normalize_hex_digest(header.get("sha-1"), 20))


# --- the check -----------------------------------------------------------------------------------

def check_for_update(source: UpdateSource, *, current_version: str | None,
                     current_sha256: str | None, current_filename: str | None, arch: str,
                     fetch: Fetcher | None = None, etags: dict | None = None,
                     current_size: int | None = None,
                     current_sha1: str | None = None) -> AvailableUpdate | None:
    """Ask ``source`` for a newer version. None = up to date (or nothing suitable for this CPU).

    Newer means ``compare_versions(remote, current) > 0``; if a version is unknown, the published
    sha256 must differ from ``current_sha256``, else the file name from ``current_filename``. A
    file whose published sha256 equals ``current_sha256`` is never offered.
    A .zsync source has neither version nor sha256: there the SHA-1 (``current_sha1``) or the
    length (``current_size``) of the installed file decides, else the file name.
    ``arch`` is the CPU the app was built for ("x86_64", "aarch64", ...); files that name another
    one are skipped. ``etags`` (``UpdateCache.etags``) is updated in place: the next check sends
    ``If-None-Match`` and reuses the remembered answer after "304 Not Modified".
    Raises NetworkError (no connection, server trouble, rate limit) or UpdateError (no or
    unreadable update information, an address that is not https)."""
    if not isinstance(source, UpdateSource) or not source.is_valid():
        raise _unreadable(f"invalid update source: {source!r}")
    fetcher = fetch or http_get
    installed = _Installed(
        version=normalize_version(_clean_text(current_version, 64)),
        sha256=normalize_hex_digest(current_sha256, 32),
        filename=_clean_text(current_filename, 255),
        arch=_canonical_arch(arch),
        size=current_size if isinstance(current_size, int) and current_size > 0 else None,
        sha1=normalize_hex_digest(current_sha1, 20))
    if source.kind == KIND_ZSYNC_URL:
        update = _check_zsync(source, installed, fetcher, etags)
        return update if update is not None and _zsync_is_newer(update, installed) else None
    if source.kind == KIND_GITHUB_ASSETS:
        update = _check_github_assets(source, installed, fetcher, etags)
    else:
        update = _check_electron(source, installed, fetcher, etags)
    return update if update is not None and _is_newer(update, installed) else None


# ------------------------------------------------------------------------------------------------
# download
# ------------------------------------------------------------------------------------------------

def _cancelled() -> UpdateCancelled:
    return UpdateCancelled(_("The download was cancelled."))


def _interrupted(details: str) -> NetworkError:
    return NetworkError(_("The download was interrupted. Please check your connection and try again."),
                        details)


def _damaged(details: str) -> UpdateError:
    return UpdateError(
        _("The downloaded file is incomplete or was changed on the way, so it was not installed. "
          "Please try again later."), details)


def _save_error(exc: OSError, path: Path) -> UpdateError:
    details = f"{path}: {exc}"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return UpdateError(_("There is not enough free space to download the update."), details)
    return UpdateError(_("The update could not be saved on this computer."), details)


def _raise_for_download_status(status: int, headers: Mapping[str, str], url: str) -> None:
    if status == 200:
        return
    response = HttpResponse(status=status, headers=headers, body=b"", url=url)
    details = f"{url}: HTTP {status}"
    if _is_rate_limited(response):
        raise NetworkError(
            _("The server is busy and refused the download. Please try again later."), details)
    if status in (404, 410):
        raise UpdateError(_("The update is no longer on the server. Please check for updates again."),
                          details)
    if status >= 500 or status == 408:
        raise NetworkError(
            _("The update server has a problem right now. Please try again later."), details)
    raise UpdateError(_("The update could not be downloaded because the server refused it."), details)


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = (headers.get("content-length") or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 15 else None


def _expected_size(update: AvailableUpdate, headers: Mapping[str, str]) -> int | None:
    """Size to expect; raises if the server announces another one than the update information."""
    announced = _content_length(headers)
    if update.size is not None and announced is not None and announced != update.size:
        raise _damaged(f"size {announced} announced, {update.size} expected")
    total = update.size if update.size is not None else announced
    if total is not None and total > MAX_DOWNLOAD_BYTES:
        raise UpdateError(_("The update is too large to be downloaded."), f"{total} bytes")
    return total


def _check_free_space(directory: Path, total: int | None) -> None:
    if total is None:
        return
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return
    if free < total:
        raise UpdateError(_("There is not enough free space to download the update."),
                          f"{directory}: {free} bytes free, {total} needed")


#: How often a download that waits for its connection looks whether it was cancelled.
CANCEL_POLL_INTERVAL = 0.1


def _open_cancellable(url: str, headers: Mapping[str, str] | None, timeout: float,
                      allow_insecure_localhost: bool,
                      cancel: threading.Event | None) -> tuple[Any, tuple[str, ...]]:
    """:func:`_open_url`, but a ``cancel`` stops the waiting at once: looking up the server
    and connecting can take long (the name lookup has no time limit at all). The connection
    is made in a helper thread; one that is no longer wanted is closed when it arrives."""
    if cancel is None:
        return _open_url(url, headers, timeout, allow_insecure_localhost)
    lock = threading.Lock()
    done = threading.Event()
    state: dict[str, Any] = {"abandoned": False}

    def connect() -> None:
        try:
            outcome: tuple[Any, BaseException | None] = (
                _open_url(url, headers, timeout, allow_insecure_localhost), None)
        except BaseException as exc:  # noqa: BLE001 - handed over to the waiting thread
            outcome = (None, exc)
        with lock:
            state["outcome"] = outcome
            abandoned = state["abandoned"]
        done.set()
        if abandoned and outcome[0] is not None:
            with contextlib.suppress(Exception):
                outcome[0][0].close()

    threading.Thread(target=connect, name="easy-installer-connect", daemon=True).start()
    while not done.wait(CANCEL_POLL_INTERVAL):
        if cancel.is_set():
            with lock:
                if "outcome" not in state:
                    state["abandoned"] = True
                    raise _cancelled()
    result, error = state["outcome"]
    if error is not None:
        raise error
    return result


def _response_socket(response: Any) -> socket.socket | None:
    """The connection under an ``http.client`` answer (None if it cannot be found)."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


@contextlib.contextmanager
def _closed_on_cancel(response: Any, cancel: threading.Event | None) -> Iterator[None]:
    """While the body is read: a ``cancel`` shuts the connection down, so that a read that
    waits for a stalled server returns at once instead of after ``DOWNLOAD_TIMEOUT``."""
    sock = _response_socket(response) if cancel is not None else None
    if sock is None:
        yield
        return
    finished = threading.Event()

    def watch() -> None:
        while not finished.is_set():
            if cancel.wait(CANCEL_POLL_INTERVAL):
                if not finished.is_set():
                    with contextlib.suppress(OSError):
                        # (the plain socket's shutdown: the TLS layer is left to the reader)
                        socket.socket.shutdown(sock, socket.SHUT_RDWR)
                return

    watcher = threading.Thread(target=watch, name="easy-installer-cancel", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join()


def _read_chunk(response: Any, url: str) -> bytes:
    try:
        reader = getattr(response, "read1", None) or response.read
        return reader(CHUNK_SIZE)
    except (http.client.HTTPException, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise _network_error(exc, url) from exc
        raise _interrupted(f"{url}: {type(exc).__name__}: {exc}") from exc


def _write_chunk(handle: Any, chunk: bytes, path: Path) -> None:
    try:
        handle.write(chunk)
    except OSError as exc:
        raise _save_error(exc, path) from exc


def _progress_text(done: int, total: int | None) -> str:
    if total:
        return _("Downloading… {done} of {total}").format(done=_format_size(done), total=_format_size(total))
    return _("Downloading… {done}").format(done=_format_size(done))


def _open_part(part: Path) -> Any:
    try:
        with contextlib.suppress(FileNotFoundError):
            part.unlink()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                     CACHE_FILE_MODE)
        return os.fdopen(fd, "wb")
    except OSError as exc:
        raise _save_error(exc, part) from exc


def _wanted_digests(update: AvailableUpdate) -> dict[str, str]:
    wanted = {"sha1": update.sha1, "sha256": update.sha256, "sha512": update.sha512}
    return {name: digest for name, digest in wanted.items() if digest}


def _stream_to_file(response: Any, part: Path, update: AvailableUpdate, total: int | None,
                    report: ProgressCallback, cancel: threading.Event | None) -> tuple[int, dict[str, str]]:
    """Write the body to ``part``; returns the number of bytes and the digests of what was written
    (only the kinds the update publishes)."""
    hashers = {name: hashlib.new(name) for name in _wanted_digests(update)}
    limit = total if total is not None else MAX_DOWNLOAD_BYTES
    received = 0
    last_report = 0.0
    cancelled = (lambda: False) if cancel is None else cancel.is_set
    with _open_part(part) as handle, _closed_on_cancel(response, cancel):
        while True:
            if cancelled():
                raise _cancelled()
            try:
                chunk = _read_chunk(response, update.url)
            except EasyInstallerError as exc:
                if cancelled():   # the connection was shut down because of the cancel
                    raise _cancelled() from exc
                raise
            if cancelled():
                raise _cancelled()
            if not chunk:
                break
            received += len(chunk)
            if received > limit:
                raise _damaged(f"more than the expected {limit} bytes")
            _write_chunk(handle, chunk, part)
            for hasher in hashers.values():
                hasher.update(chunk)
            if time.monotonic() - last_report >= PROGRESS_INTERVAL:
                last_report = time.monotonic()
                report(min(received / total, 1.0) if total else None, _progress_text(received, total))
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            raise _save_error(exc, part) from exc
    return received, {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _check_received(received: int, digests: Mapping[str, str], update: AvailableUpdate,
                    headers: Mapping[str, str]) -> None:
    """Size and every published checksum must be right."""
    announced = _content_length(headers)
    if announced is not None and received < announced:
        raise _interrupted(f"{update.url}: {received} of {announced} bytes received")
    if update.size is not None and received != update.size:
        raise _damaged(f"{received} bytes received, {update.size} expected")
    if received == 0:
        raise _damaged(f"{update.url}: empty answer")
    for name, expected in _wanted_digests(update).items():
        if not digests_equal(digests.get(name), expected):
            raise _damaged(f"{name} is {digests.get(name)}, expected {expected}")


def download_update(update: AvailableUpdate, dest_dir: Path, *, progress: ProgressCallback | None = None,
                    cancel: threading.Event | None = None,
                    allow_insecure_localhost: bool = False) -> Path:
    """Download ``update`` into ``dest_dir`` and return the file.

    Streams to ``<name>.part`` (``progress(fraction | None, text)``), checks the size and every
    checksum the update publishes (sha256, sha512, sha1), then renames to the final name. The name
    is sanitised and must end in ".AppImage". On any failure the partial file is removed.
    ``cancel`` set -> UpdateCancelled, also while connecting or waiting for a stalled server
    (it is not waited for). Raises NetworkError / UpdateError otherwise."""
    report = progress or _noop_progress
    name = safe_download_name(update.filename)
    _require_secure_url(update.url, allow_insecure_localhost)
    dest_dir = Path(dest_dir)
    if cancel is not None and cancel.is_set():
        raise _cancelled()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True, mode=CACHE_DIR_MODE)
    except OSError as exc:
        raise _save_error(exc, dest_dir) from exc
    final, part = dest_dir / name, dest_dir / f"{name}.part"
    report(None, _("Starting the download…"))
    response, _redirects = _open_cancellable(update.url, {"Accept": "application/octet-stream"},
                                             DOWNLOAD_TIMEOUT, allow_insecure_localhost, cancel)
    try:
        headers = _response_headers(response)
        _raise_for_download_status(_response_status(response), headers, update.url)
        total = _expected_size(update, headers)
        _check_free_space(dest_dir, total)
        try:
            received, digests = _stream_to_file(response, part, update, total, report, cancel)
            _check_received(received, digests, update, headers)
            try:
                os.replace(part, final)
            except OSError as exc:
                raise _save_error(exc, final) from exc
        except BaseException:
            with contextlib.suppress(OSError):
                part.unlink()
            raise
    finally:
        with contextlib.suppress(Exception):
            response.close()
    report(1.0, _progress_text(received, received))
    return final


# ------------------------------------------------------------------------------------------------
# cache of the last check results
# ------------------------------------------------------------------------------------------------

class UpdateCache:
    """``$XDG_CACHE_HOME/easy-installer/updates.json``: per app (scope + id) when it was last
    checked and which update was found (None = up to date; ``"failed": true`` when that check
    could not be answered), plus the ETags of the documents that were read (``etags``, handed
    to :func:`check_for_update`).

    A cache never gets in the way: a missing or damaged file is an empty cache, and a file that
    cannot be written is only logged. Writes are atomic."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else cache_dir() / CACHE_FILE_NAME
        self._lock = threading.RLock()
        self._apps: dict[str, dict] = {}
        self._stamp: tuple[int, int, int] | None = None
        self.etags: dict = {}
        self._sync(force=True)

    @staticmethod
    def _key(scope: Any, app_id: str) -> str:
        return f"{getattr(scope, 'value', scope)}/{app_id}"

    def get(self, scope: Any, app_id: str) -> AvailableUpdate | None:
        """The update found by the last check, or None (up to date, or never checked)."""
        with self._lock:
            self._sync()
            entry = self._apps.get(self._key(scope, app_id))
            return AvailableUpdate.from_dict(entry.get("update")) if entry else None

    def put(self, scope: Any, app_id: str, update: AvailableUpdate | None) -> None:
        """Record a finished check; ``None`` means "checked, up to date"."""
        with self._lock:
            self._sync()
            self._apps[self._key(scope, app_id)] = {
                "checked_at": utc_timestamp(),
                "update": update.to_dict() if update is not None else None,
            }
            self._write()

    def put_failed(self, scope: Any, app_id: str) -> None:
        """Record a check that could not be answered (the source moved, or publishes nothing
        that can be read): it counts as done for the interval, but an update found before
        stays known, and :meth:`failed` tells that the last check failed."""
        with self._lock:
            self._sync()
            key = self._key(scope, app_id)
            before = self._apps.get(key) or {}
            known = before.get("update")
            self._apps[key] = {
                "checked_at": utc_timestamp(),
                "update": known if isinstance(known, dict) else None,
                "failed": True,
            }
            self._write()

    def failed(self, scope: Any, app_id: str) -> bool:
        """The last check of the app could not be answered (see :meth:`put_failed`)."""
        with self._lock:
            self._sync()
            entry = self._apps.get(self._key(scope, app_id))
            return bool(entry and entry.get("failed") is True)

    def checked_at(self, scope: Any, app_id: str) -> str | None:
        with self._lock:
            self._sync()
            entry = self._apps.get(self._key(scope, app_id))
            value = entry.get("checked_at") if entry else None
            return value if isinstance(value, str) else None

    def is_due(self, scope: Any, app_id: str, interval_hours: int) -> bool:
        """True if the app was never checked, or the last check is ``interval_hours`` or more ago
        (or lies in the future: the clock was changed)."""
        checked = _parse_timestamp(self.checked_at(scope, app_id))
        if checked is None or interval_hours <= 0:
            return True
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return age < -300 or age >= interval_hours * 3600

    def forget(self, scope: Any, app_id: str) -> None:
        """Drop what is known about an app (after it was updated or uninstalled)."""
        with self._lock:
            self._sync()
            if self._apps.pop(self._key(scope, app_id), None) is not None:
                self._write()

    def save(self) -> None:
        """Write the cache now (e.g. after checks that only changed ``etags``)."""
        with self._lock:
            self._sync()
            self._write()

    # -- file handling ----------------------------------------------------------------------------

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _sync(self, force: bool = False) -> None:
        """Pick up what another process (CLI next to the GUI) wrote in the meantime."""
        stamp = self._file_stamp()
        if stamp is None or (stamp == self._stamp and not force):
            return
        apps, etags = _read_cache_file(self.path)
        self._apps = apps
        for url, entry in etags.items():
            self.etags.setdefault(url, entry)
        self._stamp = stamp

    def _write(self) -> None:
        data = {"format": CACHE_FORMAT, "apps": self._apps,
                "etags": {url: entry for url, entry in self.etags.items()
                          if isinstance(url, str) and isinstance(entry, dict)}}
        try:
            _atomic_write_json(self.path, data)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not write the update cache %s: %s", self.path, exc)
            return
        self._stamp = self._file_stamp()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_cache_file(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(64 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError, RecursionError) as exc:
        log.warning("update cache %s is unreadable (%s); starting with an empty one", path, exc)
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    apps, etags = data.get("apps"), data.get("etags")
    return (
        {key: entry for key, entry in apps.items() if isinstance(entry, dict)} if isinstance(apps, dict) else {},
        {url: entry for url, entry in etags.items() if isinstance(entry, dict)} if isinstance(etags, dict) else {},
    )


def _atomic_write_json(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=CACHE_DIR_MODE)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), CACHE_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
