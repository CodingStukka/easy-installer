"""Builders for fake portable app archives (``.tar``, ``.tar.gz``, ``.tar.xz``, ``.tar.bz2``, ``.zip``).

Names are stored exactly as given, so hostile archives can be built as easily as friendly
ones: absolute paths, ``..``, symlinks that escape, hard links, devices and FIFOs (tar), setuid
bits, zip symlinks and file types via ``external_attr``, headers that lie about sizes.

Typical use::

    from fakearchive import electron_tree, fake_elf, make_archive, make_tar, make_zip

    make_archive(tmp_path / "MyApp-1.2.3-linux-x64.tar.gz", **electron_tree())
    make_zip(tmp_path / "tool.zip", {"tool": fake_elf()}, modes={"tool": 0o755})
    make_tar(tmp_path / "evil.tar", entries=[symlink_entry("app/out", "/etc"),
                                              file_entry("app/out/passwd", b"x")])

``files`` map a member name to its content (``str`` is encoded as UTF-8), ``modes`` a member
name to its permission bits (default 0644), ``symlinks`` a member name to its target. Members
are written in this order: ``dirs``, ``files``, ``symlinks``, ``hardlinks``, ``entries`` - or
links first with ``links_first=True``. For full control pass only ``entries``.

The fake programs contain no machine code: :func:`fake_elf` is just an ELF header followed by
zeros, :func:`fake_script` a two-line shell script. Nothing here is ever executed.
"""

from __future__ import annotations

import io
import json
import stat
import struct
import tarfile
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from easy_installer.core.elf import host_machine
from fakeappimage import make_png, make_svg

EM_X86_64 = 62
EM_AARCH64 = 183
ET_REL, ET_EXEC, ET_DYN = 1, 2, 3
#: The fake programs are "built" for the computer the tests run on (no architecture warnings) ...
HOST_MACHINE = host_machine() or EM_X86_64
#: ... unless a test asks for a program made for another kind of computer.
FOREIGN_MACHINE = EM_AARCH64 if HOST_MACHINE != EM_AARCH64 else EM_X86_64

Content = bytes | str

TAR_COMPRESSIONS = {
    ".tar": "", ".tar.gz": "gz", ".tgz": "gz", ".tar.xz": "xz", ".txz": "xz",
    ".tar.bz2": "bz2", ".tbz2": "bz2",
}
ARCHIVE_SUFFIXES = (*TAR_COMPRESSIONS, ".zip")
_FIXED_TIME = (2026, 1, 2, 3, 4, 6)
_FIXED_MTIME = 1767323046


# --------------------------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------------------------

def fake_elf(machine: int | None = None, *, e_type: int = ET_DYN, bits: int = 64,
             little_endian: bool = True, size: int = 256) -> bytes:
    """An ELF header (nothing else): enough to be recognised as a program of ``machine``
    (default: this computer's)."""
    machine = HOST_MACHINE if machine is None else machine
    order = "<" if little_endian else ">"
    ident = b"\x7fELF" + bytes([2 if bits == 64 else 1, 1 if little_endian else 2, 1, 0]) + bytes(8)
    header = ident + struct.pack(order + "HHI", e_type, machine, 1)
    return header + bytes(max(size, 64) - len(header))


def fake_script(body: str = 'exec "$(dirname "$0")/program" "$@"\n', interpreter: str = "/bin/sh") -> bytes:
    return f"#!{interpreter}\n{body}".encode()


def _bytes(content: Content) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else bytes(content)


# --------------------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    kind: str                           # file | dir | symlink | hardlink | fifo | chardev | blockdev
    name: str                           # stored verbatim
    data: bytes = b""
    mode: int = 0o644                   # permission bits incl. setuid/setgid/sticky
    target: str = ""                    # symlink / hardlink target, stored verbatim
    declared_size: int | None = None    # the size the header claims (None: the truth)
    encrypted: bool = False             # zip: set the "encrypted" flag of the member


def file_entry(name: str, data: Content = b"", mode: int = 0o644, *,
               declared_size: int | None = None, encrypted: bool = False) -> Entry:
    return Entry("file", name, _bytes(data), mode, declared_size=declared_size, encrypted=encrypted)


