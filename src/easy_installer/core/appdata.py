"""Find an app's personal settings and data folders, and move them to the trash (per-user only).

Losing someone's files is the worst thing this program could do, so everything here is
deliberately narrow:

* Only **direct children** of ``$XDG_CONFIG_HOME``, ``$XDG_DATA_HOME``, ``$XDG_CACHE_HOME``,
  ``$XDG_STATE_HOME`` and ``~/.<name>`` are ever considered, and only when the folder's name
  *equals* one of the app's names (ignoring case). Names are never used as patterns and are
  never split into words.
* Short names (< 3 characters) and a list of shared or generic names (``icons``, ``mozilla``,
  ``ssh``, ``easy-installer`` ...) never match.
* A folder is skipped when it is a symbolic link, is not a real folder, belongs to someone else,
  lies on another file system, is not inside the real home folder, or contains one of the places
  Easy Installer or the desktop itself needs (the apps folder, the registry, launchers, the XDG
  folders themselves, anything the registry lists as installed ...).
* Nothing is deleted. :func:`move_to_trash` moves folders to the trash (``gio trash``, else the
  FreeDesktop trash specification for the home trash); what cannot be trashed is left alone and
  reported.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..i18n import _
from . import paths as _paths

log = logging.getLogger(__name__)

#: kinds in the order in which locations are returned
KINDS = ("config", "data", "cache", "state", "home")

MIN_HINT_LENGTH = 3
MAX_HINT_LENGTH = 100           # characters; a folder name has at most 255 bytes
MAX_HINTS = 24

MAX_BASE_ENTRIES = 20_000       # children of one base folder that are looked at
MAX_SCAN_ENTRIES = 200_000      # dir_size(): entries looked at per call ...
MAX_SCAN_SECONDS = 10.0         # ... and the time spent on them; the result is a lower bound then

MAX_REGISTRY_SIZE = 16 * 1024 * 1024

TRASH_TIMEOUT = 60.0            # seconds for one `gio trash`
_NAME_MAX = 255

#: endings of program names that are not part of the app's name ("UVtools.sh" -> "UVtools")
_EXEC_SUFFIXES = (".sh", ".bin", ".run", ".py", ".appimage", ".x86_64", ".amd64", ".aarch64")

#: Electron's updater keeps downloads in ``~/.cache/<name>-updater``
_UPDATER_SUFFIX = "-updater"

# Names that are shared between programs, belong to the desktop, hold keys and passwords, or are
# so generic that a folder with that name says nothing about who owns it. Compared casefolded.
_DENIED_NAMES = frozenset(name.casefold() for name in (
    # the layout itself and generic words
    "share", "local", "config", "cache", "state", "data", "home", "user", "users", "root",
    "tmp", "temp", "var", "etc", "usr", "opt", "bin", "sbin", "lib", "lib64", "run", "dev",
    "proc", "sys", "mnt", "media", "srv", "snap", "applications", "application", "app", "apps",
    "appimage", "appimages", "apprun", "default", "defaults", "desktop", "documents", "downloads",
    "music", "pictures", "videos", "public", "templates", "settings", "preferences", "profile",
    "profiles", "backup", "backups", "log", "logs", "history", "session", "sessions", "runtime",
    "launcher", "launch", "start", "main", "program", "programs", "linux", "unix", "install",
    "installer", "setup", "update", "updater", "updates", "uninstall", "unknown", "none", "null",
    "test", "tests", "new", "old", "files", "file", "folder",
    # shared desktop infrastructure
    "icons", "pixmaps", "mime", "fonts", "fontconfig", "themes", "sounds", "backgrounds",
    "wallpapers", "locale", "man", "doc", "docs", "info", "menus", "autostart",
    "desktop-directories", "trash", "systemd", "dconf", "gconf", "glib-2.0", "gtk-2.0", "gtk-3.0",
    "gtk-4.0", "qt5ct", "qt6ct", "kvantum", "pulse", "pipewire", "wireplumber", "dbus-1", "ibus",
    "ibus-table", "fcitx", "fcitx5", "enchant", "gvfs", "gvfs-metadata", "tracker", "tracker3",
    "thumbnails", "keyrings", "goa-1.0", "evolution", "folks", "grilo-plugins", "icc", "vulkan",
    "xorg", "mesa_shader_cache", "gstreamer-1.0", "obexd", "gnome", "gnome-shell",
    "gnome-session", "gnome-settings-daemon", "gnome-software", "gnome-remote-desktop",
    "gnome-control-center", "gnome-online-accounts", "gnome-desktop-thumbnailer", "gsd",
    "nautilus", "nautilus-python", "kde", "kdedefaults", "kdeglobals", "plasma", "xfce4",
    "cinnamon", "mate", "lxqt", "zorin", "update-notifier", "procps", "xdg-desktop-portal",
    "environment.d", "flatpak", "snapd", "containers", "webkitgtk", "tauri", "cef_user_data",
    # keys, passwords, accounts
    "ssh", "gnupg", "pki", "cert", "certs", "password-store", "aws", "azure", "gcloud", "kube",
    "docker", "netrc", "git",
    # web browsers and mail (profiles with passwords and bookmarks)
    "google-chrome", "google-chrome-beta", "google-chrome-unstable", "chromium",
    "chromium-browser", "mozilla", "firefox", "thunderbird", "bravesoftware", "microsoft",
    "microsoft-edge", "vivaldi", "opera", "epiphany",
    # programming tools, shells and command-line programs that own a folder of that name
    "python", "python3", "pip", "pipx", "conda", "node", "nodejs", "node-gyp", "npm", "nvm",
    "pnpm", "yarn", "bun", "deno", "cargo", "rustup", "java", "gradle", "maven", "golang",
    "dotnet", "nuget", "mono", "wine", "winetricks", "steam", "bash", "zsh", "fish", "oh-my-zsh",
    "vim", "nvim", "emacs", "nano", "tmux", "claude", "codex", "gemini", "huggingface",
    # what Electron calls an app that has no name of its own
    "electron",
    # Easy Installer itself
    "easy-installer", "easyinstaller", "easy_installer", "com.roothirsch.easyinstaller",
))

#: one part of a reverse-DNS id; also what a plain program name looks like
_REVERSE_DNS_PART = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class DataLocation:
    """One folder with an app's personal data. ``kind``: config | data | cache | state | home."""

    path: Path
    kind: str
    size: int
    file_count: int


