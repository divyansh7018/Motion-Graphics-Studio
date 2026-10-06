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
        self._build_editor_card()
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
        self.toggle_button = QPushButton("Disable")
        self.toggle_button.setToolTip("Enable or disable this scene in the final cut")
        self.toggle_button.clicked.connect(self._toggle_enabled)
        self.lock_button = QPushButton("Lock")
        self.lock_button.setToolTip("Lock or unlock this scene against accidental edits")
        self.lock_button.clicked.connect(self._toggle_locked)

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
        row.addWidget(self.toggle_button)
        row.addWidget(self.lock_button)
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

    def _build_editor_card(self) -> None:
        """The scene editor: element list on the left, property inspector right.

        Selecting an element shows only the properties that apply to it, and
        every change is a single undoable edit through the project service, so
        the editor and Ctrl+Z stay in step (directive section 36).
        """
        from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout

        card = self.add_card("Scene editor")
        body = QHBoxLayout()
        body.setSpacing(METRICS.md)

        left = QVBoxLayout()
        left.addWidget(QLabel("Elements (top of list = front)"))
        self.element_list = QListWidget()
        self.element_list.setMinimumHeight(160)
        self.element_list.currentItemChanged.connect(self._on_element_selected)
        left.addWidget(self.element_list)

        element_actions = QHBoxLayout()
        self.el_duplicate = QPushButton("Duplicate")
        self.el_duplicate.clicked.connect(self._duplicate_element)
        self.el_front = QPushButton("To front")
        self.el_front.clicked.connect(lambda: self._reorder_element("front"))
        self.el_back = QPushButton("To back")
        self.el_back.clicked.connect(lambda: self._reorder_element("back"))
        self.el_lock = QPushButton("Lock")
        self.el_lock.clicked.connect(self._toggle_element_lock)
        self.el_delete = QPushButton("Delete")
        self.el_delete.clicked.connect(self._delete_element)
        for button in (self.el_duplicate, self.el_front, self.el_back, self.el_lock, self.el_delete):
            element_actions.addWidget(button)
        left.addLayout(element_actions)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("Properties"))
        self.inspector = KeyValueGrid()
        right.addWidget(self.inspector)
        right.addWidget(QLabel("Text"))
        self.element_text = QLineEdit()
        self.element_text.setPlaceholderText("Selected element's text")
        self.element_text.editingFinished.connect(self._apply_element_text)
        right.addWidget(self.element_text)
        self.inspector_hint = QLabel("Select an element to edit its properties.")
        self.inspector_hint.setObjectName("Hint")
        self.inspector_hint.setWordWrap(True)
        right.addWidget(self.inspector_hint)
        body.addLayout(right, 1)

        card.body().addLayout(body)

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
            self._rows, _timeline = build_rows(project, validate=True)
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
            scene = self.project.scene_by_id(row.scene_id) if self.project else None
            enabled = bool(getattr(scene, "enabled", True)) if scene else True
            locked = bool(getattr(scene, "locked", False)) if scene else False
            narration = "narrated" if row.narration_duration > 0 else "no narration"
            flags = []
            if not enabled:
                flags.append("disabled")
            if locked:
                flags.append("locked")
            if row.errors:
                flags.append(f"{row.errors} error(s)")
            elif row.issues:
                flags.append(f"{row.issues} warning(s)")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            item = QListWidgetItem()
            item.setText(f"{row.index + 1}. {row.name}{suffix}")
            item.setData(Qt.UserRole, row.scene_id)
            item.setToolTip(
                f"{row.type} - {format_duration(row.duration)} ({row.duration_source})\n"
                f"Narration: {narration}"
                + (f"\n{row.errors} error(s), {row.issues} warning(s)" if (row.errors or row.issues) else ""))
            # A disabled scene stays selectable (so it can be re-enabled); the
            # "[disabled]" suffix and tooltip are the honest indicator.
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
            self._reload_elements()
            return
        scene = self.project.scene_by_id(row.scene_id) if self.project else None
        # Keep the enable/lock buttons honest about the selected scene.
        if scene is not None:
            self.toggle_button.setText("Enable" if not getattr(scene, "enabled", True) else "Disable")
            self.lock_button.setText("Unlock" if getattr(scene, "locked", False) else "Lock")
        self._reload_elements()
        self.detail_grid.clear()
        narration = (f"ready ({row.narration_duration:.2f}s)" if row.narration_duration > 0
                     else "none yet")
        for label, value in (
            ("Name", row.name),
            ("Type", row.type),
            ("Position", f"#{row.index + 1} of {len(self._rows)}"),
            ("Starts at", f"{row.start:.2f}s"),
            ("Duration", f"{row.duration:.2f}s"),
            ("Length from", row.duration_source),
            ("Narration", narration),
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

    # -- scene editor ----------------------------------------------------

    def _selected_scene(self):
        scene_id = self._selected_id()
        return self.project.scene_by_id(scene_id) if (self.project and scene_id) else None

    def _reload_elements(self) -> None:
        """Fill the element list for the selected scene (front of list = front)."""
        previous = self._selected_element_id()
        self.element_list.blockSignals(True)
        self.element_list.clear()
        scene = self._selected_scene()
        if scene is not None:
            # Reverse so the top of the list is the top of the z-order.
            for element in reversed(scene.elements):
                label = element.kind
                if element.text:
                    label += f": {element.text[:24]}"
                if getattr(element, "locked", False):
                    label += "  [locked]"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, element.id)
                self.element_list.addItem(item)
        self.element_list.blockSignals(False)
        if previous:
            self._select_element_by_id(previous)
        self._show_inspector_for_element()

    def _selected_element_id(self) -> Optional[str]:
        item = self.element_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _select_element_by_id(self, element_id: str) -> None:
        for index in range(self.element_list.count()):
            if self.element_list.item(index).data(Qt.UserRole) == element_id:
                self.element_list.setCurrentItem(self.element_list.item(index))
                return

    def _selected_element(self):
        scene = self._selected_scene()
        element_id = self._selected_element_id()
        if scene is None or element_id is None:
            return None
        return next((el for el in scene.elements if el.id == element_id), None)

    def _on_element_selected(self, _current, _previous) -> None:
        if self._loading:
            return
        self._show_inspector_for_element()

    def _show_inspector_for_element(self) -> None:
        element = self._selected_element()
        self.inspector.clear()
        has_element = element is not None
        for button in (self.el_duplicate, self.el_front, self.el_back, self.el_lock, self.el_delete):
            button.setEnabled(has_element)
        self.element_text.setEnabled(has_element)
        if element is None:
            self.element_text.setText("")
            self.inspector_hint.setText("Select an element to edit its properties.")
            return
        self.el_lock.setText("Unlock" if getattr(element, "locked", False) else "Lock")
        extra = element.extra if isinstance(element.extra, dict) else {}
        rows = [
            ("Kind", element.kind),
            ("Id", element.id),
            ("Anchor", element.anchor),
            ("Z-index", str(getattr(element, "z_index", 0))),
            ("Locked", "yes" if getattr(element, "locked", False) else "no"),
        ]
        # Only the properties that apply to this kind.
        if element.kind in ("text", "number"):
            rows.append(("Align", str(extra.get("align", "left"))))
        if element.kind == "number":
            rows.append(("Value", str(extra.get("value", ""))))
            rows.append(("Unit", str(extra.get("unit", ""))))
            rows.append(("Label", str(extra.get("label", ""))))
        if element.kind == "image":
            rows.append(("Asset", element.asset_id or "(none)"))
            rows.append(("Fit", str(extra.get("fit", "cover"))))
        if element.kind in ("shape", "divider", "progress"):
            rows.append(("Shape", str(extra.get("shape", ""))))
            rows.append(("Fill", str(extra.get("fill", ""))))
        if element.kind == "chart":
            rows.append(("Chart", str(extra.get("chart", "bar"))))
        if element.kind == "progress":
            rows.append(("Value", str(extra.get("value", ""))))
            rows.append(("Max", str(extra.get("max", 100))))
        preset = (element.animation or {}).get("preset", "") if isinstance(element.animation, dict) else ""
        rows.append(("Animation", preset or "default"))
        for label, value in rows:
            self.inspector.add(label, value)
        self.element_text.setText(element.text or "")
        self.inspector_hint.setText("Edit the text below, or use the element buttons. Changes are undoable.")

    def _apply_element_text(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        element = self._selected_element()
        if controller is None or scene is None or element is None:
            return
        new_text = self.element_text.text()
        if new_text == (element.text or ""):
            return
        controller.service.update_element_field(scene.id, element.id, text=new_text)
        controller.service.save(reason="Edited element text")
        self.refresh()

    def _duplicate_element(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        element = self._selected_element()
        if controller is None or scene is None or element is None:
            return
        copy = controller.service.duplicate_element(scene.id, element.id)
        controller.service.save(reason="Duplicated an element")
        self.refresh()
        if copy is not None:
            self._select_element_by_id(copy.id)

    def _reorder_element(self, where: str) -> None:
        controller = self.controller
        scene = self._selected_scene()
        element = self._selected_element()
        if controller is None or scene is None or element is None:
            return
        if where == "front":
            controller.service.element_to_front(scene.id, element.id)
        else:
            controller.service.element_to_back(scene.id, element.id)
        controller.service.save(reason="Reordered an element")
        self.refresh()
        self._select_element_by_id(element.id)

    def _toggle_element_lock(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        element = self._selected_element()
        if controller is None or scene is None or element is None:
            return
        controller.service.set_element_locked(scene.id, element.id, not getattr(element, "locked", False))
        controller.service.save(reason="Toggled element lock")
        self.refresh()
        self._select_element_by_id(element.id)

    def _delete_element(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        element = self._selected_element()
        if controller is None or scene is None or element is None:
            return
        reply = QMessageBox.question(
            self, "Delete element", "Delete this element? You can undo with Ctrl+Z.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        controller.service.remove_element(scene.id, element.id)
        controller.service.save(reason="Deleted an element")
        self.refresh()

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
        self.toggle_button.setEnabled(has_project and has_selection)
        self.lock_button.setEnabled(has_project and has_selection)
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

    def _toggle_enabled(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        if controller is None or scene is None:
            return
        controller.service.set_scene_enabled(scene.id, not getattr(scene, "enabled", True))
        controller.service.save(reason="Toggled scene enabled")
        selected = scene.id
        self.refresh()
        self._select_by_id(selected)

    def _toggle_locked(self) -> None:
        controller = self.controller
        scene = self._selected_scene()
        if controller is None or scene is None:
            return
        controller.service.set_scene_locked(scene.id, not getattr(scene, "locked", False))
        controller.service.save(reason="Toggled scene lock")
        selected = scene.id
        self.refresh()
        self._select_by_id(selected)
