"""Naming, ids, versions, target paths and the launcher (.desktop) we write for an app.

Shared by the per-user installer and the privileged helper, so everything here is pure and
side-effect free (apart from existence checks in :func:`appimage_target`).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from .desktop_entry import INVALID_CHARS_RE, DesktopEntry, exec_program, join_exec, rewrite_exec
from .imageinfo import ImageInfo, extension_for, hicolor_subdir
from .paths import Layout, Scope

# \Z (not $): "$" would also accept a trailing newline. Always use ID_RE.fullmatch().
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
DESKTOP_PREFIX = "easyinstaller-"

GENERIC_DESKTOP_STEMS = frozenset({"appimage", "apprun", "app", "default"})
FALLBACK_ICON = "application-x-executable"
UNINSTALL_ACTION = "easyinstaller-uninstall"
ACTION_GROUP_PREFIX = "Desktop Action "
EXTRACT_AND_RUN_ENV = {"APPIMAGE_EXTRACT_AND_RUN": "1"}

_REMOVED_KEYS = (
    "DBusActivatable", "Hidden", "NoDisplay", "X-AppImage-Integrate", "Implements",
    # Beyond DESIGN.md: keys that could hide the launcher on the user's desktop (OnlyShowIn=KDE;)
    # or are only valid for Type=Link and would make the file invalid once Type=Application.
    "OnlyShowIn", "NotShowIn", "URL",
)
_REMOVED_KEY_PREFIXES = ("X-AppImageLauncher-", "X-GNOME-Autostart")
#: Keys whose localized variants (``Exec[de]``) are removed: GLib/KDE would use them instead of
#: the values Easy Installer sets.
_UNLOCALIZED_KEYS = ("Exec", "TryExec", "Icon")
#: Keys holding lists, where ``\;`` is a valid escape (in plain strings GLib rejects it).
_LIST_KEYS = frozenset({"Actions", "MimeType", "Categories", "Implements", "Keywords",
                        "OnlyShowIn", "NotShowIn"})
#: Variables an embedded ``Exec=env VAR=value ...`` may keep: they only choose a display backend,
#: scaling or the language. Anything else (LD_PRELOAD, *PATH, GIO_/GTK_ modules, PYTHON*, ...)
#: could make the launcher load code from other places, so it is dropped (and values with a "/"
#: are never kept).
SAFE_EXEC_ENV = frozenset({
    "APPIMAGE_EXTRACT_AND_RUN", "APPIMAGELAUNCHER_DISABLE", "DESKTOPINTEGRATION",
    "QT_QPA_PLATFORM", "QT_AUTO_SCREEN_SCALE_FACTOR", "QT_SCALE_FACTOR", "QT_SCREEN_SCALE_FACTORS",
    "QT_ENABLE_HIGHDPI_SCALING", "QT_FONT_DPI", "GDK_BACKEND", "GDK_SCALE", "GDK_DPI_SCALE",
    "SDL_VIDEODRIVER", "ELECTRON_OZONE_PLATFORM_HINT", "MOZ_ENABLE_WAYLAND", "WINIT_UNIX_BACKEND",
    "_JAVA_AWT_WM_NONREPARENTING", "NO_AT_BRIDGE", "LANG", "LANGUAGE", "LC_ALL",
})
_SPEC_VERSIONS = frozenset({"1.0", "1.1", "1.2", "1.3", "1.4", "1.5"})
_PRE_RELEASE = frozenset({"alpha", "a", "beta", "b", "rc", "pre", "preview", "dev", "nightly", "snapshot"})


# --------------------------------------------------------------------------------------------
# ids and names
# --------------------------------------------------------------------------------------------

def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def sanitize_id(s: str) -> str | None:
    """Turn ``s`` into an id matching :data:`ID_RE` (keeping case), or None if nothing is left."""
    if not s:
        return None
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", _ascii(s))
    text = re.sub(r"-{2,}", "-", text).lstrip("._-")
    text = text[:128].rstrip("-.")
    return text if ID_RE.fullmatch(text) else None


def _slug(s: str | None) -> str | None:
    if not s:
        return None
    text = re.sub(r"[^a-z0-9]+", "-", _ascii(s).lower()).strip("-")[:128].strip("-")
    return text if text and ID_RE.fullmatch(text) else None


_ARCH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:x86[_-]64|amd64|aarch64|arm64|armhf|armv7l|i[3-6]86|x64|linux(?:32|64)?)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9.])[vV]?"
    r"(\d+(?:\.\d+)+(?:[-~+]?(?:alpha|beta|rc|pre|preview|dev|nightly|snapshot)(?:[.-]?\d+)?)?)"
    r"(?![0-9])",
    re.IGNORECASE,
)


def _strip_appimage_suffix(filename: str) -> str:
    name = PurePosixPath(filename).name
    return re.sub(r"\.appimage$", "", name, flags=re.IGNORECASE)


def parse_version_from_filename(filename: str) -> str | None:
    """``"FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"`` -> ``"1.1.3"`` (needs at least one dot)."""
    match = _VERSION_RE.search(_strip_appimage_suffix(filename))
    return match.group(1) if match else None


def name_from_filename(filename: str) -> str:
    """A human name guessed from the file name: ``"T3-Code-0.0.42-x86_64.AppImage"`` -> ``"T3 Code"``."""
    stem = _strip_appimage_suffix(filename)
    match = _VERSION_RE.search(stem)
    if match and match.start() > 0:
        stem = stem[: match.start()]
    elif match:
        stem = stem[match.end():]
    stem = _ARCH_TOKEN_RE.sub(" ", stem)
    return " ".join(part for part in re.split(r"[-_\s]+", stem) if part).strip()


def derive_app_id(desktop_filename: str | None, name: str | None, filename: str) -> str:
    stem = ""
    if desktop_filename:
        stem = PurePosixPath(desktop_filename).name
        if stem.lower().endswith(".desktop"):
            stem = stem[: -len(".desktop")]
        if stem.lower() in GENERIC_DESKTOP_STEMS:
            stem = ""
        candidate = sanitize_id(stem)
        if candidate:
            return candidate
    candidate = _slug(name)
    if candidate:
        return candidate
    # The app has a name, just no ASCII in it ("Калькулятор", "微信"): a stable id made from that
    # text - not from the file name, which Easy Installer itself changes ("App.AppImage"), so the
    # installed file is still the same app. Never a shared constant either: the second app would
    # "update" (replace) the first.
    source = next((s.strip() for s in (stem, name) if s and s.strip()), "")
    if not source:
        file_name = name_from_filename(filename)
        candidate = _slug(file_name)
        if candidate:
            return candidate
        source = next((s.strip() for s in (file_name, _strip_appimage_suffix(filename))
                       if s.strip()), "")
    digest = hashlib.sha256(unicodedata.normalize("NFC", source).encode("utf-8", "surrogatepass"))
    return "app-" + digest.hexdigest()[:10]


_LEADING_V_RE = re.compile(r"^[vV](?=\d)")


def normalize_version(version: str | None) -> str | None:
    """``"v1.2.3"`` -> ``"1.2.3"`` (git tags); other versions are returned unchanged."""
    if version is None:
        return None
    return _LEADING_V_RE.sub("", version.strip()) or None


def _version_tokens(version: str) -> list[int | str]:
    version = normalize_version(version) or ""
    return [int(t) if t.isdigit() else t.casefold() for t in re.findall(r"\d+|[A-Za-z]+", version)]


def compare_versions(a: str | None, b: str | None) -> int:
    """Natural version comparison: -1 if a < b, 0 if equal, 1 if a > b. None sorts first."""
    if a is None or b is None:
        return (a is not None) - (b is not None)
    ta, tb = _version_tokens(a), _version_tokens(b)
    for x, y in zip(ta, tb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return 1   # "1.0.1" > "1.0rc1"
        if isinstance(y, int):
            return -1
        return -1 if x < y else 1
    if len(ta) == len(tb):
        return 0
    sign = 1 if len(ta) > len(tb) else -1
    rest = (ta if sign > 0 else tb)[min(len(ta), len(tb)):]
    if all(t == 0 for t in rest):
        return 0   # "1.0" == "1.0.0"
    first = rest[0]
    if isinstance(first, str) and first in _PRE_RELEASE:
        return -sign   # "1.0-beta" < "1.0"
    return sign


def safe_file_stem(name: str) -> str:
    """``"T3 Code (Alpha)"`` -> ``"T3-Code-Alpha"``: ASCII ``[A-Za-z0-9._-]``, at most 64 chars."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", _ascii(name or ""))
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    text = text[:64].rstrip("-._")
    return text or "App"