# -- hints ------------------------------------------------------------------------------------------


def _clean_hint(value: object) -> str | None:
    """``value`` as a folder name that may be searched for, or None if it must never match."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value).strip()
    if not MIN_HINT_LENGTH <= len(text) <= MAX_HINT_LENGTH:
        return None
    if text.startswith(".") or "/" in text or "\\" in text:
        return None
    # No control, format (bidi), surrogate or unassigned characters: a name is a plain word.
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        return None
    if not any(ch.isalpha() for ch in text):
        return None                       # "1.2.3", "2026", "---" name no app
    if len(os.fsencode(text)) > _NAME_MAX - 1:
        return None
    if text.casefold() in _DENIED_NAMES:
        return None
    return text


def _clean_hints(values: Iterable[object]) -> list[str]:
    """Usable hints in their first spelling, without duplicates (case is ignored)."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        hint = _clean_hint(value)
        if hint is None or hint.casefold() in seen:
            continue
        seen.add(hint.casefold())
        result.append(hint)
        if len(result) >= MAX_HINTS:
            break
    return result


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _last_component(identifier: str) -> str | None:
    """"org.freecad.FreeCAD" -> "FreeCAD"; None unless ``identifier`` looks like a reverse-DNS id."""
    parts = identifier.split(".")
    if len(parts) < 3 or not all(_REVERSE_DNS_PART.match(part) for part in parts):
        return None
    if not parts[0].isalpha() or not parts[-1][0].isalpha():
        return None                       # "app-1.2.3" is a version, not an id
    return parts[-1]


def _id_variants(value: object) -> list[str]:
    """An app id, desktop file stem or window class, plus the app's name inside a reverse-DNS id."""
    text = _text(value)
    if not text:
        return []
    # "Keep both" copies are called "<id>--<version>": their data is the main app's.
    text = text.split("--", 1)[0]
    variants = [text]
    last = _last_component(text)
    if last:
        variants.append(last)
    return variants


