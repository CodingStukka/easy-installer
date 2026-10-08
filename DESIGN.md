# Easy Installer — Design Contract

Easy Installer turns a downloaded `.AppImage` into a "real" installed app for people who are new to
Linux: one click and the app sits in a proper location, shows up in the app menu / GNOME search with
the right icon and name, and can be uninstalled again from the same place. It is the automated,
novice-proof version of https://dev.to/lovestaco/how-to-create-a-launcher-for-your-appimage-on-linux-mc3
(move to a fixed location, `chmod +x`, icon, `.desktop` file, `update-desktop-database`) — except the
icon, name, version, categories, MIME types etc. are pulled **out of the AppImage itself**.

This document is the contract every module is built against. If you implement a module, match the
names and signatures here exactly. If you find a gap, pick the most sensible behaviour, keep the
public signature, and report the gap.

## 0. Ground rules

* **Stack:** Python ≥ 3.10, standard library only for `core/`, `helper/`, `cli.py`.
  GUI: PyGObject, **GTK 4.14 + libadwaita 1.5** (the versions on Ubuntu 24.04 / Zorin OS 18).
  Do not use APIs newer than Adw 1.5 / GTK 4.14 (e.g. no `Adw.ToggleGroup`, `Adw.Spinner`,
  `Adw.SpinnerPaintable`, `Adw.BottomSheet`, `Adw.WrapBox`, `Adw.ButtonRow` — those are 1.6+).
* External tools (all optional, detected at runtime): `unsquashfs` (squashfs-tools ≥ 4.4),
  `update-desktop-database`, `gtk-update-icon-cache`/`gtk4-update-icon-cache`, `desktop-file-validate`,
  `update-mime-database`, `pkexec`, `apparmor_parser`, `ldconfig`.
