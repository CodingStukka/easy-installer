"""Pieces shared by the install and the update dialog.

The text helpers at the top need no display (they are tested without one); the widget helpers
below them build small, consistent parts: warning rows, pill buttons and the compact list of
facts about a file ("Downloaded from github.com", "Signed with a key named …", "Gets updates
from …").
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..core.origin import origin_host
from ..core.signature import STATUS_INVALID, STATUS_VALID, SignatureInfo
from ..core.updates import UpdateSource
from ..errors import HelperError, InstallError, NetworkError, UpdateError
from ..i18n import _, ngettext
from .common import FriendlyError, describe_error

log = logging.getLogger(__name__)

#: Programs whose score is at most this far below the best one are offered in "Program to
#: start" (see ``core.portable.score_executable``: a name match is worth 30-60 points, the place
#: in the folder 8-10, being a real program 2 and a vendor's start script 5).
SIMILAR_SCORE = 25
#: At most this many programs are offered.
MAX_PROGRAM_CHOICES = 8

ICON_ORIGIN = "folder-download-symbolic"
ICON_SIGNED = "security-high-symbolic"
ICON_UPDATES = "software-update-available-symbolic"
ICON_NO_UPDATES = "document-open-recent-symbolic"
ICON_BACKUP = "edit-undo-symbolic"
ICON_DOWNLOAD = "folder-download-symbolic"
ICON_SOURCE = "web-browser-symbolic"
ICON_PASSWORD = "dialog-password-symbolic"

CSS = """
.fact-list > row {
  min-height: 0;
}
.fact-list .fact {
  padding: 9px 12px;
}
.fact-list .fact image {
  opacity: 0.7;
}
.version-change {
  font-weight: bold;
  font-feature-settings: "tnum";
}
"""

_css_displays: set[int] = set()


# ------------------------------------------------------------------------------------------------
# texts (no display needed)
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """One line of the compact fact list: a symbolic icon, a sentence, an optional tooltip."""

    icon: str
    text: str
    tooltip: str | None = None


def origin_text(url: str | None) -> str | None:
    """"Downloaded from github.com", or None if the file does not say where it came from."""
    host = origin_host(url)
    return _("Downloaded from {host}").format(host=host) if host else None


def _signature(value: object) -> SignatureInfo | None:
    if isinstance(value, SignatureInfo):
        return value
    to_dict = getattr(value, "to_dict", None)
    return SignatureInfo.from_dict(to_dict() if callable(to_dict) else value)


def signature_text(value: object, *, installed: object = None) -> str | None:
    """The line about a signature gpg confirmed; unsigned is normal and says nothing, an
    unverified signature proves nothing, so it says nothing either (a broken one is a warning
    of the installer).

    The key travels inside the file and anyone can give a key any name: only the same key as
    the one of the installed version (``installed``: its registry entry or signature) says who
    made it - "Signed by the same maker as the installed version". Else the name is only
    quoted as the key's name, never presented as a fact."""
    signature = _signature(value)
    if signature is None or signature.status != STATUS_VALID:
        return None
    reference = _signature(getattr(installed, "signature", installed))
    if reference is not None and reference.status == STATUS_VALID and reference.fingerprint \
            and reference.fingerprint == signature.fingerprint:
        return _("Signed by the same maker as the installed version")
    if signature.signer:
        return _("Signed with a key named “{signer}”").format(signer=signature.signer)
    return _("Signed with a key that has no name")


def signature_is_broken(value: object) -> bool:
    """The file carries a signature that does not match its content (gpg said so)."""
    signature = _signature(value)
    return signature is not None and signature.status == STATUS_INVALID


def fingerprint_text(value: object) -> str | None:
    """Tooltip for the signature line: the key's fingerprint in groups of four."""
    signature = _signature(value)
    if signature is None or not signature.fingerprint:
        return None
    groups = " ".join(signature.fingerprint[i:i + 4]
                      for i in range(0, len(signature.fingerprint), 4))
    return _("Key fingerprint: {fingerprint}").format(fingerprint=groups)


def _source(value: object) -> UpdateSource | None:
    if isinstance(value, UpdateSource):
        return value
    return UpdateSource.from_dict(value) if isinstance(value, dict) else None


def update_source_text(value: object) -> str | None:
    """"Gets updates from github.com/FreeCAD/FreeCAD", or None without an update source."""
    source = _source(value)
    if source is None:
        return None
    try:
        where = source.describe()
    except Exception:  # noqa: BLE001 - a display text is never worth a crash
        log.debug("cannot describe update source %r", source, exc_info=True)
        return None
    return _("Gets updates from {source}").format(source=where) if where else None


def file_facts(info: object, *, pinned: bool = False, installed: object = None) -> list[Fact]:
    """What is known about where a file comes from: origin, a valid signature, where updates
    come from. ``info`` is an ``AppImageInfo`` or ``PortableInfo`` (missing attributes are
    simply not known). ``pinned``: the file is installed as a kept copy, which never gets
    updates. ``installed``: the installed version of the app (see :func:`signature_text`)."""
    facts: list[Fact] = []
    text = origin_text(getattr(info, "origin_url", None))
    if text:
        facts.append(Fact(ICON_ORIGIN, text))
    signature = getattr(info, "signature", None)
    text = signature_text(signature, installed=installed)
    if text:
        facts.append(Fact(ICON_SIGNED, text, fingerprint_text(signature)))
    text = update_source_text(getattr(info, "update_source", None))
    if text and pinned:
        facts.append(Fact(ICON_NO_UPDATES, _("This copy stays at its version and gets no "
                                             "updates")))
    elif text:
        facts.append(Fact(ICON_UPDATES, text))
    return facts


