"""core/updates.py: update sources, checks (fake fetchers), downloads (loopback server), cache.

Nothing here touches the network: checks use a fake fetcher, http_get/download_update talk to an
http.server on 127.0.0.1, and redirects to other hosts are refused before any connection is made.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import http.server
import json
import locale
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from easy_installer import __version__
from easy_installer.core import updates
from easy_installer.core.paths import Scope
from easy_installer.core.updates import (
    AvailableUpdate,
    HttpResponse,
    SimpleYamlError,
    UpdateCache,
    UpdateSource,
    base64_to_hex,
    check_for_update,
    choose_source,
    digests_equal,
    download_update,
    hex_to_base64,
    http_get,
    is_secure_url,
    normalize_hex_digest,
    parse_electron_update_config,
    parse_simple_yaml,
    parse_update_info,
    safe_download_name,
)
from easy_installer.errors import EasyInstallerError, NetworkError, UpdateCancelled, UpdateError

# ------------------------------------------------------------------------------------------------
# real-world data (fetched 2026-09-30)
# ------------------------------------------------------------------------------------------------

FREECAD_UPD_INFO = "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"
FREECAD_API = "https://api.github.com/repos/FreeCAD/FreeCAD/releases/latest"
FREECAD_INSTALLED = "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"
FREECAD_SHA256 = "f6dc6ba676e5ac96a565ebc8d657232f94c6158e85b4352141bd1a46f6b43434"
FREECAD_AARCH64_SHA256 = "675d1a4295b2183e0e1d816a4b5557dcb63aeced4e60aaa28d6c3686f4c71dc9"

T3_APP_UPDATE_YML = """\
owner: pingdotgg
repo: t3code
provider: github
releaseType: release
updaterCacheDirName: t3code-updater
"""
T3_SHA512_B64 = "cwBG1lccgeswFTbCqtdUCTSQkCJZ2ENx1TA9y0NCZxD8Eoy6tYEHlqRVsESjIHoRhmvPh4+5fm54mizxEBsonA=="
T3_SHA512_HEX = base64.b64decode(T3_SHA512_B64).hex()
DEB_SHA512_B64 = "KMgzzwk/qrtCMUVA3fo+XXSe9GgNmroBf8ghkjLBx4yXk7TCpcY9iCTkabxW1IakAQ04o+QZebrEgHGt9ZKLZA=="
T3_LATEST_YML = f"""\
version: 0.0.44
files:
  - url: T3-Code-0.0.44-x86_64.AppImage
    sha512: {T3_SHA512_B64}
    size: 158139658
    blockMapSize: 165150
  - url: T3-Code-0.0.44-amd64.deb
    sha512: {DEB_SHA512_B64}
    size: 125486208
