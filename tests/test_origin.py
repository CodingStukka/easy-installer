"""Where a file was downloaded from (extended attributes written by web browsers), DESIGN §25."""

from __future__ import annotations

import errno
import os

import pytest

from easy_installer.core import origin
from easy_installer.core.origin import clean_url, origin_host, read_origin

ORIGIN = "user.xdg.origin.url"
REFERRER = "user.xdg.referrer.url"


@pytest.fixture
def attributes(monkeypatch):
    """Fake extended attributes: ``attributes[name] = bytes | OSError`` for every path."""
    values: dict[str, bytes | BaseException] = {}
    asked: list[tuple[str, str]] = []

    def getxattr(path, name, *args, **kwargs):
        asked.append((os.fspath(path), name))
        value = values.get(name, OSError(errno.ENODATA, "No data available"))
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(os, "getxattr", getxattr, raising=False)
    values["asked"] = asked  # type: ignore[assignment]
    return values


def test_reads_the_origin_attribute(attributes, tmp_path):
    attributes[ORIGIN] = b"https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD.AppImage"
    attributes[REFERRER] = b"https://github.com/FreeCAD/FreeCAD/releases"
    path = tmp_path / "FreeCAD.AppImage"
    assert read_origin(path) == "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD.AppImage"
    assert attributes["asked"] == [(str(path), ORIGIN)]


def test_falls_back_to_the_referrer(attributes, tmp_path):
    attributes[REFERRER] = b"https://www.freecad.org/downloads.php"
    assert read_origin(tmp_path / "x") == "https://www.freecad.org/downloads.php"
    assert [name for _path, name in attributes["asked"]] == [ORIGIN, REFERRER]


def test_unusable_origin_falls_back_to_the_referrer(attributes, tmp_path):
    attributes[ORIGIN] = b"blob:https://example.org/1234"
    attributes[REFERRER] = b"https://example.org/downloads"
    assert read_origin(tmp_path / "x") == "https://example.org/downloads"
    attributes[ORIGIN] = b""
    assert read_origin(tmp_path / "x") == "https://example.org/downloads"


def test_no_attributes_gives_none(attributes, tmp_path):
    assert read_origin(tmp_path / "x") is None


@pytest.mark.parametrize("error", [
    OSError(errno.ENODATA, "No data available"),
    OSError(errno.ENOTSUP, "Operation not supported"),       # vfat, some network shares
    OSError(errno.EOPNOTSUPP, "Operation not supported"),
    OSError(errno.ENOENT, "No such file or directory"),
    OSError(errno.EACCES, "Permission denied"),
    OSError(errno.ERANGE, "Numerical result out of range"),
    OSError(errno.E2BIG, "Argument list too long"),
    OSError(errno.ELOOP, "Too many levels of symbolic links"),
    ValueError("embedded null byte"),
    TypeError("bad path"),
])
def test_file_systems_without_attributes_are_tolerated(attributes, tmp_path, error):
    attributes[ORIGIN] = error
    attributes[REFERRER] = error
    assert read_origin(tmp_path / "x") is None


def test_platform_without_getxattr(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "getxattr", raising=False)
    assert read_origin(tmp_path / "x") is None


def test_odd_arguments_never_raise(attributes):
    assert read_origin(None) is None          # type: ignore[arg-type]
    assert read_origin(12) is None            # type: ignore[arg-type]
    assert read_origin("relative/name") is None


def test_missing_file_gives_none(tmp_path):
    assert read_origin(tmp_path / "does-not-exist.AppImage") is None
    assert read_origin(tmp_path) is None


def test_real_attribute_on_a_file(tmp_path):
    path = tmp_path / "App.AppImage"
    path.write_bytes(b"x")
    try:
        os.setxattr(path, ORIGIN, b"https://user:secret@example.org/get/App.AppImage\0")
    except (OSError, AttributeError) as exc:
        pytest.skip(f"this file system keeps no extended attributes: {exc}")
    assert read_origin(path) == "https://example.org/get/App.AppImage"
    assert origin_host(read_origin(path)) == "example.org"
    link = tmp_path / "link.AppImage"
    link.symlink_to(path)
    assert read_origin(link) == "https://example.org/get/App.AppImage"


def test_oversized_attribute_is_ignored(attributes, tmp_path):
    attributes[ORIGIN] = b"https://example.org/" + b"a" * 100_000
    attributes[REFERRER] = b"https://example.org/page"
    assert read_origin(tmp_path / "x") == "https://example.org/page"