* **We never execute the AppImage to inspect it** when `unsquashfs` is available. The squashfs payload
  starts right after the ELF runtime (offset = `e_shoff + e_shentsize * e_shnum`), which we compute
  ourselves and pass to `unsquashfs -o <offset>`. Fallback only if `unsquashfs` is missing:
  `<file> --appimage-extract <pattern>` (runs the AppImage's own runtime, never the app).
* App id: `com.roothirsch.EasyInstaller`. Python package: `easy_installer` (in `src/`). Command:
  `easy-installer`. Gettext domain: `easy-installer`.
* All user-facing strings go through `from easy_installer.i18n import _` (English source strings;
  German `de` and Dutch `nl` translations). Technical log/debug text is not translated.
* Logging via `logging.getLogger(__name__)`; no prints in library code.
* **Safety rules for development/tests (mandatory):** never move/modify/delete the real AppImages in
  `~/Downloads`, `~/Desktop`, `~/Documents` (read-only use or copy to a temp dir); never write into the
  real `~/Applications`, `~/.local/share/applications`, `~/.local/share/icons`, `/opt`, `/usr/local`,
  `/etc`, `/var/lib`; never run `pkexec`/`sudo`. Tests isolate via `HOME`, `XDG_DATA_HOME`,
  `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `EASY_INSTALLER_APPS_DIR` and `system_layout(root=tmp)`.

## 1. Repository layout

```
pyproject.toml   Makefile   README.md   DESIGN.md   .gitignore
src/easy_installer/
  __init__.py          APP_ID, APP_NAME, __version__ = "0.2.1", GETTEXT_DOMAIN
  __main__.py          entry point: CLI subcommands vs GUI
  errors.py            exception hierarchy (user-facing messages)
  i18n.py              gettext setup, `_`, `ngettext`
  cli.py               argparse CLI
  core/
    __init__.py
    elf.py             ELF parsing (payload offset, arch, sections, PT_INTERP)
    squashfs.py        payload readers (unsquashfs / --appimage-extract fallback)
    imageinfo.py       PNG/SVG/XPM probing, hicolor directory selection
    desktop_entry.py   round-trip .desktop parser/writer + Exec quoting
    inspector.py       inspect_appimage() -> AppImageInfo
    integration.py     naming, ids, versions, target paths, render_desktop_entry()
    paths.py           Scope, Layout, user_layout(), system_layout()
    registry.py        InstalledApp, Registry (JSON, atomic, locked)
    system_checks.py   SystemStatus (FUSE, userns restriction, tools)
    sandbox.py         Electron/Chromium sandbox fix (AppArmor profile rendering)
    installer.py       plan_install / execute_install / uninstall / list_installed
    privileged.py      client side of the pkexec helper
    (0.2, see §20–§26) settings.py, reconcile.py, backups.py, variants.py, updates.py, updater.py,
                       appdata.py, origin.py, signature.py, portable.py
  helper/
    __init__.py
    ops.py             root-side operations (pure functions, testable without root)
    main.py            `main()` for the pkexec entry point (JSON in/out)
    entry.py           tiny script usable as `pkexec /usr/bin/python3 <abs>/entry.py` in dev mode
  gui/
    __init__.py
    application.py     Adw.Application subclass
    window.py          main window (installed list, empty state, drag & drop)
    install_dialog.py  inspect → confirm → progress → done
    app_row.py         row widget for one installed app
    async_utils.py     run_in_thread(fn, callback) helper
    common.py          icons, markup escaping, embedded CSS, launching, friendly error texts
    settings.py        settings.json (e.g. default_handler_offer_dismissed)
    system_dialog.py   "Check This Computer" dialog + About troubleshooting text
    (0.2, see §28) details_dialog.py, uninstall_dialog.py, preferences_dialog.py (main window side)
data/
  com.roothirsch.EasyInstaller.desktop            (launcher of Easy Installer itself; MimeType=AppImage types)
  com.roothirsch.EasyInstaller.metainfo.xml
  com.roothirsch.EasyInstaller.policy             (polkit action for the helper)
  icons/hicolor/scalable/apps/com.roothirsch.EasyInstaller.svg
  icons/hicolor/symbolic/apps/com.roothirsch.EasyInstaller-symbolic.svg
  easy-installer-helper                           (/usr/libexec wrapper script)
  easy-installer                                  (/usr/bin wrapper script)
po/  POTFILES  easy-installer.pot  de.po  nl.po
tools/  msgfmt.py (stdlib .po→.mo compiler)  xgettext.py (stdlib string extractor)
        update_po.py (stdlib msgmerge)  gui_screenshots.py (headless Broadway screenshots, --language;
        one tool for every screen: 0.1 scenes 01-15, 0.2 window/details/preferences 16-25, 0.2
        install and update dialogs 30-42)
packaging/  build-deb.sh
tests/
```

## 2. `errors.py`

```python
class EasyInstallerError(Exception):
    """str(exc) is a translated, user-facing sentence. `details` = optional technical text."""
    def __init__(self, message: str, details: str | None = None): ...
    message: str; details: str | None
class NotAnAppImageError(EasyInstallerError)      # not ELF / no payload / unsupported type
class ExtractionError(EasyInstallerError)         # unsquashfs failed, damaged/incomplete download
class UnsupportedArchitectureError(EasyInstallerError)
class InstallError(EasyInstallerError)            # disk full, permission, IO
class NotInstalledError(EasyInstallerError)
class AuthorizationError(EasyInstallerError)      # pkexec dismissed (126) / not authorized (127)
class HelperError(EasyInstallerError)             # helper returned ok=false or crashed
```

## 3. `i18n.py`

* On import: `locale.setlocale(LC_ALL, "")` (ignore errors), find the locale dir, install a
  `gettext.translation(GETTEXT_DOMAIN, localedir, fallback=True)`.
* Locale dir search order: `$EASY_INSTALLER_LOCALEDIR`; `<repo>/build/locale` (source checkout, i.e.
  `Path(__file__).parents[2] / "build/locale"`); `$XDG_DATA_HOME/locale` (default
  `~/.local/share/locale`, used by `make install-user`); `<sys.prefix>/share/locale`;
  `/usr/local/share/locale`; `/usr/share/locale` — first dir that contains
  `*/LC_MESSAGES/easy-installer.mo`.
* Exports `_(msg) -> str`, `ngettext(s, p, n) -> str`, `N_(msg) -> msg` (marker only), `setup()`.
* (0.2) One word per idea in the CLI and the GUI: "update" (de: Update / aktualisieren, nl:
  update / bijwerken), "previous version" for the kept backup (never "earlier"/"old version"; de:
  vorherige Version, nl: vorige versie), "portable app" (de: portable App, nl: portable app),
  "settings and data" (de: Einstellungen und Daten, nl: instellingen en gegevens).
  `tests/test_i18n.py::test_terminology_is_consistent` enforces the translations.

## 4. `core/elf.py`

```python
@dataclass(frozen=True)
class ElfInfo:
    bits: int                      # 32 or 64
    little_endian: bool
    machine: int                   # e_machine
    arch: str                      # "x86_64" | "aarch64" | "i686" | "armhf" | "unknown"
    payload_offset: int            # e_shoff + e_shentsize * e_shnum
    appimage_type: int | None      # 1 or 2 if bytes[8:11] == b"AI\x01"/b"AI\x02", else None
    has_interp: bool               # PT_INTERP program header present (dynamically linked runtime)
    sections: dict[str, tuple[int, int]]   # section name -> (file offset, size)

def read_elf_info(path: str | os.PathLike) -> ElfInfo      # raises NotAnAppImageError
def read_section(path, info: ElfInfo, name: str, max_size: int = 1 << 20) -> bytes | None
def read_update_info(path, info: ElfInfo) -> str | None    # ".upd_info", NUL-stripped, None if empty
def payload_magic(path, info: ElfInfo) -> bytes            # 4 bytes at payload_offset (b"hsqs" = squashfs)
```
Map `e_machine` (`arch_name`): 62→x86_64, 183→aarch64, 3→i686, 40→armhf, 243→riscv64, 21→ppc64le/ppc64,
22→s390x, 258→loongarch64, 8→mips*, … else "unknown". `host_machine()` = e_machine of the running Python;
`is_foreign_arch(info)` compares e_machine with it. Bounds-check everything (truncated or
hostile headers must raise `NotAnAppImageError`, never IndexError/struct.error).

## 5. `core/squashfs.py`

```python
class PayloadReader(ABC):
    def list_members(self) -> list[str] | None     # all paths relative to the AppImage root
                                                  # ("t3code.desktop", ".DirIcon", "usr/share/..."), None if unsupported
    def member_sizes(self) -> dict[str, int] | None   # sizes of the regular files (None: unknown)
    def extract(self, members: Sequence[str], dest: Path) -> None
        # extract the given paths/glob patterns into dest (dest/<member>); missing members are
        # silently skipped; symlinks are extracted as symlinks (never followed)

class UnsquashfsReader(PayloadReader):   # unsquashfs -o OFFSET ... ; list via `-lln` (names + sizes; a
                                         # symlink's size is its target's length), else `-l` (strip "squashfs-root/")
class ExtractCommandReader(PayloadReader):  # "<appimage> --appimage-extract <pattern>" in a temp cwd, then
                                             # move squashfs-root/* into dest; list_members() -> None;
                                             # 60 s timeout, sanitized env (drop APPIMAGE_*),
                                             # requires exec bit (chmod u+x the source if missing)

def open_payload(path: Path, elf: ElfInfo) -> PayloadReader
    # UnsquashfsReader if unsquashfs is on PATH and payload_magic == b"hsqs";
    # else ExtractCommandReader for type-2 / type-1 AppImages; else raise NotAnAppImageError.
```
`unsquashfs` flags: `-o OFFSET -d DEST -f -no-xattrs -no-progress` (and `-n`/`-q` if supported), pass
member paths as positional arguments. A non-zero exit → `ExtractionError` with a friendly message
("The file seems damaged or incomplete. Try downloading it again.") + stderr in `details`.

## 6. `core/imageinfo.py`

```python
@dataclass(frozen=True)
class ImageInfo:
    format: str                 # "png" | "svg" | "xpm" | "unknown"
    width: int | None
    height: int | None
def probe_image(path: Path) -> ImageInfo       # PNG IHDR, SVG root width/height/viewBox (best effort), XPM header
HICOLOR_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
def hicolor_subdir(info: ImageInfo) -> str     # "scalable/apps" for svg; for png/xpm: "<N>x<N>/apps" where N =
                                               # exact size if square & in HICOLOR_SIZES, else largest size <= min(w,h)
                                               # (min 16; >512 -> 512; unknown size -> 256)
def extension_for(info: ImageInfo) -> str      # "png" | "svg" | "xpm"
```

## 7. `core/desktop_entry.py`

Round-trip parser: preserves group order, key order, comments, blank lines, localized keys
(`Name[de]`). Unknown content is kept verbatim.

```python
class DesktopEntry:
    MAIN = "Desktop Entry"
    @classmethod
    def parse(cls, text: str) -> "DesktopEntry"      # tolerant: skips junk lines (keeps them), handles BOM, CRLF;
                                                      # like GLib only ASCII whitespace is blank/indentation
    @classmethod
    def new(cls) -> "DesktopEntry"                    # single empty [Desktop Entry] group
    def groups(self) -> list[str]
    def has_group(self, group: str) -> bool
    def keys(self, group: str = MAIN) -> list[str]    # raw keys incl. locale suffix
    def get(self, key: str, group: str = MAIN) -> str | None          # UNESCAPED string value (\s \n \t \r \\)
    def get_raw(self, key: str, group: str = MAIN) -> str | None
    def get_localized(self, key: str, locales: Sequence[str] | None = None, group: str = MAIN) -> str | None
        # locale matching per Desktop Entry spec (lang_COUNTRY@MOD → lang_COUNTRY → lang@MOD → lang → unlocalized);
        # locales default = derived from LC_MESSAGES / LANGUAGE / LANG
    def get_list(self, key: str, group: str = MAIN) -> list[str]      # ';'-separated, honours "\;"
    def get_bool(self, key: str, group: str = MAIN, default: bool = False) -> bool
    def set(self, key: str, value: str, group: str = MAIN) -> None    # escapes value; replace in place or append
                                                                      # after last key of group; creates group if missing
    def set_raw(self, key: str, raw: str, group: str = MAIN) -> None
    def set_list(self, key: str, values: Sequence[str], group: str = MAIN) -> None   # trailing ';'
    def remove(self, key: str, group: str = MAIN, *, localized_too: bool = False) -> None
    def add_group(self, group: str) -> None
    def remove_group(self, group: str) -> None
    def to_text(self) -> str                                          # ends with "\n"

def escape_value(s: str) -> str; def unescape_value(s: str) -> str
def split_exec(value: str) -> list[str]
    # value = already-unescaped Exec string; split per spec: whitespace-separated, double-quoted args
    # may contain \" \` \$ \\ escapes; field codes (%f %F %u %U %i %c %k) stay as separate tokens;
    # raises ValueError on unbalanced quotes
def join_exec(args: Sequence[str]) -> str
    # inverse: quote args containing reserved chars (space \t \n " ' \ > < ~ | & ; $ * ? # ( ) `),
    # escape " ` $ \ inside quotes; literal '%' in normal args becomes '%%'; field codes unquoted
def rewrite_exec(value: str, program: str, *, extra_args: Sequence[str] = (),
                 env: Mapping[str, str] | None = None,
                 keep_env: Callable[[str, str], bool] | None = None) -> str
    # value = unescaped Exec. Skip a leading "env" and any VAR=val tokens (kept only if keep_env(name,
    # value) allows it, when given), replace the next token (the program, e.g. "AppRun" or "openscad")
    # by `program`, keep all remaining args (incl. field codes), insert extra_args right after the
    # program unless already present. If env: prefix "env VAR=val ...". If value is empty/unparseable
    # -> program + extra_args + " %U"-less plain.
def exec_env(value: str) -> list[tuple[str, str]] | None   # assignments of a leading "env" (None: unparseable)
```
Note: `get("Exec")` returns the unescaped string; `set("Exec", join_exec(...))` escapes backslashes again.

## 8. `core/inspector.py`

```python
@dataclass
class AppImageInfo:
    path: Path                     # resolved absolute path of the inspected file
    size: int
    sha256: str | None
    elf: ElfInfo
    appimage_type: int | None
    arch: str
    app_id: str                    # integration.derive_app_id(...)
    name: str                      # unlocalized Name= or derived from filename
    display_name: str              # localized Name for current locale (fallback name)
    version: str | None            # X-AppImage-Version, else parsed from filename
    comment: str | None            # localized Comment
    categories: list[str]
    desktop_entry: DesktopEntry | None      # embedded entry (parsed)
    desktop_filename: str | None            # e.g. "org.freecad.FreeCAD.desktop"
    icon_path: Path | None                  # extracted icon (regular file inside work_dir)
    icon_info: ImageInfo | None
    is_electron: bool                       # chrome-sandbox / chrome_crashpad_handler / resources/app.asar at the
                                            # root or nested (opt/<App>/…, usr/lib/<app>/…; ≤ 5 path components)
    exec_has_no_sandbox: bool               # embedded Exec already contains --no-sandbox
    terminal: bool
    update_info: str | None
    work_dir: Path                          # private temp dir (0700) owned by this object
    warnings: list[str]                     # translated, user-facing
    mime_types: list[MimeTypeDef]           # the app's own file types (usr/share/mime/packages/*.xml,
                                            # read only if the desktop entry has MimeType=)
    # 0.2 (all with defaults, see §22, §24, §25):
    update_source: UpdateSource | None      # choose_source(.upd_info, resources/app-update.yml)
    origin_url: str | None                  # origin.read_origin(path)
    signature: SignatureInfo | None         # signature.read_signature(path, elf); None = not signed
    data_hints: list[str]                   # appdata.data_hints_for(Name, app id, desktop stem,
                                            # StartupWMClass, program of Exec unless AppRun/env/sh)
    def cleanup(self) -> None; __enter__/__exit__ (cleanup on exit)

def inspect_appimage(path, *, compute_hash: bool = True,
                     progress: Callable[[float | None, str], None] | None = None) -> AppImageInfo
```
Algorithm:
1. `path = Path(path).resolve()`; must be a regular file; `read_elf_info`; payload must be squashfs
   (type 2) or type 1 (fallback reader). Otherwise `NotAnAppImageError("This file is not an AppImage.")`.
2. `open_payload`; extract top-level `*.desktop` (at most 16) and `.DirIcon` into `work_dir/root`.
   With member sizes (listing) no file above its limit (desktop 1 MiB, icon 10 MiB, MIME package
   256 KiB) is ever extracted - repeated bytes compress ~1000:1, a small AppImage must not fill
   /tmp by being opened. The work dir is tied to an owner object right after `mkdtemp`, so it is
   also removed when the program ends during the inspection (e.g. Ctrl+Q while reading).
3. Pick the desktop file: prefer `Type=Application` and not `NoDisplay=true`, then one whose stem
   matches `.DirIcon`'s link target stem, then alphabetical.
4. Icon resolution (resolve symlinks *inside* the payload by extracting link targets, max 8 hops, target
   must stay inside the root, never follow to the host FS). Order: for the desktop `Icon=` name
   (strip a leading "/" and any extension): `usr/share/icons/hicolor/scalable/apps/<n>.svg`, then
   `usr/share/icons/hicolor/<S>x<S>/apps/<n>.png` for S in 512,256,1024,192,128,96,64,48,32 (use
   `list_members()` when available, else try-extract candidates), then `<n>.svg|png|xpm` at root,
   `usr/share/pixmaps/<n>.*`, then `.DirIcon`. Reject files > 10 MiB or not probe-able.
5. `is_electron`: a member named `chrome-sandbox` or `chrome_crashpad_handler`, or `…/resources/app.asar`,
   at most 5 path components deep (repacked .deb layouts keep them in `opt/<App>/`); without a listing
   only `chrome-sandbox` is probed at the root and next to the Exec program (if it is a relative path).
5b. `mime_types`: up to 8 `usr/share/mime/packages/*.xml` (≤ 256 KiB each), parsed as bytes with
   `integration.parse_mime_package` (so a declared encoding / BOM is honoured). `X-AppImage-Version` is normalised (`normalize_version`: "v1.2" → "1.2").
6. `sha256` streamed in 1 MiB chunks when `compute_hash`.
7. Architecture: `arch` from ELF; plus a warning if `elf.is_foreign_arch` (e_machine differs from the
   running Python's; falls back to comparing with `platform.machine()`). A type-2 AppImage whose
   payload is not squashfs (e.g. DwarFS) gets NotAnAppImageError("This AppImage is packed in a format
   that Easy Installer cannot read yet.") — reading it would mean running its runtime.
8. (0.2) `update_source`: `resources/app-update.yml` (≤ 64 KiB) is looked up in the listing that is
   read anyway — at the root or where a repacked .deb keeps the app (`opt/<App>/resources/`, ≤ 5
   path components; without a listing only at the root of Electron apps) — and combined with
   `.upd_info` by `updates.choose_source`. `origin_url`, `signature` (unsigned files cost one
   small read; a signed one is hashed once more for gpg) and `data_hints` as above. None of
   these can fail an inspection: odd values mean None / [].
Never modifies the source file (except the documented fallback chmod u+x).

## 9. `core/integration.py` (shared by user-scope installer and root helper)

```python
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")   # always use ID_RE.fullmatch()
DESKTOP_PREFIX = "easyinstaller-"
def sanitize_id(s: str) -> str | None
def derive_app_id(desktop_filename: str | None, name: str | None, filename: str) -> str
    # 1. desktop stem (e.g. "org.freecad.FreeCAD", "t3code") unless generic ("appimage","apprun","app","default")
    # 2. slug of Name  3. if a (non-generic) desktop stem or Name exists but has no ASCII:
    #    "app-" + first 10 hex digits of sha256(NFC text of the stem, else Name) - never from the file
    #    name then, which installing changes ("App.AppImage"): the installed file is the same app
    # 4. slug of filename stem with version/arch/".AppImage" removed  5. "app-" + hash of that name
    #    (else of the stem) - never a shared constant, or an unrelated app would "update" (replace) it
def parse_version_from_filename(filename: str) -> str | None
    # "FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"->"1.1.3", "OpenSCAD-2026.03.28-x86_64.AppImage"->"2026.03.28"
    # never matches the "86_64" of x86_64; needs at least one dot ("0.9.0", "1.2")
def compare_versions(a: str | None, b: str | None) -> int      # natural compare; None < anything;
                                                               # a leading v/V before a digit is ignored
def safe_file_stem(name: str) -> str       # "T3 Code (Alpha)" -> "T3-Code-Alpha"; ASCII [A-Za-z0-9._-], ≤ 64 chars
def desktop_file_name(app_id: str) -> str  # "easyinstaller-<id>.desktop"
def icon_name(app_id: str) -> str          # "easyinstaller-<id>"
def apparmor_profile_name(app_id: str) -> str   # "easyinstaller-<id>"
def appimage_target(layout: Layout, app_id: str, name: str,
                    owner_of: Callable[[Path], str | None], source: Path | None = None) -> Path
    # layout.apps_dir / f"{safe_file_stem(name)}.AppImage"; if that path exists and is not `source`
    # and owner_of(path) != app_id -> f"{stem}-{app_id}.AppImage"
def icon_target(layout: Layout, app_id: str, info: ImageInfo) -> Path
    # layout.icons_dir / hicolor_subdir(info) / f"{icon_name(app_id)}.{extension_for(info)}"

@dataclass
class DesktopRenderSpec:
    app_id: str; name: str; appimage_path: Path; icon_name: str | None
    embedded: DesktopEntry | None; embedded_stem: str | None
    comment: str | None; version: str | None; scope: Scope
    extra_args: tuple[str, ...] = ()        # only "--no-sandbox" is ever used
    extract_and_run: bool = False           # prefix env APPIMAGE_EXTRACT_AND_RUN=1
    uninstall_command: tuple[str, ...] | None = None   # e.g. ("/usr/bin/easy-installer", "--uninstall", id)
def render_desktop_entry(spec: DesktopRenderSpec) -> str

@dataclass(frozen=True)
class MimeTypeDef:                        # type, comment, globs (only "*.ext"), sub_class_of; from_dict validates:
                                          # the media type (also of parents) must be one of application, audio,
                                          # chemical, font, image, message, model, multipart, text, video -
                                          # update-mime-database (as root for system apps) writes
                                          # <mime_dir>/<media>/<subtype>.xml, so "packages/x" would replace
                                          # another package and "mime.cache/x" break the database
def parse_mime_package(text: str | bytes) -> list[MimeTypeDef]   # shared-mime-info XML (magic/icons ignored)
def host_mime_database(directory=HOST_MIME_DIR) -> HostMimeDatabase   # /usr/share/mime types + globs2
def select_mime_types(defs, wanted, host) -> list[MimeTypeDef]
    # only types listed in the launcher's MimeType= (no x-scheme-handler/…), unknown to the host, with
    # only patterns that take no file from a host pattern (an app must never take over e.g. *.txt):
    # xdgmime tries "*.ext" globs before full globs and prefers the longest suffix, so "*.1" (man
    # pages, "*.[1-9]"), "*.so.6" ("*.so.[0-9]*") or "*.min.js" ("*.js") are dropped; ≤ 16 types, ≤ 8 globs
def render_mime_package(app_id, defs) -> str               # our own XML, escaped
def mime_package_name(app_id) -> str                       # "easyinstaller-<id>.xml"
```
`render_desktop_entry` rules (start from a copy of the embedded entry or `DesktopEntry.new()`):
* `Type=Application`; `Name` = embedded Name or spec.name (keep all `Name[xx]`, `Comment[xx]`,
  `GenericName`, `Keywords`, `MimeType`, `Categories`, `StartupNotify`, `Terminal`, `Path`).
* `Exec` = `rewrite_exec(embedded Exec or "", str(appimage_path), extra_args, env, keep_env=...)`; if the
  embedded entry has no Exec: `join_exec([path, *extra_args]) + " %U"`-free plain command. Embedded
  `env VAR=value` assignments are only kept for a small allowlist (`SAFE_EXEC_ENV`: QT_QPA_PLATFORM,
  GDK_BACKEND, ELECTRON_OZONE_PLATFORM_HINT, scaling, LANG, ...) and never with a "/" in the value (no
  LD_PRELOAD, *PATH, GIO_/GTK_ modules, ...).
  Same rewrite for every `[Desktop Action *]` group's `Exec` whose program is the app (AppRun / same
  program token as main Exec, even an absolute one / the embedded desktop file's stem / a relative
  path containing "/") — all other actions (other absolute host paths, `xdg-open`, `sh`, ...) are dropped.
* Localized `Exec[xx]`, `TryExec[xx]` and `Icon[xx]` are removed in every group, `[X-…]` groups included
  (GLib/KDE would prefer them). Lines GLib would reject (non-ASCII "whitespace" such as U+00A0, a key
  whose locale is not letters/digits/`-_.@`) or that other parsers split (a lone CR, U+2028, ...) are
  dropped, as are lines with any other control character but TAB (C0, DEL, C1 - mojibake) and groups
  whose name has one (GLib refuses the whole file); string values with escapes GLib rejects (`\x`,
  `\;` outside lists, a trailing `\`) get literal backslashes.
* `TryExec` = appimage path (plain string, no quoting). `Icon` = spec.icon_name or
  "application-x-executable" (also replace an action's `Icon` if equal to the embedded main `Icon`).
* `StartupWMClass`: keep; if missing and `embedded_stem` → set it to `embedded_stem`.
* Remove: `DBusActivatable`, `Hidden`, `NoDisplay`, `X-AppImage-Integrate`, `X-AppImageLauncher-*`,
  `X-GNOME-Autostart*`, `Implements`.
* `Categories` default `Utility;` if missing; `Comment` default spec.comment if missing.
* Add `X-EasyInstaller-Id=<id>`, `X-EasyInstaller-Scope=<user|system>`, `X-AppImage-Version=<version>` (if known).
* If `uninstall_command`: append action id `easyinstaller-uninstall` to `Actions=` and add group
  `[Desktop Action easyinstaller-uninstall]` with `Name=Uninstall…`, `Name[de]=Deinstallieren…`,
  `Name[nl]=Verwijderen…`, `Icon=user-trash-symbolic`, `Exec=join_exec(uninstall_command)`.
* Output must pass `desktop-file-validate` for the sample AppImages (warnings tolerated).

## 10. `core/paths.py`

```python
class Scope(str, Enum):  USER = "user";  SYSTEM = "system"
@dataclass(frozen=True)
class Layout:
    scope: Scope
    apps_dir: Path        # USER: $EASY_INSTALLER_APPS_DIR or ~/Applications   SYSTEM: <root>/opt/appimages
    desktop_dir: Path     # USER: $XDG_DATA_HOME/applications                  SYSTEM: <root>/usr/local/share/applications
    icons_dir: Path       # USER: $XDG_DATA_HOME/icons/hicolor                 SYSTEM: <root>/usr/local/share/icons/hicolor
    registry_path: Path   # USER: $XDG_DATA_HOME/easy-installer/registry.json  SYSTEM: <root>/var/lib/easy-installer/registry.json
    apparmor_dir: Path    # <root>/etc/apparmor.d (both scopes; root only)
    mime_dir: Path        # property: desktop_dir.parent / "mime" (packages in mime_dir/packages)
def user_layout(env: Mapping[str, str] | None = None) -> Layout     # XDG_DATA_HOME default ~/.local/share; HOME from env
def system_layout(root: Path = Path("/")) -> Layout
def layout_for(scope: Scope) -> Layout
def config_dir(env=None) -> Path       # $XDG_CONFIG_HOME/easy-installer
def settings_path(env=None) -> Path    # config_dir()/settings.json
```

## 11. `core/registry.py`

```python
@dataclass
class InstalledApp:
    id: str; name: str; version: str | None; scope: Scope
    appimage_path: str; desktop_path: str; icon_paths: list[str]; icon_name: str
    apparmor_profile: str | None          # absolute path of profile file or None
    sandbox_fix: str                      # "none" | "apparmor" | "no-sandbox"
    extract_and_run: bool
    extract_and_run_explicit: bool        # chosen with --extract-and-run yes|no, not from FUSE: never
                                          # switched automatically, kept by Repair
    sha256: str | None; size: int; arch: str
    update_info: str | None; original_filename: str; comment: str | None
    installed_at: str; updated_at: str    # ISO-8601 UTC ("2026-09-29T15:04:05Z")
    installer_version: str
    mime_package: str | None              # <mime_dir>/packages/easyinstaller-<id>.xml or None
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "InstalledApp"   # tolerant of unknown/missing optional keys
    def status(self) -> str               # "ok" | "missing-appimage" | "missing-launcher"
    @property
    def icon_path(self) -> str | None     # first existing icon path

class Registry:
    def __init__(self, path: Path)
    def load(self) -> dict[str, InstalledApp]   # missing -> {}; corrupt JSON -> rename to "<name>.corrupt-<n>" + {} + log warning
    def all(self) -> list[InstalledApp]         # sorted by name.casefold()
    def get(self, app_id: str) -> InstalledApp | None
    def owner_of(self, appimage_path: Path) -> str | None
    def put(self, app: InstalledApp) -> None    # atomic: tmp file in same dir + fsync + os.replace; fcntl.flock on "<path>.lock"
    def remove(self, app_id: str) -> None
    def locked(self, *, timeout: float | None = None, on_wait=None)   # context manager: hold the lock for a
        # whole installation (re-entrant for the same Registry object; put/remove inside do not lock
        # again); on_wait() once if busy; after `timeout` s InstallError("Another installation is running…")
```
File format: `{"format": 1, "apps": {"<id>": {...}}}`. File mode 0644 (system registry world-readable).
The lock file is 0600 (existing ones are tightened): flock() works on any open descriptor, so a
readable lock file would let every local user block the system registry. Paths that are not valid
UTF-8 are written with `\u` escapes (they round-trip); `original_filename` is stored as display text.

## 12. `core/system_checks.py`

```python
@dataclass(frozen=True)
class SystemStatus:
    unsquashfs: str | None; pkexec: str | None; apparmor_parser: str | None
    update_desktop_database: str | None; icon_cache_tool: str | None; desktop_file_validate: str | None
    libfuse2: bool            # libfuse.so.2 found (ldconfig -p, else glob /usr/lib*/**/libfuse.so.2*, /lib*/...)
    fusermount: str | None    # fusermount3 or fusermount
    dev_fuse: bool            # /dev/fuse exists
    userns_restricted: bool   # /proc/sys/kernel/apparmor_restrict_unprivileged_userns == "1"
    apparmor_enabled: bool    # /sys/kernel/security/apparmor exists (or /sys/module/apparmor/parameters/enabled == "Y")
    distro_id: str | None; distro_like: tuple[str, ...]; distro_version: str | None
    has_apt: bool
    libfuse2_package: str     # "libfuse2t64" on Ubuntu ≥ 24.04 / Debian ≥ 13 (and derivatives: Zorin 18, Mint 22, Pop 24.04), else "libfuse2"
    def can_run_appimage(self, elf: ElfInfo) -> bool
        # dynamic runtime (has_interp): libfuse2 and dev_fuse and fusermount; static runtime: dev_fuse and fusermount