path: T3-Code-0.0.44-x86_64.AppImage
sha512: {T3_SHA512_B64}
releaseDate: '2026-09-29T20:28:15.246Z'
"""
T3_YML_URL = "https://github.com/pingdotgg/t3code/releases/latest/download/latest-linux.yml"
T3_INSTALLED = "T3-Code-0.0.42-x86_64.AppImage"

FREECAD = UpdateSource(kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest",
                       pattern="FreeCAD*x86_64*.AppImage", via="upd_info")
T3 = UpdateSource(kind="electron-github", owner="pingdotgg", repo="t3code", via="app-update.yml")


def gh_asset(name: str, *, owner="FreeCAD", repo="FreeCAD", tag="1.1.4", size=1000,
             digest: str | None = None, **extra) -> dict:
    return {"name": name, "size": size, "state": "uploaded", "content_type": "application/octet-stream",
            "browser_download_url": f"https://github.com/{owner}/{repo}/releases/download/{tag}/{name}",
            "digest": digest, "download_count": 12345, **extra}


def gh_release(tag: str, assets: list[dict], *, prerelease=False, draft=False, owner="FreeCAD",
               repo="FreeCAD") -> dict:
    return {"tag_name": tag, "name": f"Release {tag}", "prerelease": prerelease, "draft": draft,
            "html_url": f"https://github.com/{owner}/{repo}/releases/tag/{tag}",
            "published_at": "2026-09-20T10:11:12Z", "body": "Release notes\n" * 2000,
            "author": {"login": "someone"}, "assets": assets}


def freecad_release(tag: str = "1.1.4") -> dict:
    assets = []
    for arch in ("x86_64", "aarch64"):
        name = f"FreeCAD_{tag}-Linux-{arch}-py311.AppImage"
        digest = "sha256:" + (FREECAD_SHA256 if arch == "x86_64" else FREECAD_AARCH64_SHA256)
        assets += [gh_asset(name, tag=tag, size=820812280 if arch == "x86_64" else 755591688, digest=digest),
                   gh_asset(name + "-SHA256.txt", tag=tag, size=108, digest="sha256:" + "ef" * 32),
                   gh_asset(name + ".zsync", tag=tag, size=1603406, digest="sha256:" + "cd" * 32)]
    for other in ("macOS-arm64-py311.dmg", "macOS-x86_64-py311.dmg", "Windows-x86_64-py311-installer.exe",
                  "Windows-x86_64-py311.7z"):
        assets += [gh_asset(f"FreeCAD_{tag}-{other}", tag=tag), gh_asset(f"FreeCAD_{tag}-{other}-SHA256.txt", tag=tag)]
    assets.append(gh_asset(f"freecad_source_{tag}.tar.gz", tag=tag))
    return gh_release(tag, assets)


# ------------------------------------------------------------------------------------------------
# fake fetcher
# ------------------------------------------------------------------------------------------------

@dataclass
class Call:
    url: str
    headers: dict
    max_bytes: int | None


@dataclass
class FakeFetcher:
    """``routes``: url -> HttpResponse | exception | callable(url, headers) -> HttpResponse."""

    routes: dict
    calls: list[Call] = field(default_factory=list)

    def __call__(self, url, *, headers=None, max_bytes=None):
        self.calls.append(Call(url, dict(headers or {}), max_bytes))
        assert url in self.routes, f"unexpected request: {url}"
        answer = self.routes[url]
        if callable(answer):
            answer = answer(url, dict(headers or {}))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]


def answer(body, *, status=200, headers=None, url="", truncated=False, redirects=()) -> HttpResponse:
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return HttpResponse(status=status, headers=dict(headers or {}), body=body, url=url, truncated=truncated,
                        redirects=tuple(redirects))


def check(source, fetch, *, version=None, sha256=None, filename=None, arch="x86_64", **kw):
    return check_for_update(source, current_version=version, current_sha256=sha256,
                            current_filename=filename, arch=arch, fetch=fetch, **kw)


def check_freecad(fetch, version="1.1.3", **kw):
    kw.setdefault("filename", FREECAD_INSTALLED)
    return check(FREECAD, fetch, version=version, **kw)


def check_t3(fetch, version="0.0.42", **kw):
    kw.setdefault("filename", T3_INSTALLED)
    return check(T3, fetch, version=version, **kw)


# ------------------------------------------------------------------------------------------------
# URLs, digests, file names
# ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://github.com/FreeCAD/FreeCAD",
    "HTTPS://Example.ORG:8443/a%20b?x=1#frag",
    "https://[2001:db8::1]/x",
])
def test_secure_urls(url):
    assert is_secure_url(url)


@pytest.mark.parametrize("url", [
    None, 42, "", "github.com/x", "http://github.com/x", "ftp://example.org/x", "file:///etc/passwd",
    "data:text/plain,hi", "javascript:alert(1)", "https://", "https:///path", "https://user@example.org/",
    "https://user:pw@example.org/", "https://example.org\\@evil.example/", "https://example.org/a b",
    "https://example.org/\nHost: evil", "https://exämple.org/", "https://example.org:port/",
    "http://127.0.0.1/x", "http://localhost:8000/x", "https://example.org/" + "a" * 3000,
])
def test_insecure_urls(url):
    assert not is_secure_url(url)


def test_insecure_localhost_only_when_asked():
    assert is_secure_url("http://127.0.0.1:8000/x", allow_insecure_localhost=True)
    assert is_secure_url("http://localhost/x", allow_insecure_localhost=True)
    assert not is_secure_url("http://127.0.0.1.example.org/x", allow_insecure_localhost=True)
    assert not is_secure_url("http://192.168.1.10/x", allow_insecure_localhost=True)
    assert not is_secure_url("http://user@localhost/x", allow_insecure_localhost=True)
    assert not is_secure_url("ftp://localhost/x", allow_insecure_localhost=True)


def test_base64_to_hex_real_sha512():
    assert base64_to_hex(T3_SHA512_B64, 64) == T3_SHA512_HEX
    assert T3_SHA512_HEX.startswith("730046d6571c81eb") and len(T3_SHA512_HEX) == 128
    assert hex_to_base64(T3_SHA512_HEX) == T3_SHA512_B64
    assert base64_to_hex(f"  {T3_SHA512_B64}\n", 64) == T3_SHA512_HEX


@pytest.mark.parametrize("value", [
    None, 12, "", "====", "not base64!", T3_SHA512_B64[:-2], T3_SHA512_B64 + "AAAA", "cwBG*lcc",
    "Zm9v", "cwBG1lcc\u00e4", T3_SHA512_B64.replace("+", "-"),
])
def test_base64_to_hex_rejects(value):
    assert base64_to_hex(value, 64) is None


def test_hex_digests():
    assert normalize_hex_digest("AB" * 32, 32) == "ab" * 32
    assert normalize_hex_digest(" " + "ab" * 32 + "\n", 32) == "ab" * 32
    for bad in (None, 5, "", "ab" * 31, "ab" * 33, "zz" * 32, "0x" + "ab" * 31):
        assert normalize_hex_digest(bad, 32) is None
    assert digests_equal("AB" * 32, "ab" * 32)
    assert not digests_equal("ab" * 32, "ab" * 31 + "ac")
    assert not digests_equal(None, None) and not digests_equal("", "") and not digests_equal("ab", None)
    with pytest.raises(ValueError):
        hex_to_base64("xyz")


@pytest.mark.parametrize("name, expected", [
    ("FreeCAD_1.1.4-Linux-x86_64-py311.AppImage", "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage"),
    ("../../etc/cron.d/evil.AppImage", "evil.AppImage"),
    ("C:\\Users\\x\\App.AppImage", "App.AppImage"),
    ("/abs/path/My App (beta) 1.0.AppImage", "My_App__beta__1.0.AppImage"),
    (".hidden.AppImage", "hidden.AppImage"),
    ("-rf.AppImage", "rf.AppImage"),
    ("Füße\n\x00.appimage", "F__e__.appimage"),
    ("a" * 300 + ".AppImage", "a" * 191 + ".AppImage"),
])
def test_safe_download_name(name, expected):
    result = safe_download_name(name)
    assert result == expected and len(result) <= 200
    assert "/" not in result and not result.startswith((".", "-"))


@pytest.mark.parametrize("name", [
    "", "App.deb", "App.AppImage.zsync", "App.AppImage.exe", ".AppImage", "dir/.AppImage", "App.AppImage/",
    "..AppImage", "___.AppImage", "latest-linux.yml", "App.AppImage\x00.sh",
])
def test_safe_download_name_refuses_non_appimages(name):
    with pytest.raises(UpdateError) as err:
        safe_download_name(name)
    assert "AppImage" in str(err.value)


# ------------------------------------------------------------------------------------------------
# UpdateSource
# ------------------------------------------------------------------------------------------------

def test_update_source_roundtrip_and_texts():
    assert UpdateSource.from_dict(FREECAD.to_dict()) == FREECAD
    assert UpdateSource.from_dict(T3.to_dict()) == T3
    json.dumps(FREECAD.to_dict())
    assert FREECAD.describe() == "github.com/FreeCAD/FreeCAD"
    assert FREECAD.homepage() == "https://github.com/FreeCAD/FreeCAD"
    assert T3.describe() == "github.com/pingdotgg/t3code"
    generic = UpdateSource(kind="electron-generic", url="https://updates.example.org/app/linux")
    assert UpdateSource.from_dict(generic.to_dict()) == generic
    assert generic.describe() == "updates.example.org" and generic.homepage() is None
    zsync = UpdateSource(kind="zsync-url", url="https://dl.example.org/App-latest.AppImage.zsync")
    assert UpdateSource.from_dict(zsync.to_dict()) == zsync
    assert zsync.describe() == "dl.example.org" and zsync.homepage() is None


def test_update_source_from_minimal_dict():
    source = UpdateSource.from_dict({"kind": "electron-github", "owner": "o", "repo": "r", "future": 1})
    assert source == UpdateSource(kind="electron-github", owner="o", repo="r")
    assert source.prerelease is False and source.via == ""


@pytest.mark.parametrize("data", [
    None, "gh", [], {}, {"kind": "github-assets"}, {"kind": "bintray", "owner": "o", "repo": "r"},
    {"kind": "github-assets", "owner": "o", "repo": "r", "release": "latest"},          # no pattern
    {"kind": "github-assets", "owner": "o", "repo": "r", "pattern": "*.AppImage"},       # no release
    {"kind": "github-assets", "owner": "o/../x", "repo": "r", "release": "latest", "pattern": "*"},
    {"kind": "github-assets", "owner": "o", "repo": "..", "release": "latest", "pattern": "*"},
    {"kind": "github-assets", "owner": "o", "repo": "r?x=1", "release": "latest", "pattern": "*"},
    {"kind": "github-assets", "owner": "o", "repo": "r", "release": "a b", "pattern": "*"},
    {"kind": "github-assets", "owner": "o", "repo": "r", "release": "../x", "pattern": "*"},
    {"kind": "github-assets", "owner": "o", "repo": "r", "release": "latest", "pattern": "a/b"},
    {"kind": "github-assets", "owner": "o", "repo": "r", "release": "latest", "pattern": "a\nb"},
    {"kind": "github-assets", "owner": 1, "repo": "r", "release": "latest", "pattern": "*"},
    {"kind": "electron-github", "owner": "-o", "repo": "r"},
    {"kind": "electron-github", "owner": "o", "repo": "r", "prerelease": "yes"},
    {"kind": "electron-generic", "url": "http://example.org/app"},
    {"kind": "electron-generic", "url": "https://example.org/${os}"},
    {"kind": "electron-generic"},
    {"kind": "zsync-url", "url": "ftp://example.org/a.zsync"},
    {"kind": "zsync-url", "url": ["https://example.org/a.zsync"]},
    {"kind": ["zsync-url"], "url": "https://example.org/a.zsync"},
])
def test_update_source_from_dict_rejects(data):
    assert UpdateSource.from_dict(data) is None


def test_update_source_via_is_sanitised():
    source = UpdateSource.from_dict({"kind": "electron-github", "owner": "o", "repo": "r", "via": "x\ny"})
    assert source is not None and source.via == ""


# ------------------------------------------------------------------------------------------------
# parse_update_info
# ------------------------------------------------------------------------------------------------

def test_parse_update_info_freecad():
    source = parse_update_info(FREECAD_UPD_INFO)
    assert source == FREECAD
    assert source.kind == "github-assets" and source.pattern == "FreeCAD*x86_64*.AppImage"
    assert source.via == "upd_info" and source.release == "latest"


@pytest.mark.parametrize("release", ["latest", "latest-pre", "latest-all", "continuous", "v1.2.3", "weekly-builds"])
def test_parse_update_info_releases(release):
    source = parse_update_info(f"gh-releases-zsync|probonopd|AppImages|{release}|Subsurface-*x86_64.AppImage.zsync")
    assert source is not None and source.release == release and source.pattern == "Subsurface-*x86_64.AppImage"


def test_parse_update_info_zsync_and_padding():
    url = "https://download.example.org/apps/App-latest-x86_64.AppImage.zsync"
    assert parse_update_info(f"zsync|{url}") == UpdateSource(kind="zsync-url", url=url, via="upd_info")
    assert parse_update_info(FREECAD_UPD_INFO + "\x00\x00\x00") == FREECAD
    assert parse_update_info(f"  {FREECAD_UPD_INFO}\n") == FREECAD


@pytest.mark.parametrize("text", [
    None, "", "   ", "\x00\x00", 12, b"zsync|https://example.org/a.zsync",
    "zsync|http://example.org/App.AppImage.zsync",                   # not https
    "zsync|ftp://example.org/App.AppImage.zsync",
    "zsync|//example.org/App.AppImage.zsync",
    "zsync|https://user:pw@example.org/App.AppImage.zsync",
    "zsync|https://example.org/a.zsync|extra",                       # extra pipe
    "zsync|",
    "zsync",
    "zsync|https://example.org/a b.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest",                      # too few fields
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|a.zsync|b",            # extra pipe
    "gh-releases-zsync||FreeCAD|latest|FreeCAD*.AppImage.zsync",     # empty fields
    "gh-releases-zsync|FreeCAD||latest|FreeCAD*.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD||FreeCAD*.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*.AppImage",    # not a .zsync name
    "gh-releases-zsync|Free CAD|FreeCAD|latest|a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|Free/CAD|latest|a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|..|latest|a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|../../x|a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|../a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest| a.AppImage.zsync",
    "gh-releases-zsync|FreeCAD|FreeCAD|latest|a\x01.AppImage.zsync",
    "gh-releases-zsync|FreeCAD\n|FreeCAD|latest|a.AppImage.zsync",
    "GH-RELEASES-ZSYNC|FreeCAD|FreeCAD|latest|a.AppImage.zsync",
    "gh-releases-direct|FreeCAD|FreeCAD|latest|a.AppImage",
    "bintray-zsync|probono|AppImages|Subsurface|Subsurface-_latestVersion-x86_64.AppImage.zsync",
    "pling-v1-zsync|1234567|App-*x86_64.AppImage.zsync",
    "https://example.org/App.AppImage.zsync",
    "|||||",
    "gh-releases-zsync|" + "a" * 5000 + "|r|latest|a.AppImage.zsync",  # giant
    "zsync|https://example.org/" + "a" * 5000 + ".zsync",
])
def test_parse_update_info_rejects(text):
    assert parse_update_info(text) is None


def test_parse_update_info_giant_input_is_fast():
    start = time.monotonic()
    assert parse_update_info("gh-releases-zsync|" + "|" * 5_000_000) is None
    assert parse_update_info("x" * 50_000_000) is None
    assert time.monotonic() - start < 2


# ------------------------------------------------------------------------------------------------
# the YAML subset
# ------------------------------------------------------------------------------------------------

def test_yaml_real_latest_linux_yml():
    data = parse_simple_yaml(T3_LATEST_YML)
    assert list(data) == ["version", "files", "path", "sha512", "releaseDate"]
    assert data["version"] == "0.0.44"
    assert data["files"] == [
        {"url": "T3-Code-0.0.44-x86_64.AppImage", "sha512": T3_SHA512_B64, "size": "158139658",
         "blockMapSize": "165150"},
        {"url": "T3-Code-0.0.44-amd64.deb", "sha512": DEB_SHA512_B64, "size": "125486208"},
    ]
    assert data["path"] == "T3-Code-0.0.44-x86_64.AppImage"
    assert data["releaseDate"] == "2026-09-29T20:28:15.246Z"


def test_yaml_real_app_update_yml():
    assert parse_simple_yaml(T3_APP_UPDATE_YML) == {
        "owner": "pingdotgg", "repo": "t3code", "provider": "github", "releaseType": "release",
        "updaterCacheDirName": "t3code-updater"}


def test_yaml_scalars_quotes_and_comments():
    data = parse_simple_yaml(
        "\ufeff# a comment\r\n"
        "---\r\n"
        "plain: some text here   # trailing comment\r\n"
        "hash: a#b\r\n"
        "url: https://example.org/x?y=1#frag\r\n"
        "single: 'it''s # not a comment'   # comment\r\n"
        'double: "tab\\there \\"q\\" \\u00e4 \\x41 \\\\ #"\r\n'
        "empty_single: ''\r\n"
        'empty_double: ""\r\n'
        "nothing:\r\n"
        "tilde: ~\r\n"
        "null_word: null\r\n"
        "quoted_null: 'null'\r\n"
        "number: 12\r\n"
        "truth: true\r\n"
        "spaced   :   value  \r\n"
        "dotted.key-1_x: v\r\n"
        "\r\n"
        "   # indented comment\r\n"
        "last: x\r\n"
        "...\r\n"
    )
    assert data == {
        "plain": "some text here", "hash": "a#b", "url": "https://example.org/x?y=1#frag",
        "single": "it's # not a comment", "double": 'tab\there "q" ä A \\ #', "empty_single": "",
        "empty_double": "", "nothing": None, "tilde": None, "null_word": None, "quoted_null": "null",
        "number": "12", "truth": "true", "spaced": "value", "dotted.key-1_x": "v", "last": "x"}


def test_yaml_lists_and_mappings():
    data = parse_simple_yaml(
        "files:\n"
        "- url: a.AppImage\n"
        "  size: 1\n"
        "\n"
        "  # comment inside\n"
        "-   url: 'b.AppImage'\n"
        "    size: 2\n"
        "    note:\n"
        "names:\n"
        "  - one\n"
        "  - 'two words'\n"
        "  - \"three\"\n"
        "info:\n"
        "    a: 1\n"
        "    b: two\n"
        "notes: |\n"
        "  line: one\n"
        "\n"
        "    - indented [x]\n"
        "after: yes\n"
        "folded: >-\n"
        "  text\n"
    )
    assert data["files"] == [{"url": "a.AppImage", "size": "1"},
                             {"url": "b.AppImage", "size": "2", "note": None}]
    assert data["names"] == ["one", "two words", "three"]
    assert data["info"] == {"a": "1", "b": "two"}
    assert data["notes"] == "line: one\n\n  - indented [x]"
    assert data["after"] == "yes" and data["folded"] == "text"


def test_yaml_empty_documents():
    assert parse_simple_yaml("") == {}
    assert parse_simple_yaml("\n# only a comment\n\n") == {}
    assert parse_simple_yaml("---\n") == {}
    assert parse_simple_yaml("key:") == {"key": None}


@pytest.mark.parametrize("text", [
    "just text",                                  # not a mapping
    "- a\n- b",                                   # top-level list
    "key:value",                                  # no space after the colon
    ": value",
    "'quoted key': 1",
    "key: [a, b]",                                # flow collections
    "key: {a: b}",
    "key: &anchor value",                         # anchors, aliases, tags
    "key: *alias",
    "key: !!python/object/apply:os.system ['id']",
    "key: !tag x",
    "<<: *merge",
    "key: @x",
    "key: `x`",
    "key: %x",
    "key: a: b",                                  # mapping inside a value
    "key: value:",
    "key: 'unterminated",
    'key: "unterminated',
    'key: "bad \\q escape"',
    'key: "bad \\u12"',
    'key: "\\ud800"',
    "key: 'a' b",
    'key: "a"b',
    "key: 'a'#b",
    "a: 1\na: 2",                                 # duplicate keys
    "files:\n  - url: a\n    url: b",
    "info:\n  a: 1\n  a: 2",
    "\tkey: value",                               # tabs as indentation
    "files:\n\t- a",
    "  key: value",                               # indented top level
    "a: 1\n  b: 2",
    "files:\n  - url: a\n      deeper: x",        # wrong indentation in an item
    "files:\n  - url: a\n   size: 1",
    "files:\n  - url:\n      nested: x",          # nesting below an item
    "files:\n  - - a",                            # list in a list
    "files:\n  -\n    url: a",
    "files:\n  - url: a\n  bad",
    "info:\n  a:\n    b: 1",                      # two levels of mappings
    "info:\n  a: 1\n    b: 2",
    "files:\n  - url: a\n    text: |\n      x",   # block scalar in an item
    "a: 1\n---\nb: 2",                            # second document
    "a: 1\n...\nb: 2",
    "%YAML 1.2\n---\na: 1",
    "? complex\n: key",
    "key: - x",
])
def test_yaml_rejects_everything_else(text):
    with pytest.raises(SimpleYamlError):
        parse_simple_yaml(text)


def test_yaml_limits():
    with pytest.raises(SimpleYamlError):
        parse_simple_yaml("a: " + "x" * (updates.MAX_YML_BYTES + 1))
    with pytest.raises(SimpleYamlError):
        parse_simple_yaml("\n" * (updates.MAX_YAML_LINES + 1))
    with pytest.raises(SimpleYamlError):
        parse_simple_yaml("files:\n" + "  - x\n" * (updates.MAX_YAML_ITEMS + 1))
    with pytest.raises(SimpleYamlError):
        parse_simple_yaml(b"a: 1")   # type: ignore[arg-type]
    many = parse_simple_yaml("files:\n" + "  - x\n" * updates.MAX_YAML_ITEMS)
    assert len(many["files"]) == updates.MAX_YAML_ITEMS


# ------------------------------------------------------------------------------------------------
# parse_electron_update_config / choose_source
# ------------------------------------------------------------------------------------------------

def test_electron_config_t3code():
    source = parse_electron_update_config(T3_APP_UPDATE_YML)
    assert source == T3
    assert source.kind == "electron-github" and source.prerelease is False
    assert source.via == "app-update.yml" and source.describe() == "github.com/pingdotgg/t3code"
    assert parse_electron_update_config(T3_APP_UPDATE_YML.encode()) == T3
    assert parse_electron_update_config(T3_APP_UPDATE_YML.replace("\n", "\r\n")) == T3


def test_electron_config_variants():
    pre = parse_electron_update_config("provider: github\nowner: o\nrepo: r\nreleaseType: prerelease\n")
    assert pre == UpdateSource(kind="electron-github", owner="o", repo="r", prerelease=True,
                               via="app-update.yml")
    assert parse_electron_update_config("provider: github\nowner: o\nrepo: r\n").prerelease is False
    assert parse_electron_update_config("provider: github\nrepo: o/r\n").owner == "o"
    quoted = parse_electron_update_config("provider: 'github'\nowner: \"o\"\nrepo: r  # x\nhost: github.com\n"
                                          "publisherName:\n  - Some Company\nchannel: latest\nprivate: false\n")
    assert quoted == UpdateSource(kind="electron-github", owner="o", repo="r", via="app-update.yml")
    generic = parse_electron_update_config(
        "provider: generic\nurl: https://updates.example.org/app/\nupdaterCacheDirName: app-updater\n")
    assert generic == UpdateSource(kind="electron-generic", url="https://updates.example.org/app/",
                                   via="app-update.yml")


@pytest.mark.parametrize("text", [
    None, "", 5, b"\xff\xfe", "provider: github", "provider: github\nowner: o",
    "provider: github\nowner: o\nrepo: r\nhost: github.example.com",        # GitHub Enterprise
    "provider: github\nowner: o\nrepo: r\nprivate: true",                   # needs a token
    "provider: github\nowner: o\nrepo: r\nreleaseType: draft",
    "provider: github\nowner: o\nrepo: r\nreleaseType: nightly",
    "provider: github\nowner: o\nrepo: r\nchannel: beta",
    "provider: github\nowner: o/x\nrepo: r",
    "provider: github\nowner: o\nrepo: ../..",
    "provider: github\nowner:\n  - o\nrepo: r",
    "provider: github\nowner: o\nrepo:\n  name: r",
    "provider: generic",
    "provider: generic\nurl: http://updates.example.org/app",               # not https
    "provider: generic\nurl: https://user:pw@updates.example.org/app",
    "provider: generic\nurl: https://updates.example.org/${os}/${arch}",
    "provider: generic\nurl: //updates.example.org/app",
    "provider: generic\nurl: https://updates.example.org/app\nchannel: beta",
    "provider: s3\nbucket: my-bucket",
    "provider: spaces\nname: x\nregion: ams3",
    "provider: keygen\naccount: x\nproduct: y",
    "provider: bitbucket\nowner: o\nslug: s",
    "provider: snapStore\nrepo: r",
    "provider: custom\nurl: https://example.org",
    "owner: o\nrepo: r",                                                    # no provider
    "provider: [github]\nowner: o\nrepo: r",
    "provider: github\nowner: &a o\nrepo: *a",
    "provider: github\nowner: o\nrepo: r\nowner: evil",                     # duplicate key
    "{provider: github, owner: o, repo: r}",
    "provider: github\n" + "x: y\n" * 40000,                                # larger than 64 KiB
    "a" * 10_000_000,
])
def test_electron_config_rejects(text):
    assert parse_electron_update_config(text) is None


def test_choose_source_prefers_electron():
    assert choose_source(FREECAD_UPD_INFO, None) == FREECAD
    assert choose_source(None, T3_APP_UPDATE_YML) == T3
    assert choose_source(FREECAD_UPD_INFO, T3_APP_UPDATE_YML) == T3
    assert choose_source(FREECAD_UPD_INFO, "provider: s3\nbucket: b") == FREECAD
    assert choose_source("bintray-zsync|a|b|c|d.zsync", "provider: custom") is None
    assert choose_source(None, None) is None


# ------------------------------------------------------------------------------------------------
# AvailableUpdate
# ------------------------------------------------------------------------------------------------

def make_update(**kw) -> AvailableUpdate:
    data = dict(version="1.1.4", url="https://github.com/o/r/releases/download/1.1.4/App-1.1.4.AppImage",
                filename="App-1.1.4.AppImage", size=1234, sha256="ab" * 32, sha512="cd" * 64,
                release_url="https://github.com/o/r/releases/tag/1.1.4", published_at="2026-09-20T10:11:12Z")
    data.update(kw)
    return AvailableUpdate(**data)


def test_available_update_roundtrip():
    update = make_update()
    data = update.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert AvailableUpdate.from_dict(data) == update
    assert data["version"] == "1.1.4" and data["size"] == 1234 and data["sha1"] is None
    minimal = AvailableUpdate.from_dict({"url": "https://example.org/a.AppImage", "filename": "a.AppImage"})
    assert minimal == AvailableUpdate(version=None, url="https://example.org/a.AppImage", filename="a.AppImage")
    positional = AvailableUpdate("1.0", "https://example.org/a.AppImage", "a.AppImage", 5, None, None, None, None)
    assert positional.size == 5 and positional.sha1 is None


def test_available_update_from_dict_cleans_up():
    update = AvailableUpdate.from_dict({
        "version": "v2.0", "url": "https://example.org/a.AppImage", "filename": "a.AppImage",
        "size": -5, "sha256": "AB" * 32, "sha512": "nope", "sha1": "EF" * 20,
        "release_url": "http://example.org/notes", "published_at": 12, "unknown": True})
    assert update == AvailableUpdate(version="2.0", url="https://example.org/a.AppImage",
                                     filename="a.AppImage", size=None, sha256="ab" * 32, sha512=None,
                                     release_url=None, published_at=None, sha1="ef" * 20)
    assert AvailableUpdate.from_dict({"url": "https://e.org/a.AppImage", "filename": "a.AppImage",
                                      "size": True}).size is None


@pytest.mark.parametrize("data", [
    None, [], "x", {}, {"url": "https://example.org/a.AppImage"}, {"filename": "a.AppImage"},
    {"url": "http://example.org/a.AppImage", "filename": "a.AppImage"},
    {"url": "file:///tmp/a.AppImage", "filename": "a.AppImage"},
    {"url": "https://example.org/a.AppImage", "filename": "../a.AppImage"},
    {"url": "https://example.org/a.AppImage", "filename": "a\nb.AppImage"},
    {"url": "https://example.org/a.AppImage", "filename": ""},
    {"url": 5, "filename": "a.AppImage"},
])
def test_available_update_from_dict_rejects(data):
    assert AvailableUpdate.from_dict(data) is None


# ------------------------------------------------------------------------------------------------
# check_for_update: github-assets
# ------------------------------------------------------------------------------------------------

def test_freecad_update_found():
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release(), url=FREECAD_API)})
    update = check_freecad(fetch)
    assert update == AvailableUpdate(
        version="1.1.4",
        url="https://github.com/FreeCAD/FreeCAD/releases/download/1.1.4/FreeCAD_1.1.4-Linux-x86_64-py311.AppImage",
        filename="FreeCAD_1.1.4-Linux-x86_64-py311.AppImage", size=820812280, sha256=FREECAD_SHA256,
        sha512=None, release_url="https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.4",
        published_at="2026-09-20T10:11:12Z")
    assert fetch.urls == [FREECAD_API]
    call = fetch.calls[0]
    assert call.max_bytes == 8 * 1024 * 1024
    assert call.headers["Accept"] == "application/vnd.github+json"
    assert "If-None-Match" not in call.headers and "Authorization" not in call.headers


@pytest.mark.parametrize("version", ["1.1.4", "v1.1.4", "1.1.4.0", "1.2.0", "2.0"])
def test_freecad_up_to_date_or_newer_installed(version):
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release())})
    assert check_freecad(fetch, version=version) is None


@pytest.mark.parametrize("pattern", ["FreeCAD*x86_64*.AppImage", "FreeCAD*x86_64*", "FreeCAD*", "*"])
def test_pattern_never_selects_zsync_or_checksum_files(pattern):
    source = UpdateSource(kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest", pattern=pattern)
    update = check(source, FakeFetcher({FREECAD_API: answer(freecad_release())}), version="1.1.3",
                   filename=FREECAD_INSTALLED)
    assert update.filename == "FreeCAD_1.1.4-Linux-x86_64-py311.AppImage"
    assert update.url.endswith("/FreeCAD_1.1.4-Linux-x86_64-py311.AppImage")


def test_pattern_is_case_sensitive():
    source = UpdateSource(kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest",
                          pattern="freecad*.AppImage")
    assert check(source, FakeFetcher({FREECAD_API: answer(freecad_release())}), version="1.1.3") is None


def test_aarch64_gets_its_own_asset_or_nothing():
    broad = UpdateSource(kind="github-assets", owner="FreeCAD", repo="FreeCAD", release="latest",
                         pattern="FreeCAD*.AppImage")
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release())})
    update = check(broad, fetch, version="1.1.3", arch="aarch64",
                   filename="FreeCAD_1.1.3-Linux-aarch64-py311.AppImage")
    assert update.filename == "FreeCAD_1.1.4-Linux-aarch64-py311.AppImage"
    assert update.sha256 == FREECAD_AARCH64_SHA256 and update.size == 755591688
    assert check(broad, fetch, version="1.1.3", arch="aarch64").filename.endswith("aarch64-py311.AppImage")
    assert check(broad, fetch, version="1.1.3", arch="x86_64").filename.endswith("x86_64-py311.AppImage")
    # the x86_64-only pattern of the installed file offers nothing to another CPU
    assert check_freecad(fetch, arch="aarch64") is None
    assert check(broad, fetch, version="1.1.3", arch="armhf") is None
    # an unknown CPU does not filter
    assert check(broad, fetch, version="1.1.3", arch="unknown") is not None


def test_arch_aliases_in_asset_names():
    url = "https://api.github.com/repos/o/r/releases/latest"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App-*.AppImage")
    assets = [gh_asset("App-2.0-arm64.AppImage"), gh_asset("App-2.0-armv7l.AppImage"),
              gh_asset("App-2.0-i386.AppImage"), gh_asset("App-2.0-amd64.AppImage")]
    fetch = FakeFetcher({url: answer(gh_release("v2.0", assets))})
    names = {arch: check(source, fetch, version="1.0", arch=arch).filename
             for arch in ("x86_64", "aarch64", "armhf", "i686")}
    assert names == {"x86_64": "App-2.0-amd64.AppImage", "aarch64": "App-2.0-arm64.AppImage",
                     "armhf": "App-2.0-armv7l.AppImage", "i686": "App-2.0-i386.AppImage"}


def test_asset_without_arch_in_name_is_accepted():
    url = "https://api.github.com/repos/o/r/releases/latest"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App-*.AppImage")
    fetch = FakeFetcher({url: answer(gh_release("v2.0", [gh_asset("App-2.0.AppImage"),
                                                         gh_asset("App-2.0-arm64.AppImage")]))})
    assert check(source, fetch, version="1.0", arch="x86_64").filename == "App-2.0.AppImage"
    assert check(source, fetch, version="1.0", arch="aarch64").filename == "App-2.0-arm64.AppImage"


def test_several_matching_assets_prefers_the_installed_flavour():
    url = "https://api.github.com/repos/o/r/releases/latest"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App*.AppImage")
    assets = [gh_asset("App_2.0-Linux-x86_64-py312.AppImage"), gh_asset("App_2.0-Linux-x86_64-py311.AppImage"),
              gh_asset("App_2.0-Linux-x86_64-py311-debug.AppImage"), gh_asset("App_2.0-Linux-aarch64-py311.AppImage"),
              gh_asset("AppTools_2.0-Linux-x86_64.AppImage")]
    fetch = FakeFetcher({url: answer(gh_release("2.0", assets))})

    def picked(filename):
        return check(source, fetch, version="1.0", filename=filename).filename

    assert picked("App_1.0-Linux-x86_64-py311.AppImage") == "App_2.0-Linux-x86_64-py311.AppImage"
    assert picked("App_1.0-Linux-x86_64-py312.AppImage") == "App_2.0-Linux-x86_64-py312.AppImage"
    assert picked("App_1.0-Linux-x86_64-py311-debug.AppImage") == "App_2.0-Linux-x86_64-py311-debug.AppImage"
    assert picked("AppTools_1.0-Linux-x86_64.AppImage") == "AppTools_2.0-Linux-x86_64.AppImage"
    # nothing known about the installed file: the first asset for this CPU
    assert picked(None) == "App_2.0-Linux-x86_64-py312.AppImage"


def test_no_matching_asset_is_not_an_update():
    url = "https://api.github.com/repos/o/r/releases/latest"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App-*x86_64.AppImage")
    for assets in ([], [gh_asset("Other-2.0-x86_64.AppImage")], [gh_asset("App-2.0-x86_64.AppImage.zsync")],
                   [gh_asset("App-2.0-x86_64.AppImage", state="new")],
                   [gh_asset("App-2.0-x86_64.AppImage", browser_download_url="http://github.com/o/r/a.AppImage")],
                   [{"name": 5}, "junk", None]):
        assert check(source, FakeFetcher({url: answer(gh_release("2.0", assets))}), version="1.0") is None


def test_version_from_tag_when_the_asset_has_none():
    url = "https://api.github.com/repos/o/r/releases/latest"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App-*.AppImage")

    def version(tag, current="1.0"):
        fetch = FakeFetcher({url: answer(gh_release(tag, [gh_asset("App-x86_64.AppImage")]))})
        update = check(source, fetch, version=current, filename="App-x86_64.AppImage")
        return update.version if update else None

    assert version("v2.0") == "2.0"
    assert version("2.0.1") == "2.0.1"
    assert version("release-2.1.0") == "2.1.0"
    assert version("3") == "3"
    assert version("v1.0") is None and version("0.9") is None      # not newer


def test_unknown_versions_compare_checksum_then_file_name():
    url = "https://api.github.com/repos/o/r/releases/tags/continuous"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="continuous", pattern="App-*.AppImage")
    new_sha, old_sha = "11" * 32, "22" * 32

    def found(assets, **kw):
        fetch = FakeFetcher({url: answer(gh_release("continuous", assets))})
        return check(source, fetch, **kw)

    with_digest = [gh_asset("App-continuous-x86_64.AppImage", digest=f"sha256:{new_sha}")]
    update = found(with_digest, version="1.0", sha256=old_sha, filename="App-continuous-x86_64.AppImage")
    assert update.version is None and update.sha256 == new_sha
    assert found(with_digest, version="1.0", sha256=new_sha.upper(), filename="App-other.AppImage") is None
    assert found(with_digest, version=None, sha256=old_sha, filename=None) is not None
    # no published checksum: the file name decides
    plain = [gh_asset("App-continuous-x86_64.AppImage", digest=None)]
    assert found(plain, version="1.0", sha256=old_sha, filename="App-continuous-x86_64.AppImage") is None
    assert found(plain, version="1.0", sha256=old_sha, filename="App-build41-x86_64.AppImage") is not None
    # nothing to compare with: never offer the same file again and again
    assert found(plain, version=None, sha256=None, filename=None) is None
    # installed version unknown, remote version known
    versioned = [gh_asset("App-2.0-x86_64.AppImage", digest=f"sha256:{new_sha}")]
    assert found(versioned, version=None, sha256=new_sha, filename="x.AppImage") is None
    assert found(versioned, version=None, sha256=old_sha, filename="x.AppImage").version == "2.0"
    # the very same file is never an update, whatever the version texts say
    assert found(versioned, version="1.0", sha256=new_sha, filename="x.AppImage") is None
    assert found(versioned, version="1.0", sha256=old_sha, filename="x.AppImage").version == "2.0"
    # a digest that is not sha256 is ignored
    odd = [gh_asset("App-continuous-x86_64.AppImage", digest="sha512:" + "ab" * 64)]
    assert found(odd, sha256=old_sha, filename="App-old.AppImage").sha256 is None


def test_release_tag_is_quoted_in_the_url():
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="builds/nightly+1", pattern="*.AppImage")
    url = "https://api.github.com/repos/o/r/releases/tags/builds%2Fnightly%2B1"
    fetch = FakeFetcher({url: answer(gh_release("builds/nightly+1", [gh_asset("App-3.0.AppImage")]))})
    assert check(source, fetch, version="1.0").version == "3.0"
    assert fetch.urls == [url]


def releases_list() -> list[dict]:
    return [
        gh_release("v3.0-draft", [gh_asset("App-3.0-x86_64.AppImage", tag="v3.0-draft")], draft=True),
        gh_release("docs-2026", [gh_asset("Manual.pdf", tag="docs-2026")]),
        gh_release("v2.1", [gh_asset("App-2.1-x86_64.AppImage", tag="v2.1")]),
        gh_release("v2.2-beta1", [gh_asset("App-2.2-beta1-x86_64.AppImage", tag="v2.2-beta1")], prerelease=True),
        gh_release("v2.0", [gh_asset("App-2.0-x86_64.AppImage", tag="v2.0")]),
    ]


def test_prerelease_channels_use_the_release_list():
    url = "https://api.github.com/repos/o/r/releases?per_page=20"
    fetch = FakeFetcher({url: answer(releases_list())})
    pre = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest-pre", pattern="App-*.AppImage")
    update = check(pre, fetch, version="2.0")
    assert update.version == "2.2-beta1" and "/v2.2-beta1/" in update.url
    every = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest-all", pattern="App-*.AppImage")
    update = check(every, fetch, version="2.0")
    assert update.version == "2.2-beta1"           # drafts and releases without the app are skipped
    assert check(every, fetch, version="2.2-beta1") is None
    assert set(fetch.urls) == {url}
    # no pre-release published: the newest release
    stable_only = [r for r in releases_list() if not r["prerelease"]]
    assert check(pre, FakeFetcher({url: answer(stable_only)}), version="2.0").version == "2.1"


def test_an_older_release_never_hides_a_newer_one():
    """UPD-4: the newest version of the channel wins, not the first release with an asset."""
    url = "https://api.github.com/repos/o/r/releases?per_page=20"

    def channel(release: str, *releases: tuple[str, bool]):
        listed = [gh_release(f"v{v}", [gh_asset(f"App-{v}-x86_64.AppImage", tag=f"v{v}")],
                             prerelease=pre) for v, pre in releases]
        source = UpdateSource(kind="github-assets", owner="o", repo="r", release=release,
                              pattern="App-*.AppImage")
        return lambda installed: check(source, FakeFetcher({url: answer(listed)}),
                                       version=installed)

    pre = channel("latest-pre", ("2.1.0", False), ("2.0.0", False), ("2.0.0-rc1", True))
    assert pre("2.0.0").version == "2.1.0"
    assert pre("1.9.0").version == "2.1.0"
    rc_then_stable = channel("latest-pre", ("2.1.0", False), ("2.1.0-rc1", True))
    assert rc_then_stable("2.1.0-rc1").version == "2.1.0"
    backport_last = channel("latest-all", ("1.9.5", False), ("2.1.0-rc1", True), ("2.0.0", False))
    assert backport_last("2.0.0").version == "2.1.0-rc1"
    assert backport_last("2.1.0-rc1") is None


def test_latest_asks_only_for_the_latest_full_release():
    # GitHub's /releases/latest never is a pre-release or draft; nothing else is requested
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest", pattern="App-*.AppImage")
    url = "https://api.github.com/repos/o/r/releases/latest"
    fetch = FakeFetcher({url: answer(gh_release("v2.1", [gh_asset("App-2.1-x86_64.AppImage")]))})
    assert check(source, fetch, version="2.0").version == "2.1"
    assert fetch.urls == [url]


def test_empty_release_list_means_no_update_information():
    url = "https://api.github.com/repos/o/r/releases?per_page=20"
    source = UpdateSource(kind="github-assets", owner="o", repo="r", release="latest-all", pattern="*.AppImage")
    with pytest.raises(UpdateError, match="No update information"):
        check(source, FakeFetcher({url: answer([])}), version="1.0")
    only_drafts = [gh_release("v1", [gh_asset("App-9.0.AppImage")], draft=True)]
    with pytest.raises(UpdateError, match="No update information"):
        check(source, FakeFetcher({url: answer(only_drafts)}), version="1.0")


def test_etag_is_stored_and_a_304_reuses_the_cached_answer():
    etags: dict = {}
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release(), headers={"ETag": 'W/"abc123"'})})
    first = check_freecad(fetch, etags=etags)
    assert first.version == "1.1.4"
    entry = etags[FREECAD_API]
    assert entry["etag"] == 'W/"abc123"' and isinstance(entry["body"], str) and entry["saved_at"].endswith("Z")
    assert "Release notes" not in entry["body"] and len(entry["body"]) < 2000    # only what is needed
    assert ".zsync" not in entry["body"] and "Windows" not in entry["body"]
    json.dumps(etags)

    def not_modified(url, headers):
        assert headers["If-None-Match"] == 'W/"abc123"'
        return answer(b"", status=304, headers={"etag": 'W/"abc123"'})

    fetch = FakeFetcher({FREECAD_API: not_modified})
    assert check_freecad(fetch, etags=etags) == first
    assert check_freecad(fetch, etags=etags, version="1.1.4") is None
    assert etags[FREECAD_API] == entry and len(fetch.calls) == 2


def test_new_answer_replaces_the_etag_and_missing_etag_forgets_it():
    etags = {FREECAD_API: {"etag": '"old"', "body": "[]", "saved_at": "2026-01-01T00:00:00Z"}}
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release("1.1.5"), headers={"etag": '"new"'})})
    assert check_freecad(fetch, etags=etags).version == "1.1.5"
    assert fetch.calls[0].headers["If-None-Match"] == '"old"'
    assert etags[FREECAD_API]["etag"] == '"new"'
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release("1.1.6"))})
    assert check_freecad(fetch, etags=etags).version == "1.1.6"
    assert FREECAD_API not in etags


def test_unusable_cached_answer_is_fetched_again():
    etags = {FREECAD_API: {"etag": '"e1"', "body": "{broken", "saved_at": "2026-01-01T00:00:00Z"}}

    def route(url, headers):
        if "If-None-Match" in headers:
            return answer(b"", status=304)
        return answer(freecad_release(), headers={"ETag": '"e2"'})

    fetch = FakeFetcher({FREECAD_API: route})
    assert check_freecad(fetch, etags=etags).version == "1.1.4"
    assert len(fetch.calls) == 2 and "If-None-Match" not in fetch.calls[1].headers
    assert etags[FREECAD_API]["etag"] == '"e2"'


@pytest.mark.parametrize("entry", [
    "text", None, {}, {"etag": '"x"'}, {"etag": 5, "body": "[]"}, {"etag": "", "body": "[]"},
    {"etag": "bad\r\nX-Injected: 1", "body": "[]"}, {"etag": "é", "body": "[]"}, {"etag": "x" * 1000, "body": "[]"},
    {"etag": '"x"', "body": None},
])
def test_broken_etag_entries_are_not_sent(entry):
    etags = {FREECAD_API: entry}
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release())})
    assert check_freecad(fetch, etags=etags).version == "1.1.4"
    assert "If-None-Match" not in fetch.calls[0].headers


def test_304_without_a_cached_answer_is_an_error():
    with pytest.raises(UpdateError):
        check_freecad(FakeFetcher({FREECAD_API: answer(b"", status=304)}), etags={})


def test_etag_table_stays_small():
    etags = {f"https://example.org/{n}": {"etag": '"x"', "body": "", "saved_at": f"2026-01-01T00:00:{n % 60:02d}Z"}
             for n in range(updates.MAX_ETAGS)}
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release(), headers={"ETag": '"fresh"'})})
    check_freecad(fetch, etags=etags)
    assert len(etags) == updates.MAX_ETAGS and FREECAD_API in etags
    # a huge document is not remembered
    huge = freecad_release()
    huge["assets"] = [gh_asset(f"FreeCAD_1.1.4-x86_64-{n:05d}.AppImage") for n in range(1900)]
    etags = {}
    check_freecad(FakeFetcher({FREECAD_API: answer(huge, headers={"ETag": '"big"'})}), etags=etags)
    assert etags == {}


def rate_limited(status=403, **headers):
    body = {"message": "API rate limit exceeded for 203.0.113.7.", "documentation_url": "https://docs.github.com"}
    return answer(body, status=status, headers=headers)


def test_rate_limit_is_a_friendly_network_error():
    reset = str(int(time.time()) + 25 * 60)
    fetch = FakeFetcher({FREECAD_API: rate_limited(**{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})})
    with pytest.raises(NetworkError) as err:
        check_freecad(fetch)
    assert "try again in about 25 minutes" in str(err.value) or "try again in about 26 minutes" in str(err.value)
    assert "HTTP 403" in err.value.details and "rate limit" in err.value.details

    with pytest.raises(NetworkError, match="try again in about 1 minute\\."):
        check_freecad(FakeFetcher({FREECAD_API: rate_limited(429, **{"Retry-After": "30"})}))
    with pytest.raises(NetworkError, match="try again in about 2 minutes"):
        check_freecad(FakeFetcher({FREECAD_API: rate_limited(403, **{"retry-after": "61"})}))
    with pytest.raises(NetworkError, match="try again later"):
        check_freecad(FakeFetcher({FREECAD_API: rate_limited(429)}))
    with pytest.raises(NetworkError, match="try again later"):
        check_freecad(FakeFetcher({FREECAD_API: rate_limited(403, **{"x-ratelimit-remaining": "0"})}))
    with pytest.raises(NetworkError, match="try again later"):
        check_freecad(FakeFetcher({FREECAD_API: rate_limited(
            403, **{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 10 * 86400)})}))


def test_a_secondary_rate_limit_without_headers_is_a_network_error():
    """UPD-3: GitHub's secondary limit may answer 403 with neither rate-limit header."""
    body = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes "
                       "before you try again.", "documentation_url": "https://docs.github.com"}
    with pytest.raises(NetworkError, match="try again later"):
        check_freecad(FakeFetcher({FREECAD_API: answer(body, status=403)}))