def dir_entry(name: str, mode: int = 0o755) -> Entry:
    return Entry("dir", name, mode=mode)


def symlink_entry(name: str, target: str) -> Entry:
    return Entry("symlink", name, target=target, mode=0o777)


def hardlink_entry(name: str, target: str) -> Entry:
    return Entry("hardlink", name, target=target)


def special_entry(name: str, kind: str = "fifo") -> Entry:
    assert kind in ("fifo", "chardev", "blockdev")
    return Entry(kind, name)


def _entries(files: Mapping[str, Content] | None, symlinks: Mapping[str, str] | None,
             modes: Mapping[str, int] | None, hardlinks: Mapping[str, str] | None,
             dirs: Iterable[str] | None, entries: Sequence[Entry] | None,
             links_first: bool) -> list[Entry]:
    modes = modes or {}
    unknown = set(modes) - set(files or {}) - set(dirs or ())
    assert not unknown, f"modes for members that do not exist: {sorted(unknown)}"
    folder_entries = [dir_entry(name, modes.get(name, 0o755)) for name in dirs or ()]
    file_entries = [file_entry(name, data, modes.get(name, 0o644)) for name, data in (files or {}).items()]
    link_entries = [symlink_entry(name, target) for name, target in (symlinks or {}).items()]
    hard_entries = [hardlink_entry(name, target) for name, target in (hardlinks or {}).items()]
    body = link_entries + file_entries if links_first else file_entries + link_entries
    return [*folder_entries, *body, *hard_entries, *(entries or ())]


# --------------------------------------------------------------------------------------------
# tar
# --------------------------------------------------------------------------------------------

_TAR_TYPES = {
    "file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE,
    "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE, "chardev": tarfile.CHRTYPE,
    "blockdev": tarfile.BLKTYPE,
}


def _tar_compression(path: Path, compression: str | None) -> str:
    if compression is not None:
        return compression
    lower = path.name.lower()
    for suffix in sorted(TAR_COMPRESSIONS, key=len, reverse=True):
        if lower.endswith(suffix):
            return TAR_COMPRESSIONS[suffix]
    return ""


def make_tar(path: str | Path, files: Mapping[str, Content] | None = None,
             symlinks: Mapping[str, str] | None = None, modes: Mapping[str, int] | None = None,
             compression: str | None = None, *, hardlinks: Mapping[str, str] | None = None,
             dirs: Iterable[str] | None = None, entries: Sequence[Entry] | None = None,
             links_first: bool = False, tar_format: int = tarfile.PAX_FORMAT) -> Path:
    """Write a tar archive; ``compression`` is "", "gz", "xz" or "bz2" (None: by file name)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:" + _tar_compression(path, compression)
    with tarfile.open(path, mode, format=tar_format) as tar:
        for entry in _entries(files, symlinks, modes, hardlinks, dirs, entries, links_first):
            info = tarfile.TarInfo(entry.name)
            info.type = _TAR_TYPES[entry.kind]
            info.mode = entry.mode
            info.mtime = _FIXED_MTIME
            info.uid = info.gid = 1000
            info.uname = info.gname = "builder"
            info.linkname = entry.target
            if entry.kind in ("chardev", "blockdev"):
                info.devmajor, info.devminor = 1, 3
            if entry.kind != "file":
                tar.addfile(info)
            elif entry.declared_size is None:
                info.size = len(entry.data)
                tar.addfile(info, io.BytesIO(entry.data))
            else:
                # A header that lies: written by hand, followed by the real (shorter) data.
                info.size = entry.declared_size
                header = info.tobuf(tar.format, tar.encoding, tar.errors)
                padding = (-len(entry.data)) % tarfile.BLOCKSIZE
                tar.fileobj.write(header + entry.data + bytes(padding))
                tar.offset += len(header) + len(entry.data) + padding
    return path


# --------------------------------------------------------------------------------------------
# zip
# --------------------------------------------------------------------------------------------

_ZIP_TYPES = {
    "file": stat.S_IFREG, "dir": stat.S_IFDIR, "symlink": stat.S_IFLNK, "fifo": stat.S_IFIFO,
    "chardev": stat.S_IFCHR, "blockdev": stat.S_IFBLK,
}


def make_zip(path: str | Path, files: Mapping[str, Content] | None = None,
             symlinks: Mapping[str, str] | None = None, modes: Mapping[str, int] | None = None,
             compression: int = zipfile.ZIP_DEFLATED, *, dirs: Iterable[str] | None = None,
             entries: Sequence[Entry] | None = None, links_first: bool = False,
             unix: bool = True) -> Path:
    """Write a zip archive. ``unix=False``: like a zip made on Windows - no permissions, no
    symlinks (``external_attr`` is 0; symlink entries are not allowed then)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lies: list[tuple[zipfile.ZipInfo, Entry]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)     # duplicate names are on purpose
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for entry in _entries(files, symlinks, modes, None, dirs, entries, links_first):
                assert entry.kind != "hardlink", "zip archives have no hard links"
                name = entry.name + "/" if entry.kind == "dir" and not entry.name.endswith("/") \
                    else entry.name
                info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
                info.compress_type = zipfile.ZIP_STORED if entry.kind == "dir" else compression
                if unix:
                    info.create_system = 3
                    info.external_attr = (_ZIP_TYPES[entry.kind] | entry.mode) << 16
                    if entry.kind == "dir":
                        info.external_attr |= 0x10
                else:
                    assert entry.kind in ("file", "dir"), "a zip without Unix modes has only files"
                    info.create_system = 0
                    info.external_attr = 0x10 if entry.kind == "dir" else 0
                data = entry.target.encode() if entry.kind == "symlink" else entry.data
                external_attr = info.external_attr
                archive.writestr(info, data)      # fills in 0600 where there is no mode
                if entry.declared_size is not None or entry.encrypted:
                    lies.append((info, entry))
                info.external_attr = external_attr
            # The central directory is written on close - with whatever the infos say by then.
            for info, entry in lies:
                if entry.declared_size is not None:
                    info.file_size = entry.declared_size
                if entry.encrypted:
                    info.flag_bits |= 0x1
    return path