def get_system_status(refresh: bool = False) -> SystemStatus    # cached (module-level)
```

## 13. `core/sandbox.py`

```python
class SandboxFix(str, Enum): NONE = "none"; APPARMOR = "apparmor"; NO_SANDBOX = "no-sandbox"
def needs_sandbox_fix(info: AppImageInfo, status: SystemStatus) -> bool
    # info.is_electron and status.userns_restricted and not info.exec_has_no_sandbox
def default_sandbox_fix(status: SystemStatus) -> SandboxFix   # APPARMOR if apparmor_parser & pkexec & apparmor_enabled else NO_SANDBOX
def apparmor_quote_path(path: str) -> str                    # escape AppArmor glob chars (*?[]{}^") and wrap in quotes
def render_apparmor_profile(app_id: str, appimage_path: str) -> str
```
Profile template (Ubuntu 24.04+ user-namespace restriction for Electron/Chromium apps):
```
# Managed by Easy Installer — allows <id> to use unprivileged user namespaces
# (required by the Chromium/Electron sandbox on Ubuntu 24.04 and newer).
abi <abi/4.0>,
include <tunables/global>

profile easyinstaller-<id> "<appimage path, escaped>" flags=(unconfined) {
  userns,

  include if exists <local/easyinstaller-<id>>
}
```

## 14. `core/installer.py`

```python
@dataclass
class InstallOptions:
    scope: Scope = Scope.USER
    keep_original: bool = False              # default: MOVE the file (like the article), copy if True
    sandbox_fix: SandboxFix | None = None    # None = automatic (only used when needs_sandbox_fix)
    extract_and_run: bool | None = None      # None = automatic (True if not status.can_run_appimage(elf));
                                             # True/False is recorded as extract_and_run_explicit
    add_uninstall_action: bool = True        # only effective if an installed `easy-installer` launcher is found
    allow_foreign_arch: bool = False

@dataclass
class InstallPlan:
    info: AppImageInfo; options: InstallOptions; layout: Layout
    app_id: str; name: str
    target_appimage: Path; desktop_path: Path; icon_name: str; icon_target: Path | None
    desktop_text: str
    action: str                     # "install" | "update" | "reinstall" | "downgrade"
    existing: InstalledApp | None   # same id, same scope
    existing_other_scope: InstalledApp | None
    sandbox_fix: SandboxFix         # resolved (NONE if not needed)
    extract_and_run: bool           # resolved
    uninstall_command: tuple[str, ...] | None
    apparmor_profile_text: str | None
    requires_root: bool             # scope == SYSTEM or sandbox_fix == APPARMOR
    in_place: bool                  # source file already is the target
    warnings: list[str]             # translated
    version: str | None             # recorded version: info.version, or - if that is unknown and the file is
                                    # the installed one (same path or sha256) - the installed version
    mime_types: list[MimeTypeDef]   # select_mime_types(info.mime_types, launcher MimeType=, host database)
    source_is_installed: bool       # the file is another installation's registered app file (e.g. the
                                    # "only for me" copy when installing for everyone): keep_original is
                                    # forced and a warning explains it

def find_uninstall_launcher(scope: Scope = Scope.USER) -> str | None
    # absolute path of an installed `easy-installer` launcher (shutil.which, /usr/bin, /usr/local/bin, ~/.local/bin)
    # only if it is NOT inside a source checkout's temp/venv — otherwise None. SYSTEM: only launchers the
    # helper trusts (helper.ops.is_trusted_executable), /usr/bin and /usr/local/bin first
def plan_install(info: AppImageInfo, options: InstallOptions | None = None, *,
                 status: SystemStatus | None = None) -> InstallPlan
    # raises UnsupportedArchitectureError if e_machine != the host's (elf.is_foreign_arch) and not
    # allow_foreign_arch. action: install (no entry with this id in this scope) / update / downgrade /
    # reinstall by compare_versions; if one version is unknown: reinstall when both are and the file
    # is the installed one (path or sha256), else update (never "downgrade"). The installed file
    # itself (user scope) is always planned in place; the registered file of another entry (other
    # scope or id) never is: keep_original is forced and a copy is installed. A registered app with
    # the same name but another id gives a warning (it is not replaced).
def execute_install(plan: InstallPlan, *, progress: Callable[[float | None, str], None] | None = None) -> InstalledApp
    # warnings that come up while installing (AppArmor fallback, original file not deletable, the
    # helper's `warnings`) are appended to plan.warnings; CLI/GUI show the ones added afterwards
def uninstall(app_id: str, scope: Scope, *, progress=None, keep_permission: bool = False) -> list[str]
    # NotInstalledError if unknown; returns translated notes (e.g. "The special permission that X
    # needed could not be removed."); keep_permission: a per-user app is removed without the
    # administrator password, its AppArmor profile stays (and a note says so)
def uninstall_needs_password(app: InstalledApp) -> bool   # system app, or a user app with an AppArmor profile
def can_keep_permission(app: InstalledApp) -> bool        # a user app with an AppArmor profile
def list_installed(scopes: Iterable[Scope] = (Scope.USER, Scope.SYSTEM)) -> list[InstalledApp]
def find_installed(app_id: str) -> list[InstalledApp]
def refresh_desktop_caches(layout: Layout, *, mime: bool = False) -> None
    # update-desktop-database <desktop_dir>; icon cache tool -f -t <icons_dir> only if icon-theme.cache exists
    # there (user scope); then always bump the mtime of <icons_dir> (like xdg-icon-resource: running
    # GNOME Shell / GTK only rescan a theme when its folder's mtime changes); with `mime` also
    # `update-mime-database <mime_dir>` — failures are logged, never raised
def update_start_mode(app, status=None) -> InstalledApp | None   # per-user app with automatic (not
    # extract_and_run_explicit) extract_and_run whose runtime can now mount (FUSE installed): launcher
    # without APPIMAGE_EXTRACT_AND_RUN=1, registry updated
def update_start_modes(status=None) -> list[InstalledApp]
```
**USER scope `execute_install`:**
0. Hold the user registry's lock for the whole installation (`Registry.locked`, progress "Waiting
   for another installation to finish…"); re-check the plan (same registry entry as when planned, the
   target still free or ours — else InstallError "…Please try again."); remove leftovers of an
   interrupted installation in the folders used (`.<name>.AppImage.part`; `.<name>.easyinstaller-old-*`
   backups are put back if `<name>` is missing and still registered to an app, else deleted - an
   uninstalled app must not come back).
1. mkdir `apps_dir`, `desktop_dir`, icon subdir (0755).
2. Place AppImage: if `in_place` → just chmod 0755. Else copy in 4 MiB chunks (progress) to
   `apps_dir/.<target>.part`, fsync, chmod 0755, `os.replace` to target (same filesystem → prefer
   `os.rename` of the source when moving). When moving across filesystems: delete the source only after
   the target is complete; if deletion fails (read-only media) → warning, not error.
3. Copy icon to `icon_target` (atomic). 4. Write desktop file atomically (0644); if `plan.mime_types`,
   write `<mime_dir>/packages/easyinstaller-<id>.xml` (0644) and run update-mime-database afterwards.
5. If `sandbox_fix == APPARMOR`: `privileged.run_helper("apparmor-install", {"app_id", "appimage_path"})`;
   on `AuthorizationError`/`HelperError` fall back to `NO_SANDBOX` (re-render desktop with
   `--no-sandbox`) and add a warning.
6. Update: remove old AppImage/icons/desktop of the previous version if their paths differ (with
   `keep_original` only the user's original file is kept, never the previous version's own app file;
   an app file another registry entry uses is kept).
7. `Registry.put`, `refresh_desktop_caches`, optionally `desktop-file-validate` (log only).
Rollback on failure: remove files created in this run, move a moved source back.

**SYSTEM scope `execute_install`:** build the manifest (§16) and call `run_helper("install", manifest)`;
the helper computes all target paths itself. If the source lies on a FUSE mount without `allow_other`
(`/proc/self/mountinfo`: gvfs network shares, the document portal, sshfs, rclone, ...), the kernel
refuses the helper even with the caller's euid (its real uid stays 0), so the client first copies the
file into a private 0700 folder (`$XDG_CACHE_HOME/easy-installer/staging-*`, else `/var/tmp`) and sends
that copy as `source_appimage` (deleted afterwards; leftovers older than a day are removed). On success,
if not `keep_original`, the client deletes the source file (it is user-owned). Returns
`InstalledApp.from_dict(result["app"])`.

**`uninstall`:** USER (under the registry lock): AppArmor profile first → `run_helper("apparmor-remove",
{"app_id"})` — a cancelled/refused password prompt (AuthorizationError) cancels the uninstall with
nothing changed, and CLI (`--keep-permission`) and GUI ("Uninstall … without the password?") then
offer `keep_permission=True` (a standard user cannot give an administrator's password; ssh has no
password agent); another helper failure keeps the profile and adds a note; then delete AppImage
(unless another registry entry uses the same file), desktop file, icons, registry entry (missing
files are fine) and the backups an interrupted update left next to them. CLI and GUI say beforehand that the password may be asked for when
`uninstall_needs_password(app)`.
SYSTEM: `run_helper("uninstall", {"app_id"})`. Then refresh caches. User data (~/.config/<app>) is never touched.

## 15. `core/privileged.py`

```python
HELPER_INSTALLED_PATH = "/usr/libexec/easy-installer/easy-installer-helper"
HELPER_LOCAL_PATH = "/usr/local/libexec/easy-installer/easy-installer-helper"   # `make install` default
def helper_command() -> list[str]
    # first of HELPER_INSTALLED_PATH / HELPER_LOCAL_PATH that exists, is root-owned and not
    # group/world-writable; else dev mode:
    # [pkexec, "/usr/bin/python3", "<abs path to src/easy_installer/helper/entry.py>"]
    # (installed: [pkexec, HELPER_INSTALLED_PATH])
def run_helper(op: str, payload: dict, *, timeout: float | None = 600) -> dict
    # runs helper_command() + [op], JSON payload on stdin (an anonymous private temp file, so a
    # long password prompt never blocks on a full pipe), JSON on stdout.
    # Timeout: kill pkexec if still possible (password prompt) -> HelperError "took too long"; once the
    # helper runs as root it cannot be killed (EPERM): wait for it (it has its own limits) and use its
    # answer. Ctrl+C: the helper gets SIGINT too; wait for its answer; ok -> return it, else re-raise
    # KeyboardInterrupt (CLI: "Cancelled.", exit 3). The helper ignores SIGINT once its change is saved
    # (`ops.on_committed`: after the registry write, before the first uninstall deletion), so its
    # answer stays truthful. "could not be started" only if Popen fails.
    # exit 126/127 from pkexec -> AuthorizationError; {"ok": false, "error": msg} -> HelperError(msg);
    # missing pkexec -> HelperError
    # env EASY_INSTALLER_DISABLE_PKEXEC=1 (set by tests) -> HelperError without running anything
