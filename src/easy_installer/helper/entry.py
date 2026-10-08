"""Development entry point of the helper: ``pkexec /usr/bin/python3 <abs path>/entry.py <op>``.

pkexec wipes the environment (no PYTHONPATH) and starts in /root or /, so the folder that contains
the ``easy_installer`` package (``src/`` of the checkout) is put on ``sys.path`` explicitly. The
installed helper (/usr/libexec/easy-installer/easy-installer-helper) does not use this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def prepare_sys_path() -> str:
    """Put the package root first on sys.path; drop the script's own folder (helper/)."""
    here = Path(__file__).resolve()
    script_dir = os.path.realpath(here.parent)
    package_root = str(here.parents[2])
    # Python puts the script's folder first; its modules (main, ops, ...) must not shadow others.
    sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != script_dir]
    sys.path.insert(0, package_root)
    return package_root


def run() -> int:
    # Running as root: never write root-owned __pycache__ files into the user's checkout.
    sys.dont_write_bytecode = True
    prepare_sys_path()
    from easy_installer.helper.main import main

    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(run())
