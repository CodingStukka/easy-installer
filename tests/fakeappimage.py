"""Builders for fake AppImages used throughout the test-suite.

A fake AppImage is a minimal but structurally valid ELF file (real section header table incl.
``.shstrtab`` and optionally ``.upd_info``, AppImage magic ``AI\\x02`` at byte 8, optional
``PT_INTERP``) followed by a real squashfs image built with ``mksquashfs``. Exactly like a real
type-2 AppImage, the payload starts at ``e_shoff + e_shentsize * e_shnum``.

The ELF runtime contains no machine code and its entry point is 0, so even if something executed
it by accident the kernel would fault immediately without running any of its bytes.

Typical use::

    from fakeappimage import make_fake_appimage, make_png, T3_DESKTOP

    app = make_fake_appimage(tmp_path / "T3.AppImage", {
        "t3code.desktop": T3_DESKTOP,
        "usr/share/icons/hicolor/512x512/apps/t3code.png": make_png(512, 512),
    }, symlinks={".DirIcon": "usr/share/icons/hicolor/512x512/apps/t3code.png"})

or simply ``make_sample_appimage(tmp_path / "T3.AppImage", "t3code")``.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import textwrap
import zlib
from pathlib import Path
from typing import Mapping, Sequence

import pytest

EM_X86_64 = 62
EM_AARCH64 = 183
EM_386 = 3
EM_ARM = 40

PT_LOAD = 1
PT_INTERP = 3
SHT_PROGBITS = 1
SHT_STRTAB = 3

INTERP_PATH = b"/lib64/ld-linux-x86-64.so.2\0"
UPD_INFO_SIZE = 1024

requires_mksquashfs = pytest.mark.skipif(
    shutil.which("mksquashfs") is None, reason="mksquashfs (squashfs-tools) is not installed"
)
requires_unsquashfs = pytest.mark.skipif(
    shutil.which("unsquashfs") is None, reason="unsquashfs (squashfs-tools) is not installed"
)


# --------------------------------------------------------------------------------------------
# Sample .desktop files (verbatim copies of the entries embedded in real AppImages)
# --------------------------------------------------------------------------------------------

T3_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=T3 Code (Alpha)\n"
    "Exec=AppRun --no-sandbox %U\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=t3code\n"
    "StartupWMClass=t3code\n"
    "X-AppImage-Version=0.0.42\n"
    "Comment=T3 Code desktop build\n"
    "MimeType=x-scheme-handler/t3code;x-scheme-handler/t3code-dev;\n"
    "Categories=Development;\n"
)

OPENSCAD_DESKTOP = (
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Version=1.0\n"
    "Name=OpenSCAD\n"
    "Comment=The Programmers Solid 3D CAD Modeller\n"
    "Icon=openscad-nightly\n"
    "Exec=openscad %f\n"
    "StartupWMClass=org.openscad.openscad\n"
    "MimeType=application/x-openscad;\n"
    "Categories=Graphics;3DGraphics;Engineering;\n"
    "Keywords=3d;solid;geometry;csg;model;stl;\n"
    "X-AppImage-Version=2026.03.28\n"
)

FREECAD_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=FreeCAD\n"
    "Comment=Feature based Parametric Modeler\n"
    "Comment[de]=Feature-basierter parametrischer Modellierer\n"
    "Comment[es]=Modelador paramétrico basado en operaciones\n"
    "Comment[ko]=형상 기반 파라메트릭 모델링 도구\n"
    "Comment[pl]=Modeler parametryczny oparty na cechach\n"
    "Comment[ru]=Система автоматизированного проектирования\n"
    "GenericName=CAD Application\n"
    "GenericName[de]=CAD-Anwendung\n"
    "GenericName[es]=Aplicación CAD\n"
    "GenericName[ko]=CAD 응용프로그램\n"
    "GenericName[pl]=Aplikacja CAD\n"
    "GenericName[ru]=Система автоматизированного проектирования\n"
    "Exec=AppRun - --single-instance %F\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=org.freecad.FreeCAD\n"
    "Categories=Graphics;Science;Education;Engineering;X-CNC;\n"
    "StartupNotify=true\n"
    "StartupWMClass=FreeCAD\n"
    "MimeType=application/x-extension-fcstd;model/obj;image/vnd.dwg;image/vnd.dxf;"
    "model/vnd.collada+xml;application/iges;model/iges;model/step;model/step+zip;model/stl;"
    "application/vnd.shp;model/vrml;\n"
)

WINBOAT_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=winboat\n"
    "Exec=AppRun --no-sandbox %U\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=winboat\n"
    "StartupWMClass=winboat\n"
    "X-AppImage-Version=0.9.0\n"
    "Comment=Windows for Penguins\n"
    "Categories=Utility;\n"
)

ANYTYPE_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=Anytype\n"
    "Exec=AppRun --ozone-platform-hint=auto %U\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=anytype\n"
    "StartupWMClass=anytype\n"
    "X-AppImage-Version=0.55.4\n"
    "Categories=Utility;\n"
    "Keywords=project management;\n"
    "MimeType=x-scheme-handler/anytype;\n"
    "Comment=Anytype\n"
)

# Note: no trailing newline, exactly like the real file.
ANYTYPE_XWAYLAND_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=Anytype (XWayland)\n"
    "Comment=Project management and knowledge workspace (XWayland mode)\n"
    "Exec=anytype --ozone-platform=x11 %u\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=anytype\n"
    "Categories=Utility;Office;Calendar;ProjectManagement;\n"
    "StartupWMClass=anytype\n"
    "Keywords=project management;\n"
    "MimeType=x-scheme-handler/anytype;\n"
    "NoDisplay=true"
)

PEN_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=Pen\n"
    "Exec=AppRun --no-sandbox %U\n"
    "Terminal=false\n"
    "Type=Application\n"
    "Icon=pen\n"
    "StartupWMClass=Pen\n"
    "X-AppImage-Version=1.2.8\n"
    "Comment=Desktop app for pen.dev\n"
    "MimeType=x-scheme-handler/pencil;\n"
    "Categories=Graphics;\n"
)

#: file name -> text of the embedded desktop entries of the real sample AppImages
SAMPLE_DESKTOP: dict[str, str] = {
    "t3code.desktop": T3_DESKTOP,
    "openscad.desktop": OPENSCAD_DESKTOP,
    "org.freecad.FreeCAD.desktop": FREECAD_DESKTOP,
    "winboat.desktop": WINBOAT_DESKTOP,
    "anytype.desktop": ANYTYPE_DESKTOP,
    "anytype-xwayland.desktop": ANYTYPE_XWAYLAND_DESKTOP,
    "pen.desktop": PEN_DESKTOP,
}


#: shared-mime-info packages shipped by the real OpenSCAD and FreeCAD AppImages (verbatim)
OPENSCAD_MIME_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
    '   <mime-type type="application/x-openscad">\n'
    "     <comment>OpenSCAD Model</comment>\n"
    '     <glob pattern="*.scad"/>\n'
    '     <icon name="openscad"/>\n'
    "   </mime-type>\n"
    "</mime-info>\n"
)
FREECAD_MIME_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<mime-info xmlns='http://www.freedesktop.org/standards/shared-mime-info'>\n"
    '  <mime-type type="application/x-extension-fcstd">\n'
    '    <!-- <sub-class-of type="application/zip"/> -->\n'
    "    <comment>FreeCAD document files</comment>\n"
    '    <glob pattern="*.fcstd"/>\n'
    '    <generic-icon name="application-x-extension-fcstd"/>\n'
    "  </mime-type>\n"
    "</mime-info>\n"
)


# --------------------------------------------------------------------------------------------
# Tiny images
# --------------------------------------------------------------------------------------------

def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def make_png(width: int, height: int, rgba: tuple[int, int, int, int] = (32, 96, 200, 255)) -> bytes:
    """A valid RGBA PNG of the given size filled with one colour."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = b"\x00" + bytes(rgba) * width
    idat = zlib.compress(row * height, 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def make_svg(width: int | None = 128, height: int | None = 128, *, view_box: str | None = None) -> str:
    attrs = ['xmlns="http://www.w3.org/2000/svg"']
    if width is not None:
        attrs.append(f'width="{width}"')
    if height is not None:
        attrs.append(f'height="{height}"')
    if view_box is not None:
        attrs.append(f'viewBox="{view_box}"')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<svg {' '.join(attrs)}>\n"
        '  <rect x="0" y="0" width="10" height="10" fill="#3366cc"/>\n'
        "</svg>\n"
    )


