"""Friendly error system (directive sections 18, 40, 41).

Every user facing failure is described by three questions:

1. **What happened?**   - a short plain-language headline
2. **Why did it happen?** - the cause in one sentence
3. **What can you do?**  - one or more concrete next steps

The original exception text is always kept as *technical detail* so nothing is
hidden (section 62), but it is never the only thing the user sees.  A log file
path is attached so support can ask for it.

Subsystem errors derive from :class:`AppError`, which lets the GUI show a
consistent dialog for anything that escapes a job, while still allowing a
failure in one subsystem to be contained (section 41).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence

from . import logging_setup
from .logging_setup import log_event


class Severity(str, Enum):
    """How serious a reported problem is."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class FriendlyError:
    """A user facing error description plus an optional technical cause."""

    title: str
    what_happened: str
    why: str
    actions: Sequence[str] = field(default_factory=tuple)
    technical: Optional[str] = None
    severity: Severity = Severity.ERROR
    error_code: Optional[str] = None

    # -- rendering ---------------------------------------------------------

    def to_message(self) -> str:
        """Multi-paragraph text for a dialog box (plain text, wraps well)."""
        lines = [self.what_happened.strip(), ""]
        if self.why:
            lines.extend([f"Why: {self.why.strip()}", ""])
        if self.actions:
            lines.append("What you can do:")
            lines.extend(f"  {index}. {action}" for index, action in enumerate(self.actions, start=1))
            lines.append("")
        lines.append(f"Log file: {logging_setup.log_path_for_user()}")
        if self.technical:
            lines.extend(["", "Technical details:", self.technical.strip()])
        return "\n".join(lines).strip()

    def to_short_text(self) -> str:
        """One-line summary used by status bars and list entries."""
        return f"{self.title}: {self.what_happened}"

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "what_happened": self.what_happened,
            "why": self.why,
            "actions": list(self.actions),
            "technical": self.technical,
            "severity": self.severity.value,
            "error_code": self.error_code,
        }


# --------------------------------------------------------------------------
# Base exception
# --------------------------------------------------------------------------

class AppError(Exception):
    """Base class for all expected, reportable application failures.

    Raising this (instead of a bare ``Exception``) signals that the failure has
    a known, friendly explanation available through :meth:`friendly`.
    """

    #: Default headline used when a subclass does not provide one.
    default_title = "Something needs your attention"
    #: Default hint list.
    default_actions: tuple[str, ...] = ("Try the action again.", "If it keeps failing, check the log file.")

    def __init__(
        self,
        what_happened: str,
        why: str = "",
        actions: Optional[Iterable[str]] = None,
        technical: Optional[str] = None,
        title: Optional[str] = None,
        severity: Severity = Severity.ERROR,
        error_code: Optional[str] = None,
    ) -> None:
        super().__init__(what_happened)
        self.what_happened = what_happened
        self.why = why or ""
        self.actions: tuple[str, ...] = tuple(actions) if actions is not None else self.default_actions
        self.technical = technical
        self.title = title or self.default_title
        self.severity = severity
        self.error_code = error_code

    def friendly(self) -> FriendlyError:
        return FriendlyError(
            title=self.title,
            what_happened=self.what_happened,
            why=self.why,
            actions=self.actions,
            technical=self.technical or repr(self),
            severity=self.severity,
            error_code=self.error_code,
        )

    def log(self, extra_event: Optional[str] = None) -> None:
        """Write this error to the structured log once."""
        log_event(
            extra_event or "ERROR",
            self.title,
            level=logging.ERROR,
            logger=logging_setup.get_logger("error"),
            code=self.error_code,
            reason=self.why or "-",
            detail=(self.technical or self.what_happened),
        )


# --------------------------------------------------------------------------
# Subsystem specific errors
# --------------------------------------------------------------------------