def _exec_variants(value: object) -> list[str]:
    text = _text(value)
    if not text:
        return []
    name = text.rstrip("/").rsplit("/", 1)[-1]
    lowered = name.lower()
    for suffix in _EXEC_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return [name]


def data_hints_for(*, name: str | None, app_id: str | None, desktop_stem: str | None,
                   wm_class: str | None, exec_name: str | None) -> list[str]:
    """Folder names under which an app may keep its settings and data.

    The names are taken as they are (the app's ``Name``, its id, the stem of its desktop file,
    ``StartupWMClass``, the program's file name) plus two safe variants: the last part of a
    reverse-DNS id ("org.freecad.FreeCAD" -> "FreeCAD") and "<name>-updater" (the download cache
    of Electron apps). Names are never split into words. Names that are too short or too generic
    are dropped; see :func:`find_app_data` for how the rest is used.
    """
    candidates: list[str] = []
    if _text(name):
        candidates.append(_text(name))
    short_names: list[str] = []
    for value in (app_id, desktop_stem, wm_class):
        variants = _id_variants(value)
        candidates += variants
        if len(variants) == 1:            # plain names only ("pen"), no reverse-DNS ids
            short_names += variants
    for variant in _exec_variants(exec_name):
        candidates.append(variant)
        short_names.append(variant)
    for short in short_names:
        # Only program-like names ("t3code"): that is what Electron's updater uses.
        if _clean_hint(short) is not None and _REVERSE_DNS_PART.match(short):
            candidates.append(short + _UPDATER_SUFFIX)
    return _clean_hints(candidates)


# -- where to look ----------------------------------------------------------------------------------


def _state_home(env: Mapping[str, str]) -> Path:
    value = env.get("XDG_STATE_HOME")
    if value and os.path.isabs(value):
        return Path(value)
    return _paths.home_dir(env) / ".local" / "state"


def _real(path: str | os.PathLike) -> str:
    return os.path.realpath(os.fspath(path))


def _is_inside(path: str, parent: str) -> bool:
    """``path`` lies strictly below ``parent`` (both absolute, symlinks already resolved)."""
    if path == parent:
        return False
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return path.startswith(prefix)


def _owned_dir(path: str) -> os.stat_result | None:
    """lstat of ``path`` if it is a real folder (no symlink) that belongs to this user."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        return None
    return st


def _real_home(env: Mapping[str, str]) -> str | None:
    """The user's real home folder, or None if there is none we may look into."""
    home = os.fspath(_paths.home_dir(env))
    if not os.path.isabs(home):
        return None
    real = _real(home)
    if real == os.sep or _owned_dir(real) is None:
        return None
    return real


@dataclass(frozen=True)
class _Places:
    """The folders that are searched and the ones that must never be touched."""

    home: str
    bases: tuple[tuple[str, str], ...]   # (kind, real path) of the XDG folders inside the home
    containers: frozenset[str]           # never returned, nor a folder that contains one of them
    owned: frozenset[str]                # ... and additionally nothing inside these


def _registered_paths(registry_path: Path) -> list[str]:
    """Every file and folder the per-user registry names (apps, launchers, icons, backups).

    Read directly and read-only (``Registry.load`` would move a damaged file aside): whatever
    is installed must never end up inside a folder that is offered for removal - e.g. when the
    apps folder was somewhere else at the time.
    """
    try:
        if os.path.getsize(registry_path) > MAX_REGISTRY_SIZE:
            return []
        with open(registry_path, encoding="utf-8", errors="surrogateescape") as handle:
            apps = json.load(handle).get("apps")
    except (OSError, ValueError, AttributeError):
        return []
    found: list[str] = []
    for app in apps.values() if isinstance(apps, dict) else ():
        if not isinstance(app, dict):
            continue
        previous = app.get("previous")
        values = [app.get("appimage_path"), app.get("install_dir"), app.get("desktop_path"),
                  app.get("mime_package"), previous.get("path") if isinstance(previous, dict) else None]
        icons = app.get("icon_paths")
        values += icons if isinstance(icons, list) else []
        found += [value for value in values
                  if isinstance(value, str) and os.path.isabs(value) and "\0" not in value]
    return found


