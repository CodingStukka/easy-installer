"""Read files out of an AppImage payload without running the app.

The preferred reader is :class:`UnsquashfsReader`, which uses ``unsquashfs -o <offset>`` on the
squashfs image embedded after the ELF runtime. Only if ``unsquashfs`` is not available (or the
payload is not squashfs) :class:`ExtractCommandReader` asks the AppImage's own runtime to extract
files (``<file> --appimage-extract <pattern>``) - that executes the runtime, never the app.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from ..errors import ExtractionError, NotAnAppImageError
from ..i18n import _
from .elf import ElfInfo, payload_magic

log = logging.getLogger(__name__)

SQUASHFS_MAGIC = b"hsqs"
LIST_PREFIX = "squashfs-root"
UNSQUASHFS_TIMEOUT = 300.0
EXTRACT_TIMEOUT = 60.0

# Environment variables that change what an AppImage runtime does (or which file it reads).
_RUNTIME_ENV_BLOCKLIST = ("APPIMAGE", "TARGET_APPIMAGE", "APPDIR", "ARGV0", "OWD")
_GLOB_SPECIAL = set("\\*?[]()+@!")


def damaged_file_message() -> str:
    return _("The file seems damaged or incomplete. Try downloading it again.")


def find_unsquashfs() -> str | None:
    return shutil.which("unsquashfs")


def escape_pattern(path: str) -> str:
    """Escape glob characters so ``path`` is matched literally by unsquashfs / the runtime."""
    return "".join("\\" + c if c in _GLOB_SPECIAL else c for c in path)


class PayloadReader(ABC):
    @abstractmethod
    def list_members(self) -> list[str] | None:
        """All paths relative to the AppImage root, or None if the reader cannot list."""

    @abstractmethod
    def extract(self, members: Sequence[str], dest: Path) -> None:
        """Extract paths/glob patterns into ``dest``; missing members are skipped silently.

        Symlinks are extracted as symlinks and never followed.
        """

    def member_sizes(self) -> dict[str, int] | None:
        """Sizes of the regular files among :meth:`list_members`, or None if unknown."""
        return None


# --------------------------------------------------------------------------------------------
# unsquashfs
# --------------------------------------------------------------------------------------------

_HELP_OPTION_RE = re.compile(r"^\s*(-[A-Za-z0-9-]+)(?:\[([A-Za-z0-9-]+)\])?", re.MULTILINE)
#: A line of ``unsquashfs -lln``: mode, uid/gid, size (devices: "major,minor"), date, time, path.
_LONG_LIST_RE = re.compile(
    rb"(?P<mode>\S{10}) +\S+ +(?P<size>\d+|\d+, *\d+) +\S+ +\S+ (?P<path>.*)\Z", re.DOTALL)


@functools.lru_cache(maxsize=8)
def _unsquashfs_options(tool: str) -> frozenset[str]:
    """Options listed by ``unsquashfs -help`` (both abbreviated and full spellings)."""
    try:
        proc = subprocess.run(
            [tool, "-help"], stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            env=_c_locale_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("could not query %s -help: %s", tool, exc)
        return frozenset()
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    options: set[str] = set()
    for match in _HELP_OPTION_RE.finditer(text):
        options.add(match.group(1))
        if match.group(2):
            options.add(match.group(1) + match.group(2))
    return frozenset(options)


def _c_locale_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def _decode_stderr(data: bytes, limit: int = 4000) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return text[-limit:]


class UnsquashfsReader(PayloadReader):
    def __init__(self, path: Path, offset: int, tool: str | None = None,
                 timeout: float = UNSQUASHFS_TIMEOUT):
        self.path = Path(os.path.abspath(path))
        self.offset = int(offset)
        tool = tool or find_unsquashfs()
        if tool is None:
            raise ExtractionError(
                _("A system component needed to read apps (squashfs-tools) is missing."),
                "unsquashfs not found on PATH",
            )
        self.tool = tool
        self.timeout = timeout
        self._members: list[str] | None = None
        self._sizes: dict[str, int] | None = None

    def _run(self, args: Sequence[str]) -> bytes:
        cmd = [self.tool, "-o", str(self.offset), *args]
        log.debug("running %s", cmd)
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout,
                env=_c_locale_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractionError(
                _("Reading the app took too long. The file may be damaged."), str(exc)
            ) from exc
        except OSError as exc:
            raise ExtractionError(
                _("A system component needed to read apps (squashfs-tools) could not be started."),
                str(exc),
            ) from exc
        if proc.returncode != 0:
            raise ExtractionError(
                damaged_file_message(),
                f"{' '.join(cmd)} exited with {proc.returncode}: {_decode_stderr(proc.stderr)}",
            )
        return proc.stdout

    def _options(self, *wanted: str) -> list[str]:
        known = _unsquashfs_options(self.tool)
        # If -help could not be parsed, assume a modern unsquashfs.
        return [opt for opt in wanted if not known or opt in known]

    def list_members(self) -> list[str] | None:
        if self._members is None:
            long_list = self._options("-lln")
            if long_list:
                parsed = self._parse_long_list(
                    self._run([*long_list, "-d", LIST_PREFIX, str(self.path)]))
                if parsed is not None:
                    self._members, self._sizes = parsed
                    return list(self._members)
            out = self._run(["-l", "-d", LIST_PREFIX, str(self.path)])
            prefix = LIST_PREFIX + "/"
            members = []
            for raw in out.split(b"\n"):
                line = os.fsdecode(raw)
                if line.startswith(prefix) and len(line) > len(prefix):
                    members.append(line[len(prefix):])
            self._members = members
        return list(self._members)

    def member_sizes(self) -> dict[str, int] | None:
        self.list_members()
        return None if self._sizes is None else dict(self._sizes)

    @staticmethod
    def _parse_long_list(out: bytes) -> tuple[list[str], dict[str, int]] | None:
        """Members and regular file sizes from ``-lln`` output; None if a line is not understood.

        Knowing the sizes lets the inspector skip huge members *before* extracting them.
        """
        prefix = LIST_PREFIX.encode() + b"/"
        members: list[str] = []
        sizes: dict[str, int] = {}
        for line in out.split(b"\n"):
            if not line.strip():
                continue
            match = _LONG_LIST_RE.match(line)
            if match is None:
                log.debug("unexpected unsquashfs listing line %r", line[:200])
                return None
            mode, path = match.group("mode"), match.group("path")
            if mode.startswith(b"l"):
                # "<name> -> <target>", and the size is the target's length in bytes
                size = int(match.group("size"))
                arrow = len(path) - size - len(b" -> ")
                if arrow < 0 or path[arrow:arrow + 4] != b" -> ":
                    return None
                path = path[:arrow]
            if not path.startswith(prefix) or len(path) == len(prefix):
                continue  # the root itself
            name = os.fsdecode(path[len(prefix):])
            members.append(name)
            if mode.startswith(b"-"):
                sizes[name] = int(match.group("size"))
        return members, sizes

    def extract(self, members: Sequence[str], dest: Path) -> None:
        members = [m for m in members if m]
        if not members:
            return
        dest = Path(os.path.abspath(dest))
        dest.mkdir(parents=True, exist_ok=True)
        args = ["-d", str(dest), "-f"]
        args += self._options("-no-xattrs", "-no-progress", "-q")
        self._run([*args, str(self.path), *members])


# --------------------------------------------------------------------------------------------
# <appimage> --appimage-extract fallback
# --------------------------------------------------------------------------------------------

def sanitized_runtime_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of the environment without variables that alter the AppImage runtime's behaviour."""
    source = dict(os.environ if env is None else env)
    return {
        key: value for key, value in source.items()
        if not any(key == blocked or key.startswith(blocked + "_") for blocked in _RUNTIME_ENV_BLOCKLIST)
    }


