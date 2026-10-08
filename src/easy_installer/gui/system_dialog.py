""""Check This Computer": what Easy Installer needs, in plain words."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Callable

from gi.repository import Adw, Gtk

from .. import APP_NAME, __version__
from ..core.sandbox import SandboxFix, default_sandbox_fix
from ..core.system_checks import SystemStatus
from ..i18n import _

OK, INFO, PROBLEM = "ok", "info", "problem"
_ICONS = {
    OK: ("emblem-ok-symbolic", "success"),
    INFO: ("dialog-information-symbolic", "accent"),
    PROBLEM: ("dialog-warning-symbolic", "warning"),
}


@dataclass(frozen=True)
class CheckItem:
    title: str
    state: str
    text: str
    #: The missing FUSE library can be installed from here (Debian/Ubuntu family only).
    offers_fuse_install: bool = False


def fuse_ready(status: SystemStatus) -> bool:
    return status.libfuse2 and status.dev_fuse and status.fusermount is not None


def check_items(status: SystemStatus) -> list[CheckItem]:
    items = []
    if status.unsquashfs:
        items.append(CheckItem(_("Reading AppImages"), OK, _("Ready")))
    else:
        items.append(CheckItem(
            _("Reading AppImages"), PROBLEM,
            _("A helper tool is missing, so AppImages have to be opened briefly to read them. "
              "Installing the “squashfs-tools” package avoids this.")))

    if fuse_ready(status):
        items.append(CheckItem(_("Starting apps"), OK, _("Ready")))
    else:
        items.append(CheckItem(
            _("Starting apps"), PROBLEM,
            _("A system component (FUSE) is missing. Apps still work, but start a bit slower."),
            offers_fuse_install=status.has_apt and not status.libfuse2))

    if not status.userns_restricted:
        items.append(CheckItem(_("Apps with a security sandbox"), OK,
                               _("No extra permission needed")))
    elif default_sandbox_fix(status) is SandboxFix.APPARMOR:
        items.append(CheckItem(
            _("Apps with a security sandbox"), INFO,
            _("This computer blocks app sandboxes by default (AppArmor user namespace "
              "restriction). Easy Installer can allow them per app; you will be asked for your "
              "password.")))
    else:
        items.append(CheckItem(
            _("Apps with a security sandbox"), PROBLEM,
            _("This computer blocks app sandboxes and the permission cannot be given here, so "
              "such apps will start without their sandbox.")))

    if status.pkexec:
        items.append(CheckItem(_("Installing for everyone"), OK,
                               _("Available — asks for your password")))
    else:
        items.append(CheckItem(
            _("Installing for everyone"), PROBLEM,
            _("Not possible, because the password prompt (pkexec) is not installed.")))

    if status.update_desktop_database:
        items.append(CheckItem(_("Updating the app menu"), OK, _("Ready")))
    else:
        items.append(CheckItem(
            _("Updating the app menu"), INFO,
            _("New apps may take a moment to appear in the app menu "
              "(update-desktop-database is missing).")))
    return items


def debug_info(status: SystemStatus | None) -> str:
    """Technical summary for the About dialog's "Troubleshooting" page (not translated)."""
    lines = [
        f"{APP_NAME} {__version__}",
        f"Python {platform.python_version()}",
        f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}."
        f"{Adw.get_micro_version()}",
        f"Architecture {platform.machine()}",
    ]
    if status is not None:
        lines.append("")
        for field in status.__dataclass_fields__:
            lines.append(f"{field}: {getattr(status, field)}")
    return "\n".join(lines) + "\n"


class SystemCheckDialog(Adw.Dialog):
    __gtype_name__ = "EasyInstallerSystemCheckDialog"

    def __init__(self, status: SystemStatus, *, install_fuse: Callable[[], None] | None = None):
        super().__init__(title=_("Check This Computer"), content_width=480)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=_("What Easy Installer needs to install and start apps on this computer."))
        for item in check_items(status):
            row = Adw.ActionRow(title=item.title, subtitle=item.text, use_markup=False,
                                subtitle_lines=0)
            icon_name, css = _ICONS[item.state]
            icon = Gtk.Image(icon_name=icon_name, valign=Gtk.Align.CENTER)
            icon.add_css_class(css)
            row.add_prefix(icon)
            if install_fuse is not None and item.offers_fuse_install:
                button = Gtk.Button(label=_("_Install"), use_underline=True,
                                    valign=Gtk.Align.CENTER)
                button.connect("clicked", lambda *_a: (self.close(), install_fuse()))
                row.add_suffix(button)
            group.add(row)
        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)
