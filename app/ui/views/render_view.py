"""The Render page: pick a target, see a real plan, then render (sections 20-56).

Everything on this page is read from the machine or from the project:

* The codec list comes from ``ffmpeg -encoders`` on *this* FFmpeg, fetched on a
  worker thread.  A codec the local build does not have cannot be selected, and
  the render stops before drawing a frame if one is somehow chosen anyway.
* The plan - length, frames, output name and size - is computed by
  :class:`RenderService.plan` before the button is pressed, and the size is
  labelled **Estimated** because it is an estimate.
* Output naming goes through :class:`OutputService`, which advances the sequence
  past existing files, so a render never overwrites an earlier one.

No control here is decorative: changing a setting writes it to
``project.format`` and re-plans, and the render itself is a cancellable job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.project.presets import (
    ASPECT_PRESETS,
    CONTAINERS,
    FPS_OPTIONS,
    QUALITY_PRESETS,
    VIDEO_CODECS,
    aspect_preset,
    quality_preset,
)
from app.render.jobs import (
    capabilities_spec,
    qc_spec,
    render_plan_spec,
    render_spec,
)
from app.render.output import OutputService
from app.render.platform import PLATFORM_PRESETS

from ..theme import mark_primary
from ..widgets.common import ButtonRow, HintLabel, KeyValueGrid, Page, Row

#: Encoder speeds offered for x264/x265.  ``veryslow`` is real, just slow.
ENCODER_PRESETS: tuple[str, ...] = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

PIXEL_FORMATS: tuple[str, ...] = ("yuv420p", "yuv422p", "yuv444p")


def _spin(minimum: float, maximum: float, value: float, *, step: float = 1.0,
          suffix: str = "", decimals: int = 0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setValue(value)
    return box


class RenderPage(Page):
    """Choose the target, check the plan, render, then inspect the result."""

    project_changed = Signal()
    #: Emitted with the finished file so the window can offer to open it.
    render_completed = Signal(str)

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Render",
            "Export the finished video. The codec list comes from your own FFmpeg "
            "installation, and nothing is ever overwritten.",
            parent,
        )
        self.context = context
        self._loading = False
        self._render_job: Optional[str] = None
        self._plan_job: Optional[str] = None
        self._capabilities_job: Optional[str] = None
        self._qc_job: Optional[str] = None
        self._last_result: dict = {}
        #: Codecs FFmpeg actually has, per container.  Empty until detected.
        self._codecs_by_container: dict[str, list] = {}
        self._detected = False

        self._build_destination_card()
        self._build_quality_card()
        self._build_content_card()
        self._build_plan_card()
        self._build_render_card()
        self._build_history_card()
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
    def format(self):
        project = self.project
        return getattr(project, "format", None) if project is not None else None

    # -- cards ------------------------------------------------------------

    def _build_destination_card(self) -> None:
        card = self.add_card("Destination")
        self.platform_combo = QComboBox()
        for preset in PLATFORM_PRESETS:
            self.platform_combo.addItem(
                f"{preset.label}  ({preset.width}x{preset.height})", preset.key)
            index = self.platform_combo.count() - 1
            self.platform_combo.setItemData(index, preset.note, Qt.ToolTipRole)
        self.platform_combo.currentIndexChanged.connect(self._apply_platform)
        self.apply_platform_button = QPushButton("Apply")
        self.apply_platform_button.clicked.connect(self._apply_platform)
        card.add(Row("Platform preset", _pair(self.platform_combo, self.apply_platform_button),
                     "Sets resolution, frame rate and quality in one step. You can "
                     "still change any of them afterwards."))

        self.aspect_combo = QComboBox()
        for preset in ASPECT_PRESETS:
            self.aspect_combo.addItem(preset.label, preset.key)
        self.aspect_combo.currentIndexChanged.connect(self._on_aspect_changed)
        card.add(Row("Aspect", self.aspect_combo))

        self.resolution_combo = QComboBox()
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)
        card.add(Row("Resolution", self.resolution_combo,
                     "Every scene is laid out for the size you pick, so nothing is "
                     "hard-coded to one resolution."))

        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 8192)
        self.width_spin.setSingleStep(2)
        self.width_spin.valueChanged.connect(self._on_size_changed)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 8192)
        self.height_spin.setSingleStep(2)
        self.height_spin.valueChanged.connect(self._on_size_changed)
        card.add(Row("Custom size", _pair(self.width_spin, self.height_spin),
                     "Width and height must both be even numbers for H.264."))

        self.fps_combo = QComboBox()
        for fps in FPS_OPTIONS:
            self.fps_combo.addItem(f"{fps} fps", fps)
        self.fps_combo.currentIndexChanged.connect(self._on_changed)
        card.add(Row("Frame rate", self.fps_combo))

        self.container_combo = QComboBox()
        for name in CONTAINERS:
            self.container_combo.addItem(f".{name}", name)
        self.container_combo.currentIndexChanged.connect(self._on_container_changed)
        card.add(Row("Container", self.container_combo))

        self.output_folder = QLabel("-")
        self.output_folder.setObjectName("Hint")
        self.output_folder.setWordWrap(True)
        self.output_folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.choose_folder = QPushButton("Change…")
        self.choose_folder.clicked.connect(self._choose_folder)
        card.add(Row("Output folder", _pair(self.output_folder, self.choose_folder)))

    def _build_quality_card(self) -> None:
        card = self.add_card("Quality")
        self.quality_combo = QComboBox()
        for preset in QUALITY_PRESETS:
            self.quality_combo.addItem(preset.label, preset.key)
            index = self.quality_combo.count() - 1
            self.quality_combo.setItemData(index, preset.note, Qt.ToolTipRole)
        self.quality_combo.addItem("Custom", "custom")
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        card.add(Row("Quality", self.quality_combo))

        self.codec_combo = QComboBox()
        for codec in VIDEO_CODECS:
            self.codec_combo.addItem(codec, codec)
        self.codec_combo.currentIndexChanged.connect(self._on_changed)
        card.add(Row("Video codec", self.codec_combo,
                     "The list is filled from the encoders your FFmpeg actually has. "
                     "Press 'Check FFmpeg' if you installed a new build."))

        self.detect_button = QPushButton("Check FFmpeg")
        self.detect_button.clicked.connect(self._detect_capabilities)
        self.codec_hint = HintLabel("Not checked yet on this machine.")
        card.add(Row("", _pair(self.detect_button, QWidget())))
        card.add(self.codec_hint)

        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.valueChanged.connect(self._on_changed)
        self.bitrate_spin = _spin(0, 200000, 0, step=250, suffix=" kbps")
        self.bitrate_spin.valueChanged.connect(self._on_changed)
        card.add(Row("CRF / bitrate", _pair(self.crf_spin, self.bitrate_spin),
                     "CRF is constant quality (lower is better). Set a bitrate to "
                     "target a size instead; 0 means use CRF."))

        self.encoder_preset_combo = QComboBox()
        for name in ENCODER_PRESETS:
            self.encoder_preset_combo.addItem(name, name)
        self.encoder_preset_combo.currentIndexChanged.connect(self._on_changed)
        self.pixel_format_combo = QComboBox()
        for name in PIXEL_FORMATS:
            self.pixel_format_combo.addItem(name, name)
        self.pixel_format_combo.currentIndexChanged.connect(self._on_changed)
        card.add(Row("Encoder speed / pixels",
                     _pair(self.encoder_preset_combo, self.pixel_format_combo),
                     "Slower encodes the same quality in less space but takes longer."))

        self.two_pass = QCheckBox("Two-pass encoding")
        self.two_pass.toggled.connect(self._on_changed)
        card.add(Row("Two-pass", self.two_pass,
                     "Only used when a bitrate is set. Takes about twice as long."))

        self.advanced_mode = QCheckBox("Show every setting")
        self.advanced_mode.toggled.connect(self._on_advanced_toggled)
        card.add(self.advanced_mode)

    def _build_content_card(self) -> None:
        card = self.add_card("What to include")
        self.include_audio = QCheckBox("Narration, music and effects")
        self.include_audio.toggled.connect(self._on_changed)
        card.add(Row("Audio", self.include_audio))

        self.include_subtitles = QCheckBox("Write .srt and .vtt caption files next to the video")
        self.include_subtitles.toggled.connect(self._on_changed)
        card.add(Row("Caption files", self.include_subtitles))

        self.burn_subtitles = QCheckBox("Burn the captions into the picture")
        self.burn_subtitles.toggled.connect(self._on_changed)
        card.add(Row("Burn in", self.burn_subtitles,
                     "Needs FFmpeg's libass filter. This is checked before rendering, "
                     "and the render stops with an explanation if it is missing."))

    def _build_plan_card(self) -> None:
        card = self.add_card("Plan")
        self.plan_grid = KeyValueGrid()
        for label in ("Length", "Frames", "Resolution", "Quality", "Output file",
                      "Estimated size", "Estimated time"):
            self.plan_grid.set_value(label, "-")
        card.add(self.plan_grid)

        self.plan_button = QPushButton("Work out the plan")
        self.plan_button.clicked.connect(self._plan)
        card.add(ButtonRow([self.plan_button]))
        self.plan_hint = HintLabel(
            "Sizes and times are estimates. The finished file is measured and "
            "quality-checked for real.")
        card.add(self.plan_hint)

    def _build_render_card(self) -> None:
        card = self.add_card("Render")
        self.render_button = QPushButton("Render video")
        mark_primary(self.render_button)
        self.render_button.clicked.connect(self._render)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        card.add(ButtonRow([self.render_button, self.cancel_button]))

        self.status_hint = HintLabel("Nothing rendered yet.")
        card.add(self.status_hint)

        self.result_buttons = QWidget()
        layout = QHBoxLayout(self.result_buttons)
        layout.setContentsMargins(0, 0, 0, 0)
        self.open_video = QPushButton("Open video")
        self.open_video.clicked.connect(self._open_video)
        self.open_folder = QPushButton("Open folder")
        self.open_folder.clicked.connect(self._open_output_folder)
        self.copy_path = QPushButton("Copy path")
        self.copy_path.clicked.connect(self._copy_path)
        self.view_qc = QPushButton("Run quality check")
        self.view_qc.clicked.connect(self._run_qc)
        for button in (self.open_video, self.open_folder, self.copy_path, self.view_qc):
            layout.addWidget(button)
            button.setEnabled(False)
        layout.addStretch(1)
        card.add(self.result_buttons)

        self.qc_view = QPlainTextEdit()
        self.qc_view.setReadOnly(True)
        self.qc_view.setMaximumHeight(180)
        self.qc_view.setPlaceholderText("The quality report appears here after a render.")
        card.add(self.qc_view)

    def _build_history_card(self) -> None:
        card = self.add_card("Previous renders")
        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(
            ["When", "File", "Result", "Size", "Resolution"])
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setMinimumHeight(140)
        card.add(self.history_table)
        self.history_hint = HintLabel("Every render is recorded, including failures.")

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        self._loading = True
        try:
            fmt = self.format
            project = self.project
            has_project = fmt is not None
            self.set_controls_enabled(has_project)
            if fmt is None:
                self.output_folder.setText("No project is open.")
                return

            self._reload_aspect_and_resolution(fmt)
            self.width_spin.setValue(int(fmt.width))
            self.height_spin.setValue(int(fmt.height))
            self._select(self.fps_combo, int(fmt.fps))
            self._select(self.container_combo, str(fmt.container))
            self._select(self.quality_combo, str(fmt.quality_preset))
            self._select_codec(str(fmt.codec))
            self.crf_spin.setValue(int(fmt.crf))
            self.bitrate_spin.setValue(float(fmt.bitrate_kbps))
            self._select(self.encoder_preset_combo, str(fmt.encoder_preset))
            self._select(self.pixel_format_combo, str(fmt.pixel_format))

            subtitles = getattr(project, "subtitles", None)
            self.include_audio.setChecked(True)
            self.include_subtitles.setChecked(
                bool(getattr(subtitles, "enabled", False)) if subtitles else False)
            self.burn_subtitles.setChecked(
                bool(getattr(subtitles, "burn_in", False)) if subtitles else False)

            folder = self._output_directory()
            self.output_folder.setText(str(folder) if folder else "-")
            self._reload_history()
            self._on_advanced_toggled(self.advanced_mode.isChecked())
        finally:
            self._loading = False

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.platform_combo, self.apply_platform_button, self.aspect_combo,
                       self.resolution_combo, self.width_spin, self.height_spin,
                       self.fps_combo, self.container_combo, self.choose_folder,
                       self.quality_combo, self.codec_combo, self.detect_button,
                       self.crf_spin, self.bitrate_spin, self.encoder_preset_combo,
                       self.pixel_format_combo, self.two_pass, self.include_audio,
                       self.include_subtitles, self.burn_subtitles, self.plan_button,
                       self.render_button):
            widget.setEnabled(enabled)

    def _reload_aspect_and_resolution(self, fmt) -> None:
        width, height = int(fmt.width), int(fmt.height)
        self.aspect_combo.blockSignals(True)
        try:
            match = 0
            for index, preset in enumerate(ASPECT_PRESETS):
                if (width, height) in preset.resolutions:
                    match = index
                    break
            self.aspect_combo.setCurrentIndex(match)
        finally:
            self.aspect_combo.blockSignals(False)
        self._reload_resolutions(self.aspect_combo.currentData())
        self._select(self.resolution_combo, f"{width}x{height}")

    def _reload_resolutions(self, aspect_key: str) -> None:
        preset = aspect_preset(str(aspect_key)) if aspect_key else None
        self.resolution_combo.blockSignals(True)
        try:
            self.resolution_combo.clear()
            for width, height in (preset.resolutions if preset else ()):
                self.resolution_combo.addItem(f"{width} x {height}", f"{width}x{height}")
            self.resolution_combo.addItem("Custom…", "custom")
        finally:
            self.resolution_combo.blockSignals(False)

    def _output_directory(self) -> Optional[Path]:
        root = self.project_dir
        fmt = self.format
        if root is None or fmt is None:
            return None
        return root / (str(getattr(self.project.export, "output_dir", "") or "renders"))

    def _reload_history(self) -> None:
        self.history_table.setRowCount(0)
        root = self.project_dir
        if root is None:
            self.history_hint.setText("Open a project to see its render history.")
            return
        try:
            entries = OutputService(root).history()
        except OSError as exc:
            self.history_hint.setText(f"The history could not be read: {exc}")
            return
        if not entries:
            self.history_hint.setText("No renders recorded for this project yet.")
            return
        self.history_hint.setText(f"{len(entries)} render(s) recorded.")
        for entry in entries[:25]:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            resolution = (f"{entry.width}x{entry.height}"
                          if entry.width and entry.height else "-")
            cells = [str(entry.at or "-"), Path(str(entry.path or "")).name or "-",
                     str(entry.status or "-"),
                     f"{entry.duration:.2f}s" if entry.duration else "-",
                     resolution]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column == 1:
                    cell.setToolTip(str(entry.path or ""))
                self.history_table.setItem(row, column, cell)

    # -- edits ------------------------------------------------------------

    def _select(self, combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(index)
            finally:
                combo.blockSignals(False)

    def _select_codec(self, codec: str) -> None:
        index = self.codec_combo.findData(codec)
        if index >= 0:
            self.codec_combo.blockSignals(True)
            try:
                self.codec_combo.setCurrentIndex(index)
            finally:
                self.codec_combo.blockSignals(False)
        else:
            # A codec this FFmpeg does not have is still shown, but labelled, so
            # the user can see why the render would be refused.
            self.codec_combo.blockSignals(True)
            try:
                self.codec_combo.addItem(f"{codec}  (not available here)", codec)
                self.codec_combo.setCurrentIndex(self.codec_combo.count() - 1)
            finally:
                self.codec_combo.blockSignals(False)

    def _apply_platform(self, *_args) -> None:
        if self._loading or self.project is None:
            return
        key = str(self.platform_combo.currentData() or "")
        if not key:
            return
        from app.render.service import RenderService

        root = self.project_dir
        if root is None:
            return
        try:
            service = RenderService(None, project_dir=root)
            changed = service.apply_platform(self.project, key)
        except (ValueError, RuntimeError) as exc:
            self.plan_hint.setText(f"The preset could not be applied: {exc}")
            return
        self.refresh()
        self.project_changed.emit()
        summary = ", ".join(f"{name}: {old} -> {new}"
                            for name, (old, new) in changed.items()) or "no changes"
        self.plan_hint.setText(f"Applied the preset ({summary}). Press 'Work out the plan'.")

    def _on_aspect_changed(self, *_args) -> None:
        if self._loading:
            return
        self._reload_resolutions(self.aspect_combo.currentData())
        self._on_resolution_changed()

    def _on_resolution_changed(self, *_args) -> None:
        if self._loading:
            return
        value = str(self.resolution_combo.currentData() or "")
        if value == "custom" or not value:
            return
        try:
            width, height = (int(part) for part in value.split("x"))
        except ValueError:
            return
        self._loading = True
        try:
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)
        finally:
            self._loading = False
        self._on_changed()

    def _on_size_changed(self, *_args) -> None:
        if self._loading:
            return
        self._on_changed()

    def _on_container_changed(self, *_args) -> None:
        if self._loading:
            return
        self._refresh_codec_list()
        self._on_changed()

    def _on_quality_changed(self, *_args) -> None:
        if self._loading:
            return
        key = str(self.quality_combo.currentData() or "")
        preset = quality_preset(key)
        if preset is not None:
            self._loading = True
            try:
                self.crf_spin.setValue(int(preset.crf))
                self.bitrate_spin.setValue(float(preset.bitrate_kbps))
                self._select(self.encoder_preset_combo, preset.encoder_preset)
                self._select(self.pixel_format_combo, preset.pixel_format)
            finally:
                self._loading = False
        self._on_changed()

    def _on_advanced_toggled(self, checked: bool) -> None:
        for widget in (self.encoder_preset_combo, self.pixel_format_combo,
                       self.two_pass, self.bitrate_spin):
            widget.setVisible(bool(checked))

    def _on_changed(self, *_args) -> None:
        """Write the controls into project.format - one handler for all of them."""
        if self._loading:
            return
        fmt = self.format
        if fmt is None:
            return
        fmt.width = int(self.width_spin.value())
        fmt.height = int(self.height_spin.value())
        fmt.fps = int(self.fps_combo.currentData() or 30)
        fmt.container = str(self.container_combo.currentData() or "mp4")
        fmt.quality_preset = str(self.quality_combo.currentData() or "high")
        fmt.codec = str(self.codec_combo.currentData() or "h264_cpu")
        fmt.crf = int(self.crf_spin.value())
        fmt.bitrate_kbps = int(self.bitrate_spin.value())
        fmt.encoder_preset = str(self.encoder_preset_combo.currentData() or "medium")
        fmt.pixel_format = str(self.pixel_format_combo.currentData() or "yuv420p")

        subtitles = getattr(self.project, "subtitles", None)
        if subtitles is not None:
            subtitles.enabled = self.include_subtitles.isChecked()
            subtitles.burn_in = self.burn_subtitles.isChecked()
        self.project_changed.emit()
        self.plan_hint.setText("Settings changed. Press 'Work out the plan' to refresh.")

    def _choose_folder(self) -> None:
        if self.project is None:
            return
        start = str(self._output_directory() or self.project_dir or self.context.paths.data_root)
        chosen = QFileDialog.getExistingDirectory(self, "Choose the output folder", start)
        if not chosen:
            return
        root = self.project_dir
        target = Path(chosen)
        if root is not None and target.is_relative_to(root):
            self.project.export.output_dir = str(target.relative_to(root))
        else:
            self.project.export.output_dir = str(target)
        self.output_folder.setText(str(target))
        self.project_changed.emit()

    # -- jobs -------------------------------------------------------------

    def _payload(self) -> dict:
        return {"project": self.project, "project_dir": str(self.project_dir or "")}

    def _submit(self, spec):
        spec.paths = self.context.paths
        return self.context.jobs.submit(spec)

    def _detect_capabilities(self) -> None:
        if self.project is None:
            self.codec_hint.setText("Open a project first.")
            return
        if self._capabilities_job is not None:
            self.codec_hint.setText("FFmpeg is already being checked.")
            return
        job = self._submit(capabilities_spec(self._payload()))
        if job is None:
            self.codec_hint.setText("FFmpeg is already being checked.")
            return
        self._capabilities_job = job.id
        self.detect_button.setEnabled(False)
        self.codec_hint.setText("Asking FFmpeg which encoders it has…")

    def _plan(self) -> None:
        if self.project is None:
            self.plan_hint.setText("Open a project first.")
            return
        if self._plan_job is not None:
            self.plan_hint.setText("A plan is already being worked out.")
            return
        job = self._submit(render_plan_spec(self._payload()))
        if job is None:
            self.plan_hint.setText("A plan is already being worked out.")
            return
        self._plan_job = job.id
        self.plan_button.setEnabled(False)
        self.plan_hint.setText("Working out the plan…")

    def _render(self) -> None:
        if self.project is None or self.project_dir is None:
            self.status_hint.setText("Open a project first.")
            return
        if self._render_job is not None:
            self.status_hint.setText("A render is already running.")
            return
        job = self._submit(render_spec(
            self._payload(),
            include_audio=self.include_audio.isChecked(),
            include_subtitles=self.include_subtitles.isChecked(),
            burn_subtitles=self.burn_subtitles.isChecked(),
            two_pass=self.two_pass.isChecked(),
            resume=True,
            persist_settings=True,
        ))
        if job is None:
            self.status_hint.setText("A render is already running.")
            return
        self._render_job = job.id
        self.render_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_hint.setText("Rendering. You can keep using the app while it runs.")

    def _cancel(self) -> None:
        job_id = (self._render_job or self._plan_job or self._capabilities_job
                  or self._qc_job)
        if job_id:
            self.context.jobs.cancel(job_id, "Stopped by the user.")
            self.status_hint.setText("Cancelling. FFmpeg will be stopped…")

    def _run_qc(self) -> None:
        path = str(self._last_result.get("path", "") or "")
        fmt = self.format
        if not path or fmt is None:
            self.status_hint.setText("Render a video first.")
            return
        job = self._submit(qc_spec(
            {"path": path, "expected_width": int(fmt.width),
             "expected_height": int(fmt.height), "expected_fps": float(fmt.fps),
             "expect_audio": self.include_audio.isChecked(), "deep": True}))
        if job is None:
            self.status_hint.setText("A quality check is already running.")
            return
        self._qc_job = job.id
        self.view_qc.setEnabled(False)
        self.status_hint.setText("Running the quality checks…")

    # -- job results ------------------------------------------------------

    def on_capabilities_finished(self, result) -> None:
        self._capabilities_job = None
        self.detect_button.setEnabled(True)
        if result.cancelled:
            self.codec_hint.setText("The FFmpeg check was cancelled.")
            return
        if result.error is not None:
            self.codec_hint.setText(f"The FFmpeg check failed: {result.error.title}")
            return
        value = result.value or {}
        caps = value.get("capabilities", {})
        self._codecs_by_container = value.get("codecs_by_container", {}) or {}
        self._detected = True
        encoders = caps.get("video_encoders", []) or []
        problems = value.get("problems", []) or []
        self._refresh_codec_list()
        if problems:
            self.codec_hint.setText(
                f"FFmpeg {caps.get('ffmpeg_version', '')} has {len(encoders)} video "
                f"encoder(s), but: " + " ".join(str(item) for item in problems))
        else:
            self.codec_hint.setText(
                f"FFmpeg {caps.get('ffmpeg_version', '')} has {len(encoders)} video "
                f"encoder(s): {', '.join(str(name) for name in encoders[:6])}.")

    def _refresh_codec_list(self) -> None:
        """Show only the codecs this FFmpeg can write into the chosen container."""
        container = str(self.container_combo.currentData() or "mp4")
        available = [str(name) for name in self._codecs_by_container.get(container, [])]
        current = str(self.codec_combo.currentData() or "")
        self.codec_combo.blockSignals(True)
        try:
            self.codec_combo.clear()
            for codec in VIDEO_CODECS:
                if not self._detected or codec in available:
                    self.codec_combo.addItem(codec, codec)
            # Keep a codec that is not available visible but clearly labelled,
            # so the user can see what they had chosen and why it will not work.
            if current and self.codec_combo.findData(current) < 0:
                self.codec_combo.addItem(f"{current}  (not available for .{container})",
                                         current)
            if self.codec_combo.count() == 0:
                self.codec_combo.addItem("No encoder available", "")
            self.codec_combo.setCurrentIndex(0)
        finally:
            self.codec_combo.blockSignals(False)

    def on_plan_finished(self, result) -> None:
        self._plan_job = None
        self.plan_button.setEnabled(True)
        if result.cancelled:
            self.plan_hint.setText("The plan was cancelled.")
            return
        if result.error is not None:
            self.plan_hint.setText(f"The plan failed: {result.error.title}")
            return
        value = result.value or {}
        self._show_plan(value)

    def _show_plan(self, value: dict) -> None:
        output = value.get("output", {}) or {}
        size = value.get("size_estimate", {}) or {}
        time = value.get("time_estimate", {}) or {}
        self.plan_grid.set_value("Length", f"{float(value.get('duration', 0.0)):.2f}s")
        self.plan_grid.set_value(
            "Frames", f"{int(value.get('frames', 0))} "
                      f"({int(value.get('segments', 0))} segment(s), "
                      f"{int(value.get('transitions', 0))} transition(s))")
        self.plan_grid.set_value("Resolution", str(value.get("resolution", "-")))
        self.plan_grid.set_value("Quality", str(value.get("quality", "-")))
        self.plan_grid.set_value("Output file",
                                 str(output.get("filename", "")) or "-")
        # Always labelled as an estimate - never presented as a measurement.
        self.plan_grid.set_value(
            "Estimated size",
            str(size.get("label", "")) or "Estimating…")
        self.plan_grid.set_value(
            "Estimated time",
            str(time.get("label", "")) or "Estimating…")

        errors = value.get("errors", []) or []
        warnings = value.get("warnings", []) or []
        if errors:
            self.plan_hint.setText(
                f"This render is blocked by {len(errors)} error(s). Fix them first.")
        elif not value.get("ready", False):
            self.plan_hint.setText("The plan is not ready yet.")
        else:
            self.plan_hint.setText(
                "Ready to render." + (f" {len(warnings)} warning(s) below."
                                      if warnings else ""))
        self.qc_view.setPlainText("\n".join(
            f"[{str(item.get('severity', 'warning')).upper()}] {item.get('code')}: "
            f"{item.get('message')}" for item in [*errors, *warnings])
            or "No problems found.")

    def on_render_finished(self, result) -> None:
        self._render_job = None
        self.render_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self._reload_history()
        if result.cancelled:
            self.status_hint.setText(
                "The render was cancelled. No video file was left behind, and the "
                "project is untouched.")
            self.qc_view.setPlainText("")
            return
        if result.error is not None:
            self.status_hint.setText(f"The render failed: {result.error.title}")
            return

        value = result.value or {}
        self._last_result = value
        status = str(value.get("status", ""))
        if status != "COMPLETED":
            self.status_hint.setText(str(value.get("message", "The render did not finish.")))
            lines = [str(value.get("what_to_do", ""))] if value.get("what_to_do") else []
            for item in value.get("errors", []) or []:
                if isinstance(item, dict):
                    lines.append(f"[{item.get('code')}] {item.get('message')}")
                else:
                    lines.append(str(item))
            self.qc_view.setPlainText("\n".join(line for line in lines if line))
            return

        path = str(value.get("path", ""))
        qc = value.get("qc") or {}
        verdict = str(qc.get("verdict", ""))
        seconds = float(value.get("seconds", 0.0) or 0.0)
        self.status_hint.setText(
            f"Rendered in {seconds:.1f}s. Quality check: {verdict or 'not run'}.")
        for button in (self.open_video, self.open_folder, self.copy_path, self.view_qc):
            button.setEnabled(bool(path))
        self.render_completed.emit(path)

        lines = [f"File: {path}"]
        measured = qc.get("measured", {}) or {}
        if measured:
            lines.append(
                f"Measured: {measured.get('width')}x{measured.get('height')} "
                f"@ {measured.get('fps')} fps, {measured.get('duration')}s, "
                f"{measured.get('video_codec')}/{measured.get('pixel_format')}, "
                f"audio {measured.get('audio_codec') or 'none'}")
        for issue in qc.get("issues", []) or []:
            lines.append(f"[{str(issue.get('severity', '')).upper()}] "
                         f"{issue.get('code')}: {issue.get('message')}")
        self.qc_view.setPlainText("\n".join(lines))
        if verdict == "FAIL":
            self.status_hint.setText(
                f"The file was written but the quality check FAILED: {verdict}. "
                f"Do not publish it until the report below is resolved.")

    def on_qc_finished(self, result) -> None:
        self._qc_job = None
        self.view_qc.setEnabled(True)
        if result.cancelled:
            self.status_hint.setText("The quality check was cancelled.")
            return
        if result.error is not None:
            self.status_hint.setText(f"The quality check failed: {result.error.title}")
            return
        value = result.value or {}
        measured = value.get("measured", {}) or {}
        lines = [f"Quality check: {value.get('verdict', '')}",
                 f"Measured: {measured.get('width')}x{measured.get('height')} "
                 f"@ {measured.get('fps')} fps, {measured.get('duration')}s, "
                 f"{measured.get('video_codec')}/{measured.get('pixel_format')}, "
                 f"audio {measured.get('audio_codec') or 'none'}"]
        for issue in value.get("issues", []) or []:
            lines.append(f"[{str(issue.get('severity', '')).upper()}] "
                         f"{issue.get('code')}: {issue.get('message')}")
        self.qc_view.setPlainText("\n".join(lines))
        self.status_hint.setText(f"Quality check: {value.get('verdict', '')}")

    # -- result actions ---------------------------------------------------

    def _open_video(self) -> None:
        """Hand the finished file to the system's default player."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from ..notifications import show_info

        path = Path(str(self._last_result.get("path", "") or ""))
        if not path.is_file():
            self.status_hint.setText("The rendered file is no longer there.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            show_info(self, "The video could not be opened.",
                      f"No application claimed the file.\n{path}")
            return
        self.status_hint.setText(f"Opened {path.name} in your default player.")

    def _open_output_folder(self) -> None:
        from ..notifications import open_folder

        path = self._last_result.get("path", "")
        folder = Path(path).parent if path else self._output_directory()
        if folder is not None and Path(folder).exists():
            open_folder(self, Path(folder))
        else:
            self.status_hint.setText("The output folder does not exist yet.")

    def _copy_path(self) -> None:
        from PySide6.QtWidgets import QApplication

        path = str(self._last_result.get("path", "") or "")
        if not path:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(path)
            self.status_hint.setText("The file path is on the clipboard.")


def _pair(left: QWidget, right: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(left)
    layout.addWidget(right)
    layout.addStretch(1)
    return holder
