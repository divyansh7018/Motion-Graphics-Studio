"""Qt-side glue between the project service and the interface.

Everything that needs a widget lives here - file dialogs, the unsaved-changes
prompt, the recovery prompt, the autosave timer - so pages and the main window
stay free of decisions about *how* a save happens (directive section 34).

The controller owns the :class:`ProjectService` and is the only thing that
changes which project is open.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QWidget

from ..core.errors import AppError, ProjectConflictError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec
from ..project.recent import RecentProject
from ..project.service import ProjectService
from ..project.store import RecoveryCandidate
from .context import AppContext
from .dialogs.project_dialogs import (
    ConflictChoice,
    MissingAssetChoice,
    RecoveryChoice,
    UnsavedChoice,
    ask_external_change,
    ask_missing_asset,
    ask_recovery,
    ask_unsaved_changes,
)
from .notifications import ask_confirm, show_error, show_info

LOGGER = get_logger("project.controller")


class ProjectController(QObject):
    """One place that opens, saves and closes projects for the GUI."""

    project_opened = Signal(object)       # Project
    project_closed = Signal()
    project_saved = Signal(object)        # ProjectSaveResult
    dirty_changed = Signal(bool)
    recent_changed = Signal()
    recovery_available = Signal(object)   # RecoveryCandidate
    message = Signal(str, int)

    def __init__(self, context: AppContext, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.context = context
        self._dirty = False
        self.service = ProjectService(
            context.paths,
            context.settings,
            notifier=lambda event, fields: self._on_service_event(event, fields),
        )
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(self._autosave_interval_ms())
        self._autosave_timer.timeout.connect(self.autosave_now)
        context.settings_changed.connect(self._on_settings_changed)

    # -- state -------------------------------------------------------------

    @property
    def project(self):
        return self.service.current

    @property
    def layout(self):
        return self.service.current_layout

    @property
    def is_open(self) -> bool:
        return self.service.is_open()

    @property
    def dirty(self) -> bool:
        if not self.is_open:
            return False
        dirty = self.service.session.dirty
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)
        return dirty

    def refresh_dirty(self) -> bool:
        return self.dirty

    def title_suffix(self) -> str:
        """The ``*`` shown next to the project name when it has unsaved edits."""
        if not self.is_open:
            return ""
        return " *" if self.dirty else ""

    def display_name(self) -> str:
        return self.project.project.name if self.is_open else ""

    # -- autosave ----------------------------------------------------------

    def _autosave_interval_ms(self) -> int:
        seconds = max(15, int(self.context.settings.autosave.interval_seconds))
        return seconds * 1000

    def start_autosave(self) -> None:
        if self.context.settings.autosave.enabled:
            self._autosave_timer.start()

    def stop_autosave(self) -> None:
        self._autosave_timer.stop()

    def _on_settings_changed(self, _settings) -> None:
        self._autosave_timer.setInterval(self._autosave_interval_ms())
        if self.context.settings.autosave.enabled and self.is_open:
            self.start_autosave()
        elif not self.context.settings.autosave.enabled:
            self.stop_autosave()

    def autosave_now(self) -> None:
        """Queue the recovery write as a background job (never on the UI thread)."""
        if not self.is_open:
            return
        snapshot = self.service.autosave_payload()
        if snapshot is None:
            return
        payload, layout = snapshot
        store = self.service.store

        def body(context: JobContext):
            return store.write_autosave_payload(payload, layout)

        spec = JobSpec(
            key=JobKeys.PROJECT_SAVE,
            title="Autosave",
            description="Writing the recovery copy of the project.",
            body=body,
            visible=False,
            settings=self.context.settings,
            paths=self.context.paths,
            payload={"project": str(layout.project_file)},
        )
        job = self.context.jobs.submit(spec)
        if job is None:
            # A save is already running; the next tick will try again.
            log_event(Event.PROJECT_AUTOSAVE, "Autosave skipped: another save is running", logger=LOGGER)

    # -- new / open --------------------------------------------------------

    def new_project(self, parent: Optional[QWidget]) -> bool:
        """Run the wizard and create the project.  Returns whether one was created."""
        if not self.confirm_close(parent, reason="creating a new project"):
            return False

        from ..tools.kokoro import discover_voices
        from .wizard.new_project import NewProjectWizard

        catalogue = discover_voices(
            model_dir=self.context.paths.kokoro_model_dir,
            extra_dirs=[self.context.paths.models_dir],
        )
        channels = self.service.channels.load()
        wizard = NewProjectWizard(channels, catalogue, self.context.settings, parent)
        if not wizard.exec():
            return False

        request = wizard.create_request()
        try:
            project = self.service.create_project(request)
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be created")
            return False

        self._after_open(project)
        self.message.emit(f"Created “{project.project.name}”.", 6000)
        return True

    def open_project_dialog(self, parent: Optional[QWidget]) -> bool:
        if not self.confirm_close(parent, reason="opening another project"):
            return False
        start = str(self.context.paths.projects_dir)
        folder = QFileDialog.getExistingDirectory(parent, "Choose a project folder", start)
        if not folder:
            return False
        return self.open_path(Path(folder), parent)

    def open_path(self, path: Path, parent: Optional[QWidget] = None) -> bool:
        try:
            project = self.service.open_project(Path(path))
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be opened")
            return False
        self._after_open(project)
        return True

    def _after_open(self, project) -> None:
        self._dirty = False
        self.dirty_changed.emit(False)
        self.recent_changed.emit()
        self.start_autosave()
        self.project_opened.emit(project)
        notes = self.service.session.notes if self.service.session else []
        if notes:
            self.message.emit(f"{len(notes)} note(s) about this project - see the Project page.", 9000)

    # -- save --------------------------------------------------------------

    def save(self, parent: Optional[QWidget], *, reason: str = "") -> bool:
        if not self.is_open:
            return False
        try:
            result = self.service.save(reason=reason)
        except ProjectConflictError as exc:
            return self._resolve_conflict(parent, exc)
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be saved")
            return False

        if not result.ok:
            report = result.validation
            detail = report.to_text() if report is not None else (result.error or "")
            show_info(parent, "The project was not saved.", detail, title="Save refused")
            return False

        self._dirty = False
        self.dirty_changed.emit(False)
        self.recent_changed.emit()
        self.project_saved.emit(result)
        self.message.emit(result.summary(), 6000)
        return True

    def save_as(self, parent: Optional[QWidget]) -> bool:
        if not self.is_open:
            return False
        start = str(self.context.paths.projects_dir)
        suggested = f"{self.display_name()} copy"
        folder = QFileDialog.getExistingDirectory(parent, "Choose a folder for the copy", start)
        if not folder:
            return False
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(parent, "Save as", "New project name:", text=suggested)
        if not ok or not name.strip():
            return False
        try:
            project = self.service.save_as(name.strip(), Path(folder))
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be saved under a new name")
            return False
        self._after_open(project)
        self.message.emit(f"Saved as “{project.project.name}”.", 6000)
        return True

    def duplicate(self, parent: Optional[QWidget], source: Optional[Path] = None) -> bool:
        target = Path(source) if source else (self.layout.root if self.layout else None)
        if target is None:
            return False
        copy_assets = ask_confirm(
            parent,
            "Copy the media files too?",
            "Yes: the duplicate is fully independent.\n"
            "No: it points at the original project's files (smaller, but not portable).",
            confirm_label="Copy media",
            cancel_label="Reference only",
        )
        try:
            duplicate = self.service.duplicate_project(target, copy_assets=copy_assets)
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be duplicated")
            return False
        self._after_open(duplicate)
        self.message.emit(f"Duplicated as “{duplicate.project.name}”.", 6000)
        return True

    def _resolve_conflict(self, parent: Optional[QWidget], exc: ProjectConflictError) -> bool:
        choice = ask_external_change(parent, exc.friendly())
        if choice is ConflictChoice.RELOAD:
            try:
                project = self.service.discard_changes()
            except AppError as error:
                show_error(parent, error.friendly(), "The project could not be reloaded")
                return False
            self._dirty = False
            self.dirty_changed.emit(False)
            self.project_opened.emit(project)
            self.message.emit("Reloaded the version from disk.", 6000)
            return False
        if choice is ConflictChoice.SAVE_AS:
            return self.save_as(parent)
        if choice is ConflictChoice.KEEP:
            # Keep editing; the next save still needs force, so ask explicitly.
            if ask_confirm(
                parent,
                "Overwrite the newer file on disk?",
                "The file changed outside this application. Saving now replaces that version. "
                "A backup of it is kept in the project's backups folder.",
                confirm_label="Overwrite it",
                dangerous=True,
            ):
                result = self.service.save(force=True, reason="user chose to overwrite")
                if result.ok:
                    self._dirty = False
                    self.dirty_changed.emit(False)
                    self.project_saved.emit(result)
                    self.message.emit("Saved over the newer file (a backup was kept).", 8000)
                    return True
        return False

    # -- close -------------------------------------------------------------

    def confirm_close(self, parent: Optional[QWidget], reason: str = "closing the project") -> bool:
        """Ask about unsaved changes.  Returns False when the user cancels."""
        if not self.is_open or not self.dirty:
            return True
        choice = ask_unsaved_changes(parent, self.display_name(), f"You are about to close it by {reason}.")
        if choice is UnsavedChoice.CANCEL:
            return False
        if choice is UnsavedChoice.SAVE:
            return self.save(parent, reason=reason)
        self.service.discard_changes()
        return True

    def close(self, parent: Optional[QWidget], reason: str = "closing the project") -> bool:
        if not self.is_open:
            return True
        if not self.confirm_close(parent, reason=reason):
            return False
        self.stop_autosave()
        self.service.close_project(save=False)
        self._dirty = False
        self.dirty_changed.emit(False)
        self.recent_changed.emit()
        self.project_closed.emit()
        return True

    # -- recovery ----------------------------------------------------------

    def check_recovery(self, parent: Optional[QWidget]) -> Optional[RecoveryCandidate]:
        """Offer recovery data at startup.  Nothing is applied without a choice."""
        try:
            candidates = self.service.scan_recovery()
        except Exception as exc:  # noqa: BLE001 - startup must never fail here
            log_event(
                Event.WARNING,
                "The recovery scan failed",
                level=logging.WARNING,
                logger=LOGGER,
                reason=str(exc),
            )
            return None
        if not candidates:
            return None

        candidate = candidates[0]
        self.recovery_available.emit(candidate)
        choice = ask_recovery(parent, candidate)
        if choice is RecoveryChoice.RESTORE:
            try:
                project = self.service.restore_recovery(candidate)
            except AppError as exc:
                show_error(parent, exc.friendly(), "The recovery data could not be restored")
                return None
            self._after_open(project)
            self.message.emit("Recovered project restored. The previous file is in backups/.", 9000)
            return candidate
        if choice is RecoveryChoice.IGNORE:
            self.service.ignore_recovery(candidate)
            self.message.emit("Recovery data set aside. It is still on disk in the project folder.", 8000)
            return None
        # "Open original": open the project file as it is.
        self.open_path(candidate.path.parent.parent, parent)
        return None

    # -- missing assets ----------------------------------------------------

    def fix_missing_asset(self, parent: Optional[QWidget], asset_id: str) -> bool:
        if not self.is_open:
            return False
        asset = self.project.asset_by_id(asset_id)
        if asset is None:
            return False
        expected = str(asset.resolve(self.layout.root))
        choice, new_path = ask_missing_asset(parent, asset.label(), expected)
        if choice is MissingAssetChoice.CANCEL:
            return False
        if choice is MissingAssetChoice.IGNORE:
            from ..project.assets import ignore_missing_asset

            ignore_missing_asset(self.project, asset)
            self.dirty_changed.emit(True)
            return True
        if new_path is None:
            return False
        try:
            self.service.relink_asset(asset_id, new_path, copy=(choice is MissingAssetChoice.REPLACE))
        except AppError as exc:
            show_error(parent, exc.friendly(), "The asset could not be relinked")
            return False
        self.message.emit(f"“{asset.label()}” now points at {new_path.name}.", 7000)
        return True

    # -- recent list -------------------------------------------------------

    def recent_projects(self) -> list[RecentProject]:
        return self.service.list_recent()

    def remove_from_recent(self, path: Path) -> bool:
        removed = self.service.remove_from_recent(path)
        if removed:
            self.recent_changed.emit()
            self.message.emit("Removed from the recent list. The project files were not touched.", 7000)
        return removed

    def delete_project(self, parent: Optional[QWidget], path: Path, name: str) -> bool:
        if not ask_confirm(
            parent,
            f"Delete “{name}”?",
            "This deletes the project folder and everything inside it - scenes, assets and "
            "renders. This cannot be undone.",
            confirm_label="Delete permanently",
            dangerous=True,
        ):
            return False
        try:
            self.service.delete_project(path, confirm=True)
        except AppError as exc:
            show_error(parent, exc.friendly(), "The project could not be deleted")
            return False
        self.recent_changed.emit()
        self.message.emit(f"“{name}” was deleted.", 6000)
        return True

    def toggle_favorite(self, path: Path) -> None:
        entry = self.service.recents.find(Path(path))
        if entry is None:
            return
        self.service.set_favorite(Path(path), not entry.favorite)
        self.recent_changed.emit()

    # -- internals ---------------------------------------------------------

    def _on_service_event(self, event: str, fields: dict) -> None:
        if event == Event.PROJECT_SAVE:
            self.recent_changed.emit()
        elif event in (Event.PROJECT_CREATE, Event.PROJECT_DUPLICATE, Event.PROJECT_SAVE_AS):
            self.recent_changed.emit()

    def shutdown(self) -> None:
        """Release the project lock without prompting (used while closing)."""
        self.stop_autosave()
        if self.is_open:
            self.service.close_project(save=False)


__all__ = ["ProjectController"]