def test_http_statuses_are_mapped():
    def error_for(status, **headers):
        with pytest.raises(EasyInstallerError) as err:
            check_freecad(FakeFetcher({FREECAD_API: answer({"message": "Not Found"}, status=status, headers=headers)}))
        return err.value

    assert type(error_for(404)) is UpdateError and "No update information" in str(error_for(404))
    assert "HTTP 404" in error_for(404).details
    assert type(error_for(410)) is UpdateError
    for status in (500, 502, 503, 504, 408):
        assert type(error_for(status)) is NetworkError and "try again later" in str(error_for(status))
    # 403 without rate-limit headers is not "try again later"
    assert type(error_for(403)) is UpdateError and "did not allow" in str(error_for(403))
    assert type(error_for(403, **{"x-ratelimit-remaining": "41"})) is UpdateError
    assert type(error_for(401)) is UpdateError
    assert type(error_for(400)) is UpdateError and type(error_for(302)) is UpdateError
    assert type(error_for(204)) is UpdateError


def test_fetchers_may_answer_with_their_own_objects():
    from types import SimpleNamespace

    body = json.dumps(freecad_release())
    loose = SimpleNamespace(status=200, headers={"ETag": '"x"'}, body=body)        # str body, no url
    etags: dict = {}
    assert check_freecad(FakeFetcher({FREECAD_API: loose}), etags=etags).version == "1.1.4"
    assert etags[FREECAD_API]["etag"] == '"x"'
    bare = SimpleNamespace(status=404, headers=None, body=None, url=None)
    with pytest.raises(UpdateError, match="No update information"):
        check_freecad(FakeFetcher({FREECAD_API: bare}))
    for junk in (None, "text", SimpleNamespace(status="x", headers={}, body=b""), SimpleNamespace(status=200)):
        with pytest.raises(UpdateError, match="could not be read"):
            check_freecad(FakeFetcher({FREECAD_API: lambda url, headers, junk=junk: junk}))


