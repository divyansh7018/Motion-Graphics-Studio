"""Shared pytest fixtures.

All tests run against throw-away folders inside pytest's ``tmp_path`` and never
touch the developer's real data folder, output folder or logs.  The GUI tests
use Qt's ``offscreen`` platform so they can run on a build machine with no
display and never pop up a window.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repository importable regardless of the working directory
# (directive section 69: the application must not depend on the cwd).
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Qt must never try to open a window during tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Keep Qt from writing configuration into the user profile during tests.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """An empty folder that acts as the application data root."""
    root = tmp_path / "appdata"
    root.mkdir()
    return root


@pytest.fixture()
def paths(data_root: Path):
    """A fully created :class:`AppPaths` instance pointing at a temp folder."""
    from app.core.paths import AppPaths

    instance = AppPaths(data_root=data_root, source_root=REPO_ROOT, reason="pytest")
    instance.ensure()
    return instance


@pytest.fixture()
def settings():
    from app.core.settings import Settings

    return Settings()


@pytest.fixture()
def logging_ready(tmp_path: Path):
    """Start file logging for a test and shut it down afterwards."""
    from app.core import logging_setup

    log_dir = tmp_path / "logs"
    logging_setup.setup_logging(log_dir, level="DEBUG", console=False, force=True)
    yield logging_setup.active_log_file()
    logging_setup.shutdown_logging()


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole test session."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