class ExtractCommandReader(PayloadReader):
    """Runs ``<appimage> --appimage-extract <pattern>`` once per member in a private temp dir.

    ``command`` overrides the executable (tests use an emulator script); by default the
    AppImage file itself is run, which requires its exec bit (added with ``chmod u+x``).
    """

    def __init__(self, path: Path, command: Sequence[str] | None = None,
                 timeout: float = EXTRACT_TIMEOUT):
        self.path = Path(os.path.abspath(path))
        self.command = list(command) if command else None
        self.timeout = timeout

    def list_members(self) -> list[str] | None:
        return None

    def _ensure_executable(self) -> None:
        try:
            st = os.stat(self.path)
            if not st.st_mode & stat.S_IXUSR:
                log.info("adding the exec bit to %s for --appimage-extract", self.path)
                os.chmod(self.path, stat.S_IMODE(st.st_mode) | stat.S_IXUSR)
        except OSError as exc:
            raise ExtractionError(
                _("The app file could not be prepared for reading. Check that you are allowed to change it."),
                str(exc),
            ) from exc

    def _run_once(self, member: str, cwd: Path) -> None:
        cmd = [*(self.command or [str(self.path)]), "--appimage-extract", member]
        log.debug("running %s in %s", cmd, cwd)
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=sanitized_runtime_env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            raise ExtractionError(
                _("The app could not be read. It may be damaged or made for a different kind of computer."),
                str(exc),
            ) from exc
        try:
            _out, err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            raise ExtractionError(
                _("Reading the app took too long. The file may be damaged."), str(exc)
            ) from exc
        if proc.returncode != 0:
            raise ExtractionError(
                damaged_file_message(),
                f"{' '.join(cmd)} exited with {proc.returncode}: {_decode_stderr(err)}",
            )

    def extract(self, members: Sequence[str], dest: Path) -> None:
        members = [m for m in members if m]
        if not members:
            return
        if self.command is None:
            self._ensure_executable()
        dest = Path(os.path.abspath(dest))
        dest.mkdir(parents=True, exist_ok=True)
        for member in members:
            scratch = Path(tempfile.mkdtemp(prefix=".extract-", dir=dest.parent))
            try:
                self._run_once(member, scratch)
                extracted = scratch / "squashfs-root"
                if extracted.is_dir() and not extracted.is_symlink():
                    _merge_tree(extracted, dest)
            finally:
                remove_tree(scratch)


