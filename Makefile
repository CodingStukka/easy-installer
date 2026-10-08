# Easy Installer - development, translation and packaging tasks (see DESIGN.md §19).
#
#   make dev             create .venv (with system site-packages for PyGObject) and install pytest
#   make test            run the test suite
#   make lint            byte-compile all Python sources and validate the data files
#   make po              refresh po/POTFILES, po/easy-installer.pot and po/<lang>.po
#   make mo              compile translations into build/locale (picked up by `make run`)
#   make run             start the app from the source tree (pass arguments with ARGS="...")
#   make install         install for all users: PREFIX=/usr/local by default, DESTDIR supported
#   make uninstall       remove what install installed (same PREFIX/DESTDIR; apps you installed are kept)
#   make install-user    install for the current user only, into ~/.local (no password needed)
#   make uninstall-user  remove what install-user installed (apps you installed are kept)
#   make deb             build dist/easy-installer_<version>_all.deb
#   make clean           remove build artefacts

PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
TEST_PYTHON := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),$(PYTHON))
ARGS ?=

APP_ID := com.roothirsch.EasyInstaller
DOMAIN := easy-installer
VERSION := $(shell sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' src/easy_installer/__init__.py)
LINGUAS := $(shell cat po/LINGUAS 2>/dev/null)
POT_FILE := po/$(DOMAIN).pot
PO_FILES := $(wildcard po/*.po)
LANGS := $(patsubst po/%.po,%,$(PO_FILES))
MO_FILES := $(foreach lang,$(LANGS),build/locale/$(lang)/LC_MESSAGES/$(DOMAIN).mo)

# System-wide installation (make install).
PREFIX ?= /usr/local
DESTDIR ?=
BINDIR ?= $(PREFIX)/bin
LIBEXECDIR ?= $(PREFIX)/libexec
DATADIR ?= $(PREFIX)/share
LOCALEDIR ?= $(DATADIR)/locale
PYTHONDIR ?= $(PREFIX)/lib/easy-installer
# polkit only reads /usr/share/polkit-1/actions (pkg-config: polkit-gobject-1 policydir), whatever
# the PREFIX; the installed policy points at $(HELPER), and Easy Installer also looks for its helper
# in /usr/local/libexec, so PREFIX=/usr/local gets the dedicated password prompt too.
POLKITDIR ?= /usr/share/polkit-1/actions
HELPER = $(LIBEXECDIR)/easy-installer/easy-installer-helper

# Per-user installation (make install-user / uninstall-user).
USER_BINDIR ?= $(HOME)/.local/bin
USER_DATADIR ?= $(or $(XDG_DATA_HOME),$(HOME)/.local/share)
USER_PYTHONDIR ?= $(USER_DATADIR)/easy-installer/lib
USER_LOCALEDIR ?= $(USER_DATADIR)/locale

ICON_SVG := icons/hicolor/scalable/apps/$(APP_ID).svg
ICON_SYMBOLIC := icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg

# $(call copy_package,DEST) - copy src/easy_installer to DEST/easy_installer (no bytecode).
define copy_package
	rm -rf "$(1)/easy_installer"
	find src/easy_installer -type f ! -name '*.py[co]' ! -path '*/__pycache__/*' | LC_ALL=C sort | \
		while IFS= read -r f; do install -D -m 0644 "$$f" "$(1)/$${f#src/}" || exit 1; done
endef

# $(call wrapper,SOURCE,LIBDIR,LOCALEDIR) - print a wrapper script with its placeholders filled in.
wrapper = sed -e 's|^_LIBDIR = ""|_LIBDIR = "$(2)"|' -e 's|^_LOCALEDIR = ""|_LOCALEDIR = "$(3)"|' $(1)

# $(call install_mo,LOCALEDIR) - install every compiled catalog below LOCALEDIR.
define install_mo
	for lang in $(LANGS); do \
		install -D -m 0644 "build/locale/$$lang/LC_MESSAGES/$(DOMAIN).mo" \
			"$(1)/$$lang/LC_MESSAGES/$(DOMAIN).mo" || exit 1; \
	done
endef

# $(call refresh_caches,DATADIR) - like the app itself: rebuild the icon cache only if one exists,
# and always bump the hicolor folder's mtime (running desktops only rescan a theme when it changes).
define refresh_caches
	@if command -v update-desktop-database >/dev/null 2>&1; then \
		update-desktop-database -q "$(1)/applications" || true; \
	fi
	@if [ -f "$(1)/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then \
		gtk-update-icon-cache -q -t -f "$(1)/icons/hicolor" || true; \
	fi
	@if [ -d "$(1)/icons/hicolor" ]; then touch -m "$(1)/icons/hicolor" || true; fi
endef

.PHONY: all dev test lint po mo run install uninstall install-user uninstall-user deb clean

all: mo

dev:
	test -x $(VENV_PYTHON) || $(PYTHON) -m venv --system-site-packages $(VENV)
	$(VENV_PYTHON) -m pip install --quiet pytest

test:
	$(TEST_PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q src tools
	$(PYTHON) -c 'import ast, sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]' \
		data/easy-installer data/easy-installer-helper
	bash -n packaging/build-deb.sh
	$(PYTHON) -c 'import sys, xml.etree.ElementTree as ET; [ET.parse(f) for f in sys.argv[1:]]' \
		data/$(APP_ID).policy data/$(APP_ID).metainfo.xml data/$(ICON_SVG) data/$(ICON_SYMBOLIC)
	@if command -v desktop-file-validate >/dev/null 2>&1; then \
		echo "desktop-file-validate data/$(APP_ID).desktop"; \
		desktop-file-validate data/$(APP_ID).desktop; \
	fi
	@# Only errors fail: "url-homepage-missing" stays a warning until the project has a homepage.
	@if command -v appstreamcli >/dev/null 2>&1; then \
		echo "appstreamcli validate --no-net data/$(APP_ID).metainfo.xml"; \
		out=$$(appstreamcli validate --no-net data/$(APP_ID).metainfo.xml 2>&1); \
		printf '%s\n' "$$out"; \
		! printf '%s\n' "$$out" | grep -q '^E:'; \
	fi

po:
	find src/easy_installer -name '*.py' ! -empty ! -path '*/__pycache__/*' | LC_ALL=C sort > po/POTFILES
	$(PYTHON) tools/xgettext.py -o $(POT_FILE) --files-from po/POTFILES
	$(PYTHON) tools/update_po.py $(POT_FILE) $(foreach lang,$(LINGUAS),po/$(lang).po)

mo: $(MO_FILES)

build/locale/%/LC_MESSAGES/$(DOMAIN).mo: po/%.po tools/msgfmt.py
	@mkdir -p $(dir $@)
	$(PYTHON) tools/msgfmt.py --check $< -o $@

run: mo
	PYTHONPATH=src $(PYTHON) -m easy_installer $(ARGS)

install: mo
	@test -n "$(VERSION)" || { echo "Cannot read __version__ from src/easy_installer/__init__.py" >&2; exit 1; }
	$(call copy_package,$(DESTDIR)$(PYTHONDIR))
	install -d "$(DESTDIR)$(BINDIR)" "$(DESTDIR)$(LIBEXECDIR)/easy-installer"
	$(call wrapper,data/easy-installer,$(PYTHONDIR),$(LOCALEDIR)) > "$(DESTDIR)$(BINDIR)/easy-installer"
	chmod 0755 "$(DESTDIR)$(BINDIR)/easy-installer"
	$(call wrapper,data/easy-installer-helper,$(PYTHONDIR),$(LOCALEDIR)) > "$(DESTDIR)$(HELPER)"
	chmod 0755 "$(DESTDIR)$(HELPER)"
	install -D -m 0644 data/$(APP_ID).desktop "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	install -D -m 0644 data/$(APP_ID).metainfo.xml "$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml"
	install -D -m 0644 data/$(ICON_SVG) "$(DESTDIR)$(DATADIR)/$(ICON_SVG)"
	install -D -m 0644 data/$(ICON_SYMBOLIC) "$(DESTDIR)$(DATADIR)/$(ICON_SYMBOLIC)"
	install -d "$(DESTDIR)$(POLKITDIR)"
	sed 's|/usr/libexec/easy-installer/easy-installer-helper|$(HELPER)|' data/$(APP_ID).policy \
		> "$(DESTDIR)$(POLKITDIR)/$(APP_ID).policy"
	chmod 0644 "$(DESTDIR)$(POLKITDIR)/$(APP_ID).policy"
	$(call install_mo,$(DESTDIR)$(LOCALEDIR))
	$(if $(DESTDIR),,$(call refresh_caches,$(DATADIR)))
	@case "$(HELPER)" in \
	    /usr/libexec/easy-installer/easy-installer-helper|/usr/local/libexec/easy-installer/easy-installer-helper) \
	        helper_found=yes ;; \
	    *) helper_found=no ;; \
	esac; \
	if [ "$$helper_found" = no ] || [ "$(POLKITDIR)" != "/usr/share/polkit-1/actions" ]; then \
		echo "Note: installing for all users will ask for the password with a generic prompt;"; \
		echo "      the dedicated prompt needs PREFIX=/usr or /usr/local (or the .deb from 'make deb')."; \
	fi

# The polkit policy lies outside PREFIX; it is only removed if it points at this PREFIX's helper
# (a policy of the .deb or of another PREFIX stays).
uninstall:
	rm -rf "$(DESTDIR)$(PYTHONDIR)/easy_installer"
	[ ! -d "$(DESTDIR)$(PYTHONDIR)" ] || rmdir --ignore-fail-on-non-empty "$(DESTDIR)$(PYTHONDIR)"
	rm -f "$(DESTDIR)$(BINDIR)/easy-installer" "$(DESTDIR)$(HELPER)"
	[ ! -d "$(DESTDIR)$(LIBEXECDIR)/easy-installer" ] || \
		rmdir --ignore-fail-on-non-empty "$(DESTDIR)$(LIBEXECDIR)/easy-installer"
	rm -f "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	rm -f "$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml"
	rm -f "$(DESTDIR)$(DATADIR)/$(ICON_SVG)" "$(DESTDIR)$(DATADIR)/$(ICON_SYMBOLIC)"
	if [ -f "$(DESTDIR)$(POLKITDIR)/$(APP_ID).policy" ] && \
	   grep -qF '>$(HELPER)<' "$(DESTDIR)$(POLKITDIR)/$(APP_ID).policy"; then \
		rm -f "$(DESTDIR)$(POLKITDIR)/$(APP_ID).policy"; \
	fi
	for lang in $(sort $(LINGUAS) $(LANGS)); do rm -f "$(DESTDIR)$(LOCALEDIR)/$$lang/LC_MESSAGES/$(DOMAIN).mo"; done
	$(if $(DESTDIR),,$(call refresh_caches,$(DATADIR)))
	@echo "Easy Installer was removed. Apps installed with it for everyone are still installed;"
	@echo "their list is kept in /var/lib/easy-installer/registry.json."

install-user: mo
	$(call copy_package,$(USER_PYTHONDIR))
	install -d "$(USER_BINDIR)"
	$(call wrapper,data/easy-installer,$(USER_PYTHONDIR),$(USER_LOCALEDIR)) > "$(USER_BINDIR)/easy-installer"
	chmod 0755 "$(USER_BINDIR)/easy-installer"
	install -d "$(USER_DATADIR)/applications"
	sed 's|^Exec=easy-installer |Exec="$(USER_BINDIR)/easy-installer" |' data/$(APP_ID).desktop \
		> "$(USER_DATADIR)/applications/$(APP_ID).desktop"
	chmod 0644 "$(USER_DATADIR)/applications/$(APP_ID).desktop"
	install -D -m 0644 data/$(ICON_SVG) "$(USER_DATADIR)/$(ICON_SVG)"
	install -D -m 0644 data/$(ICON_SYMBOLIC) "$(USER_DATADIR)/$(ICON_SYMBOLIC)"
	$(call install_mo,$(USER_LOCALEDIR))
	$(call refresh_caches,$(USER_DATADIR))
	@echo "Easy Installer is installed for you. Start it from the app menu or with $(USER_BINDIR)/easy-installer"

uninstall-user:
	rm -rf "$(USER_PYTHONDIR)/easy_installer"
	[ ! -d "$(USER_PYTHONDIR)" ] || rmdir --ignore-fail-on-non-empty "$(USER_PYTHONDIR)"
	rm -f "$(USER_BINDIR)/easy-installer"
	rm -f "$(USER_DATADIR)/applications/$(APP_ID).desktop"
	rm -f "$(USER_DATADIR)/$(ICON_SVG)" "$(USER_DATADIR)/$(ICON_SYMBOLIC)"
	for lang in $(sort $(LINGUAS) $(LANGS)); do rm -f "$(USER_LOCALEDIR)/$$lang/LC_MESSAGES/$(DOMAIN).mo"; done
	$(call refresh_caches,$(USER_DATADIR))
	@echo "Easy Installer was removed. Apps you installed with it are still installed;"
	@echo "their list is kept in $(USER_DATADIR)/easy-installer/registry.json."

deb:
	bash packaging/build-deb.sh

clean:
	rm -rf build dist .pytest_cache
	find src tools tests -name __pycache__ -type d -prune -exec rm -rf {} +