def _check_id(app_id: str) -> str:
    if not isinstance(app_id, str) or not ID_RE.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id!r}")
    return app_id


def desktop_file_name(app_id: str) -> str:
    return f"{DESKTOP_PREFIX}{_check_id(app_id)}.desktop"


def icon_name(app_id: str) -> str:
    return f"{DESKTOP_PREFIX}{_check_id(app_id)}"


def apparmor_profile_name(app_id: str) -> str:
    return f"{DESKTOP_PREFIX}{_check_id(app_id)}"


# --------------------------------------------------------------------------------------------
# a second version next to the installed one ("keep both") and kept previous versions
# --------------------------------------------------------------------------------------------

VARIANT_SEPARATOR = "--"
MAX_ID_LENGTH = 128
MAX_VERSION_SLUG = 32
_SHA_PREFIX = 8


def version_slug(version: str | None, sha256: str | None = None) -> str:
    """Text that tells two versions of an app apart and is safe in ids and file names.

    The sanitized version (``"0.0.42"``, ``"1.2-beta.1"``); without a usable version the first
    8 hex digits of the file's sha256; without either ``"copy"``.
    """
    text = re.sub(r"[^A-Za-z0-9._]+", "-", _ascii(normalize_version(version) or ""))
    text = text.strip("-._")[:MAX_VERSION_SLUG].strip("-._")
    if text:
        return text
    digest = re.sub(r"[^0-9a-f]", "", (sha256 or "").lower())[:_SHA_PREFIX]
    return digest or "copy"


