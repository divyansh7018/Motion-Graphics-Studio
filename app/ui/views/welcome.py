"""Dashboard: recent projects, new project, open project, system status.

This is the landing page (directive section 3).  It shows real data only -
recent projects come from the recent list on disk with a cached thumbnail, and
anything not built yet is labelled as such instead of being a dead button
(sections 3 and 54).

The class is still called ``WelcomePage`` so the Stage A page key ``welcome``
and its tests keep working; the page itself is now the dashboard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.version import APP_STAGE_LABEL, APP_VERSION, PRIMARY_PLATFORM
from ...project.thumbnails import thumbnail_path
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page

#: Card grid columns by available width.
CARD_WIDTH = 268


class ProjectCard(QWidget):
    """One recent project on the dashboard."""

    open_requested = Signal(object)
    reveal_requested = Signal(object)
    remove_requested = Signal(object)
    delete_requested = Signal(object)
    favorite_requested = Signal(object)

    def __init__(self, entry, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setFixedWidth(CARD_WIDTH)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.sm)

        self.thumb = QLabel()
        self.thumb.setFixedSize(QSize(244, 137))
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setObjectName("Thumbnail")
        self.thumb.setText("No preview yet")
        layout.addWidget(self.thumb)

        name_row = QHBoxLayout()
        self.name = QLabel(entry.name or entry.folder.name)
        font = self.name.font()
        font.setBold(True)
        self.name.setFont(font)
        self.name.setWordWrap(True)
        name_row.addWidget(self.name, 1)
        self.star = QToolButton()
        self.star.setText("★" if entry.favorite else "☆")
        self.star.setToolTip("Favourite" if entry.favorite else "Mark as favourite")
        self.star.setAutoRaise(True)
        self.star.clicked.connect(lambda: self.favorite_requested.emit(self.entry))
        name_row.addWidget(self.star)
        layout.addLayout(name_row)

        self.meta = HintLabel("")
        layout.addWidget(self.meta)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.xs)
        open_button = QPushButton("Open")
        mark_primary(open_button)
        open_button.clicked.connect(lambda: self.open_requested.emit(self.entry))
        menu_button = QToolButton()
        menu_button.setText("⋯")
        menu_button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(menu_button)
        menu.addAction("Reveal in folder", lambda: self.reveal_requested.emit(self.entry))
        menu.addAction("Remove from recent list", lambda: self.remove_requested.emit(self.entry))
        menu.addSeparator()
        menu.addAction("Delete project…", lambda: self.delete_requested.emit(self.entry))
        menu_button.setMenu(menu)
        buttons.addWidget(open_button, 1)
        buttons.addWidget(menu_button)
        layout.addLayout(buttons)

        self.refresh(entry)

    def refresh(self, entry) -> None:
        self.entry = entry
        self.name.setText(("★ " if entry.favorite else "") + (entry.name or entry.folder.name))
        self.star.setText("★" if entry.favorite else "☆")
        duration = entry.duration_seconds
        duration_text = f"{duration:.0f}s" if duration else "no duration yet"
        self.meta.setText(
            f"{entry.channel_name or 'no channel'} · {entry.resolution_label()}\n"
            f"Scenes: {entry.scenes} · {duration_text}\n"
            f"Modified: {entry.modified_at or 'unknown'} · {entry.status_label()}"
        )
        self.set_missing(not entry.folder.is_dir())

    def set_missing(self, missing: bool) -> None:
        self.name.setStyleSheet("color: #b00020;" if missing else "")
        if missing:
            self.meta.setText(self.meta.text() + "\nFolder not found at the recorded location.")

    def load_thumbnail(self, image_path: Optional[Path]) -> None:
        if image_path is None or not Path(image_path).is_file():
            self.thumb.setText("No preview yet")
            return
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self.thumb.setText("No preview yet")
            return
        self.thumb.setPixmap(pixmap.scaled(self.thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class WelcomePage(Page):
    """Dashboard: recent projects plus the two ways to start working."""

    open_system_check = Signal()
    open_settings = Signal()
    open_data_folder = Signal()
    open_output_folder = Signal()
    new_project = Signal()
    open_project = Signal()
    open_browser = Signal()
    open_project_path = Signal(object)          # Path

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Dashboard",
            "Start a new video, reopen a recent project, or check that this PC is ready.",
            parent,
        )
        self.context = context
        self.cards: list[ProjectCard] = []
        self._thumb_cache: dict[str, object] = {}

        self._build_project_card()
        self._build_recent_card()
        self._build_status_card()
        self._build_workflow_card()
        self._build_folders_card()
        self.refresh()

    # -- cards -------------------------------------------------------------

    def _build_project_card(self) -> None:
        card = self.add_card("Start working")
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.new_button = QPushButton("New project")
        mark_primary(self.new_button)
        self.new_button.setToolTip("Nine short steps: name, channel, format, quality, voice.")
        self.new_button.clicked.connect(self.new_project.emit)
        self.open_button = QPushButton("Open project…")
        self.open_button.clicked.connect(self.open_project.emit)
        self.browser_button = QPushButton("Project browser")
        self.browser_button.clicked.connect(self.open_browser.emit)
        row.addWidget(self.new_button)
        row.addWidget(self.open_button)
        row.addWidget(self.browser_button)
        row.addStretch(1)
        card.body().addLayout(row)
        card.add(HintLabel("Everything runs on this computer - no account, no internet connection, no GPU."))

    def _build_recent_card(self) -> None:
        card = self.add_card("Recent projects")
        controls = QHBoxLayout()
        controls.setSpacing(METRICS.sm)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search recent projects")
        self.search_edit.setMaximumWidth(280)
        self.search_edit.textChanged.connect(self.refresh)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Recently modified", "Name", "Channel"])
        self.sort_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.search_edit, 1)
        controls.addWidget(self.sort_combo)
        card.body().addLayout(controls)

        self.grid = QGridLayout()
        self.grid.setSpacing(METRICS.md)
        card.body().addLayout(self.grid)

        self.empty_hint = HintLabel("No projects yet - press “New project” to create the first one.")
        card.add(self.empty_hint)

    def _build_status_card(self) -> None:
        card = self.add_card("This build")
        self.status_grid = KeyValueGrid()
        card.add(self.status_grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.check_button = QPushButton("Run system check")
        mark_primary(self.check_button)
        self.check_button.clicked.connect(self.open_system_check.emit)
        buttons.addWidget(self.check_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings.emit)
        buttons.addWidget(self.settings_button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

    def _build_workflow_card(self) -> None:
        card = self.add_card("The local workflow")
        hint = HintLabel(
            "Everything below runs on this computer. Items marked 'later stage' are not built yet - "
            "they will appear as they are finished and tested."
        )
        card.add(hint)

        self.workflow_rows: list[QLabel] = []
        steps = [
            ("Project creation, settings, recovery", "built (stage B)"),
            ("Script", "stored as text; editing UI later stage"),
            ("Kokoro voice", "later stage (C)"),
            ("Voice preview", "later stage (C)"),
            ("Images", "later stage (H)"),
            ("Scenes and storyboard", "later stage (D)"),
            ("Preview", "later stage (D)"),
            ("Audio mixing", "later stage (E)"),
            ("Rendering to MP4 (FFmpeg)", "later stage (F)"),
            ("Quality control and output", "later stage (G)"),
            ("Video library", "later stage (G)"),
        ]
        grid = QVBoxLayout()
        grid.setSpacing(2)
        for name, state in steps:
            row = QHBoxLayout()
            bullet = QLabel("•")
            bullet.setFixedWidth(14)
            row.addWidget(bullet)
            label = QLabel(name)
            row.addWidget(label)
            row.addStretch(1)
            state_label = QLabel(state)
            state_label.setObjectName("Hint")
            row.addWidget(state_label)
            wrapper = QWidget()
            wrapper.setLayout(row)
            grid.addWidget(wrapper)
            self.workflow_rows.append(state_label)
        card.body().addLayout(grid)

        note = HintLabel(
            "Implemented in this build: application shell, settings, folder management, logging, "
            "background jobs with cancellation, the system check, and the full project lifecycle "
            "(create, open, save, autosave, recovery, duplicate, rename, delete)."
        )
        card.add(note)

    def _build_folders_card(self) -> None:
        card = self.add_card("Folders")
        self.folders_grid = KeyValueGrid()
        card.add(self.folders_grid)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        data_button = QPushButton("Open data folder")
        data_button.clicked.connect(self.open_data_folder.emit)
        row.addWidget(data_button)
        output_button = QPushButton("Open output folder")
        output_button.clicked.connect(self.open_output_folder.emit)
        row.addWidget(output_button)
        row.addStretch(1)
        card.body().addLayout(row)

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        context = self.context
        paths = context.paths

        self.status_grid.clear()
        self.status_grid.add("Application", f"Motion Graphics Studio {APP_VERSION}")
        self.status_grid.add("Build stage", APP_STAGE_LABEL)
        self.status_grid.add("Target machine", PRIMARY_PLATFORM)
        self.status_grid.add("Project schema", f"v{self._schema_version()}")
        self.status_grid.add("Session", self._session_text())
        self.status_grid.add("System check", self._readiness_text())

        self.folders_grid.clear()
        self.folders_grid.add("Data folder", str(paths.data_root), monospace=True)
        self.folders_grid.add("Chosen because", paths.reason)
        self.folders_grid.add("Projects", str(paths.projects_dir), monospace=True)
        self.folders_grid.add("Output", str(paths.output_dir), monospace=True)
        self.folders_grid.add("Logs", str(paths.logs_dir), monospace=True)

        self._refresh_recent()

    def _refresh_recent(self) -> None:
        """Rebuild the recent project cards from the stored recent list."""
        controller = self.context.projects
        entries = controller.recent_projects() if controller is not None else []

        query = self.search_edit.text().strip().lower()
        sort_mode = self.sort_combo.currentIndex()
        if query:
            entries = [
                entry
                for entry in entries
                if query in f"{entry.name} {entry.folder.name} {entry.channel_name}".lower()
            ]
        if sort_mode == 1:
            entries = sorted(entries, key=lambda entry: entry.name.lower())
        elif sort_mode == 2:
            entries = sorted(entries, key=lambda entry: (entry.channel_name or "").lower())

        # Clear the old cards.
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                self.grid.removeWidget(widget)
                widget.deleteLater()
        self.cards.clear()

        columns = max(1, min(4, self.width() // CARD_WIDTH)) if self.width() else 3
        for index, entry in enumerate(entries):
            card = ProjectCard(entry)
            card.open_requested.connect(self._on_open)
            card.reveal_requested.connect(self._on_reveal)
            card.remove_requested.connect(self._on_remove)
            card.delete_requested.connect(self._on_delete)
            card.favorite_requested.connect(self._on_favorite)
            self.grid.addWidget(card, index // columns, index % columns)
            self.cards.append(card)
            self._load_thumbnail(card)

        self.empty_hint.setVisible(not entries)
        self.empty_hint.setText(
            "No projects match that search." if query else "No projects yet - press “New project” to create the first one."
        )

    def _load_thumbnail(self, card: ProjectCard) -> None:
        """Use a cached thumbnail when there is one; never render a video for it."""
        entry = card.entry
        if not entry.folder:
            return
        # Thumbnails are cached under the folder name: they are previews of the
        # project, never a frame of a finished render (section 19).
        cached = thumbnail_path(self.context.paths, entry.folder.name)
        if cached.is_file():
            card.load_thumbnail(cached)
            return
        card.load_thumbnail(None)
        self._thumb_cache[str(entry.folder)] = cached

    # -- card actions ------------------------------------------------------

    def _on_open(self, entry) -> None:
        if not entry.folder.is_dir():
            from ..notifications import show_warning

            show_warning(
                self,
                "That project folder is not there any more.",
                f"Looked for: {entry.folder}\n\n"
                "If the project was moved, open it from its new folder; otherwise remove it "
                "from the recent list.",
            )
            return
        self.open_project_path.emit(entry.folder)

    def _on_reveal(self, entry) -> None:
        from ..notifications import open_path_in_explorer

        open_path_in_explorer(entry.folder)

    def _on_remove(self, entry) -> None:
        if self.context.projects is not None:
            self.context.projects.remove_from_recent(entry.folder)
            self._refresh_recent()

    def _on_delete(self, entry) -> None:
        if self.context.projects is not None:
            self.context.projects.delete_project(self, entry.folder, entry.name or entry.folder.name)
            self._refresh_recent()

    def _on_favorite(self, entry) -> None:
        if self.context.projects is not None:
            self.context.projects.toggle_favorite(entry.folder)
            self._refresh_recent()

    # -- helpers -----------------------------------------------------------

    def _schema_version(self) -> int:
        from ...core.version import PROJECT_SCHEMA_VERSION

        return PROJECT_SCHEMA_VERSION

    def _session_text(self) -> str:
        from ...core.logging_setup import current_session_id

        return current_session_id()

    def _readiness_text(self) -> str:
        report = self.context.last_report
        if report is None:
            return "not run yet"
        return report.headline()


# ``WelcomeView`` keeps its Stage A name for existing imports/tests.
WelcomeView = WelcomePage

__all__ = ["ProjectCard", "WelcomePage", "WelcomeView"]
