"""Helpers shared by the GUI modules: icons, markup escaping, styling, launching, error texts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

from ..core.installer import display_text
from ..errors import (
    ArchiveError,
    AuthorizationError,
    EasyInstallerError,
    ExtractionError,
    HelperError,
    InstallError,
    NetworkError,
    NotAnAppImageError,
    NotInstalledError,
    UnsupportedArchitectureError,
    UpdateCancelled,
    UpdateError,
)
from ..i18n import _
from .async_utils import format_exception

log = logging.getLogger(__name__)

FALLBACK_APP_ICON = "application-x-executable"
APPIMAGE_MIME_TYPES = ("application/vnd.appimage", "application/x-iso9660-appimage")

CSS = """
.drop-overlay {
  margin: 12px;
  border-radius: 12px;
  border: 2px dashed alpha(@accent_color, 0.9);
  background-color: alpha(@window_bg_color, 0.93);
}
.drop-overlay image {
  color: @accent_color;
}
.install-header .title-1 {
  margin-top: 6px;
}
.app-comment {
  margin-top: 6px;
}
.details-text {
  padding: 6px 12px;
}
.banner-dismiss {
  margin: 0 6px;
}
/* Keep the banner text clear of the dismiss button overlaid on its start edge. */
banner.dismissable-banner > revealer > widget {
  padding-left: 46px;
}
/* A small, quiet label next to an app's name, e.g. "Portable". */
.app-tag {
  font-size: smaller;
  font-weight: bold;
  padding: 1px 8px;
  border-radius: 999px;
  color: alpha(currentColor, 0.75);
  background-color: alpha(currentColor, 0.1);
}
.details-header .title-1 {
  margin-top: 6px;
}
.details-header .app-comment {
  margin-top: 2px;
}
"""


def load_css(display: Gdk.Display | None = None) -> None:
    display = display or Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK >= 4.12
        provider.load_from_string(CSS)
    else:  # pragma: no cover - older GTK
        provider.load_from_data(CSS, -1)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def escape(text: str | None) -> str:
    """Escape text for widgets that interpret Pango markup (toasts, row titles, status pages)."""
    return GLib.markup_escape_text(text or "")


def app_gicon(icon_path: str | Path | None) -> Gio.Icon:
    if icon_path:
        path = Path(icon_path)
        if path.is_file():
            return Gio.FileIcon.new(Gio.File.new_for_path(str(path)))
    return Gio.ThemedIcon.new(FALLBACK_APP_ICON)


def app_icon_image(icon_path: str | Path | None, size: int) -> Gtk.Image:
    image = Gtk.Image(gicon=app_gicon(icon_path), pixel_size=size,
                      accessible_role=Gtk.AccessibleRole.PRESENTATION)
    image.add_css_class("icon-dropshadow")
    return image


def app_icon_paintable(widget: Gtk.Widget, icon_path: str | Path | None,
                       size: int) -> Gdk.Paintable | None:
    """A crisp paintable of the app icon (SVGs are rendered at ``size``), e.g. for Adw.StatusPage."""
    display = widget.get_display() or Gdk.Display.get_default()
    if display is None:
        return None
    theme = Gtk.IconTheme.get_for_display(display)
    try:
        return theme.lookup_by_gicon(app_gicon(icon_path), size, max(widget.get_scale_factor(), 1),
                                     Gtk.TextDirection.NONE, Gtk.IconLookupFlags(0))
    except GLib.Error as exc:
        log.debug("cannot load icon %s: %s", icon_path, exc)
        return None


def launch_desktop_file(desktop_path: str | Path, widget: Gtk.Widget) -> None:
    """Start an installed app via its launcher file. Raises GLib.Error or ValueError."""
    info = Gio.DesktopAppInfo.new_from_filename(str(desktop_path))
    if info is None:
        raise ValueError(f"cannot load launcher {desktop_path}")
    display = widget.get_display() or Gdk.Display.get_default()
    context = display.get_app_launch_context() if display is not None else None
    info.launch([], context)


@dataclass(frozen=True)
class FriendlyError:
    title: str
    message: str
    details: str | None


def describe_error(error: BaseException, *, reading: bool = False) -> FriendlyError:
    """User-facing title, message and technical details for any exception.

    ``reading``: the error happened while looking at the file (before anything was installed).
    """
    if isinstance(error, EasyInstallerError):
        if isinstance(error, (NotAnAppImageError, ArchiveError)):
            title = _("This file cannot be installed")
        elif isinstance(error, ExtractionError):
            title = _("The file seems to be damaged")
        elif isinstance(error, UnsupportedArchitectureError):
            title = _("This app does not work on this computer")
        elif isinstance(error, NotInstalledError):
            title = _("The app is not installed")
        elif isinstance(error, AuthorizationError):
            title = _("Authentication failed")
        elif isinstance(error, NetworkError):
            title = _("No connection")
        elif isinstance(error, UpdateCancelled):
            title = _("The update was cancelled")
        elif isinstance(error, UpdateError):
            title = _("The update could not be installed")
        elif isinstance(error, (InstallError, HelperError)):
            title = _("Installation failed")
        else:
            title = _("This file cannot be installed") if reading else _("Something went wrong")
        return FriendlyError(title, display_text(str(error)),
                             display_text(error.details) if error.details else error.details)
    return FriendlyError(
        _("Something went wrong"),
        _("An unexpected problem occurred. The technical details below can help when "
          "reporting it."),
        display_text(format_exception(error)),
    )


def toast_text(text: str) -> str:
    """Toasts are short labels: no trailing full stop, markup escaped."""
    text = text.strip()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    return escape(text)


def format_size(size: int | None) -> str:
    """A file size for people ("820.3 MB"), as GNOME shows sizes."""
    return GLib.format_size(max(int(size or 0), 0))


def format_date(iso_text: str | None) -> str | None:
    """"2026-09-29T15:04:05Z" as a local date ("29 September 2026"); None if unreadable."""
    if not iso_text:
        return None
    try:
        moment = GLib.DateTime.new_from_iso8601(iso_text, GLib.TimeZone.new_utc())
    except (TypeError, GLib.Error):
        moment = None
    if moment is None:
        return None
    # Translators: a date as in "29 September 2026" (GLib.DateTime.format codes: %-d day,
    # %B month name, %Y year), e.g. German "%-d. %B %Y".
    return moment.to_local().format(_("%-d %B %Y"))


def display_path(path: str | os.PathLike | None) -> str:
    """A path as people read it: the home folder as "~", names that are not UTF-8 with U+FFFD."""
    if not path:
        return ""
    text = os.fspath(path)
    home = os.path.expanduser("~").rstrip("/")
    if home and (text == home or text.startswith(home + "/")):
        text = "~" + text[len(home):]
    return display_text(text)


def icon_button(icon_name: str, tooltip: str, *, flat: bool = True) -> Gtk.Button:
    """An icon-only button with a tooltip that is also its accessible name."""
    button = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    if flat:
        button.add_css_class("flat")
    return button