def make_xpm(width: int = 16, height: int = 16) -> str:
    rows = ",\n".join('"' + "." * width + '"' for _ in range(height))
    return (
        "/* XPM */\n"
        "static char * icon_xpm[] = {\n"
        f'"{width} {height} 1 1",\n'
        '". c #3366CC",\n'
        f"{rows}}};\n"
    )


# --------------------------------------------------------------------------------------------
# ELF runtime
# --------------------------------------------------------------------------------------------

def build_runtime(
    *,
    bits: int = 64,
    little_endian: bool = True,
    machine: int = EM_X86_64,
    appimage_type: int | None = 2,
    upd_info: str | None = None,
    dynamic_interp: bool = False,
    extra_sections: Mapping[str, bytes] | None = None,
) -> bytes:
    """Return ELF "runtime" bytes whose length equals ``e_shoff + e_shentsize * e_shnum``.

    The section header table is the last thing in the returned bytes, so appending a payload
    places it exactly at the AppImage payload offset.
    """
    if bits not in (32, 64):
        raise ValueError("bits must be 32 or 64")
    e = "<" if little_endian else ">"
    ehsize, phentsize, shentsize = (64, 56, 64) if bits == 64 else (52, 32, 40)

    sections: list[tuple[str, int, bytes]] = []  # (name, sh_type, data)
    if dynamic_interp:
        sections.append((".interp", SHT_PROGBITS, INTERP_PATH))
    if upd_info is not None:
        raw = upd_info.encode("utf-8")
        size = max(UPD_INFO_SIZE, len(raw) + 1)
        sections.append((".upd_info", SHT_PROGBITS, raw.ljust(size, b"\0")))
    for name, data in (extra_sections or {}).items():
        sections.append((name, SHT_PROGBITS, bytes(data)))

    names = [s[0] for s in sections] + [".shstrtab"]
    shstrtab = b"\0"
    name_offsets: dict[str, int] = {}
    for name in names:
        name_offsets[name] = len(shstrtab)
        shstrtab += name.encode("ascii") + b"\0"
    sections.append((".shstrtab", SHT_STRTAB, shstrtab))

    phnum = 2 if dynamic_interp else 1
    offset = ehsize + phnum * phentsize
    placed: list[tuple[str, int, bytes, int]] = []  # (name, type, data, file offset)
    for name, sh_type, data in sections:
        placed.append((name, sh_type, data, offset))
        offset += len(data)
    shoff = (offset + 7) & ~7
    shnum = len(placed) + 1  # + NULL section
    shstrndx = shnum - 1
    total = shoff + shnum * shentsize

    out = bytearray(total)
    ident = bytearray(16)
    ident[0:4] = b"\x7fELF"
    ident[4] = 2 if bits == 64 else 1
    ident[5] = 1 if little_endian else 2
    ident[6] = 1
    if appimage_type is not None:
        ident[8:11] = b"AI" + bytes([appimage_type])
    out[0:16] = ident
    phoff = ehsize
    if bits == 64:
        struct.pack_into(
            e + "HHIQQQIHHHHHH", out, 16,
            2, machine, 1, 0, phoff, shoff, 0, ehsize, phentsize, phnum, shentsize, shnum, shstrndx,
        )
    else:
        struct.pack_into(
            e + "HHIIIIIHHHHHH", out, 16,
            2, machine, 1, 0, phoff, shoff, 0, ehsize, phentsize, phnum, shentsize, shnum, shstrndx,
        )

    # Program headers: PT_LOAD over the whole runtime (+ PT_INTERP).
    phdrs = [(PT_LOAD, 0, total)]
    if dynamic_interp:
        interp = next(p for p in placed if p[0] == ".interp")
        phdrs.insert(0, (PT_INTERP, interp[3], len(interp[2])))
    for i, (p_type, p_offset, p_size) in enumerate(phdrs):
        at = phoff + i * phentsize
        if bits == 64:
            struct.pack_into(e + "IIQQQQQQ", out, at, p_type, 4, p_offset, 0x400000 + p_offset,
                             0x400000 + p_offset, p_size, p_size, 1)
        else:
            struct.pack_into(e + "IIIIIIII", out, at, p_type, p_offset, 0x8048000 + p_offset,
                             0x8048000 + p_offset, p_size, p_size, 4, 1)

    for name, _sh_type, data, file_offset in placed:
        out[file_offset:file_offset + len(data)] = data

    for index, (name, sh_type, data, file_offset) in enumerate(placed, start=1):
        at = shoff + index * shentsize
        if bits == 64:
            struct.pack_into(e + "IIQQQQIIQQ", out, at, name_offsets[name], sh_type, 0, 0,
                             file_offset, len(data), 0, 0, 1, 0)
        else:
            struct.pack_into(e + "IIIIIIIIII", out, at, name_offsets[name], sh_type, 0, 0,
                             file_offset, len(data), 0, 0, 1, 0)
    return bytes(out)


