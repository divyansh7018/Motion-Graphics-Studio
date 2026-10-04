"""Structured log event names.

Directive section 39 requires every major action to be traceable.  Using a
closed set of event names (instead of free-form sentences) keeps the log
greppable and machine readable:

    grep EVENT=RENDER_START logs/studio.log

Events that are not needed until a later stage are declared now so that the
whole vocabulary lives in one place and cannot drift.
"""

from __future__ import annotations


class Event:
    """Log event identifiers (``EVENT=<name>`` appears in every log line)."""

    # -- application lifecycle --------------------------------------------
    APP_START = "APP_START"
    APP_READY = "APP_READY"
    APP_EXIT = "APP_EXIT"
    APP_CRASH = "APP_CRASH"
    DATA_ROOT_RESOLVED = "DATA_ROOT_RESOLVED"
    DIRECTORIES_ENSURED = "DIRECTORIES_ENSURED"
    LOGGING_READY = "LOGGING_READY"

    # -- configuration ----------------------------------------------------
    SETTINGS_LOADED = "SETTINGS_LOADED"
    SETTINGS_DEFAULTS_USED = "SETTINGS_DEFAULTS_USED"
    SETTINGS_CORRUPT = "SETTINGS_CORRUPT"
    SETTINGS_SAVED = "SETTINGS_SAVED"
    SETTINGS_RESET = "SETTINGS_RESET"
    SETTINGS_MIGRATED = "SETTINGS_MIGRATED"

    # -- system checks ----------------------------------------------------
    SYSTEM_CHECK_START = "SYSTEM_CHECK_START"
    SYSTEM_CHECK_COMPLETE = "SYSTEM_CHECK_COMPLETE"
    SYSTEM_CHECK_ITEM = "SYSTEM_CHECK_ITEM"
    SYSTEM_CHECK_FAILED = "SYSTEM_CHECK_FAILED"
    ENVIRONMENT_PROBE = "ENVIRONMENT_PROBE"

    # -- dependencies -----------------------------------------------------
    FFMPEG_DETECTED = "FFMPEG_DETECTED"
    FFMPEG_MISSING = "FFMPEG_MISSING"
    FFPROBE_DETECTED = "FFPROBE_DETECTED"
    FFPROBE_MISSING = "FFPROBE_MISSING"
    KOKORO_DETECTED = "KOKORO_DETECTED"
    KOKORO_MISSING = "KOKORO_MISSING"
    VOICES_SCANNED = "VOICES_SCANNED"
    PACKAGE_DETECTED = "PACKAGE_DETECTED"
    PACKAGE_MISSING = "PACKAGE_MISSING"

    # -- projects (Stage B onward) ---------------------------------------
    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_OPENED = "PROJECT_OPENED"
    PROJECT_SAVED = "PROJECT_SAVED"
    PROJECT_SAVE_FAILED = "PROJECT_SAVE_FAILED"
    PROJECT_AUTOSAVED = "PROJECT_AUTOSAVED"
    PROJECT_BACKUP_CREATED = "PROJECT_BACKUP_CREATED"
    PROJECT_RECOVERY_FOUND = "PROJECT_RECOVERY_FOUND"
    PROJECT_CLOSED = "PROJECT_CLOSED"

    # -- jobs -------------------------------------------------------------
    JOB_SUBMITTED = "JOB_SUBMITTED"
    JOB_START = "JOB_START"
    JOB_PROGRESS = "JOB_PROGRESS"
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_CANCEL_REQUESTED = "JOB_CANCEL_REQUESTED"
    JOB_CANCEL_TIMEOUT = "JOB_CANCEL_TIMEOUT"
    JOB_REJECTED_DUPLICATE = "JOB_REJECTED_DUPLICATE"

    # -- voice / narration (Stage C) -------------------------------------
    KOKORO_INIT_START = "KOKORO_INIT_START"
    KOKORO_INITIALIZED = "KOKORO_INITIALIZED"
    KOKORO_INIT_FAILED = "KOKORO_INIT_FAILED"
    VOICE_SELECTED = "VOICE_SELECTED"
    TTS_START = "TTS_START"
    TTS_COMPLETE = "TTS_COMPLETE"
    TTS_FAILED = "TTS_FAILED"
    VOICE_PREVIEW_START = "VOICE_PREVIEW_START"
    VOICE_PREVIEW_COMPLETE = "VOICE_PREVIEW_COMPLETE"
    AUDIO_VALIDATION_FAILED = "AUDIO_VALIDATION_FAILED"

    # -- scenes / preview / render (Stages D, F) -------------------------
    SCENE_PREVIEW = "SCENE_PREVIEW"
    STORYBOARD_UPDATED = "STORYBOARD_UPDATED"
    RENDER_PREVIEW_START = "RENDER_PREVIEW_START"
    RENDER_PREVIEW_COMPLETE = "RENDER_PREVIEW_COMPLETE"
    RENDER_START = "RENDER_START"
    RENDER_COMPLETE = "RENDER_COMPLETE"
    RENDER_FAILED = "RENDER_FAILED"
    FFMPEG_START = "FFMPEG_START"
    FFMPEG_COMPLETE = "FFMPEG_COMPLETE"
    FFMPEG_FAILED = "FFMPEG_FAILED"

    # -- quality control / output (Stage G) ------------------------------
    QC_START = "QC_START"
    QC_COMPLETE = "QC_COMPLETE"
    QC_FAILED = "QC_FAILED"
    OUTPUT_SAVED = "OUTPUT_SAVED"
    OUTPUT_VALIDATED = "OUTPUT_VALIDATED"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"

    # -- maintenance ------------------------------------------------------
    CACHE_CLEARED = "CACHE_CLEARED"
    TEMP_CLEANED = "TEMP_CLEANED"
    DISK_SPACE_LOW = "DISK_SPACE_LOW"
    ERROR = "ERROR"
    WARNING = "WARNING"
    CANCELLED = "CANCELLED"
    USER_ACTION = "USER_ACTION"


#: Human readable descriptions used by the log viewer and documentation.
EVENT_DESCRIPTIONS: dict[str, str] = {
    Event.APP_START: "The application started.",
    Event.APP_READY: "Startup finished and the main window is interactive.",
    Event.APP_EXIT: "The application is shutting down.",
    Event.APP_CRASH: "An unexpected error was caught by the global error handler.",
    Event.SYSTEM_CHECK_START: "A system readiness check began.",
    Event.SYSTEM_CHECK_COMPLETE: "A system readiness check finished.",
    Event.JOB_START: "A background job started.",
    Event.JOB_SUCCEEDED: "A background job finished successfully.",
    Event.JOB_FAILED: "A background job failed.",
    Event.JOB_CANCELLED: "A background job was cancelled by the user.",
    Event.CACHE_CLEARED: "Cached files were removed.",
}