def test_fetch_errors_pass_through():
    boom = NetworkError("The internet could not be reached.", "details")
    with pytest.raises(NetworkError) as err:
        check_freecad(FakeFetcher({FREECAD_API: boom}))
    assert err.value is boom


@pytest.mark.parametrize("body", [
    b"", b"<html>login</html>", b"{", b"null", b"42", b'"text"', b"\xff\xfe", {"message": "hello"},
    {"tag_name": "1.1.4"}, {"tag_name": "1.1.4", "assets": "none"}, [{"tag_name": "x"}], [5],
    b"[" * 100000,
])
def test_unreadable_answers(body):
    with pytest.raises(UpdateError, match="could not be read"):
        check_freecad(FakeFetcher({FREECAD_API: answer(body)}))


def test_oversized_answer_is_refused():
    big = answer(freecad_release(), truncated=True)
    with pytest.raises(UpdateError, match="could not be read"):
        check_freecad(FakeFetcher({FREECAD_API: big}))
    fat = answer(b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(UpdateError, match="could not be read"):
        check_freecad(FakeFetcher({FREECAD_API: fat}))


def test_answer_from_an_insecure_address_is_refused():
    # a fetcher that followed a redirect to plain http
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release(), url="http://api.github.com/repos/x")})
    with pytest.raises(UpdateError, match="not secure"):
        check_freecad(fetch)


def test_hostile_release_fields_are_dropped():
    release = freecad_release()
    release["html_url"] = "javascript:alert(1)"
    release["published_at"] = "yesterday\nX: y"
    release["assets"].insert(0, gh_asset("FreeCAD_9.9.9-Linux-x86_64-py311.AppImage",
                                         browser_download_url="http://evil.example/FreeCAD.AppImage"))
    release["assets"].insert(0, gh_asset("FreeCAD_9.9.8-x86_64\n.AppImage"))
    update = check_freecad(FakeFetcher({FREECAD_API: answer(release)}))
    assert update.version == "1.1.4" and update.release_url is None and update.published_at is None
    # a size that is not a number is unknown, a wrong digest is no digest
    release = gh_release("1.1.4", [gh_asset("FreeCAD_1.1.4-x86_64.AppImage", size="big", digest="sha256:zz")])
    update = check_freecad(FakeFetcher({FREECAD_API: answer(release)}))
    assert update.size is None and update.sha256 is None


@pytest.mark.parametrize("source", [
    UpdateSource(kind="github-assets", owner="o", repo="r"),
    UpdateSource(kind="github-assets", owner="o/../..", repo="r", release="latest", pattern="*"),
    UpdateSource(kind="electron-generic", url="http://example.org/app"),
    UpdateSource(kind="zsync-url", url=None),
    UpdateSource(kind="bintray", owner="o", repo="r"),
    {"kind": "electron-github", "owner": "o", "repo": "r"},
    None,
])
def test_invalid_source_is_refused_without_a_request(source):
    fetch = FakeFetcher({})
    with pytest.raises(UpdateError, match="could not be read"):
        check(source, fetch, version="1.0")
    assert fetch.calls == []


# ------------------------------------------------------------------------------------------------
# check_for_update: electron
# ------------------------------------------------------------------------------------------------

def test_t3code_update_found():
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, url="https://release-assets.githubusercontent.com/x?sig=1")})
    update = check_t3(fetch)
    assert update == AvailableUpdate(
        version="0.0.44",
        url="https://github.com/pingdotgg/t3code/releases/latest/download/T3-Code-0.0.44-x86_64.AppImage",
        filename="T3-Code-0.0.44-x86_64.AppImage", size=158139658, sha256=None, sha512=T3_SHA512_HEX,
        release_url="https://github.com/pingdotgg/t3code/releases/latest",
        published_at="2026-09-29T20:28:15.246Z")
    assert fetch.urls == [T3_YML_URL]
    assert fetch.calls[0].max_bytes == 1024 * 1024


T3_BLOB_URL = ("https://release-assets.githubusercontent.com/github-production-release-asset/1153130349/"
               "5cf5000c-b8ef-4ca4-a072-3dea26530d4f?sp=r&sig=abc%3D&rscd=attachment%3B+filename%3Dlatest-linux.yml")