# -- clean_url --------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("https://example.org/a/b.AppImage", "https://example.org/a/b.AppImage"),
    (b"https://example.org/a\0\0", "https://example.org/a"),
    ("  https://example.org/a\n", "https://example.org/a"),
    ("http://example.org/a", "http://example.org/a"),
    ("HTTPS://Example.ORG/Path", "https://example.org/Path"),
    ("https://example.org", "https://example.org"),
    ("https://example.org:443/a", "https://example.org/a"),
    ("http://example.org:80/a", "http://example.org/a"),
    ("https://example.org:8443/a", "https://example.org:8443/a"),
    ("https://my_bucket.s3.amazonaws.com/App.AppImage", "https://my_bucket.s3.amazonaws.com/App.AppImage"),
    ("https://[2001:db8::1]:8443/a", "https://[2001:db8::1]:8443/a"),
    ("https://192.168.1.10/a", "https://192.168.1.10/a"),
    ("https://example.org/a%20b/c?file=App.AppImage&v=2", "https://example.org/a%20b/c?file=App.AppImage&v=2"),
    ("https://example.org/a#section", "https://example.org/a"),
    ("https://bücher.example/a", "https://xn--bcher-kva.example/a"),
])
def test_clean_url_accepts(value, expected):
    assert clean_url(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("https://user:secret@example.org/a", "https://example.org/a"),
    ("https://user@example.org/a", "https://example.org/a"),
    ("https://user:p%40ss@example.org:8443/a?x=1", "https://example.org:8443/a?x=1"),
    ("https://github.com@evil.example/a", "https://evil.example/a"),      # the real host is shown
    ("https://example.org/a?token=abc123", "https://example.org/a"),
    ("https://example.org/a?file=x&access_token=abc", "https://example.org/a"),
    ("https://example.org/a?X-Amz-Signature=abc&X-Amz-Expires=300", "https://example.org/a"),
    ("https://release-assets.githubusercontent.com/x/y?sp=r&sv=2018-11-09&sr=b&sig=AbC%3D&jwt=e.y.z",
     "https://release-assets.githubusercontent.com/x/y"),
    ("https://example.org/a?api_key=1", "https://example.org/a"),
    ("https://example.org/a?password=1;x=2", "https://example.org/a"),
])
def test_clean_url_strips_credentials(value, expected):
    assert clean_url(value) == expected
    assert "secret" not in (clean_url(value) or "")


@pytest.mark.parametrize("value", [
    None, 5, [], "", "   ", b"", "example.org/a", "//example.org/a", "/local/path",
    "ftp://example.org/a", "file:///home/user/App.AppImage", "javascript:alert(1)",
    "data:text/plain,hi", "blob:https://example.org/uuid", "about:blank", "smb://server/share",
    "https://", "https:///path", "https://:443/a", "https://user:pw@/a",
    "https://exa mple.org/a", "https://example.org/a b", "https://example.org/a\nb",
    "https://example.org/a\x1b[31m", "https://example.org/‮gpj.exe", "https://example.org/​",
    "https://exam\u0000ple.org/", "https://example.org:99999/a", "https://example.org:port/a",
    "https://[broken/a", "https://-bad-.example/a", "https://exa$mple.org/a", "https://a..b/a",
    b"https://example.org/\xff\xfe", "https://example.org/" + "a" * 2048,
])
def test_clean_url_rejects(value):
    assert clean_url(value) is None


def test_length_limit_is_2048_characters():
    fits = "https://example.org/" + "a" * (2048 - len("https://example.org/"))
    assert len(fits) == 2048 and clean_url(fits) == fits
    assert clean_url(fits + "a") is None
    assert origin.MAX_URL_LENGTH == 2048


# -- origin_host ------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, host", [
    ("https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD.AppImage", "github.com"),
    ("https://www.FreeCAD.org/downloads.php", "www.freecad.org"),
    ("https://user:pw@example.org:8443/a?token=1", "example.org"),
    ("http://[2001:db8::1]/a", "2001:db8::1"),
    ("https://github.com@evil.example/a", "evil.example"),
    ("https://bücher.example/a", "xn--bcher-kva.example"),
])
def test_origin_host(url, host):
    assert origin_host(url) == host


@pytest.mark.parametrize("url", [None, "", "not a url", "ftp://example.org/a", "file:///etc/passwd",
                                 "https://", 12, b"\xff", "https://exa mple.org"])
def test_origin_host_of_nothing(url):
    assert origin_host(url) is None