def version_label(version: str | None, sha256: str | None = None) -> str:
    """What is shown after the name of a kept copy: the version, else :func:`version_slug`."""
    text = " ".join((normalize_version(version) or "").split())
    if text and not any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in text):
        return text[:MAX_VERSION_SLUG]
    return version_slug(None, sha256)


def variant_id(base_id: str, version: str | None, sha256: str | None) -> str:
    """The id of a copy installed next to ``base_id``: ``"<base_id>--<slug>"``, always valid.

    ``slug`` is :func:`version_slug`. A base id too long to take the suffix is shortened and
    gets a few hex digits of its own hash, so two long ids never end up with the same copy id.
    """
    _check_id(base_id)
    suffix = VARIANT_SEPARATOR + version_slug(version, sha256)
    room = MAX_ID_LENGTH - len(suffix)
    base = base_id
    if len(base) > room:
        digest = hashlib.sha256(base_id.encode("ascii")).hexdigest()[:_SHA_PREFIX]
        base = base_id[: room - len(digest) - 1].rstrip("-._") + "-" + digest
    return _check_id(base + suffix)


def variant_name(name: str, label: str) -> str:
    """``"FreeCAD"`` + ``"1.0.2"`` -> ``"FreeCAD 1.0.2"``."""
    return f"{name} {label}".strip()


def with_name_suffix(entry: DesktopEntry | None, label: str) -> DesktopEntry | None:
    """A copy of ``entry`` whose ``Name`` and every ``Name[xx]`` end in ``" <label>"``.

    The launcher of a kept copy is rendered from this, so the two versions can be told apart in
    the app menu in every language. An entry without a name is returned unchanged (the renderer
    then uses the name it is given).
    """
    if entry is None or not label or not entry.has_group(DesktopEntry.MAIN):
        return entry
    result = entry.copy()
    for key in result.keys():
        if key != "Name" and not key.startswith("Name["):
            continue
        value = result.get(key)
        if value is not None and value.strip():
            result.set(key, variant_name(value.strip(), label))
    return result


def backup_file_name(name: str, version: str | None, sha256: str | None) -> str:
    """File name of a kept previous version: ``"<SafeName>-<version|sha8>.AppImage"``."""
    return f"{safe_file_stem(name)}-{version_slug(version, sha256)}.AppImage"


# --------------------------------------------------------------------------------------------
# target paths
# --------------------------------------------------------------------------------------------

def _same_path(a: Path, b: Path | None) -> bool:
    if b is None:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def appimage_target(layout: Layout, app_id: str, name: str,
                    owner_of: Callable[[Path], str | None], source: Path | None = None) -> Path:
    _check_id(app_id)
    stem = safe_file_stem(name)
    target = layout.apps_dir / f"{stem}.AppImage"
    if (target.exists() or target.is_symlink()) and not _same_path(target, source) \
            and owner_of(target) != app_id:
        target = layout.apps_dir / f"{stem}-{app_id}.AppImage"
    return target


