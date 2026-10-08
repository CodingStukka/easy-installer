# Easy Installer

**Install downloaded AppImage apps with one click, the way you would on Windows or macOS.**

Easy Installer is a small GNOME app (Python, GTK 4 and libadwaita). It turns a downloaded
`.AppImage` file into an app that feels properly installed: it sits in a fixed place, shows up in
the app menu and in the search with its real name and icon, and can be uninstalled again.
Apps that come as a `.zip` or `.tar.gz` archive can be installed the same way.

Since version 0.2 it also looks after the apps once they are installed, like a small app store:
it finds new versions and updates them with one click, keeps the previous version for a while so
you can go back, notices apps that updated themselves, lets you keep two versions side by side,
shows where an app came from and whether it is signed, and can move an app's settings and data
to the trash when you uninstall it.

## Who it is for

It is for people who are new to Linux, especially if you come from Windows or macOS. There,
you download an installer, double-click it, and the app appears in the Start menu or the
Applications folder. On Linux, many apps come as a single `.AppImage` file instead. You can run
that file directly, but it does not appear in your app menu, has no icon, and after a while
you forget which file in `Downloads` was which app.

Easy Installer fills that gap. Double-click an AppImage (or drop it onto the Easy Installer
window), check the name and icon, click **Install**, and you are done.

## What it does