```
As built (0.2 review): every request carries `"client_version": __version__`; the helper
refuses a request of another version (HelperError "…belongs to another version of Easy
Installer…", details "helper X, program Y"), and before an installed helper is started its
version is found out without the password (`installed_helper_version(path)`: its interpreter
imports the package of its `_LIBDIR` as the user) - another version than the program's is never
started (an older helper would misread the request and drop every field it does not know from
the system registry). A helper script of the running package (dev mode) is never checked.

## 16. `helper/` (runs as root via pkexec)

* `main.py: main(argv) -> int`: argv[1] = op ∈ {"install","uninstall","apparmor-install",
  "apparmor-remove","install-fuse"}; read ≤ 1 MiB JSON from stdin; caller uid/gid from `PKEXEC_UID`
  (must be set and numeric when euid == 0) and `pwd`; dispatch to `ops`; print one JSON object
  `{"ok": true, ...}` or `{"ok": false, "error": "..."}`; exit 0/1. Never trusts paths from the client
  for destinations; always `system_layout(Path("/"))`. `umask(0o022)`.
* `ops.py` (pure, testable with `layout=system_layout(tmp)` and `run_commands=False`):
  ```python
  def op_install(m: dict, *, layout: Layout, caller_uid: int, caller_gid: int, run_commands: bool = True) -> dict
  def op_uninstall(m: dict, *, layout: Layout, run_commands: bool = True) -> dict
  def op_apparmor_install(m: dict, *, layout: Layout, caller_uid: int, caller_home: Path, run_commands: bool = True) -> dict
  def op_apparmor_remove(m: dict, *, layout: Layout, run_commands: bool = True) -> dict
  def op_install_fuse(m: dict, *, status: SystemStatus, run_commands: bool = True) -> dict
  ```
* Install manifest (client → helper):
  `{"app_id", "name", "version", "comment", "source_appimage", "sha256", "embedded_desktop" (text|null),
  "embedded_desktop_filename" (str|null), "icon_source" (path|null), "extra_args" (list, only
  "--no-sandbox" allowed), "extract_and_run" (bool), "extract_and_run_explicit" (bool), "apparmor" (bool), "uninstall_command"
  (list|null), "arch", "size", "update_info", "original_filename", "is_electron", "mime_types" (list of
  MimeTypeDef dicts)}`. The helper validates `mime_types` (MimeTypeDef.from_dict), selects again with
  the embedded MimeType= and /usr/share/mime, renders the XML itself into
  /usr/local/share/mime/packages/easyinstaller-<id>.xml and runs `update-mime-database /usr/local/share/mime`.
* Security requirements:
  - Validate `app_id` with `ID_RE`; `name` ≤ 200 chars, no control chars; unknown keys ignored.
    `embedded_desktop` is only parsed (no lone surrogates): the renderer drops lines with control
    characters exactly as for per-user installs, and `verify_desktop` checks the result.
  - **Open every client-supplied source file with the caller's privileges** (temporarily
    `os.setegid(caller_gid)`/`os.seteuid(caller_uid)` around `open(path, O_RDONLY|O_NOFOLLOW|O_CLOEXEC)`,
    then restore root) so the helper can never be used to read files the caller could not read.
    Skip the switch when not running as root (tests). `fstat` must show a regular file.
  - AppImage source must start with the ELF magic; sha256 computed while copying must equal the
    manifest's sha256 (else abort + delete partial file). Icon ≤ 10 MiB and `probe_image` must succeed.
  - Destinations computed by the helper (`integration.appimage_target`, `icon_target`,
    `desktop_file_name`); written atomically, owner root:root, AppImage 0755, others 0644;
    refuse to write through symlinks (`O_NOFOLLOW`, check parents are real dirs).
  - `uninstall_command` accepted only if it is exactly `[<abs path of a root-owned existing file named
    easy-installer>, "--uninstall", app_id]`; otherwise dropped.
  - The rendered launcher is verified before it is written (`verify_desktop`): every Exec (main and
    actions) runs the installed AppImage (the uninstall action: the trusted launcher), `env`
    assignments only as allowed by `exec_env_allowed`, no `Exec[xx]`/`TryExec[xx]`/`Icon[xx]` keys
    and no control characters but TAB/LF (other parsers read some as line breaks).
  - `op_uninstall` deletes only paths recorded in the system registry that lie inside the layout's
    dirs (`apps_dir`, `desktop_dir`, `icons_dir`, `apparmor_dir/easyinstaller-*`,
    `mime_dir/packages/easyinstaller-<id>.xml`).
  - `op_apparmor_install`: `appimage_path` must be absolute, a regular non-symlink file inside the
    caller's home **or** inside `layout.apps_dir`, owned by the caller (or root for system apps); write
    `apparmor_dir/easyinstaller-<id>` and run `apparmor_parser -r <file>`.
  - `op_install_fuse`: only `apt-get install -y <status.libfuse2_package>` (fixed allowlist
    {"libfuse2","libfuse2t64"}), `DEBIAN_FRONTEND=noninteractive`, only if `has_apt`.
  - System registry at `layout.registry_path` is updated by the helper (0644). `install` and
    `uninstall` take its lock (0600) before touching any file and hold it to the end; if another
    operation keeps it for 120 s they fail with "Another installation is running…" and change nothing.
  - After changes: `update-desktop-database /usr/local/share/applications`,
    `gtk-update-icon-cache -f -t /usr/local/share/icons/hicolor` and bump the mtime of that hicolor
    folder (when `run_commands`).
* Result of `op_install`: `{"ok": true, "app": InstalledApp.to_dict(), "action": "install"|"update",
  "warnings": [...]}`. `warnings` and every `error` are the helper's **English** source strings (it
  runs without the user's locale); the client passes them through `_()` and appends `warnings` to
  `plan.warnings` (e.g. "started without its built-in security sandbox" when the AppArmor profile
  could not be installed and the helper fell back to `--no-sandbox`).
  `apparmor-install` returns `profile_path`, `profile_name`, `appimage_path` (resolved path the
  profile is attached to); `apparmor-remove` returns `removed`, `reason`; `uninstall` returns
  `removed`, `skipped`; `install-fuse` returns `package`. Errors: `{"ok": false, "error", "details"}`.
* AppArmor profiles are named per app id only (`easyinstaller-<id>`), so all scopes/users share one
  file. The helper only replaces/removes a profile carrying the "# Managed by Easy Installer" marker
  whose attachment path is inside the caller's home (user ops) or `apps_dir` (system ops); on a
  conflict the client falls back to `--no-sandbox`.
* `data/com.roothirsch.EasyInstaller.policy`: action `com.roothirsch.EasyInstaller.manage`,
  `allow_active=auth_admin_keep`, annotation `org.freedesktop.policykit.exec.path` =
  `/usr/libexec/easy-installer/easy-installer-helper`; message in en/de/nl.

## 17. CLI (`cli.py`, `__main__.py`)

```
easy-installer                         → GUI
easy-installer FILE.AppImage [...]     → GUI, opens the install dialog for FILE
easy-installer --uninstall ID          → GUI, asks for confirmation (used by the desktop action)
easy-installer install FILE [--system] [--keep] [--sandbox-fix auto|apparmor|no-sandbox|none]
                                        [--extract-and-run auto|yes|no] [--allow-foreign-arch] [-y]