def test_t3code_update_is_pinned_to_the_release_github_redirects_to():
    # what http_get reports for the real request: github.com -> the tagged release -> the file store
    tagged = "https://github.com/pingdotgg/t3code/releases/download/v0.0.44/latest-linux.yml"
    etags: dict = {}
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, url=T3_BLOB_URL, redirects=(tagged, T3_BLOB_URL),
                                            headers={"etag": '"0x8DF1E6B0F3ABA66"'})})
    update = check_t3(fetch, etags=etags)
    assert update.url == "https://github.com/pingdotgg/t3code/releases/download/v0.0.44/T3-Code-0.0.44-x86_64.AppImage"
    assert update.release_url == "https://github.com/pingdotgg/t3code/releases/tag/v0.0.44"
    assert (update.version, update.sha512, update.size) == ("0.0.44", T3_SHA512_HEX, 158139658)
    # "not modified" still tells which release is the latest
    fetch = FakeFetcher({T3_YML_URL: answer(b"", status=304, url=T3_BLOB_URL, redirects=(tagged, T3_BLOB_URL))})
    assert check_t3(fetch, etags=etags) == update
    # GitHub writes owner and repository the way they are spelled today
    renamed = "https://github.com/PingDotGG/T3Code/releases/download/v0.0.44/latest-linux.yml"
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, redirects=(renamed,))})
    assert check_t3(fetch).url == "https://github.com/PingDotGG/T3Code/releases/download/v0.0.44/T3-Code-0.0.44-x86_64.AppImage"


@pytest.mark.parametrize("redirect", [
    "https://github.com/someone/else/releases/download/v9/latest-linux.yml",          # another repository
    "https://github.com/pingdotgg/t3code/releases/download/v0.0.44/other.yml",
    "https://github.com/pingdotgg/t3code/releases/download/a/b/latest-linux.yml",
    "https://github.com/pingdotgg/t3code/releases/download/../latest-linux.yml",
    "https://github.com/pingdotgg/t3code/releases/download//latest-linux.yml",
    "https://github.com/pingdotgg/t3code/releases/download/v1/latest-linux.yml?x=/latest-linux.yml",
    "https://github.com.evil.example/pingdotgg/t3code/releases/download/v1/latest-linux.yml",
    "http://github.com/pingdotgg/t3code/releases/download/v1/latest-linux.yml",
    "https://evil.example/latest-linux.yml",
    T3_BLOB_URL,
])
def test_t3code_update_is_only_pinned_to_its_own_repository(redirect):
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, redirects=(redirect,))})
    update = check_t3(fetch)
    assert update.url == "https://github.com/pingdotgg/t3code/releases/latest/download/T3-Code-0.0.44-x86_64.AppImage"
    assert update.release_url == "https://github.com/pingdotgg/t3code/releases/latest"


@pytest.mark.parametrize("version", ["0.0.44", "v0.0.44", "0.0.45", "0.1.0", "1.0"])
def test_t3code_up_to_date(version):
    assert check_t3(FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML)}), version=version) is None


def test_t3code_unknown_installed_version_uses_the_file_name():
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML)})
    assert check_t3(fetch, version=None).version == "0.0.44"
    assert check_t3(fetch, version=None, filename="T3-Code-0.0.44-x86_64.AppImage") is None
    assert check_t3(fetch, version=None, filename=None) is None


@pytest.mark.parametrize("arch, name", [
    ("x86_64", "latest-linux.yml"), ("aarch64", "latest-linux-arm64.yml"), ("armhf", "latest-linux-arm.yml"),
    ("i686", "latest-linux-ia32.yml"), ("unknown", "latest-linux.yml"),
])
def test_electron_update_file_per_cpu(arch, name):
    url = f"https://github.com/pingdotgg/t3code/releases/latest/download/{name}"
    yml = T3_LATEST_YML.replace("x86_64", {"aarch64": "arm64", "armhf": "armv7l", "i686": "i386"}.get(arch, "x86_64"))
    fetch = FakeFetcher({url: answer(yml)})
    update = check_t3(fetch, arch=arch)
    assert fetch.urls == [url] and update.version == "0.0.44"


def test_electron_file_for_another_cpu_is_not_offered():
    fetch = FakeFetcher({T3_YML_URL.replace("linux.yml", "linux-arm64.yml"): answer(T3_LATEST_YML)})
    assert check_t3(fetch, arch="aarch64") is None     # the yml only lists the x86_64 AppImage


def test_electron_etag_304():
    etags: dict = {}
    fetch = FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, headers={"ETag": '"0x8DE"'})})
    first = check_t3(fetch, etags=etags)
    assert etags[T3_YML_URL]["body"] == T3_LATEST_YML
    fetch = FakeFetcher({T3_YML_URL: answer(b"", status=304)})
    assert check_t3(fetch, etags=etags) == first
    assert fetch.calls[0].headers == {"If-None-Match": '"0x8DE"'}


def test_electron_prerelease_channel_goes_through_the_api():
    source = UpdateSource(kind="electron-github", owner="o", repo="r", prerelease=True)
    api = "https://api.github.com/repos/o/r/releases?per_page=20"
    yml_url = "https://github.com/o/r/releases/download/v2.0.0-beta.3/latest-linux.yml"
    releases = [
        gh_release("v2.0.0-beta.4", [gh_asset("App-2.0.0-beta.4.AppImage", owner="o", repo="r")], draft=True),
        gh_release("v2.0.0-mac", [gh_asset("latest-mac.yml", owner="o", repo="r", tag="v2.0.0-mac")]),
        gh_release("v2.0.0-beta.3", prerelease=True, owner="o", repo="r", assets=[
            gh_asset("latest-linux.yml", owner="o", repo="r", tag="v2.0.0-beta.3"),
            gh_asset("App-2.0.0-beta.3.AppImage", owner="o", repo="r", tag="v2.0.0-beta.3", digest="sha256:" + "0a" * 32),
        ]),
        gh_release("v1.9.0", [gh_asset("latest-linux.yml", owner="o", repo="r", tag="v1.9.0")]),
    ]
    yml = (f"version: 2.0.0-beta.3\nfiles:\n  - url: App-2.0.0-beta.3.AppImage\n    sha512: {T3_SHA512_B64}\n"
           "    size: 99\npath: App-2.0.0-beta.3.AppImage\n")
    fetch = FakeFetcher({api: answer(releases), yml_url: answer(yml)})
    update = check(source, fetch, version="1.9.0", filename="App-1.9.0.AppImage")
    assert fetch.urls == [api, yml_url]
    assert update == AvailableUpdate(
        version="2.0.0-beta.3", url="https://github.com/o/r/releases/download/v2.0.0-beta.3/App-2.0.0-beta.3.AppImage",
        filename="App-2.0.0-beta.3.AppImage", size=99, sha256="0a" * 32, sha512=T3_SHA512_HEX,
        release_url="https://github.com/o/r/releases/tag/v2.0.0-beta.3", published_at="2026-09-20T10:11:12Z")
    assert check(source, fetch, version="2.0.0", filename="App-2.0.0.AppImage") is None
    with pytest.raises(UpdateError, match="No update information"):
        check(source, FakeFetcher({api: answer(releases[:2])}), version="1.0")


def test_electron_generic_source():
    yml = (f"version: 3.1.0\nfiles:\n  - url: My App-3.1.0.AppImage\n    sha512: {T3_SHA512_B64}\n    size: 42\n"
           "releaseDate: 2026-09-01T00:00:00.000Z\n")
    for base in ("https://updates.example.org/app/linux", "https://updates.example.org/app/linux/"):
        source = UpdateSource(kind="electron-generic", url=base)
        yml_url = "https://updates.example.org/app/linux/latest-linux.yml"
        fetch = FakeFetcher({yml_url: answer(yml)})
        update = check(source, fetch, version="3.0.0", filename="My App-3.0.0.AppImage")
        assert fetch.urls == [yml_url]
        assert update.url == "https://updates.example.org/app/linux/My%20App-3.1.0.AppImage"
        assert update.filename == "My App-3.1.0.AppImage" and update.release_url is None
        assert update.size == 42 and update.published_at == "2026-09-01T00:00:00.000Z"


def test_electron_file_urls():
    source = UpdateSource(kind="electron-generic", url="https://updates.example.org/app")
    yml_url = "https://updates.example.org/app/latest-linux.yml"

    def url_for(file_url):
        yml = f"version: 2.0\nfiles:\n  - url: {file_url}\n    sha512: {T3_SHA512_B64}\n"
        return check(source, FakeFetcher({yml_url: answer(yml)}), version="1.0")

    assert url_for("https://cdn.example.net/dl/App-2.0.AppImage").url == "https://cdn.example.net/dl/App-2.0.AppImage"
    assert url_for("sub/App-2.0.AppImage").url == "https://updates.example.org/app/sub/App-2.0.AppImage"
    assert url_for("/root/App-2.0.AppImage").url == "https://updates.example.org/root/App-2.0.AppImage"
    update = url_for("App-2.0.AppImage?dl=1")
    assert update.url.endswith("/app/App-2.0.AppImage?dl=1") and update.filename == "App-2.0.AppImage"
    assert url_for("../App-2.0.AppImage").filename == "App-2.0.AppImage"
    for bad in ("http://cdn.example.net/App-2.0.AppImage", "ftp://cdn.example.net/App-2.0.AppImage",
                "//cdn.example.net/App-2.0.AppImage?x=http://", "file:///tmp/App-2.0.AppImage",
                "'C:\\App-2.0.AppImage'", "https://user:pw@cdn.example.net/App-2.0.AppImage"):
        if bad.startswith("//"):
            assert url_for(bad).url.startswith("https://cdn.example.net/")   # still https
            continue
        with pytest.raises(UpdateError, match="not secure"):
            url_for(bad)


def test_electron_legacy_and_partial_update_files():
    # old electron-builder: no files list
    legacy = f"version: 1.5.0\npath: App-1.5.0.AppImage\nsha512: {T3_SHA512_B64}\nsha2: {'AB' * 32}\n"
    update = check_t3(FakeFetcher({T3_YML_URL: answer(legacy)}))
    assert (update.version, update.filename, update.sha512, update.sha256, update.size) == (
        "1.5.0", "App-1.5.0.AppImage", T3_SHA512_HEX, "ab" * 32, None)
    # no checksum published at all
    bare = "version: 1.5.0\nfiles:\n  - url: App-1.5.0.AppImage\n"
    update = check_t3(FakeFetcher({T3_YML_URL: answer(bare)}))
    assert update.sha512 is None and update.size is None
    # no AppImage in the list (only .deb): nothing suitable
    only_deb = f"version: 1.5.0\nfiles:\n  - url: app_1.5.0_amd64.deb\n    sha512: {DEB_SHA512_B64}\n"
    assert check_t3(FakeFetcher({T3_YML_URL: answer(only_deb)})) is None
    assert check_t3(FakeFetcher({T3_YML_URL: answer("version: 1.5.0\nfiles:\n")})) is None
    # several AppImages: the one for this CPU, the first on a tie
    multi = ("version: 1.5.0\nfiles:\n  - url: App-1.5.0-arm64.AppImage\n  - url: App-1.5.0-x86_64.AppImage\n"
             "  - url: App-1.5.0-x86_64-lite.AppImage\n")
    assert check_t3(FakeFetcher({T3_YML_URL: answer(multi)}), filename=None).filename == "App-1.5.0-x86_64.AppImage"
    # a broken checksum of a file we do not use does not matter
    other = ("version: 1.5.0\nfiles:\n  - url: app.deb\n    sha512: broken!\n  - url: App-1.5.0.AppImage\n"
             f"    sha512: {T3_SHA512_B64}\n")
    assert check_t3(FakeFetcher({T3_YML_URL: answer(other)})).sha512 == T3_SHA512_HEX
    # the version may be quoted and carry a "v"
    quoted = "version: 'v1.5.0'\nfiles:\n  - url: App.AppImage\n"
    assert check_t3(FakeFetcher({T3_YML_URL: answer(quoted)})).version == "1.5.0"


@pytest.mark.parametrize("yml", [
    "",                                                                        # empty
    "files:\n  - url: App-2.0.AppImage\n",                                     # no version
    "version:\nfiles:\n  - url: App-2.0.AppImage\n",
    "version: 2.0 beta\nfiles:\n  - url: App-2.0.AppImage\n",
    'version: "2.0\\n"\nfiles:\n  - url: App-2.0.AppImage\n',
    "version: 2.0\nfiles: App-2.0.AppImage\n",                                 # files is not a list
    "version: 2.0\nfiles:\n  url: App-2.0.AppImage\n",
    "version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    sha512: not-base64!\n",          # bad base64
    "version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    sha512: Zm9v\n",                 # too short
    f"version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    sha512: {T3_SHA512_B64[:-3]}\n",
    "version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    sha2: xyz\n",
    "version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    size: big\n",
    "version: 2.0\nfiles:\n  - url: App-2.0.AppImage\n    size: -1\n",
    "version: 2.0\nfiles: [{url: App-2.0.AppImage}]\n",                        # weird YAML
    "version: &v 2.0\nfiles:\n  - url: App-2.0.AppImage\n",
    "version: 2.0\nversion: 3.0\nfiles:\n  - url: App-2.0.AppImage\n",
    "version: 2.0\nfiles:\n\t- url: App-2.0.AppImage\n",
    "<!DOCTYPE html><html><body>Sign in</body></html>",
    "{\"version\": \"2.0\"}",
    b"\xff\xfeversion: 2.0",
    "version: 2.0\n" + "x" * 2_000_000,
])
def test_electron_unreadable_update_files(yml):
    with pytest.raises(UpdateError, match="could not be read"):
        check_t3(FakeFetcher({T3_YML_URL: answer(yml)}))


def test_electron_statuses():
    with pytest.raises(UpdateError, match="No update information"):
        check_t3(FakeFetcher({T3_YML_URL: answer(b"Not Found", status=404)}))
    with pytest.raises(NetworkError, match="try again later"):
        check_t3(FakeFetcher({T3_YML_URL: answer(b"", status=503)}))
    with pytest.raises(NetworkError, match="try again"):
        check_t3(FakeFetcher({T3_YML_URL: answer(b"", status=429, headers={"Retry-After": "120"})}))
    with pytest.raises(UpdateError, match="not secure"):
        check_t3(FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, url="http://mirror.example.org/latest-linux.yml")}))
    with pytest.raises(UpdateError, match="could not be read"):
        check_t3(FakeFetcher({T3_YML_URL: answer(T3_LATEST_YML, truncated=True)}))


# ------------------------------------------------------------------------------------------------
# check_for_update: zsync-url
# ------------------------------------------------------------------------------------------------

ZSYNC_URL = "https://download.example.org/apps/App-latest-x86_64.AppImage.zsync"
ZSYNC = UpdateSource(kind="zsync-url", url=ZSYNC_URL, via="upd_info")