def _remove_path(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        remove_tree(Path(path))
    elif os.path.lexists(path):
        os.unlink(path)


def _merge_tree(src: Path, dest: Path) -> None:
    """Move the contents of ``src`` into ``dest`` without ever following symlinks in ``dest``."""
    for entry in os.scandir(src):
        target = os.path.join(dest, entry.name)
        if entry.is_dir(follow_symlinks=False) and os.path.isdir(target) and not os.path.islink(target):
            _merge_tree(Path(entry.path), Path(target))
            continue
        _remove_path(target)
        os.rename(entry.path, target)


def remove_tree(path: Path) -> None:
    """``rm -rf`` that also copes with read-only directories extracted from a payload."""
    path_str = os.fspath(path)
    if not os.path.lexists(path_str):
        return
    if os.path.islink(path_str) or not os.path.isdir(path_str):
        try:
            os.unlink(path_str)
        except OSError as exc:
            log.warning("could not remove %s: %s", path_str, exc)
        return
    try:
        os.chmod(path_str, 0o700)
    except OSError:
        pass
    for dirpath, dirnames, _files in os.walk(path_str):
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if not os.path.islink(sub):
                try:
                    os.chmod(sub, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path_str, ignore_errors=True)
    if os.path.lexists(path_str):
        log.warning("could not completely remove %s", path_str)


# --------------------------------------------------------------------------------------------

def check_squashfs_complete(path: Path, offset: int) -> None:
    """Raise :class:`ExtractionError` if the squashfs superblock says the file is truncated."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            superblock = fh.read(96)
            size = os.fstat(fh.fileno()).st_size
    except OSError as exc:
        raise ExtractionError(_("The file could not be read."), str(exc)) from exc
    if len(superblock) < 96 or superblock[:4] != SQUASHFS_MAGIC:
        raise ExtractionError(damaged_file_message(), "squashfs superblock is incomplete")
    (major,) = struct.unpack_from("<H", superblock, 28)
    (bytes_used,) = struct.unpack_from("<Q", superblock, 40)
    if major != 4:
        raise ExtractionError(damaged_file_message(), f"unsupported squashfs version {major}")
    if offset + bytes_used > size:
        raise ExtractionError(
            damaged_file_message(),
            f"squashfs needs {offset + bytes_used} bytes but the file has only {size}",
        )


def open_payload(path: Path, elf: ElfInfo) -> PayloadReader:
    magic = payload_magic(path, elf)
    if magic == SQUASHFS_MAGIC:
        check_squashfs_complete(path, elf.payload_offset)
        tool = find_unsquashfs()
        if tool:
            return UnsquashfsReader(path, elf.payload_offset, tool)
    if elf.appimage_type in (1, 2):
        log.info("using the AppImage runtime to read %s (unsquashfs unavailable or not squashfs)", path)
        return ExtractCommandReader(path)
    raise NotAnAppImageError(_("This file is not an AppImage."), f"payload magic {magic!r}")
