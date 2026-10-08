"""GTK 4 / libadwaita 1.5 user interface.

Importing this package pins the GObject introspection versions, so every GUI module can simply
``from gi.repository import Adw, Gtk``. The CLI never imports it (see ``__main__``).
"""

from __future__ import annotations

import gi

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelibs missing: gir1.2-gtk-4.0 / gir1.2-adw-1
    raise ImportError(
        "Easy Installer needs GTK 4 and libadwaita (packages gir1.2-gtk-4.0 and gir1.2-adw-1)."
    ) from exc

#: Oldest libadwaita this GUI is written against (Adw.Dialog, Adw.AlertDialog, Adw.AboutDialog).
MIN_ADW_VERSION = (1, 5)