easy-installer info FILE [--json]
easy-installer list [--json] [--user|--system]
easy-installer uninstall ID [--system|--user] [--keep-permission] [-y]
easy-installer launch ID
easy-installer check [--json]           (system status + human hints)
```
`__main__.main(argv=None) -> int`: no arguments, a window option (`--uninstall[=ID]`, `--`) or a file
as `argv[1]` (it exists, contains "/", ends with ".AppImage" in any case, or is a URI) →
`gui.application.run(argv)` (import lazily so the CLI works without GTK); everything else
(install, info, list, uninstall, launch, check, --help, -h, --version, but also typos such as
"remove" or "help") → `cli.main(argv)`, where argparse explains unknown commands (exit 2). Leading
-v/--verbose flags are skipped for this decision. CLI prints friendly messages (translated), exit codes: 0 ok, 1 error, 2 usage,
3 cancelled (also: no answer because stdin is empty or closed). `install` shows a summary and asks
`Proceed? [Y/n]` unless `-y`. Output is flushed before returning, so a reader that quits early
(`| true`) never causes "Exception ignored … BrokenPipeError" / exit 120; other write errors (a full
disk) give "Error: The output could not be written: …" and exit 1. Paths and file names that are not
UTF-8 are shown with U+FFFD (`installer.display_text`), also in `info --json`.

## 18. GUI (`gui/`) — libadwaita 1.5

* `application.py`: `class EasyInstallerApp(Adw.Application)` with
  `application_id=APP_ID`, flags `HANDLES_OPEN | HANDLES_COMMAND_LINE`; `do_command_line` parses file
  args and `--uninstall ID`; `run(argv) -> int`. Actions: `app.open` (Ctrl+O), `app.about`,
  `app.quit` (Ctrl+Q), `app.check-system`. About = `Adw.AboutDialog`. Checks Adw ≥ 1.5 at startup.
* `window.py`: `Adw.ApplicationWindow` 760×640 → `Adw.ToastOverlay` → `Adw.ToolbarView` with
  `Adw.HeaderBar` (start: "Install App…" button with icon; end: primary menu) and `Adw.Banner`s at the
  top of the content (a vertical `Gtk.Box` [banners, stack] — not top bars: ToolbarView 1.5 reserves
  one line per banner, so wrapped banners covered the list):
  (a) "Open AppImages with Easy Installer when you double-click them?" [Make default] — shown if we are
  not the default handler for `application/vnd.appimage` and not dismissed (settings.json);
  (b) FUSE missing → "Apps will start a bit slower because a system component (FUSE) is missing."
  [Install] (helper `install-fuse`, only if `has_apt`). Afterwards (and whenever the system status is
  loaded) apps installed in extract-and-run mode automatically (not `extract_and_run_explicit`) whose
  runtime can now mount are switched back: per-user launchers directly (`update_start_modes`), apps
  for everyone after installing FUSE via "Repair" (the helper; the password is usually still
  remembered; a cancelled prompt stops it for all of them).
  Content `Gtk.Stack`: "empty" `Adw.StatusPage` (app icon, "No apps installed yet", text explaining
  drag & drop / double-click, pill button "Choose AppImage…") and "list": `Adw.Clamp` →
  `Adw.PreferencesGroup` "Installed Apps" with one `AppRow` per app (+ a `Gtk.SearchEntry` when > 6 apps).
  Whole window is a `Gtk.DropTarget` for `Gdk.FileList` with a visible "Drop to install" overlay.
  Files dropped/opened while an install dialog is open are queued and acknowledged with a toast in
  that dialog. Quit (Ctrl+Q) drops the queue and quits (unless something is being installed); a
  close request with the install dialog on top closes it (libadwaita) and drops the queue (an alert
  on top of the install dialog just closes). File names that are not UTF-8 are shown with U+FFFD.
  Reload the list after every change and on window focus.
* `app_row.py`: `Adw.ActionRow` — prefix 48 px icon (`Gio.FileIcon` from `icon_path`, fallback
  `application-x-executable`), title = name, subtitle = "Version X · Only for me" / "For all users" or a
  problem text ("App file is missing"); suffix: "Open" button (hidden if broken) and a menu button
  (Show in Files [`Gtk.FileLauncher.open_containing_folder`], Repair [if missing-launcher], Uninstall…).
  Launch via `Gio.DesktopAppInfo.new_from_filename(desktop_path).launch([], ctx)`.
* `install_dialog.py`: `Adw.Dialog` (content width 460) with `Adw.ToolbarView` + `Gtk.Stack` pages:
  1. *loading* — `Gtk.Spinner` + "Reading app information…" (inspection runs in a thread);
  2. *confirm* — 128 px icon, name (`title-1`), "Version · size" (`dim-label`), comment; group
     "Install for": two `Adw.ActionRow`s with grouped `Gtk.CheckButton` radios ("Only for me" default,
     "Everyone on this computer — requires your password"); update/reinstall/downgrade info row
     ("Version 1.1.3 is installed and will be replaced by 1.1.4"); an `Adw.ExpanderRow` "More options":
     `Adw.SwitchRow` "Keep the original file" and, only if a sandbox fix is needed, an `Adw.ComboRow`
     (Automatic / Allow with system permission (recommended) / Start without sandbox) with an
     explanation row; warnings (arch, extract-and-run) as rows with `dialog-warning-symbolic`; target
     path in small dim text; trust note "Only install apps from sources you trust."; bottom bar
     Cancel / **Install** (`suggested-action`).
  3. *progress* — `Gtk.ProgressBar` (fraction or pulse) + status text; not closable while running.
  4. *done* — `Adw.StatusPage` "<Name> is installed" + "You can find it in your app menu and search."
     buttons "Open" and "Done".
  5. *error* — `Adw.StatusPage` with the friendly message, details in an expander, "Close".
  `AuthorizationError` → back to *confirm* with a toast "Authentication was cancelled". The dialog's
  `Adw.ToastOverlay` wraps only the page stack, so toasts never cover Cancel/Install; the stack is
  neither h- nor v-homogeneous (hidden pages must not widen the dialog). The "Only for me" subtitle
  says when the password is still needed (sandbox permission). Access keys never clash with the
  main window's (header "Install _App…", empty page "C_hoose AppImage…", FUSE banner "In_stall").
* Uninstall: `Adw.AlertDialog` "Uninstall <Name>?" body "The app will be removed. Your personal files
  and settings are kept." (+ a password note if `uninstall_needs_password`, + "That copy stays
  installed." if the other scope has it too) responses Cancel / Uninstall (`DESTRUCTIVE`), then toast
  ("was uninstalled"; "was removed" only if the app file was already missing), or with notes an
  alert with that heading and the notes (a toast would cut them off). A refused password prompt for
  a per-user app with a profile offers "Uninstall <Name> without the password?" (keep_permission).
* `async_utils.run_in_thread(fn, callback, *args)`: runs fn in a daemon thread and calls
  `callback(result, error)` on the main loop via `GLib.idle_add`. Progress callbacks likewise marshal
  through `GLib.idle_add`. Never touch widgets from worker threads.
* Everything must remain usable with keyboard only and look right in light and dark mode.
* Deliberate deviations (implemented and reviewed via tools/gui_screenshots.py in en/de/nl):
  sentence-case group title "Installed apps"; confirm page icon 96 px (so "More options" fits in
  the default window), done page 128 px; scope rows are title "Everyone on this computer" +
  subtitle "Requires your password" ("Only for me" + "No password needed"); the trust note sits in
  the bottom bar above Cancel/Install; done page button "Open App"; error page offers "Back" to
  the confirm page after a failed install; Adw.Banner has one button, so the default-handler
  banner gets an overlaid close button ("Don't Ask Again", CSS class `dismissable-banner`
  reserves room for it); the keyboard-shortcuts window is shown by our own
  `win.show-help-overlay` action (Gtk.ApplicationWindow.set_help_overlay() connects the
  deprecated GtkWindow::keys-changed signal in GTK 4.14).

## 19. Packaging

* `Makefile`: `dev` (create `.venv` with `--system-site-packages`, install pytest), `test`, `lint`
  (`python3 -m compileall`), `po` (update pot/po via `tools/xgettext.py`), `mo` (compile to
  `build/locale/<lang>/LC_MESSAGES/easy-installer.mo` via `tools/msgfmt.py`), `run`
  (`PYTHONPATH=src python3 -m easy_installer`), `install` (PREFIX=/usr/local, DESTDIR supported),
  `uninstall` (removes exactly what `install` put there with the same PREFIX/DESTDIR; the polkit
  policy only if it points at this PREFIX's helper),
  `install-user` (to `~/.local`: package under `~/.local/share/easy-installer/lib`, launcher
  `~/.local/bin/easy-installer`, desktop file, icons, locale; no polkit policy/helper), `deb`.
* `packaging/build-deb.sh` → `dist/easy-installer_<version>_all.deb`: package to
  `/usr/lib/python3/dist-packages/easy_installer`, `/usr/bin/easy-installer`,
  `/usr/libexec/easy-installer/easy-installer-helper`, desktop file, metainfo, icons, polkit policy,
  `/usr/share/locale/*/LC_MESSAGES/easy-installer.mo`. Depends: `python3 (>= 3.10), python3-gi,
  gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), squashfs-tools, desktop-file-utils, pkexec | policykit-1`.
  Recommends: `libfuse2t64 | libfuse2, apparmor, gpg` (0.2: signatures). postinst/postrm: `update-desktop-database`,
  `gtk-update-icon-cache`.

---

# Version 0.2 — "App registry" features (contract addendum)

Goal: Easy Installer becomes a small, friendly app registry ("like Windows' Apps list, but nicer"):
versions that stay correct, update checks with one-click updates, replace-or-keep-both, rollback to
the previous version, uninstall with optional data clean-up, trust information, and support for
portable archive apps. Everything in §0–§19 stays valid unless changed here.

**Ground rules (additions to §0)**
* The developer machine now has the v0.1 `.deb` installed and REAL apps in `~/Applications`
  (FreeCAD, Pen, T3 Code) with a real registry in `~/.local/share/easy-installer/registry.json`.
  These are the user's working apps: read-only at most (inspection), never modify/move/delete them or
  any real launcher/icon/registry/settings file. All experiments in isolated temp HOMEs.
* **Backward compatibility is mandatory:** a v0.1 `registry.json` and v0.1 launchers must keep
  working unchanged; every new `InstalledApp` field is optional with a default. No format bump.
* **Tests never use the network** (inject fake fetchers or a loopback `http.server`); tests must be
  hermetic w.r.t. the machine (e.g. not depend on whether `/usr/bin/easy-installer` exists).
* Network code: HTTPS only (plain http only for `127.0.0.1`/`localhost` and only when the caller passes
  `allow_insecure_localhost=True`, used by tests), redirects only to https, timeouts, response size
  caps, `User-Agent: EasyInstaller/<version>`. No tokens, no telemetry. The only hosts contacted are the
  ones named by an installed app's own update information.
* The root helper does NOT learn to inspect AppImages, download, extract archives or delete user
  data. Portable apps and data clean-up are per-user only.

## 20. Registry additions (`core/registry.py`)

```python
# new optional InstalledApp fields (defaults in parentheses)
kind: str                 # "appimage" (default) | "portable"
install_dir: str | None   # portable only: the app folder inside apps_dir (None)
mtime_ns: int             # st_mtime_ns of appimage_path when last recorded (0 = unknown)
base_id: str | None       # keep-both copy: id of the main app (None)
pinned: bool              # keep-both copy: never offered updates (False)
previous: dict | None     # backup of the replaced version:
                          # {"version": str|None, "path": str, "sha256": str|None, "size": int, "saved_at": iso}
update_source: dict | None   # UpdateSource.to_dict() (§22) or None
origin_url: str | None       # where the file was downloaded from, if known
signature: dict | None       # SignatureInfo.to_dict() (§25); None = not signed
data_hints: list[str]        # names used to find the app's settings/data folders (§24) ([])
@property
def exec_path(self) -> str   # == appimage_path (for portable apps: the executable inside install_dir)
@property
def main_id(self) -> str     # base_id or id
```
`appimage_path` keeps its name for compatibility; for `kind == "portable"` it is the executable that
the launcher starts. `status()` gains `"changed"` (file on disk differs from the recorded size/mtime —
only reported by `reconcile`, not computed in `status()`).

As built: constants `STATUS_CHANGED`, `KIND_APPIMAGE`, `KIND_PORTABLE`. `from_dict` reads the new
keys tolerantly (wrong types → the default; `previous` is normalised to exactly its five keys, a
record without a `path` is "no backup"; `update_source`/`signature` must be objects, else None).
`make_previous(version=, path=, sha256=, size=, saved_at=None, original_filename=None) -> dict`
builds a `previous` record.
An entry written by 0.1 round-trips with every old key unchanged; 0.1 reading a 0.2 file ignores the
new keys (and drops them if it rewrites the file — `prune_backups` then cleans up the backup).
Since the 0.2 review an entry also keeps the keys it does not know (`InstalledApp.extra`, not
compared, written back as they are): a later version's fields survive this version's rewrites.
`previous` may carry `original_filename` (the name of the file that version was installed from;
`make_previous(..., original_filename=None)`); a rollback records it as the entry's
`original_filename` (unknown for versions kept by 0.2.0: empty, never the wrong name; a kept
folder's marker knows it).

## 21. Settings (`core/settings.py`, stdlib)

```python
@dataclass
class Settings:
    check_updates: bool = True          # look for updates automatically (at most every interval)
    update_interval_hours: int = 24
    backup_days: int = 14               # keep the replaced version this long; 0 = do not keep
    extra: dict                         # every other key of settings.json, preserved verbatim
def load_settings(path: Path | None = None) -> Settings
def save_settings(settings: Settings, path: Path | None = None) -> None      # atomic, never raises
```
`gui/settings.py` keeps its current functions (thin wrappers over the same file; existing keys such
as `default_handler_offer_dismissed` stay valid).

As built: JSON keys `check_updates`, `update_interval_hours`, `backup_days`. A missing, unreadable
or damaged file means the defaults; a value of the wrong type falls back to its default;
`update_interval_hours` is clamped to 1..720, `backup_days` to 0..90 (the helper's limit);
`Settings.keep_backups` = `backup_days > 0`. `save_settings` merges into the file as it is *now*:
the three known keys are always written, of `extra` only what was changed since loading (a key
another window saved in the meantime is not put back; a key removed from `extra` is removed).
Raw access for the other keys: `load_raw`, `save_raw`, `get_setting`, `set_setting`.

## 22. Updates (`core/updates.py` = sources, check, download; `core/updater.py` = orchestration)

```python
# core/updates.py — pure, no registry/installer imports
@dataclass(frozen=True)
class UpdateSource:
    kind: str             # "github-assets" | "electron-github" | "electron-generic" | "zsync-url"
    owner: str | None = None; repo: str | None = None
    release: str | None = None      # github-assets: "latest" | "latest-pre" | "latest-all" | <tag>
    pattern: str | None = None      # github-assets: glob of the AppImage asset (".zsync" suffix removed)
    url: str | None = None          # electron-generic: base URL; zsync-url: URL of the .zsync file
    prerelease: bool = False        # electron-github: releaseType == "prerelease"
    via: str = ""                   # "upd_info" | "app-update.yml"
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "UpdateSource | None"
    def describe(self) -> str       # "github.com/FreeCAD/FreeCAD" / host of url
    def homepage(self) -> str | None   # https://github.com/<owner>/<repo> or None

def parse_update_info(text: str | None) -> UpdateSource | None
    # AppImage ".upd_info": "gh-releases-zsync|owner|repo|release|glob.zsync", "zsync|https://…/x.zsync";
    # anything else (bintray, pling, http://, malformed) -> None
def parse_electron_update_config(text: str | None) -> UpdateSource | None
    # electron-builder "resources/app-update.yml" (flat "key: value"): provider github (owner, repo,
    # releaseType; custom "host" -> None) or generic (url must be https); other providers -> None
def choose_source(update_info: str | None, electron_config: str | None) -> UpdateSource | None
    # prefer the electron source (gives version + sha512), else upd_info

@dataclass(frozen=True)
class AvailableUpdate:
    version: str | None; url: str; filename: str; size: int | None
    sha256: str | None; sha512: str | None        # lower-case hex, when the source publishes them
    release_url: str | None; published_at: str | None
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "AvailableUpdate | None"

@dataclass(frozen=True)
class HttpResponse: status: int; headers: Mapping[str, str]; body: bytes; url: str
Fetcher = Callable[..., HttpResponse]    # fetch(url, *, headers=None, max_bytes=...) ; default = urllib based `http_get`

def check_for_update(source: UpdateSource, *, current_version: str | None, current_sha256: str | None,
                     current_filename: str | None, arch: str, fetch: Fetcher | None = None,
                     etags: dict | None = None) -> AvailableUpdate | None
    # None = up to date (or nothing suitable for this arch). Raises NetworkError / UpdateError.
    # github-assets: api.github.com/repos/<o>/<r>/releases/latest (or /releases for -pre/-all, or
    #   /releases/tags/<tag>); pick the asset matching `pattern` (fnmatch, case-sensitive), if several
    #   prefer the one closest to current_filename (same non-version tokens, same arch token);
    #   version = version parsed from asset name, else tag (leading "v" stripped); sha256 from the
    #   asset's "digest" ("sha256:<hex>") when present. Uses ETag (If-None-Match) via `etags`.
    #   HTTP 403/429 rate limit -> NetworkError with a friendly "try again later" text.
    # electron-github: GET https://github.com/<o>/<r>/releases/latest/download/latest-linux.yml
    #   (aarch64 host: latest-linux-arm64.yml); parse version + files[] (first url ending in
    #   ".AppImage"), sha512 (base64 -> hex), size; download URL = same .../latest/download/<url>.
    #   prerelease=True -> resolve the newest release through the API instead.
    # electron-generic: <url>/latest-linux.yml, file URLs relative to it.
    # zsync-url: GET the .zsync header (first 64 KiB; "Filename", "URL", "SHA-1", "Length"), URL
    #   relative to the .zsync URL; no version: "newer" iff Length/SHA-1 differ from the installed file
    #   (sha1 compare is skipped when unknown -> compare filename + length).
    # Newer = compare_versions(remote, current) > 0; if either version is unknown: newer iff the
    #   sha256 (when published) differs from current_sha256, else iff the remote filename differs
    #   from current_filename.
def download_update(update: AvailableUpdate, dest_dir: Path, *, progress=None,
                    cancel: threading.Event | None = None, allow_insecure_localhost: bool = False) -> Path
    # streams to dest_dir/<safe filename>.part (1 MiB chunks, progress(fraction|None, text)), checks
    # size + sha256/sha512 when known, renames to the final name, returns it. cancel -> UpdateCancelled
    # (partial file removed). Filename sanitised (no path separators), must end in ".AppImage".
class UpdateCache:            # $XDG_CACHE_HOME/easy-installer/updates.json
    def get(self, scope: str, app_id: str) -> AvailableUpdate | None
    def put(self, scope: str, app_id: str, update: AvailableUpdate | None) -> None   # None = "checked, up to date"
    def is_due(self, scope: str, app_id: str, interval_hours: int) -> bool
    def forget(self, scope: str, app_id: str) -> None
    etags: dict

# core/updater.py — orchestration on top of registry/installer
def has_update_source(app: InstalledApp) -> bool          # appimage, not pinned, update_source set
def check_app_update(app: InstalledApp, *, force: bool = False, fetch=None) -> AvailableUpdate | None
def check_all_updates(apps: Iterable[InstalledApp] | None = None, *, force: bool = False,
                      progress=None, fetch=None) -> dict[tuple[str, str], AvailableUpdate]
    # key = (scope.value, id). Per-app errors are collected, not raised: returns also `.errors`
    # (use a small result class `UpdateCheckResult(updates: dict, errors: dict, checked: int)`).
def cached_updates() -> dict[tuple[str, str], AvailableUpdate]     # no network (for `list` and GUI start)
def apply_update(app: InstalledApp, update: AvailableUpdate, *, progress=None,
                 cancel: threading.Event | None = None, allow_signer_change: bool = False,
                 downloader=None) -> InstalledApp
    # download to $XDG_CACHE_HOME/easy-installer/downloads/<id>/ -> inspect -> the file's main id
    # must equal app.main_id and its arch must fit (else UpdateError, file deleted) -> signature
    # continuity (§25) -> plan_install(options copied from the installed app: scope, sandbox_fix,
    # extract_and_run(+explicit), keep_original=False, keep_backup per settings) -> execute_install
    # -> cache.forget. The download folder is always cleaned up.
```
The inspector fills `AppImageInfo.update_source` (reads `resources/app-update.yml`, ≤ 64 KiB, from the
payload when the app is Electron, plus `.upd_info`) and the installer stores it in the registry.
Apps installed by v0.1 get their `update_source` lazily from `update_info`, and from the AppImage
itself the next time they are reconciled or repaired.

**As built (§22)**
* `core/updates.py` — additions to the contract above (all optional, positional order unchanged):
  `check_for_update(..., current_size=None, current_sha1=None)` (a .zsync publishes only SHA-1
  and length); `AvailableUpdate.sha1`; `HttpResponse.truncated`, `.redirects`, `.header(name)`;
  `UpdateCache.checked_at()`, `.save()`. An address that is not secure (http, credentials, a
  redirect away from https) is an `UpdateError`, not a `NetworkError`. electron-github: when the
  redirect chain shows the tagged release, download and release URL are pinned to that tag.
  `parse_electron_update_config` accepts `str | bytes` and answers None for GitHub Enterprise
  hosts, private repositories, draft releases and channels other than `latest`.
* `core/updater.py`:
  ```python
  def update_source_of(app) -> UpdateSource | None   # recorded source, else parse_update_info(app.update_info)
  def has_update_source(app) -> bool     # kind == "appimage", not pinned, no base_id, update_source_of(app)
  def is_newer_than_installed(update, app) -> bool   # a remembered update is still news for the app
  def check_app_update(app, *, force=False, fetch=None) -> AvailableUpdate | None
  def check_all_updates(apps=None, *, force=False, progress=None, fetch=None) -> UpdateCheckResult
  @dataclass
  class UpdateCheckResult(Mapping):      # is the mapping (scope, id) -> update itself
      updates: dict[tuple[str, str], AvailableUpdate]
      errors: dict[tuple[str, str], EasyInstallerError]    # str(error) = the sentence to show
      checked: int                                         # apps with an update source looked at
  def cached_updates() -> dict[tuple[str, str], AvailableUpdate]
  def apply_update(app, update, *, progress=None, cancel=None, allow_signer_change=False,
                   downloader=None, warnings: list[str] | None = None) -> InstalledApp
      # warnings (integration addition): a list that gets the translated notes the user should
      # read after a successful update - what came up while installing (e.g. "The previous
      # version could not be kept…", the helper's notes) and a broken signature of the new
      # file; the plan's other notes (sandbox choice, another installation) are left out.
      # The CLI prints them ("Please note:", JSON key "notes"), the update dialog shows them on
      # its done page.
  class SignerChangedError(UpdateError)  # apply_update without allow_signer_change
  def downloads_dir() -> Path            # $XDG_CACHE_HOME/easy-installer/downloads
  ```
  - A check asks the network only when the last one is older than `Settings.update_interval_hours`
    (or with `force`); otherwise the remembered answer is returned — filtered by
    `is_newer_than_installed`, as are `cached_updates()`: an app that caught up in another way
    (self-update, a file installed by hand) is never offered an old update. `NetworkError` is not
    remembered (tried again next time); `UpdateError` (no/unreadable update information) counts
    as a finished check and is raised again only by a forced check. The check passes
    `current_size=app.size`, `arch=app.arch`, `current_filename=app.original_filename`.
  - `check_all_updates` reports `progress(i/n, "Checking <name>…")`; one app's failure (even a
    bug) never stops the others. Within one run every address is asked once (the same app in
    both scopes shares the answer); a failed request is not shared.
  - `apply_update` works on the registry's current entry (the caller's object may be stale):
    `NotInstalledError` if it is gone, `UpdateError` for a pinned copy or a portable app. The
    download folder is `downloads_dir()/<id>` (0700; leftovers of an interrupted update are
    removed first; the id must match `ID_RE`). The downloader is called as
    `downloader(update, folder, progress=…, cancel=…)` and must return a file in that folder.
    `cancel` is honoured before the download, by the download (also while it connects or waits
    for a stalled server: the connection is made in a helper thread and shut down on cancel,
    so a cancel never waits for `DOWNLOAD_TIMEOUT`), and once more before the installation
    starts (`UpdateCancelled`); after that the installation runs to its end.
    A download that is not an AppImage or is cut off: `UpdateError`. Another app id than
    `app.main_id`, or a file for another kind of computer than the installed one: `UpdateError`.
    The plan is `installer.plan_update(info, app, version_hint=update.version)` (the announced
    version is used when the file does not tell its own, e.g. FreeCAD); `origin_url` of the new
    entry is the download address. Progress: 0–0.7 download, –0.78 checking, –1.0 installing.
    On every error the installed app is byte-for-byte what it was and the download folder is gone.
  - An app that replaced its own file since it was last looked at (`reconcile.needs_reconcile`)
    is reconciled first - before the download and again after it (a self-updater that runs
    when the app is quit) - so nothing is decided, or kept as the previous version, by what
    the app was before. `UpdateNotNeededError` (an `UpdateError`: "<Name> is now at version X,
    so this update is no longer needed.") when the update is then not newer
    (`is_newer_than_installed`): nothing is downloaded or changed. A download whose own version
    is older than the installed one is refused (`UpdateError`), never a silent downgrade.
  - `apply_update(..., confirm_signer_change=None)`: called with the sentence when the signer
    changed, with the file downloaded; True installs it (the CLI asks there - no second
    download), else `SignerChangedError`.
  - The download folder is held (`flock`) while the update runs (`core/downloads.py`:
    `downloads_dir`, `app_download_dir`, `hold`/`release`, `remove_leftovers`): a second update
    of the same app is refused ("… is already being updated"); folders nobody holds whose files
    did not change for 15 minutes are what an update that was stopped hard left - removed at
    GUI start, before CLI `list`/`update`/`details`, and when the app is uninstalled or
    replaced.
  - A failed check (`UpdateError`) is recorded with `UpdateCache.put_failed()` (`"failed": true`,
    the update found before is kept; `UpdateCache.failed()`): it counts as a finished check,
    but a known update is not lost and the CLI never calls it "up to date". A 403 whose body
    mentions "rate limit" (GitHub's secondary limit, without the usual headers) is the
    friendly rate-limit `NetworkError`. Within one `check_all_updates` a request with another
    `If-None-Match` is another request (the retry after an unusable cached body gets a fresh
    answer).
  - `latest-pre` / `latest-all`: of the releases with a matching asset the newest version wins
    (an older pre-release or a backport published last never hides a newer release); equal or
    unknown versions: the order of the list.
  - `installer.execute_install` forgets the remembered update of an app whenever another file
    than the installed one is installed (also by reconcile); `installer.uninstall` forgets it too.

## 23. Reconcile, keep-both, backups and rollback (`core/reconcile.py`, `core/installer.py`)

```python
# core/reconcile.py
def needs_reconcile(app: InstalledApp) -> bool
    # appimage kind only: file exists and (size != app.size or (app.mtime_ns and mtime_ns != app.mtime_ns))
@dataclass
class ReconcileResult: app: InstalledApp; change: str; old_version: str | None; new_version: str | None
    # change: "unchanged" | "recorded" (v0.1 entry: mtime stored) | "updated" | "adopted" | "missing"
    #         | "foreign" (file is now a different app / not an AppImage) | "needs-admin" (system scope)
def reconcile_app(app: InstalledApp, *, progress=None) -> ReconcileResult
def reconcile_all(scopes: Iterable[Scope] = (Scope.USER, Scope.SYSTEM)) -> list[ReconcileResult]
```
* **updated**: an app replaced its own file (self-updater). USER scope: re-inspect, then the normal
  in-place install path (launcher re-rendered, icon/MIME refreshed, registry version/sha256/size/
  mtime_ns/update_source updated) under the registry lock; settings of the installation (sandbox fix,
  extract-and-run, uninstall action) are preserved; no backup is made. SYSTEM scope: no change,
  `"needs-admin"` (the GUI/CLI offers Repair).
* **adopted**: the recorded file is gone, but exactly one unregistered `*.AppImage` in `apps_dir`
  (non-recursive) inspects to the same main id (self-updaters that rename the file) → the entry is
  moved to that file (USER scope only).
* Runs at GUI start (worker thread), before `list`/`update` in the CLI, and when the window regains
  focus (cheap `needs_reconcile` stat first). Must never raise for one broken app.

```python
# core/installer.py additions
InstallOptions.keep_both: bool = False        # install next to the existing version instead of replacing it
InstallOptions.keep_backup: bool | None = None   # None = Settings.backup_days > 0
InstallPlan.keep_both_available: bool         # an installation of the same main id exists in this scope
InstallPlan.backup_target: Path | None        # where the replaced version will be kept
def variant_id(base_id: str, version: str | None, sha256: str | None) -> str
    # f"{base_id}--{slug}" with slug = sanitized version, else first 8 hex of sha256; always ID_RE-valid
def rollback(app_id: str, scope: Scope, *, progress=None) -> InstalledApp     # swap with `previous`
def drop_backup(app_id: str, scope: Scope) -> None
def prune_backups(max_age_days: int, scopes: Iterable[Scope] = (Scope.USER,)) -> list[InstalledApp]
def backups_dir(layout: Layout) -> Path       # layout.apps_dir / ".easyinstaller-backups"
```
* **Keep both** (both scopes; system scope just sends the variant id/name in the manifest): the NEW
  file is installed as a separate app: id `variant_id(...)`, name `"<Name> <version>"` (all localized
  `Name[xx]` get the same suffix), file `<SafeName>-<version>.AppImage`, `base_id` = main id,
  `pinned = True`. The existing entry is untouched. Opening a file whose sha256 equals an installed
  variant = reinstall of that variant. A new file matches the MAIN entry for update/downgrade detection.
* **Backups**: when an update/downgrade replaces a different file and `keep_backup` is on, the old
  file is moved (same filesystem rename) to `backups_dir/<id>/<SafeName>-<version|sha8>.AppImage` and
  recorded in `previous`; an older backup of the same app is deleted (one backup per app). No backup
  for reinstall of the same file, for reconcile, or when the old file is missing. `uninstall` deletes
  the backup. `_clean_leftovers` never touches `backups_dir`. `rollback` = inspect `previous.path` →
  plan with `keep_backup=True` → execute; the current version becomes the new `previous`
  (action wording "rollback"). `prune_backups` deletes backups older than `max_age_days`
  (user scope directly; system scope is pruned by the helper during its next install).
* Helper (§16) additions — no new inspection logic as root: manifest keys `keep_backup` (bool),
  `consume_backup` (bool; the source must be exactly the registry's `previous.path` of this app id —
  the helper deletes/swaps it itself), `backup_max_age_days` (int, clamped 1..90; the helper prunes
  older system backups during install), pass-through display fields `base_id`, `pinned`,
  `update_source`, `origin_url`, `signature`, `data_hints` (strictly validated: types, lengths, no
  control characters, `base_id` must match ID_RE); `mtime_ns` is measured by the helper. New op
  `drop-backup {"app_id"}`. Backups live in `<apps_dir>/.easyinstaller-backups/<id>/` (root-owned).

**As built (§23)** — modules `core/reconcile.py`, `core/backups.py` (per-user backup files),
`core/variants.py` (which entry a file belongs to); naming helpers in `core/integration.py`
(`variant_id`, `version_slug`, `version_label`, `variant_name`, `with_name_suffix`,
`backup_file_name`); `paths.BACKUPS_DIR_NAME`, `Layout.backups_dir`.
* More public names in `core/installer.py`: `ACTION_ROLLBACK = "rollback"`;
  `plan_rollback(app_id, scope, *, status=None) -> InstallPlan` (what `rollback` executes; the
  caller cleans up `plan.info`); `plan_refresh(info, app, *, path=None)` (reconcile's in-place
  plan); `options_from_app(app, *, keep_original=False, keep_backup=None) -> InstallOptions`
  (scope, sandbox fix and an explicit start mode of an installed app — for updates, rollback,
  repair); `recorded_extras(plan) -> dict` (`update_source`, `origin_url`, `signature`,
  `data_hints` taken from optional attributes of `plan.info`, objects via `to_dict()`;
  `update_source`/`data_hints` fall back to the installed entry's, origin/signature only when the
  very same content is installed again). More `InstallPlan` fields (all with defaults): `kind`,
  `keep_backup` (resolved), `base_id`, `pinned`, `name_suffix`, `main_installed` (the entry
  "keep both" keeps), `consumes_backup`, `unattended`.
* `keep_both_available` is True only when there is a choice: the main entry exists in this scope
  AND the file is none of the app's installed files (not the main's or a copy's path, not the
  sha256 of one). `options.keep_both` is reset to False in the plan when it does not apply. A copy
  without a version is told apart by the first 8 hex digits of its sha256 (id, name and file
  name). Another build of an already kept version replaces that copy (one copy per version).
* Reconcile: `"recorded"` is also the answer when the file was only touched (new mtime, same
  sha256). A file found `"foreign"` is remembered per process (size + mtime) and not read again
  until it changes. `reconcile_app` works on the registry's current entry (the caller's object
  may be stale) and returns `"unchanged"` — never raises — for EasyInstallerError/OSError (busy
  registry: it waits 5 s only; a file that is still being written; a full disk); everything
  else is caught by `reconcile_all`. Adoption looks at the 16 newest unregistered `*.AppImage`
  files; the adopted file stays where it is. Reconcile never starts the helper: if the new
  version needs a sandbox permission that is not there yet, it gets `--no-sandbox`; an existing
  profile for the same path is kept. An existing `previous` survives a self-update.
* A backup is only made of the file the entry describes (`backups.matches_entry`: same size,
  and the same mtime once recorded). A file the app replaced itself is never kept under the
  old version's name: the plan has no `backup_target` and a note ("<Name> has changed its own
  file since Easy Installer last looked at it, so the version that is there now is not
  kept."); if it changes between planning and executing, `backups.stash` (it checks what it
  linked) raises `backups.FileChangedError` and nothing is changed ("…has changed its own file
  in the meantime… Please try again."); `plan_rollback` refuses such an app; the GUI's going
  back reconciles first. The helper's `keep_previous_version` refuses a file that is not the
  recorded one the same way (the update goes on without a backup, with its note).
* Backups are made with a **hard link** into `backups_dir/<id>/` (the old file keeps its
  registered path until the new one replaces it, so no crash leaves the app without a file);
  file systems without hard links, and folders, use a rename. Never a copy: if the backup folder
  is on another file system there is no backup. A backup that cannot be made is a warning on
  `plan.warnings` ("The previous version could not be kept…") and the update goes on — except
  for a rollback, which then changes nothing (InstallError). `previous` is always the most
  recent kept version: replaced by the new backup; deleted when a different file is installed
  with backups off; left alone when the same file is installed again, when the app updated
  itself, or when the newer backup could not be made. A name clash in the backup folder gets
  `-2`, `-3`, …. After a crash between placing the new file and saving the registry (update or
  rollback), the next reconcile brings the entry in line with the file and finds the version
  that was already kept again (`backups.find_unrecorded`: same size and sha256 as the entry
  described) — it becomes `previous`, so the interrupted operation ends up complete.
* `prune_backups(0)` deletes every backup. It also forgets records whose file is gone and
  deletes entries in the backup folders that no registry entry refers to once they are older
  than the limit (by ctime); it waits at most 10 s for a running installation. The helper does
  the same for system apps (`ops.remove_unrecorded_backups`: during its pruning by age, and
  all of an app's on `uninstall` and `drop-backup`), and accepts `backup_max_age_days` 0..90:
  the client always sends `Settings.backup_days`, 0 (= off) deletes every kept system version
  at the next installation for everyone (a 0.2.0 helper takes 0 as 1 day).
* After a kill between setting the installed file aside and putting the new one in place
  (update or rollback), `installer.recover_interrupted()` (what the next installation would
  do: `_clean_leftovers` under the registry lock, 5 s) puts the file back before anything is
  reported: it runs first in `reconcile_all` (GUI start, CLI) and in `plan_repair`.
* Reconcile: a file at the same path with another sha256 is another file
  (`installer._is_same_file`); a new file that does not tell its version gets the version of
  the remembered update if it is that update (same sha256, or without one the same size),
  else none - never the version of the file it replaced. `reconcile.went_back(result)`: the
  app replaced itself with an OLDER version - the CLI says "<Name> went back to version X by
  itself." (not as good news), the GUI toast "<Name> went back to version X by itself".
* Everything moved or deleted for backups lies directly in `backups_dir/<id>/`, both folders
  must be real folders; a `previous.path` anywhere else is never used or deleted (only
  forgotten). `backups.stash/remove_backup/remove_app_backups` handle files and folders.
* Helper: `consume_backup` without `keep_backup` replaces the current version without keeping
  it. The source being the installed file itself ("Repair") never makes a backup. Stored values
  (`update_source`, `signature`): flat objects, ≤ 16 keys `[A-Za-z][A-Za-z0-9_-]{0,63}`, values
  null/bool/int (|n| ≤ 2^53)/text ≤ 4096 chars (TAB/LF allowed); `origin_url`: http(s), ≤ 2048;
  `data_hints`: ≤ 32 names of ≤ 128 chars without "/" — the client cleans what it sends with
  `ops.storable_dict/storable_url/storable_hints`, so an odd value never fails an installation.
  `drop-backup` answers `{"ok", "removed": [...], "skipped": [...]}`; pruning leaves a record
  with an unreadable date alone. `op_uninstall`/updates keep an app file that another registry
  entry uses (as the per-user installer does).
* `privileged.HELPER_OPS` lists the 0.1 operations plus `drop-backup`.
* Reconcile and entries made by 0.1 (`mtime_ns == 0`): when the modification time is recorded
  (`"recorded"`), the unchanged file is inspected once (no hash, never run) and the entry gets
  what it lacks — `update_source`, `origin_url`, `signature`, `data_hints`
  (`installer.file_details(info)`); what is already recorded is never replaced there.
* Repair: `installer.plan_repair(app_id, scope, *, status=None) -> InstallPlan` and
  `installer.repair(app_id, scope, *, progress=None) -> InstalledApp` make launcher, icon and
  registry entry again from the installed file itself (both scopes; the file stays, the
  choices of the installation and `previous` are kept, a kept copy stays that copy, no backup is
  made; InstallError "… cannot be repaired automatically" if the file is another app now).
  This also fills in what an entry made by 0.1 lacks. Portable apps: see §26.
* `installer.plan_update(info, app, *, keep_backup=None, version_hint=None, status=None)`: the
  plan that replaces exactly the entry `app` by a downloaded file (what `apply_update` executes).

## 24. App data clean-up (`core/appdata.py`, per-user only)

```python
@dataclass(frozen=True)
class DataLocation: path: Path; kind: str; size: int; file_count: int    # kind: config|data|cache|state|home
def data_hints_for(*, name: str | None, app_id: str | None, desktop_stem: str | None,
                   wm_class: str | None, exec_name: str | None) -> list[str]
def find_app_data(hints: Sequence[str], *, env: Mapping[str, str] | None = None,
                  exclude: Iterable[Path] = ()) -> list[DataLocation]
def dir_size(path: Path) -> tuple[int, int]            # bytes, files; never follows symlinks
def move_to_trash(paths: Iterable[Path]) -> list[tuple[Path, str | None]]   # (path, error text or None)
```
Safety rules (data loss is the worst possible bug here):
* Only **direct children** of `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`, `$XDG_CACHE_HOME`,
  `$XDG_STATE_HOME` and `~/.<hint>` are considered; the child's name must equal a hint
  case-insensitively. Hints shorter than 3 characters, and a deny-list of shared/generic names
  (e.g. applications, icons, mime, fonts, themes, autostart, systemd, dconf, gtk-3.0, gtk-4.0, pulse,
  menus, trash, flatpak, keyrings, easy-installer, google-chrome, chromium, mozilla, …) never match.
* Never a symlink, never outside the real home, never a path that contains an install/apps/registry
  folder, never anything on another user's files.
* Nothing is ever deleted permanently: `move_to_trash` uses `gio trash` (fallback: FreeDesktop trash
  spec for the home trash); if trashing is impossible the path is reported as failed and left alone.
* Data is only offered when no other installation of the same main id (other scope, keep-both copy)
  remains, and the user must tick what to remove (nothing pre-selected in the GUI; CLI lists and asks).

**As built (§24)** — the rule above is `core/installer.py`:
```python
def removable_data(app: InstalledApp) -> list[DataLocation]   # what may be offered for `app`; never raises
def other_installations(app: InstalledApp) -> list[InstalledApp]   # same main id: other scope, kept copies
def data_hints_of(app: InstalledApp) -> list[str]    # app.data_hints; entries made by 0.1: from the name,
                                                     # the main id and StartupWMClass of the installed launcher
def app_data(app: InstalledApp) -> list[DataLocation]  # the same folders also while other installations
                                                     # share them (to show them, e.g. in the details dialog)
```
`removable_data` answers `[]` while `other_installations(app)` is not empty; `app` itself does not
count, so it works before and after the uninstall, for apps of both scopes (the folders are always
the calling user's). A folder that another installed app (any id, both scopes) looks for under
the same name is never offered either (the same program from an AppImage and from its archive
has two ids and one settings folder). It excludes the app's file/folder, launcher, kept version, the apps folders
and Easy Installer's own folders on top of what `find_app_data` protects anyway. Nothing is
deleted there: the caller passes the ticked `location.path`s to `appdata.move_to_trash`.
`appdata` extras: `KINDS`; hints also get `<program>-updater` (Electron's download cache); names
are compared case-insensitively, so hints differing only in case are merged; only folders are
returned, symlink-resolved; every path the user registry lists is protected. `find_app_data`
gives lower bounds for very large trees (200 000 entries / 10 s).

## 25. Origin and signature (`core/origin.py`, `core/signature.py`)

```python
def read_origin(path: Path) -> str | None      # xattr user.xdg.origin.url, else user.xdg.referrer.url; http(s) only, ≤ 2048 chars
def origin_host(url: str | None) -> str | None
@dataclass(frozen=True)
class SignatureInfo:
    status: str               # "valid" | "invalid" | "unverified"
    fingerprint: str | None; signer: str | None; details: str | None
    def to_dict(self) -> dict; @classmethod def from_dict(cls, d) -> "SignatureInfo | None"
def read_signature(path: Path, elf: ElfInfo) -> SignatureInfo | None     # None = not signed (empty sections)
```
* `"valid"` may only be reported if the implementation follows the AppImage reference algorithm
  (appimagetool / AppImageKit `validate`) exactly and was checked against at least one real signed
  AppImage; gpg runs with a private temporary `GNUPGHOME` (0700), `--batch --no-tty`, timeouts, never
  touching the user's keyring. If that cannot be established, report `"unverified"` (signature
  present, key fingerprint shown) — never guess.
* UX: unsigned is the norm and is NOT a warning. A valid signature is shown positively ("Signed by …").
  As built (0.2 review): the key travels inside the file and anyone can name a key, so the
  name is only quoted as the key's ("Signed with a key named “…”", "Signed with a key that has
  no name"); only the installed version's key says who made a file: "Signed by the same maker
  as the installed version" (`dialog_common.signature_text(value, installed=…)`, the CLI's
  summary likewise).
* Signature continuity: if the installed app has `signature.status == "valid"` and an update/new file
  is unsigned, invalid or signed by another fingerprint → `plan_install` adds a prominent warning and
  `apply_update` refuses unless `allow_signer_change=True`.

**As built (§25)**
* `origin.clean_url(value)` (what `read_origin` applies: http(s) only, no credentials, no
  fragment, a query that carries tokens or signatures is dropped); `signature.STATUS_VALID/
  STATUS_INVALID/STATUS_UNVERIFIED`, `signature.reference_digest(path, elf)`. Verification is
  real (checked against a signed AppImage): "valid" = gpg GOODSIG + VALIDSIG for the key embedded
  in the file; an expired key is "unverified", a revoked key or BADSIG "invalid";
  `SignatureInfo.details` is a translated sentence (None when valid) and `SignatureInfo.reason`
  the same as a stable code (`signature.REASONS`: no-gpg, not-checked, no-key, other-key,
  expired, not-covered, changed, revoked, unreadable); `.explanation` translates the code when
  it is shown (records made by 0.2.0 only have their sentence). The key travels inside the
  file, so a signer's *name* proves nothing — what counts is the same fingerprint from one
  version to the next.
* Continuity in `core/installer.py`: `signer_changed(installed, info) -> bool` and
  `InstallPlan.signer_changed`. True when the reference entry (the entry that is replaced, or
  for "keep both" the main entry) carries a valid signature with a fingerprint and the file is
  unsigned, not "valid", or signed with another fingerprint. Never for the very same file
  (equal sha256), for reconcile, or for a rollback. The warning ("Careful: the installed version
  of … was signed by its maker, but this file is not signed by the same maker…") is
  `plan.warnings[0]`. A file whose own signature is "invalid" always gets a warning ("The
  signature of this file does not match…"), installed app or not.
* `apply_update` raises `updater.SignerChangedError` (an `UpdateError`) when
  `plan.signer_changed` and not `allow_signer_change`; nothing is changed.
* What is recorded (`installer.recorded_extras(plan)` on top of `file_details(plan.info)`):
  `update_source` and `data_hints` belong to the app — when the file says nothing, the
  installed entry's are kept; `origin_url` and `signature` belong to one file — they are only
  kept when the very same content is installed again (an inspected AppImage always states its
  signature, "not signed" included).

## 26. Portable archive apps (`core/portable.py` + installer integration; USER scope only)

```python
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar", ".zip")
def is_portable_archive(path: Path) -> bool          # suffix AND magic bytes
@dataclass(frozen=True)
class ExecutableCandidate: relpath: str; kind: str; score: int     # kind: "elf" | "script"
@dataclass
class PortableInfo:
    path: Path; size: int; sha256: str | None
    app_id: str; name: str; display_name: str; version: str | None; comment: str | None
    categories: list[str]; terminal: bool
    strip_prefix: str | None              # single top-level folder that is stripped on extraction
    desktop_entry: DesktopEntry | None; desktop_filename: str | None
    executables: list[ExecutableCandidate]        # best first (never empty)
    executable: str                               # chosen relpath (default: best; GUI/CLI may change it)
    icon_path: Path | None; icon_info: ImageInfo | None      # extracted into work_dir
    is_electron: bool; arch: str | None
    tree_size: int; file_count: int
    work_dir: Path; warnings: list[str]
    def cleanup(self) -> None; __enter__/__exit__
def inspect_portable(path, *, compute_hash: bool = True, progress=None) -> PortableInfo   # raises ArchiveError
def extract_portable(info: PortableInfo, dest_dir: Path, *, progress=None) -> None
```
* Inspection reads the member list and extracts only small metadata files into `work_dir` (embedded
  `.desktop`, icons, `product-info.json` (JetBrains), `application.ini` (Mozilla), `version` files,
  the first bytes of executable candidates). Heuristics: single top-level folder is stripped; name
  from `.desktop` → `product-info.json` → folder/archive name; version from those or the file name;
  executable candidates = ELF files / scripts with the exec bit at depth ≤ 2 (root, `bin/`), scored by
  similarity to the app/folder/archive name, `Exec=` of the embedded desktop file,
  `product-info.json` launcher, penalising helpers (`chrome-sandbox`, `crashpad`, `*.so`, updater,
  uninstall); icon from the desktop `Icon=`, `product-info.json`, then the largest matching
  png/svg. Archives without any executable candidate → `ArchiveError("… does not look like an app")`.
  As built (0.2 review), refused as well (`ArchiveError` with what to do instead): an
  installer as the program (`install.sh`, `setup`, `*installer*`, any `.run`), source code
  (only scripts as candidates and `configure`, `CMakeLists.txt`, `meson.build`, `setup.py`, …
  at the top), an AppImage packed into an archive (ELF with the AppImage magic: "unpack the
  archive first, then install the AppImage" - it would get no FUSE fallback, sandbox fix,
  icon or version as a portable app) and nothing but helpers (best score below 0). Warnings:
  the archive's menu entry starts a program that is not in it; a tool for the terminal (no
  menu entry, icon, Electron or product metadata, but manual pages). Program candidates are
  also the files in `usr/bin/`, `usr/local/bin/` (+8 like `bin/`) and the program the menu
  entry names, at any depth; names that are not UTF-8 are never candidates. Generic folder
  and archive names ("linux", "app", "release", "artifact", …) give the app the program's
  name. An Electron app's icon may come from `resources/app.asar` (its JSON file list; from
  a compressed tar only from its first 8 MiB, kept while the archive passes by). No icon: "Easy
  Installer could not find an icon for this app."
* Extraction is hostile-input safe: no absolute paths or `..`, no devices/FIFOs, links only to
  targets inside the tree, setuid/setgid/sticky bits stripped, limits (8 GiB total, 200 000 entries),
  zip exec bits and symlinks from `external_attr`, tar via the `data` filter plus the same checks.
* Install: `plan_install` accepts a `PortableInfo` (`InstallPlan.kind == "portable"`); the tree is
  extracted to `apps_dir/.<name>.easyinstaller-new-<rand>/`, a marker `.easy-installer.json`
  (`{"id": …}`) is written, then renamed to `apps_dir/<SafeName>/`; launcher `Exec` = the chosen
  executable (absolute), `Path=` the install dir; registry `kind="portable"`, `install_dir`.
  Replace = swap folders; the old folder becomes the backup (`previous.path` is then a directory) or
  is deleted when backups are off. Uninstall/replace may only remove a folder that is inside
  `apps_dir`, is not a symlink, is recorded in the registry AND carries the matching marker.
  The original archive is kept by default (it is an archive, not the app) unless `keep_original=False`
  is requested explicitly. No system scope, no keep-both, no update checks for portable apps;
  the Electron sandbox fix (`--no-sandbox` only) applies. Easy Installer does NOT register itself as
  a handler for archive MIME types.

**As built (§26)**
* `core/portable.py` extras: `PortableInfo.exec_has_no_sandbox`, `.wm_class` (product-info.json),
  `.origin_url` (xattr of the archive) and the property `.data_hints`; `MARKER_NAME`,
  `archive_suffix()`, `archive_stem()`, `desktop_exec_matches(entry, relpath)`,
  `score_executable()`, `adopt_work_dir(info)`. Libraries (`*.so*`, `*.node`, `.o`, `.a`) are never
  program candidates; a `version` file is ignored for Electron apps (it is Electron's version);
  links that lead out of the tree are left out (real archives contain them), not an error;
  `extract_portable` never unpacks a top-level `.easy-installer.json`.
* `extract_portable` opens the archive once, compares its sha256 with the inspected one
  (`ArchiveError` "…was changed after it was opened") and unpacks from that same open file:
  what is unpacked is what was inspected and recorded. `PortableInfo.has_member(relpath)`
  (None when not known): `plan_install` refuses a program that is not in the archive or whose
  name is not UTF-8 (InstallError, before anything is shown); `installer.installed_program(info)`
  is the program the installed version starts (GUI and CLI keep it for a newer archive).
* The launcher starts a program whose path contains `%` through `env` (GLib looks the
  program up before it expands `%%` and would not load the launcher). The `--no-sandbox` of
  the archive's menu entry only counts when that entry's command is kept.
* `InstallOptions.keep_original` is `bool | None`: None = the usual for the kind of file (an
  AppImage is moved, an archive is kept); a plan always carries True/False.
* `plan_install(PortableInfo)` → `InstallPlan` with `kind == "portable"`, `portable` (= `info`),
  `install_dir`, `target_appimage` = the program in it, `requires_root = False`,
  `extract_and_run = False`, `keep_both_available = False`, `mime_types = []`. `info.executable`
  may be set to any file of the archive before planning (must be a plain relative path, else
  InstallError). InstallError (friendly) for `Scope.SYSTEM` and for `keep_both`;
  `UnsupportedArchitectureError` when `info.arch` is known and differs from the host's, unless
  `allow_foreign_arch`. An AppImage and a portable app with the same id never replace each
  other (InstallError "… installed from a different kind of file. Please uninstall it first…").
  Sandbox: where the computer blocks the Electron sandbox the launcher gets `--no-sandbox`
  (also when `apparmor` was asked for — a permission is only ever given to AppImages, the
  password is never needed); `SandboxFix.NONE` leaves it out with a warning.
* Folder: `apps_dir/<SafeName>`; an update reuses the app's folder only if it is verifiably its
  own or gone; a name that is taken by anything else gets `<SafeName>-<id>` (`-2`, …) — nothing
  that is there is ever overwritten. Launcher: `Exec`/`TryExec` = the program (absolute),
  `Path=` the folder; the arguments of the archive's own menu entry are kept only when that
  entry starts the chosen program (`desktop_exec_matches`), else the program is started plainly.
* Marker `<install_dir>/.easy-installer.json` (written last into the unpacked tree, created
  exclusively, never through a link): `{"format": 1, "id", "name", "version", "executable",
  "desktop" (the archive's menu entry, text), "desktop_filename", "comment", "categories",
  "terminal", "is_electron", "exec_has_no_sandbox", "wm_class", "arch", "tree_size",
  "file_count", "icon" (".easy-installer-icon.<png|svg|xpm>", a copy of the icon kept next to
  it), "sha256" (of the archive), "original_filename", "origin_url", "installed_at",
  "installer_version"}`. Only `"id"` decides whether a folder may be touched; the rest lets
  the launcher be made again without the archive and is read as untrusted text
  (`installer.portable_info_from_folder(folder, app_id) -> PortableInfo`).
* `installer.portable_dir_state(layout, app, registry=None) -> ("ok" | "missing" | "unsafe",
  details)`: "ok" only if `app.kind == "portable"` and `app.install_dir` is spelled as a plain,
  not hidden name directly inside `apps_dir`, is a real folder of this user (no link, no mount
  point), carries the marker with `app.id`, and contains no path of another registry entry.
  Every removal and replacement of a folder goes through it.
* Registry entry: `kind="portable"`, `install_dir`, `appimage_path` = the program,
  `sha256` = the archive's, `size` = the unpacked size, `original_filename` = the archive's
  name, `update_info`/`update_source`/`signature` = None, `data_hints` from name, id, menu
  entry and program name.
* Execution (under the registry lock): unpack to `apps_dir/.<Name>.easyinstaller-new-<8 hex>`,
  write icon copy and marker, then — only if the installed folder is "ok" — move it to
  `backups_dir/<id>/<SafeName>-<version>` (it becomes `previous`) or set it aside as
  `.<Name>.easyinstaller-old-<8 hex>` (deleted once the registry is saved), rename the new tree
  into place, icon, launcher, registry. Any failure puts the old folder back and removes the
  new tree. A folder that is in the way and not "ok": InstallError, nothing changed. A backup
  that cannot be made is a warning for an update and an error for a rollback (§23). The same
  archive again ("reinstall") makes a fresh copy and no backup. Two renames are not one atomic
  step: a kill between them leaves the app without its folder until it is installed again
  (leftovers are cleaned up by the next installation: `…-new-…` folders are deleted; an
  `…-old-…` folder is put back if its app is registered and its place is empty, else deleted —
  only if it carries a marker).
* `rollback` / `plan_rollback`: the kept folder (`plan.source_dir`) and the installed one swap;
  the kept folder must lie in the app's backup folder, carry the marker with the app's id and
  still contain its program (checked when planning and again when executing); version, sha256
  and size come from the registry's `previous` record. The current version is always kept.
  `drop_backup` / `prune_backups` / `uninstall` delete kept folders, read-only ones included.
* `plan_repair` / `repair` for a portable app: launcher, icon and registry entry are made again
  from the marker; the folder is not touched (`plan.in_place`). InstallError "… cannot be
  repaired automatically" if the folder is not "ok" or its program is gone.
* `uninstall` of a portable app: launcher, icon, kept version and registry entry go; the folder
  only if `portable_dir_state` is "ok" (it is first renamed aside, looked at once more, then
  deleted — never followed through a link). "missing" is fine. Otherwise the folder stays and
  the returned notes say so ("The folder … was not removed, because Easy Installer cannot be
  sure that it belongs to …"); single files of a portable app are never deleted.
* Reconcile ignores portable apps (`"unchanged"`); `updater.has_update_source` is False.

## 27. CLI additions (§17)

```
easy-installer update [ID ...] [--all] [--check] [--user|--system] [-y] [--json]
    # --check: only look (exit 0; with --json a list of {id, scope, installed, available, source});
    # without IDs and without --all: same as --check plus a hint
easy-installer rollback ID [--user|--system] [-y]
easy-installer details ID [--user|--system] [--json]       # everything known: version, size, data folders+sizes,
                                                           # update source, origin, signature, previous version
easy-installer install FILE … [--keep-both] [--no-backup] [--executable RELPATH]   # FILE may be a portable archive
easy-installer uninstall ID … [--delete-data]              # lists the data folders and asks (moves to trash)
easy-installer settings [KEY [VALUE]]                      # check-updates, update-interval-hours, backup-days
easy-installer list                                        # new columns: SIZE, UPDATE (from the cache, no network)
```

**As built (§27)**
* Additional commands: `repair ID [--user|--system]` (§23: "the CLI offers Repair";
  `installer.repair`), `settings --json`. `info` also takes archives (JSON: `kind` "portable",
  `executables` with scores, `unpacked_size`, ...); AppImage `info --json` gains `kind`,
  `update_source`, `origin_url`, `signature`, `data_hints`; `list --json` entries gain
  `update` (the remembered AvailableUpdate or null).
* Before `list`, `update` and `details`: `reconcile` plus `prune_backups(backup_days)`; a
  self-update is reported in one line ("T3 Code (Alpha) updated itself to version 0.0.44."),
  a changed system app with the `repair` command.
* `update --json`: a list of `{id, scope, name, installed, available, source, size, url,
  result, version, error, notes}`; `result` is one of update-available, up-to-date, error,
  updated, failed, cancelled, skipped. Installing with `--json` needs `-y` (exit 2 otherwise).
  Exit codes: 0; 1 when something failed or nothing could be checked; 3 cancelled (Ctrl+C
  stops a download cleanly, an installation that started runs to its end).
* `update --check` and `update` without IDs always ask the sources. Installing (`update ID`,
  `--all`) uses an answer younger than 10 minutes as it is (`cli.FRESH_ANSWER_HOURS`): the
  look-then-install flow the CLI suggests costs one request, not two - GitHub counts
  conditional requests against its hourly limit, too. A check that failed is asked again
  (its reason is told, never "up to date"); `details` says "last checked …, but that check
  failed" (JSON `last_check_failed`).
* An update that is no longer needed (the app updated itself while it was downloaded:
  `UpdateNotNeededError`) is reported as up to date ("✓ <Name> is now at version X, so this
  update is no longer needed.").
* The first Ctrl+C during a download prints "Stopping the download…" at once. A changed
  signer is asked about while the download waits ("Install this update anyway?"), so a yes
  does not download it again; Ctrl+C there is a no.
* Apps from archives are listed apart by `update` ("Not checked, because they were installed
  from an archive: … To update one, install the archive of its new version").
* `install --executable` with a program that is not in the archive: usage error (exit 2,
  with the candidates) before the summary. A newer archive keeps the program the installed
  version starts (also with `-y`; offered first and as Enter in the numbered choice:
  "started by the installed version").
* `details --json`: the list entry plus `can_check_updates, source, source_homepage,
  last_checked, origin_host, previous_available, previous_kept_until, data, data_size,
  data_shared_with`; with the app in both scopes it needs `--user`/`--system`.
* `list` with one known update names that app in its hint (`update ID`), else `update --all`.
* Archive programs within 10 points of the best (`cli.SIMILAR_SCORE_MARGIN`) are offered as a
  numbered choice unless `-y`.

## 28. GUI additions (§18)

* List rows: subtitle shows version · scope · size; when an update is cached/found the row shows a
  prominent **Update** button ("Update to 1.1.4"); a header-bar refresh action "Check for updates"
  (also in the menu, Ctrl+R) with a spinner while checking; a banner/row "N updates available —
  Update all" when N ≥ 2. Automatic check at start when due (Settings.check_updates), in a thread,
  silent on network errors (a manual check reports them in a toast).
* `update_dialog.py`: confirm ("FreeCAD 1.1.3 → 1.1.4", size, source "from github.com/FreeCAD/FreeCAD",
  note that the current version is kept for N days) → download progress with Cancel → install →
  done / error. System-scope apps ask for the password at the install step.
* `details_dialog.py` (row activation / menu "Details"): icon, name, version, scope, location (Show in
  Files), app size + data size (computed in a thread), installed/updated dates, update source with
  "Check now", origin, signature line, previous version with "Go back to version X" (rollback) and
  "Delete previous version", Repair, Uninstall.
* Install dialog: when the same app is installed in the chosen scope with a different version: a
  choice "Replace version X" (default) / "Keep both". Portable archives: same dialog; if several
  executable candidates have a similar score an `Adw.ComboRow` "Program to start"; scope fixed to
  "Only for me" with an explanation.

**As built (§28, install and update dialog)** — shared pieces in `gui/dialog_common.py`
(display-free texts + `FactList`, `warning_row`, `pill`, `describe_update_error`).
* Install dialog, confirm page order: header; trust warnings (another signer than the installed
  version's = `plan.warnings[0]`, a broken signature) as red rows at the top; the choice
  "Replace version X" / "Keep both" (radio rows, only when `plan.keep_both_available`; else the
  v0.1 action row); a compact fact list ("Version X is kept for N days, so you can go back to
  it" when `plan.backup_target`, "Downloaded from <host>", "Signed with a key named …" /
  "Signed by the same maker as the installed version" only for a valid signature with the
  fingerprint as tooltip, "Gets updates from …", for a kept copy "This copy
  stays at its version and gets no updates"); "Program to start"; scope; other warnings; More
  options; target. Button: Install / Update / Reinstall / "Go Back to <version>" (downgrade) /
  Install for "Keep both"; with a changed signer "Update Anyway" / "Install Anyway" in
  `destructive-action`. The close button is hidden while installing.
* Archives: `portable.is_portable_archive` → `inspect_portable` in the worker; the confirm page
  shows the unpacked size, "Keep the archive" (on by default), "Everyone on this computer"
  insensitive ("Not possible for apps that come as an archive"), no sandbox combo (the core
  decides), "The app will be unpacked to <folder>". "Program to start" lists the candidates
  scoring within 25 points of the best (`dialog_common.program_choices`, ≤ 8); a newer archive
  of an installed portable app preselects the program the installed version starts.
* `UpdateDialog(window, app, update, *, autostart=False, on_done=None)` presents itself on
  `window` (a second `present()` is ignored); pages confirm → download (bar with percent,
  "x MB of y MB", Cancel; Escape/Ctrl+W = Cancel) → install (from `apply_update`'s 0.7 on; not
  closable, no close button) → done ("Open App" / "Done") / error ("Try Again" / "Close", says
  the installed version still works; no retry for NotInstalledError, nor when the app is no
  longer in the registry: "<Name> is no longer installed, so it was not updated.").
  `UpdateNotNeededError` shows the done page as "<Name> is up to date" with its sentence (no
  "Open App"); `UpdateDialog.cancelled`: the person cancelled the download or closed the
  dialog before the update ran. Confirm page: icon, name,
  "old → new", facts (download size, "From <source>", backup note when `backup_days > 0`, a
  password note for system apps), "What’s New" (`Gtk.UriLauncher`) when `release_url`.
  AuthorizationError → confirm + toast; SignerChangedError → confirm with a red warning and
  "Update Anyway" (the retry passes `allow_signer_change=True`); a cancelled download closes
  the dialog. `on_done(new_app | None)` exactly once, after the dialog was closed and the worker
  has ended (a force-close during the installation reports its result when it ends); also a
  GObject signal `updated(app)`; properties `page`, `busy`, `cancelling`, `new_app`. The done
  page lists the notes `apply_update(..., warnings=)` reported (e.g. the previous version
  could not be kept) as warning rows.
* Dates in the GUI (`common.format_date`) use a translatable GLib format (`_("%-d %B %Y")`,
  German "%-d. %B %Y"); month names come from the user's locale.
* `uninstall_dialog.py`: the confirmation lists the app's settings/data folders with sizes as
  unchecked check rows ("Also move settings and data to the trash"), only when allowed by §24.
* `preferences_dialog.py` (`Adw.PreferencesDialog`): check for updates automatically; keep the
  previous version (Off / 7 / 14 / 30 days).
* Reconcile at start and on focus; a toast "T3 Code was updated to 0.0.44" when a self-update was
  picked up; system apps that changed offer Repair.

**As built (§28, main window, details, uninstall, preferences)**
* Start (`window.startup_maintenance()`, worker thread, never the network):
  `prune_backups(settings.backup_days)` → `reconcile_all()` → `cached_updates()` → whether a
  check is due (`window.updates_due`: an app with `has_update_source` whose cache entry is older
  than `update_interval_hours`, only when `check_updates`). Then, if due, the quiet automatic
  check `check_all_updates(force=False)`: no toast at all, errors are only logged. The manual
  check (header button → spinner in its place, menu "Check for _Updates", Ctrl+R =
  `win.check-updates`) uses `force=True` and ends with a toast ("2 updates are available",
  "All apps are up to date", "None of your apps tells where to find updates", the
  `NetworkError` sentence when nothing could be reached, "… · N apps could not be checked" with
  a "Details" button listing each app's reason). What a check could not answer keeps what was
  known (`window.merge_updates`). The header button is hidden while no app is installed.
* Rows: subtitle "Version X · <scope> · <size>" (`GLib.format_size`); "Update to 1.1.4"
  (`suggested-action`) before "Open" while an update is known and still newer
  (`updater.is_newer_than_installed`) and the app works; a quiet "Portable" tag
  (`.app-tag`, tooltip) for portable apps, which never get update or keep-both affordances.
  Activating a row opens its details; the row menu adds "Update to …", "Details" and "Repair"
  (missing launcher, or a changed system app). A window breakpoint (`max-width: 560sp`) makes
  rows compact: the update button becomes a round icon button (tooltip/accessible name "Update
  <Name> to version X"), "Open" moves into the menu while an update is shown, the tag becomes
  "Portable app · …" in the subtitle, and the header bar leaves out the window's name and
  shows only the icon of "Install App…" (German and Dutch labels would be cut off).
* Banner "N updates are available" [Update A_ll] (the access key must not clash with "Install
  _App…") for N ≥ 2. Update All presents `UpdateDialog(..., autostart=True)` one after another
  (the banner hides meanwhile); an app that is busy is skipped; a dialog the person cancelled
  stops the queue - the banner offers the rest again; one that failed does not (its dialog
  said why), the next app follows. The banners' buttons do nothing while a dialog is shown
  (their access keys reach them behind it).
* `MainWindow.start_update(app, update)` (rows, details, the queue) offers the update that is
  known for the registry's entry of the app *now* (`update_for`; the passed one only while
  it is still newer): an app that updated itself meanwhile is never taken back to an older
  version - a toast "<Name> is already up to date" and False instead.
* Typing a printable character in the list (more than 6 apps) starts the search
  (`MainWindow.starts_search`, a capture-phase key controller - not the search entry's key
  capture, which takes Return as well): Return, Space, Tab, arrows and shortcuts stay with
  the focused row. Results of background work (checks, start-up checks, reconcile, "Check
  Now") only rebuild the list when something shown changed; a rebuild gives the keyboard
  focus to the same app's new row. The start-up checks end with the quiet automatic check
  only if the preferences say so *then* (switched off meanwhile: none; switched on: if due).
* "Uninstall…" of a busy app (from its launcher's menu while it is updated): an alert "<Name>
  cannot be uninstalled right now", and a confirmed uninstall of a busy app is refused with a
  toast.
* Focus: rows that are not busy and not waiting for a Repair get one `needs_reconcile` stat;
  the changed ones are reconciled in a thread (never two runs at once, not during the start
  checks). One self-update: "<Name> was updated to <version>", several: "N apps were updated".
  `needs-admin` marks the app for the session: a toast "<Name> changed since it was installed"
  [Repair] once per app, "Repair" in its row menu, a highlighted Repair in its details.
  `window.repair_app` is `installer.repair` (kept copies and portable apps included). After a
  rollback the app's known update is dropped for this session, so the version the user just
  left is not offered again right away.
* Keyboard focus returns to the app's (rebuilt) row when its details, update or uninstall dialog
  closes. Dialog/app state is only touched on the main loop; results of worker threads for a
  dialog that was closed meanwhile are dropped (the window still learns e.g. a "Check Now"
  answer via `remember_update`).
* `details_dialog.DetailsDialog(host, app)` (`host` = the window: `show_in_files`,
  `repair(app, on_done)`, `confirm_uninstall`, `start_update`, `rollback_app(app, on_done)`,
  `drop_backup_app(app, on_done)`, `remember_update`, `update_for`, `needs_repair`,
  `is_app_busy`): header (icon, name, "Version X · <scope>", comment) → Updates ("New versions:
  From <source>" + "Last checked on …" + "Check Now"; "Version X is available" [Update]; "<Name>
  is up to date" / the error after a check; texts for portable apps, kept copies and apps
  without a source) → App (Type, Location + Show in Files, App size, "Settings and data" as an
  expander filled in a thread - also while another installation shares the folders, then only
  shown -, Installed, Last changed) → Previous version (only if the backup is really there:
  "Version X", "Kept until <saved_at + backup_days>" · size - for an app for everyone whose
  kept version is due (or backups are off): "Deleted the next time an app is installed or
  updated for everyone", the helper prunes those; [Go Back] + delete icon, both confirmed by
  an alert) → Origin (Downloaded from <host>, full address as tooltip; Signature: "Not signed
  (most apps are not)" without any warning styling, "Signed with a key named “<signer>”" +
  key fingerprint in groups of four with a check mark, unverified/invalid with their
  sentence) →
  Troubleshooting (Repair; a password note for system apps) + "Uninstall…". No selectable
  labels (the first one would take the focus and show its text selected).
* `uninstall_dialog.UninstallDialog(app)` (an `Adw.AlertDialog`; the v0.1 texts, password and
  other-scope notes moved here, plus "The other installed versions of this app stay installed."
  and "The previous version that was kept is removed, too."): `removable_data` runs in a thread
  while the alert is shown; found folders become unticked check rows ("~/.config/FreeCAD",
  "Settings · 60 kB"). The window runs `uninstall_and_trash` (uninstall first; only then the
  ticked folders go to the trash) and shows a toast "<Name> was uninstalled, its settings and
  data are in the trash" [Open Trash], or an alert with the notes and every folder that stayed.
  A refused password prompt still offers uninstalling without it, with the same folders.
* `preferences_dialog.PreferencesDialog` (`app.preferences`, Ctrl+comma, menu "_Preferences"):
  switch "Check for updates automatically"; combo "Keep the previous version" Off / 7 / 14 / 30
  days (a value set with the CLI is offered as well); saved at once (settings re-read before
  each save); "Off" says that kept versions are deleted at the next start (those of apps for
  everyone at the next installation for everyone). Switching the automatic check on runs a
  quiet check if one is due.
* The file chooser's default filter shows AppImages and portable archives.
