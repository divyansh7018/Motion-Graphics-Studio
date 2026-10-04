"""Version and identity constants for Motion Graphics Studio.

This module is intentionally dependency free and has no side effects so that it
is safe to import from anywhere (GUI, CLI, tests, worker processes).
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Application identity
# --------------------------------------------------------------------------
APP_NAME = "Motion Graphics Studio"
APP_SHORT_NAME = "Motion Studio"
APP_ID = "motion-graphics-studio"
APP_PUBLISHER = "Motion Graphics Studio"

#: Semantic version of the desktop application itself.
APP_VERSION = "0.1.0"

#: Development stage currently implemented (see docs/ROADMAP.md).
#: Stage A = application shell + settings + system check.
APP_STAGE = "A"

#: Human readable stage label used in the UI.
APP_STAGE_LABEL = "Stage A - Foundation (shell, settings, system check)"

# --------------------------------------------------------------------------
# On-disk schema versions.  Every persisted format carries its own version so
# that future changes can be migrated instead of guessed at (see directive
# sections 6 and 12).
# --------------------------------------------------------------------------
SETTINGS_SCHEMA_VERSION = 1
PROJECT_SCHEMA_VERSION = 1  # reserved for Stage B (project model)

#: Minimum Python required by the application.
MIN_PYTHON = (3, 10)

#: Windows is the first supported release target (directive section 2 / 10).
PRIMARY_PLATFORM = "Windows 10/11 (CPU only)"


def version_string() -> str:
    """Return a short human readable version string, e.g. ``0.1.0 (stage A)``."""
    return f"{APP_VERSION} (stage {APP_STAGE})"


def full_version_string() -> str:
    """Return a longer version string used in logs and the About screen."""
    return f"{APP_NAME} {APP_VERSION} - stage {APP_STAGE} - schema {SETTINGS_SCHEMA_VERSION}"