def backup_note(version: str | None, days: int) -> str:
    """The replaced version is kept for ``days`` days (rollback)."""
    if version:
        return ngettext("Version {version} is kept for {days} day, so you can go back to it",
                        "Version {version} is kept for {days} days, so you can go back to it",
                        days).format(version=version, days=days)
    return ngettext("The current version is kept for {days} day, so you can go back to it",
                    "The current version is kept for {days} days, so you can go back to it",
                    days).format(days=days)


def version_change_text(old: str | None, new: str | None) -> str:
    """"1.1.3 → 1.1.4" (what an update changes), with wording for unknown versions."""
    if old and new:
        return _("{old} → {new}").format(old=old, new=new)
    if new:
        return _("Version {version}").format(version=new)
    return _("A newer version")


def version_change_label(old: str | None, new: str | None) -> str:
    """The same for screen readers ("from version 1.1.3 to 1.1.4")."""
    if old and new:
        return _("From version {old} to {new}").format(old=old, new=new)
    return version_change_text(old, new)


def program_choices(info: object) -> list[str]:
    """The programs to offer in "Program to start" for a portable archive: the chosen one and
    every candidate that is about as likely to be the app's program as the best one. Empty when
    there is nothing to choose (one clear winner)."""
    candidates = list(getattr(info, "executables", None) or ())
    chosen = getattr(info, "executable", None)
    if not candidates:
        return []
    best = max(candidate.score for candidate in candidates)
    similar = [candidate.relpath for candidate in candidates
               if best - candidate.score <= SIMILAR_SCORE]
    if chosen and chosen not in similar:
        similar.insert(0, chosen)
    similar = list(dict.fromkeys(similar))[:MAX_PROGRAM_CHOICES]
    return similar if len(similar) > 1 else []


def describe_update_error(error: BaseException) -> FriendlyError:
    """Like ``common.describe_error``, with titles that fit an update."""
    friendly = describe_error(error)
    if isinstance(error, NetworkError):
        title = _("The update could not be downloaded")
    elif isinstance(error, (UpdateError, InstallError, HelperError)):
        title = _("The update could not be installed")
    else:
        return friendly
    return FriendlyError(title, friendly.message, friendly.details)


def progress_share(fraction: float | None, start: float, end: float) -> float | None:
    """The part of an overall progress ``fraction`` that lies between ``start`` and ``end``,
    as 0..1 (None stays None: the step does not know how far it is)."""
    if fraction is None:
        return None
    if end <= start:
        return 1.0
    return max(0.0, min(1.0, (fraction - start) / (end - start)))


def download_text(fraction: float | None, total: int | None) -> str | None:
    """"12.3 MB of 158.0 MB" while downloading ``total`` bytes, None if it cannot be told."""
    if fraction is None or not total or total <= 0:
        return None
    done = int(max(0.0, min(1.0, fraction)) * total)
    return _("{done} of {total}").format(done=GLib.format_size(done),
                                          total=GLib.format_size(total))


# ------------------------------------------------------------------------------------------------
# widgets
# ------------------------------------------------------------------------------------------------


def ensure_css(widget: Gtk.Widget | None = None) -> None:
    """Load the dialogs' own style rules once per display."""
    display = (widget.get_display() if widget is not None else None) or Gdk.Display.get_default()
    if display is None or hash(display) in _css_displays:
        return
    _css_displays.add(hash(display))
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK >= 4.12
        provider.load_from_string(CSS)
    else:  # pragma: no cover - older GTK
        provider.load_from_data(CSS, -1)
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def warning_row(text: str, *, serious: bool = False) -> Adw.ActionRow:
    """A row with a warning icon; ``serious`` uses the error colour (e.g. another signer)."""
    row = Adw.ActionRow(title=text, use_markup=False, title_lines=0)
    icon = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER)
    icon.add_css_class("error" if serious else "warning")
    row.add_prefix(icon)
    return row


def pill(label: str, *, suggested: bool = False) -> Gtk.Button:
    button = Gtk.Button(label=label, use_underline=True, can_shrink=True)
    button.add_css_class("pill")
    if suggested:
        button.add_css_class("suggested-action")
    return button


class FactList(Gtk.ListBox):
    """A compact boxed list of facts (icon + sentence); hides itself when empty."""

    __gtype_name__ = "EasyInstallerFactList"

    def __init__(self) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, visible=False)
        self.add_css_class("boxed-list")
        self.add_css_class("fact-list")
        self._rows: list[Gtk.ListBoxRow] = []
        self._texts: list[str] = []

    @property
    def texts(self) -> list[str]:
        return list(self._texts)

    def set_facts(self, facts: Sequence[Fact]) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows = [self._make_row(fact) for fact in facts]
        self._texts = [fact.text for fact in facts]
        for row in self._rows:
            self.append(row)
        self.set_visible(bool(self._rows))

    @staticmethod
    def _make_row(fact: Fact) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12)
        box.add_css_class("fact")
        icon = Gtk.Image(icon_name=fact.icon, valign=Gtk.Align.CENTER,
                         accessible_role=Gtk.AccessibleRole.PRESENTATION)
        box.append(icon)
        label = Gtk.Label(label=fact.text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          xalign=0, hexpand=True)
        box.append(label)
        row = Gtk.ListBoxRow(child=box, activatable=False, selectable=False)
        if fact.tooltip:
            row.set_tooltip_text(fact.tooltip)
        return row