Guides like
[How to create a launcher for your AppImage on Linux](https://dev.to/lovestaco/how-to-create-a-launcher-for-your-appimage-on-linux-mc3)
describe the manual steps:

1. move the AppImage to a permanent folder,
2. make it executable (`chmod +x`),
3. find an icon for it,
4. write a `.desktop` launcher file by hand (name, command, icon, categories),
5. run `update-desktop-database` so the desktop picks the launcher up.

Easy Installer does all of this for you, and it gets the details from **inside the AppImage**
instead of making you type them:

1. **Reads the AppImage without starting it.** It finds where the embedded file system starts
   and reads only the app's own launcher file and icon with `unsquashfs`. The app itself is never
   run just to get this information.
2. **Shows you what it found:** name, icon, version, size and description, in your language if
   the app provides it. It also warns you about anything unusual, such as an app built for a
   different kind of processor.
3. **Moves the AppImage to a fixed place** and makes it executable. It is moved, not copied,
   unless you choose "Keep the original file".
4. **Installs the icon** in the right size folder of the icon theme.
5. **Writes a launcher** based on the app's own `.desktop` file: name and translations,
   categories, file types it can open, keywords, and the window class so the dock shows the right
   icon. The command is changed to point at the installed AppImage.
6. **Refreshes the app menu**, so the app shows up in GNOME search and the app grid right away.
7. **Remembers the installation** in a small registry file. That is how it can later update or
   uninstall the app. When Easy Installer itself is installed (not just run from the source
   folder), it also adds an "Uninstall with Easy Installer…" entry to the app's right-click menu.
8. **Handles updates:** opening a newer version of an installed app replaces the old version
   instead of adding a second copy - or, if you choose **Keep both**, installs it next to it as
   a separate app ("FreeCAD 1.1.4") that stays at its version.
9. **Fixes two common problems** on current Ubuntu-based systems (see below): Electron apps that
   do not start because of the sandbox, and AppImages that need the FUSE library.

Uninstalling removes the AppImage, the launcher and the icon. Your personal files are never
touched. The app's settings and data folders (for example `~/.config/FreeCAD`) are only moved to
the trash if you tick them in the uninstall dialog (or pass `--delete-data`); nothing is
selected by default, and nothing is ever deleted permanently.

## Updates, earlier versions and trust (0.2)

* **Where updates come from.** Many AppImages say where their new versions are published: the
  AppImage update information (GitHub releases or a `.zsync` file, for example FreeCAD) or the
  `app-update.yml` of Electron apps (for example T3 Code and Pen). Easy Installer only asks the
  servers an installed app names itself, only over HTTPS, at most once a day (you can change or
  turn off the automatic check in **Preferences**), and sends nothing about you or your apps.
  Apps that do not say where their updates are cannot be checked; open a newer download of
  them instead.
* **One-click updates.** A row shows **Update to 1.1.4** when a newer version is known; with
  two or more, a banner offers **Update All**. The download is checked against the size and
  checksum the maker published, and the app is only replaced once the new file is complete.
* **Going back.** The replaced version is kept for 14 days (Off / 7 / 14 / 30 days in
  **Preferences**). The app's details offer **Go Back**, which swaps the two versions, so you
  can switch forward again too.
* **Apps that update themselves** (many Electron apps do) are noticed when Easy Installer starts
  or gets the focus again: the list shows the new version and the launcher is refreshed. An
  update found earlier is never installed over a newer version such an app installed itself,
  and the version it replaced is never kept under the wrong version number.
* **Where it came from, and who signed it.** The details show the website a file was
  downloaded from (if your browser noted it) and, for signed AppImages, the name of the key it
  was signed with. Anyone can give a key any name, so what counts is that the key stays the
  same: a new version signed with the installed version's key says "Signed by the same maker
  as the installed version"; if an installed app was signed by its maker and a new version is
  not signed by the same key, Easy Installer warns you clearly before installing it. Unsigned
  apps are normal and are not flagged.

## Apps that come as an archive (0.2)

Some apps (for example balenaEtcher or UVtools) are offered as a `.zip`, `.tar.gz`, `.tar.xz` or
`.tar.bz2` archive with a program inside. Easy Installer unpacks such an archive safely into
its own folder (`~/Applications/<Name>/`), finds the program that starts the app (and lets you
choose if there are several), and adds a launcher. The archive itself is kept. These "portable"
apps are installed just for you, are not checked for updates (install the archive of a newer
version to update), and are uninstalled like any other app. Archives that are not an app ready
to start - an installer (`install.sh`, a `.run` file), source code, or an AppImage packed into
a `.zip` - are refused with a hint what to do instead.

## Where the files go

You can install an app **only for you** (no password needed) or **for everyone on this
computer** (asks for an administrator password).

| What                  | Only for me                                                       | For everyone                                   |
|-----------------------|-------------------------------------------------------------------|------------------------------------------------|
| The AppImage          | `~/Applications/<Name>.AppImage`                                  | `/opt/appimages/<Name>.AppImage`               |
| Launcher              | `~/.local/share/applications/easyinstaller-<id>.desktop`          | `/usr/local/share/applications/easyinstaller-<id>.desktop` |
| Icon                  | `~/.local/share/icons/hicolor/<size>/apps/easyinstaller-<id>.png` (or `scalable/…svg`) | `/usr/local/share/icons/hicolor/<size>/apps/easyinstaller-<id>.png` |
| Registry              | `~/.local/share/easy-installer/registry.json`                     | `/var/lib/easy-installer/registry.json`        |
| Sandbox permission (Electron apps, if needed) | `/etc/apparmor.d/easyinstaller-<id>`      | `/etc/apparmor.d/easyinstaller-<id>`           |
| Portable app (archive) | `~/Applications/<Name>/`                                         | (only for you)                                 |
| Kept earlier version  | `~/Applications/.easyinstaller-backups/<id>/`                     | `/opt/appimages/.easyinstaller-backups/<id>/`  |
| Settings              | `~/.config/easy-installer/settings.json`                          |                                                |
| Update checks         | `~/.cache/easy-installer/updates.json` (downloads go to `~/.cache/easy-installer/downloads/` and are removed afterwards) | |

`<id>` is the app's own id (for example `org.freecad.FreeCAD` or `t3code`); `<Name>` is a
file-name-friendly version of the app name (for example `T3-Code-Alpha`). The "Only for me"
folders follow `$XDG_DATA_HOME` if you have set it.

## Electron apps on Ubuntu 24.04 and newer

Ubuntu 24.04 and newer, and many systems based on it such as Zorin OS 18, block "unprivileged
user namespaces" unless an AppArmor profile allows them. Apps built with Electron or Chromium
use exactly this for their security sandbox, so many of them close right away with an error
about the "SUID sandbox helper". Easy Installer checks whether your system has this restriction
(`/proc/sys/kernel/apparmor_restrict_unprivileged_userns`) and only offers a fix when it does.

Easy Installer recognizes Electron apps (they contain `chrome-sandbox` or
`resources/app.asar`) and offers two fixes:

* **Allow with system permission (recommended):** Easy Installer adds a small AppArmor profile
  that allows the sandbox for this one AppImage file only. This asks for your password once.
* **Start without sandbox:** the launcher starts the app with `--no-sandbox`. This needs no
  password, but the app runs with less protection.

Apps whose launcher already uses `--no-sandbox` (for example T3 Code) do not need either fix.

## FUSE

AppImages normally mount themselves with FUSE version 2 (`libfuse.so.2`). Recent Ubuntu releases
no longer install that library by default. Without it, AppImages do not start at all.

Easy Installer checks for it. If it is missing, you can:

* let Easy Installer install it (the package is `libfuse2t64` on Ubuntu 24.04 and newer, and
  `libfuse2` on older releases; this asks for your password), or
* keep going: apps are then started with `APPIMAGE_EXTRACT_AND_RUN=1`, which works without FUSE
  but makes them start a bit slower.

Do **not** install the package called `fuse` to fix this: on Ubuntu it replaces `fuse3` and can
remove parts of the desktop.

## Using the app

* **Install:** double-click an `.AppImage` file, drag it onto the Easy Installer window, or click
  **Install App…**. Check the details, choose "Only for me" or "Everyone on this computer", and
  click **Install**.
* **Open:** installed apps are in your app menu and search, like any other app. Easy Installer
  also lists them, with an **Open** button.
* **Uninstall:** click the menu button next to an app in Easy Installer and choose
  **Uninstall…**, or right-click the app in the dock or app grid and choose **Uninstall with Easy Installer…**.
* **Repair:** if an app's launcher was deleted, Easy Installer offers to recreate it.
* **Details:** click an app to see its version, size, where its updates come from, where it was
  downloaded from, its signature, the kept earlier version (**Go Back**) and its settings and
  data folders.
* **Updates:** the refresh button in the header bar (or Ctrl+R) checks for new versions;
  **Update to …** in a row or **Update All** in the banner installs them.
* **Preferences** (Ctrl+comma): automatic update checks, and how long replaced versions are
  kept.
* The first time, Easy Installer offers to become the default app for AppImage files, so
  double-clicking one opens it.

Only install apps from sources you trust. An AppImage can do anything you can do.

## Command line

```
easy-installer                            open the window
easy-installer FILE.AppImage              open the install dialog for FILE
easy-installer --uninstall ID             ask whether to uninstall ID (used by the launcher menu)

easy-installer install FILE [--system] [--keep] [--sandbox-fix auto|apparmor|no-sandbox|none]
                            [--extract-and-run auto|yes|no] [--allow-foreign-arch] [-y]
                            [--keep-both] [--no-backup] [--executable RELPATH]
easy-installer info FILE [--json]         show what is inside an AppImage or app archive
easy-installer list [--json] [--user|--system]      (with SIZE and UPDATE columns)
easy-installer update [ID ...] [--all] [--check] [--user|--system] [-y] [--json]
easy-installer rollback ID [--user|--system] [-y]
easy-installer details ID [--user|--system] [--json]
easy-installer repair ID [--user|--system]
easy-installer settings [KEY [VALUE]] [--json]
easy-installer uninstall ID [--system|--user] [--keep-permission] [--delete-data] [-y]
easy-installer launch ID
easy-installer check [--json]             check FUSE, sandbox restrictions and helper tools
```

* `install` also accepts app archives (`.zip`, `.tar.gz`, …); `--executable` chooses the
  program in the archive. `--keep-both` installs a new version next to the installed one,
  `--no-backup` does not keep the replaced version.
* `update` without an ID only shows which new versions there are; `update ID` or
  `update --all` installs them (a change of signer is asked about; with `-y` it is refused).
  `--check` only looks, also for scripts (`--json`).
* `rollback ID` goes back to the version that was kept at the last update; the current one is
  kept instead.
* `details ID` shows everything Easy Installer knows about an app, including its settings and
  data folders; `uninstall --delete-data` moves those to the trash after asking.
* `settings` shows or changes `check-updates` (yes/no), `update-interval-hours` (1-720) and
  `backup-days` (0-90).

`install` shows a summary and asks `Proceed? [Y/n]` unless you pass `-y`. By default the file
is moved; `--keep` copies it instead. `--extract-and-run yes|no` is remembered: Easy Installer
never switches such an app to another start mode by itself. `uninstall --keep-permission` removes
an app installed just for you without the administrator password; only its special sandbox
permission then stays. Exit codes: `0` success, `1` error, `2` wrong usage, `3` cancelled.

Examples:

```sh
easy-installer info ~/Downloads/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage
easy-installer install ~/Downloads/T3-Code-0.0.42-x86_64.AppImage -y
easy-installer install ~/Downloads/balenaEtcher-linux-x64-2.1.4.zip
easy-installer list
easy-installer update                 # which new versions are there?
easy-installer update t3code          # install one of them
easy-installer rollback t3code        # changed my mind: back to the previous version
easy-installer details t3code
easy-installer settings backup-days 30
easy-installer uninstall t3code --delete-data
```

## Requirements

* Python 3.10 or newer
* PyGObject with GTK 4.14+ and libadwaita 1.5+ (Ubuntu 24.04 / Zorin OS 18 or newer)
* `squashfs-tools` (`unsquashfs` 4.4 or newer); without it Easy Installer falls back to the
  AppImage's own `--appimage-extract`, which starts the AppImage runtime but not the app
* `desktop-file-utils` (`update-desktop-database`)
* `pkexec` (polkit), only for installing for everyone and for the sandbox and FUSE fixes
* recommended: `libfuse2t64` (or `libfuse2`), `apparmor`, `gpg` (to check signatures of signed
  AppImages; without it a signature is shown as "could not be checked")

On Ubuntu and its derivatives:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 squashfs-tools desktop-file-utils pkexec
```

The core, the command line and the helper use only the Python standard library.

## Building and running from source

```sh
make dev            # create .venv (with system site-packages, for PyGObject) and install pytest
make test           # run the tests (they never touch your real home folder or system)
make lint           # byte-compile everything, validate the .desktop, metainfo and policy files
make run            # start the app from the source tree; arguments: make run ARGS="info FILE"
make po             # update po/POTFILES, po/easy-installer.pot, po/de.po and po/nl.po
make mo             # compile translations to build/locale (used automatically by `make run`)
make deb            # build dist/easy-installer_<version>_all.deb
make install-user   # install for yourself into ~/.local (no password needed)
make uninstall-user # remove that again (apps you installed stay installed)
sudo make uninstall # remove what `sudo make install` installed (same PREFIX)
make clean
```

The `.deb` is the recommended way to install Easy Installer for everyone:

```sh
make deb
sudo apt install ./dist/easy-installer_0.2.2_all.deb
```

`make && sudo make install` also works (`PREFIX=/usr/local` by default, `DESTDIR` is
supported; running `make` first keeps root-owned files out of `build/`); `sudo make uninstall`
with the same `PREFIX` removes it again. The
Python package then goes to `$(PREFIX)/lib/easy-installer`, and the `easy-installer` command is
set up to find it. The polkit policy always goes to `/usr/share/polkit-1/actions` (the only
folder polkit reads; override with `POLKITDIR=...`) and points at
`$(PREFIX)/libexec/easy-installer/easy-installer-helper`. Easy Installer looks for its helper in
`/usr/libexec/easy-installer/` and `/usr/local/libexec/easy-installer/`, so both `PREFIX=/usr` and
the default `PREFIX=/usr/local` get the dedicated password prompt; any other prefix falls back to
a generic prompt ("run python3 as administrator").

The translation tools in `tools/` (`xgettext.py`, `update_po.py`, `msgfmt.py`) need only Python,
so you do not have to install gettext. To add a language, add its code to `po/LINGUAS` and run
`make po`.

## Project layout

```
src/easy_installer/
  __main__.py, cli.py     entry point and command line
  i18n.py, errors.py      translations, user-facing error types
  core/                   everything that does not need a GUI (standard library only):
                          ELF/squashfs reading, .desktop parsing and writing, inspection,
                          install/uninstall, registry, system checks, sandbox fix; 0.2: update
                          sources and downloads, reconcile, backups, portable archives,
                          signatures, origin, app data clean-up, settings
  helper/                 the privileged helper that pkexec starts for system-wide tasks
  gui/                    GTK 4 / libadwaita user interface
data/                     launcher, AppStream metainfo, polkit policy, icons, wrapper scripts
po/                       translations (POTFILES, LINGUAS, template, de, nl)
tools/                    stdlib-only xgettext / msgfmt / msgmerge replacements, and
                          gui_screenshots.py (headless screenshots of every screen, --language)
packaging/build-deb.sh    Debian package builder
tests/                    pytest suite (isolated from your real files)
DESIGN.md                 the design contract every module follows
```

## Security model of the helper

Installing for everyone, adding an AppArmor profile, and installing FUSE need administrator
rights. The app itself never runs as root. Instead, it starts a small helper,
`/usr/libexec/easy-installer/easy-installer-helper`, through `pkexec`:

* The polkit action `com.roothirsch.EasyInstaller.manage` covers only that helper. It asks for
  an administrator password and remembers it for a few minutes.
* The helper gets a small JSON request on standard input and supports only a fixed list of
  operations: `install`, `uninstall`, `apparmor-install`, `apparmor-remove`, `install-fuse` and
  `drop-backup` (delete the kept earlier version of an app installed for everyone). It never
  downloads anything, never unpacks archives and never touches your personal files.
* It opens every file you hand it **with your own permissions**, not root's, so it cannot be
  used to read files you could not read anyway. It checks that the AppImage is a regular ELF
  file whose SHA-256 matches, and that the icon is a real, reasonably small image.
* It computes every destination path itself (`/opt/appimages`, `/usr/local/share/...`). It never
  writes through symbolic links and only deletes files recorded in its own registry that are
  inside those folders.
* `install-fuse` can only run `apt-get install` for `libfuse2t64` or `libfuse2`.
* The helper is started with `python3 -I`, so environment variables and user site-packages
  cannot change which code runs as root.

When a root-owned helper is installed (by the `.deb` in `/usr/libexec`, or by `make install`
in `/usr/local/libexec`), every Easy Installer on the computer uses it - also one that runs
from a source folder or from `make install-user` - but only if it belongs to the same version:
otherwise tasks for everyone are refused ("…belongs to another version of Easy Installer"),
because another version's helper could misread the request or drop what it does not know from
the registry. Without an installed helper, tasks for everyone run the helper script of the
running program through `pkexec python3`, with a generic password prompt. That is fine for
development, but for everyday use install the `.deb`.

## Roadmap

* **Smaller downloads** with zsync deltas (today the whole new version is downloaded).
* **`.tar.zst` and `.7z` archives** (today Easy Installer says that it cannot open them yet),
  and icons that lie deep inside the `app.asar` of a compressed Electron archive.
* **Flatpak packaging** of Easy Installer itself.

## License

Easy Installer is released under the [MIT License](LICENSE).
