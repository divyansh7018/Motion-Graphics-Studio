"""The Storyboard page (Stage D).

The storyboard shows one card per scene with a real thumbnail, and gives the
beginner a small, safe set of actions: add a scene from the registry, duplicate,
rename, reorder and delete.  A detail panel shows the selected scene's timing
(which follows the narration) and any validation issues.

Rules this page follows:

* Thumbnails and previews are rendered by background jobs - nothing pixel-heavy
  ever runs on the Qt thread, so the window stays responsive.
* The page never invents scenes: with no project open, or an empty project, it
  says so instead of showing fake content.
* Add/duplicate/rename/reorder/delete are the project service's single
  undoable edits, so Ctrl+Z works exactly as the user expects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QPlainTextEdit,
)

from app.scene.storyboard import build_rows
from app.scene import jobs as scene_jobs
from app.scene.templates import default_templates_registered, template_summaries
from app.scene.timing import format_duration

from ..theme import METRICS
from ..widgets.common import KeyValueGrid, Page


class StoryboardPage(Page):
    """Arrange scenes into a sequence, with live thumbnails and timing."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(
            "Storyboard",
            "Arrange your scenes into a sequence. Scene length follows the narration when one is ready.",
            parent,
        )
        self.context = context
        self._rows: list = []
        self._storyboard_job: Optional[str] = None
        self._preview_job: Optional[str] = None
        self._loading = False

        default_templates_registered()
        self._build_toolbar_card()
        self._build_strip_card()
        self._build_detail_card()
        self.refresh()

    # -- access ----------------------------------------------------------

    @property
    def controller(self):
        return self.context.projects

    @property
    def project(self):
        return self.controller.project if self.controller is not None else None

    @property
    def project_dir(self) -> Optional[Path]:
        layout = self.controller.layout if self.controller is not None else None
        return Path(layout.root) if layout is not None else None

    # -- cards -----------------------------------------------------------

    def _build_toolbar_card(self) -> None:
        card = self.add_card("Add and arrange")
        self.template_combo = QComboBox()
        for summary in template_summaries():
            self.template_combo.addItem(summary["label"], summary["key"])
            index = self.template_combo.count() - 1
            self.template_combo.setItemData(index, summary["description"], Qt.ToolTipRole)

        self.add_button = QPushButton("Add scene")
        self.add_button.clicked.connect(self._add_scene)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self._duplicate_scene)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self._rename_scene)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete_scene)
        self.up_button = QPushButton("\u2191")
        self.up_button.setToolTip("Move scene earlier")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("\u2193")
        self.down_button.setToolTip("Move scene later")
        self.down_button.clicked.connect(lambda: self._move(1))

        from PySide6.QtWidgets import QHBoxLayout

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        row.addWidget(QLabel("New scene:"))
        row.addWidget(self.template_combo, 1)
        row.addWidget(self.add_button)
        row.addWidget(self.duplicate_button)
        row.addWidget(self.rename_button)
        row.addWidget(self.up_button)
        row.addWidget(self.down_button)
        row.addWidget(self.delete_button)
        card.body().addLayout(row)

    def _build_strip_card(self) -> None:
        card = self.add_card("Scenes")
        self.strip = QListWidget()
        self.strip.setViewMode(QListWidget.IconMode)
        self.strip.setIconSize(QSize(192, 108))
        self.strip.setResizeMode(QListWidget.Adjust)
        self.strip.setSpacing(METRICS.md)
        self.strip.setMinimumHeight(180)
        self.strip.currentItemChanged.connect(self._on_selection_changed)
        card.add(self.strip)
        self.strip_hint = QLabel("")
        self.strip_hint.setObjectName("Hint")
        self.strip_hint.setWordWrap(True)
        card.add(self.strip_hint)

    def _build_detail_card(self) -> None:
        card = self.add_card("Selected scene")
        self.preview_label = QLabel("No scene selected.")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(240)
        self.preview_label.setStyleSheet("background: #101014;")
        card.add(self.preview_label)

        self.time_slider = QSlider(Qt.Horizontal)
        self.time_slider.setRange(0, 1000)
        self.time_slider.valueChanged.connect(self._on_time_changed)
        card.add(self.time_slider)

        self.detail_grid = KeyValueGrid()
        card.add(self.detail_grid)

        self.issues_edit = QPlainTextEdit()
        self.issues_edit.setReadOnly(True)
        self.issues_edit.setMaximumHeight(120)
        self.issues_edit.setPlaceholderText("No issues - this scene will render as shown.")
        card.add(self.issues_edit)

        self.render_button = QPushButton("Render preview")
        self.render_button.clicked.connect(self._submit_preview)
        card.add(self.render_button)

    # -- data ------------------------------------------------------------

    def refresh(self) -> None:
        """Reload rows from the project (no pixels) and refresh the strip."""
        self._loading = True
        try:
            project = self.project
            if project is None:
                self._rows = []
                self.strip.clear()
                self.strip_hint.setText("No project is open. Open or create one to start building scenes.")
                self._set_detail_empty("No project is open.")
                self._update_buttons()
                return
            self._rows, _timeline = build_rows(project)
            self._reload_strip()
            if not self._rows:
                self.strip_hint.setText(
                    "This project has no scenes yet. Pick a template above and choose “Add scene”.")
            else:
                self.strip_hint.setText(
                    f"{len(self._rows)} scene(s) - total {format_duration(sum(r.duration for r in self._rows))}.")
            self._update_buttons()
            self._show_details_for_selection()
        finally:
            self._loading = False

    def _reload_strip(self) -> None:
        selected = self._selected_id()
        self.strip.blockSignals(True)
        self.strip.clear()
        for row in self._rows:
            item = QListWidgetItem()
            item.setText(f"{row.index + 1}. {row.name}")
            item.setData(Qt.UserRole, row.scene_id)
            item.setToolTip(f"{row.type} - {format_duration(row.duration)} ({row.duration_source})")
            if row.thumbnail_path and Path(row.thumbnail_path).is_file():
                pixmap = QPixmap(row.thumbnail_path)
                if not pixmap.isNull():
                    item.setIcon(pixmap.scaled(192, 108, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.strip.addItem(item)
        self.strip.blockSignals(False)
        if selected:
            self._select_by_id(selected)

    def ensure_storyboard(self) -> None:
        """Called when the user opens the page: kick a thumbnail render if needed."""
        self.refresh()
        if self.project is not None and self._rows and self._storyboard_job is None:
            if not all(Path(r.thumbnail_path).is_file() for r in self._rows if r.thumbnail_path):
                self._submit_storyboard()

    # -- jobs ------------------------------------------------------------

    def _submit_storyboard(self) -> None:
        if self.project is None or self._storyboard_job is not None:
            return
        payload = {"project": self.project, "project_dir": str(self.project_dir or "")}
        spec = scene_jobs.storyboard_spec(payload, long_edge=480)
        spec.paths = self.context.paths  # so the worker can reach the preview folder
        job = self.context.jobs.submit(spec)
        if job is not None:
            self._storyboard_job = job.id

    def on_storyboard_finished(self, result) -> None:
        self._storyboard_job = None
        if result.failed or result.cancelled:
            return
        if result.value is None:
            return
        # Attach the produced thumbnail paths back onto our rows and refresh.
        produced = {row["index"]: row["thumbnail_path"] for row in result.value.get("rows", [])}
        for row in self._rows:
            if row.index in produced:
                row.thumbnail_path = produced[row.index]
        self._reload_strip()
        self._show_details_for_selection()

    def _submit_preview(self, time: Optional[float] = None) -> None:
        scene_id = self._selected_id()
        if self.project is None or scene_id is None or self._preview_job is not None:
            return
        moment = time if time is not None else self._slider_time()
        payload = {"project": self.project, "project_dir": str(self.project_dir or "")}
        spec = scene_jobs.scene_preview_spec(payload, scene_id=scene_id, time=moment)
        spec.paths = self.context.paths
        job = self.context.jobs.submit(spec)
        if job is not None:
            self._preview_job = job.id
            self.render_button.setEnabled(False)

    def on_preview_finished(self, result) -> None:
        self._preview_job = None
        self.render_button.setEnabled(True)
        if result.failed or result.cancelled or result.value is None:
            return
        path = result.value.get("path")
        if not path or not Path(path).is_file():
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        scaled = pixmap.scaledToWidth(min(640, pixmap.width()), Qt.SmoothTransformation)
        self.preview_label.setPixmap(scaled)

    # -- timing slider ---------------------------------------------------

    def _slider_time(self) -> float:
        row = self._selected_row()
        if row is None:
            return 0.0
        fraction = self.time_slider.value() / 1000.0
        return max(0.0, min(row.duration, fraction * row.duration))

    def _on_time_changed(self, _value: int) -> None:
        # Scrubbing updates the label only; rendering happens on release or button.
        row = self._selected_row()
        if row is not None:
            self.preview_label.setToolTip(f"t = {self._slider_time():.2f}s of {row.duration:.2f}s")

    # -- selection and details ------------------------------------------

    def _selected_id(self) -> Optional[str]:
        item = self.strip.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _selected_row(self):
        scene_id = self._selected_id()
        return next((r for r in self._rows if r.scene_id == scene_id), None)

    def _select_by_id(self, scene_id: str) -> None:
        for index in range(self.strip.count()):
            if self.strip.item(index).data(Qt.UserRole) == scene_id:
                self.strip.setCurrentItem(self.strip.item(index))
                return

    def _on_selection_changed(self, _current, _previous) -> None:
        if self._loading:
            return
        self._show_details_for_selection()

    def _show_details_for_selection(self) -> None:
        row = self._selected_row()
        if row is None:
            self._set_detail_empty("Select a scene to see its details.")
            return
        scene = self.project.scene_by_id(row.scene_id) if self.project else None
        self.detail_grid.clear()
        for label, value in (
            ("Name", row.name),
            ("Type", row.type),
            ("Position", f"#{row.index + 1} of {len(self._rows)}"),
            ("Starts at", f"{row.start:.2f}s"),
            ("Duration", f"{row.duration:.2f}s"),
            ("Length from", row.duration_source),
            ("Elements", str(row.element_count)),
        ):
            self.detail_grid.add(label, value)
        issues = []
        if scene is not None:
            from app.scene.validate import validate_scene
            from app.scene.storyboard import build_context

            context = build_context(self.project, project_dir=self.project_dir)
            validation = validate_scene(scene, canvas=context.canvas, ctx=context, project=self.project)
            issues = [f"[{issue.severity}] {issue.message}" for issue in validation.issues]
        self.issues_edit.setPlainText("\n".join(issues) if issues else "")

    def _set_detail_empty(self, message: str) -> None:
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(message)
        self.detail_grid.clear()
        self.issues_edit.setPlainText("")

    # -- actions ---------------------------------------------------------

    def _update_buttons(self) -> None:
        has_project = self.project is not None
        has_selection = self._selected_id() is not None
        self.add_button.setEnabled(has_project)
        self.template_combo.setEnabled(has_project)
        self.duplicate_button.setEnabled(has_project and has_selection)
        self.rename_button.setEnabled(has_project and has_selection)
        self.delete_button.setEnabled(has_project and has_selection)
        self.up_button.setEnabled(has_project and has_selection)
        self.down_button.setEnabled(has_project and has_selection)
        self.render_button.setEnabled(has_project and has_selection)

    def _add_scene(self) -> None:
        controller = self.controller
        if controller is None or not controller.is_open:
            return
        key = self.template_combo.currentData() or "blank"
        controller.service.add_scene_from_template(key)
        controller.service.save(reason="Added a scene")
        self.refresh()
        self._submit_storyboard()

    def _duplicate_scene(self) -> None:
        controller = self.controller
        scene_id = self._selected_id()
        if controller is None or scene_id is None:
            return
        controller.service.duplicate_scene(scene_id)
        controller.service.save(reason="Duplicated a scene")
        self.refresh()
        self._submit_storyboard()

    def _rename_scene(self) -> None:
        controller = self.controller
        scene_id = self._selected_id()
        if controller is None or scene_id is None:
            return
        current = self._selected_row().name if self._selected_row() else ""
        name, ok = QInputDialog.getText(self, "Rename scene", "Scene name:", text=current)
        if not ok or not name.strip():
            return
        controller.service.rename_scene(scene_id, name.strip())
        controller.service.save(reason="Renamed a scene")
        self.refresh()

    def _move(self, delta: int) -> None:
        controller = self.controller
        scene_id = self._selected_id()
        if controller is None or scene_id is None:
            return
        controller.service.move_scene_by(scene_id, delta)
        controller.service.save(reason="Reordered scenes")
        selected = self._selected_id()
        self.refresh()
        if selected:
            self._select_by_id(selected)

    def _delete_scene(self) -> None:
        controller = self.controller
        scene_id = self._selected_id()
        if controller is None or scene_id is None:
            return
        reply = QMessageBox.question(
            self, "Delete scene", "Delete this scene? You can undo with Ctrl+Z.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        controller.service.remove_scene(scene_id)
        controller.service.save(reason="Deleted a scene")
        self.refresh()
