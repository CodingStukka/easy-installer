"""Tiny image probing (PNG / SVG / XPM) and hicolor theme directory selection."""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HICOLOR_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
DEFAULT_SIZE = 256

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEAD_SIZE = 64 * 1024
_MAX_DIMENSION = 1 << 16


@dataclass(frozen=True)
class ImageInfo:
    format: str          # "png" | "svg" | "xpm" | "unknown"
    width: int | None
    height: int | None


UNKNOWN = ImageInfo("unknown", None, None)


def _plausible(value: float | None) -> int | None:
    if value is None:
        return None
    number = int(round(value))
    return number if 0 < number <= _MAX_DIMENSION else None


def _probe_png(head: bytes) -> ImageInfo | None:
    if not head.startswith(PNG_SIGNATURE):
        return None
    if len(head) < 24 or head[12:16] != b"IHDR":
        return ImageInfo("unknown", None, None)
    width, height = struct.unpack(">II", head[16:24])
    if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
        return ImageInfo("unknown", None, None)
    return ImageInfo("png", width, height)


_SVG_ROOT_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?svg(?=[\s/>])([^>]*)>", re.DOTALL)
_ATTR_RE = re.compile(r"""(?:^|\s)(width|height|viewBox)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
_LENGTH_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z%]*)\s*$")
_UNITS = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54,
          "in": 96.0, "em": 16.0, "ex": 8.0}


def _svg_length(value: str) -> float | None:
    match = _LENGTH_RE.match(value)
    if not match:
        return None
    factor = _UNITS.get(match.group(2).lower())
    if factor is None:  # "%" and unknown units
        return None
    return float(match.group(1)) * factor


def _probe_svg(head: bytes) -> ImageInfo | None:
    text = head.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")
    if not text.startswith("<"):
        return None
    match = _SVG_ROOT_RE.search(text)
    if not match:
        return None
    # Everything before the root element must be XML prolog material, not another document.
    prolog = re.sub(r"<!--.*?-->", "", text[: match.start()], flags=re.DOTALL)
    if re.search(r"<(?![?!])", prolog):
        return None
    attrs = {m.group(1): m.group(3) for m in _ATTR_RE.finditer(match.group(1))}
    width = _svg_length(attrs["width"]) if "width" in attrs else None
    height = _svg_length(attrs["height"]) if "height" in attrs else None
    if (width is None or height is None) and "viewBox" in attrs:
        parts = re.split(r"[\s,]+", attrs["viewBox"].strip())
        if len(parts) == 4:
            try:
                vb_w, vb_h = float(parts[2]), float(parts[3])
            except ValueError:
                vb_w = vb_h = 0.0
            if vb_w > 0 and vb_h > 0:
                width = width if width is not None else vb_w
                height = height if height is not None else vb_h
    return ImageInfo("svg", _plausible(width), _plausible(height))


_XPM_HEADER_RE = re.compile(r'"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)')


def _probe_xpm(head: bytes) -> ImageInfo | None:
    text = head.decode("latin-1").lstrip()
    if not text.startswith("/* XPM */"):
        return None
    match = _XPM_HEADER_RE.search(text)
    if not match:
        return ImageInfo("xpm", None, None)
    return ImageInfo("xpm", _plausible(int(match.group(1))), _plausible(int(match.group(2))))


def probe_image(path: Path) -> ImageInfo:
    """Best-effort format and size detection; ``format == "unknown"`` if not a usable icon."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_SIZE)
    except OSError as exc:
        log.debug("cannot probe %s: %s", path, exc)
        return UNKNOWN
    for probe in (_probe_png, _probe_xpm, _probe_svg):
        result = probe(head)
        if result is not None:
            return result
    return UNKNOWN


def _theme_size(width: int | None, height: int | None) -> int:
    if not width or not height:
        return DEFAULT_SIZE
    if width == height and width in HICOLOR_SIZES:
        return width
    smallest = min(width, height)
    if smallest > HICOLOR_SIZES[-1]:
        return HICOLOR_SIZES[-1]
    fitting = [s for s in HICOLOR_SIZES if s <= smallest]
    return fitting[-1] if fitting else HICOLOR_SIZES[0]


def hicolor_subdir(info: ImageInfo) -> str:
    """Directory below ``.../icons/hicolor`` for this image, e.g. ``"256x256/apps"``."""
    if info.format == "svg":
        return "scalable/apps"
    size = _theme_size(info.width, info.height)
    return f"{size}x{size}/apps"


def extension_for(info: ImageInfo) -> str:
    """File extension for the icon ("png" for unknown formats)."""
    return info.format if info.format in ("png", "svg", "xpm") else "png"