def make_archive(path: str | Path, files: Mapping[str, Content] | None = None,
                 symlinks: Mapping[str, str] | None = None, modes: Mapping[str, int] | None = None,
                 **kwargs: object) -> Path:
    """:func:`make_zip` or :func:`make_tar`, whatever the file name says."""
    path = Path(path)
    if path.name.lower().endswith(".zip"):
        return make_zip(path, files, symlinks, modes, **kwargs)  # type: ignore[arg-type]
    return make_tar(path, files, symlinks, modes, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# Realistic app layouts (keyword arguments for make_archive / make_tar / make_zip)
# --------------------------------------------------------------------------------------------

def with_top(tree: Mapping[str, Mapping], top: str | None) -> dict[str, dict]:
    """Put a whole layout into one top-level folder (symlink targets stay as they are)."""
    if not top:
        return {key: dict(value) for key, value in tree.items()}
    return {key: {f"{top}/{name}": value for name, value in mapping.items()}
            for key, mapping in tree.items()}


def jetbrains_tree(top: str | None = "idea-IU-241.18034.62", *, version: str = "2024.1.4",
                   launcher: str = "bin/idea.sh") -> dict[str, dict]:
    """IntelliJ IDEA: ``bin/idea.sh``, ``bin/idea.svg``, ``product-info.json``, no .desktop."""
    product = {
        "name": "IntelliJ IDEA", "version": version, "buildNumber": "241.18034.62",
        "productCode": "IU", "dataDirectoryName": "IntelliJIdea2024.1",
        "svgIconPath": "bin/idea.svg", "productVendor": "JetBrains",
        "launch": [
            {"os": "Windows", "arch": "amd64", "launcherPath": "bin/idea64.exe"},
            {"os": "Linux", "arch": "amd64", "launcherPath": launcher,
             "javaExecutablePath": "jbr/bin/java", "vmOptionsFilePath": "bin/idea64.vmoptions",
             "startupWmClass": "jetbrains-idea"},
        ],
    }
    files = {
        "product-info.json": json.dumps(product, indent=2),
        "build.txt": "IU-241.18034.62",
        "bin/idea.sh": fake_script('exec "$IDE_BIN_HOME/../jbr/bin/java" "$@"\n'),
        "bin/idea": fake_elf(),
        "bin/idea.svg": make_svg(128, 128),
        "bin/idea.png": make_png(128, 128),
        "bin/idea64.vmoptions": "-Xmx2048m\n",
        "bin/fsnotifier": fake_elf(),
        "bin/restarter": fake_elf(),
        "bin/format.sh": fake_script(),
        "bin/inspect.sh": fake_script(),
        "bin/libdbm.so": fake_elf(),
        "jbr/bin/java": fake_elf(),
        "lib/app.jar": b"PK\x03\x04 not really a jar",
        "plugins/terminal/lib/terminal.jar": b"PK\x03\x04",
    }
    modes = {name: 0o755 for name in ("bin/idea.sh", "bin/idea", "bin/fsnotifier", "bin/restarter",
                                      "bin/format.sh", "bin/inspect.sh", "bin/libdbm.so",
                                      "jbr/bin/java")}
    return with_top({"files": files, "modes": modes, "symlinks": {}}, top)


def firefox_tree(top: str | None = "firefox", *, version: str = "128.0.3") -> dict[str, dict]:
    """Firefox: ``firefox``, ``firefox-bin``, ``application.ini``, icons deep in the tree."""
    files = {
        "application.ini": (
            "[App]\nVendor=Mozilla\nName=Firefox\nRemotingName=firefox\nCodeName=Firefox\n"
            f"Version={version}\nBuildID=20240725162350\n\n[Gecko]\nMinVersion={version}\n"),
        "firefox": fake_elf(),
        "firefox-bin": fake_elf(),
        "crashreporter": fake_elf(),
        "updater": fake_elf(),
        "pingsender": fake_elf(),
        "plugin-container": fake_elf(),
        "glxtest": fake_elf(),
        "libxul.so": fake_elf(size=4096),
        "libmozgtk.so": fake_elf(),
        "omni.ja": b"PK\x03\x04",
        "browser/omni.ja": b"PK\x03\x04",
        "browser/chrome/icons/default/default16.png": make_png(16, 16),
        "browser/chrome/icons/default/default32.png": make_png(32, 32),
        "browser/chrome/icons/default/default48.png": make_png(48, 48),
        "browser/chrome/icons/default/default64.png": make_png(64, 64),
        "browser/chrome/icons/default/default128.png": make_png(128, 128),
        "defaults/pref/channel-prefs.js": 'pref("app.update.channel", "release");\n',
    }
    modes = {name: 0o755 for name in ("firefox", "firefox-bin", "crashreporter", "updater",
                                      "pingsender", "plugin-container", "glxtest", "libxul.so",
                                      "libmozgtk.so")}
    return with_top({"files": files, "modes": modes, "symlinks": {}}, top)


BLENDER_DESKTOP = (
    "[Desktop Entry]\n"
    "Name=Blender\n"
    "GenericName=3D modeler\n"
    "GenericName[de]=3D-Modellierer\n"
    "Comment=3D modeling, animation, rendering and post-production\n"
    "Comment[de]=3D-Modellierung, Animation, Rendering und Nachbearbeitung\n"
    "Keywords=3d;cg;modeling;animation;painting;sculpting;texturing;video editing;\n"
    "Exec=blender %f\n"
    "Icon=blender\n"
    "Terminal=false\n"
    "Type=Application\n"
    "PrefersNonDefaultGPU=true\n"
    "Categories=Graphics;3DGraphics;\n"
    "MimeType=application/x-blender;\n"
)


def blender_tree(top: str | None = "blender-4.2.0-linux-x64") -> dict[str, dict]:
    """Blender: ``blender``, ``blender.desktop``, ``blender.svg`` and a versioned data folder."""
    files = {
        "blender": fake_elf(size=8192),
        "blender-launcher": fake_elf(),
        "blender-softwaregl": fake_script('exec "$(dirname "$0")/blender" "$@"\n'),
        "blender-thumbnailer": fake_elf(),
        "blender.desktop": BLENDER_DESKTOP,
        "blender.svg": make_svg(None, None, view_box="0 0 64 64"),
        "blender-symbolic.svg": make_svg(16, 16),
        "copyright.txt": "Blender Foundation\n",
        "readme.html": "<html></html>\n",
        "4.2/datafiles/icons/ops.generic.select.dat": b"\0" * 64,
        "4.2/datafiles/studiolights/world/city.exr": b"\x76\x2f\x31\x01",
        "4.2/python/bin/python3.11": fake_elf(),
        "lib/libcycles_kernel_oneapi_aot.so": fake_elf(),
    }
    modes = {name: 0o755 for name in ("blender", "blender-launcher", "blender-softwaregl",
                                      "blender-thumbnailer", "4.2/python/bin/python3.11",
                                      "lib/libcycles_kernel_oneapi_aot.so")}
    return with_top({"files": files, "modes": modes, "symlinks": {}}, top)


def electron_tree(top: str | None = "MyApp-linux-x64", *, program: str = "my-app",
                  absolute_link: bool = True) -> dict[str, dict]:
    """An Electron app in one folder, modelled on balenaEtcher's zip: the program, Chromium's
    helpers, a ``version`` file that holds the version of Electron, no icon outside
    ``app.asar`` - and a symlink with an absolute target from the build machine."""
    files = {
        program: fake_elf(size=16384),
        "chrome-sandbox": fake_elf(),
        "chrome_crashpad_handler": fake_elf(),
        "libEGL.so": fake_elf(),
        "libffmpeg.so": fake_elf(),
        "libvulkan.so.1": fake_elf(),
        "icudtl.dat": b"\0" * 1024,
        "resources.pak": b"\0" * 512,
        "version": "37.2.4",
        "LICENSE": "MIT\n",
        "locales/en-US.pak": b"\0" * 128,
        "locales/de.pak": b"\0" * 128,
        "resources/app.asar": b"\x04\0\0\0" + b"\0" * 252,
        "resources/helper-util": fake_elf(),
    }
    modes = {name: 0o755 for name in (program, "chrome-sandbox", "chrome_crashpad_handler",
                                      "libEGL.so", "libffmpeg.so", "libvulkan.so.1",
                                      "resources/helper-util")}
    symlinks = {"MyApp": f"/home/runner/work/out/MyApp-linux-x64/{program}"} if absolute_link else {}
    return with_top({"files": files, "modes": modes, "symlinks": symlinks}, top)


def flat_tree(name: str = "UVtools") -> dict[str, dict]:
    """A .NET app zipped without a top folder, every file marked executable (like UVtools)."""
    files = {
        f"{name}": fake_elf(size=4096),
        f"{name}.sh": fake_script(f'exec "$(dirname "$0")/{name}" "$@"\n', "/usr/bin/env bash"),
        f"{name}Cmd": fake_elf(size=4096),
        f"{name}.dll": b"MZ\x90\0" + b"\0" * 124,
        f"{name}.deps.json": "{}\n",
        f"{name}.runtimeconfig.json": "{}\n",
        "createdump": fake_elf(),
        "libcoreclr.so": fake_elf(),
        "libSkiaSharp.so": fake_elf(),
        "System.Private.CoreLib.dll": b"MZ\x90\0" + b"\0" * 124,
        "LICENSE": "AGPL\n",
        "de/Microsoft.CodeAnalysis.resources.dll": b"MZ\x90\0" + b"\0" * 60,
        "Assets/PrusaSlicer/printer/Some Printer.ini": "[printer]\n",
    }
    return {"files": files, "modes": {name: 0o777 for name in files}, "symlinks": {}}


def libraries_tree(top: str | None = "libfoo-1.2.3") -> dict[str, dict]:
    """Not an app: only libraries, headers and documentation."""
    files = {
        "lib/libfoo.so.1.2.3": fake_elf(),
        "lib/libbar.so": fake_elf(),
        "libbaz.so": fake_elf(),
        "plugin.node": fake_elf(),
        "foo.o": fake_elf(e_type=ET_REL),
        "include/foo.h": "#pragma once\n",
        "README.md": "# libfoo\n",
    }
    modes = {name: 0o755 for name in ("lib/libfoo.so.1.2.3", "lib/libbar.so", "libbaz.so",
                                      "plugin.node", "foo.o")}
    symlinks = {"lib/libfoo.so.1": "libfoo.so.1.2.3", "lib/libfoo.so": "libfoo.so.1"}
    return with_top({"files": files, "modes": modes, "symlinks": symlinks}, top)
