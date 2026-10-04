"""Reading and writing ``project.json`` safely.

Built on the Stage A atomic I/O layer - no second, parallel storage mechanism
(directive section 38).  Guarantees:

* **Atomic writes.**  A save either fully lands or not at all; a crash during a
  write leaves the previous valid file untouched.
* **Rolling backups.**  Before every save the current file is copied into
  ``backups/project_YYYY-MM-DD_NNN.json`` and old backups are pruned, so the
  folder never grows forever (section 12).
* **Autosave never touches ``project.json``.**  It writes
  ``autosave/project.autosave.json``, which is why a crash cannot corrupt the
  project (section 11).
* **Recovery is offered, never applied.**  A recovery file newer than the
  project produces a candidate for the UI to offer as Restore / Open original /
  Ignore (section 13).
* **Validation before writing.**  A project with validation *errors* is not
  saved; the user sees the list of problems instead (section 14).
* **A damaged file is preserved**, copied aside, and reported - never silently
  replaced.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.atomicio import FileWriteError, atomic_write_json, atomic_write_text, load_json
from ..core.errors import ProjectFormatError, ProjectVersionError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.version import APP_VERSION
from .layout import AUTOSAVE_FILENAME, IGNORED_SUFFIX, ProjectLayout
from .migrations import MigrationResult, migrate_project_data
from .model import Project, utc_now_iso
from .validation import ValidationReport, validate_project

LOGGER = get_logger("project.store")

DEFAULT_KEEP_BACKUPS = 10
DEFAULT_KEEP_AUTOSAVES = 10


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class ProjectSaveResult:
    """What a save did."""

    ok: bool
    path: Path
    project_version: int = 0
    backup: Optional[Path] = None
    validation: Optional[ValidationReport] = None
    error: Optional[str] = None
    bytes_written: int = 0
    saved_at: str = ""

    def summary(self) -> str:
        if not self.ok:
            if self.validation is not None and not self.validation.ok:
                return f"Not saved: {self.validation.summary()}"
            return f"Not saved: {self.error or 'unknown problem'}"
        text = f"Saved version {self.project_version} ({self.bytes_written} bytes)"
        if self.backup is not None:
            text += f", previous version kept as {self.backup.name}"
        return text


@dataclass
class ProjectLoadResult:
    """What a load found."""

    ok: bool
    path: Path
    project: Optional[Project] = None
    #: ``file`` (normal), ``recovery`` (autosave) or ``backup``.
    source: str = "file"
    migration: Optional[MigrationResult] = None
    validation: Optional[ValidationReport] = None
    error: Optional[str] = None
    friendly: Optional[object] = None
    quarantined: Optional[Path] = None

    @property
    def migrated(self) -> bool:
        return self.migration is not None and self.migration.migrated

    def summary(self) -> str:
        if not self.ok:
            return f"Could not open the project: {self.error}"
        parts = [f"Opened {self.path.name}"]
        if self.migrated and self.migration is not None:
            parts.append(f"migrated from schema {self.migration.from_version}")
        if self.validation is not None and self.validation.count:
            parts.append(self.validation.summary())
        return " - ".join(parts)


@dataclass
class RecoveryCandidate:
    """A recovery file the user can choose to restore (never applied silently)."""

    kind: str                    # "autosave" | "backup" | "orphan"
    path: Path
    modified_at: float
    project_name: str = ""
    size_bytes: int = 0
    reason: str = ""
    #: Set when the project file itself is missing, so restoring is the only way in.
    project_file_missing: bool = False

    def headline(self) -> str:
        return "Recovered project available."

    def detail(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.modified_at)) if self.modified_at else "unknown time"
        label = {"autosave": "an autosave", "backup": "a backup", "orphan": "a recovery file"}.get(self.kind, "a recovery file")
        extra = " The project file itself is missing." if self.project_file_missing else ""
        return (
            f"{label.capitalize()} of '{self.project_name or 'the project'}' was written at {when} "
            f"and is newer than the saved project.{extra}\n\n"
            "Nothing has been changed yet. Choose what to do:"
        )


@dataclass
class ProjectFingerprint:
    """Identity of a project file on disk, for external-change detection."""

    mtime: float = 0.0
    size: int = 0
    project_version: int = 0

    def same_file(self, other: "ProjectFingerprint") -> bool:
        return abs(self.mtime - other.mtime) < 1e-6 and self.size == other.size


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------

def fingerprint(path: Path, project_version: int = 0) -> Optional[ProjectFingerprint]:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return ProjectFingerprint(mtime=stat.st_mtime, size=stat.st_size, project_version=project_version)


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def next_backup_path(backups_dir: Path, when: Optional[time.struct_time] = None) -> Path:
    """``backups/project_2026-10-05_001.json`` - the naming from section 12."""
    stamp = time.strftime("%Y-%m-%d", when or time.localtime())
    backups_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        candidate = backups_dir / f"project_{stamp}_{index:03d}.json"
        if not candidate.exists():
            return candidate
        index += 1


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Delete the oldest backups, keeping the newest *keep* (never grows forever)."""
    if keep <= 0 or not backups_dir.is_dir():
        return []
    files = sorted(
        (item for item in backups_dir.iterdir() if item.is_file() and item.suffix == ".json"),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    removed: list[Path] = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            continue
    return removed


def backup_project_file(
    project_file: Path,
    backups_dir: Path,
    keep: int = DEFAULT_KEEP_BACKUPS,
) -> Optional[Path]:
    """Copy the current project into the rolling backup chain.

    Returns the backup path, or ``None`` when there is nothing to back up.  A
    failed backup is logged but never blocks a save.
    """
    project_file = Path(project_file)
    if not project_file.exists():
        return None
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        target = next_backup_path(backups_dir)
        shutil.copy2(project_file, target)
    except OSError as exc:
        log_event(
            Event.WARNING,
            "A project backup could not be written",
            level=logging.WARNING,
            logger=LOGGER,
            path=str(project_file),
            reason=str(exc),
        )
        return None
    removed = prune_backups(backups_dir, keep)
    log_event(
        Event.PROJECT_BACKUP_CREATED,
        "Project backup created",
        logger=LOGGER,
        backup=str(target),
        pruned=len(removed),
        keep=keep,
    )
    return target


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

class ProjectStore:
    """All ``project.json`` I/O for the application (GUI and CLI alike)."""

    def __init__(self, keep_backups: int = DEFAULT_KEEP_BACKUPS, keep_autosaves: int = DEFAULT_KEEP_AUTOSAVES) -> None:
        self.keep_backups = max(1, int(keep_backups))
        self.keep_autosaves = max(1, int(keep_autosaves))

    # -- load --------------------------------------------------------------

    def load(
        self,
        path: Path,
        *,
        check_files: bool = True,
        validate: bool = True,
    ) -> ProjectLoadResult:
        """Read a project file: parse, version-check, migrate, validate.

        Never raises for a damaged file - the failure is described instead, and
        the damaged file is copied into ``backups/`` so nothing is lost.
        """
        path = Path(path)
        layout = ProjectLayout.from_project_file(path)

        result = load_json(path, default=None, quarantine_dir=layout.backups_dir)
        if not result.exists:
            return ProjectLoadResult(ok=False, path=path, error="The project file does not exist.")
        if not result.ok or not isinstance(result.data, dict):
            log_event(
                Event.PROJECT_OPEN,
                "Project file could not be read",
                level=logging.ERROR,
                logger=LOGGER,
                path=str(path),
                reason=result.error,
                quarantined=str(result.corrupted_copy) if result.corrupted_copy else None,
            )
            error = ProjectFormatError(
                path=str(path),
                why=f"The file could not be read as JSON: {result.error}",
                technical=result.error,
            )
            return ProjectLoadResult(
                ok=False,
                path=path,
                error=result.error,
                friendly=error.friendly(),
                quarantined=result.corrupted_copy,
            )

        try:
            migration = migrate_project_data(result.data, path=str(path))
        except ProjectVersionError as exc:
            return ProjectLoadResult(ok=False, path=path, error=str(exc), friendly=exc.friendly())

        project = Project.from_dict(migration.data)
        report = validate_project(project, project_dir=layout.root, check_files=check_files) if validate else None

        log_event(
            Event.PROJECT_OPEN,
            "Project opened",
            logger=LOGGER,
            path=str(path),
            name=project.project.name,
            schema=migration.to_version,
            migrated=migration.migrated,
            issues=report.count if report is not None else 0,
        )
        return ProjectLoadResult(
            ok=True,
            path=path,
            project=project,
            source="file",
            migration=migration,
            validation=report,
        )

    # -- save --------------------------------------------------------------

    def save(
        self,
        project: Project,
        layout: ProjectLayout,
        *,
        backup: bool = True,
        write_script_file: bool = True,
        clear_autosave: bool = True,
        bump_version: bool = True,
        reason: str = "",
    ) -> ProjectSaveResult:
        """Validate, back up, then write atomically.

        A project with validation errors is **not** written: the report is
        returned so the UI can show every problem at once.
        """
        report = validate_project(project, project_dir=layout.root, check_files=False)
        if not report.ok:
            log_event(
                Event.PROJECT_SAVE_FAILED,
                "Save refused: the project has validation errors",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(layout.project_file),
                errors=len(report.errors),
            )
            return ProjectSaveResult(ok=False, path=layout.project_file, validation=report)

        project.touch()
        # A brand new project is written as version 1; later saves increment.
        version = project.bump_version() if bump_version else int(project.project.project_version or 1)
        project.project.project_version = version
        project.application_version = APP_VERSION
        payload = project.to_dict()

        layout.ensure()
        previous = None
        if backup:
            previous = backup_project_file(layout.project_file, layout.backups_dir, keep=self.keep_backups)
            if previous is not None:
                # Written before the project file and removed after it: if this
                # process dies in between, startup knows the save was cut short
                # without having to compare file timestamps (which are coarse on
                # Windows and can tie).
                self._mark_save_pending(layout, previous)

        try:
            atomic_write_json(layout.project_file, payload)
            if write_script_file:
                self._write_script_file(project, layout)
        except (FileWriteError, OSError) as exc:
            # OSError as well as FileWriteError: on Windows a locked file
            # (OneDrive, antivirus, an open editor) can raise PermissionError
            # straight out of os.replace.  Either way the answer to the user is
            # "not saved", never a traceback.
            self._clear_save_pending(layout)
            log_event(
                Event.PROJECT_SAVE_FAILED,
                "Project could not be written",
                level=logging.ERROR,
                logger=LOGGER,
                path=str(layout.project_file),
                reason=str(exc),
            )
            return ProjectSaveResult(ok=False, path=layout.project_file, error=str(exc), validation=report)

        self._clear_save_pending(layout)
        if clear_autosave:
            self._clear_autosave(layout)

        size = layout.project_file.stat().st_size if layout.project_file.exists() else 0
        log_event(
            Event.PROJECT_SAVE,
            f"Project saved{f' ({reason})' if reason else ''}",
            logger=LOGGER,
            path=str(layout.project_file),
            name=project.project.name,
            version=version,
            bytes=size,
            backup=str(previous) if previous else None,
        )
        return ProjectSaveResult(
            ok=True,
            path=layout.project_file,
            project_version=version,
            backup=previous,
            validation=report,
            bytes_written=size,
            saved_at=project.project.modified_at,
        )

    # -- interrupted-save marker -------------------------------------------

    @staticmethod
    def _pending_marker(layout: ProjectLayout) -> Path:
        return layout.backups_dir / ".pending-save.json"

    def _mark_save_pending(self, layout: ProjectLayout, backup: Path) -> None:
        try:
            atomic_write_json(self._pending_marker(layout), {"backup": backup.name, "at": utc_now_iso()})
        except (FileWriteError, OSError):
            pass  # a missing marker only means a less precise recovery hint

    def _clear_save_pending(self, layout: ProjectLayout) -> None:
        try:
            marker = self._pending_marker(layout)
            if marker.exists():
                marker.unlink()
        except OSError:
            pass

    def pending_backup(self, layout: ProjectLayout) -> Optional[Path]:
        """The backup of a save that never finished, if there is one."""
        marker = self._pending_marker(layout)
        if not marker.exists():
            return None
        data = load_json(marker, default=None).data
        name = data.get("backup") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name:
            return None
        candidate = layout.backups_dir / name
        return candidate if candidate.exists() else None

    def _write_script_file(self, project: Project, layout: ProjectLayout) -> None:
        """Keep ``script.txt`` in step with the script (never reformatting it).

        The file is only written when the project actually has script text, so
        an empty project does not gain an empty file the user did not ask for.
        """
        text = project.script.source_text
        if not text:
            if layout.script_file.exists():
                try:
                    layout.script_file.unlink()
                except OSError:
                    pass
            return
        # Byte-exact: the file is a copy of the stored text, never reformatted
        # and never given a trailing newline the user did not type.
        atomic_write_text(layout.script_file, text)

    # -- autosave ----------------------------------------------------------

    def autosave(self, project: Project, layout: ProjectLayout) -> Optional[Path]:
        """Write the recovery copy.  ``project.json`` is never touched here.

        Returns the autosave path, or ``None`` when nothing was written (for
        example because the disk refused the write - which must never be
        reported as a successful save).
        """
        report = validate_project(project, project_dir=layout.root, check_files=False)
        if not report.ok:
            log_event(
                Event.PROJECT_AUTOSAVE,
                "Autosave skipped: the project has validation errors",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(layout.project_file),
                errors=len(report.errors),
            )
            return None

        payload = project.to_dict()
        payload["project"]["modified_at"] = utc_now_iso()
        try:
            layout.autosave_dir.mkdir(parents=True, exist_ok=True)
            path = atomic_write_json(layout.autosave_file, payload)
            self._rotate_autosaves(layout)
        except (FileWriteError, OSError) as exc:
            log_event(
                Event.WARNING,
                "Autosave could not be written",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(layout.autosave_file),
                reason=str(exc),
            )
            return None

        log_event(
            Event.PROJECT_AUTOSAVE,
            "Autosave written",
            logger=LOGGER,
            path=str(path),
            name=project.project.name,
        )
        return path

    def write_autosave_payload(self, payload: dict, layout: ProjectLayout) -> Optional[Path]:
        """Write an already-serialised snapshot as the recovery copy.

        Used by the GUI, which builds the snapshot on the interface thread (a
        cheap dict copy) and hands the actual disk write to a background job, so
        autosaving never blocks the window (directive section 37).
        """
        try:
            layout.autosave_dir.mkdir(parents=True, exist_ok=True)
            path = atomic_write_json(layout.autosave_file, payload)
            self._rotate_autosaves(layout)
        except (FileWriteError, OSError) as exc:
            log_event(
                Event.WARNING,
                "Autosave could not be written",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(layout.autosave_file),
                reason=str(exc),
            )
            return None
        log_event(Event.PROJECT_AUTOSAVE, "Autosave written", logger=LOGGER, path=str(path))
        return path

    def _rotate_autosaves(self, layout: ProjectLayout) -> None:
        """Keep a bounded history of autosaves next to the live one."""
        history = [item for item in layout.autosave_files() if item.name != AUTOSAVE_FILENAME]
        keep = max(0, self.keep_autosaves - 1)
        for old in history[keep:]:
            try:
                old.unlink()
            except OSError:
                continue

    def snapshot_autosave(self, layout: ProjectLayout) -> Optional[Path]:
        """Keep the current autosave as a timestamped copy (crash forensics)."""
        if not layout.autosave_file.exists():
            return None
        target = layout.autosave_dir / f"project.{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            shutil.copy2(layout.autosave_file, target)
        except OSError:
            return None
        self._rotate_autosaves(layout)
        return target

    def _clear_autosave(self, layout: ProjectLayout) -> None:
        """Drop the recovery file once ``project.json`` is newer again."""
        try:
            if layout.autosave_file.exists():
                layout.autosave_file.unlink()
        except OSError as exc:
            log_event(
                Event.WARNING,
                "The autosave file could not be removed after a save",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(layout.autosave_file),
                reason=str(exc),
            )

    # -- recovery ----------------------------------------------------------

    def detect_recovery(self, layout: ProjectLayout) -> Optional[RecoveryCandidate]:
        """Find recovery data that holds work the project file does not.

        Deliberately **not** based on comparing file timestamps: those are coarse
        on Windows and two writes can share one tick.  Instead:

        * an autosave whose content differs from the project holds unsaved work;
        * a pending-save marker means the last save was interrupted, so the
          backup it names is the last known good file;
        * a missing project file with a backup present is recoverable.

        Called at startup.  It only reports; the user decides (section 13).
        """
        missing = not layout.project_file.exists()

        for path in layout.autosave_files():
            if not self._is_readable(path):
                continue  # damaged beyond use - offering it would only fail again
            if self._same_content_as_project(path, layout):
                continue  # nothing new in it - offering it would only be noise
            return self._candidate("autosave", path, layout, missing, "The autosave holds changes the saved project does not have.")

        pending = self.pending_backup(layout)
        if pending is not None:
            return self._candidate(
                "backup", pending, layout, missing, "The last save was interrupted, so this backup is the newest complete file."
            )

        if missing:
            for path in layout.backup_files():
                return self._candidate("backup", path, layout, True, "The project file is missing, but a backup exists.")

        return None

    def _candidate(self, kind: str, path: Path, layout: ProjectLayout, missing: bool, reason: str) -> RecoveryCandidate:
        candidate = RecoveryCandidate(
            kind=kind,
            path=path,
            modified_at=_safe_mtime(path),
            project_name=self._peek_name(path),
            size_bytes=_safe_size(path),
            project_file_missing=missing,
            reason=reason,
        )
        log_event(
            Event.PROJECT_RECOVERY_FOUND,
            "Recovery data found",
            logger=LOGGER,
            kind=kind,
            path=str(candidate.path),
            project=str(layout.project_file),
        )
        return candidate

    @staticmethod
    def _is_readable(path: Path) -> bool:
        """Can this recovery file be parsed at all?"""
        result = load_json(path, default=None)
        return result.ok and isinstance(result.data, dict)

    def _same_content_as_project(self, candidate: Path, layout: ProjectLayout) -> bool:
        """True when a recovery file adds nothing to the saved project.

        Timestamps and the save counter are ignored, so an autosave taken a
        second before a clean save is correctly treated as identical.
        """
        if not layout.project_file.exists():
            return False
        current = load_json(layout.project_file, default=None)
        other = load_json(candidate, default=None)
        if not (current.ok and other.ok):
            return False
        try:
            return Project.from_dict(current.data).content_hash() == Project.from_dict(other.data).content_hash()
        except Exception:  # noqa: BLE001 - a damaged file is never "identical"
            return False

    def _peek_name(self, path: Path) -> str:
        """Read just the project name for the recovery prompt."""
        result = load_json(path, default=None)
        if result.ok and isinstance(result.data, dict):
            meta = result.data.get("project")
            if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                return meta["name"]
        return ""

    def apply_recovery(self, candidate: RecoveryCandidate, layout: ProjectLayout) -> ProjectLoadResult:
        """Restore a recovery file over ``project.json``.

        The current file is backed up first, so choosing Restore can never
        destroy data (section 13).
        """
        loaded = self.load(candidate.path, check_files=False)
        if not loaded.ok or loaded.project is None:
            return loaded

        layout.ensure()
        backup_project_file(layout.project_file, layout.backups_dir, keep=self.keep_backups)
        try:
            atomic_write_json(layout.project_file, loaded.project.to_dict())
        except FileWriteError as exc:
            return ProjectLoadResult(ok=False, path=layout.project_file, error=str(exc))

        self._retire_recovery_file(candidate)
        log_event(
            Event.PROJECT_RECOVERY,
            "Recovery data restored into project.json",
            logger=LOGGER,
            source=str(candidate.path),
            target=str(layout.project_file),
            name=loaded.project.project.name,
        )
        loaded.path = layout.project_file
        loaded.source = "recovery"
        return loaded

    def ignore_recovery(self, candidate: RecoveryCandidate) -> bool:
        """Mark a recovery file as dealt with so it is not offered again.

        The file is renamed, never deleted: the user may change their mind.
        """
        target = candidate.path.with_name(candidate.path.stem + IGNORED_SUFFIX)
        try:
            candidate.path.rename(target)
        except OSError as exc:
            log_event(
                Event.WARNING,
                "The recovery file could not be set aside",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(candidate.path),
                reason=str(exc),
            )
            return False
        log_event(Event.PROJECT_RECOVERY, "Recovery data ignored by the user", logger=LOGGER, path=str(target))
        return True

    def _retire_recovery_file(self, candidate: RecoveryCandidate) -> None:
        """After a restore, keep the source as history instead of deleting it."""
        target = candidate.path.with_name(candidate.path.stem + time.strftime(".restored-%Y%m%d-%H%M%S.json"))
        try:
            candidate.path.rename(target)
        except OSError:
            return
        if candidate.path.parent == ProjectLayout(candidate.path.parent.parent).autosave_dir:
            self._rotate_autosaves(ProjectLayout(candidate.path.parent.parent))

    # -- backups -----------------------------------------------------------

    def list_backups(self, layout: ProjectLayout) -> list[Path]:
        return layout.backup_files()

    def restore_backup(self, path: Path, layout: ProjectLayout) -> ProjectLoadResult:
        """Open a specific backup as the current project (user's explicit choice)."""
        candidate = RecoveryCandidate(kind="backup", path=Path(path), modified_at=_safe_mtime(path))
        return self.apply_recovery(candidate, layout)

    # -- external changes --------------------------------------------------

    @staticmethod
    def changed_outside(path: Path, expected: Optional[ProjectFingerprint]) -> bool:
        """True when the file on disk is no longer the one this session loaded."""
        current = fingerprint(Path(path))
        if expected is None:
            return False
        if current is None:
            return True
        return not current.same_file(expected)


def _safe_mtime(path: Path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _safe_size(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


__all__ = [
    "DEFAULT_KEEP_AUTOSAVES",
    "DEFAULT_KEEP_BACKUPS",
    "ProjectFingerprint",
    "ProjectLoadResult",
    "ProjectSaveResult",
    "ProjectStore",
    "RecoveryCandidate",
    "backup_project_file",
    "fingerprint",
    "next_backup_path",
    "prune_backups",
]
