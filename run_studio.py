#!/usr/bin/env python
"""Launcher for Motion Graphics Studio (double-click friendly).

This file is the shortcut target on Windows: it works no matter what the
current working directory is, because it puts its own folder on ``sys.path``
first (directive section 69).  It contains no application logic - that lives in
``app/main.py`` - so it is safe to run from a shortcut, from a terminal, or
from another directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _ensure_source_root_on_path() -> Path:
    source_root = Path(__file__).resolve().parent
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    # Run relative paths (assets, tools, docs) from the application folder.
    try:
        os.chdir(source_root)
    except OSError:
        pass
    return source_root


def main() -> int:
    root = _ensure_source_root_on_path()

    missing = []
    if sys.version_info < (3, 10):
        missing.append(f"Python 3.10 or newer is required (found {sys.version.split()[0]}).")
    try:
        import PySide6  # noqa: F401
    except ImportError:
        missing.append(
            "PySide6 is not installed. Install it with:\n"
            "    pip install -r requirements.txt"
        )

    if missing:
        message = "\n\n".join(missing)
        print(f"Motion Graphics Studio cannot start:\n\n{message}", file=sys.stderr)
        try:
            sys.path.insert(0, str(root))
            from app.main import _show_fatal_dialog  # type: ignore

            _show_fatal_dialog("Motion Graphics Studio cannot start", message)
        except Exception:
            pass
        return 1

    from app.main import main as app_main

    return app_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
