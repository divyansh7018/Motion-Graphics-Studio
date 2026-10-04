"""Allow ``python -m app`` to start the graphical application."""

from __future__ import annotations

from .main import run

if __name__ == "__main__":  # pragma: no cover - manual launch path
    run()