class MissingDependencyError(AppError):
    """A required external tool, package or model is not installed."""

    default_title = "A required component is missing"

    def __init__(
        self,
        component: str,
        why: str = "",
        actions: Optional[Iterable[str]] = None,
        technical: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        super().__init__(
            what_happened=f"{component} was not found.",
            why=why,
            actions=actions,
            technical=technical,
            error_code=error_code,
        )
        self.component = component


class FFmpegNotFoundError(MissingDependencyError):
    """FFmpeg or FFprobe could not be located (directive section 29)."""

    def __init__(self, program: str = "FFmpeg", searched: Sequence[str] = (), technical: Optional[str] = None) -> None:
        searched_text = ", ".join(searched) if searched else "the standard locations"
        super().__init__(
            component=program,
            why=(
                f"{program} was not found in the application's 'tools' folder, in the configured path, "
                f"or on the Windows PATH (searched: {searched_text})."
            ),
            actions=(
                "Open Settings → Media Tools and set the FFmpeg folder.",
                "Or copy ffmpeg.exe and ffprobe.exe into the 'tools' folder next to the app.",
                "Or install FFmpeg and add it to the Windows PATH, then press Re-check.",
            ),
            technical=technical,
            error_code="FFMPEG_NOT_FOUND",
        )


class KokoroNotAvailableError(MissingDependencyError):
    """The Kokoro TTS engine or model is missing (directive sections 15, 16)."""

    def __init__(self, why: str = "", actions: Optional[Iterable[str]] = None, technical: Optional[str] = None) -> None:
        super().__init__(
            component="Kokoro voice engine",
            why=why or "The Kokoro package or its model files could not be loaded.",
            actions=actions
            or (
                "Open Settings → Voice Engine to see exactly which part is missing.",
                "Install the voice engine with the Setup page, then press Re-check.",
            ),
            technical=technical,
            error_code="KOKORO_NOT_AVAILABLE",
        )


class ProjectError(AppError):
    """Project loading/saving problems (directive section 12)."""

    default_title = "Project problem"

    def __init__(
        self,
        what_happened: str,
        why: str = "",
        actions: Optional[Iterable[str]] = None,
        technical: Optional[str] = None,
        error_code: str = "PROJECT_ERROR",
    ) -> None:
        super().__init__(what_happened, why, actions, technical, error_code=error_code)


class ProjectFormatError(ProjectError):
    """``project.json`` exists but is not a project this build understands."""

    def __init__(self, path: str, why: str, technical: Optional[str] = None) -> None:
        super().__init__(
            what_happened=f"The project file could not be opened:\n{path}",
            why=why,
            actions=(
                "Try opening the most recent backup instead (Project → Open Backup).",
                "If the file was edited by hand, check it against docs/PROJECT_FORMAT.md.",
                "The original file has been left untouched.",
            ),
            technical=technical,
            error_code="PROJECT_FORMAT_ERROR",
        )


class ProjectVersionError(ProjectFormatError):
    """``project.json`` uses a schema version this build cannot handle.

    Refusing is deliberate: partially reading a newer file and saving it back
    would silently delete the parts this build does not understand
    (directive section 4: "Unknown future schema versions must produce a clear
    compatibility error instead of corrupting the project").
    """

    def __init__(self, path: str, stored: int, supported: int, minimum: int = 1) -> None:
        newer = stored > supported
        why = (
            f"This project file uses schema version {stored}, but this build understands "
            f"versions {minimum}-{supported}."
        )
        actions = (
            (
                "Update Motion Graphics Studio to a newer version that supports this project.",
                "Or open the project on the computer that created it and export it in an older format.",
                "The file has NOT been changed - nothing was lost.",
            )
            if newer
            else (
                "This project file is older than this build can migrate.",
                "Open the most recent backup from the project's backups folder.",
                "The file has NOT been changed - nothing was lost.",
            )
        )
        super().__init__(
            path=path,
            why=why,
            technical=f"schema_version={stored}, supported={minimum}..{supported}",
        )
        self.actions = actions
        self.stored = int(stored)
        self.supported = int(supported)
        self.title = "This project needs a different version of the app"


class ProjectConflictError(ProjectError):
    """The project file changed outside this session (directive section 30).

    The application never resolves this silently: overwriting somebody else's
    newer save is exactly the kind of quiet data loss this build exists to
    prevent.
    """

    def __init__(self, path: str, loaded_at: str = "", changed_at: str = "") -> None:
        detail = "Loaded"
        if loaded_at:
            detail += f" here at {loaded_at}"
        if changed_at:
            detail += f"; changed on disk at {changed_at}"
        super().__init__(
            what_happened="The project changed outside this application.",
            why=(
                f"The file on disk is not the one this window loaded ({detail}). "
                "Another copy of the app, a sync tool or a manual edit changed it."
            ),
            actions=(
                "Reload to see the newer version (your unsaved edits here would be lost).",
                "Or keep editing this copy and use 'Save as' to store it under a new name.",
                "Nothing has been overwritten.",
            ),
            technical=f"path={path}",
            error_code="PROJECT_EXTERNAL_CHANGE",
        )


class ValidationError(AppError):
    """Data failed a validation rule (timeline, text fit, audio, output...)."""

    default_title = "Validation failed"

    def __init__(
        self,
        what_happened: str,
        why: str = "",
        actions: Optional[Iterable[str]] = None,
        technical: Optional[str] = None,
        error_code: str = "VALIDATION_FAILED",
    ) -> None:
        super().__init__(what_happened, why, actions, technical, error_code=error_code)


class JobError(AppError):
    """A background job failed (rendering, generation, encoding...)."""

    default_title = "The task could not be completed"

    def __init__(
        self,
        what_happened: str,
        why: str = "",
        actions: Optional[Iterable[str]] = None,
        technical: Optional[str] = None,
        error_code: str = "JOB_FAILED",
    ) -> None:
        super().__init__(what_happened, why, actions, technical, error_code=error_code)


class JobCancelled(Exception):
    """Raised inside a worker when the user cancelled the job.

    This is deliberately **not** an :class:`AppError`: cancellation is a normal
    outcome (section 18/42), not a failure, and the UI shows it differently.
    """


class InsufficientDiskSpaceError(AppError):
    """Not enough free space for the requested operation."""

    default_title = "Not enough free disk space"

    def __init__(self, needed_bytes: int, free_bytes: int, location: str = "") -> None:
        from .atomicio import human_size

        where = f" on '{location}'" if location else ""
        super().__init__(
            what_happened=f"This operation needs about {human_size(needed_bytes)} of free space{where}.",
            why=f"Only {human_size(free_bytes)} is currently available.",
            actions=(
                "Free up disk space, or move the data folder to a drive with more room (Settings → Folders).",
                "Clear cached files from Settings → Maintenance, then try again.",
            ),
            error_code="DISK_FULL",
        )


class DiskFullError(InsufficientDiskSpaceError):
    """Alias kept for readability at call sites."""


# --------------------------------------------------------------------------
# Conversion helpers
# --------------------------------------------------------------------------

def to_friendly(exc: BaseException, context: str = "") -> FriendlyError:
    """Convert *any* exception into a :class:`FriendlyError`.

    Known :class:`AppError` instances keep their authored text.  Unexpected
    exceptions get an honest "unexpected problem" description that still tells
    the user what to do (save, retry, send the log).
    """
    if isinstance(exc, AppError):
        error = exc.friendly()
        if context and context not in error.what_happened:
            return FriendlyError(
                title=error.title,
                what_happened=f"{context}\n{error.what_happened}",
                why=error.why,
                actions=error.actions,
                technical=error.technical,
                severity=error.severity,
                error_code=error.error_code,
            )
        return error

    if isinstance(exc, JobCancelled):
        return FriendlyError(
            title="Cancelled",
            what_happened=context or "The task was cancelled.",
            why="You cancelled the task before it finished.",
            actions=("Start it again when you are ready.",),
            severity=Severity.INFO,
            error_code="CANCELLED",
        )

    if isinstance(exc, FileNotFoundError):
        return FriendlyError(
            title="A file is missing",
            what_happened=f"{context}\nA required file could not be found: {exc.filename or exc}",
            why="The file may have been moved, renamed or deleted.",
            actions=(
                "Check that the file still exists.",
                "If it is a project asset, re-import it.",
            ),
            technical=repr(exc),
            error_code="FILE_NOT_FOUND",
        )

    if isinstance(exc, PermissionError):
        return FriendlyError(
            title="Windows blocked access to a file",
            what_happened=f"{context}\nAccess was denied: {exc.filename or exc}",
            why="Another program is using the file, or it is in a protected folder such as Program Files.",
            actions=(
                "Close any program that may be using the file and try again.",
                "If the app lives in Program Files, run it once as administrator or move the data folder.",
            ),
            technical=repr(exc),
            error_code="PERMISSION_DENIED",
        )

    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:  # ENOSPC
        return FriendlyError(
            title="Not enough free disk space",
            what_happened=context or "The disk is full.",
            why="The drive ran out of space while writing.",
            actions=("Free up space and try again.",),
            technical=repr(exc),
            error_code="DISK_FULL",
        )

    return FriendlyError(
        title="An unexpected problem occurred",
        what_happened=context or "The application hit an unexpected error.",
        why="This is a bug in this build, not something you did.",
        actions=(
            "Your project has not been changed - save it if you can, then try again.",
            "Restart the application if the problem repeats.",
            "Send the log file (path below) so the cause can be found.",
        ),
        technical=f"{type(exc).__name__}: {exc}",
        error_code="UNEXPECTED",
    )


def report(exc: BaseException, context: str = "", event: str = "ERROR") -> FriendlyError:
    """Log *exc* and return its friendly description (single call for callers)."""
    friendly = to_friendly(exc, context=context)
    level = logging.INFO if friendly.severity is Severity.INFO else logging.ERROR
    log_event(
        event,
        friendly.title,
        level=level,
        logger=logging_setup.get_logger("error"),
        code=friendly.error_code,
        detail=(friendly.technical or friendly.what_happened),
    )
    if level >= logging.ERROR:
        logging_setup.get_logger("error").debug("Failure context: %s", context, exc_info=exc)
    return friendly
