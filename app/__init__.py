"""Application package root.

Importing :mod:`app` must stay cheap and side-effect free: the GUI, the CLI and
the tests all import it, and a worker process must never start doing work just
because a module was imported (directive sections 8 and 44).
"""

from __future__ import annotations

from .core.version import APP_NAME, APP_VERSION, version_string

__all__ = ["APP_NAME", "APP_VERSION", "version_string"]
