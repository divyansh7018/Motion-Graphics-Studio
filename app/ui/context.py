"""Shared application context handed to every page.

The context is the *only* object pages need: it carries the resolved folders,
the settings store, the live settings, the job manager and the last system
report.  Pages never reach for globals, which keeps them testable and keeps
"where does this path come from?" answerable in one place (directive section 6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..checks import items as check_items
from ..checks.status import CheckReport
from ..core import env as env_module
from ..core import logging_setup, maintenance
from ..core.atomicio import FileWriteError
from ..core.errors import to_friendly
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.paths import AppPaths
from ..core.settings import Settings, SettingsStore, disk_free_bytes, settings_summary
from ..jobs.keys import JobKeys
from ..jobs.manager import JobManager
from ..jobs.progress import ProgressReporter
from ..jobs.spec import JobContext, JobSpec

LOGGER = get_logger("context")


@dataclass
class StartupInfo:
    """Facts collected during startup, shown on the Welcome page."""

    data_root_reason: str
    directories_created: int
    stale_temp_removed: int
    recovery_available: bool = False       # Stage B fills this in
    warnings: tuple[str, ...] = ()


class AppContext(QObject):
    """Application-wide state and services."""

    settings_changed = Signal(object)      # Settings
    theme_changed = Signal(str)            # theme name
    report_updated = Signal(object)        # CheckReport
    status_message = Signal(str, int)      # text, timeout ms
    project_changed = Signal(object)       # reserved for Stage B

    def __init__(
        self,
        paths: AppPaths,
        settings_store: SettingsStore,
        settings: Settings,
        jobs: JobManager,
        environment=None,
        startup: Optional[StartupInfo] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.settings_store = settings_store
        self._settings = settings
        self.jobs = jobs
        self.environment = environment if environment is not None else env_module.probe_environment()
        self.startup = startup
        self.last_report: Optional[CheckReport] = None
        self._settings_dirty = False

    # -- settings ----------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def settings_dirty(self) -> bool:
        return self._settings_dirty

    @property
    def theme(self) -> str:
        return self._settings.general.theme

    @property
    def advanced_mode(self) -> bool:
        return self._settings.general.advanced_mode

    def apply_settings(self, new_settings: Settings, *, save: bool = True, reason: str = "") -> bool:
        """Adopt *new_settings*, apply side effects and (optionally) save."""
        theme_changed = new_settings.general.theme != self._settings.general.theme
        level_changed = new_settings.logging.level != self._settings.logging.level
        self._settings = new_settings

        if level_changed:
            logging_setup.set_level(new_settings.logging.level)

        if save:
            try:
                self.settings_store.save(new_settings, keep_backups=new_settings.autosave.keep_backups)
                self._settings_dirty = False
            except (FileWriteError, OSError) as exc:
                friendly = to_friendly(exc, "Your settings could not be saved.")
                log_event(Event.ERROR, "Settings save failed", level=logging.ERROR, logger=LOGGER, reason=friendly.why)
                self.status_message.emit("Settings could not be saved - see the log", 8000)
                return False

        self.settings_changed.emit(new_settings)
        if theme_changed:
            self.theme_changed.emit(new_settings.general.theme)
        if reason:
            log_event(Event.SETTINGS_SAVED, f"Settings updated ({reason})", logger=LOGGER)
        return True

    def mark_settings_dirty(self, dirty: bool = True) -> None:
        self._settings_dirty = dirty

    def reset_settings(self) -> Settings:
        defaults = self.settings_store.reset_to_defaults()
        self._settings = defaults
        self.settings_changed.emit(defaults)
        self.theme_changed.emit(defaults.general.theme)
        return defaults

    # -- environment -------------------------------------------------------

    def refresh_environment(self) -> None:
        """Re-probe the machine (used by 'Re-check system')."""
        self.environment = env_module.probe_environment()

    def disk_free_bytes(self) -> int:
        return disk_free_bytes(Path(self.paths.data_root))

    def settings_summary_text(self) -> str:
        return settings_summary(self._settings)

    # -- system check ------------------------------------------------------

    def build_check_context(self, deep: bool = False):
        return check_items.build_context(self.paths, self._settings, deep=deep, environment=self.environment)

    def submit_system_check(self, deep: bool = False):
        """Queue a system check.  Returns the job, or ``None`` if one is running."""
        from ..checks.job import make_system_check_spec

        spec = make_system_check_spec(
            self.paths,
            self._settings,
            deep=deep,
            environment=self.environment,
        )
        return self.jobs.submit(spec)

    def store_report(self, report: CheckReport) -> None:
        self.last_report = report
        self.report_updated.emit(report)

    # -- maintenance -------------------------------------------------------

    def clear_caches(self, targets, progress: Optional[ProgressReporter] = None) -> maintenance.CleanupReport:
        return maintenance.run_cleanup(self.paths, targets, progress=progress)

    def submit_cache_cleanup(self, targets) -> Optional[object]:
        """Queue a cache cleanup job (one job per click - section 9)."""
        targets = list(targets)

        def body(context: JobContext):
            return maintenance.run_cleanup(context.paths, targets, progress=context.progress)

        spec = JobSpec(
            key=JobKeys.CACHE_CLEANUP,
            title="Clear cache",
            description="Removing temporary files that can be recreated.",
            body=body,
            settings=self._settings,
            paths=self.paths,
            payload={"targets": targets},
        )
        return self.jobs.submit(spec)

    # -- notifications -----------------------------------------------------

    def notify(self, message: str, timeout_ms: int = 5000) -> None:
        """Show a short message in the status bar."""
        self.status_message.emit(message, timeout_ms)

    # -- diagnostics -------------------------------------------------------

    def diagnostics_text(self) -> str:
        """Everything support needs, in one copyable block."""
        lines = [
            "=== Motion Graphics Studio diagnostics ===",
            f"Log file         : {logging_setup.log_path_for_user()}",
            f"Session          : {logging_setup.current_session_id()}",
            "",
            "--- Folders ---",
            self.paths.describe(),
            "",
            "--- Settings ---",
            settings_summary(self._settings),
            "",
        ]
        if self.environment is not None:
            lines.append("--- Machine ---")
            lines.extend(self.environment.summary_lines())
            lines.append("")
        if self.last_report is not None:
            lines.append("--- Last system check ---")
            lines.append(self.last_report.to_text())
        else:
            lines.append("--- Last system check ---")
            lines.append("(not run yet)")
        return "\n".join(str(line) for line in lines)


def create_context(
    paths: AppPaths,
    jobs: JobManager,
    startup: Optional[StartupInfo] = None,
    load_result=None,
) -> AppContext:
    """Load settings and build the context used for the whole session.

    A previously loaded result can be passed in (the entry point loads settings
    early to decide about the single-instance guard), which avoids reading the
    file twice.
    """
    store = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    result = load_result if load_result is not None else store.load()
    if result.notes:
        for note in result.notes:
            log_event(Event.WARNING, f"Settings note: {note}", logger=LOGGER)
    environment = env_module.probe_environment()
    log_event(
        Event.ENVIRONMENT_PROBE,
        "Machine probed",
        logger=LOGGER,
        cpu=environment.cpu_count_logical,
        ram_gb=round(environment.ram_total_gb, 1) if environment.ram_total_gb else None,
        windows=environment.is_windows,
    )
    context = AppContext(
        paths=paths,
        settings_store=store,
        settings=result.settings,
        jobs=jobs,
        environment=environment,
        startup=startup,
    )
    if result.source == "recovered":
        context.notify("Settings could not be read - defaults were restored.", 8000)
    return context
