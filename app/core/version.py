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
APP_VERSION = "0.5.0"

#: Development stage currently implemented (see docs/ROADMAP.md).
#: Stage A = application shell + settings + system check.
#: Stage B = project system (project.json, save, autosave, recovery).
#: Stage C = script, Kokoro narration and voice.
#: Stage D = scene engine, storyboard and responsive visual system.
#: Stage E = audio mix, subtitles, timeline, final render and QC.
APP_STAGE = "E"

#: Human readable stage label used in the UI.
APP_STAGE_LABEL = "Stage E - Audio, subtitles, timeline, final render and QC"

# --------------------------------------------------------------------------
# On-disk schema versions.  Every persisted format carries its own version so
# that future changes can be migrated instead of guessed at (see directive
# sections 6 and 12).
# --------------------------------------------------------------------------
SETTINGS_SCHEMA_VERSION = 1
#: Project file format version written by this build.
#:
#: 1 = the draft shape published in ``docs/PROJECT_FORMAT.md`` during Stage A.
#: 2 = the implemented shape (top-level ``project``/``format``/``script``/
#:     ``voice``/``theme``/``audio``/``scenes``/``assets``/``export`` sections).
#: Older files are migrated by :mod:`app.project.migrations`; newer files are
#: refused with a clear message instead of being partially read.
#: Stage E added audio, subtitle and export fields to the model, but every one
#: of them is additive with a default: a schema-3 file written before them loads
#: with the defaults filled in, and an unknown key is preserved in ``extra``
#: rather than dropped.  No migration is needed, so the version is unchanged -
#: bumping it would force a migration that has nothing to do.
PROJECT_SCHEMA_VERSION = 3
MIN_SUPPORTED_PROJECT_SCHEMA = 1

#: Minimum Python required by the application.
MIN_PYTHON = (3, 10)

#: Windows is the first supported release target (directive section 2 / 10).
PRIMARY_PLATFORM = "Windows 10/11 (CPU only)"


def version_string() -> str:
    """Return a short human readable version string, e.g. ``0.1.0 (stage A)``."""
    return f"{APP_VERSION} (stage {APP_STAGE})"


def full_version_string() -> str:
    """Return a longer version string used in logs and the About screen."""
    return f"{APP_NAME} {APP_VERSION} - stage {APP_STAGE} - settings schema {SETTINGS_SCHEMA_VERSION} - project schema {PROJECT_SCHEMA_VERSION}"
