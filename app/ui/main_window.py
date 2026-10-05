"""Main window: navigation, status bar, menus and window lifecycle.

Responsibilities kept here (and nowhere else):

* the startup sequence after the window is shown (system check + readiness),
* turning job signals into visible status without blocking the interface,
* remembering window geometry and the current page,
* a clean close that cancels jobs, saves settings and logs the exit
  (directive section 70).

Pages themselves own nothing global; they receive the :class:`AppContext`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..core import logging_setup
from ..core import maintenance
from ..core.env import process_cpu_percent, system_cpu_percent
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..jobs.keys import JobKeys
from ..core.version import APP_NAME, APP_STAGE_LABEL, APP_VERSION, version_string
from ..jobs.manager import JobManager
from ..jobs.states import JobState
from .context import AppContext
from .notifications import (
    ask_confirm,
    open_folder,
    open_log_folder,
    show_error,
    show_info,
    status_message_for_job,
)
from .theme import METRICS, status_color
from .views.diagnostics import DiagnosticsPage
from .views.maintenance import MaintenancePage
from .views.settings_view import SettingsPage
from .views.system_check import SystemCheckPage
from .views.welcome import WelcomePage
from ..core.errors import AppError
from .views.project_browser import ProjectBrowserPage
from .views.project_settings import ProjectSettingsPage
from .views.narration_view import NarrationPage
from .views.project_view import ProjectPage
from .views.script_view import ScriptPage
from .views.storyboard_view import StoryboardPage
from .widgets.job_panel import JobProgressWidget

LOGGER = get_logger("main_window")

#: Pages that exist in this build.  Everything else in the sidebar is a
#: clearly-labelled placeholder for a later stage (directive section 54).
PAGES: tuple[tuple[str, str, str, str], ...] = (
    # (key, label, section, status)
    ("welcome", "Dashboard", "Start", "ready"),
    ("project", "Project", "Create", "ready"),
    ("script", "Script", "Create", "ready"),
    ("narration", "Narration", "Create", "ready"),
    ("storyboard", "Storyboard", "Create", "ready"),
    ("project_settings", "Project settings", "Create", "ready"),
    ("projects", "Projects", "Create", "ready"),
    ("system_check", "System check", "Start", "ready"),
)

FUTURE_PAGES: tuple[tuple[str, str, str], ...] = (
    # (label, section, stage note)
    ("Visuals", "Create", "Stage H - images"),
    ("Music", "Create", "Stage E - audio"),
    ("Timeline", "Advanced", "Stage D - timeline"),
    ("Render", "Advanced", "Stage F - renderer"),
    ("Video library", "Advanced", "Stage G - output"),
)

SETTINGS_PAGES: tuple[tuple[str, str, str, str], ...] = (
    ("settings", "Settings", "Application", "ready"),
    ("maintenance", "Maintenance", "Application", "ready"),
    ("diagnostics", "Diagnostics", "Application", "ready"),
)


class NavigationList(QListWidget):
    """Sidebar list that ignores clicks on disabled (future) entries."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("NavList")
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSelectionMode(QListWidget.SingleSelection)


