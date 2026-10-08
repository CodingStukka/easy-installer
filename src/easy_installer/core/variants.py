"""Several installed versions of one app ("keep both"): which registry entry a file belongs to.

An app's **main** entry has the id derived from the app itself (``org.freecad.FreeCAD``). A copy
kept next to it has the id ``variant_id(main id, version, sha256)``, ``base_id`` = the main id
and is ``pinned``. Matching rules for a file that is about to be installed:

1. the file *is* the app file of an entry of this app (same path) -> that entry (repair);
2. its sha256 equals that of an installed copy -> that copy (a reinstall of it);
3. otherwise the main entry: a new file updates or downgrades the main installation, unless the
   user asks to keep both.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .integration import variant_id, variant_name, version_label, with_name_suffix
from .registry import InstalledApp

__all__ = ["Match", "family", "match_installed", "variant_id", "variant_name", "version_label",
           "with_name_suffix"]


@dataclass(frozen=True)
class Match:
    #: the entry with the app's own id (None: only copies, or nothing, is installed)
    main: InstalledApp | None
    #: the entry the file belongs to, or that it would replace (None: nothing to replace)
    entry: InstalledApp | None
    #: every installed copy ("keep both") of the app
    copies: tuple[InstalledApp, ...]
    #: ``entry`` was found by rule 1 or 2: the file is already installed as that entry
    is_installed_file: bool


def family(apps: Iterable[InstalledApp], main_id: str) -> list[InstalledApp]:
    """The main entry and all kept copies of the app ``main_id`` (in one scope)."""
    return [app for app in apps if app.main_id == main_id]


def _same_file_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def match_installed(apps: Iterable[InstalledApp], main_id: str, path: Path,
                    sha256: str | None) -> Match:
    members = family(apps, main_id)
    main = next((app for app in members if app.id == main_id), None)
    copies = tuple(app for app in members if app.id != main_id)
    for app in members:
        if app.appimage_path and _same_file_path(path, app.appimage_path):
            return Match(main, app, copies, True)
    if sha256:
        for app in copies:
            if app.sha256 and app.sha256 == sha256:
                return Match(main, app, copies, True)
    same = bool(main is not None and sha256 and main.sha256 and main.sha256 == sha256)
    return Match(main, main, copies, same)