# --------------------------------------------------------------------------------------------
# squashfs payload
# --------------------------------------------------------------------------------------------

def populate_tree(
    root: Path,
    files: Mapping[str, bytes | str],
    symlinks: Mapping[str, str] | None = None,
    modes: Mapping[str, int] | None = None,
) -> None:
    """Create files (keys ending in "/" are directories) and symlinks below ``root``."""
    for rel, content in files.items():
        target = root / rel
        if rel.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content)
    for rel, link_target in (symlinks or {}).items():
        link = root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target, link)
    for rel, mode in (modes or {}).items():
        os.chmod(root / rel, mode)


def make_squashfs(
    out: Path,
    files: Mapping[str, bytes | str],
    symlinks: Mapping[str, str] | None = None,
    modes: Mapping[str, int] | None = None,
    pseudo: Sequence[str] = (),
) -> Path:
    """``pseudo``: extra mksquashfs pseudo definitions, e.g. ``"big.bin f 644 0 0 head -c 9 /dev/zero"``
    (a file made by a command: large but tiny in the image; its folder must exist in ``files``)."""
    tool = shutil.which("mksquashfs")
    if tool is None:
        pytest.skip("mksquashfs (squashfs-tools) is not installed")
    with tempfile.TemporaryDirectory(prefix="fakeappimage-") as tmp:
        src = Path(tmp) / "root"
        src.mkdir()
        populate_tree(src, files, symlinks)
        # Modes are applied inside the image (pseudo definitions), so that mksquashfs can still
        # read files that end up unreadable (e.g. mode 000) in the payload.
        definitions: list[str] = []
        for rel, mode in (modes or {}).items():
            definitions += ["-p", f"{rel.strip('/')} m {mode:o} 0 0"]
        for definition in pseudo:
            definitions += ["-p", definition]
        subprocess.run(
            [tool, str(src), str(out), "-noappend", "-all-root", "-comp", "gzip",
             "-no-progress", "-no-xattrs", "-quiet", *definitions],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    return out


def make_fake_appimage(
    path: str | os.PathLike,
    files: Mapping[str, bytes | str],
    symlinks: Mapping[str, str] | None = None,
    *,
    upd_info: str | None = None,
    appimage_magic: bool = True,
    machine: int = EM_X86_64,
    dynamic_interp: bool = False,
    appimage_type: int = 2,
    bits: int = 64,
    little_endian: bool = True,
    payload: bytes | None = None,
    modes: Mapping[str, int] | None = None,
    executable: bool = True,
    pseudo: Sequence[str] = (),
) -> Path:
    """Write a fake type-2 AppImage (ELF runtime + squashfs payload) and return its path.

    ``payload`` replaces the squashfs image with arbitrary bytes (e.g. to fake a broken
    download); ``modes`` sets permission bits of payload members before packing.
    """
    path = Path(path)
    runtime = build_runtime(
        bits=bits,
        little_endian=little_endian,
        machine=machine,
        appimage_type=appimage_type if appimage_magic else None,
        upd_info=upd_info,
        dynamic_interp=dynamic_interp,
    )
    if payload is None:
        with tempfile.TemporaryDirectory(prefix="fakeappimage-sqfs-") as tmp:
            image = make_squashfs(Path(tmp) / "payload.squashfs", files, symlinks, modes, pseudo)
            payload = image.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(runtime + payload)
    os.chmod(path, 0o755 if executable else 0o644)
    return path


# --------------------------------------------------------------------------------------------
# Realistic samples
# --------------------------------------------------------------------------------------------

_SVG_ICON = make_svg(64, 64, view_box="0 0 64 64")


def sample_files(kind: str) -> tuple[dict[str, bytes | str], dict[str, str], dict[str, object]]:
    """Return (files, symlinks, make_fake_appimage kwargs) mimicking a real sample AppImage."""
    if kind == "t3code":
        icon = "usr/share/icons/hicolor/512x512/apps/t3code.png"
        return (
            {
                "t3code.desktop": T3_DESKTOP,
                "AppRun": "#!/bin/sh\nexec \"$APPDIR/t3code\" \"$@\"\n",
                "t3code": b"\x7fELF fake electron binary",
                "chrome-sandbox": b"\x7fELF fake chrome-sandbox",
                "chrome_crashpad_handler": b"\x7fELF fake crashpad",
                "resources/app.asar": b"fake asar archive",
                icon: make_png(512, 512),
                "usr/share/icons/hicolor/256x256/apps/t3code.png": make_png(256, 256),
            },
            {".DirIcon": icon, "t3code.png": icon},
            {"dynamic_interp": True, "upd_info": ""},
        )
    if kind == "openscad":
        return (
            {
                "openscad.desktop": OPENSCAD_DESKTOP,
                "usr/bin/openscad": b"\x7fELF fake openscad",
                "usr/share/icons/hicolor/64x64/apps/openscad-nightly.png": make_png(64, 64),
                "usr/share/icons/hicolor/128x128/apps/openscad-nightly.png": make_png(128, 128),
                "usr/share/icons/hicolor/256x256/apps/openscad-nightly.png": make_png(256, 256),
                "usr/share/mime/packages/openscad.xml": OPENSCAD_MIME_XML,
            },
            {
                ".DirIcon": "openscad-nightly.png",
                "AppRun": "usr/bin/openscad",
                "openscad-nightly.png": "usr/share/icons/hicolor/64x64/apps/openscad-nightly.png",
            },
            {"upd_info": ""},
        )
    if kind == "freecad":
        return (
            {
                "org.freecad.FreeCAD.desktop": FREECAD_DESKTOP,
                "AppRun": "#!/bin/sh\nexec \"$APPDIR/usr/bin/freecad\" \"$@\"\n",
                "org.freecad.FreeCAD.svg": _SVG_ICON,
                "usr/bin/freecad": b"\x7fELF fake freecad",
                "usr/share/icons/hicolor/scalable/apps/org.freecad.FreeCAD.svg": _SVG_ICON,
                "usr/share/icons/hicolor/64x64/apps/org.freecad.FreeCAD.png": make_png(64, 64),
                "usr/share/pixmaps/freecad.svg": _SVG_ICON,
                "usr/share/mime/packages/org.freecad.FreeCAD.xml": FREECAD_MIME_XML,
            },
            {".DirIcon": "org.freecad.FreeCAD.svg"},
            {"upd_info": "gh-releases-zsync|FreeCAD|FreeCAD|latest|FreeCAD*x86_64*.AppImage.zsync"},
        )
    raise ValueError(f"unknown sample kind {kind!r}")


def make_sample_appimage(path: str | os.PathLike, kind: str = "t3code", **overrides: object) -> Path:
    """A fake AppImage shaped like one of the real samples: "t3code", "openscad" or "freecad".

    Suggested file names: "T3-Code-0.0.42-x86_64.AppImage", "OpenSCAD-2026.03.28-x86_64.AppImage",
    "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage".
    """
    files, symlinks, kwargs = sample_files(kind)
    kwargs = {**kwargs, **overrides}
    return make_fake_appimage(path, files, symlinks, **kwargs)  # type: ignore[arg-type]


def payload_offset_of(path: str | os.PathLike) -> int:
    """``e_shoff + e_shentsize * e_shnum`` of an ELF64/ELF32 little-endian file."""
    header = Path(path).read_bytes()[:64]
    if header[4] == 2:
        shoff = struct.unpack_from("<Q", header, 40)[0]
        shentsize, shnum = struct.unpack_from("<HH", header, 58)
    else:
        shoff = struct.unpack_from("<I", header, 32)[0]
        shentsize, shnum = struct.unpack_from("<HH", header, 46)
    return shoff + shentsize * shnum


def make_runtime_emulator(directory: Path, appimage: Path, *, env_dump: Path | None = None,
                          extra: str = "", name: str = "runtime.sh") -> Path:
    """A shell script that behaves like ``<appimage> --appimage-extract <pattern>``.

    Useful with ``ExtractCommandReader(appimage, command=[str(script)])`` to exercise the
    fallback reader without executing anything from the fake AppImage. ``extra`` is shell code
    run first (e.g. ``"exit 3"`` or ``"sleep 10"``).
    """
    tool = shutil.which("unsquashfs")
    if tool is None:
        pytest.skip("unsquashfs (squashfs-tools) is not installed")
    script = Path(directory) / name
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        {f'env > "{env_dump}"' if env_dump else ''}
        {extra}
        [ "$1" = "--appimage-extract" ] || exit 64
        exec "{tool}" -o {payload_offset_of(appimage)} -d squashfs-root -f -no-xattrs -no-progress \\
            "{appimage}" "$2" >/dev/null
        """))
    script.chmod(0o755)
    return script