def icon_target(layout: Layout, app_id: str, info: ImageInfo) -> Path:
    return layout.icons_dir / hicolor_subdir(info) / f"{icon_name(app_id)}.{extension_for(info)}"


# --------------------------------------------------------------------------------------------
# launcher rendering
# --------------------------------------------------------------------------------------------

@dataclass
class DesktopRenderSpec:
    app_id: str
    name: str
    appimage_path: Path
    icon_name: str | None
    embedded: DesktopEntry | None
    embedded_stem: str | None
    comment: str | None
    version: str | None
    scope: Scope
    extra_args: tuple[str, ...] = ()
    extract_and_run: bool = False
    uninstall_command: tuple[str, ...] | None = None
    #: ``Path=`` of the launcher (portable apps start in their own folder); None: whatever the
    #: embedded entry says
    working_dir: Path | None = None
    #: ``StartupWMClass`` when the embedded entry has none (before ``embedded_stem``)
    startup_wm_class: str | None = None


def _action_group(action: str) -> str:
    return ACTION_GROUP_PREFIX + action


def exec_env_allowed(name: str, value: str) -> bool:
    """May an ``env NAME=value`` assignment of an embedded Exec stay in our launcher?"""
    return name in SAFE_EXEC_ENV and "/" not in value


def _remove_localized(entry: DesktopEntry, key: str, group: str) -> None:
    for existing in entry.keys(group):
        if existing.startswith(key + "["):
            entry.remove(existing, group)