class MainWindow(QMainWindow):
    """The application shell."""

    page_changed = Signal(str)

    def __init__(self, context: AppContext, jobs: JobManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.context = context
        self.jobs = jobs
        self._closing = False
        self._job_results_shown: set[str] = set()

        self._base_title = f"{APP_NAME} {APP_VERSION} - {APP_STAGE_LABEL}"
        self.setWindowTitle(self._base_title)
        self.setMinimumSize(METRICS.window_min_width, METRICS.window_min_height)

        self._build_ui()
        self._build_menus()
        self._connect_signals()
        self._update_project_actions()
        self._restore_geometry()
        self._refresh_readiness_indicator()
        self._start_resource_timer()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal, self)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setMinimumWidth(METRICS.sidebar_width)
        sidebar.setMaximumWidth(METRICS.sidebar_width + 60)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        brand = QLabel(APP_NAME)
        brand.setObjectName("Brand")
        brand.setWordWrap(True)
        sidebar_layout.addWidget(brand)

        self.nav = NavigationList()
        sidebar_layout.addWidget(self.nav, 1)

        self.version_label = QLabel(version_string())
        self.version_label.setObjectName("Hint")
        self.version_label.setContentsMargins(METRICS.md, METRICS.sm, METRICS.md, METRICS.md)
        sidebar_layout.addWidget(self.version_label)

        splitter.addWidget(sidebar)

        self.stack = QStackedWidget()
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([METRICS.sidebar_width, 1000])

        self.setCentralWidget(splitter)
        self._populate_navigation()
        self._build_pages()
        self._build_status_bar()

    def _populate_navigation(self) -> None:
        """Sidebar entries: implemented pages first, then clearly marked future ones."""
        self._page_keys: list[str] = []
        self._section_items: dict[str, QListWidgetItem] = {}

        def add_section(name: str) -> None:
            item = QListWidgetItem(name.upper())
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(self.palette().mid())
            self.nav.addItem(item)
            self._section_items[name] = item

        def add_page(key: str, label: str) -> None:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setToolTip(f"Open the {label} page.")
            self.nav.addItem(item)
            self._page_keys.append(key)

        def add_future(label: str, note: str) -> None:
            item = QListWidgetItem(f"{label}  (later)")
            item.setFlags(Qt.NoItemFlags)
            item.setToolTip(f"{label} is not available in this build yet.\n{note}")
            font = item.font()
            font.setItalic(True)
            item.setFont(font)
            item.setForeground(self.palette().mid())
            self.nav.addItem(item)

        current_section = ""
        for key, label, section, _status in PAGES:
            if section != current_section:
                add_section(section)
                current_section = section
            add_page(key, label)

        # Future workflow pages, grouped so the roadmap is visible but honest.
        current_section = ""
        for label, section, note in FUTURE_PAGES:
            if section != current_section:
                add_section(section)
                current_section = section
            add_future(label, note)

        current_section = ""
        for key, label, section, _status in SETTINGS_PAGES:
            if section != current_section:
                add_section(section)
                current_section = section
            add_page(key, label)

        self.nav.setCurrentRow(1)

    def _build_pages(self) -> None:
        self.welcome_page = WelcomePage(self.context)
        self.project_page = ProjectPage(self.context)
        self.script_page = ScriptPage(self.context)
        self.narration_page = NarrationPage(self.context)
        self.storyboard_page = StoryboardPage(self.context)
        self.project_settings_page = ProjectSettingsPage(self.context)
        self.projects_page = ProjectBrowserPage(self.context)
        self.system_check_page = SystemCheckPage(self.context)
        self.settings_page = SettingsPage(self.context)
        self.maintenance_page = MaintenancePage(self.context)
        self.diagnostics_page = DiagnosticsPage(self.context)

        self._pages: dict[str, QWidget] = {
            "welcome": self.welcome_page,
            "project": self.project_page,
            "script": self.script_page,
            "narration": self.narration_page,
            "storyboard": self.storyboard_page,
            "project_settings": self.project_settings_page,
            "projects": self.projects_page,
            "system_check": self.system_check_page,
            "settings": self.settings_page,
            "maintenance": self.maintenance_page,
            "diagnostics": self.diagnostics_page,
        }
        for key in self._page_keys:
            page = self._pages.get(key)
            if page is not None:
                self.stack.addWidget(page)
        self._key_for_widget = {widget: key for key, widget in self._pages.items()}

        self.welcome_page.open_system_check.connect(lambda: self.show_page("system_check"))
        self.welcome_page.open_settings.connect(lambda: self.show_page("settings"))
        self.welcome_page.open_data_folder.connect(lambda: open_folder(self, self.context.paths.data_root))
        self.welcome_page.open_output_folder.connect(lambda: open_folder(self, self.context.paths.output_dir))

        self._connect_project_pages()

        self.system_check_page.check_requested.connect(self.run_system_check)
        self.system_check_page.open_settings.connect(lambda: self.show_page("settings"))
        self.settings_page.restart_required.connect(self._on_restart_required)
        self.maintenance_page.cleanup_requested.connect(self.run_cache_cleanup)

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        bar.setSizeGripEnabled(True)
        self.setStatusBar(bar)

        self.readiness_label = QLabel("System: not checked")
        self.readiness_label.setToolTip("Click to open the System check page.")
        self.readiness_label.setCursor(Qt.PointingHandCursor)
        self.readiness_label.mousePressEvent = lambda _event: self.show_page("system_check")  # type: ignore[assignment]
        bar.addWidget(self.readiness_label)

        self.job_widget = JobProgressWidget()
        self.job_widget.cancel_requested.connect(self._cancel_job)
        bar.addWidget(self.job_widget, 1)

        self.resource_label = QLabel("")
        self.resource_label.setObjectName("Hint")
        bar.addPermanentWidget(self.resource_label)

        self.jobs_count_label = QLabel("")
        self.jobs_count_label.setObjectName("Hint")
        bar.addPermanentWidget(self.jobs_count_label)

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        # -- File ---------------------------------------------------------
        file_menu = menu_bar.addMenu("&File")
        self.new_project_action = QAction("New project...", self)
        self.new_project_action.setShortcut("Ctrl+N")
        self.new_project_action.triggered.connect(self.new_project)

        self.open_project_action = QAction("Open project...", self)
        self.open_project_action.setShortcut("Ctrl+O")
        self.open_project_action.triggered.connect(self.open_project)

        self.save_project_action = QAction("Save project", self)
        self.save_project_action.setShortcut("Ctrl+S")
        self.save_project_action.triggered.connect(self.save_project)

        self.save_as_project_action = QAction("Save project as...", self)
        self.save_as_project_action.setShortcut("Ctrl+Shift+S")
        self.save_as_project_action.triggered.connect(self.save_project_as)

        self.duplicate_project_action = QAction("Duplicate project...", self)
        self.duplicate_project_action.triggered.connect(self.duplicate_project)

        self.close_project_action = QAction("Close project", self)
        self.close_project_action.triggered.connect(self.close_project)

        file_menu.addAction(self.new_project_action)
        file_menu.addAction(self.open_project_action)
        file_menu.addAction(self.save_project_action)
        file_menu.addAction(self.save_as_project_action)
        file_menu.addAction(self.duplicate_project_action)
        file_menu.addSeparator()
        file_menu.addAction(self.close_project_action)

        # The recent list lives in the File menu, rebuilt whenever it changes.
        self.recent_menu = file_menu.addMenu("Recent projects")
        file_menu.addSeparator()

        open_data_action = QAction("Open data folder", self)
        open_data_action.triggered.connect(lambda: open_folder(self, self.context.paths.data_root))
        file_menu.addAction(open_data_action)

        open_output_action = QAction("Open output folder", self)
        open_output_action.triggered.connect(lambda: open_folder(self, self.context.paths.output_dir))
        file_menu.addAction(open_output_action)

        file_menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # -- Tools --------------------------------------------------------
        tools_menu = menu_bar.addMenu("&Tools")
        check_action = QAction("Run system check", self)
        check_action.setShortcut("F5")
        check_action.triggered.connect(lambda: self.run_system_check(deep=True))
        tools_menu.addAction(check_action)

        clear_temp_action = QAction("Clear temporary files", self)
        clear_temp_action.triggered.connect(lambda: self.run_cache_cleanup(["temp_files"], confirm=False))
        tools_menu.addAction(clear_temp_action)

        clear_cache_action = QAction("Clear preview cache", self)
        clear_cache_action.triggered.connect(lambda: self.run_cache_cleanup(["preview_cache"], confirm=False))
        tools_menu.addAction(clear_cache_action)

        tools_menu.addSeparator()
        smoke_action = QAction("Run smoke test", self)
        smoke_action.setToolTip(
            "End-to-end check: folders, settings, FFmpeg, a real one-second encode, "
            "job handling and cancellation. Takes a few seconds."
        )
        smoke_action.triggered.connect(self.run_smoke_test)
        tools_menu.addAction(smoke_action)

        tools_menu.addSeparator()
        log_action = QAction("Open log folder", self)
        log_action.triggered.connect(lambda: open_log_folder(self))
        tools_menu.addAction(log_action)

        # -- View ---------------------------------------------------------
        view_menu = menu_bar.addMenu("&View")
        theme_menu = view_menu.addMenu("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self._theme_actions: dict[str, QAction] = {}
        for label, value in (("Dark", "dark"), ("Light", "light"), ("Match Windows", "system")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.context.theme == value)
            action.triggered.connect(lambda _checked=False, v=value: self._set_theme(v))
            theme_group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[value] = action

        self.advanced_action = QAction("Advanced mode", self)
        self.advanced_action.setCheckable(True)
        self.advanced_action.setChecked(self.context.advanced_mode)
        self.advanced_action.setToolTip(
            "Shows advanced pages (scenes, timeline, render settings) as those stages are released."
        )
        self.advanced_action.triggered.connect(self._toggle_advanced_mode)
        view_menu.addAction(self.advanced_action)

        view_menu.addSeparator()
        reload_action = QAction("Reload page", self)
        reload_action.setShortcut("F6")
        reload_action.triggered.connect(self._reload_current_page)
        view_menu.addAction(reload_action)

        # -- Help ---------------------------------------------------------
        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        docs_action = QAction("Open documentation folder", self)
        docs_action.triggered.connect(lambda: open_folder(self, self.context.paths.docs_dir))
        help_menu.addAction(docs_action)

        diagnostics_action = QAction("Copy diagnostics for support", self)
        diagnostics_action.triggered.connect(self._copy_diagnostics)
        help_menu.addAction(diagnostics_action)


    def _future_action(self, label: str, shortcut: str, note: str) -> QAction:
        """Create a disabled, clearly labelled action for a later stage."""
        action = QAction(f"{label}  (later stage)", self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setEnabled(False)
        action.setToolTip(f"Not in this build yet.\n{note}")
        action.setStatusTip(note)
        return action

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self.nav.currentItemChanged.connect(self._on_nav_changed)
        self.context.status_message.connect(self._show_status_message)
        self.context.report_updated.connect(self._on_report_updated)
        self.context.theme_changed.connect(self._on_theme_changed)

        self.jobs.job_submitted.connect(self._on_job_submitted)
        self.jobs.job_started.connect(self._on_job_started)
        self.jobs.job_progress.connect(self._on_job_progress)
        self.jobs.job_finished.connect(self._on_job_finished)
        self.jobs.job_rejected.connect(self._on_job_rejected)
        self.jobs.job_cancel_timeout.connect(self._on_job_cancel_timeout)
        self.jobs.job_stalled.connect(self._on_job_stalled)

    def _on_nav_changed(self, current: Optional[QListWidgetItem], _previous=None) -> None:
        if current is None:
            return
        key = current.data(Qt.UserRole)
        if not key:
            return
        page = self._pages.get(str(key))
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.page_changed.emit(str(key))
        self._remember_page(str(key))
        if str(key) == "diagnostics":
            self.diagnostics_page.refresh()
        if str(key) == "maintenance":
            self.maintenance_page.refresh()
        if str(key) == "project":
            self.project_page.refresh()
        if str(key) == "narration":
            # Voice discovery happens when the user gets here, not at start-up.
            self.narration_page.ensure_catalogue()
        if str(key) == "storyboard":
            self.storyboard_page.ensure_storyboard()
        if str(key) == "project_settings":
            self.project_settings_page.refresh()
        if str(key) == "welcome":
            self.welcome_page.refresh()

    # ------------------------------------------------------------------
    # Projects (Stage B)
    # ------------------------------------------------------------------

    def _connect_project_pages(self) -> None:
        """Wire the project pages to the controller - no logic in the widgets."""
        controller = self.context.projects
        if controller is None:
            return

        controller.project_opened.connect(self._on_project_opened)
        controller.project_closed.connect(self._on_project_closed)
        controller.dirty_changed.connect(lambda _dirty: self._update_title())
        controller.recent_changed.connect(self._refresh_recent_menu)
        controller.message.connect(self._show_status_message)

        page = self.project_page
        page.save_requested.connect(lambda: self.save_project())
        page.save_as_requested.connect(lambda: self.save_project_as())
        page.duplicate_requested.connect(lambda: self.duplicate_project())
        page.rename_requested.connect(lambda: self.rename_project())
        page.rename_folder_requested.connect(lambda: self.rename_project_folder())
        page.close_requested.connect(lambda: self.close_project())
        page.open_settings_page.connect(lambda: self.show_page("project_settings"))
        page.project_changed.connect(self._on_project_edited)

        settings_page = self.project_settings_page
        settings_page.save_requested.connect(lambda: self._save_project_settings())
        settings_page.discard_requested.connect(lambda: self._discard_project_settings())
        settings_page.project_changed.connect(self._on_project_edited)

        self.welcome_page.new_project.connect(self.new_project)
        self.welcome_page.open_project.connect(self.open_project)
        self.welcome_page.open_browser.connect(lambda: self.show_page("projects"))
        self.welcome_page.open_project_path.connect(lambda path: self.open_project_path(path))
        self.projects_page.open_project_path.connect(lambda path: self.open_project_path(path))
        self.projects_page.new_project.connect(self.new_project)

        self._refresh_recent_menu()
        self._update_title()

    def new_project(self) -> None:
        controller = self.context.projects
        if controller is not None and controller.new_project(self):
            self.show_page("project")

    def open_project(self) -> None:
        controller = self.context.projects
        if controller is not None and controller.open_project_dialog(self):
            self.show_page("project")

    def open_project_path(self, path) -> None:
        controller = self.context.projects
        if controller is not None and controller.open_path(Path(path), self):
            self.show_page("project")

    def save_project(self) -> None:
        controller = self.context.projects
        if controller is not None:
            controller.save(self, reason="user pressed save")

    def save_project_as(self) -> None:
        controller = self.context.projects
        if controller is not None:
            controller.save_as(self)

    def duplicate_project(self) -> None:
        controller = self.context.projects
        if controller is not None:
            controller.duplicate(self)

    def rename_project(self) -> None:
        """Change the project name only; the folder is a separate action."""
        controller = self.context.projects
        if controller is None or not controller.is_open:
            self._show_status_message("No project is open.", 4000)
            return
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, "Rename project", "New name:", text=controller.display_name()
        )
        if not ok or not name.strip():
            return
        try:
            controller.service.rename_project(name.strip())
        except AppError as exc:
            show_error(self, exc.friendly(), "The project could not be renamed")
            return
        self.project_page.refresh()
        self._update_title()
        self._show_status_message(f"Renamed to “{name.strip()}”. Press Save to write it to disk.", 7000)

    def rename_project_folder(self) -> None:
        controller = self.context.projects
        if controller is None or not controller.is_open:
            self._show_status_message("No project is open.", 4000)
            return
        from PySide6.QtWidgets import QInputDialog

        current = controller.layout.root.name
        name, ok = QInputDialog.getText(self, "Rename project folder", "New folder name:", text=current)
        if not ok or not name.strip() or name.strip() == current:
            return
        if not ask_confirm(
            self,
            "Rename the folder on disk?",
            f"'{current}' becomes '{name.strip()}'.\n\n"
            "Anything outside the project that points at the old folder will stop finding it.",
            confirm_label="Rename folder",
        ):
            return
        try:
            controller.service.rename_project_folder(name.strip())
        except AppError as exc:
            show_error(self, exc.friendly(), "The folder could not be renamed")
            return
        self.project_page.refresh()
        self._update_title()
        self._refresh_recent_menu()

    def close_project(self) -> None:
        controller = self.context.projects
        if controller is not None and controller.close(self):
            self.show_page("welcome")

    def _save_project_settings(self) -> None:
        self.project_settings_page.apply_changes()
        self.save_project()
        self.project_settings_page.refresh()
        self.project_page.refresh()

    def _discard_project_settings(self) -> None:
        controller = self.context.projects
        if controller is None or not controller.is_open:
            return
        controller.service.discard_changes()
        self.project_settings_page.refresh()
        self.project_page.refresh()
        self._update_title()
        self._show_status_message("Project settings restored to the last saved version.", 6000)

    def _on_project_edited(self) -> None:
        self._update_title()
        self.project_page.refresh_title()

    def _on_project_opened(self, project) -> None:
        self.project_page.refresh()
        self.project_settings_page.refresh()
        self.welcome_page.refresh()
        self._refresh_recent_menu()
        self._update_title()
        self._update_project_actions()

    def _on_project_closed(self) -> None:
        self.project_page.refresh()
        self.project_settings_page.refresh()
        self.welcome_page.refresh()
        self._refresh_recent_menu()
        self._update_title()
        self._update_project_actions()

    def _update_project_actions(self) -> None:
        controller = self.context.projects
        available = controller is not None and controller.is_open
        for action in (
            self.save_project_action,
            self.save_as_project_action,
            self.duplicate_project_action,
            self.close_project_action,
        ):
            action.setEnabled(available)

    def _update_title(self) -> None:
        controller = self.context.projects
        if controller is None or not controller.is_open:
            self.setWindowTitle(self._base_title)
            return
        self.setWindowTitle(f"{controller.display_name()}{controller.title_suffix()} - {self._base_title}")

    def _refresh_recent_menu(self) -> None:
        """Rebuild File > Recent projects from the stored recent list."""
        menu = getattr(self, "recent_menu", None)
        controller = self.context.projects
        if menu is None or controller is None:
            return
        menu.clear()
        entries = controller.recent_projects()
        if not entries:
            empty = menu.addAction("No recent projects")
            empty.setEnabled(False)
            return
        for entry in entries[:10]:
            label = entry.name or entry.folder.name
            if not entry.folder.is_dir():
                label += " (folder missing)"
            action = menu.addAction(label)
            action.setToolTip(str(entry.folder))
            folder = entry.folder
            action.triggered.connect(lambda _checked=False, path=folder: self.open_project_path(path))

    def show_page(self, key: str) -> None:
        """Switch to a page by key (used by the welcome cards and menus)."""
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item.data(Qt.UserRole) == key:
                self.nav.setCurrentRow(row)
                return

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def run_system_check(self, deep: bool = False) -> None:
        """Start a system check as a background job (never on the UI thread)."""
        job = self.context.submit_system_check(deep=deep)
        if job is None:
            self.context.notify("A system check is already running.")
            return
        self.system_check_page.set_running(True, "Checking this computer...")
        self.show_page("system_check")

    def run_cache_cleanup(self, targets, confirm: bool = True) -> None:
        if confirm:
            names = ", ".join(maintenance.TARGET_LABELS.get(name, name) for name in targets)
            if not ask_confirm(
                self,
                f"Clear {names}?",
                "Cached files are recreated automatically. Projects and exported videos are not affected.",
                confirm_label="Clear",
            ):
                return
        job = self.context.submit_cache_cleanup(targets)
        if job is None:
            self.context.notify("A cleanup is already running.")

    def run_smoke_test(self) -> None:
        """Queue the end-to-end smoke test as a background job."""
        from ..diagnostics.smoke import make_smoke_test_spec

        spec = make_smoke_test_spec(self.context.paths, self.context.settings, verbose=False)
        job = self.jobs.submit(spec)
        if job is None:
            self.context.notify("A smoke test is already running.")
            return
        self.context.notify("Smoke test started - results appear when it finishes.", 6000)

    def _cancel_job(self, job_id: str) -> None:
        job = self.jobs.job(job_id)
        if job is None:
            return
        self.jobs.cancel(job_id)
        self.job_widget.set_state(JobState.CANCELLING)
        self.context.notify(f"Cancelling {job.title}...", 4000)

    def _on_job_submitted(self, job) -> None:
        self.job_widget.bind_job(job)
        self._update_jobs_count()

    def _on_job_started(self, _job_id: str) -> None:
        self._update_jobs_count()

    def _on_job_progress(self, job_id: str, progress) -> None:
        job = self.jobs.job(job_id)
        if job is None:
            return
        if self.job_widget._job_id != job_id:  # the panel follows the newest job
            self.job_widget.bind_job(job)
        self.job_widget.update_progress(progress)
        if job.key == "system.check":
            text = progress.text() or "Checking..."
            self.system_check_page.set_running(True, text)

    def _on_job_finished(self, result) -> None:
        self.job_widget.finish(result)
        self._update_jobs_count()
        self._show_status_message(status_message_for_job(result), 6000)

        if result.key == "system.check":
            self.system_check_page.set_running(False, "")
            if result.succeeded and result.value is not None:
                self.context.store_report(result.value)
            elif result.failed and result.error is not None:
                self.system_check_page.set_running(False, "The system check could not be completed.")
                show_error(self, result.error, "System check failed")

        if result.key == "maintenance.clear_cache":
            if result.succeeded and result.value is not None:
                self.maintenance_page.show_result(result.value.summary())
                self.context.notify(result.value.summary(), 8000)
            elif result.failed and result.error is not None:
                show_error(self, result.error, "Cleanup failed")

        if result.key == JobKeys.VOICE_SCAN:
            self.narration_page.on_scan_finished(result)

        if result.key == JobKeys.VOICE_PREVIEW:
            self.narration_page.on_preview_finished(result)

        if result.key == JobKeys.TTS_NARRATION:
            self.narration_page.on_narration_finished(result)
            # The script page shows the narration state too.
            self.script_page.refresh()
            self.project_page.refresh()

        if result.key == JobKeys.STORYBOARD_RENDER:
            self.storyboard_page.on_storyboard_finished(result)

        if result.key == JobKeys.SCENE_PREVIEW:
            self.storyboard_page.on_preview_finished(result)

        if result.key == "diagnostics.smoke_test":
            self._show_smoke_result(result)

        # A job that is not one of the known light jobs gets a dialog only when
        # it failed, so the user is never interrupted by success messages.
        if result.failed and result.key not in ("system.check", "maintenance.clear_cache"):
            if result.error is not None:
                show_error(self, result.error)

    def _show_smoke_result(self, result) -> None:
        if not result.succeeded or result.value is None:
            return
        from .notifications import show_info

        report = result.value
        show_info(
            self,
            report.summary(),
            "Open the Diagnostics page to see every step in detail.",
            title="Smoke test",
        )
        self.diagnostics_page.refresh()

    def _on_job_rejected(self, spec, reason: str) -> None:
        self.context.notify(reason, 7000)
        log_event(Event.WARNING, f"Job '{spec.key}' was not started: {reason}", logger=LOGGER)

    def _on_job_cancel_timeout(self, job_id: str, seconds: float) -> None:
        job = self.jobs.job(job_id)
        title = job.title if job is not None else "The task"
        self.context.notify(f"{title} is taking longer than expected to stop...", 8000)

    def _on_job_stalled(self, job_id: str, seconds: float) -> None:
        job = self.jobs.job(job_id)
        title = job.title if job is not None else "A task"
        self.context.notify(f"{title} has not reported progress for {seconds:.0f}s. It is still running.", 8000)

    def _update_jobs_count(self) -> None:
        count = self.jobs.active_count()
        self.jobs_count_label.setText(f"{count} task(s) running" if count else "")

    # ------------------------------------------------------------------
    # Status / readiness
    # ------------------------------------------------------------------

    def _show_status_message(self, message: str, timeout_ms: int = 5000) -> None:
        if message:
            self.statusBar().showMessage(message, timeout_ms)

    def _on_report_updated(self, report) -> None:
        self.system_check_page.show_report(report)
        self.welcome_page.refresh()
        self._refresh_readiness_indicator()

    def _refresh_readiness_indicator(self) -> None:
        report = self.context.last_report
        theme = self.context.theme
        if report is None:
            self.readiness_label.setText("System: not checked yet")
            self.readiness_label.setStyleSheet(f"color: {status_color(theme, 'muted')};")
            return
        counts = report.summary_counts()
        ready = counts.get("ready", 0)
        problems = counts.get("missing", 0) + counts.get("blocked", 0)
        optional = counts.get("optional", 0) + counts.get("warning", 0)
        text = f"System: {ready} ready"
        if problems:
            text += f", {problems} missing"
        if optional:
            text += f", {optional} optional"
        self.readiness_label.setText(text)
        colour = "ok" if not problems else "bad"
        self.readiness_label.setStyleSheet(f"color: {status_color(theme, colour)};")
        self.readiness_label.setToolTip(f"{report.headline()}\nClick for details.")

    def _on_theme_changed(self, _theme: str) -> None:
        # Page widgets re-apply their own accent colours where needed.
        self.welcome_page.refresh()
        self._refresh_readiness_indicator()
        for page in (self.system_check_page, self.diagnostics_page, self.maintenance_page):
            if hasattr(page, "refresh"):
                page.refresh()

    # ------------------------------------------------------------------
    # Menus / settings side effects
    # ------------------------------------------------------------------

    def _set_theme(self, theme: str) -> None:
        settings = self.context.settings.copy()
        settings.general.theme = theme
        if self.context.apply_settings(settings, save=True, reason="theme"):
            self.context.notify(f"Theme set to {theme}.")

    def _toggle_advanced_mode(self, checked: bool) -> None:
        settings = self.context.settings.copy()
        settings.general.advanced_mode = checked
        self.context.apply_settings(settings, save=True, reason="advanced mode")
        self.context.notify("Advanced mode enabled." if checked else "Advanced mode disabled.")

    def _on_restart_required(self, message: str) -> None:
        show_info(
            self,
            message,
            "Close and reopen the application to use the new folder. Nothing has been moved.",
        )

    def _reload_current_page(self) -> None:
        page = self.stack.currentWidget()
        for name, candidate in self._pages.items():
            if candidate is page and hasattr(page, "refresh"):
                page.refresh()  # type: ignore[attr-defined]
                break
        self.context.notify("Page reloaded.")

    def _copy_diagnostics(self) -> None:
        from .notifications import copy_to_clipboard

        copy_to_clipboard(self.context.diagnostics_text())
        self.context.notify("Diagnostics copied to the clipboard.")

    def _show_about(self) -> None:
        from ..core.version import APP_STAGE_LABEL, PRIMARY_PLATFORM, full_version_string
        from ..core.paths import DATA_SUBDIRECTORIES

        log_file = logging_setup.active_log_file()
        text = (
            f"{full_version_string()}\n\n"
            f"Local, offline video production studio.\n"
            f"Target machine: {PRIMARY_PLATFORM}\n\n"
            f"Build stage: {APP_STAGE_LABEL}\n\n"
            f"Data folder: {self.context.paths.data_root}\n"
            f"Log file: {log_file or 'not available'}\n"
            f"Session: {logging_setup.current_session_id()}\n"
            f"Folders: {', '.join(DATA_SUBDIRECTORIES)}\n"
        )
        show_info(self, "About Motion Graphics Studio", text, title="About")

    # ------------------------------------------------------------------
    # Geometry / page memory
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        window = self.context.settings.window
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None

        width = max(METRICS.window_min_width, int(window.width))
        height = max(METRICS.window_min_height, int(window.height))
        if available is not None:
            width = min(width, available.width())
            height = min(height, available.height())
        self.resize(QSize(width, height))

        if window.x >= 0 and window.y >= 0 and available is not None:
            # Only restore a position that is still on a visible screen
            # (monitors get unplugged; a window must never open off-screen).
            from PySide6.QtCore import QRect

            position = QRect(int(window.x), int(window.y), width, height)
            if available.intersects(position):
                self.move(int(window.x), int(window.y))
        if window.maximized:
            self.showMaximized()

        if window.last_page in self._pages:
            self.show_page(window.last_page)

    def _remember_page(self, key: str) -> None:
        """Remember the page in memory only; it is written with the rest of the
        settings on exit so page switching never touches the disk (section 48)."""
        self._pending_page = key

    def _save_window_state(self) -> None:
        try:
            settings = self.context.settings.copy()
            geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
            settings.window.width = max(METRICS.window_min_width, geometry.width())
            settings.window.height = max(METRICS.window_min_height, geometry.height())
            settings.window.x = geometry.x()
            settings.window.y = geometry.y()
            settings.window.maximized = self.isMaximized()
            pending_page = getattr(self, "_pending_page", None)
            if pending_page:
                settings.window.last_page = pending_page
            self.context.apply_settings(settings, save=True, reason="window state")
        except Exception as exc:  # noqa: BLE001 - never block shutdown on this
            log_event(Event.WARNING, "Window state could not be saved", logger=LOGGER, reason=str(exc))

    # ------------------------------------------------------------------
    # Resource monitor
    # ------------------------------------------------------------------

    def _start_resource_timer(self) -> None:
        self._resource_timer = QTimer(self)
        self._resource_timer.setInterval(2000)
        self._resource_timer.timeout.connect(self._update_resource_label)
        if self.context.settings.general.show_resource_monitor:
            self._resource_timer.start()
        self._update_resource_label()

    def _update_resource_label(self) -> None:
        if not self.context.settings.general.show_resource_monitor:
            self.resource_label.setText("")
            return
        cpu = system_cpu_percent()
        process_cpu = process_cpu_percent()
        if cpu is None and process_cpu is None:
            self.resource_label.setText("")
            self.resource_label.setToolTip(
                "Install the optional 'psutil' package to see live CPU and memory usage."
            )
            return
        parts = []
        if cpu is not None:
            parts.append(f"CPU {cpu:.0f}%")
        if process_cpu is not None:
            parts.append(f"App {process_cpu:.0f}%")
        from ..core.env import current_memory_usage
        from ..core.atomicio import human_size

        usage = current_memory_usage()
        if usage is not None:
            parts.append(f"RAM {human_size(usage[0])}")
        self.resource_label.setText("   ".join(parts))
        self.resource_label.setToolTip("CPU use of the whole computer and of this application, and its memory use.")

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    def begin_startup_checks(self) -> None:
        """Run the readiness check shortly after the window appears.

        Deferred with a single-shot timer so the window paints first - startup
        never blocks on the check (sections 46/48).
        """
        QTimer.singleShot(250, self._begin_startup_work)

    def _begin_startup_work(self) -> None:
        """Offer recovery data first, then run the readiness check.

        Both happen after the window is painted, so startup never blocks on a
        scan of the projects folder (sections 13 and 36).
        """
        controller = self.context.projects
        if controller is not None:
            controller.check_recovery(self)
        self.run_system_check(deep=False)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Close safely: confirm, cancel jobs, save state, flush logs."""
        if self._closing:
            event.accept()
            return

        controller = self.context.projects
        if controller is not None and not controller.close(self, reason="closing the application"):
            event.ignore()
            return

        active = self.jobs.active_jobs()
        if active:
            names = ", ".join(job.title for job in active[:3])
            more = f" and {len(active) - 3} more" if len(active) > 3 else ""
            if not ask_confirm(
                self,
                "A task is still running." if len(active) == 1 else f"{len(active)} tasks are still running.",
                f"Still running: {names}{more}.\n\n"
                "Closing now will cancel them. Nothing in your project will be lost.",
                confirm_label="Cancel tasks and close",
                dangerous=True,
            ):
                event.ignore()
                return

        if self.context.settings_dirty and self.context.settings.general.confirm_on_exit:
            if not ask_confirm(
                self,
                "Close the application?",
                "Settings changes that have not been applied will be lost. "
                "Press Apply on the Settings page first if you want to keep them.",
                confirm_label="Close anyway",
            ):
                event.ignore()
                return

        self._closing = True
        log_event(Event.APP_EXIT, "Closing the application", logger=LOGGER, active_jobs=len(active))

        if active:
            self.jobs.cancel_all("Cancelled because the application is closing.")
        stragglers = self.jobs.shutdown(timeout_ms=8000)
        if stragglers:
            log_event(Event.WARNING, "Some tasks did not stop before closing", logger=LOGGER, count=len(stragglers))

        self._resource_timer.stop()
        if self.context.projects is not None:
            self.context.projects.shutdown()
        self._save_window_state()
        self.context.jobs.forget_finished()
        logging_setup.get_logger().info("Application closed cleanly")
        logging_setup.shutdown_logging()
        event.accept()
