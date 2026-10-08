"""Where a downloaded file came from.

Web browsers (and ``curl --xattr``, ``wget --xattr``) note the address a file was downloaded
from in the extended attributes ``user.xdg.origin.url`` and ``user.xdg.referrer.url``. This is
only information for the user ("downloaded from github.com"); nothing is ever decided from it.
Many file systems and programs do not keep these attributes - then there simply is no origin.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

log = logging.getLogger(__name__)

#: tried in this order: the download address itself, then the page it was linked from
ORIGIN_ATTRIBUTES = ("user.xdg.origin.url", "user.xdg.referrer.url")
MAX_URL_LENGTH = 2048

# Underscores are not allowed in host names, but some download servers have them anyway.
_LABEL = r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"{_LABEL}(\.{_LABEL})*\.?\Z")
_IPV6_RE = re.compile(r"[0-9a-f:.]{2,45}\Z")
# Parameters that carry a password or a temporary key (signed download links): the whole query
# is dropped then, the rest of the address still says where the file came from.
_SECRET_PARAMETER = re.compile(
    r"(^|[-_.])(sig|signature|token|auth|authorization|key|apikey|secret|password|passwd|pwd|jwt|"
    r"credential|session|sid|code|expires|se|sp|sv|sr)($|[-_.])|^x-(amz|goog|ms)-",
    re.IGNORECASE,
)


def _host(parts: SplitResult) -> str | None:
    """The host name in lower case (IDN as punycode), or None if it is not a plausible one."""
    host = parts.hostname
    if not host:
        return None
    if ":" in host:                       # an IPv6 address (urlsplit removed the brackets)
        return host if _IPV6_RE.match(host) else None
    try:
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if len(host) > 253 or not _HOST_RE.match(host):
        return None
    return host


def _query_is_secret(query: str) -> bool:
    for item in re.split(r"[&;]", query):
        name = item.split("=", 1)[0]
        if _SECRET_PARAMETER.search(name):
            return True
    return False


def clean_url(value: object) -> str | None:
    """``value`` as an ``http(s)`` address that is safe to store and show, else None.

    At most :data:`MAX_URL_LENGTH` characters, no spaces or control characters. The user name
    and password (``https://user:secret@host/``), the fragment and a query string with keys or
    tokens in it are removed.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip("\0 \t\r\n")
    if not text or len(text) > MAX_URL_LENGTH:
        return None
    # No spaces, control characters or invisible "format" characters (they can reorder text).
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in text):
        return None
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        host = _host(parts)
        port = parts.port
    except ValueError:                    # "https://[broken", port out of range
        return None
    if host is None:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        netloc += f":{port}"
    query = "" if _query_is_secret(parts.query) else parts.query
    url = SplitResult(scheme, netloc, parts.path, query, "").geturl()
    return url if len(url) <= MAX_URL_LENGTH else None


def _read_attribute(path: str, name: str) -> bytes | None:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:                  # not Linux
        return None
    try:
        return getxattr(path, name)
    except OSError as exc:                # no such attribute, not supported here, file is gone
        log.debug("no %s on %s: %s", name, path, exc)
    except (ValueError, TypeError) as exc:
        log.debug("cannot read %s of %r: %s", name, path, exc)
    return None


def read_origin(path: Path) -> str | None:
    """The address ``path`` was downloaded from, if the browser noted it on the file.

    Reads ``user.xdg.origin.url``, else ``user.xdg.referrer.url``; see :func:`clean_url` for
    what is accepted. Never raises.
    """
    try:
        name = os.fspath(path)
    except TypeError:
        return None
    for attribute in ORIGIN_ATTRIBUTES:
        raw = _read_attribute(name, attribute)
        if not raw or len(raw) > 4 * MAX_URL_LENGTH:
            continue
        url = clean_url(raw)
        if url is not None:
            return url
    return None


def origin_host(url: str | None) -> str | None:
    """The site of an origin address for display ("github.com"), None if there is none."""
    cleaned = clean_url(url)
    if cleaned is None:
        return None
    try:
        return _host(urlsplit(cleaned))
    except ValueError:
        return None