def _fix_escapes(raw: str, *, list_mode: bool) -> str:
    """Make unknown escapes (``\\x``, ``\\;`` outside lists, a trailing ``\\``) literal backslashes.

    GLib rejects such values (the menu would show "Unnamed"); :func:`unescape_value` keeps them
    verbatim, so this keeps the meaning Easy Installer showed.
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            nxt = raw[i + 1] if i + 1 < n else ""
            if nxt in ("s", "n", "t", "r", "\\") or (list_mode and nxt == ";"):
                out.append(c + nxt)
                i += 2
                continue
            out.append("\\\\")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _repair_escapes(entry: DesktopEntry) -> None:
    for group in entry.groups():
        for key in entry.keys(group):
            raw = entry.get_raw(key, group)
            if raw is None or "\\" not in raw:
                continue
            fixed = _fix_escapes(raw, list_mode=key.partition("[")[0] in _LIST_KEYS)
            if fixed != raw:
                entry.set_raw(key, fixed, group)


def _rewrite_action(entry: DesktopEntry, group: str, spec: DesktopRenderSpec,
                    env: dict[str, str] | None, old_icon: str | None, new_icon: str,
                    app_programs: frozenset[str]) -> bool:
    """Point an action at the AppImage; False if the action must be dropped."""
    value = entry.get("Exec", group)
    if not value or not (entry.get("Name", group) or "").strip():
        return False
    program = exec_program(value)
    if program is None:
        return False  # unparseable
    if program not in app_programs and (program.startswith("/") or "/" not in program):
        return False  # another program of the host system (xdg-open, sh, ...), not the app
    entry.set("Exec", rewrite_exec(value, str(spec.appimage_path), extra_args=spec.extra_args,
                                   env=env, keep_env=exec_env_allowed), group)
    if old_icon is not None and entry.get("Icon", group) == old_icon:
        entry.set("Icon", new_icon, group)
    return True


def _scope_value(scope: Scope | str) -> str:
    return Scope(scope).value


def _drop_orphan_localized_keys(entry: DesktopEntry) -> None:
    """``Keywords[de]`` without ``Keywords`` is invalid; drop such keys in every group."""
    for group in entry.groups():
        keys = entry.keys(group)
        for key in keys:
            base, bracket, _locale = key.partition("[")
            if bracket and base not in keys:
                entry.remove(key, group)


def render_desktop_entry(spec: DesktopRenderSpec) -> str:
    main = DesktopEntry.MAIN
    embedded = spec.embedded if spec.embedded is not None and spec.embedded.has_group(main) else None
    entry = embedded.copy() if embedded is not None else DesktopEntry.new()
    entry.remove_invalid_lines()
    entry.move_group_to_front(main)
    for group in entry.groups():
        if group != main and not group.startswith((ACTION_GROUP_PREFIX, "X-")):
            entry.remove_group(group)

    path = str(spec.appimage_path)
    env = dict(EXTRACT_AND_RUN_ENV) if spec.extract_and_run else None
    old_icon = entry.get("Icon")
    new_icon = spec.icon_name or FALLBACK_ICON
    # Actions may start the app itself: AppRun, the main Exec's program (even an absolute one,
    # e.g. /usr/bin/brave-browser-stable of a repacked .deb: the main Exec is rewritten, too)
    # or the app's own name.
    app_programs = frozenset(p for p in ("AppRun", exec_program(entry.get("Exec") or ""),
                                         spec.embedded_stem) if p)
    _repair_escapes(entry)
    for group in entry.groups():  # the main group, actions and X- groups
        for key in _UNLOCALIZED_KEYS:
            _remove_localized(entry, key, group)

    entry.set("Type", "Application")
    if not (entry.get("Name") or "").strip():
        entry.set("Name", spec.name)
    version_key = entry.get("Version")
    if version_key is not None and version_key.strip() not in _SPEC_VERSIONS:
        entry.remove("Version")
    if not (entry.get("Comment") or "").strip() and spec.comment:
        entry.set("Comment", spec.comment)
    entry.set("Exec", rewrite_exec(entry.get("Exec") or "", path, extra_args=spec.extra_args, env=env,
                                   keep_env=exec_env_allowed))
    entry.set("TryExec", path)
    if spec.working_dir is not None:
        entry.set("Path", str(spec.working_dir))
    entry.set("Icon", new_icon)
    if not (entry.get("StartupWMClass") or "").strip():
        wm_class = (spec.startup_wm_class or "").strip()
        if not wm_class or INVALID_CHARS_RE.search(wm_class):
            wm_class = spec.embedded_stem or ""
        if wm_class:
            entry.set("StartupWMClass", wm_class)
    for key in _REMOVED_KEYS:
        entry.remove(key, localized_too=True)
    for key in entry.keys():
        if key.startswith(_REMOVED_KEY_PREFIXES):
            entry.remove(key)
    if not entry.get_list("Categories"):
        entry.set_list("Categories", ["Utility"])
    entry.set("X-EasyInstaller-Id", spec.app_id)
    entry.set("X-EasyInstaller-Scope", _scope_value(spec.scope))
    if spec.version:
        entry.set("X-AppImage-Version", spec.version)

    # Desktop actions ("New Window", ...): keep those that start the app itself.
    kept: list[str] = []
    for action in dict.fromkeys(entry.get_list("Actions")):
        group = _action_group(action)
        if action == UNINSTALL_ACTION or not entry.has_group(group):
            continue
        if _rewrite_action(entry, group, spec, env, old_icon, new_icon, app_programs):
            kept.append(action)
    for group in entry.groups():
        if group.startswith(ACTION_GROUP_PREFIX) and group[len(ACTION_GROUP_PREFIX):] not in kept:
            entry.remove_group(group)

    if spec.uninstall_command:
        group = _action_group(UNINSTALL_ACTION)
        entry.add_group(group)
        # Fixed strings: the launcher carries its own translations.
        entry.set("Name", "Uninstall…", group)
        entry.set("Name[de]", "Deinstallieren…", group)
        entry.set("Name[nl]", "Verwijderen…", group)
        entry.set("Icon", "user-trash-symbolic", group)
        entry.set("Exec", join_exec(spec.uninstall_command), group)
        kept.append(UNINSTALL_ACTION)

    if kept:
        entry.set_list("Actions", kept)
    else:
        entry.remove("Actions")
    _drop_orphan_localized_keys(entry)
    return entry.to_text()


# --------------------------------------------------------------------------------------------
# MIME types the app defines itself (usr/share/mime/packages/*.xml)
# --------------------------------------------------------------------------------------------

MIME_NS = "http://www.freedesktop.org/standards/shared-mime-info"
#: The distribution's MIME database: types and file name patterns it already knows.
HOST_MIME_DIR = Path("/usr/share/mime")
MAX_MIME_TYPES = 16
MAX_MIME_GLOBS = 8
MAX_MIME_PARENTS = 4
MAX_MIME_COMMENT = 200
#: The media types of files. update-mime-database writes ``<mime_dir>/<media>/<subtype>.xml`` for
#: every type (as root for apps installed for everyone), so any other first part would write into
#: its own folders ("packages/<x>" replaces another package, "mime.cache/x" breaks the database);
#: inode/, x-content/ and x-scheme-handler/ are no file types either.
MIME_MEDIA_TYPES = frozenset({"application", "audio", "chemical", "font", "image", "message",
                              "model", "multipart", "text", "video"})
_MIME_SUBTYPE = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
MIME_TYPE_RE = re.compile(rf"(?:{'|'.join(sorted(MIME_MEDIA_TYPES))})/{_MIME_SUBTYPE}\Z")
#: Only "*.<extension>": a package must not claim every file ("*") or names like "README".
MIME_GLOB_RE = re.compile(r"\*\.([A-Za-z0-9][A-Za-z0-9_+-]{0,15}(?:\.[A-Za-z0-9_+-]{1,15}){0,2})\Z")


@dataclass(frozen=True)
class MimeTypeDef:
    """A file type from the app's shared-mime-info package (only what Easy Installer uses)."""

    type: str
    comment: str | None = None
    globs: tuple[str, ...] = ()
    sub_class_of: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "comment": self.comment, "globs": list(self.globs),
                "sub_class_of": list(self.sub_class_of)}

    @classmethod
    def from_dict(cls, data: Any) -> "MimeTypeDef":
        """Validating constructor (also used by the root helper for client data)."""
        if not isinstance(data, dict):
            raise ValueError("MIME type definition must be an object")
        mime = data.get("type")
        if not isinstance(mime, str) or not MIME_TYPE_RE.match(mime):
            raise ValueError(f"invalid MIME type {mime!r}")
        comment = data.get("comment")
        if comment is not None and (not isinstance(comment, str) or len(comment) > MAX_MIME_COMMENT
                                    or any(unicodedata.category(c) in ("Cc", "Cs") for c in comment)):
            raise ValueError(f"invalid comment for {mime}")
        globs = data.get("globs") or []
        parents = data.get("sub_class_of") or []
        if not isinstance(globs, list) or len(globs) > MAX_MIME_GLOBS \
                or not all(isinstance(g, str) and MIME_GLOB_RE.match(g) for g in globs):
            raise ValueError(f"invalid globs for {mime}: {globs!r}"[:200])
        if not isinstance(parents, list) or len(parents) > MAX_MIME_PARENTS \
                or not all(isinstance(p, str) and MIME_TYPE_RE.match(p) for p in parents):
            raise ValueError(f"invalid sub-class-of for {mime}: {parents!r}"[:200])
        return cls(mime, comment or None, tuple(dict.fromkeys(globs)), tuple(dict.fromkeys(parents)))