def zsync_file(filename="App-latest-x86_64.AppImage", url="App-latest-x86_64.AppImage", length=5000,
               sha1="ab" * 20, extra="") -> bytes:
    head = (f"zsync: 0.6.2\nFilename: {filename}\nMTime: Tue, 29 Sep 2026 20:28:15 +0000\nBlocksize: 2048\n"
            f"Length: {length}\nHash-Lengths: 2,2,5\nURL: {url}\nSHA-1: {sha1}\n{extra}\n")
    return head.encode() + bytes(range(256)) * 40 + b"\n\nnot: a header\n"


def test_zsync_reads_only_the_header():
    fetch = FakeFetcher({ZSYNC_URL: answer(zsync_file(), status=206)})
    update = check(ZSYNC, fetch, filename="App-latest-x86_64.AppImage", current_size=4000)
    assert update == AvailableUpdate(
        version=None, url="https://download.example.org/apps/App-latest-x86_64.AppImage",
        filename="App-latest-x86_64.AppImage", size=5000, sha256=None, sha512=None, release_url=None,
        published_at="2026-09-29T20:28:15Z", sha1="ab" * 20)
    call = fetch.calls[0]
    assert call.max_bytes == 64 * 1024 and call.headers["Range"] == "bytes=0-65535"


def test_zsync_newer_by_sha1_then_length_then_name():
    fetch = FakeFetcher({ZSYNC_URL: answer(zsync_file())})
    name = "App-latest-x86_64.AppImage"
    assert check(ZSYNC, fetch, filename=name, current_sha1="AB" * 20, current_size=1) is None
    assert check(ZSYNC, fetch, filename=name, current_sha1="cd" * 20, current_size=5000) is not None
    assert check(ZSYNC, fetch, filename="renamed.AppImage", current_size=5000) is None
    assert check(ZSYNC, fetch, filename=name, current_size=4999) is not None
    # the size of the installed file is unknown: only the name is left
    assert check(ZSYNC, fetch, filename=name) is None
    assert check(ZSYNC, fetch, filename="App-build-7.AppImage") is not None
    assert check(ZSYNC, fetch, filename=None) is None
    # the sha256 of the installed file says nothing about a .zsync source
    assert check(ZSYNC, fetch, filename=name, sha256="12" * 32) is None


def test_zsync_with_versions():
    body = zsync_file(filename="App-2.0-x86_64.AppImage", url="https://cdn.example.net/App-2.0-x86_64.AppImage")
    fetch = FakeFetcher({ZSYNC_URL: answer(body)})
    update = check(ZSYNC, fetch, version="1.9", current_size=5000)
    assert update.version == "2.0" and update.url == "https://cdn.example.net/App-2.0-x86_64.AppImage"
    assert check(ZSYNC, fetch, version="2.0", current_size=1, filename="x.AppImage") is None
    assert check(ZSYNC, fetch, version="2.1") is None


def test_zsync_whole_file_or_cut_off_answers_work():
    whole = answer(zsync_file() + b"\x00" * 200_000, status=200, truncated=True)
    assert check(ZSYNC, FakeFetcher({ZSYNC_URL: whole}), filename="old.AppImage") is not None
    crlf = answer(zsync_file().replace(b"\n", b"\r\n"))
    assert check(ZSYNC, FakeFetcher({ZSYNC_URL: crlf}), filename="old.AppImage").size == 5000
    # no usable Length / SHA-1 / MTime
    odd = zsync_file(length="many", sha1="xyz").replace(b"MTime: Tue, 29 Sep 2026 20:28:15 +0000", b"MTime: soon")
    update = check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(odd)}), filename="old.AppImage")
    assert (update.size, update.sha1, update.published_at) == (None, None, None)
    # the Filename is not an AppImage name, but the URL is
    named = zsync_file(filename="download", url="files/App-3.0.AppImage")
    update = check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(named)}), version="1.0")
    assert update.filename == "App-3.0.AppImage" and update.version == "3.0"
    assert update.url == "https://download.example.org/apps/files/App-3.0.AppImage"
    # several URL lines: the first
    twice = zsync_file(extra="URL: https://mirror.example.net/other.AppImage\n")
    assert check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(twice)}), filename="old.AppImage").url.endswith(
        "/apps/App-latest-x86_64.AppImage")


def test_zsync_for_another_cpu():
    body = zsync_file(filename="App-latest-aarch64.AppImage", url="App-latest-aarch64.AppImage")
    assert check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(body)}), filename="old.AppImage") is None
    assert check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(body)}), filename="old.AppImage", arch="aarch64") is not None


@pytest.mark.parametrize("body, message", [
    (b"", "could not be read"),
    (b"<html>not a zsync file</html>\n\n", "could not be read"),
    (b"zsync: 0.6.2\nFilename: App.AppImage\nURL: App.AppImage\n", "could not be read"),      # no end of header
    (b"zsync: 0.6.2\ngarbage line\n\n", "could not be read"),
    (b"zsync: 0.6.2\nLength: 5\n\n", "could not be read"),                                     # no URL, no name
    (b"zsync: 0.6.2\nFilename: \xff\xfe.AppImage\nURL: a.AppImage\n\n", "could not be read"),
    (zsync_file(filename="App.tar.gz", url="App.tar.gz"), "could not be read"),                # not an AppImage
    (zsync_file(url="http://download.example.org/App.AppImage"), "not secure"),
    (zsync_file(url="ftp://download.example.org/App.AppImage"), "not secure"),
    (b"x" * 70000, "could not be read"),
])
def test_zsync_unreadable(body, message):
    with pytest.raises(UpdateError, match=message):
        check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(body)}), filename="old.AppImage")


def test_zsync_etag_and_statuses():
    etags: dict = {}
    fetch = FakeFetcher({ZSYNC_URL: answer(zsync_file(), status=206, headers={"ETag": '"z1"'})})
    first = check(ZSYNC, fetch, filename="old.AppImage", etags=etags)
    assert etags[ZSYNC_URL]["body"].startswith("zsync: 0.6.2\n") and len(etags[ZSYNC_URL]["body"]) < 400
    json.dumps(etags)
    fetch = FakeFetcher({ZSYNC_URL: answer(b"", status=304)})
    assert check(ZSYNC, fetch, filename="old.AppImage", etags=etags) == first
    assert fetch.calls[0].headers["If-None-Match"] == '"z1"'
    with pytest.raises(UpdateError, match="No update information"):
        check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(b"", status=404)}), filename="old.AppImage")
    with pytest.raises(NetworkError):
        check(ZSYNC, FakeFetcher({ZSYNC_URL: answer(b"", status=500)}), filename="old.AppImage")


# ------------------------------------------------------------------------------------------------
# loopback web server
# ------------------------------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        server: _Server = self.server   # type: ignore[assignment]
        path = self.path.split("?", 1)[0]
        server.seen.append((self.path, {key.lower(): value for key, value in self.headers.items()}))
        route = server.routes.get(path)
        try:
            if route is None:
                self.reply(404, b"no such page")
            else:
                route(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, status: int, body: bytes = b"", headers: dict | None = None, *, length: bool = True) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if length and not any(key.lower() == "content-length" for key in (headers or {})):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes: dict = {}
        self.seen: list[tuple[str, dict]] = []
        self.release = threading.Event()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"

    def serve(self, path: str, body: bytes = b"", status: int = 200, headers: dict | None = None, **kw) -> str:
        self.routes[path] = lambda handler: handler.reply(status, body, headers, **kw)
        return self.url(path)

    @property
    def paths(self) -> list[str]:
        return [path for path, _headers in self.seen]


@pytest.fixture
def web():
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ------------------------------------------------------------------------------------------------
# http_get
# ------------------------------------------------------------------------------------------------

def test_http_get_returns_body_headers_and_sends_user_agent(web):
    url = web.serve("/doc", b'{"ok": true}', headers={"ETag": '"abc"', "X-Thing": "1"})
    response = http_get(url, headers={"Accept": "application/json"}, allow_insecure_localhost=True)
    assert response == HttpResponse(status=200, headers=response.headers, body=b'{"ok": true}', url=url)
    assert response.headers["etag"] == '"abc"' and response.header("ETag") == '"abc"'
    assert response.header("x-THING") == "1" and response.header("missing") is None
    assert not response.truncated
    path, headers = web.seen[0]
    assert headers["user-agent"] == f"EasyInstaller/{__version__}"
    assert headers["accept"] == "application/json"
    assert "authorization" not in headers and "cookie" not in headers


def test_http_get_caps_the_body(web):
    url = web.serve("/big", b"x" * 100_000)
    response = http_get(url, max_bytes=1000, allow_insecure_localhost=True)
    assert response.body == b"x" * 1000 and response.truncated
    exact = http_get(url, max_bytes=100_000, allow_insecure_localhost=True)
    assert len(exact.body) == 100_000 and not exact.truncated
    assert http_get(url, max_bytes=0, allow_insecure_localhost=True).body == b""


