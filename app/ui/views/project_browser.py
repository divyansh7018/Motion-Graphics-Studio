"""Project browser: every project on this machine, with real actions.

The dashboard shows the recent list; this page shows what is actually in the
projects folder, so a project that was copied in by hand still appears
(directive sections 17-20).  Scanning is a background job: with many projects
the window must stay responsive (section 36).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...jobs.keys import JobKeys
from ...jobs.spec import JobContext, JobSpec
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, Page


class ProjectBrowserPage(Page):
    """Searchable, sortable list of every project in the projects folder."""

    open_project_path = Signal(object)   # Path
    new_project = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__("Projects", "Every project stored on this computer.", parent)
        self.context = context
        self.rows: list[dict] = []

        self._scanning_folder: Optional[Path] = None
        self._build_controls()
        self._build_table()
        # Job results arrive on the manager, filtered by key and folder.
        self.context.jobs.job_finished.connect(self._on_job_finished)
        self.status = HintLabel("Press “Scan projects folder” to list the projects on this machine.")
        self._layout().addWidget(self.status)

    def _layout(self) -> QVBoxLayout:
        layout = self.layout()
        return layout if isinstance(layout, QVBoxLayout) else QVBoxLayout()

    # -- widgets -----------------------------------------------------------

    def _build_controls(self) -> None:
        card = self.add_card("Find a project")
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name, channel or folder")
        self.search_edit.textChanged.connect(self._apply_filters)

        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All projects", "Favourites", "Recently used", "Missing folder"])
        self.filter_combo.currentIndexChanged.connect(self._apply_filters)

        self.scan_button = QPushButton("Scan projects folder")
        mark_primary(self.scan_button)
        self.scan_button.clicked.connect(self.scan)

        self.new_button = QPushButton("New project")
        self.new_button.clicked.connect(self.new_project.emit)

        row.addWidget(self.search_edit, 1)
        row.addWidget(self.filter_combo)
        row.addWidget(self.scan_button)
        row.addWidget(self.new_button)
        card.body().addLayout(row)

    def _build_table(self) -> None:
        card = self.add_card("Projects")
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Name", "Channel", "Format", "Scenes", "Modified", "Folder"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        card.add(self.table)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        open_button = QPushButton("Open")
        open_button.clicked.connect(self._open_selected)
        duplicate_button = QPushButton("Duplicate…")
        duplicate_button.clicked.connect(self._duplicate_selected)
        rename_button = QPushButton("Rename…")
        rename_button.clicked.connect(self._rename_selected)
        reveal_button = QPushButton("Reveal in folder")
        reveal_button.clicked.connect(self._reveal_selected)
        remove_button = QPushButton("Remove from recent")
        remove_button.clicked.connect(self._remove_selected)
        delete_button = QPushButton("Delete…")
        delete_button.clicked.connect(self._delete_selected)
        for button in (
            open_button,
            duplicate_button,
            rename_button,
            reveal_button,
            remove_button,
            delete_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

    # -- scanning ----------------------------------------------------------

    def scan(self) -> None:
        """List the projects folder in a background job (never on the UI thread)."""
        projects_dir = self.context.paths.projects_dir
        service = self.context.projects.service

        def body(context: JobContext) -> list:
            return service.index_projects(projects_dir)

        spec = JobSpec(
            key=JobKeys.PROJECT_LOAD,
            title="Scanning projects",
            description=f"Looking through {projects_dir}",
            body=body,
            settings=self.context.settings,
            paths=self.context.paths,
            payload={"folder": str(projects_dir)},
        )
        job = self.context.jobs.submit(spec)
        if job is None:
            self.status.setText("A scan is already running.")
            return
        self._scanning_folder = projects_dir
        self.status.setText("Scanning the projects folder…")

    def _on_job_finished(self, result) -> None:
        if result.key != JobKeys.PROJECT_LOAD:
            return
        if result.failed or result.cancelled or result.value is None:
            reason = result.error.title if result.error is not None else "cancelled"
            self.status.setText(
                f"The scan did not finish: {reason}. The previous list is unchanged."
            )
            self._scanning_folder = None
            return
        self.rows = list(result.value)
        self._scanning_folder = None
        self._apply_filters()

    # -- filtering ---------------------------------------------------------

    def _apply_filters(self, *_args) -> None:
        query = self.search_edit.text().strip().lower()
        mode = self.filter_combo.currentIndex()
        rows = list(self.rows)

        if query:
            rows = [
                row
                for row in rows
                if query in f"{row['name']} {row['channel']} {row['path']}".lower()
            ]
        if mode == 1:
            rows = [row for row in rows if row.get("favorite")]
        elif mode == 2:
            rows = [row for row in rows if row.get("recent")]
        elif mode == 3:
            rows = [row for row in rows if not row.get("exists", True)]

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for row in rows:
            index = self.table.rowCount()
            self.table.insertRow(index)
            name = ("★ " if row.get("favorite") else "") + str(row["name"])
            if not row.get("exists", True):
                name += "  (folder missing)"
            values = (
                name,
                str(row.get("channel") or ""),
                str(row.get("format") or ""),
                str(row.get("scenes", "")),
                str(row.get("modified") or ""),
                str(row.get("path") or ""),
            )
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.setSortingEnabled(True)
        self.status.setText(
            f"{self.table.rowCount()} project(s) shown of {len(self.rows)} found."
            if self.rows
            else "No projects found in the projects folder."
        )

    # -- actions -----------------------------------------------------------

    def _selected_path(self) -> Optional[Path]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 5)
        return Path(item.text()) if item is not None else None

    def _require_selection(self) -> Optional[Path]:
        path = self._selected_path()
        if path is None:
            self.status.setText("Select a project row first.")
        return path

    def _open_selected(self) -> None:
        path = self._require_selection()
        if path is not None:
            self.open_project_path.emit(path)

    def _duplicate_selected(self) -> None:
        path = self._require_selection()
        if path is None:
            return
        controller = self.context.projects
        if controller is not None:
            controller.duplicate(self, path)
            self.scan()

    def _rename_selected(self) -> None:
        path = self._require_selection()
        if path is None:
            return
        from PySide6.QtWidgets import QInputDialog

        current = Path(path).name
        name, ok = QInputDialog.getText(self, "Rename project", "New name:", text=current)
        if not ok or not name.strip() or name.strip() == current:
            return
        controller = self.context.projects
        if controller is None:
            return
        # The service renames the folder of the project it has open, so this
        # opens the chosen project, renames it and keeps it open.
        from ...core.errors import AppError
        from ..notifications import show_error

        try:
            if not controller.is_open or controller.layout.root != Path(path):
                controller.open_path(Path(path), self)
            controller.service.rename_project_folder(name.strip())
        except AppError as exc:
            show_error(self, exc.friendly(), "The folder could not be renamed")
            return
        self.status.setText(f"Folder renamed to “{name.strip()}”.")
        self.open_project_path.emit(controller.layout.root)
        self.scan()

    def _reveal_selected(self) -> None:
        path = self._require_selection()
        if path is not None:
            from ..notifications import open_path_in_explorer

            open_path_in_explorer(Path(path))

    def _remove_selected(self) -> None:
        path = self._require_selection()
        controller = self.context.projects
        if path is not None and controller is not None:
            controller.remove_from_recent(Path(path))
            self.scan()

    def _delete_selected(self) -> None:
        path = self._require_selection()
        controller = self.context.projects
        if path is None or controller is None:
            return
        controller.delete_project(self, Path(path), Path(path).name)
        self.scan()

    def refresh(self) -> None:
        """Called when the page is shown; keeps the list without a re-scan."""
        if not self.rows:
            self.scan()


__all__ = ["ProjectBrowserPage"]
