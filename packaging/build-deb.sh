#!/usr/bin/env bash
# Build dist/easy-installer_<version>_all.deb from the source tree (see DESIGN.md §19).
#
# Only writes into build/deb/ and dist/ of the repository; needs python3 and dpkg-deb, no root
# (dpkg-deb --root-owner-group makes every file root:root inside the package).
#
# Environment: PYTHON (default python3), MAINTAINER (default: placeholder address below).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE=easy-installer
APP_ID=com.roothirsch.EasyInstaller
DOMAIN=easy-installer
PYTHON="${PYTHON:-python3}"
# TODO: replace the placeholder once there is a real maintainer address.
MAINTAINER="${MAINTAINER:-roothirsch <noreply@roothirsch.invalid>}"

die() { echo "build-deb.sh: $*" >&2; exit 1; }
command -v dpkg-deb >/dev/null 2>&1 || die "dpkg-deb is missing (sudo apt install dpkg)"
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON is missing"

cd "$REPO"
umask 022

VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' src/easy_installer/__init__.py)"
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.+~-]*$ ]] || die "cannot read a valid __version__ from src/easy_installer/__init__.py"

STAGE="$REPO/build/deb/${PACKAGE}_${VERSION}_all"
OUTPUT="$REPO/dist/${PACKAGE}_${VERSION}_all.deb"
PYDIR="$STAGE/usr/lib/python3/dist-packages"

# The wrappers must still have empty placeholders: the package lives in dist-packages.
for wrapper in data/easy-installer data/easy-installer-helper; do
    grep -q '^_LIBDIR = ""' "$wrapper" || die "$wrapper: _LIBDIR placeholder was modified"
done

echo "Building $PACKAGE $VERSION in ${STAGE#"$REPO"/}"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$REPO/dist"

# --- Python package (sources only; syntax-checked, no bytecode) ------------------------------
mapfile -t sources < <(find src/easy_installer -type f ! -name '*.py[co]' ! -path '*/__pycache__/*' | LC_ALL=C sort)
[[ ${#sources[@]} -gt 0 ]] || die "src/easy_installer is empty"
"$PYTHON" - "${sources[@]}" <<'EOF' || die "syntax errors in the Python sources"
import sys
failed = False
for name in sys.argv[1:]:
    if name.endswith(".py"):
        try:
            compile(open(name, "rb").read(), name, "exec", dont_inherit=True)
        except SyntaxError as exc:
            print(f"build-deb.sh: {exc}", file=sys.stderr)
            failed = True
sys.exit(1 if failed else 0)
EOF
for file in "${sources[@]}"; do
    install -D -m 0644 "$file" "$PYDIR/${file#src/}"
done

# --- Programs ------------------------------------------------------------------------------
install -D -m 0755 data/easy-installer "$STAGE/usr/bin/easy-installer"
install -D -m 0755 data/easy-installer-helper "$STAGE/usr/libexec/easy-installer/easy-installer-helper"

# --- Data ----------------------------------------------------------------------------------
install -D -m 0644 "data/$APP_ID.desktop" "$STAGE/usr/share/applications/$APP_ID.desktop"
install -D -m 0644 "data/$APP_ID.metainfo.xml" "$STAGE/usr/share/metainfo/$APP_ID.metainfo.xml"
install -D -m 0644 "data/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -D -m 0644 "data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
    "$STAGE/usr/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
install -D -m 0644 "data/$APP_ID.policy" "$STAGE/usr/share/polkit-1/actions/$APP_ID.policy"

# --- Translations --------------------------------------------------------------------------
shopt -s nullglob
po_files=(po/*.po)
shopt -u nullglob
if [[ ${#po_files[@]} -eq 0 ]]; then
    echo "build-deb.sh: warning: no po/*.po files, the package will be English only" >&2
fi
for po in "${po_files[@]}"; do
    lang="$(basename "$po" .po)"
    "$PYTHON" tools/msgfmt.py --check "$po" -o "$STAGE/usr/share/locale/$lang/LC_MESSAGES/$DOMAIN.mo" \
        || die "could not compile $po"
done

# --- Documentation -------------------------------------------------------------------------
install -d "$STAGE/usr/share/doc/$PACKAGE"
cat > "$STAGE/usr/share/doc/$PACKAGE/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Easy Installer
Upstream-Contact: $MAINTAINER

Files: *
Copyright: 2026 roothirsch
License: Expat
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 SOFTWARE.
EOF
chmod 0644 "$STAGE/usr/share/doc/$PACKAGE/copyright"

# --- Control files -------------------------------------------------------------------------
installed_size="$(du -sk --apparent-size --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: $MAINTAINER
Homepage: https://codingstukka.github.io/easy-installer/
Installed-Size: $installed_size
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), squashfs-tools, desktop-file-utils, pkexec | policykit-1
Recommends: libfuse2t64 | libfuse2, apparmor, gpg
Description: install AppImage apps like real apps
 Easy Installer turns a downloaded AppImage file into an installed app: it
 moves the file to a fixed place, takes the name, icon and version from
 inside the AppImage, adds the app to the app menu and search, and can
 uninstall it again. Apps that come as a .zip or .tar archive are unpacked
 into their own folder.
 .
 It looks for new versions of the installed apps and updates them with one
 click, keeps the replaced version for a while so you can go back, notices
 apps that updated themselves, and shows where an app came from and whether
 it is signed.
 .
 It is made for people who are new to Linux. Apps can be installed just for
 the current user or, with an administrator password, for everyone on the
 computer.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    configure|abort-upgrade|abort-remove|abort-deconfigure)
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
        fi
        ;;
esac
exit 0
EOF

# Python may write __pycache__ next to the modules (e.g. when the helper runs as root);
# remove it before dpkg deletes the package files so no directories are left behind.
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    remove|upgrade|deconfigure)
        if command -v py3clean >/dev/null 2>&1; then
            py3clean -p easy-installer || true
        else
            find /usr/lib/python3/dist-packages/easy_installer -name __pycache__ -type d \
                -prune -exec rm -rf {} + 2>/dev/null || true
        fi
        ;;
esac
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
    remove|purge|abort-install|abort-upgrade|disappear)
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
        fi
        ;;
esac
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"
chmod 0644 "$STAGE/DEBIAN/control"

# Make directory permissions independent of the caller's umask/ACLs.
find "$STAGE" -type d -exec chmod 0755 {} +

rm -f "$OUTPUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT" >/dev/null
echo "Built ${OUTPUT#"$REPO"/}"
