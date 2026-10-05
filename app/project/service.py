"""The project service: every project operation, in one place.

Directive sections 33 and 34: the GUI calls this, the CLI calls this, the tests
call this.  No JSON handling, backup logic, migration, validation or path
safety lives in a widget.

The service is Qt-free on purpose - it takes an optional ``notifier`` callable
so the GUI can turn events into signals, while the CLI and the tests can ignore
them or print them.

Session model
-------------
At most one project is open.  The session remembers the file fingerprint it
loaded, so a save can detect that the file changed underneath
(:class:`ProjectConflictError`) instead of overwriting newer data.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.errors import ProjectConflictError, ProjectError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.paths import AppPaths, is_within, safe_filename, unique_path
from ..core.settings import Settings
from . import assets as asset_tools
from .assets import AssetCheck, ImportReport, verify_assets
from .history import ProjectHistory
from .layout import PROJECT_FILENAME, ProjectLayout, find_project_file, looks_like_project_folder
from .lock import ProjectLock
from .model import Project, SceneSpec, build_project, utc_now_iso
from .presets import PROJECT_TEMPLATES, project_template, resolve_quality
from .recent import (
    CHANNELS_FILENAME,
    RECENT_FILENAME,
    ChannelStore,
    RecentProject,
    RecentProjectsStore,
    entry_from_project,
)
from .store import ProjectFingerprint, ProjectSaveResult, ProjectStore, RecoveryCandidate, fingerprint
from .validation import ValidationReport, validate_project

LOGGER = get_logger("project.service")

Notifier = Callable[[str, dict], None]


# --------------------------------------------------------------------------
# Requests and sessions
# --------------------------------------------------------------------------

@dataclass
class ScriptImportResult:
    """What a script import produced."""

    ok: bool = False
    text: str = ""
    encoding: str = ""
    error: str = ""
    notes: list = field(default_factory=list)


@dataclass
class ScriptExportResult:
    """What a script export produced."""

    ok: bool = False
    path: Optional[Path] = None
    bytes_written: int = 0
    error: str = ""


def _recorded_model_version(project: Project) -> str:
    """The model version recorded on the newest narration track, if any."""
    newest = max(
        (track for track in project.narration.tracks if track.model_version),
        key=lambda track: track.generated_at or "",
        default=None,
    )
    return newest.model_version if newest is not None else ""


@dataclass
class NarrationSettingsSnapshot:
    """The narration choices stored in the project, plus its current state."""

    settings: object = None
    status: str = "not_generated"
    mode: str = "full_script"


@dataclass
class CreateRequest:
    """Everything the New Project wizard collects."""

    name: str
    description: str = ""
    channel_id: str = ""
    channel_name: str = ""
    template: str = "blank"
    #: Zero/empty means "use the template's value", which is how the wizard
    #: distinguishes "the user chose this" from "the user did not change it".
    width: int = 0
    height: int = 0
    fps: int = 0
    #: A preset key ("high") or an already resolved settings dict.
    quality: object = ""
    background: str = ""
    accent: str = ""
    heading_font: str = "DejaVu Sans"
    body_font: str = "DejaVu Sans"
    theme_id: str = "clean-dark"
    subtitles_enabled: bool = False
    subtitle_font_size: int = 44
    filename_template: str = "{name}_{seq}"
    language: str = "en-us"
    gender: str = ""
    voice: str = ""
    script_note: str = ""
    #: Explicit destination folder; defaults to ``projects/<safe name>``.
    folder: Optional[Path] = None


@dataclass
class ProjectSession:
    """The project that is open right now."""

    project: Project
    layout: ProjectLayout
    fingerprint: Optional[ProjectFingerprint] = None
    history: ProjectHistory = field(default_factory=ProjectHistory)
    lock: Optional[ProjectLock] = None
    saved_hash: str = ""
    opened_at: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return self.project.content_hash() != self.saved_hash

    @property
    def name(self) -> str:
        return self.project.project.name


# --------------------------------------------------------------------------
# Name checks
# --------------------------------------------------------------------------

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_project_name(name: str) -> list[str]:
    """Human readable problems with a project name (empty list = fine)."""
    problems: list[str] = []
    text = (name or "").strip()
    if not text:
        problems.append("The project needs a name.")
        return problems
    if len(text) > 120:
        problems.append("The name is longer than 120 characters.")
    if text.strip(" .") == "":
        problems.append("The name cannot be only dots or spaces.")
    if text.upper() in WINDOWS_RESERVED:
        problems.append(f"'{text}' is a reserved Windows name and cannot be used as a folder.")
    if any(ord(ch) < 32 for ch in text):
        problems.append("The name contains control characters.")
    return problems


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class ProjectService:
    """Create, open, edit, save and recover projects."""

    def __init__(
        self,
        paths: AppPaths,
        settings: Settings,
        *,
        store: Optional[ProjectStore] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.store = store or ProjectStore(
            keep_backups=settings.autosave.keep_backups,
            keep_autosaves=settings.autosave.keep_autosaves,
        )
        self.notifier = notifier
        self.recents = RecentProjectsStore(paths.config_dir / RECENT_FILENAME)
        self.channels = ChannelStore(paths.config_dir / CHANNELS_FILENAME)
        self.session: Optional[ProjectSession] = None

    # -- helpers -----------------------------------------------------------

    def _notify(self, event: str, **fields) -> None:
        log_event(event, fields.pop("message", ""), logger=LOGGER, **fields)
        if self.notifier is not None:
            try:
                self.notifier(event, fields)
            except Exception:  # noqa: BLE001 - a listener must never break a save
                LOGGER.exception("A project event listener failed")

    @property
    def current(self) -> Optional[Project]:
        return self.session.project if self.session else None

    @property
    def current_layout(self) -> Optional[ProjectLayout]:
        return self.session.layout if self.session else None

    def is_open(self) -> bool:
        return self.session is not None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_project(self, request: CreateRequest, *, open_after: bool = True) -> Project:
        """Create the folder, the model and the first ``project.json``.

        A failure leaves nothing behind: the folder is removed if this call
        created it, and the project is never added to the recent list
        (directive section 8).
        """
        name = (request.name or "").strip()
        problems = validate_project_name(name)
        if problems:
            raise ProjectError(
                what_happened="The project could not be created.",
                why=" ".join(problems),
                actions=("Choose a different name.", "Names may contain letters, numbers, spaces and dashes."),
                error_code="PROJECT_NAME_INVALID",
            )

        template = project_template(request.template) or PROJECT_TEMPLATES[0]
        folder = self._creation_folder(name, request.folder)
        created_here = not folder.exists()

        layout = ProjectLayout(folder)
        try:
            layout.ensure()
        except OSError as exc:
            raise ProjectError(
                what_happened="The project folder could not be created.",
                why=str(exc),
                actions=(
                    "Check that the projects folder is writable.",
                    f"Projects folder: {self.paths.projects_dir}",
                ),
                technical=str(exc),
                error_code="PROJECT_FOLDER_FAILED",
            ) from exc

        try:
            project = build_project(
                name=name,
                description=request.description,
                channel_id=request.channel_id,
                channel_name=request.channel_name or template.label,
                template_key=template.key,
                width=int(request.width or template.width),
                height=int(request.height or template.height),
                fps=int(request.fps or template.fps),
                quality=self._resolved_quality(request.quality, template.quality),
                background=request.background or template.background,
                accent=request.accent or template.accent,
                heading_font=request.heading_font or template.heading_font,
                body_font=request.body_font or template.body_font,
                theme_id=request.theme_id or template.theme_id,
                subtitles_enabled=bool(request.subtitles_enabled or template.subtitles_enabled),
                subtitle_font_size=int(request.subtitle_font_size or template.subtitle_font_size),
                filename_template=request.filename_template or template.filename_template,
                language=request.language or "en-us",
                gender=request.gender or "",
                voice=request.voice or "",
                script_note=request.script_note or template.script_note,
            )
            project.ensure_ids()
            result = self.store.save(project, layout, backup=False, bump_version=False, reason="created")
            if not result.ok:
                raise ProjectError(
                    what_happened="The project file could not be written.",
                    why=result.error or (result.validation.summary() if result.validation else "unknown problem"),
                    actions=("Check the free disk space.", f"Folder: {folder}"),
                    technical=result.error,
                    error_code="PROJECT_WRITE_FAILED",
                )
        except Exception:
            if created_here:
                self._remove_folder_quietly(folder)
            raise

        self._notify(
            Event.PROJECT_CREATE,
            message="Project created",
            name=name,
            path=str(folder),
            template=template.key,
        )
        if open_after:
            self._adopt(project, layout, lock=self._acquire_lock(layout), notes=[f"Created from the '{template.label}' template."])
        return project

    def _creation_folder(self, name: str, explicit: Optional[Path]) -> Path:
        if explicit is not None:
            folder = Path(explicit)
            if looks_like_project_folder(folder):
                raise ProjectError(
                    what_happened="That folder already contains a project.",
                    why=f"{folder} has a project.json already.",
                    actions=("Open the existing project instead.", "Or choose a different folder."),
                    error_code="PROJECT_EXISTS",
                )
            return folder

        folder_name = safe_filename(name, fallback="project")
        folder = self.paths.projects_dir / folder_name
        if folder.exists():
            raise ProjectError(
                what_happened=f"A project folder called '{folder_name}' already exists.",
                why="Two projects cannot share one folder.",
                actions=(
                    "Choose a different name.",
                    f"Or open the existing project: {folder}",
                ),
                error_code="PROJECT_EXISTS",
            )
        return folder

    def _resolved_quality(self, requested: object, fallback: str) -> dict:
        """Accept a preset key ("high") or an already resolved settings dict."""
        if isinstance(requested, dict):
            resolved = dict(requested)
            resolved.setdefault("quality_preset", "custom")
            return resolved
        return resolve_quality(str(requested or fallback))

    def _remove_folder_quietly(self, folder: Path) -> None:
        try:
            if folder.is_dir() and is_within(folder, self.paths.data_root):
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open_project(self, path: Path, *, lock: bool = True) -> Project:
        """Open a project folder or a ``project.json`` path."""
        target = find_project_file(Path(path))
        if target is None:
            raise ProjectError(
                what_happened="That folder is not a project.",
                why=f"No project.json was found in '{path}'.",
                actions=(
                    "Choose the project folder (the one that contains project.json).",
                    "Or create a new project.",
                ),
                error_code="PROJECT_NOT_FOUND",
            )

        if self.session is not None and Path(self.session.layout.root) == target.parent:
            return self.session.project

        loaded = self.store.load(target, check_files=True)
        if not loaded.ok or loaded.project is None:
            self._notify(
                Event.PROJECT_OPEN,
                message="A project could not be opened",
                path=str(target),
                reason=loaded.error,
            )
            raise ProjectError(
                what_happened="The project could not be opened.",
                why=loaded.error or "The file could not be read.",
                actions=(
                    "Try the most recent backup in the project's backups folder.",
                    "The original file has not been changed.",
                ),
                technical=loaded.error,
                error_code="PROJECT_OPEN_FAILED",
            )

        layout = ProjectLayout.from_project_file(target)
        if lock:
            project_lock = ProjectLock(layout, data_root=str(self.paths.data_root))
            outcome = project_lock.acquire()
            if not outcome.ok:
                raise ProjectError(
                    what_happened="The project is already open.",
                    why=outcome.reason,
                    actions=(
                        "Close the other window first.",
                        "Or open the project read-only from the project browser.",
                    ),
                    error_code="PROJECT_LOCKED",
                )
        else:
            project_lock = None

        notes: list[str] = []
        if loaded.validation is not None:
            notes.extend(issue.to_line() for issue in loaded.validation.warnings)
        if loaded.migrated and loaded.migration is not None:
            notes.insert(0, loaded.migration.summary())
        missing = asset_tools.update_missing_flags(loaded.project, layout)
        if missing:
            notes.insert(0, f"{len(missing)} asset(s) could not be found. Relink or ignore them on the Assets page.")

        self._adopt(loaded.project, layout, lock=project_lock, notes=notes)
        self._notify(
            Event.PROJECT_OPEN,
            message="Project opened",
            name=loaded.project.project.name,
            path=str(target),
            migrated=loaded.migrated,
            missing_assets=len(missing),
        )
        return loaded.project

    def _acquire_lock(self, layout: ProjectLayout) -> Optional[ProjectLock]:
        """Lock a project this session owns (best effort, never fatal)."""
        project_lock = ProjectLock(layout, data_root=str(self.paths.data_root))
        project_lock.acquire()
        return project_lock

    def _adopt(
        self,
        project: Project,
        layout: ProjectLayout,
        *,
        lock: Optional[ProjectLock] = None,
        notes: Optional[list[str]] = None,
    ) -> None:
        if self.session is not None:
            self.close_project(save=False)
        project.ensure_ids()
        session = ProjectSession(
            project=project,
            layout=layout,
            fingerprint=fingerprint(layout.project_file, project.project.project_version),
            lock=lock,
            saved_hash=project.content_hash(),
            opened_at=utc_now_iso(),
            notes=notes or [],
        )
        self.session = session
        self.recents.add(entry_from_project(project, layout.project_file))

    def close_project(self, *, save: bool = False) -> Optional[ProjectSaveResult]:
        """Close the open project.  ``save=True`` writes it first."""
        if self.session is None:
            return None
        result = self.save(reason="closing") if save else None
        session, self.session = self.session, None
        if session.lock is not None:
            session.lock.release()
        self._notify(Event.PROJECT_CLOSED, message="Project closed", name=session.name)
        return result

    # ------------------------------------------------------------------
    # Editing (with undo)
    # ------------------------------------------------------------------

    def edit(self, label: str, mutate: Callable[[Project], None]) -> Project:
        """Apply one undoable edit through the service.

        Every mutation goes through here so undo, dirty tracking and logging
        behave the same no matter which page made the change.
        """
        session = self._require_session()
        session.history.record(label, session.project)
        mutate(session.project)
        session.project.touch()
        self._notify(Event.USER_ACTION, message=f"Edit: {label}", project=session.name)
        return session.project

    def set_script_text(self, text: str) -> Project:
        def apply(project: Project) -> None:
            project.script.source_text = text
            project.script.recompute_estimate()

        return self.edit("Edit script", apply)

    # -- script import / export (Stage C, sections 18-19) -------------------

    def import_script_file(self, path: Path) -> "ScriptImportResult":
        """Read a script file into the project.

        The text is stored **exactly as written**; nothing is reformatted and the
        original file is never touched (sections 12 and 19).
        """
        from app.script.io import import_script

        result = import_script(Path(path))
        if not result.ok:
            return ScriptImportResult(ok=False, error=result.error or "The file could not be read.")
        self.set_script_text(result.text)
        return ScriptImportResult(
            ok=True,
            text=result.text,
            encoding=result.encoding,
            notes=list(result.notes),
        )

    def export_script_file(self, path: Path, *, target: str = "txt", title: str = "") -> "ScriptExportResult":
        """Write the current script out.  An existing file is never replaced."""
        from app.script.io import export_script

        session = self._require_session()
        result = export_script(Path(path), session.project.script.source_text,
                               target=target, title=title or session.project.project.name)
        return ScriptExportResult(ok=result.ok, path=result.path,
                                  bytes_written=result.bytes_written, error=result.error)

    # -- narration (Stage C, sections 24-27, 41-43) ------------------------

    def set_voice_settings(self, *,
                           voice: Optional[str] = None,
                           language: Optional[str] = None,
                           gender: Optional[str] = None,
                           speed: Optional[float] = None,
                           volume: Optional[float] = None) -> Project:
        """Store the user's narration choices (one undoable edit)."""
        def apply(project: Project) -> None:
            if voice is not None:
                project.voice.voice = voice
            if language is not None:
                project.voice.language = language
            if gender is not None:
                project.voice.gender = gender
            if speed is not None:
                project.voice.speed = float(speed)
            if volume is not None:
                project.voice.volume = float(volume)

        return self.edit("Change narration voice settings", apply)

    def set_narration_mode(self, mode: str) -> Project:
        """Switch between full-script and per-section narration."""
        from .model import NARRATION_MODES

        if mode not in NARRATION_MODES:
            raise ProjectError(f"Unknown narration mode '{mode}'.")

        def apply(project: Project) -> None:
            project.narration.mode = mode

        return self.edit("Change narration mode", apply)

    def narration_settings(self, model_version: str = "") -> "NarrationSettingsSnapshot":
        """The stored narration choices, for the interface and for staleness.

        ``model_version`` matters for staleness: the hash recorded on a track
        includes the model that produced it.  Callers that have probed the engine
        pass the version they found; without one this falls back to the version
        recorded on the newest track, i.e. "assume the model did not change".
        Passing an empty string instead would make every track look stale.
        """
        from app.tts.narration import NarrationSettings

        session = self._require_session()
        project = session.project
        resolved_model = (model_version or "").strip() or _recorded_model_version(project)
        return NarrationSettingsSnapshot(
            settings=NarrationSettings(
                voice=project.voice.voice,
                language=project.voice.language,
                speed=float(project.voice.speed or 1.0),
                volume=float(project.voice.volume or 1.0),
                sample_rate=int(project.voice.sample_rate or 24000),
                model_version=resolved_model,
                preprocessing=dict(project.narration.preprocessing or {}),
            ),
            status=project.narration.status,
            mode=project.narration.mode,
        )

    def refresh_narration_statuses(self, model_version: str = "") -> list[str]:
        """Re-check narration files and staleness.  Never generates or deletes."""
        from app.tts.narration import refresh_statuses

        session = self._require_session()
        snapshot = self.narration_settings(model_version=model_version)
        notes = refresh_statuses(session.project, session.layout.root, snapshot.settings)
        session.project.narration.recompute_status()
        if notes:
            self._notify(Event.USER_ACTION,
                         message="Narration statuses rechecked",
                         project=session.name, notes=len(notes))
        return notes

    def mark_narration_generating(self) -> None:
        """Flag the narration as in progress so the UI cannot lie about it."""
        session = self._require_session()
        session.project.narration.status = "generating"

    def apply_narration_result(self, project_after_generation: Project) -> None:
        """Copy the generated narration section back into the open session."""
        session = self._require_session()
        session.project.narration = project_after_generation.narration
        session.project.touch()

    def add_scene(self, scene_type: str = "blank", name: str = "") -> SceneSpec:
        self._require_session()
        created: dict = {}

        def apply(project: Project) -> None:
            scene = project.add_scene(SceneSpec(type=scene_type, name=name or f"Scene {len(project.scenes) + 1}"))
            created["scene"] = scene

        self.edit("Add scene", apply)
        return created["scene"]

    def remove_scene(self, scene_id: str) -> bool:
        session = self._require_session()
        if session.project.scene_by_id(scene_id) is None:
            return False
        self.edit("Delete scene", lambda project: project.remove_scene(scene_id))
        return True

    def move_scene(self, scene_id: str, index: int) -> bool:
        session = self._require_session()
        if session.project.scene_by_id(scene_id) is None:
            return False

        def apply(project: Project) -> None:
            project.move_scene(scene_id, index)

        self.edit("Move scene", apply)
        return True

    def move_scene_by(self, scene_id: str, delta: int) -> bool:
        session = self._require_session()
        if session.project.scene_by_id(scene_id) is None:
            return False
        self.edit("Reorder scene", lambda project: project.move_scene_by(scene_id, delta))
        return True

    def duplicate_scene(self, scene_id: str) -> Optional[SceneSpec]:
        """Copy a scene as one undoable edit and return the copy."""
        session = self._require_session()
        if session.project.scene_by_id(scene_id) is None:
            return None
        created: dict = {}

        def apply(project: Project) -> None:
            created["copy"] = project.duplicate_scene(scene_id)

        self.edit("Duplicate scene", apply)
        return created.get("copy")

    def rename_scene(self, scene_id: str, new_name: str) -> bool:
        session = self._require_session()
        if session.project.scene_by_id(scene_id) is None:
            return False
        self.edit("Rename scene", lambda project: project.rename_scene(scene_id, new_name))
        return True

    def add_scene_from_template(self, template_key: str, content: Optional[dict] = None,
                                **options) -> SceneSpec:
        """Add a scene built from the registry; the template defines the layout."""
        from ..scene.templates import create_scene_from_template

        self._require_session()
        created: dict = {}

        def apply(project: Project) -> None:
            scene = create_scene_from_template(template_key, content, **options)
            project.add_scene(scene)
            created["scene"] = scene

        self.edit(f"Add {template_key} scene", apply)
        return created["scene"]

    def undo(self) -> Optional[str]:
        session = self._require_session()
        outcome = session.history.undo(session.project)
        if outcome is None:
            return None
        session.project = outcome.project
        self._notify(Event.USER_ACTION, message=f"Undo: {outcome.label}")
        return outcome.label

    def redo(self) -> Optional[str]:
        session = self._require_session()
        outcome = session.history.redo(session.project)
        if outcome is None:
            return None
        session.project = outcome.project
        self._notify(Event.USER_ACTION, message=f"Redo: {outcome.label}")
        return outcome.label

    def discard_changes(self) -> Project:
        """Throw away unsaved edits by reloading the file from disk."""
        session = self._require_session()
        loaded = self.store.load(session.layout.project_file, check_files=True)
        if not loaded.ok or loaded.project is None:
            raise ProjectError(
                what_happened="The saved project could not be reloaded.",
                why=loaded.error or "The file could not be read.",
                actions=("Your edits are still in this window.", "Save the project under a new name to keep them."),
                technical=loaded.error,
            )
        session.project = loaded.project
        session.saved_hash = loaded.project.content_hash()
        session.fingerprint = fingerprint(session.layout.project_file, loaded.project.project.project_version)
        session.history.clear()
        return session.project

    def _require_session(self) -> ProjectSession:
        if self.session is None:
            raise ProjectError(
                what_happened="No project is open.",
                why="This action needs an open project.",
                actions=("Open a project first, or create a new one.",),
                error_code="NO_PROJECT_OPEN",
            )
        return self.session

    # ------------------------------------------------------------------
    # Save / Save As / Duplicate
    # ------------------------------------------------------------------

    def validate(self, *, check_files: bool = False) -> ValidationReport:
        session = self._require_session()
        report = validate_project(session.project, project_dir=session.layout.root, check_files=check_files)
        self._notify(
            Event.PROJECT_VALIDATE,
            message="Project validated",
            errors=len(report.errors),
            warnings=len(report.warnings),
        )
        return report

    def check_external_change(self) -> bool:
        """True when the file on disk is not the one this session loaded."""
        session = self._require_session()
        return self.store.changed_outside(session.layout.project_file, session.fingerprint)

    def save(self, *, reason: str = "", force: bool = False) -> ProjectSaveResult:
        """Write the open project, refusing to clobber newer external changes."""
        session = self._require_session()
        if not force and self.check_external_change():
            self._notify(
                Event.PROJECT_EXTERNAL_CHANGE,
                message="The project changed outside this application",
                path=str(session.layout.project_file),
            )
            raise ProjectConflictError(str(session.layout.project_file), loaded_at=session.opened_at)

        result = self.store.save(session.project, session.layout, reason=reason or "manual")
        if result.ok:
            self._notify(
                Event.PROJECT_SAVE,
                message="Project saved",
                name=session.name,
                path=str(session.layout.project_file),
                version=result.project_version,
            )
        else:
            self._notify(
                Event.PROJECT_SAVE_FAILED,
                message="Project was not saved",
                name=session.name,
                reason=result.error or (result.validation.summary() if result.validation else ""),
            )
        if result.ok:
            session.saved_hash = session.project.content_hash()
            session.fingerprint = fingerprint(session.layout.project_file, session.project.project.project_version)
            self.recents.update(
                session.layout.root,
                name=session.project.project.name,
                modified_at=session.project.project.modified_at,
                project_version=session.project.project.project_version,
                scenes=len(session.project.scenes),
                duration_seconds=float(session.project.estimated_duration_seconds() or 0.0),
            )
        return result

    def autosave(self) -> Optional[Path]:
        """Write the recovery copy when there is something worth recovering."""
        if self.session is None or not self.settings.autosave.enabled:
            return None
        if not self.session.dirty:
            return None
        path = self.store.autosave(self.session.project, self.session.layout)
        if path is not None:
            self._notify(Event.PROJECT_AUTOSAVE, message="Autosaved", path=str(path))
        return path

    def autosave_payload(self) -> Optional[tuple]:
        """Snapshot for a background autosave: ``(payload, layout)`` or ``None``.

        Building the snapshot here (on the caller's thread) keeps the worker job
        from reading a project the interface is still editing.
        """
        if self.session is None or not self.settings.autosave.enabled or not self.session.dirty:
            return None
        report = validate_project(self.session.project, project_dir=self.session.layout.root, check_files=False)
        if not report.ok:
            return None
        payload = self.session.project.to_dict()
        payload["project"]["modified_at"] = utc_now_iso()
        return payload, self.session.layout

    def save_as(self, name: str, folder: Optional[Path] = None) -> Project:
        """Save the open project as a new, independent project."""
        session = self._require_session()
        problems = validate_project_name(name)
        if problems:
            raise ProjectError(
                what_happened="The project could not be saved under that name.",
                why=" ".join(problems),
                actions=("Choose a different name.",),
                error_code="PROJECT_NAME_INVALID",
            )

        target = self._copy_folder(name, folder, session.layout)
        clone = session.project.clone_as(name.strip())
        layout = ProjectLayout(target)
        layout.ensure()
        result = self.store.save(clone, layout, backup=False, bump_version=False, reason="save as")
        if not result.ok:
            self._remove_folder_quietly(target)
            raise ProjectError(
                what_happened="The new project could not be written.",
                why=result.error or "The file could not be written.",
                actions=("Check the free disk space and the folder permissions.",),
                technical=result.error,
            )

        self._adopt(clone, layout, lock=self._acquire_lock(layout), notes=[f"Saved as a copy of '{session.name}'."])
        self._notify(
            Event.PROJECT_SAVE_AS,
            message="Project saved as a new project",
            name=clone.project.name,
            path=str(target),
        )
        return clone

    def duplicate_project(self, source: Path, name: str = "", *, copy_assets: bool = True) -> Project:
        """Create a completely independent copy of a project (section 19)."""
        source_file = find_project_file(Path(source))
        if source_file is None:
            raise ProjectError(
                what_happened="That folder is not a project.",
                why=f"No project.json was found in '{source}'.",
                actions=("Choose the project folder that contains project.json.",),
                error_code="PROJECT_NOT_FOUND",
            )
        loaded = self.store.load(source_file, check_files=False)
        if not loaded.ok or loaded.project is None:
            raise ProjectError(
                what_happened="The project could not be read for duplicating.",
                why=loaded.error or "The file could not be read.",
                actions=("Open the project first to see what is wrong with it.",),
                technical=loaded.error,
            )

        base_name = (name or f"{loaded.project.project.name} (copy)").strip()
        source_layout = ProjectLayout.from_project_file(source_file)
        target = self._copy_folder(base_name, None, source_layout, auto_number=True)
        layout = ProjectLayout(target)
        layout.ensure()

        clone = loaded.project.clone_as(base_name)
        notes: list[str] = []
        if copy_assets:
            copied = self._copy_media_folders(source_layout, layout)
            notes.append(f"Copied {copied} media file(s) into the new project.")
        else:
            for asset in clone.assets:
                if asset.path:
                    asset.absolute_path = str((source_layout.root / asset.path).resolve())
                    asset.path = ""
            notes.append("Assets are referenced from the original project - this copy is not portable.")

        result = self.store.save(clone, layout, backup=False, bump_version=False, reason="duplicate")
        if not result.ok:
            self._remove_folder_quietly(target)
            raise ProjectError(
                what_happened="The duplicate could not be written.",
                why=result.error or "The file could not be written.",
                actions=("Check the free disk space.",),
                technical=result.error,
            )

        self._adopt(clone, layout, lock=self._acquire_lock(layout), notes=notes)
        self._notify(
            Event.PROJECT_DUPLICATE,
            message="Project duplicated",
            name=clone.project.name,
            source=str(source_file),
            path=str(target),
            copied_assets=copy_assets,
        )
        return clone

    def _copy_folder(self, name: str, explicit: Optional[Path], source_layout: ProjectLayout, *, auto_number: bool = False) -> Path:
        if explicit is not None:
            target = Path(explicit)
            if target.exists() and any(target.iterdir()):
                raise ProjectError(
                    what_happened="That folder is not empty.",
                    why=f"{target} already contains files.",
                    actions=("Choose an empty folder.", "Or choose a different name."),
                )
            return target

        stem = safe_filename(name, fallback="project")
        if auto_number:
            return unique_path(self.paths.projects_dir, stem, "")
        target = self.paths.projects_dir / stem
        if target.exists():
            raise ProjectError(
                what_happened=f"A project folder called '{stem}' already exists.",
                why="Two projects cannot share one folder.",
                actions=("Choose a different name.", f"Or open the existing project: {target}"),
                error_code="PROJECT_EXISTS",
            )
        return target

    def _copy_media_folders(self, source: ProjectLayout, target: ProjectLayout) -> int:
        """Copy the folders a project owns (never the regenerable ones)."""
        copied = 0
        for name in ("assets", "audio"):
            origin = source.root / name
            if not origin.is_dir():
                continue
            destination = target.root / name
            destination.mkdir(parents=True, exist_ok=True)
            for item in origin.rglob("*"):
                if not item.is_file():
                    continue
                relative = item.relative_to(origin)
                out = destination / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(item, out)
                    copied += 1
                except OSError as exc:
                    log_event(
                        Event.WARNING,
                        "A media file could not be copied",
                        level=logging.WARNING,
                        logger=LOGGER,
                        source=str(item),
                        reason=str(exc),
                    )
        return copied

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def rename_project(self, name: str) -> Project:
        """Change the display name only - the folder is left alone (section 18)."""
        problems = validate_project_name(name)
        if problems:
            raise ProjectError(
                what_happened="The project could not be renamed.",
                why=" ".join(problems),
                actions=("Choose a different name.",),
                error_code="PROJECT_NAME_INVALID",
            )
        session = self._require_session()
        self.edit("Rename project", lambda project: setattr(project.project, "name", name.strip()))
        self.recents.update(session.layout.root, name=name.strip())
        self._notify(Event.PROJECT_RENAME, message="Project renamed", name=name.strip(), folder=session.layout.root.name)
        return session.project

    def rename_project_folder(self, new_name: str, *, save_first: bool = True) -> Path:
        """Rename the folder on disk.  Relative asset paths keep working."""
        problems = validate_project_name(new_name)
        if problems:
            raise ProjectError(
                what_happened="The folder could not be renamed.",
                why=" ".join(problems),
                actions=("Choose a different folder name.",),
                error_code="PROJECT_NAME_INVALID",
            )
        session = self._require_session()
        if save_first and session.dirty:
            result = self.save(reason="before renaming the folder")
            if not result.ok:
                raise ProjectError(
                    what_happened="The project could not be saved before renaming.",
                    why=result.error or result.validation.summary() if result.validation else "The save failed.",
                    actions=("Fix the reported problems, then rename again.",),
                )

        stem = safe_filename(new_name, fallback="project")
        target = self.paths.projects_dir / stem
        if target.exists():
            raise ProjectError(
                what_happened=f"A folder called '{stem}' already exists in the projects folder.",
                why="The project folder was not renamed.",
                actions=("Choose a different folder name.",),
                error_code="PROJECT_EXISTS",
            )
        if not is_within(session.layout.root, self.paths.projects_dir):
            raise ProjectError(
                what_happened="This project does not live in the projects folder.",
                why="Only projects inside the projects folder can be renamed automatically.",
                actions=(
                    "Rename the folder in Windows Explorer, then open the project from its new location.",
                    "Project-relative assets keep working after the move.",
                ),
                error_code="PROJECT_OUTSIDE",
            )

        old_root = session.layout.root
        had_lock = session.lock is not None
        if session.lock is not None:
            session.lock.release()
            session.lock = None
        try:
            old_root.rename(target)
        except OSError as exc:
            session.lock = self._acquire_lock(session.layout)  # put the lock back
            raise ProjectError(
                what_happened="The folder could not be renamed.",
                why=str(exc),
                actions=("Close any window or Explorer view that has the folder open, then try again.",),
                technical=str(exc),
            ) from exc

        layout = ProjectLayout(target)
        session.layout = layout
        session.fingerprint = fingerprint(layout.project_file, session.project.project.project_version)
        # A lock holds the folder it was taken for, so it has to be re-created
        # for the new location - otherwise it would recreate the old folder.
        if had_lock:
            session.lock = self._acquire_lock(layout)
        # The old entry points at a folder that no longer exists: replace it.
        self.recents.remove(old_root)
        self.recents.add(entry_from_project(session.project, layout.project_file))
        self._notify(
            Event.PROJECT_RENAME,
            message="Project folder renamed",
            name=session.project.project.name,
            path=str(target),
        )
        return target

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def import_asset(self, source: Path, *, kind: str = "", copy: bool = True, name: str = "") -> ImportReport:
        session = self._require_session()
        session.history.record("Add asset", session.project)
        report = asset_tools.import_asset(session.project, session.layout, Path(source), kind=kind, copy=copy, name=name)
        if report.asset is None:
            session.history.undo(session.project)  # nothing changed, drop the step
        return report

    def verify_assets(self) -> list[AssetCheck]:
        """Check every asset against the disk and refresh the stored flags.

        The check and the flag update happen together, so the interface never
        shows a stale "ready" for a file that has gone (directive section 21).
        """
        session = self._require_session()
        checks = verify_assets(session.project, session.layout)
        asset_tools.update_missing_flags(session.project, session.layout)
        return checks

    def relink_asset(self, asset_id: str, new_source: Path, *, copy: bool = True) -> bool:
        session = self._require_session()
        asset = session.project.asset_by_id(asset_id)
        if asset is None:
            return False
        session.history.record("Relink asset", session.project)
        asset_tools.relink_asset(session.project, session.layout, asset, Path(new_source), copy=copy)
        return True

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def scan_recovery(self, path: Optional[Path] = None) -> list[RecoveryCandidate]:
        """Look for recoverable projects (startup, section 13).

        Without *path* every known project is scanned, which is what the
        startup prompt needs.  The scan is read-only.
        """
        candidates: list[RecoveryCandidate] = []
        layouts: list[ProjectLayout] = []
        if path is not None:
            layouts.append(ProjectLayout.from_project_file(find_project_file(Path(path)) or Path(path)))
        else:
            for entry in self.recents.load():
                folder = Path(entry.path)
                if folder.is_dir():
                    layouts.append(ProjectLayout(folder))
        for layout in layouts:
            if not layout.root.is_dir():
                continue
            candidate = self.store.detect_recovery(layout)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def restore_recovery(self, candidate: RecoveryCandidate) -> Project:
        """Apply a recovery file (the user pressed Restore)."""
        # A recovery file lives in autosave/ or backups/, one level below the project.
        layout = ProjectLayout(candidate.path.parent.parent)
        if self.session is not None and self.session.layout.root == layout.root:
            self.close_project(save=False)
        loaded = self.store.apply_recovery(candidate, layout)
        if not loaded.ok or loaded.project is None:
            raise ProjectError(
                what_happened="The recovered project could not be read.",
                why=loaded.error or "The recovery file could not be parsed.",
                actions=(
                    "Try 'Open original' instead.",
                    "The project file has not been changed.",
                ),
                technical=loaded.error,
            )
        self.open_project(layout.project_file)
        self._notify(
            Event.PROJECT_RECOVERY,
            message="Recovered project restored",
            name=loaded.project.project.name,
            source=str(candidate.path),
        )
        return loaded.project

    def ignore_recovery(self, candidate: RecoveryCandidate) -> bool:
        return self.store.ignore_recovery(candidate)

    # ------------------------------------------------------------------
    # Recent projects / browser
    # ------------------------------------------------------------------

    def list_recent(self, *, include_archived: bool = False) -> list[RecentProject]:
        entries = self.recents.refresh_status()
        if not include_archived:
            entries = [entry for entry in entries if not entry.archived]
        return entries

    def remove_from_recent(self, path: Path) -> bool:
        """Forget a project.  Project files are **not** deleted."""
        return self.recents.remove(Path(path))

    def set_favorite(self, path: Path, value: bool) -> bool:
        return self.recents.set_favorite(Path(path), value)

    def set_archived(self, path: Path, value: bool) -> bool:
        return self.recents.set_archived(Path(path), value)

    def index_projects(self, folder: Optional[Path] = None) -> list[dict]:
        """List every project in the projects folder, newest first.

        Only ``project.json`` is read, never a video, so scanning stays quick
        (directive section 36).  A folder without a readable project file is
        still listed, marked as unreadable, so nothing silently disappears.
        """
        root = Path(folder) if folder is not None else self.paths.projects_dir
        known = {entry.folder: entry for entry in self.recents.load()}
        rows: list[dict] = []

        if not root.is_dir():
            return rows

        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            project_file = child / PROJECT_FILENAME
            entry = known.get(child)
            row: dict = {
                "name": child.name,
                "path": child,
                "exists": True,
                "channel": entry.channel_name if entry is not None else "",
                "format": "",
                "scenes": "",
                "modified": entry.modified_at if entry is not None else "",
                "favorite": bool(entry.favorite) if entry is not None else False,
                "recent": entry is not None,
                "readable": False,
                "id": "",
            }
            if project_file.is_file():
                result = self.store.load(child)
                if result.ok and result.project is not None:
                    project = result.project
                    row.update(
                        {
                            "name": project.project.name or child.name,
                            "channel": project.project.channel_name,
                            "format": f"{project.format.width}x{project.format.height} @ {project.format.fps}",
                            "scenes": len(project.scenes),
                            "modified": project.project.modified_at or "",
                            "readable": True,
                            "id": project.project.id,
                        }
                    )
                else:
                    row["format"] = "unreadable project.json"
            else:
                row["format"] = "no project.json"
            rows.append(row)

        rows.sort(key=lambda row: str(row.get("modified") or ""), reverse=True)
        log_event(
            Event.PROJECT_INDEXED,
            "Projects folder indexed",
            logger=LOGGER,
            folder=str(root),
            count=len(rows),
        )
        return rows

    def delete_project(self, path: Path, *, confirm: bool = False) -> bool:
        """Delete a project folder.  Only inside the projects folder, only confirmed."""
        folder = Path(path).resolve()
        if not confirm:
            raise ProjectError(
                what_happened="Deleting a project needs an explicit confirmation.",
                why="This removes the project folder and everything in it.",
                actions=("Confirm the deletion to continue.",),
                error_code="CONFIRM_REQUIRED",
            )
        if not is_within(folder, self.paths.projects_dir):
            raise ProjectError(
                what_happened="Only projects inside the projects folder can be deleted from here.",
                why=f"'{folder}' is somewhere else on this PC.",
                actions=(
                    "Open the folder in Windows Explorer and delete it there.",
                    "Then use 'Remove from recent' to clear it from the list.",
                ),
                error_code="PROJECT_OUTSIDE",
            )
        if self.session is not None and self.session.layout.root == folder:
            self.close_project(save=False)
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            raise ProjectError(
                what_happened="The project folder could not be deleted.",
                why=str(exc),
                actions=("Close anything that has the folder open, then try again.",),
                technical=str(exc),
            ) from exc
        self.recents.remove(folder)
        self._notify(Event.PROJECT_DELETED, message="Project deleted", path=str(folder))
        return True


__all__ = [
    "CreateRequest",
    "ProjectSession",
    "ProjectService",
    "validate_project_name",
]