def _places(env: Mapping[str, str], exclude: Iterable[os.PathLike | str] = ()) -> _Places | None:
    home = _real_home(env)
    if home is None:
        return None
    xdg = (
        ("config", _paths.config_home(env)),
        ("data", _paths.data_home(env)),
        ("cache", _paths.cache_home(env)),
        ("state", _state_home(env)),
    )
    bases: list[tuple[str, str]] = []
    containers = {home, _real(os.path.join(home, ".local"))}
    for kind, folder in xdg:
        real = _real(folder)
        containers.add(real)
        # An XDG folder outside the home (or the home itself) is not searched at all.
        if _is_inside(real, home) and all(real != seen for _kind, seen in bases):
            bases.append((kind, real))

    layout = _paths.user_layout(env)
    data = _paths.data_home(env)
    owned = {
        _real(layout.apps_dir),
        _real(layout.desktop_dir),
        _real(layout.icons_dir.parent),
        _real(layout.mime_dir),
        _real(layout.registry_dir),
        _real(_paths.config_dir(env)),
        _real(_paths.cache_home(env) / "easy-installer"),
        _real(data / "Trash"),
    }
    for item in (*_registered_paths(layout.registry_path), *exclude):
        try:
            owned.add(_real(item))
        except (TypeError, ValueError):
            continue
    return _Places(home=home, bases=tuple(bases), containers=frozenset(containers),
                   owned=frozenset(owned))


def _is_protected(path: str, places: _Places) -> bool:
    """``path`` (real) is, or contains, something that must stay - or lies inside such a folder."""
    if not _is_inside(path, places.home):
        return True
    for keep in places.containers | places.owned:
        if path == keep or _is_inside(keep, path):
            return True
    return any(_is_inside(path, keep) for keep in places.owned)


def _matching_children(base: str, wanted: Mapping[str, str], *, dotted: bool,
                       places: _Places) -> list[str]:
    """Real folders directly in ``base`` whose name is one of ``wanted`` (casefolded names)."""
    base_st = _owned_dir(base)
    if base_st is None:
        return []
    found: list[str] = []
    try:
        with os.scandir(base) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_BASE_ENTRIES:
                    log.warning("%s has too many entries; not all of them were looked at", base)
                    break
                name = entry.name
                if dotted:
                    if not name.startswith(".") or name.startswith(".."):
                        continue
                    name = name[1:]
                if unicodedata.normalize("NFC", name).casefold() not in wanted:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISDIR(st.st_mode):        # also skips symbolic links
                    continue
                if st.st_uid != os.geteuid() or st.st_dev != base_st.st_dev:
                    continue                            # someone else's, or another file system
                path = os.path.join(base, entry.name)
                if os.path.ismount(path) or _is_protected(path, places):
                    continue
                found.append(path)
    except OSError as exc:
        log.debug("cannot list %s: %s", base, exc)
    return sorted(found)


