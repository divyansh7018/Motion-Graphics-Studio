"""The open project page: what is in the project, and how to save it.

Every control here performs exactly one action on the open project through the
:class:`ProjectService` (directive section 34) - no widget reaches for a file
directly.  Things that are not built yet are stated in words rather than shown
as buttons that do nothing (section 54).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.atomicio import human_size
from ...project.presets import aspect_ratio_label
from ...project.validation import validate_for_render
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page


class ProjectPage(Page):
    """Details of the project that is currently open."""

    save_requested = Signal()
    save_as_requested = Signal()
    duplicate_requested = Signal()
    rename_requested = Signal()
    rename_folder_requested = Signal()
    close_requested = Signal()
    open_settings_page = Signal()
    project_changed = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__("Project", "Everything stored in the open project.", parent)
        self.context = context

        self.empty = QLabel("No project is open. Use “New project” on the Dashboard to create one.")
        self.empty.setWordWrap(True)
        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.addWidget(self.empty)

        self._build_header_card()
        self._build_script_card()
        self._build_scenes_card()
        self._build_assets_card()
        self._build_validation_card()
        self.refresh()

    @property
    def controller(self):
        return self.context.projects

    @property
    def project(self):
        return self.controller.project if self.controller is not None else None

    # -- cards -------------------------------------------------------------

    def _build_header_card(self) -> None:
        card = self.add_card("Project")
        self.header_grid = KeyValueGrid()
        card.add(self.header_grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.save_button = QPushButton("Save")
        mark_primary(self.save_button)
        self.save_button.setShortcut("Ctrl+S")
        self.save_button.clicked.connect(self.save_requested.emit)
        self.save_as_button = QPushButton("Save as…")
        self.save_as_button.clicked.connect(self.save_as_requested.emit)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate_requested.emit)
        self.settings_button = QPushButton("Project settings")
        self.settings_button.clicked.connect(self.open_settings_page.emit)
        self.close_button = QPushButton("Close project")
        self.close_button.clicked.connect(self.close_requested.emit)
        for button in (
            self.save_button,
            self.save_as_button,
            self.duplicate_button,
            self.settings_button,
            self.close_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

        self.rename_button = QPushButton("Rename project…")
        self.rename_button.clicked.connect(self.rename_requested.emit)
        self.rename_folder_button = QPushButton("Rename folder…")
        self.rename_folder_button.setToolTip(
            "Changes the folder on disk too. Use this only when nothing outside "
            "the project links to that folder."
        )
        self.rename_folder_button.clicked.connect(self.rename_folder_requested.emit)
        rename_row = QHBoxLayout()
        rename_row.setSpacing(METRICS.sm)
        rename_row.addWidget(self.rename_button)
        rename_row.addWidget(self.rename_folder_button)
        rename_row.addStretch(1)
        card.body().addLayout(rename_row)

    def _build_script_card(self) -> None:
        card = self.add_card("Script")
        card.add(
            HintLabel(
                "The script is stored in script.txt inside the project folder and is never rewritten "
                "by the application. Splitting it into scenes arrives in a later stage."
            )
        )
        self.script_edit = QPlainTextEdit()
        self.script_edit.setPlaceholderText("Write or paste the narration script here.")
        self.script_edit.setMinimumHeight(180)
        self.script_edit.textChanged.connect(self._on_script_edited)
        card.add(self.script_edit)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.apply_script_button = QPushButton("Apply script changes")
        self.apply_script_button.clicked.connect(self._apply_script)
        self.script_status = HintLabel("")
        row.addWidget(self.apply_script_button)
        row.addWidget(self.script_status, 1)
        card.body().addLayout(row)

    def _build_scenes_card(self) -> None:
        card = self.add_card("Scenes")
        self.scenes_grid = KeyValueGrid()
        card.add(self.scenes_grid)
        card.add(
            HintLabel(
                "Scenes are stored in the project file now; the scene editor (timings, images, "
                "transitions, storyboard) arrives in Stage D."
            )
        )

    def _build_assets_card(self) -> None:
        card = self.add_card("Assets")
        self.assets_table = QTableWidget(0, 5)
        self.assets_table.setHorizontalHeaderLabels(["Asset", "Kind", "File", "Size", "Status"])
        self.assets_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.assets_table.verticalHeader().setVisible(False)
        self.assets_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.assets_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.assets_table.setMinimumHeight(160)
        card.add(self.assets_table)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.import_button = QPushButton("Import file into assets…")
        self.import_button.clicked.connect(self._import_asset)
        self.relink_button = QPushButton("Relink selected…")
        self.relink_button.clicked.connect(self._relink_selected)
        self.refresh_assets_button = QPushButton("Re-check files")
        self.refresh_assets_button.clicked.connect(self._recheck_assets)
        row.addWidget(self.import_button)
        row.addWidget(self.relink_button)
        row.addWidget(self.refresh_assets_button)
        row.addStretch(1)
        card.body().addLayout(row)
        self.assets_hint = HintLabel("")
        card.add(self.assets_hint)

    def _build_validation_card(self) -> None:
        card = self.add_card("Checks")
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.validate_button = QPushButton("Validate project")
        self.validate_button.clicked.connect(lambda: self._validate(strict=False))
        self.validate_strict_button = QPushButton("Check render settings")
        self.validate_strict_button.setToolTip("The stricter check that a render would run first.")
        self.validate_strict_button.clicked.connect(lambda: self._validate(strict=True))
        row.addWidget(self.validate_button)
        row.addWidget(self.validate_strict_button)
        row.addStretch(1)
        card.body().addLayout(row)
        self.validation_output = QPlainTextEdit()
        self.validation_output.setReadOnly(True)
        self.validation_output.setMaximumHeight(160)
        card.add(self.validation_output)

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        project = self.project
        has_project = project is not None
        self.empty.setVisible(not has_project)
        for widget in (
            self.header_grid,
            self.script_edit,
            self.scenes_grid,
            self.assets_table,
        ):
            widget.setVisible(has_project)
        for button in (
            self.save_button,
            self.save_as_button,
            self.duplicate_button,
            self.settings_button,
            self.close_button,
            self.rename_button,
            self.rename_folder_button,
            self.apply_script_button,
            self.import_button,
            self.relink_button,
            self.refresh_assets_button,
            self.validate_button,
            self.validate_strict_button,
        ):
            button.setVisible(has_project)
        self.script_status.setVisible(has_project)
        self.assets_hint.setVisible(has_project)
        self.validation_output.setVisible(has_project)
        if not has_project:
            self.header_grid.clear()
            self.scenes_grid.clear()
            self.assets_table.setRowCount(0)
            return

        controller = self.controller
        layout = controller.layout
        fmt = project.format

        self.header_grid.clear()
        self.header_grid.add("Name", project.project.name + controller.title_suffix())
        self.header_grid.add("Folder", str(layout.root), monospace=True)
        self.header_grid.add(
            "Format",
            f"{fmt.width}x{fmt.height} @ {fmt.fps} fps "
            f"({aspect_ratio_label(fmt.width, fmt.height)})",
        )
        self.header_grid.add("Quality", fmt.quality_label())
        self.header_grid.add("Voice", project.voice.label())
        self.header_grid.add("Scenes", str(len(project.scenes)))
        self.header_grid.add("Assets", str(len(project.assets)))
        self.header_grid.add("Saved version", str(project.project.project_version))
        self.header_grid.add("Modified", project.project.modified_at or "")

        self.script_edit.blockSignals(True)
        self.script_edit.setPlainText(project.script.source_text)
        self.script_edit.blockSignals(False)
        self.script_status.setText(
            f"script.txt · {project.script.word_count()} words"
            if hasattr(project.script, "word_count")
            else "script.txt"
        )

        self.scenes_grid.clear()
        duration = project.estimated_duration_seconds()
        self.scenes_grid.add("Scene count", str(len(project.scenes)))
        self.scenes_grid.add("Estimated duration", f"{duration:.1f}s" if duration else "not set")
        for index, scene in enumerate(project.scenes[:20], start=1):
            label = scene.name or scene.id or f"scene {index}"
            seconds = f"{scene.duration:.1f}s" if scene.duration else "no duration"
            self.scenes_grid.add(f"#{index}", f"{label} - {seconds}")

        self._refresh_assets()

    def _refresh_assets(self) -> None:
        project = self.project
        self.assets_table.setRowCount(0)
        if project is None:
            return
        for asset in project.assets:
            row = self.assets_table.rowCount()
            self.assets_table.insertRow(row)
            self.assets_table.setItem(row, 0, QTableWidgetItem(asset.label()))
            self.assets_table.setItem(row, 1, QTableWidgetItem(asset.kind or "unknown"))
            self.assets_table.setItem(row, 2, QTableWidgetItem(asset.path or "(not imported)"))
            self.assets_table.setItem(
                row, 3, QTableWidgetItem(human_size(asset.size_bytes) if asset.size_bytes else "-")
            )
            status_item = QTableWidgetItem("missing" if asset.missing else "ready")
            status_item.setData(Qt.UserRole, asset.id)
            self.assets_table.setItem(row, 4, status_item)

        missing = [asset for asset in project.assets if asset.missing]
        total = len(project.assets)
        if not total:
            self.assets_hint.setText("No assets yet. Imported files are copied into assets/.")
        elif missing:
            self.assets_hint.setText(
                f"{len(missing)} of {total} asset(s) could not be found. "
                "Select one and use “Relink selected…”."
            )
        else:
            self.assets_hint.setText(
                f"{total} asset(s). All files are where the project expects them."
            )

    def refresh_title(self) -> None:
        """Update the name row (the ``*`` moves) without rebuilding the page."""
        if self.project is not None:
            self.header_grid.set_value(
                "Name", self.project.project.name + self.controller.title_suffix()
            )

    # -- actions -----------------------------------------------------------

    def _on_script_edited(self) -> None:
        if self.project is None:
            return
        if self.script_edit.toPlainText() != self.project.script.source_text:
            self.script_status.setText("Changed - press “Apply script changes” to keep it.")
        else:
            self.script_status.setText("Up to date.")

    def _apply_script(self) -> None:
        if self.project is None:
            return
        self.controller.service.set_script_text(self.script_edit.toPlainText())
        self.script_status.setText("Applied. Press Save to write it to script.txt.")
        self.project_changed.emit()

    def _import_asset(self) -> None:
        if self.project is None:
            return
        start = str(self.controller.layout.root)
        files, _selected = QFileDialog.getOpenFileNames(self, "Choose files to import", start)
        if not files:
            return
        imported = 0
        errors: list[str] = []
        for file in files:
            report = self.controller.service.import_asset(Path(file))
            if report.error:
                errors.append(f"{Path(file).name}: {report.error}")
            else:
                imported += 1
        self._refresh_assets()
        self.project_changed.emit()
        message = f"{imported} of {len(files)} file(s) imported into assets/."
        if errors:
            message += " Could not import: " + "; ".join(errors[:3])
        self.controller.message.emit(message, 8000)

    def _relink_selected(self) -> None:
        row = self.assets_table.currentRow()
        if row < 0:
            self.controller.message.emit("Select an asset row first.", 4000)
            return
        item = self.assets_table.item(row, 4)
        if item is None:
            return
        asset_id = item.data(Qt.UserRole)
        if asset_id and self.controller.fix_missing_asset(self, str(asset_id)):
            self._refresh_assets()
            self.project_changed.emit()

    def _recheck_assets(self) -> None:
        if self.project is None:
            return
        checks = self.controller.service.verify_assets()
        self._refresh_assets()
        missing = [check for check in checks if not check.exists]
        self.controller.message.emit(
            f"{len(checks)} asset(s) checked, {len(missing)} missing."
            + (f" First problem: {missing[0].message}" if missing else ""),
            7000,
        )

    def _validate(self, strict: bool = False) -> None:
        if self.project is None:
            return
        controller = self.controller
        if strict:
            report = validate_for_render(controller.project, controller.layout.root)
        else:
            report = controller.service.validate(check_files=True)
        self.validation_output.setPlainText(report.to_text())
        self.controller.message.emit(report.headline(), 6000)


__all__ = ["ProjectPage"]
