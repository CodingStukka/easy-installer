from __future__ import annotations

import gzip

import pytest

from easy_installer.core.imageinfo import (
    HICOLOR_SIZES,
    ImageInfo,
    extension_for,
    hicolor_subdir,
    probe_image,
)

from fakeappimage import make_png, make_svg, make_xpm


def write(tmp_path, data: bytes | str, name: str = "icon"):
    path = tmp_path / name
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("w, h", [(1, 1), (16, 16), (512, 512), (300, 200), (1024, 1024)])
def test_png(tmp_path, w, h):
    assert probe_image(write(tmp_path, make_png(w, h))) == ImageInfo("png", w, h)


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n",                                  # signature only
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDX" + b"\0" * 13,  # wrong chunk
    make_png(4, 4)[:20],                                   # truncated IHDR
])
def test_broken_png_is_unknown(tmp_path, data):
    assert probe_image(write(tmp_path, data)).format == "unknown"


def test_png_zero_size_is_unknown(tmp_path):
    data = bytearray(make_png(4, 4))
    data[16:24] = b"\0" * 8
    assert probe_image(write(tmp_path, bytes(data))).format == "unknown"


# ---------------------------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("svg, expected", [
    (make_svg(128, 128), (128, 128)),
    (make_svg(None, None, view_box="0 0 48 32"), (48, 32)),
    (make_svg(None, None), (None, None)),
    (make_svg(64, None, view_box="0 0 10 20"), (64, 20)),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="64px" height="32px"/>', (64, 32)),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="12pt" height="12pt"/>', (16, 16)),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="0.5in"/>', (96, 48)),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="25.4mm" height="2.54cm"/>', (96, 96)),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0,0,256,256"/>', (256, 256)),
    ("<svg width='1e2' height='50.4' xmlns='http://www.w3.org/2000/svg'></svg>", (100, 50)),
    ('<svg:svg xmlns:svg="http://www.w3.org/2000/svg" width="24" height="24"/>', (24, 24)),
    ('<svg stroke-width="3" width="40" height="30"/>', (40, 30)),
    ('<svg\n  xmlns="http://www.w3.org/2000/svg"\n  viewBox="0 0 512 512"\n>', (512, 512)),
    ('<svg width="0" height="-5" viewBox="0 0 0 0"/>', (None, None)),
])
def test_svg_sizes(tmp_path, svg, expected):
    info = probe_image(write(tmp_path, svg))
    assert info.format == "svg"
    assert (info.width, info.height) == expected


def test_svg_with_prolog_comment_doctype_and_bom(tmp_path):
    svg = (
        "﻿<?xml version='1.0' encoding='UTF-8' standalone='no'?>\n"
        "<!-- Created with <Inkscape> (http://www.inkscape.org/) -->\n"
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><g/></svg>\n'
    )
    assert probe_image(write(tmp_path, svg)) == ImageInfo("svg", 48, 48)


@pytest.mark.parametrize("text", [
    "<html><body><svg width='10' height='10'></svg></body></html>",
    "hello <svg width='10' height='10'/>",
    "<svgfoo width='10'/>",
])
def test_not_svg(tmp_path, text):
    assert probe_image(write(tmp_path, text)).format == "unknown"


def test_svgz_is_not_accepted(tmp_path):
    data = gzip.compress(make_svg(32, 32).encode())
    assert probe_image(write(tmp_path, data)).format == "unknown"


# ---------------------------------------------------------------------------------------------
# XPM & others
# ---------------------------------------------------------------------------------------------

def test_xpm(tmp_path):
    assert probe_image(write(tmp_path, make_xpm(32, 24))) == ImageInfo("xpm", 32, 24)


def test_xpm_without_header_values(tmp_path):
    assert probe_image(write(tmp_path, "/* XPM */\nstatic char *x[] = {};\n")) == ImageInfo("xpm", None, None)


@pytest.mark.parametrize("data", [b"", b"GIF89a....", b"\xff\xd8\xff\xe0JFIF", b"random junk", b"\0" * 100])
def test_unknown(tmp_path, data):
    assert probe_image(write(tmp_path, data)) == ImageInfo("unknown", None, None)


def test_missing_or_directory_is_unknown(tmp_path):
    assert probe_image(tmp_path / "missing.png").format == "unknown"
    assert probe_image(tmp_path).format == "unknown"


# ---------------------------------------------------------------------------------------------
# hicolor directory selection
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("info, subdir", [
    (ImageInfo("svg", 48, 48), "scalable/apps"),
    (ImageInfo("svg", None, None), "scalable/apps"),
    (ImageInfo("png", 512, 512), "512x512/apps"),
    (ImageInfo("png", 22, 22), "22x22/apps"),
    (ImageInfo("png", 1024, 1024), "512x512/apps"),
    (ImageInfo("png", 4096, 2048), "512x512/apps"),
    (ImageInfo("png", 300, 200), "128x128/apps"),
    (ImageInfo("png", 200, 300), "128x128/apps"),
    (ImageInfo("png", 100, 100), "96x96/apps"),
    (ImageInfo("png", 192, 192), "128x128/apps"),
    (ImageInfo("png", 10, 10), "16x16/apps"),
    (ImageInfo("png", 1, 1), "16x16/apps"),
    (ImageInfo("png", 48, 32), "32x32/apps"),
    (ImageInfo("png", None, None), "256x256/apps"),
    (ImageInfo("xpm", 16, 16), "16x16/apps"),
    (ImageInfo("xpm", None, None), "256x256/apps"),
    (ImageInfo("unknown", None, None), "256x256/apps"),
])
def test_hicolor_subdir(info, subdir):
    assert hicolor_subdir(info) == subdir


@pytest.mark.parametrize("size", HICOLOR_SIZES)
def test_every_standard_size_is_exact(size):
    assert hicolor_subdir(ImageInfo("png", size, size)) == f"{size}x{size}/apps"


@pytest.mark.parametrize("fmt, ext", [("png", "png"), ("svg", "svg"), ("xpm", "xpm"), ("unknown", "png")])
def test_extension_for(fmt, ext):
    assert extension_for(ImageInfo(fmt, None, None)) == ext