def mime_package_name(app_id: str) -> str:
    return f"{DESKTOP_PREFIX}{_check_id(app_id)}.xml"


def parse_mime_package(text: str | bytes) -> list[MimeTypeDef]:
    """The usable definitions of a shared-mime-info XML file (invalid parts are skipped).

    Only ``type``, ``comment`` (unlocalized), ``glob`` patterns of the form ``*.ext`` and
    ``sub-class-of`` are kept; magic rules, icons, aliases and other elements are ignored.
    Pass the file's bytes, so that its declared encoding (or BOM) is used.
    """
    import xml.etree.ElementTree as ET  # expat: no external entities; nested entities limited

    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return []
    result: list[MimeTypeDef] = []
    for element in root.findall(f"{{{MIME_NS}}}mime-type"):
        comment = None
        for child in element.findall(f"{{{MIME_NS}}}comment"):
            if not child.attrib and child.text and child.text.strip():
                comment = " ".join(child.text.split())[:MAX_MIME_COMMENT]
                break
        data = {
            "type": element.get("type", ""),
            "comment": comment,
            "globs": [g.get("pattern", "") for g in element.findall(f"{{{MIME_NS}}}glob")
                      if g.get("case-sensitive") != "true" and MIME_GLOB_RE.match(g.get("pattern", ""))
                      ][:MAX_MIME_GLOBS],
            "sub_class_of": [p.get("type", "") for p in element.findall(f"{{{MIME_NS}}}sub-class-of")
                             if MIME_TYPE_RE.match(p.get("type", ""))][:MAX_MIME_PARENTS],
        }
        try:
            result.append(MimeTypeDef.from_dict(data))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class HostMimeDatabase:
    types: frozenset[str] = frozenset()
    globs: frozenset[str] = frozenset()   # lower-case patterns, e.g. "*.txt"