def find_app_data(hints: Sequence[str], *, env: Mapping[str, str] | None = None,
                  exclude: Iterable[Path] = ()) -> list[DataLocation]:
    """The settings and data folders of the app with these names, with their sizes.

    ``hints`` come from :func:`data_hints_for` (they are checked again here, so a registry file
    that was tampered with cannot widen the search). ``exclude``: folders that must not be
    returned, e.g. the app's own install folder. The result is ordered config, data, cache, state,
    home and contains every folder at most once.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if isinstance(hints, str):            # a single name, not its letters
        hints = [hints]
    wanted = {hint.casefold(): hint for hint in _clean_hints(hints)}
    if not wanted:
        return []
    try:
        places = _places(environ, exclude)
    except (OSError, ValueError) as exc:
        log.debug("cannot work out where app data lives: %s", exc)
        return []
    if places is None:
        return []

    deadline = time.monotonic() + MAX_SCAN_SECONDS
    seen: set[str] = set()
    result: list[DataLocation] = []
    searches = [(kind, base, False) for kind, base in places.bases] + [("home", places.home, True)]
    for kind, base, dotted in searches:
        for path in _matching_children(base, wanted, dotted=dotted, places=places):
            if path in seen:
                continue
            seen.add(path)
            size, count, _complete = _scan_tree(path, deadline=deadline)
            result.append(DataLocation(path=Path(path), kind=kind, size=size, file_count=count))
    return result


# -- sizes ------------------------------------------------------------------------------------------


def _scan_tree(path: str | os.PathLike, *, deadline: float | None = None) -> tuple[int, int, bool]:
    """``(bytes, files, complete)`` of a folder tree; see :func:`dir_size`."""
    root = os.fspath(path)
    try:
        root_st = os.lstat(root)
    except (OSError, ValueError):
        return 0, 0, True
    if not stat.S_ISDIR(root_st.st_mode):
        return (root_st.st_size if stat.S_ISREG(root_st.st_mode) or stat.S_ISLNK(root_st.st_mode)
                else 0), 1, True
    if deadline is None:
        deadline = time.monotonic() + MAX_SCAN_SECONDS

    total = files = looked_at = 0
    hard_links: set[tuple[int, int]] = set()
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            entries = os.scandir(folder)
        except OSError as exc:            # unreadable, vanished, name too long
            log.debug("cannot read %s: %s", folder, exc)
            continue
        with entries:
            try:
                for entry in entries:
                    looked_at += 1
                    if looked_at > MAX_SCAN_ENTRIES or (looked_at % 512 == 0
                                                        and time.monotonic() > deadline):
                        log.info("%s is very large; its size is only an estimate", root)
                        return total, files, False
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev == root_st.st_dev:    # never into another file system
                            pending.append(entry.path)
                        continue
                    files += 1
                    if stat.S_ISREG(st.st_mode):
                        if st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in hard_links:
                                continue
                            hard_links.add(key)
                        total += st.st_size
                    elif stat.S_ISLNK(st.st_mode):
                        total += st.st_size            # the link itself, never its target
            except OSError as exc:
                log.debug("reading %s stopped: %s", folder, exc)
    return total, files, True


def dir_size(path: Path) -> tuple[int, int]:
    """``(bytes, files)`` of everything in ``path``.

    Symbolic links are counted as links and never followed, other file systems mounted inside
    are not entered, unreadable folders are skipped and a file with several names is counted
    once. Very large trees are not read to the end (``MAX_SCAN_ENTRIES`` / ``MAX_SCAN_SECONDS``):
    the numbers are a lower bound then. A missing path gives ``(0, 0)``.
    """
    total, files, _complete = _scan_tree(path)
    return total, files


# -- trash ------------------------------------------------------------------------------------------


def _protected_text() -> str:
    return _("This folder is protected and was left alone.")


def _failed_text() -> str:
    return _("It could not be moved to the trash, so it was left alone.")


def _trash_refusal(path: Path, places: _Places | None) -> str | None:
    """Why ``path`` must not be trashed (translated), or None if it may be."""
    text = os.fspath(path)
    if not os.path.isabs(text) or "\0" in text:
        return _protected_text()
    try:
        st = os.lstat(text)
    except FileNotFoundError:
        return _("It no longer exists.")
    except (OSError, ValueError):
        return _failed_text()
    if places is None or stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid():
        return _protected_text()
    # The folder it is in may be reached through links ($HOME itself); the item itself is not
    # resolved - it is what gets moved.
    real = os.path.join(_real(os.path.dirname(text.rstrip(os.sep)) or os.sep),
                        os.path.basename(text.rstrip(os.sep)))
    if os.path.basename(real) in ("", ".", "..") or _is_protected(real, places):
        return _protected_text()
    if os.path.ismount(text):
        return _protected_text()
    return None


def _gio_trash(path: Path) -> bool:
    """Try ``gio trash``; True if the path is gone afterwards."""
    gio = shutil.which("gio")
    if gio is None:
        return False
    try:
        proc = subprocess.run(
            [gio, "trash", "--", os.fspath(path)], stdin=subprocess.DEVNULL,
            capture_output=True, timeout=TRASH_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("gio trash %s took too long", path)
    except (OSError, ValueError) as exc:
        log.debug("gio trash could not be started: %s", exc)
    else:
        if proc.returncode != 0:
            log.debug("gio trash %s failed (%s): %s", path, proc.returncode,
                      proc.stderr.decode("utf-8", "replace").strip())
    return not os.path.lexists(path)


def _trash_name(name: str, attempt: int) -> str:
    """A file name for the trash: ``name``, ``name.2``, ``name.3`` ... (short enough for ".trashinfo")."""
    suffix = "" if attempt == 0 else f".{attempt + 1}"
    room = _NAME_MAX - len(".trashinfo") - len(suffix)
    raw = os.fsencode(name)
    if len(raw) > room:
        raw = raw[:room]
        # never end in the middle of a UTF-8 character
        raw = raw.decode("utf-8", "ignore").encode("utf-8") or raw
    return os.fsdecode(raw) + suffix


def _trash_info(path: str) -> bytes:
    deleted = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    return (f"[Trash Info]\nPath={quote(os.fsencode(path), safe='/')}\n"
            f"DeletionDate={deleted}\n").encode("ascii")


def _spec_trash(path: Path) -> bool:
    """Move ``path`` into the home trash as the FreeDesktop trash specification describes it.

    Only a rename: if the trash is on another file system nothing is copied (and so nothing can
    be lost half-way); the path stays where it is and False is returned.
    """
    source = os.fspath(path)
    trash = _paths.data_home() / "Trash"
    files_dir, info_dir = trash / "files", trash / "info"
    try:
        for folder in (trash, files_dir, info_dir):
            os.makedirs(folder, mode=0o700, exist_ok=True)
        if os.lstat(source).st_dev != os.stat(files_dir).st_dev:
            log.info("the trash is on another file system than %s", source)
            return False
    except OSError as exc:
        log.debug("the trash folder cannot be used: %s", exc)
        return False

    original = os.path.join(_real(os.path.dirname(source.rstrip(os.sep))),
                            os.path.basename(source.rstrip(os.sep)))
    for attempt in range(1000):
        name = _trash_name(os.path.basename(original), attempt)
        info_path = info_dir / f"{name}.trashinfo"
        target = files_dir / name
        try:
            # Creating the info file first (and exclusively) reserves the name.
            fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            log.debug("cannot write %s: %s", info_path, exc)
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(_trash_info(original))
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.lexists(target):
                os.unlink(info_path)
                continue
            os.rename(source, target)
        except OSError as exc:
            log.debug("cannot move %s to the trash: %s", source, exc)
            try:
                os.unlink(info_path)
            except OSError:
                pass
            return False
        return True
    return False


def _trash_one(path: Path, places: _Places | None) -> str | None:
    refusal = _trash_refusal(path, places)
    if refusal is not None:
        return refusal
    if _gio_trash(path):
        return None
    if _spec_trash(path) and not os.path.lexists(path):
        return None
    return _failed_text()


def move_to_trash(paths: Iterable[Path]) -> list[tuple[Path, str | None]]:
    """Move each path to the trash. Returns ``(path, None)`` or ``(path, translated error)``.

    Nothing is ever deleted: a path that cannot be trashed stays where it is. Only real files
    and folders inside the home folder are accepted; the home folder itself, the XDG folders,
    Easy Installer's own folders and the folder with the installed apps are refused.
    """
    places = _trash_places()
    results: list[tuple[Path, str | None]] = []
    for item in paths:
        try:
            path = Path(item)
        except TypeError:
            results.append((Path(str(item)), _failed_text()))
            continue
        try:
            error = _trash_one(path, places)
        except Exception as exc:  # noqa: BLE001 - one odd path must not stop the others
            log.warning("moving %s to the trash failed: %s", path, exc)
            error = _failed_text()
        results.append((path, error))
    return results


def trash_folder(path: Path) -> str | None:
    """Move an installed portable app's own folder to the trash (``move_to_trash`` refuses
    everything in the apps folder; the caller has made sure that ``path`` is such a folder).

    Only a real folder of this user that is not a mount point is moved - never copied: where
    the trash is on another file system and ``gio`` cannot use one on the folder's own, it
    stays. Returns None, or the translated reason why it stays where it is.
    """
    text = os.fspath(path)
    try:
        st = os.lstat(text)
    except FileNotFoundError:
        return _("It no longer exists.")
    except (OSError, ValueError):
        return _failed_text()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or os.path.ismount(text):
        return _protected_text()
    if _gio_trash(Path(text)):
        return None
    if _spec_trash(Path(text)) and not os.path.lexists(text):
        return None
    return _failed_text()


def _trash_places() -> _Places | None:
    try:
        return _places(os.environ)
    except (OSError, ValueError) as exc:
        log.debug("cannot work out which folders are protected: %s", exc)
        return None
