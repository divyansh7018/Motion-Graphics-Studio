"""The Subtitles page: real captions from real narration (sections 12-18).

The captions on this page come from the measured narration timings.  Word-level
timing is never invented: if a scene's length was not measured from an audio
file, the page says so instead of pretending the captions are frame-accurate.

Editing is honest too.  Splitting a caption divides its *text* at a word
boundary and marks the result manual - it does not claim to know when each word
was spoken.  Merging only joins neighbours, because joining two captions with
silence between them would leave text on screen while nothing is being said.

Captions that fall outside the safe area are reported, never moved (section 16).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.project.model import SUBTITLE_POSITIONS
from app.render.jobs import subtitle_build_spec, subtitle_export_spec
from app.subtitles.service import SubtitleService

from ..theme import mark_primary
from ..widgets.common import ButtonRow, HintLabel, Page, Row


def _spin(minimum: float, maximum: float, value: float, *, step: float = 0.5,
          suffix: str = "", decimals: int = 2) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setValue(value)
    return box


class ColorButton(QPushButton):
    """A swatch button that opens the colour picker and stores the hex value."""

    changed = Signal(str)

    def __init__(self, value: str = "#ffffff", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = value
        self.setMinimumWidth(88)
        self.clicked.connect(self._pick)
        self._paint()

    @property
    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value or "#ffffff"
        self._paint()

    def _pick(self) -> None:
        from PySide6.QtGui import QColor

        chosen = QColorDialog.getColor(QColor(self._value), self, "Choose a colour")
        if chosen.isValid():
            self._value = chosen.name()
            self._paint()
            self.changed.emit(self._value)

    def _paint(self) -> None:
        self.setText(self._value)
        self.setStyleSheet(
            f"background-color: {self._value}; color: {self._value}; border: 1px solid #555;")
        self.setToolTip(f"Caption colour: {self._value}")


class SubtitlesPage(Page):
    """Captions, their style, and the files the player or platform needs."""

    project_changed = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Subtitles",
            "Captions are generated from the narration timings. Nothing is "
            "invented, and nothing is moved without telling you.",
            parent,
        )
        self.context = context
        self.service = SubtitleService()
        self._loading = False
        self._build_job: Optional[str] = None
        self._export_job: Optional[str] = None

        self._build_settings_card()
        self._build_style_card()
        self._build_cue_card()
        self._build_export_card()
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

    @property
    def spec(self):
        project = self.project
        return getattr(project, "subtitles", None) if project is not None else None

    @property
    def style(self):
        project = self.project
        theme = getattr(project, "theme", None) if project is not None else None
        return getattr(theme, "subtitle_style", None) if theme is not None else None

    # -- cards ------------------------------------------------------------

    def _build_settings_card(self) -> None:
        card = self.add_card("Captions")
        self.enabled = QCheckBox("Include captions in this video")
        self.enabled.toggled.connect(self._on_changed)
        card.add(Row("Captions", self.enabled))

        self.burn_in = QCheckBox("Burn the captions into the picture")
        self.burn_in.toggled.connect(self._on_changed)
        card.add(Row("Burn in", self.burn_in,
                     "Burnt-in captions are part of the video and always show. "
                     "Needs FFmpeg's libass filter, which is checked before rendering."))

        self.language = QLineEdit()
        self.language.setPlaceholderText("en")
        self.language.setMaximumWidth(120)
        self.language.editingFinished.connect(self._on_changed)
        card.add(Row("Caption language", self.language,
                     "Independent of the narration language. Captions are never "
                     "translated automatically."))

        self.timing_label = QLabel("Not generated yet.")
        self.timing_label.setObjectName("Hint")
        card.add(Row("Timing source", self.timing_label))

        self.build_button = QPushButton("Generate captions")
        mark_primary(self.build_button)
        self.build_button.clicked.connect(self._generate)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_job)
        self.cancel_button.setEnabled(False)
        card.add(ButtonRow([self.build_button, self.cancel_button]))

        self.status_hint = HintLabel("Nothing generated yet.")
        card.add(self.status_hint)

    def _build_style_card(self) -> None:
        card = self.add_card("Style")
        self.font = QLineEdit()
        self.font.setMaximumWidth(220)
        self.font.editingFinished.connect(self._on_changed)
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 200)
        self.font_size.valueChanged.connect(self._on_changed)
        card.add(Row("Font", _pair(self.font, self.font_size),
                     "The number is the caption size in pixels at the project's height."))

        self.color = ColorButton("#ffffff")
        self.color.changed.connect(self._on_changed)
        self.outline_color = ColorButton("#000000")
        self.outline_color.changed.connect(self._on_changed)
        card.add(Row("Text / outline", _pair(self.color, self.outline_color)))

        self.outline_width = _spin(0.0, 12.0, 2.0, step=0.5, suffix=" px")
        self.outline_width.valueChanged.connect(self._on_changed)
        self.max_lines = QSpinBox()
        self.max_lines.setRange(1, 6)
        self.max_lines.valueChanged.connect(self._on_changed)
        card.add(Row("Outline / max lines", _pair(self.outline_width, self.max_lines)))

        self.position = QComboBox()
        for value in SUBTITLE_POSITIONS:
            self.position.addItem(value.capitalize(), value)
        self.position.currentIndexChanged.connect(self._on_changed)
        self.margin = _spin(0.0, 40.0, 6.0, step=0.5, suffix=" %")
        self.margin.valueChanged.connect(self._on_changed)
        card.add(Row("Position / margin", _pair(self.position, self.margin),
                     "Margin is a percentage of the frame height."))

        self.shadow = QCheckBox("Drop shadow")
        self.shadow.toggled.connect(self._on_changed)
        self.background_enabled = QCheckBox("Box behind the text")
        self.background_enabled.toggled.connect(self._on_changed)
        card.add(Row("Effects", _pair(self.shadow, self.background_enabled)))

        self.background_color = ColorButton("#000000")
        self.background_color.changed.connect(self._on_changed)
        self.background_opacity = _spin(0.0, 100.0, 50.0, step=5.0,
                                        suffix=" %", decimals=0)
        self.background_opacity.valueChanged.connect(self._on_changed)
        card.add(Row("Box colour / opacity",
                     _pair(self.background_color, self.background_opacity)))

        self.check_style_button = QPushButton("Check the captions")
        self.check_style_button.clicked.connect(self._check_style)
        card.add(ButtonRow([self.check_style_button]))

    def _build_cue_card(self) -> None:
        card = self.add_card("Caption list")
        self.cue_table = QTableWidget(0, 4)
        self.cue_table.setHorizontalHeaderLabels(["Start", "End", "Text", ""])
        self.cue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.cue_table.setColumnWidth(0, 90)
        self.cue_table.setColumnWidth(1, 90)
        self.cue_table.setColumnWidth(3, 84)
        self.cue_table.verticalHeader().setVisible(False)
        self.cue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cue_table.setSelectionMode(QTableWidget.SingleSelection)
        self.cue_table.setMinimumHeight(220)
        self.cue_table.itemChanged.connect(self._on_cue_edited)
        self.cue_table.cellClicked.connect(self._on_cue_clicked)
        card.add(self.cue_table)

        self.split_button = QPushButton("Split")
        self.split_button.clicked.connect(self._split_cue)
        self.merge_button = QPushButton("Merge with next")
        self.merge_button.clicked.connect(self._merge_cue)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete_cue)
        card.add(ButtonRow([self.split_button, self.merge_button, self.delete_button]))
        card.add(HintLabel(
            "Type in the Text column to reword a caption. Splitting divides the "
            "words at the nearest boundary; it does not invent word timings."))

    def _build_export_card(self) -> None:
        card = self.add_card("Export")
        self.format_srt = QCheckBox("SRT")
        self.format_srt.setChecked(True)
        self.format_vtt = QCheckBox("WebVTT")
        self.format_vtt.setChecked(True)
        self.format_ass = QCheckBox("ASS (styled)")
        card.add(Row("Formats", _triple(self.format_srt, self.format_vtt, self.format_ass)))

        self.export_button = QPushButton("Export caption files")
        self.export_button.clicked.connect(self._export)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        card.add(ButtonRow([self.export_button, self.open_folder_button]))

        self.export_hint = HintLabel("Caption files are written into the project's "
                                     "subtitles folder.")

        self.issues_view = QPlainTextEdit()
        self.issues_view.setReadOnly(True)
        self.issues_view.setMaximumHeight(140)
        self.issues_view.setPlaceholderText("Problems found while checking appear here.")
        card.add(self.issues_view)
        card.add(self.export_hint)

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        self._loading = True
        try:
            spec = self.spec
            style = self.style
            has_project = spec is not None
            self.set_controls_enabled(has_project)
            if spec is None:
                self.cue_table.setRowCount(0)
                self.timing_label.setText("No project is open.")
                return

            self.enabled.setChecked(bool(spec.enabled))
            self.burn_in.setChecked(bool(spec.burn_in))
            self.language.setText(str(spec.language or ""))
            self.timing_label.setText(spec.timing_label())

            if style is not None:
                self.font.setText(str(style.font))
                self.font_size.setValue(int(style.font_size))
                self.color.set_value(str(style.color))
                self.outline_color.set_value(str(style.outline_color))
                self.outline_width.setValue(float(style.outline_width))
                self.max_lines.setValue(int(style.max_lines))

            index = self.position.findData(spec.position)
            self.position.setCurrentIndex(max(0, index))
            self.margin.setValue(float(spec.margin_percent))
            self.shadow.setChecked(bool(spec.shadow))
            has_background = bool(spec.background)
            self.background_enabled.setChecked(has_background)
            if has_background:
                self.background_color.set_value(str(spec.background))
            self.background_opacity.setValue(float(spec.background_opacity) * 100.0)

            self._reload_cues()
        finally:
            self._loading = False

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.enabled, self.burn_in, self.language, self.build_button,
                       self.font, self.font_size, self.color, self.outline_color,
                       self.outline_width, self.max_lines, self.position, self.margin,
                       self.shadow, self.background_enabled, self.background_color,
                       self.background_opacity, self.check_style_button, self.split_button,
                       self.merge_button, self.delete_button, self.export_button,
                       self.open_folder_button):
            widget.setEnabled(enabled)

    def _reload_cues(self) -> None:
        spec = self.spec
        self.cue_table.blockSignals(True)
        try:
            self.cue_table.setRowCount(0)
            if spec is None:
                return
            for cue in spec.sorted_cues():
                row = self.cue_table.rowCount()
                self.cue_table.insertRow(row)
                for column, text in enumerate((f"{cue.start:.3f}", f"{cue.end:.3f}",
                                               str(cue.text))):
                    cell = QTableWidgetItem(text)
                    if column == 2:
                        cell.setFlags(cell.flags() | Qt.ItemIsEditable)
                    else:
                        cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    if column == 0:
                        cell.setData(Qt.UserRole, cue.id)
                    self.cue_table.setItem(row, column, cell)
                action = QTableWidgetItem("Edit times")
                action.setFlags(action.flags() & ~Qt.ItemIsEditable)
                action.setTextAlignment(Qt.AlignCenter)
                self.cue_table.setItem(row, 3, action)
        finally:
            self.cue_table.blockSignals(False)

    # -- edits ------------------------------------------------------------

    def _on_changed(self, *_args) -> None:
        if self._loading:
            return
        spec = self.spec
        style = self.style
        if spec is None:
            return
        spec.enabled = self.enabled.isChecked()
        spec.burn_in = self.burn_in.isChecked()
        spec.language = self.language.text().strip()
        spec.position = str(self.position.currentData() or "bottom")
        spec.margin_percent = float(self.margin.value())
        spec.shadow = self.shadow.isChecked()
        spec.background = (self.background_color.value
                           if self.background_enabled.isChecked() else "")
        spec.background_opacity = float(self.background_opacity.value()) / 100.0
        if style is not None:
            style.font = self.font.text().strip() or style.font
            style.font_size = int(self.font_size.value())
            style.color = self.color.value
            style.outline_color = self.outline_color.value
            style.outline_width = float(self.outline_width.value())
            style.max_lines = int(self.max_lines.value())
        self.project_changed.emit()

    def _selected_cue_id(self) -> str:
        row = self.cue_table.currentRow()
        if row < 0:
            return ""
        cell = self.cue_table.item(row, 0)
        return str(cell.data(Qt.UserRole)) if cell is not None else ""

    def _on_cue_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 2:
            return
        cue_id = self._cue_id_for_row(item.row())
        if not cue_id or self.project is None:
            return
        if self.service.edit(self.project, cue_id, text=item.text()):
            self.project_changed.emit()
        else:
            self.status_hint.setText("That caption could not be changed.")

    def _cue_id_for_row(self, row: int) -> str:
        cell = self.cue_table.item(row, 0)
        return str(cell.data(Qt.UserRole)) if cell is not None else ""

    def _on_cue_clicked(self, row: int, column: int) -> None:
        """Clicking the last column opens the timing editor."""
        if column == 3:
            self._edit_times(self._cue_id_for_row(row))

    def _edit_times(self, cue_id: str) -> None:
        spec = self.spec
        if spec is None or not cue_id:
            return
        cue = next((item for item in spec.cues if item.id == cue_id), None)
        if cue is None:
            return
        text, accepted = QInputDialog.getText(
            self, "Caption timing", "Start and end in seconds, separated by a space:",
            text=f"{cue.start:.3f} {cue.end:.3f}")
        if not accepted:
            return
        parts = text.replace(",", " ").split()
        if len(parts) != 2:
            self.status_hint.setText("Type two numbers, for example: 1.500 4.250")
            return
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            self.status_hint.setText("Those are not numbers. Use seconds, like 1.500.")
            return
        if end <= start:
            self.status_hint.setText("The caption has to end after it starts.")
            return
        if self.service.edit(self.project, cue_id, start=start, end=end):
            self._reload_cues()
            self.status_hint.setText("Caption timing updated.")
            self.project_changed.emit()

    def _split_cue(self) -> None:
        cue_id = self._selected_cue_id()
        if not cue_id or self.project is None:
            self.status_hint.setText("Select a caption to split first.")
            return
        text, accepted = QInputDialog.getText(
            self, "Split caption", "Split at this many seconds into the video:")
        if not accepted:
            return
        try:
            moment = float(text)
        except ValueError:
            self.status_hint.setText("Type a number of seconds, like 2.5.")
            return
        created = self.service.split(self.project, cue_id, moment)
        if created is None:
            self.status_hint.setText(
                "That point is not inside the caption, so there was nothing to split.")
            return
        self._reload_cues()
        self.status_hint.setText("Caption split in two. The timings are now manual.")
        self.project_changed.emit()

    def _merge_cue(self) -> None:
        row = self.cue_table.currentRow()
        if row < 0 or row + 1 >= self.cue_table.rowCount() or self.project is None:
            self.status_hint.setText("Select a caption that has one after it.")
            return
        first = self._cue_id_for_row(row)
        second = self._cue_id_for_row(row + 1)
        if self.service.merge(self.project, first, second):
            self._reload_cues()
            self.status_hint.setText("Captions merged.")
            self.project_changed.emit()
        else:
            self.status_hint.setText(
                "Only neighbouring captions can be merged. Joining two with a gap "
                "between them would leave text on screen during silence.")

    def _delete_cue(self) -> None:
        cue_id = self._selected_cue_id()
        if not cue_id or self.project is None:
            self.status_hint.setText("Select a caption to delete first.")
            return
        if self.service.delete(self.project, cue_id):
            self._reload_cues()
            self.status_hint.setText("Caption deleted.")
            self.project_changed.emit()

    def _check_style(self) -> None:
        """Cheap and local: measures the caption block against the frame."""
        if self.project is None:
            self.status_hint.setText("Open a project first.")
            return
        issues = self.service.validate(self.project)
        if not issues:
            self.issues_view.setPlainText("The captions fit the frame.")
            self.status_hint.setText("Caption style is fine.")
            return
        self.issues_view.setPlainText("\n".join(
            f"[{issue.severity.upper()}] {issue.code}: {issue.message}"
            + (f"  ({issue.what_to_do})" if issue.what_to_do else "")
            for issue in issues))
        self.status_hint.setText(f"{len(issues)} thing(s) to look at.")

    # -- jobs -------------------------------------------------------------

    def _payload(self) -> dict:
        return {"project": self.project, "project_dir": str(self.project_dir or "")}

    def _submit(self, spec):
        spec.paths = self.context.paths
        return self.context.jobs.submit(spec)

    def _generate(self) -> None:
        if self.project is None or self.project_dir is None:
            self.status_hint.setText("Open a project first.")
            return
        if self._build_job is not None:
            self.status_hint.setText("Captions are already being generated.")
            return
        job = self._submit(subtitle_build_spec(self._payload()))
        if job is None:
            self.status_hint.setText("Captions are already being generated.")
            return
        self._build_job = job.id
        self.build_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_hint.setText("Generating captions from the narration…")

    def _cancel_job(self) -> None:
        job_id = self._build_job or self._export_job
        if job_id:
            self.context.jobs.cancel(job_id, "Stopped by the user.")
            self.status_hint.setText("Cancelling…")

    def _export(self) -> None:
        if self.project is None or self.project_dir is None:
            self.status_hint.setText("Open a project first.")
            return
        formats = [name for box, name in ((self.format_srt, "srt"),
                                          (self.format_vtt, "vtt"),
                                          (self.format_ass, "ass")) if box.isChecked()]
        if not formats:
            self.status_hint.setText("Choose at least one format to export.")
            return
        output = self.project_dir / "subtitles"
        job = self._submit(subtitle_export_spec(
            self._payload(), output_dir=str(output), formats=formats,
            stem=self.project.project.name or "subtitles"))
        if job is None:
            self.status_hint.setText("An export is already running.")
            return
        self._export_job = job.id
        self.export_button.setEnabled(False)
        self.status_hint.setText("Writing the caption files…")

    def _open_folder(self) -> None:
        from ..notifications import open_folder

        folder = self.project_dir / "subtitles" if self.project_dir else None
        if folder is None or not folder.exists():
            self.export_hint.setText("Export the captions first, then the folder will exist.")
            return
        open_folder(self, folder)

    # -- job results ------------------------------------------------------

    def on_build_finished(self, result) -> None:
        self._build_job = None
        self.build_button.setEnabled(True)
        self.cancel_button.setEnabled(bool(self._export_job))
        if result.cancelled:
            self.status_hint.setText("Caption generation was cancelled.")
            return
        if result.error is not None:
            self.status_hint.setText(f"Caption generation failed: {result.error.title}")
            return
        value = result.value or {}
        count = int(value.get("count", 0) or 0)
        issues = value.get("issues", [])
        self._reload_cues()
        if self.spec is not None:
            self.timing_label.setText(self.spec.timing_label())
        if count == 0:
            self.status_hint.setText(
                "No captions were generated. Generate the narration first - captions "
                "need measured audio timings.")
        else:
            self.status_hint.setText(
                f"{count} caption(s) generated." +
                (f" {len(issues)} note(s) below." if issues else ""))
        self.issues_view.setPlainText("\n".join(
            f"[{str(issue.get('severity', 'warning')).upper()}] "
            f"{issue.get('code')}: {issue.get('message')}" for issue in issues)
            or "No problems found.")
        self.project_changed.emit()

    def on_export_finished(self, result) -> None:
        self._export_job = None
        self.export_button.setEnabled(True)
        if result.cancelled:
            self.status_hint.setText("The export was cancelled.")
            return
        if result.error is not None:
            self.status_hint.setText(f"The export failed: {result.error.title}")
            return
        value = result.value or {}
        files = value.get("files", {})
        if not files:
            self.export_hint.setText(
                "Nothing was written: there are no captions to export yet.")
            return
        names = ", ".join(Path(str(path)).name for path in files.values())
        self.export_hint.setText(f"Wrote {len(files)} file(s): {names}")
        self.status_hint.setText(f"Exported {len(files)} caption file(s).")
        issues = value.get("issues", [])
        if issues:
            self.issues_view.setPlainText("\n".join(
                f"[{str(issue.get('severity', 'warning')).upper()}] "
                f"{issue.get('code')}: {issue.get('message')}" for issue in issues))


def _pair(left: QWidget, right: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(left)
    layout.addWidget(right)
    layout.addStretch(1)
    return holder


def _triple(first: QWidget, second: QWidget, third: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    for widget in (first, second, third):
        layout.addWidget(widget)
    layout.addStretch(1)
    return holder