def host_mime_database(directory: Path | None = None) -> HostMimeDatabase:
    """Types and patterns the distribution already knows (``types`` and ``globs2``)."""
    directory = directory or HOST_MIME_DIR
    types: set[str] = set()
    globs: set[str] = set()
    try:
        types = {line.strip() for line in (directory / "types").read_text(
            encoding="utf-8", errors="replace").splitlines() if line.strip()}
    except OSError:
        pass
    try:
        for line in (directory / "globs2").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 3:
                globs.add(parts[2].strip().lower())
    except OSError:
        pass
    return HostMimeDatabase(frozenset(types), frozenset(globs))


_GLOB_META_RE = re.compile(r"[*?\[\]]")


def _takes_over(glob: str, suffixes: frozenset[str], full_globs: Iterable[str]) -> bool:
    """Would the new pattern ``glob`` ("*.ext") win files a pattern of the host matches today?

    xdgmime tries simple "*.ext" patterns before other ("full") globs and prefers the longest
    suffix: "*.1" would take man pages from "*.[1-9]", "*.so.6" libraries from "*.so.[0-9]*",
    "*.min.js" JavaScript files from "*.js".
    """
    suffix = glob[1:].lower()                       # ".min.js"
    parts = suffix.split(".")[1:]                   # ["min", "js"]
    if any("." + ".".join(parts[i:]) in suffixes for i in range(len(parts))):
        return True
    for pattern in full_globs:
        if fnmatch.fnmatchcase("x" + suffix, pattern):
            return True
        tail = _GLOB_META_RE.split(pattern)[-1]     # ".vdr" of "[0-9][0-9][0-9].vdr"
        if tail.startswith(".") and (tail.endswith(suffix) or suffix.endswith(tail)):
            return True
    return False


def select_mime_types(definitions: Iterable[MimeTypeDef], wanted: Iterable[str],
                      host: HostMimeDatabase) -> list[MimeTypeDef]:
    """What to register: types the launcher opens (``MimeType=``) that this computer does not
    know yet, with only patterns that take no files from a known type (an app must not take
    over e.g. *.txt, man pages or *.min.js)."""
    wanted_set = set(wanted)  # MimeTypeDef only holds file types (no x-scheme-handler/...)
    simple = {g for g in host.globs if g.startswith("*.") and not _GLOB_META_RE.search(g, 1)}
    suffixes = frozenset(g[1:] for g in simple)                     # ".txt"
    full_globs = [g for g in host.globs - simple if _GLOB_META_RE.search(g)]  # not literal names
    chosen: dict[str, MimeTypeDef] = {}
    for definition in definitions:
        if definition.type not in wanted_set or definition.type in host.types \
                or definition.type in chosen:
            continue
        globs = tuple(g for g in definition.globs if not _takes_over(g, suffixes, full_globs))
        if not globs:
            continue  # without a pattern the type would never be recognised
        chosen[definition.type] = MimeTypeDef(definition.type, definition.comment, globs,
                                              definition.sub_class_of)
        if len(chosen) >= MAX_MIME_TYPES:
            break
    return list(chosen.values())


def render_mime_package(app_id: str, definitions: Iterable[MimeTypeDef]) -> str:
    """The shared-mime-info XML Easy Installer writes (from validated values only)."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f"<!-- Managed by Easy Installer for {xml_escape(_check_id(app_id))} -->",
             f'<mime-info xmlns="{MIME_NS}">']
    for definition in definitions:
        lines.append(f"  <mime-type type={quoteattr(definition.type)}>")
        if definition.comment:
            lines.append(f"    <comment>{xml_escape(definition.comment)}</comment>")
        for parent in definition.sub_class_of:
            lines.append(f"    <sub-class-of type={quoteattr(parent)}/>")
        for glob in definition.globs:
            lines.append(f"    <glob pattern={quoteattr(glob)}/>")
        lines.append("  </mime-type>")
    lines.append("</mime-info>")
    return "\n".join(lines) + "\n"
