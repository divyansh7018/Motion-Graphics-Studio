"""Settings page: sections, values, validation and safe saving.

Behaviour that matters for stability (directive sections 12, 13, 29, 52):

* Values are validated/clamped before they are stored - a typo cannot put the
  application into an impossible state.
* Nothing heavy happens while the user types: options that need detection
  (FFmpeg path, voice model folder) are verified only when the user presses
  "Test" / "Re-check".
* Changes that require a restart (the data folder) say so instead of silently
  taking effect next launch.
* "Restore defaults" asks for confirmation and keeps a backup of the old file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.settings import (
    LOG_LEVELS,
    RENDER_QUALITY_PRESETS,
    SUPPORTED_FPS,
    Settings,
    disk_free_bytes,
)
from ...tools import ffmpeg as ffmpeg_tools
from ...tools import kokoro as kokoro_tools
from ..notifications import ask_confirm, open_folder, show_info
from ..theme import METRICS, mark_primary
from ..widgets.common import Card, HintLabel, Page


class SettingsPage(Page):
    """Editor for every user-visible option."""

    settings_applied = Signal(object)   # Settings
    restart_required = Signal(str)      # reason

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__("Settings", "Changes are saved as soon as you press Apply, with a backup of the previous file.", parent)
        self.context = context
        self._loading = False

        self.tabs = QTabWidget(self)
        self.add(self.tabs)
        self.tabs.addTab(self._build_general_tab(), "General")
        self.tabs.addTab(self._build_folders_tab(), "Folders")
        self.tabs.addTab(self._build_media_tab(), "Media tools")
        self.tabs.addTab(self._build_voice_tab(), "Voice")
        self.tabs.addTab(self._build_output_tab(), "Video & preview")
        self.tabs.addTab(self._build_autosave_tab(), "Saving")
        self.tabs.addTab(self._build_advanced_tab(), "Advanced")

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.reset_button = QPushButton("Restore defaults")
        self.reset_button.clicked.connect(self._on_reset)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)

        self.revert_button = QPushButton("Revert changes")
        self.revert_button.clicked.connect(self.load_from_settings)
        buttons.addWidget(self.revert_button)

        self.apply_button = QPushButton("Apply")
        mark_primary(self.apply_button)
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        self.add(QWidget())

        container = QWidget()
        container.setLayout(buttons)
        self.body().addWidget(container)

        self.load_from_settings()

    # -- tab builders ------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("Appearance")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Match Windows", "system")
        form.addRow("Theme", self.theme_combo)

        self.advanced_check = QCheckBox("Show advanced tools (scenes, timeline, render settings)")
        self.advanced_check.setToolTip(
            "Beginner mode keeps the interface simple. Advanced mode reveals scene editing, "
            "the element tree and render options as those stages are released."
        )
        form.addRow("", self.advanced_check)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Behaviour")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)

        self.confirm_exit_check = QCheckBox("Ask before closing when work is unsaved")
        form.addRow("", self.confirm_exit_check)

        self.single_instance_check = QCheckBox("Allow only one copy of the application at a time")
        self.single_instance_check.setToolTip(
            "Prevents two windows from writing the same project at the same time, which could lose work."
        )
        form.addRow("", self.single_instance_check)

        self.monitor_check = QCheckBox("Show CPU and memory usage in the status bar")
        self.monitor_check.setToolTip("Only shown when the optional 'psutil' package is installed.")
        form.addRow("", self.monitor_check)
        card.body().addLayout(form)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_folders_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("Where your work is stored")
        card.add(HintLabel(
            "The data folder holds projects, assets, models, caches and exported videos. "
            "Changing it needs a restart."
        ))

        self.data_root_edit = QLineEdit()
        self.data_root_edit.setReadOnly(True)
        self.data_root_edit.setToolTip("Current data folder (read-only here).")

        row = QHBoxLayout()
        row.addWidget(self.data_root_edit, 1)

        choose_button = QPushButton("Choose folder...")
        choose_button.clicked.connect(self._choose_data_root)
        row.addWidget(choose_button)

        open_button = QPushButton("Open folder")
        open_button.clicked.connect(lambda: open_folder(self, self.context.paths.data_root))
        row.addWidget(open_button)
        card.body().addLayout(row)

        self.pending_data_root: Optional[Path] = None
        self.data_root_note = HintLabel("")
        card.add(self.data_root_note)

        card.add(HintLabel(
            "The folder must be writable. A folder inside Documents is recommended. "
            "Avoid Program Files - Windows protects it."
        ))
        layout.addWidget(card)

        space_card = Card("Disk space")
        self.space_label = QLabel("")
        self.space_label.setWordWrap(True)
        space_card.add(self.space_label)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self._refresh_space)
        space_card.body().addWidget(refresh_button)
        layout.addWidget(space_card)
        layout.addStretch(1)
        return page

    def _build_media_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("FFmpeg and FFprobe")
        card.add(HintLabel(
            "FFmpeg creates the video file and FFprobe checks it afterwards. "
            "Leave the path empty to let the application search the 'tools' folder and the Windows PATH."
        ))

        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_edit.setPlaceholderText("Folder containing ffmpeg.exe, or the full path to ffmpeg.exe")
        row = QHBoxLayout()
        row.addWidget(self.ffmpeg_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_ffmpeg)
        row.addWidget(browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.ffmpeg_edit.setText(""))
        row.addWidget(clear)
        card.body().addLayout(row)

        test_button = QPushButton("Test FFmpeg now")
        test_button.clicked.connect(self._test_ffmpeg)
        card.body().addWidget(test_button)

        self.ffmpeg_status = QLabel("")
        self.ffmpeg_status.setWordWrap(True)
        card.add(self.ffmpeg_status)

        self.bundled_check = QCheckBox("Also look in the application's own 'tools' folder")
        card.add(self.bundled_check)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 256)
        self.threads_spin.setSpecialValueText("Automatic")
        self.threads_spin.setToolTip("How many CPU threads FFmpeg may use. Automatic leaves one free for the interface.")

        self.hw_combo = QComboBox()
        for value, label in (
            ("auto", "Automatic (use hardware only if it is available)"),
            ("off", "Always use the CPU (recommended - works on every PC)"),
            ("nvenc", "NVIDIA (NVENC) - only if you have an NVIDIA GPU"),
            ("qsv", "Intel Quick Sync - only if it is enabled"),
            ("amf", "AMD (AMF) - only if you have an AMD GPU"),
        ):
            self.hw_combo.addItem(label, value)

        self.preset_combo = QComboBox()
        for value in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"):
            self.preset_combo.addItem(value, value)
        self.preset_combo.setToolTip("CPU encoding speed. Faster presets produce larger files.")

        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        form.addRow("CPU threads", self.threads_spin)
        form.addRow("Hardware encoding", self.hw_combo)
        form.addRow("Encoder speed", self.preset_combo)
        card.body().addLayout(form)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_voice_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("Voice engine")
        card.add(HintLabel(
            "This build uses Kokoro-82M for narration. Other voice engines are not supported."
        ))
        self.voice_status = QLabel("")
        self.voice_status.setWordWrap(True)
        card.add(self.voice_status)

        self.voice_recheck_button = QPushButton("Check voice engine now")
        self.voice_recheck_button.clicked.connect(self._check_voice)
        card.body().addWidget(self.voice_recheck_button)

        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        self.model_dir_edit = QLineEdit()
        self.model_dir_edit.setPlaceholderText("Leave empty to use the models folder inside the data folder")
        row = QHBoxLayout()
        row.addWidget(self.model_dir_edit, 1)
        model_browse = QPushButton("Browse...")
        model_browse.clicked.connect(self._choose_model_dir)
        row.addWidget(model_browse)
        form.addRow("Model folder", self._wrap(row))
        card.body().addLayout(form)

        self.online_check = QCheckBox("Allow the one-time model download when Kokoro is used for the first time")
        card.add(self.online_check)
        card.add(HintLabel(
            "Everything else in the application works without an internet connection. "
            "The download only happens once, and only for the voice model."
        ))
        layout.addWidget(card)

        quality_card = Card("Narration defaults")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setSingleStep(0.05)
        self.speed_spin.setDecimals(2)
        form.addRow("Speaking speed", self.speed_spin)

        self.volume_spin = QDoubleSpinBox()
        self.volume_spin.setRange(0.0, 2.0)
        self.volume_spin.setSingleStep(0.05)
        self.volume_spin.setDecimals(2)
        form.addRow("Volume", self.volume_spin)

        self.preview_text_edit = QLineEdit()
        self.preview_text_edit.setPlaceholderText("Text used by the voice Preview button")
        form.addRow("Preview text", self.preview_text_edit)

        self.voice_threads_spin = QSpinBox()
        self.voice_threads_spin.setRange(0, 256)
        self.voice_threads_spin.setSpecialValueText("Automatic")
        form.addRow("CPU threads", self.voice_threads_spin)
        quality_card.body().addLayout(form)
        layout.addWidget(quality_card)
        layout.addStretch(1)
        return page

    def _build_output_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("New project defaults")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(256, 7680)
        self.width_spin.setSingleStep(2)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(256, 7680)
        self.height_spin.setSingleStep(2)

        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)
        size_row.addStretch(1)
        form.addRow("Resolution", self._wrap(size_row))

        self.fps_combo = QComboBox()
        for fps in SUPPORTED_FPS:
            self.fps_combo.addItem(f"{fps} fps", fps)
        form.addRow("Frame rate", self.fps_combo)

        self.quality_combo = QComboBox()
        for quality in RENDER_QUALITY_PRESETS:
            self.quality_combo.addItem(quality.capitalize(), quality)
        form.addRow("Default quality", self.quality_combo)

        self.title_edit = QLineEdit()
        form.addRow("Default title", self.title_edit)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Export")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)

        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setToolTip("Lower is better quality and a larger file. 18-24 is a good range.")
        form.addRow("Quality (CRF)", self.crf_spin)

        self.audio_bitrate_spin = QSpinBox()
        self.audio_bitrate_spin.setRange(64, 512)
        self.audio_bitrate_spin.setSingleStep(16)
        self.audio_bitrate_spin.setSuffix(" kbps")
        form.addRow("Audio bitrate", self.audio_bitrate_spin)

        self.container_combo = QComboBox()
        self.container_combo.addItem("MP4 (plays everywhere)", "mp4")
        self.container_combo.addItem("MKV (more tolerant, fewer players)", "mkv")
        form.addRow("File format", self.container_combo)

        self.keep_frames_check = QCheckBox("Keep temporary render frames (only for troubleshooting)")
        self.keep_frames_check.setToolTip("Leaving this off keeps the disk clean after a render.")
        form.addRow("", self.keep_frames_check)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Preview quality")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        self.draft_scale_spin = QDoubleSpinBox()
        self.draft_scale_spin.setRange(0.1, 1.0)
        self.draft_scale_spin.setSingleStep(0.05)
        self.draft_scale_spin.setDecimals(2)
        form.addRow("Draft scale", self.draft_scale_spin)

        self.draft_fps_spin = QSpinBox()
        self.draft_fps_spin.setRange(5, 60)
        form.addRow("Draft fps", self.draft_fps_spin)

        self.medium_scale_spin = QDoubleSpinBox()
        self.medium_scale_spin.setRange(0.1, 1.0)
        self.medium_scale_spin.setSingleStep(0.05)
        self.medium_scale_spin.setDecimals(2)
        form.addRow("Medium scale", self.medium_scale_spin)

        self.medium_fps_spin = QSpinBox()
        self.medium_fps_spin.setRange(5, 60)
        form.addRow("Medium fps", self.medium_fps_spin)
        card.body().addLayout(form)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_autosave_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("Saving and recovery")
        card.add(HintLabel(
            "Autosave writes a separate recovery copy while you work, so a crash or power cut cannot "
            "destroy your project. The project file itself is only replaced when you press Save."
        ))
        form = QFormLayout()
        form.setSpacing(METRICS.sm)

        self.autosave_check = QCheckBox("Enable autosave")
        form.addRow("", self.autosave_check)

        self.autosave_interval_spin = QSpinBox()
        self.autosave_interval_spin.setRange(15, 3600)
        self.autosave_interval_spin.setSuffix(" s")
        form.addRow("Autosave interval", self.autosave_interval_spin)

        self.autosave_focus_check = QCheckBox("Also save when the window loses focus")
        form.addRow("", self.autosave_focus_check)

        self.keep_autosaves_spin = QSpinBox()
        self.keep_autosaves_spin.setRange(0, 500)
        form.addRow("Autosaves to keep", self.keep_autosaves_spin)

        self.keep_backups_spin = QSpinBox()
        self.keep_backups_spin.setRange(0, 500)
        form.addRow("Backups to keep per file", self.keep_backups_spin)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Log files")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        self.log_level_combo = QComboBox()
        for level in LOG_LEVELS:
            self.log_level_combo.addItem(level, level)
        form.addRow("Log detail", self.log_level_combo)

        self.log_days_spin = QSpinBox()
        self.log_days_spin.setRange(1, 3650)
        self.log_days_spin.setSuffix(" days")
        form.addRow("Keep logs for", self.log_days_spin)
        card.body().addLayout(form)

        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(lambda: open_folder(self, self.context.paths.logs_dir))
        card.body().addWidget(open_logs)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(METRICS.md, METRICS.md, METRICS.md, METRICS.md)
        layout.setSpacing(METRICS.md)

        card = Card("Image generation")
        card.add(HintLabel(
            "Image AI is optional. Without it you can still import images and use built-in graphics."
        ))
        self.image_backend_combo = QComboBox()
        self.image_backend_combo.addItem("None - import and built-in graphics only", "none")
        self.image_backend_combo.addItem("Built-in graphics only", "procedural")
        self.image_backend_combo.addItem("Local AI model (when installed)", "local_ai")
        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        form.addRow("Backend", self.image_backend_combo)

        self.image_fit_combo = QComboBox()
        for value, label in (
            ("cover", "Cover - fill the frame, crop what does not fit (default)"),
            ("contain", "Contain - show the whole image, add background if needed"),
            ("crop", "Crop"),
            ("center", "Centre without scaling"),
            ("anchor", "Anchor to a position"),
        ):
            self.image_fit_combo.addItem(label, value)
        form.addRow("Default image fit", self.image_fit_combo)

        self.thumbnail_spin = QSpinBox()
        self.thumbnail_spin.setRange(64, 1024)
        self.thumbnail_spin.setSuffix(" px")
        form.addRow("Thumbnail size", self.thumbnail_spin)

        self.max_megapixels_spin = QSpinBox()
        self.max_megapixels_spin.setRange(1, 500)
        self.max_megapixels_spin.setSuffix(" MP")
        form.addRow("Maximum import size", self.max_megapixels_spin)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Subtitles")
        self.subtitles_check = QCheckBox("Generate subtitles from narration (off by default)")
        card.add(self.subtitles_check)
        form = QFormLayout()
        form.setSpacing(METRICS.sm)
        self.subtitle_lines_spin = QSpinBox()
        self.subtitle_lines_spin.setRange(1, 6)
        form.addRow("Maximum lines", self.subtitle_lines_spin)
        self.subtitle_size_spin = QSpinBox()
        self.subtitle_size_spin.setRange(12, 200)
        self.subtitle_size_spin.setSuffix(" px")
        form.addRow("Font size", self.subtitle_size_spin)
        self.subtitle_safe_spin = QDoubleSpinBox()
        self.subtitle_safe_spin.setRange(0.0, 30.0)
        self.subtitle_safe_spin.setSuffix(" %")
        form.addRow("Safe area", self.subtitle_safe_spin)
        card.body().addLayout(form)
        layout.addWidget(card)

        card = Card("Diagnostics")
        card.add(HintLabel(
            "Caches and temporary files are cleaned from the Maintenance page. "
            "Nothing here deletes projects or exported videos."
        ))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _wrap(layout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    # -- load / apply ------------------------------------------------------

    def load_from_settings(self) -> None:
        """Copy the current settings into the widgets."""
        self._loading = True
        settings: Settings = self.context.settings
        try:
            # General
            index = self.theme_combo.findData(settings.general.theme)
            self.theme_combo.setCurrentIndex(max(0, index))
            self.advanced_check.setChecked(settings.general.advanced_mode)
            self.confirm_exit_check.setChecked(settings.general.confirm_on_exit)
            self.single_instance_check.setChecked(settings.general.single_instance)
            self.monitor_check.setChecked(settings.general.show_resource_monitor)

            # Folders
            self.pending_data_root = None
            self.data_root_edit.setText(str(self.context.paths.data_root))
            self.data_root_note.setText("")
            self._refresh_space()

            # Media
            self.ffmpeg_edit.setText(settings.media.ffmpeg_path)
            self.bundled_check.setChecked(settings.media.use_bundled_tools)
            self.threads_spin.setValue(settings.media.thread_count)
            self._set_combo(self.hw_combo, settings.media.hardware_encoding)
            self._set_combo(self.preset_combo, settings.media.encoder_preset)
            self.ffmpeg_status.setText("Press 'Test FFmpeg now' to check the current path.")

            # Voice
            self.model_dir_edit.setText(settings.voice.model_dir)
            self.online_check.setChecked(settings.voice.allow_online_first_run)
            self.speed_spin.setValue(settings.voice.speed)
            self.volume_spin.setValue(settings.voice.volume)
            self.preview_text_edit.setText(settings.voice.preview_text)
            self.voice_threads_spin.setValue(settings.voice.threads)
            self._refresh_voice_status()

            # Video & preview
            self.width_spin.setValue(settings.project_defaults.width)
            self.height_spin.setValue(settings.project_defaults.height)
            self._set_combo(self.fps_combo, settings.project_defaults.fps)
            self._set_combo(self.quality_combo, settings.project_defaults.quality)
            self.title_edit.setText(settings.project_defaults.title)
            self.crf_spin.setValue(settings.media.crf)
            self.audio_bitrate_spin.setValue(settings.media.audio_bitrate_kbps)
            self._set_combo(self.container_combo, settings.media.output_container)
            self.keep_frames_check.setChecked(settings.media.keep_temp_frames)
            self.draft_scale_spin.setValue(settings.preview.draft_scale)
            self.draft_fps_spin.setValue(settings.preview.draft_fps)
            self.medium_scale_spin.setValue(settings.preview.medium_scale)
            self.medium_fps_spin.setValue(settings.preview.medium_fps)

            # Saving
            self.autosave_check.setChecked(settings.autosave.enabled)
            self.autosave_interval_spin.setValue(settings.autosave.interval_seconds)
            self.autosave_focus_check.setChecked(settings.autosave.autosave_on_focus_loss)
            self.keep_autosaves_spin.setValue(settings.autosave.keep_autosaves)
            self.keep_backups_spin.setValue(settings.autosave.keep_backups)
            self._set_combo(self.log_level_combo, settings.logging.level)
            self.log_days_spin.setValue(settings.logging.keep_days)

            # Advanced
            self._set_combo(self.image_backend_combo, settings.image.backend)
            self._set_combo(self.image_fit_combo, settings.image.default_fit)
            self.thumbnail_spin.setValue(settings.image.thumbnail_size)
            self.max_megapixels_spin.setValue(settings.image.max_import_megapixels)
            self.subtitles_check.setChecked(settings.subtitles.enabled)
            self.subtitle_lines_spin.setValue(settings.subtitles.max_lines)
            self.subtitle_size_spin.setValue(settings.subtitles.font_size)
            self.subtitle_safe_spin.setValue(settings.subtitles.safe_area_percent)
        finally:
            self._loading = False

    @staticmethod
    def _set_combo(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def collect(self) -> Settings:
        """Build a Settings object from the current widget values."""
        settings = self.context.settings.copy()

        settings.general.theme = self.theme_combo.currentData()
        settings.general.advanced_mode = self.advanced_check.isChecked()
        settings.general.confirm_on_exit = self.confirm_exit_check.isChecked()
        settings.general.single_instance = self.single_instance_check.isChecked()
        settings.general.show_resource_monitor = self.monitor_check.isChecked()

        settings.media.ffmpeg_path = self.ffmpeg_edit.text().strip()
        settings.media.use_bundled_tools = self.bundled_check.isChecked()
        settings.media.thread_count = self.threads_spin.value()
        settings.media.hardware_encoding = self.hw_combo.currentData()
        settings.media.encoder_preset = self.preset_combo.currentData()
        settings.media.crf = self.crf_spin.value()
        settings.media.audio_bitrate_kbps = self.audio_bitrate_spin.value()
        settings.media.output_container = self.container_combo.currentData()
        settings.media.keep_temp_frames = self.keep_frames_check.isChecked()

        settings.voice.model_dir = self.model_dir_edit.text().strip()
        settings.voice.allow_online_first_run = self.online_check.isChecked()
        settings.voice.speed = self.speed_spin.value()
        settings.voice.volume = self.volume_spin.value()
        settings.voice.preview_text = self.preview_text_edit.text()
        settings.voice.threads = self.voice_threads_spin.value()

        settings.project_defaults.width = self.width_spin.value()
        settings.project_defaults.height = self.height_spin.value()
        settings.project_defaults.fps = self.fps_combo.currentData()
        settings.project_defaults.quality = self.quality_combo.currentData()
        settings.project_defaults.title = self.title_edit.text().strip() or "Untitled Project"

        settings.preview.draft_scale = self.draft_scale_spin.value()
        settings.preview.draft_fps = self.draft_fps_spin.value()
        settings.preview.medium_scale = self.medium_scale_spin.value()
        settings.preview.medium_fps = self.medium_fps_spin.value()

        settings.autosave.enabled = self.autosave_check.isChecked()
        settings.autosave.interval_seconds = self.autosave_interval_spin.value()
        settings.autosave.autosave_on_focus_loss = self.autosave_focus_check.isChecked()
        settings.autosave.keep_autosaves = self.keep_autosaves_spin.value()
        settings.autosave.keep_backups = self.keep_backups_spin.value()
        settings.logging.level = self.log_level_combo.currentData()
        settings.logging.keep_days = self.log_days_spin.value()

        settings.image.backend = self.image_backend_combo.currentData()
        settings.image.default_fit = self.image_fit_combo.currentData()
        settings.image.thumbnail_size = self.thumbnail_spin.value()
        settings.image.max_import_megapixels = self.max_megapixels_spin.value()
        settings.subtitles.enabled = self.subtitles_check.isChecked()
        settings.subtitles.max_lines = self.subtitle_lines_spin.value()
        settings.subtitles.font_size = self.subtitle_size_spin.value()
        settings.subtitles.safe_area_percent = self.subtitle_safe_spin.value()

        return settings

    def apply(self) -> bool:
        """Validate, save and publish the settings."""
        settings = self.collect()
        normalized, notes = settings.normalized()
        if notes:
            show_info(
                self,
                "Some values were adjusted so the application stays reliable:",
                "\n".join(f"• {note}" for note in notes),
            )
        applied = self.context.apply_settings(normalized, save=True, reason="settings page")
        if applied:
            self.load_from_settings()
            self.settings_applied.emit(normalized)
            self.context.notify("Settings saved.")
            if self.pending_data_root is not None:
                self.restart_required.emit(
                    "The data folder change will take effect after a restart."
                )
        return applied

    # -- actions -----------------------------------------------------------

    def _refresh_space(self) -> None:
        free = disk_free_bytes(Path(self.context.paths.data_root))
        from ...core.atomicio import human_size

        self.space_label.setText(
            f"Free space on the data drive: {human_size(free)}\n"
            f"Data folder: {self.context.paths.data_root}"
        )

    def _choose_data_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose the data folder", str(self.context.paths.data_root))
        if not chosen:
            return
        candidate = Path(chosen)
        from ...core.paths import directory_is_writable

        if not directory_is_writable(candidate):
            show_info(self, "That folder cannot be written to.", f"Choose another folder.\n\n{candidate}")
            return
        self.pending_data_root = candidate
        self.data_root_note.setText(
            f"Will change to: {candidate}\nThe change takes effect the next time the application starts. "
            "Existing projects are not moved."
        )

    def _choose_ffmpeg(self) -> None:
        file_name, _filter = QFileDialog.getOpenFileName(
            self,
            "Select ffmpeg.exe",
            self.ffmpeg_edit.text() or str(self.context.paths.tools_dir),
            "Executable (*.exe);;All files (*)",
        )
        if file_name:
            self.ffmpeg_edit.setText(file_name)

    def _choose_model_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose the folder with the Kokoro model files", self.model_dir_edit.text() or "")
        if chosen:
            self.model_dir_edit.setText(chosen)

    def _test_ffmpeg(self) -> None:
        path_text = self.ffmpeg_edit.text().strip()
        discovery = ffmpeg_tools.discover_ffmpeg(
            source_root=Path(self.context.paths.source_root) if self.bundled_check.isChecked() else None,
            ffmpeg_dir_setting=path_text if Path(path_text).is_dir() else "",
            ffmpeg_exe_setting=path_text if Path(path_text).is_file() else "",
            extra_dirs=(Path(self.context.paths.tools_dir), Path(self.context.paths.data_root)),
        )
        if discovery.is_complete:
            self.ffmpeg_status.setText(
                f"✓ {discovery.ffmpeg.path}\n   version {discovery.ffmpeg.version or 'unknown'}\n"
                f"✓ {discovery.ffprobe.path}"
            )
            self.context.notify("FFmpeg and FFprobe were found.")
        else:
            lines = ["✗ " + discovery.summary()]
            if discovery.errors:
                lines.extend(discovery.errors[:3])
            lines.append("Put ffmpeg.exe and ffprobe.exe in the 'tools' folder, or set the path above.")
            self.ffmpeg_status.setText("\n".join(lines))

    def _refresh_voice_status(self) -> None:
        model_dir = Path(self.model_dir_edit.text().strip()) if self.model_dir_edit.text().strip() else Path(self.context.paths.kokoro_model_dir)
        status = kokoro_tools.probe_kokoro(model_dir=model_dir, deep_import_check=False)
        glyph = "✓" if status.usable else "⚠"
        lines = [f"{glyph} {status.headline()}"]
        lines.extend(status.details())
        self.voice_status.setText("\n".join(lines))

    def _check_voice(self) -> None:
        self.voice_recheck_button.setEnabled(False)
        self.voice_recheck_button.setText("Checking...")
        try:
            self._refresh_voice_status()
            self.context.notify("Voice engine checked.")
        finally:
            self.voice_recheck_button.setEnabled(True)
            self.voice_recheck_button.setText("Check voice engine now")

    def _on_reset(self) -> None:
        if not ask_confirm(
            self,
            "Restore all settings to their defaults?",
            "A backup of the current settings file is kept in the backups folder. "
            "Your projects, assets and exported videos are not affected.",
            confirm_label="Restore defaults",
            dangerous=True,
        ):
            return
        self.context.reset_settings()
        self.load_from_settings()
        self.context.notify("Settings restored to defaults.")

    # -- external updates --------------------------------------------------

    def reload(self) -> None:
        """Reload the widgets from the current settings (called on theme change)."""
        self.load_from_settings()

    def refresh_dynamic(self) -> None:
        """Refresh values that can change outside this page."""
        if hasattr(self, "space_label"):
            self._refresh_space()
