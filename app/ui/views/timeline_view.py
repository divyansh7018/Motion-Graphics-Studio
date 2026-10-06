"""The Timeline page: when every scene runs, and whether that is safe to render.

There is exactly one timeline in this application, built by
:class:`app.scene.service.TimelineService` from the real scene content.  The
preview, the audio mix, the captions and the final render all read it, so this
page shows the same numbers the renderer will use - not a second opinion.

Two things are deliberately honest here:

* **The length column says where the length came from.**  "From the narration
  file" means it was measured from real audio.  "Set manually" or "Default
  length" means it was not, and the user can see that at a glance.
* **The check is the render's check.**  The engine runs the same service before
  drawing a frame, so an error listed here is exactly the error that will stop a
  render, and a warning is exactly the one it will let through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.render.jobs import timeline_check_spec
from app.scene.service import TimelineService, describe_timeline
from app.scene.timing import format_duration

from ..theme import mark_primary
from ..widgets.common import ButtonRow, HintLabel, KeyValueGrid, Page, Row


class TimelinePage(Page):
    """The sequence: real timings, real problems, no invented durations."""

    project_changed = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Timeline",
            "Every scene's start and length, worked out from your narration and "
            "transitions. This is the same timeline the final render uses.",
            parent,
        )
        self.context = context
        self.service = TimelineService()
        self._loading = False
        self._check_job: Optional[str] = None

        self._build_summary_card()
        self._build_table_card()
        self._build_scene_card()
        self._build_check_card()
        self.refresh()

    # -- access -----------------------------------------------------------

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

    # -- cards ------------------------------------------------------------

    def _build_summary_card(self) -> None:
        card = self.add_card("Summary")
        self.summary_grid = KeyValueGrid()
        self.summary_grid.set_value("Total length", "-")
        self.summary_grid.set_value("Scenes", "-")
        self.summary_grid.set_value("Frames", "-")
        self.summary_grid.set_value("Resolution", "-")
        self.summary_grid.set_value("Narration measured", "-")
        card.add(self.summary_grid)
        self.summary_hint = HintLabel("Open a project to see its timeline.")
        card.add(self.summary_hint)

    def _build_table_card(self) -> None:
        card = self.add_card("Scenes")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["#", "Scene", "Start", "End", "Length", "Length from", "Transitions"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (0, 2, 3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setMinimumHeight(240)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        card.add(self.table)
        card.add(HintLabel(
            "Disabled scenes are greyed and take no time. With overlapping "
            "transitions a scene starts while the previous one is still fading out."))

    def _build_scene_card(self) -> None:
        card = self.add_card("Selected scene")
        self.scene_name = QLabel("No scene selected.")
        self.scene_name.setObjectName("Hint")
        self.scene_name.setWordWrap(True)
        card.add(Row("Scene", self.scene_name))

        self.scene_enabled = QCheckBox("Include this scene in the video")
        self.scene_enabled.toggled.connect(self._on_scene_changed)
        card.add(Row("Enabled", self.scene_enabled))

        self.scene_duration = QDoubleSpinBox()
        self.scene_duration.setRange(0.1, 86400.0)
        self.scene_duration.setSingleStep(0.5)
        self.scene_duration.setDecimals(2)
        self.scene_duration.setSuffix(" s")
        self.scene_duration.valueChanged.connect(self._on_scene_changed)
        card.add(Row("Manual length", self.scene_duration))

        self.scene_note = HintLabel(
            "A scene with narration follows the measured audio length, so the "
            "manual length is only used when there is no narration.")
        card.add(self.scene_note)

    def _build_check_card(self) -> None:
        card = self.add_card("Check")
        self.check_button = QPushButton("Check the timeline")
        mark_primary(self.check_button)
        self.check_button.clicked.connect(self._check)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_job)
        self.cancel_button.setEnabled(False)
        card.add(ButtonRow([self.check_button, self.cancel_button]))

        self.check_hint = HintLabel(
            "Errors stop a render. Warnings are reported and you decide.")
        card.add(self.check_hint)

        self.issues_view = QPlainTextEdit()
        self.issues_view.setReadOnly(True)
        self.issues_view.setMaximumHeight(200)
        self.issues_view.setPlaceholderText("Problems found by the check appear here.")
        card.add(self.issues_view)

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the table from the project's real timings."""
        self._loading = True
        try:
            project = self.project
            if project is None:
                self.table.setRowCount(0)
                self.summary_grid.set_value("Total length", "-")
                self.summary_grid.set_value("Scenes", "-")
                self.summary_grid.set_value("Frames", "-")
                self.summary_grid.set_value("Resolution", "-")
                self.summary_grid.set_value("Narration measured", "-")
                self.summary_hint.setText("Open a project to see its timeline.")
                self.scene_name.setText("No scene selected.")
                self._set_scene_controls_enabled(False)
                return

            timeline = self.service.build(project)
            fmt = project.format
            measured = sum(1 for timing in timeline.timings
                           if timing.source == "narration")

            self.summary_grid.set_value("Total length", timeline.format_total())
            self.summary_grid.set_value("Scenes", str(timeline.scene_count))
            self.summary_grid.set_value(
                "Frames", f"{timeline.total_frames(int(fmt.fps))} at {int(fmt.fps)} fps")
            self.summary_grid.set_value(
                "Resolution", f"{int(fmt.width)}x{int(fmt.height)}")
            self.summary_grid.set_value(
                "Narration measured",
                f"{measured} of {timeline.scene_count} scene(s)"
                if timeline.scene_count else "No scenes yet")
            if timeline.scene_count and measured < timeline.scene_count:
                self.summary_hint.setText(
                    "Some scenes have no measured narration, so their length is a "
                    "manual or default value. Generate the narration for exact timing.")
            elif timeline.scene_count:
                self.summary_hint.setText(
                    "Every scene's length was measured from its narration audio.")
            else:
                self.summary_hint.setText("Add scenes on the Storyboard page.")

            self._reload_table(project, timeline)
            self._set_scene_controls_enabled(self.table.rowCount() > 0)
        finally:
            self._loading = False

    def _reload_table(self, project, timeline) -> None:
        """One row per scene in the project, not one per scene in the cut.

        ``build_timeline`` drops disabled scenes, so iterating the timings alone
        would make a switched-off scene vanish from this page with no
        explanation.  Walking the project's scenes instead means every scene is
        accounted for, and an excluded one says so.
        """
        self.table.setRowCount(0)
        for scene in project.scenes:
            timing = timeline.timing_for(str(scene.id))
            row = self.table.rowCount()
            self.table.insertRow(row)

            if timing is None:
                cells = ["-", scene.name or scene.id, "-", "-", "-",
                         "Excluded", "not in the video"]
            else:
                cells = [
                    str(timing.index + 1),
                    timing.name or timing.scene_id,
                    format_duration(timing.start),
                    format_duration(timing.end),
                    f"{timing.duration:.2f}s",
                    timing.source_label() + (" (raised)" if timing.raised else ""),
                    f"{timing.transition_in} / {timing.transition_out}",
                ]

            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, str(scene.id))
                if column == 0:
                    cell.setTextAlignment(Qt.AlignCenter)
                if timing is None:
                    cell.setForeground(Qt.gray)
                self.table.setItem(row, column, cell)

            if timing is None:
                self.table.item(row, 1).setToolTip(
                    "This scene is switched off, so it takes no time and is not "
                    "in the rendered video.")
            elif timing.raised:
                self.table.item(row, 5).setToolTip(
                    "This scene was raised to the minimum length.")

    def _set_scene_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.scene_enabled, self.scene_duration):
            widget.setEnabled(enabled)

    def _selected_scene(self):
        row = self.table.currentRow()
        if row < 0 or self.project is None:
            return None
        cell = self.table.item(row, 0)
        scene_id = cell.data(Qt.UserRole) if cell is not None else None
        if not scene_id:
            return None
        return self.project.scene_by_id(str(scene_id))

    def _on_selection_changed(self) -> None:
        if self._loading:
            return
        scene = self._selected_scene()
        if scene is None:
            self.scene_name.setText("No scene selected.")
            self._set_scene_controls_enabled(False)
            return
        self._loading = True
        try:
            self.scene_name.setText(f"{scene.name or scene.id}  ({scene.id})")
            self.scene_enabled.setChecked(bool(scene.enabled))
            self.scene_duration.setValue(float(scene.duration))
            has_narration = bool(str(getattr(scene.narration, "file", "") or ""))
            self.scene_note.setText(
                "This scene has narration, so its length follows the measured audio "
                f"({float(getattr(scene.narration, 'duration', 0.0)):.2f}s). The manual "
                "length is a fallback used only if the narration is removed."
                if has_narration else
                "No narration on this scene yet, so the manual length is what the "
                "timeline uses.")
        finally:
            self._loading = False
        self._set_scene_controls_enabled(True)

    def _on_scene_changed(self, *_args) -> None:
        if self._loading:
            return
        scene = self._selected_scene()
        if scene is None:
            return
        scene.enabled = self.scene_enabled.isChecked()
        scene.duration = float(self.scene_duration.value())
        self.project_changed.emit()
        # Timings depend on this scene, so the table is stale until rebuilt.
        self.refresh()

    # -- the check --------------------------------------------------------

    def _check(self) -> None:
        if self.project is None:
            self.check_hint.setText("Open a project first.")
            return
        if self._check_job is not None:
            self.check_hint.setText("A check is already running.")
            return
        spec = timeline_check_spec({"project": self.project,
                                    "project_dir": str(self.project_dir or "")})
        spec.paths = self.context.paths
        job = self.context.jobs.submit(spec)
        if job is None:
            self.check_hint.setText("A check is already running.")
            return
        self._check_job = job.id
        self.check_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.check_hint.setText("Checking the timeline…")

    def _cancel_job(self) -> None:
        if self._check_job:
            self.context.jobs.cancel(self._check_job, "Stopped by the user.")
            self.check_hint.setText("Cancelling…")

    def on_check_finished(self, result) -> None:
        self._check_job = None
        self.check_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if result.cancelled:
            self.check_hint.setText("The check was cancelled.")
            return
        if result.error is not None:
            self.check_hint.setText(f"The check failed: {result.error.title}")
            return
        value = result.value or {}
        issues = value.get("issues", [])
        errors = [item for item in issues if item.get("severity") == "error"]
        warnings = [item for item in issues if item.get("severity") != "error"]
        if errors:
            self.check_hint.setText(
                f"{len(errors)} error(s) will stop a render. Fix them first.")
        elif warnings:
            self.check_hint.setText(
                f"Safe to render, with {len(warnings)} warning(s) to look at.")
        else:
            self.check_hint.setText("The timeline is valid. Ready to render.")
        self.issues_view.setPlainText("\n".join(
            f"[{str(item.get('severity', 'warning')).upper()}] {item.get('code')}: "
            f"{item.get('message')}"
            + (f"\n        To do: {item.get('what_to_do')}"
               if item.get("what_to_do") else "")
            for item in issues) or "No problems found.")

    def timeline_text(self) -> str:
        """The readable timeline, used by the CLI-style summary and logs."""
        project = self.project
        if project is None:
            return "No project is open."
        return describe_timeline(self.service.build(project))


def _pair(left: QWidget, right: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(left)
    layout.addWidget(right)
    layout.addStretch(1)
    return holder