def test_http_get_returns_error_statuses(web):
    web.serve("/gone", b"nope", status=404)
    web.serve("/limit", b'{"message": "rate limit"}', status=403,
              headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"})
    web.serve("/same", b"", status=304, headers={"ETag": '"abc"'})
    web.serve("/down", b"oops", status=503)
    gone = http_get(web.url("/gone"), allow_insecure_localhost=True)
    assert (gone.status, gone.body) == (404, b"nope")
    limit = http_get(web.url("/limit"), allow_insecure_localhost=True)
    assert limit.status == 403 and limit.header("x-ratelimit-remaining") == "0"
    same = http_get(web.url("/same"), headers={"If-None-Match": '"abc"'}, allow_insecure_localhost=True)
    assert (same.status, same.body) == (304, b"") and web.seen[-1][1]["if-none-match"] == '"abc"'
    assert http_get(web.url("/down"), allow_insecure_localhost=True).status == 503
    assert http_get(web.url("/unknown"), allow_insecure_localhost=True).status == 404


def test_http_get_follows_redirects_on_the_same_kind_of_address(web):
    target = web.serve("/final", b"arrived")
    web.serve("/hop", status=302, headers={"Location": "/final"})
    web.serve("/start", status=301, headers={"Location": web.url("/hop")})
    response = http_get(web.url("/start"), allow_insecure_localhost=True)
    assert (response.status, response.body, response.url) == (200, b"arrived", target)
    assert response.redirects == (web.url("/hop"), target)
    assert web.paths == ["/start", "/hop", "/final"]
    assert http_get(target, allow_insecure_localhost=True).redirects == ()


@pytest.mark.parametrize("location", [
    "http://example.invalid/latest-linux.yml",        # plain http on another host
    "http://192.0.2.1/x",
    "ftp://127.0.0.1/x",
    "file:///etc/passwd",
    "http://user:pw@127.0.0.1/x",
])
def test_http_get_refuses_insecure_redirects(web, location):
    web.serve("/start", status=302, headers={"Location": location})
    with pytest.raises(UpdateError, match="not secure") as err:
        http_get(web.url("/start"), allow_insecure_localhost=True)
    assert "redirect" in err.value.details
    assert web.paths == ["/start"]


def test_http_get_follows_redirects_with_unencoded_characters(web):
    web.serve("/files/My%20App.AppImage", b"found")
    web.serve("/start", status=302, headers={"Location": "/files/My App.AppImage"})
    assert http_get(web.url("/start"), allow_insecure_localhost=True).body == b"found"
    web.serve("/relative", status=307, headers={"Location": "files/My%20App.AppImage"})
    assert http_get(web.url("/relative"), allow_insecure_localhost=True).body == b"found"
    web.serve("/nowhere", status=302)               # a redirect without Location is just an answer
    assert http_get(web.url("/nowhere"), allow_insecure_localhost=True).status == 302


def test_http_get_stops_redirect_loops(web):
    web.serve("/loop", status=302, headers={"Location": "/loop"})
    response = http_get(web.url("/loop"), allow_insecure_localhost=True)
    assert response.status == 302 and len(web.seen) <= 12


@pytest.mark.parametrize("url", [
    "http://example.invalid/x", "ftp://example.invalid/x", "file:///etc/passwd", "data:text/plain,x",
    "https://user:pw@example.invalid/x", "example.invalid/x", "", "https://example.invalid/a b",
])
def test_http_get_refuses_insecure_addresses_without_connecting(url, monkeypatch):
    monkeypatch.setattr(updates, "_build_opener", lambda *a: pytest.fail("a connection was attempted"))
    for allow in (False, True):
        with pytest.raises(UpdateError, match="not secure"):
            http_get(url, allow_insecure_localhost=allow)


def test_http_get_needs_the_flag_for_localhost(web, monkeypatch):
    url = web.serve("/doc", b"x")
    with pytest.raises(UpdateError, match="not secure"):
        http_get(url)
    assert web.seen == []


def test_http_get_network_errors(web):
    with pytest.raises(NetworkError, match="internet could not be reached") as err:
        http_get(f"http://127.0.0.1:{closed_port()}/x", allow_insecure_localhost=True)
    assert "ConnectionRefusedError" in err.value.details

    web.routes["/slow"] = lambda handler: web.release.wait(10)
    with pytest.raises(NetworkError, match="did not answer in time"):
        http_get(web.url("/slow"), timeout=0.3, allow_insecure_localhost=True)

    def slow_body(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        handler.wfile.write(b"x" * 10)
        handler.wfile.flush()
        web.release.wait(10)

    web.routes["/stall"] = slow_body
    with pytest.raises(NetworkError, match="did not answer in time"):
        http_get(web.url("/stall"), timeout=0.3, allow_insecure_localhost=True)

    web.routes["/garbage"] = lambda handler: handler.wfile.write(b"this is not http\r\n\r\n")
    with pytest.raises(NetworkError):
        http_get(web.url("/garbage"), allow_insecure_localhost=True)


def test_http_get_overall_deadline(web, monkeypatch):
    def drip(handler):
        handler.send_response(200)
        handler.end_headers()
        for _n in range(200):
            handler.wfile.write(b"x")
            handler.wfile.flush()
            if web.release.wait(0.05):
                return

    web.routes["/drip"] = drip
    monkeypatch.setattr(updates, "METADATA_DEADLINE", 0.3)
    start = time.monotonic()
    with pytest.raises(NetworkError, match="did not answer in time"):
        http_get(web.url("/drip"), timeout=0.2, allow_insecure_localhost=True)
    assert time.monotonic() - start < 5


def test_http_get_ignores_proxies_for_localhost(web, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed_port()}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert http_get(web.serve("/doc", b"direct"), allow_insecure_localhost=True).body == b"direct"


@pytest.fixture
def tls_web(tmp_path):
    """An https server on 127.0.0.1 with a self-signed certificate."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    done = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
         "-keyout", str(key), "-out", str(cert)], capture_output=True, timeout=60, check=False)
    if done.returncode != 0:
        pytest.skip("openssl could not create a test certificate")
    server = _Server()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_http_get_verifies_certificates(tls_web):
    tls_web.serve("/doc", b"secret")
    url = f"https://127.0.0.1:{tls_web.server_address[1]}/doc"
    with pytest.raises(NetworkError, match="secure connection to 127.0.0.1 could not be set up") as err:
        http_get(url)
    assert "certificate" in err.value.details.lower()
    update = AvailableUpdate(version="2.0", url=url.replace("/doc", "/App.AppImage"), filename="App.AppImage")
    with pytest.raises(NetworkError, match="secure connection"):
        download_update(update, Path(os.environ["HOME"]) / "dl")


def test_check_for_update_uses_http_get_by_default(monkeypatch):
    seen = []

    def fake_http_get(url, *, headers=None, max_bytes=None):
        seen.append(url)
        return answer(T3_LATEST_YML, url=url)

    monkeypatch.setattr(updates, "http_get", fake_http_get)
    assert check_for_update(T3, current_version="0.0.42", current_sha256=None, current_filename=T3_INSTALLED,
                            arch="x86_64").version == "0.0.44"
    assert seen == [T3_YML_URL]


# ------------------------------------------------------------------------------------------------
# download_update
# ------------------------------------------------------------------------------------------------

PAYLOAD = (b"\x7fELF" + bytes(range(256)) * 1200)[:300_000]


def local_update(url: str, body: bytes = PAYLOAD, **kw) -> AvailableUpdate:
    data = dict(version="2.0", url=url, filename="App-2.0-x86_64.AppImage", size=len(body),
                sha256=hashlib.sha256(body).hexdigest(), sha512=hashlib.sha512(body).hexdigest())
    data.update(kw)
    return AvailableUpdate(**data)


def download(update, dest, **kw) -> Path:
    return download_update(update, dest, allow_insecure_localhost=True, **kw)


def assert_nothing_left(dest: Path) -> None:
    assert not dest.exists() or list(dest.iterdir()) == []


def test_download_success(web, tmp_path):
    url = web.serve("/files/App-2.0-x86_64.AppImage", PAYLOAD)
    dest = tmp_path / "cache" / "downloads" / "app"
    reports: list[tuple[float | None, str]] = []
    path = download(local_update(url), dest, progress=lambda fraction, text: reports.append((fraction, text)))
    assert path == dest / "App-2.0-x86_64.AppImage" and path.read_bytes() == PAYLOAD
    assert [p.name for p in dest.iterdir()] == ["App-2.0-x86_64.AppImage"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert reports[0] == (None, "Starting the download…") and reports[-1][0] == 1.0
    fractions = [fraction for fraction, _text in reports[1:]]
    assert all(0 < f <= 1 for f in fractions) and fractions == sorted(fractions)
    total = locale.format_string("%.1f", 0.3)      # the decimal separator follows the user's locale
    assert all(text.startswith("Downloading… ") and text.endswith(f" of {total} MB") for _f, text in reports[1:])
    headers = web.seen[0][1]
    assert headers["user-agent"] == f"EasyInstaller/{__version__}" and "range" not in headers


def test_download_checks_every_published_digest(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    good = dict(sha256=hashlib.sha256(PAYLOAD).hexdigest(), sha512=hashlib.sha512(PAYLOAD).hexdigest(),
                sha1=hashlib.sha1(PAYLOAD).hexdigest())
    assert download(local_update(url, **{k: v.upper() for k, v in good.items()}), tmp_path / "ok").exists()
    for name, wrong in (("sha256", "00" * 32), ("sha512", "00" * 64), ("sha1", "00" * 20),
                        ("sha256", "not hex"), ("sha512", good["sha256"])):
        dest = tmp_path / f"bad-{name}-{len(wrong)}"
        with pytest.raises(UpdateError, match="incomplete or was changed") as err:
            download(local_update(url, **{**good, name: wrong}), dest)
        assert name in err.value.details
        assert_nothing_left(dest)
    # nothing published: only the size is checked
    plain = local_update(url, sha256=None, sha512=None)
    assert download(plain, tmp_path / "plain").read_bytes() == PAYLOAD


def test_download_size_mismatch(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    for size in (len(PAYLOAD) + 1, len(PAYLOAD) - 1, 0):
        dest = tmp_path / f"size-{size}"
        with pytest.raises(UpdateError, match="incomplete or was changed") as err:
            download(local_update(url, size=size, sha256=None, sha512=None), dest)
        assert "expected" in err.value.details
        assert_nothing_left(dest)
    # a server that does not announce the length: too much and too little are both noticed
    stream = web.serve("/stream.AppImage", PAYLOAD, length=False)
    for size in (len(PAYLOAD) - 5000, len(PAYLOAD) + 5000):
        dest = tmp_path / f"stream-{size}"
        with pytest.raises(UpdateError, match="incomplete or was changed"):
            download(local_update(stream, size=size, sha256=None, sha512=None), dest)
        assert_nothing_left(dest)
    assert download(local_update(stream), tmp_path / "stream-ok").read_bytes() == PAYLOAD


def test_download_unknown_size_uses_the_servers(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    reports = []
    path = download(local_update(url, size=None), tmp_path / "a", progress=lambda f, t: reports.append((f, t)))
    assert path.read_bytes() == PAYLOAD and reports[-1][0] == 1.0 and reports[1][0] is not None
    stream = web.serve("/stream.AppImage", PAYLOAD, length=False)
    reports.clear()
    path = download(local_update(stream, size=None), tmp_path / "b", progress=lambda f, t: reports.append((f, t)))
    assert path.read_bytes() == PAYLOAD
    assert reports[1][0] is None and " of " not in reports[1][1] and reports[-1][0] == 1.0


def test_download_empty_answer_is_not_an_update(web, tmp_path):
    url = web.serve("/App.AppImage", b"")
    for update in (local_update(url, size=None, sha256=None, sha512=None),
                   local_update(url, b""), local_update(web.serve("/stream.AppImage", b"", length=False), size=None)):
        dest = tmp_path / "dl"
        with pytest.raises(UpdateError, match="incomplete or was changed"):
            download(update, dest)
        assert_nothing_left(dest)


def test_download_interrupted_connection(web, tmp_path):
    def cut(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(PAYLOAD)))
        handler.end_headers()
        handler.wfile.write(PAYLOAD[:100_000])

    web.routes["/cut.AppImage"] = cut
    for update in (local_update(web.url("/cut.AppImage")),
                   local_update(web.url("/cut.AppImage"), size=None, sha256=None, sha512=None)):
        dest = tmp_path / f"cut-{update.size}"
        with pytest.raises(NetworkError, match="interrupted") as err:
            download(update, dest)
        assert "100000 of 300000" in err.value.details
        assert_nothing_left(dest)


def test_download_cancel_mid_way(web, tmp_path):
    sent = []

    def slow(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(PAYLOAD)))
        handler.end_headers()
        for start in range(0, len(PAYLOAD), 10_000):
            handler.wfile.write(PAYLOAD[start:start + 10_000])
            handler.wfile.flush()
            sent.append(start)
            if web.release.wait(0.05):
                return

    web.routes["/slow.AppImage"] = slow
    cancel = threading.Event()
    seen = []

    def progress(fraction, text):
        seen.append(fraction)
        if fraction:
            cancel.set()

    dest = tmp_path / "dl"
    with pytest.raises(UpdateCancelled, match="cancelled"):
        download(local_update(web.url("/slow.AppImage")), dest, progress=progress, cancel=cancel)
    assert_nothing_left(dest)
    assert seen[-1] is not None and seen[-1] < 1.0
    assert len(sent) < len(PAYLOAD) // 10_000      # the rest was never sent


def test_download_cancel_while_the_server_stalls_is_quick(web, tmp_path):
    """GUI-3 / CLI-3: the server sends a little, then nothing; Cancel must not wait for the
    read timeout (DOWNLOAD_TIMEOUT, 30 s)."""
    def stall(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(PAYLOAD)))
        handler.end_headers()
        handler.wfile.write(PAYLOAD[:1000])
        handler.wfile.flush()
        web.release.wait(30)

    web.routes["/stall.AppImage"] = stall
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    dest = tmp_path / "dl"
    started = time.monotonic()
    with pytest.raises(UpdateCancelled):
        download(local_update(web.url("/stall.AppImage")), dest, cancel=cancel)
    assert time.monotonic() - started < 5
    assert_nothing_left(dest)


def test_download_cancel_while_connecting_is_quick(tmp_path):
    """A server that accepts the connection but never answers (or a name lookup that hangs)."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        url = f"http://127.0.0.1:{server.getsockname()[1]}/App.AppImage"
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(UpdateCancelled):
            download(local_update(url), tmp_path / "dl", cancel=cancel)
        assert time.monotonic() - started < 5
    assert not list((tmp_path / "dl").glob("*"))


def test_download_cancelled_before_it_starts(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UpdateCancelled):
        download(local_update(url), tmp_path / "dl", cancel=cancel)
    assert web.seen == [] and not (tmp_path / "dl").exists()


def test_download_disk_write_error(web, tmp_path, monkeypatch):
    url = web.serve("/App.AppImage", PAYLOAD)
    dest = tmp_path / "dl"
    real_write = updates._write_chunk
    calls = []

    def full_disk(handle, chunk, path):
        calls.append(len(chunk))
        if len(calls) > 1:
            try:
                raise OSError(28, "No space left on device")
            except OSError as exc:
                raise updates._save_error(exc, path) from exc
        real_write(handle, chunk, path)

    monkeypatch.setattr(updates, "_write_chunk", full_disk)
    monkeypatch.setattr(updates, "CHUNK_SIZE", 50_000)
    with pytest.raises(UpdateError, match="not enough free space"):
        download(local_update(url), dest)
    assert len(calls) == 2
    assert_nothing_left(dest)


class _BrokenFile:
    """A file object whose writes fail like a broken disk."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()

    def write(self, data):
        raise OSError(5, "Input/output error")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_download_write_errors_are_update_errors(web, tmp_path, monkeypatch):
    url = web.serve("/App.AppImage", PAYLOAD)
    real_open_part = updates._open_part
    monkeypatch.setattr(updates, "_open_part", lambda part: _BrokenFile(real_open_part(part)))
    dest = tmp_path / "dl"
    with pytest.raises(UpdateError, match="could not be saved") as err:
        download(local_update(url), dest)
    assert "Input/output error" in err.value.details
    assert_nothing_left(dest)


def test_download_fsync_and_rename_errors(web, tmp_path, monkeypatch):
    url = web.serve("/App.AppImage", PAYLOAD)
    dest = tmp_path / "dl"

    def failing(*args, **kw):
        raise OSError(122, "Disk quota exceeded")

    with monkeypatch.context() as patch:
        patch.setattr(updates.os, "fsync", failing)
        with pytest.raises(UpdateError, match="not enough free space"):
            download(local_update(url), dest)
    assert_nothing_left(dest)
    with monkeypatch.context() as patch:
        patch.setattr(updates.os, "replace", failing)
        with pytest.raises(UpdateError, match="not enough free space"):
            download(local_update(url), dest)
    assert_nothing_left(dest)
    # the final name is taken by a folder
    (dest / "App-2.0-x86_64.AppImage").mkdir(parents=True)
    with pytest.raises(UpdateError, match="could not be saved"):
        download(local_update(url), dest)
    assert [p.name for p in dest.iterdir()] == ["App-2.0-x86_64.AppImage"]


def test_download_unusable_destination(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(UpdateError, match="could not be saved"):
        download(local_update(url), blocker / "sub")
    with pytest.raises(UpdateError, match="could not be saved"):
        download(local_update(url), blocker)
    assert blocker.read_text() == "x" and web.seen == []
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            with pytest.raises(UpdateError, match="could not be saved"):
                download(local_update(url), locked)
        finally:
            locked.chmod(0o700)


def test_download_not_enough_space(web, tmp_path, monkeypatch):
    url = web.serve("/App.AppImage", PAYLOAD)
    monkeypatch.setattr(updates.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**9, 10**9 - 5, 5))
    dest = tmp_path / "dl"
    with pytest.raises(UpdateError, match="not enough free space"):
        download(local_update(url), dest)
    assert_nothing_left(dest)


def test_download_replaces_leftovers(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    dest = tmp_path / "dl"
    dest.mkdir()
    (dest / "App-2.0-x86_64.AppImage").write_bytes(b"old download")
    (dest / "App-2.0-x86_64.AppImage.part").write_bytes(b"stale partial file")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    path = download(local_update(url), dest)
    assert path.read_bytes() == PAYLOAD and sorted(p.name for p in dest.iterdir()) == ["App-2.0-x86_64.AppImage"]
    # a .part that is a symlink is removed, never written through
    (dest / "App-2.0-x86_64.AppImage.part").symlink_to(outside)
    assert download(local_update(url), dest).read_bytes() == PAYLOAD
    assert outside.read_text() == "keep me" and not (dest / "App-2.0-x86_64.AppImage.part").is_symlink()


def test_download_redirects(web, tmp_path):
    web.serve("/files/real.AppImage", PAYLOAD)
    url = web.serve("/latest/download/App.AppImage", status=302, headers={"Location": "/files/real.AppImage"})
    path = download(local_update(url), tmp_path / "dl")
    assert path.name == "App-2.0-x86_64.AppImage" and path.read_bytes() == PAYLOAD
    assert web.paths == ["/latest/download/App.AppImage", "/files/real.AppImage"]

    for location in ("http://example.invalid/App.AppImage", "ftp://127.0.0.1/App.AppImage", "file:///etc/passwd"):
        bad = web.serve("/bad/App.AppImage", status=302, headers={"Location": location})
        dest = tmp_path / "refused"
        with pytest.raises(UpdateError, match="not secure"):
            download(local_update(bad), dest)
        assert_nothing_left(dest)


def test_download_http_errors(web, tmp_path):
    dest = tmp_path / "dl"

    def fails(status, headers=None):
        url = web.serve("/App.AppImage", b"<html>error page</html>", status=status, headers=headers)
        with pytest.raises(EasyInstallerError) as err:
            download(local_update(url), dest)
        assert_nothing_left(dest)
        assert f"HTTP {status}" in err.value.details
        return err.value

    assert type(fails(404)) is UpdateError and "no longer on the server" in str(fails(404))
    assert type(fails(500)) is NetworkError and type(fails(503)) is NetworkError
    assert type(fails(429)) is NetworkError and "try again later" in str(fails(429))
    assert type(fails(403, {"Retry-After": "60"})) is NetworkError
    assert type(fails(403)) is UpdateError and type(fails(401)) is UpdateError
    assert type(fails(206)) is UpdateError      # a partial answer is never accepted as the file
    with pytest.raises(NetworkError, match="internet could not be reached"):
        download(local_update(f"http://127.0.0.1:{closed_port()}/App.AppImage"), dest)
    assert_nothing_left(dest)


def test_download_timeout(web, tmp_path, monkeypatch):
    def stall(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(PAYLOAD)))
        handler.end_headers()
        handler.wfile.write(PAYLOAD[:1000])
        handler.wfile.flush()
        web.release.wait(10)

    web.routes["/stall.AppImage"] = stall
    monkeypatch.setattr(updates, "DOWNLOAD_TIMEOUT", 0.3)
    dest = tmp_path / "dl"
    with pytest.raises(NetworkError, match="did not answer in time"):
        download(local_update(web.url("/stall.AppImage")), dest)
    assert_nothing_left(dest)


@pytest.mark.parametrize("filename", [
    "App-2.0.deb", "App-2.0.AppImage.zsync", "setup.exe", "", ".AppImage", "App.AppImage/", "latest-linux.yml",
])
def test_download_refuses_other_file_types_without_a_request(web, tmp_path, filename):
    url = web.serve("/App.AppImage", PAYLOAD)
    with pytest.raises(UpdateError, match="not an AppImage"):
        download(local_update(url, filename=filename), tmp_path / "dl")
    assert web.seen == [] and not (tmp_path / "dl").exists()


@pytest.mark.parametrize("filename, stored", [
    ("../../outside/App-2.0.AppImage", "App-2.0.AppImage"),
    ("/etc/cron.d/App.AppImage", "App.AppImage"),
    ("..\\..\\App.AppImage", "App.AppImage"),
    ("My App (2.0) x86_64.AppImage", "My_App__2.0__x86_64.AppImage"),
    (".hidden.AppImage", "hidden.AppImage"),
    ("App\n.AppImage", "App_.AppImage"),
])
def test_download_sanitises_the_file_name(web, tmp_path, filename, stored):
    url = web.serve("/App.AppImage", PAYLOAD)
    dest = tmp_path / "a" / "dl"
    path = download(local_update(url, filename=filename), dest)
    assert path == dest / stored and path.read_bytes() == PAYLOAD
    assert [p.name for p in dest.iterdir()] == [stored]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "home"]


@pytest.mark.parametrize("url", [
    "http://example.invalid/App.AppImage", "ftp://example.invalid/App.AppImage", "file:///tmp/App.AppImage",
    "https://user:pw@example.invalid/App.AppImage", "App.AppImage", "",
])
def test_download_refuses_insecure_addresses(tmp_path, url, monkeypatch):
    monkeypatch.setattr(updates, "_build_opener", lambda *a: pytest.fail("a connection was attempted"))
    for allow in (False, True):
        with pytest.raises(UpdateError, match="not secure"):
            download_update(local_update(url), tmp_path / "dl", allow_insecure_localhost=allow)
    assert not (tmp_path / "dl").exists()


def test_download_needs_the_flag_for_localhost(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    with pytest.raises(UpdateError, match="not secure"):
        download_update(local_update(url), tmp_path / "dl")
    assert web.seen == []


def test_download_refuses_endless_and_oversized_files(web, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "MAX_DOWNLOAD_BYTES", 100_000)
    url = web.serve("/App.AppImage", PAYLOAD)
    with pytest.raises(UpdateError, match="too large"):
        download(local_update(url), tmp_path / "a")
    with pytest.raises(UpdateError, match="too large"):
        download(local_update(url, size=None, sha256=None, sha512=None), tmp_path / "b")
    stream = web.serve("/stream.AppImage", PAYLOAD, length=False)
    with pytest.raises(UpdateError, match="incomplete or was changed"):
        download(local_update(stream, size=None, sha256=None, sha512=None), tmp_path / "c")
    for name in "abc":
        assert_nothing_left(tmp_path / name)


def test_download_keyboard_interrupt_removes_the_partial_file(web, tmp_path):
    url = web.serve("/App.AppImage", PAYLOAD)
    dest = tmp_path / "dl"

    def progress(fraction, text):
        if fraction:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        download(local_update(url), dest, progress=progress)
    assert_nothing_left(dest)


# ------------------------------------------------------------------------------------------------
# UpdateCache
# ------------------------------------------------------------------------------------------------

def test_cache_default_location(isolated_env):
    cache = UpdateCache()
    assert cache.path == isolated_env / ".cache" / "easy-installer" / "updates.json"
    assert updates.cache_dir() == isolated_env / ".cache" / "easy-installer"
    assert updates.cache_dir({"HOME": "/h", "XDG_CACHE_HOME": "/c"}) == Path("/c/easy-installer")
    assert updates.cache_dir({"HOME": "/h"}) == Path("/h/.cache/easy-installer")
    assert not cache.path.exists() and cache.etags == {}
    assert cache.get("user", "org.example.App") is None and cache.is_due("user", "org.example.App", 24)
    assert not cache.path.parent.exists()      # reading never creates anything


def test_cache_put_get_roundtrip(tmp_path):
    path = tmp_path / "cache" / "easy-installer" / "updates.json"
    cache = UpdateCache(path)
    update = make_update()
    cache.put("user", "org.freecad.FreeCAD", update)
    cache.put(Scope.SYSTEM, "t3code", None)
    assert cache.get("user", "org.freecad.FreeCAD") == update
    assert cache.get(Scope.USER, "org.freecad.FreeCAD") == update
    assert cache.get("system", "org.freecad.FreeCAD") is None        # keyed by scope + id
    assert cache.get("system", "t3code") is None
    assert not cache.is_due("system", "t3code", 24) and cache.is_due("user", "t3code", 24)

    data = json.loads(path.read_text())
    assert data["format"] == 1 and set(data["apps"]) == {"user/org.freecad.FreeCAD", "system/t3code"}
    assert data["apps"]["system/t3code"]["update"] is None
    assert data["apps"]["user/org.freecad.FreeCAD"]["update"] == update.to_dict()
    checked = data["apps"]["user/org.freecad.FreeCAD"]["checked_at"]
    assert checked == cache.checked_at("user", "org.freecad.FreeCAD")
    assert abs(time.time() - calendar.timegm(time.strptime(checked, "%Y-%m-%dT%H:%M:%SZ"))) < 120
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["updates.json"]     # no temp files left

    again = UpdateCache(path)
    assert again.get("user", "org.freecad.FreeCAD") == update
    assert again.checked_at("system", "t3code") == cache.checked_at("system", "t3code")


def test_cache_is_due(tmp_path):
    path = tmp_path / "updates.json"

    def cache_checked(delta_seconds):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta_seconds))
        path.write_text(json.dumps({"format": 1, "apps": {"user/app": {"checked_at": stamp, "update": None}}}))
        return UpdateCache(path)

    assert not cache_checked(-3600).is_due("user", "app", 24)
    assert not cache_checked(-23 * 3600).is_due("user", "app", 24)
    assert cache_checked(-25 * 3600).is_due("user", "app", 24)
    assert cache_checked(-3600).is_due("user", "app", 1)
    assert cache_checked(-60).is_due("user", "app", 0)              # interval 0: always
    assert cache_checked(-60).is_due("user", "app", -5)
    assert not cache_checked(60).is_due("user", "app", 24)          # small clock differences are fine
    assert cache_checked(3 * 86400).is_due("user", "app", 24)       # "checked in the future": check again
    assert cache_checked(-60).is_due("user", "other", 24)
    for broken in ("yesterday", None, 12, "2026-13-45T99:00:00Z"):
        path.write_text(json.dumps({"apps": {"user/app": {"checked_at": broken, "update": None}}}))
        cache = UpdateCache(path)
        assert cache.is_due("user", "app", 24) and cache.checked_at("user", "app") in (None, broken)


def test_cache_forget(tmp_path):
    path = tmp_path / "updates.json"
    cache = UpdateCache(path)
    cache.put("user", "a", make_update())
    cache.put("user", "b", make_update(version="9"))
    cache.forget("user", "a")
    cache.forget("user", "never-there")
    assert cache.get("user", "a") is None and cache.is_due("user", "a", 24)
    assert cache.get("user", "b").version == "9"
    assert set(json.loads(path.read_text())["apps"]) == {"user/b"}
    empty = UpdateCache(tmp_path / "none.json")
    empty.forget("user", "a")
    assert not empty.path.exists()


def test_cache_stores_etags(tmp_path):
    path = tmp_path / "updates.json"
    cache = UpdateCache(path)
    fetch = FakeFetcher({FREECAD_API: answer(freecad_release(), headers={"ETag": '"e1"'})})
    update = check_freecad(fetch, etags=cache.etags)
    cache.put("user", "org.freecad.FreeCAD", update)

    second = UpdateCache(path)
    assert second.etags[FREECAD_API]["etag"] == '"e1"'
    fetch = FakeFetcher({FREECAD_API: answer(b"", status=304)})
    assert check_freecad(fetch, etags=second.etags) == update == second.get("user", "org.freecad.FreeCAD")
    assert fetch.calls[0].headers["If-None-Match"] == '"e1"'
    # save() alone persists new etags
    second.etags["https://example.org/x"] = {"etag": '"x"', "body": "b", "saved_at": "2026-01-01T00:00:00Z"}
    second.etags["junk"] = "not an entry"
    second.save()
    third = UpdateCache(path)
    assert set(third.etags) == {FREECAD_API, "https://example.org/x"}
    assert path.stat().st_size < 4000


@pytest.mark.parametrize("content", [
    b"", b"{", b"not json at all", b"\xff\xfe\x00", b"[]", b"null", b"42", b'"text"',
    b'{"apps": [], "etags": "x"}', b'{"apps": {"user/a": "junk", "user/b": null}, "etags": {"u": 5}}',
    b'{"apps": {"user/a": {"update": "junk", "checked_at": 5}}}',
    b'{"apps": {"user/a": {"update": {"url": "http://evil.example/a.AppImage", "filename": "a.AppImage"}}}}',
    b'{"apps": {"user/a": {"update": {"url": "https://e.org/a.AppImage", "filename": "../a.AppImage"}}}}',
    b"[" * 200000,
])
def test_cache_tolerates_corrupt_files(tmp_path, content):
    path = tmp_path / "updates.json"
    path.write_bytes(content)
    cache = UpdateCache(path)
    assert cache.get("user", "a") is None and cache.get("user", "b") is None
    assert cache.is_due("user", "a", 24)
    assert all(isinstance(entry, dict) for entry in cache.etags.values())
    cache.put("user", "a", make_update())
    assert UpdateCache(path).get("user", "a") == make_update()
    assert json.loads(path.read_text())["format"] == 1


def test_cache_never_raises_when_it_cannot_write(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache = UpdateCache(blocker / "sub" / "updates.json")
    cache.put("user", "a", make_update())
    cache.save()
    cache.forget("user", "b")
    assert cache.get("user", "a") == make_update()        # still usable in this session
    assert not cache.is_due("user", "a", 24)
    assert "could not write the update cache" in caplog.text
    # a directory in place of the file
    folder = tmp_path / "updates.json"
    folder.mkdir()
    cache = UpdateCache(folder)
    cache.put("user", "a", None)
    assert folder.is_dir() and list(folder.iterdir()) == []


def test_cache_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "updates.json"
    cache = UpdateCache(path)
    cache.put("user", "a", make_update())
    before = path.read_bytes()

    def failing_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(updates.os, "replace", failing_replace)
    cache.put("user", "b", make_update())
    assert path.read_bytes() == before                      # the old file is intact
    assert [p.name for p in tmp_path.iterdir() if p.name != "home"] == ["updates.json"]
    monkeypatch.undo()
    cache.put("user", "c", None)
    assert set(json.loads(path.read_text())["apps"]) == {"user/a", "user/b", "user/c"}


def test_two_caches_see_each_other(tmp_path):
    path = tmp_path / "updates.json"
    gui, cli = UpdateCache(path), UpdateCache(path)
    gui.put("user", "a", make_update(version="1"))
    assert cli.get("user", "a").version == "1"
    cli.etags["https://example.org/cli"] = {"etag": '"c"', "body": "", "saved_at": "2026-01-01T00:00:00Z"}
    cli.put("user", "b", make_update(version="2"))
    gui.etags["https://example.org/gui"] = {"etag": '"g"', "body": "", "saved_at": "2026-01-01T00:00:00Z"}
    gui.put("system", "c", None)
    final = UpdateCache(path)
    assert final.get("user", "a").version == "1" and final.get("user", "b").version == "2"
    assert not final.is_due("system", "c", 24)
    assert set(final.etags) == {"https://example.org/cli", "https://example.org/gui"}
    cli.forget("user", "a")
    assert gui.get("user", "a") is None


def test_cache_is_thread_safe(tmp_path):
    cache = UpdateCache(tmp_path / "updates.json")
    errors = []

    def work(n):
        try:
            for i in range(15):
                cache.put("user", f"app-{n}-{i}", make_update(version=f"{n}.{i}"))
                assert cache.get("user", f"app-{n}-{i}").version == f"{n}.{i}"
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len(json.loads(cache.path.read_text())["apps"]) == 90


# ------------------------------------------------------------------------------------------------
# the module stays pure
# ------------------------------------------------------------------------------------------------

def test_module_does_not_import_registry_or_installer():
    code = ("import sys; import easy_installer.core.updates; "
            "bad = [m for m in sys.modules if m.startswith('easy_installer.core.') and "
            "m.rsplit('.', 1)[1] in ('registry', 'installer', 'updater', 'inspector', 'privileged')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    src = Path(updates.__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(src)}, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


def test_messages_are_translatable_sentences():
    fetch = FakeFetcher({FREECAD_API: answer(b"", status=404)})
    with pytest.raises(UpdateError) as err:
        check_freecad(fetch)
    assert str(err.value) == "No update information was found for this app."
    assert err.value.message == str(err.value) and "https://" in err.value.details
