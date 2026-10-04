"""Project settings - every stored section, editable after creation.

Directive sections 28-31: this page is the only place that edits the project's
sections, and it shows only settings that are real for the chosen encoder and
container.  Editing marks the project dirty; nothing is written until Save.

Every field below is a real field of the project model - there are no cosmetic
controls that look like settings but store nothing.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...project.presets import (
    ASPECT_PRESETS,
    CHANNEL_COLORS,
    CODECS_BY_CONTAINER,
    CODEC_LABELS,
    CONTAINERS,
    CONTAINER_NOTES,
    CRF_RANGES,
    ENCODER_PRESETS,
    FPS_LABELS,
    FPS_OPTIONS,
    PIXEL_FORMATS_BY_CODEC,
    QUALITY_NOTES,
    QUALITY_PRESETS,
    aspect_preset,
    preview_filename,
)
from ..theme import METRICS
from ..widgets.common import HintLabel, Page

#: The eight pages the directive lists.
TABS = (
    "General",
    "Format",
    "Quality",
    "Voice",
    "Audio",
    "Theme",
    "Export",
    "Assets",
)


class ProjectSettingsPage(Page):
    """Project settings tabs, one action per control, no silent defaults."""

    save_requested = Signal()
    discard_requested = Signal()
    project_changed = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__("Project settings", "Change the project's stored settings.", parent)
        self.context = context
        self._loading = False

        self.empty = QLabel("No project is open.")
        self.tabs = QTabWidget()
        self.save_button = QPushButton("Save changes")
        self.discard_button = QPushButton("Discard changes")
        self.status = HintLabel("")

        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.addWidget(self.empty)
            layout.addWidget(self.tabs)
            buttons = QHBoxLayout()
            buttons.setSpacing(METRICS.sm)
            self.save_button.clicked.connect(self.save_requested.emit)
            self.discard_button.clicked.connect(self.discard_requested.emit)
            buttons.addWidget(self.save_button)
            buttons.addWidget(self.discard_button)
            buttons.addStretch(1)
            wrapper = QWidget()
            wrapper.setLayout(buttons)
            layout.addWidget(wrapper)
            layout.addWidget(self.status)

        self._build_general_tab()
        self._build_format_tab()
        self._build_quality_tab()
        self._build_voice_tab()
        self._build_audio_tab()
        self._build_theme_tab()
        self._build_export_tab()
        self._build_assets_tab()
        self.refresh()

    @property
    def controller(self):
        return self.context.projects

    @property
    def project(self):
        return self.controller.project if self.controller is not None else None

    # -- tabs --------------------------------------------------------------

    def _build_general_tab(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.name_edit = QLineEdit()
        self.description_edit = QLineEdit()
        self.channel_edit = QLineEdit()
        self.channel_id_edit = QLineEdit()
        self.template_label = QLabel("")
        self.template_label.setObjectName("Hint")
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Channel", self.channel_edit)
        form.addRow("Channel id", self.channel_id_edit)
        form.addRow("Created from", self.template_label)
        form.addRow(HintLabel("Renaming the folder is a separate action on the Project page."))
        self.tabs.addTab(panel, "General")

    def _build_format_tab(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.aspect_combo = QComboBox()
        for preset in ASPECT_PRESETS:
            self.aspect_combo.addItem(f"{preset.label} ({preset.key})", preset.key)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(256, 7680)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(256, 7680)
        self.fps_combo = QComboBox()
        for fps in FPS_OPTIONS:
            self.fps_combo.addItem(f"{FPS_LABELS[fps]} ({fps} fps)", fps)
        form.addRow("Aspect ratio", self.aspect_combo)
        form.addRow("Width", self.width_spin)
        form.addRow("Height", self.height_spin)
        form.addRow("Frame rate", self.fps_combo)
        self.format_note = HintLabel("")
        form.addRow(self.format_note)
        self.aspect_combo.currentIndexChanged.connect(self._on_aspect_changed)
        self.tabs.addTab(panel, "Format")

    def _build_quality_tab(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.quality_combo = QComboBox()
        for preset in QUALITY_PRESETS:
            self.quality_combo.addItem(preset.label, preset.key)
        form.addRow("Quality", self.quality_combo)
        self.quality_note = HintLabel("")
        form.addRow(self.quality_note)
        layout.addLayout(form)

        self.advanced_box = QGroupBox("Encoder settings (only real options for the chosen codec)")
        form2 = QFormLayout(self.advanced_box)
        self.container_combo = QComboBox()
        self.container_combo.addItems(list(CONTAINERS))
        self.codec_combo = QComboBox()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(ENCODER_PRESETS))
        self.pixel_combo = QComboBox()
        self.crf_spin = QSpinBox()
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(0, 200000)
        self.bitrate_spin.setSpecialValueText("Use CRF instead")
        self.keyframe_spin = QSpinBox()
        self.keyframe_spin.setRange(1, 30)
        form2.addRow("Container", self.container_combo)
        form2.addRow("Video codec", self.codec_combo)
        form2.addRow("Encoder speed", self.preset_combo)
        form2.addRow("Pixel format", self.pixel_combo)
        form2.addRow("CRF (lower is better)", self.crf_spin)
        form2.addRow("Bitrate kbps (0 = CRF)", self.bitrate_spin)
        form2.addRow("Keyframe interval (seconds)", self.keyframe_spin)
        layout.addWidget(self.advanced_box)

        self.show_advanced = QCheckBox("Show advanced encoder settings")
        self.show_advanced.toggled.connect(self.advanced_box.setVisible)
        self.advanced_box.setVisible(False)
        layout.addWidget(self.show_advanced)
        layout.addStretch(1)

        self.container_combo.currentIndexChanged.connect(self._on_container_changed)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self.tabs.addTab(panel, "Quality")

    def _build_voice_tab(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.voice_engine = QComboBox()
        self.voice_engine.addItem("Kokoro-82M", "kokoro")
        self.voice_language = QComboBox()
        self.voice_gender = QComboBox()
        self.voice_gender.addItems(["", "female", "male", "other"])
        self.voice_name = QComboBox()
        self.voice_speed = QDoubleSpinBox()
        self.voice_speed.setRange(0.5, 2.0)
        self.voice_speed.setSingleStep(0.05)
        self.voice_volume = QDoubleSpinBox()
        self.voice_volume.setRange(0.0, 2.0)
        self.voice_volume.setSingleStep(0.05)
        form.addRow("Engine", self.voice_engine)
        form.addRow("Language", self.voice_language)
        form.addRow("Gender", self.voice_gender)
        form.addRow("Voice", self.voice_name)
        form.addRow("Speed", self.voice_speed)
        form.addRow("Volume", self.voice_volume)
        layout.addLayout(form)

        self.voice_note = HintLabel("")
        layout.addWidget(self.voice_note)
        refresh = QPushButton("Re-scan installed voices")
        refresh.clicked.connect(self._load_voice_catalogue)
        layout.addWidget(refresh, alignment=Qt.AlignLeft)
        layout.addStretch(1)
        self.tabs.addTab(panel, "Voice")

    def _build_audio_tab(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.narration_enabled = QCheckBox("Narration")
        self.narration_volume = QDoubleSpinBox()
        self.narration_volume.setRange(0.0, 2.0)
        self.narration_volume.setSingleStep(0.05)
        self.ducking_enabled = QCheckBox("Lower the music while someone speaks")
        self.ducking_level = QDoubleSpinBox()
        self.ducking_level.setRange(0.0, 1.0)
        self.ducking_level.setSingleStep(0.05)
        self.normalize_enabled = QCheckBox("Normalise loudness")
        self.target_lufs = QDoubleSpinBox()
        self.target_lufs.setRange(-40.0, -8.0)
        self.target_lufs.setSingleStep(0.5)
        self.audio_sample_rate = QComboBox()
        self.audio_sample_rate.addItems(["24000", "44100", "48000"])
        form.addRow("", self.narration_enabled)
        form.addRow("Narration volume", self.narration_volume)
        form.addRow("", self.ducking_enabled)
        form.addRow("Ducking level", self.ducking_level)
        form.addRow("", self.normalize_enabled)
        form.addRow("Target loudness (LUFS)", self.target_lufs)
        form.addRow("Audio sample rate", self.audio_sample_rate)
        form.addRow(
            HintLabel(
                "Music and sound-effect files are added in a later stage. The codec and bitrate "
                "used for the final file are on the Quality and Export pages."
            )
        )
        self.tabs.addTab(panel, "Audio")

    def _build_theme_tab(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.theme_id = QLineEdit()
        self.theme_id.setPlaceholderText("clean-dark")
        self.bg_color = QLineEdit()
        self.bg_color.setPlaceholderText("#101014")
        self.accent_color = QLineEdit()
        self.accent_color.setPlaceholderText("#4c8dff")
        self.heading_font = QLineEdit()
        self.body_font = QLineEdit()
        self.heading_scale = QDoubleSpinBox()
        self.heading_scale.setRange(0.5, 6.0)
        self.heading_scale.setSingleStep(0.1)
        self.body_scale = QDoubleSpinBox()
        self.body_scale.setRange(0.5, 4.0)
        self.body_scale.setSingleStep(0.1)
        self.subtitles_enabled = QCheckBox("Burn subtitles into the video")
        self.subtitle_font = QLineEdit()
        self.subtitle_size = QSpinBox()
        self.subtitle_size.setRange(8, 160)
        form.addRow("Theme id", self.theme_id)
        form.addRow("Background colour", self.bg_color)
        form.addRow("Accent colour", self.accent_color)
        form.addRow("Heading font", self.heading_font)
        form.addRow("Body font", self.body_font)
        form.addRow("Heading scale", self.heading_scale)
        form.addRow("Body scale", self.body_scale)
        form.addRow("", self.subtitles_enabled)
        form.addRow("Subtitle font", self.subtitle_font)
        form.addRow("Subtitle size", self.subtitle_size)

        swatches = QHBoxLayout()
        for name, value in CHANNEL_COLORS.items():
            button = QPushButton(name)
            button.setStyleSheet(f"background: {value}; color: white;")
            button.setFixedWidth(96)
            button.clicked.connect(lambda _checked=False, color=value: self.accent_color.setText(color))
            swatches.addWidget(button)
        swatches.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(swatches)
        form.addRow("Accent presets", wrapper)
        self.tabs.addTab(panel, "Theme")

    def _build_export_tab(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.filename_template = QLineEdit()
        self.output_dir = QLineEdit()
        self.sequence_spin = QSpinBox()
        self.sequence_spin.setRange(1, 9999)
        form.addRow("Output file name", self.filename_template)
        self.output_preview = HintLabel("")
        form.addRow("Example", self.output_preview)
        form.addRow("Output folder (inside the data folder)", self.output_dir)
        form.addRow("Next file number", self.sequence_spin)
        form.addRow(
            HintLabel(
                "An existing output file is never replaced: the number increases until the name "
                "is free."
            )
        )
        self.filename_template.textChanged.connect(self._refresh_output_preview)
        self.tabs.addTab(panel, "Export")

    def _build_assets_tab(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.assets_list = QLabel("")
        self.assets_list.setWordWrap(True)
        self.assets_list.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.assets_list)
        layout.addWidget(
            HintLabel("Importing, relinking and replacing files happens on the Project page.")
        )
        layout.addStretch(1)
        self.tabs.addTab(panel, "Assets")

    # -- loading -----------------------------------------------------------

    def refresh(self) -> None:
        project = self.project
        has_project = project is not None
        self.empty.setVisible(not has_project)
        self.tabs.setVisible(has_project)
        self.save_button.setVisible(has_project)
        self.discard_button.setVisible(has_project)
        self.status.setVisible(has_project)
        if not has_project:
            return

        self._loading = True
        meta = project.project
        fmt = project.format

        self.name_edit.setText(meta.name)
        self.description_edit.setText(meta.description)
        self.channel_edit.setText(meta.channel_name)
        self.channel_id_edit.setText(meta.channel_id)
        self.template_label.setText(meta.template or "not from a template")

        self._select_combo(self.aspect_combo, fmt.aspect_ratio)
        self.width_spin.setValue(fmt.width)
        self.height_spin.setValue(fmt.height)
        self._select_combo(self.fps_combo, fmt.fps)
        self.format_note.setText(
            "Changing the aspect ratio suggests matching resolutions; odd sizes are rounded down "
            "because the H.264 encoder needs even numbers."
        )

        self._select_combo(self.quality_combo, fmt.quality_preset)
        self.container_combo.setCurrentText(fmt.container)
        self._on_container_changed(load_codec=fmt.codec)
        self.preset_combo.setCurrentText(fmt.encoder_preset)
        self.crf_spin.setValue(fmt.crf)
        self.bitrate_spin.setValue(fmt.bitrate_kbps)
        self.keyframe_spin.setValue(max(1, fmt.keyframe_interval))
        self._update_quality_note()

        self._select_combo(self.voice_engine, project.voice.engine)
        self.voice_speed.setValue(project.voice.speed)
        self.voice_volume.setValue(project.voice.volume)
        self._load_voice_catalogue(
            selected_language=project.voice.language, selected_voice=project.voice.voice
        )
        self.voice_gender.setCurrentText(project.voice.gender)

        audio = project.audio
        self.narration_enabled.setChecked(audio.narration_enabled)
        self.narration_volume.setValue(audio.narration_volume)
        self.ducking_enabled.setChecked(audio.ducking_enabled)
        self.ducking_level.setValue(audio.ducking_level)
        self.normalize_enabled.setChecked(audio.normalize_enabled)
        self.target_lufs.setValue(audio.target_lufs)
        self.audio_sample_rate.setCurrentText(str(audio.sample_rate))

        theme = project.theme
        self.theme_id.setText(theme.id)
        self.bg_color.setText(theme.background)
        self.accent_color.setText(theme.accent)
        self.heading_font.setText(theme.typography.heading_font)
        self.body_font.setText(theme.typography.body_font)
        self.heading_scale.setValue(theme.typography.heading_scale)
        self.body_scale.setValue(theme.typography.body_scale)
        self.subtitles_enabled.setChecked(theme.subtitle_style.enabled)
        self.subtitle_font.setText(theme.subtitle_style.font)
        self.subtitle_size.setValue(theme.subtitle_style.font_size)

        export = project.export
        self.filename_template.setText(export.filename_template)
        self.output_dir.setText(export.output_dir)
        self.sequence_spin.setValue(max(1, export.next_sequence_number))
        self._refresh_output_preview()

        self._refresh_assets_summary()
        self._loading = False
        self.status.setText("These values are the project's stored settings.")

    def _select_combo(self, combo: QComboBox, value) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value or combo.itemText(index) == str(value):
                combo.setCurrentIndex(index)
                return

    # -- reactive updates --------------------------------------------------

    def _on_aspect_changed(self) -> None:
        if self._loading:
            return
        preset = aspect_preset(str(self.aspect_combo.currentData() or ""))
        if preset is None or not preset.resolutions:
            return
        width, height = preset.resolutions[-1]
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)
        self.format_note.setText(
            "Suggested: " + ", ".join(f"{w}x{h}" for w, h in preset.resolutions)
        )

    def _on_container_changed(self, load_codec: str = "") -> None:
        container = self.container_combo.currentText()
        self.container_combo.setToolTip(CONTAINER_NOTES.get(container, ""))
        current = load_codec or str(self.codec_combo.currentData() or "")
        self.codec_combo.blockSignals(True)
        self.codec_combo.clear()
        for codec in CODECS_BY_CONTAINER.get(container, ()):
            self.codec_combo.addItem(CODEC_LABELS.get(codec, codec), codec)
        self.codec_combo.blockSignals(False)
        for index in range(self.codec_combo.count()):
            if self.codec_combo.itemData(index) == current:
                self.codec_combo.setCurrentIndex(index)
                break
        self._on_codec_changed()

    def _on_codec_changed(self) -> None:
        codec = str(self.codec_combo.currentData() or "h264_cpu")
        low, high = CRF_RANGES.get(codec, (0, 51))
        self.crf_spin.setRange(low, high)
        self.crf_spin.setValue(min(max(self.crf_spin.value(), low), high))
        current_pixel = self.pixel_combo.currentText()
        self.pixel_combo.blockSignals(True)
        self.pixel_combo.clear()
        self.pixel_combo.addItems(list(PIXEL_FORMATS_BY_CODEC.get(codec, ("yuv420p",))))
        self.pixel_combo.blockSignals(False)
        if current_pixel:
            index = self.pixel_combo.findText(current_pixel)
            if index >= 0:
                self.pixel_combo.setCurrentIndex(index)
        self._update_quality_note()

    def _update_quality_note(self) -> None:
        self.quality_note.setText(QUALITY_NOTES.get(str(self.quality_combo.currentData() or ""), ""))

    def _load_voice_catalogue(self, selected_language: str = "", selected_voice: str = "") -> None:
        from ...tools.kokoro import discover_voices

        catalogue = discover_voices(
            model_dir=self.context.paths.kokoro_model_dir,
            extra_dirs=[self.context.paths.models_dir],
        )
        self.voice_language.blockSignals(True)
        self.voice_language.clear()
        self.voice_language.addItem("Any language", "")
        for language in catalogue.languages():
            self.voice_language.addItem(language, language)
        self.voice_language.blockSignals(False)
        if selected_language:
            self._select_combo(self.voice_language, selected_language)

        language = str(self.voice_language.currentData() or "")
        self.voice_name.blockSignals(True)
        self.voice_name.clear()
        self.voice_name.addItem("First available voice", "")
        for voice in catalogue.by_language(language):
            self.voice_name.addItem(voice.id, voice.id)
        self.voice_name.blockSignals(False)
        if selected_voice:
            self._select_combo(self.voice_name, selected_voice)

        self.voice_note.setText(catalogue.headline())

    def _refresh_output_preview(self) -> None:
        template = self.filename_template.text() or "{project}_{n}"
        project = self.project
        name = project.project.name if project is not None else "My Project"
        channel = project.project.channel_name if project is not None else ""
        sequence = int(self.sequence_spin.value())
        self.output_preview.setText(preview_filename(template, name, channel, sequence))

    def _refresh_assets_summary(self) -> None:
        project = self.project
        if project is None:
            return
        layout = self.controller.layout
        lines = [f"{len(project.assets)} asset(s) recorded."]
        for asset in project.assets:
            state = "MISSING" if asset.missing else "ready"
            lines.append(
                f"• {asset.label()} [{asset.kind or 'unknown'}] - {state}\n"
                f"   {asset.resolve(layout.root)}"
            )
        self.assets_list.setText("\n".join(lines))

    # -- writing back ------------------------------------------------------

    def collect_changes(self) -> dict:
        """Everything the user changed, as a dict for the service to apply."""
        return {
            "meta": {
                "name": self.name_edit.text().strip(),
                "description": self.description_edit.text().strip(),
                "channel_id": self.channel_id_edit.text().strip(),
                "channel_name": self.channel_edit.text().strip(),
            },
            "format": {
                "aspect_ratio": str(self.aspect_combo.currentData() or ""),
                "width": int(self.width_spin.value()),
                "height": int(self.height_spin.value()),
                "fps": int(self.fps_combo.currentData() or 30),
                "quality_preset": str(self.quality_combo.currentData() or "high"),
                "container": self.container_combo.currentText(),
                "codec": str(self.codec_combo.currentData() or "h264_cpu"),
                "encoder_preset": self.preset_combo.currentText(),
                "pixel_format": self.pixel_combo.currentText(),
                "crf": int(self.crf_spin.value()),
                "bitrate_kbps": int(self.bitrate_spin.value()),
                "keyframe_interval": int(self.keyframe_spin.value()),
            },
            "voice": {
                "engine": str(self.voice_engine.currentData() or "kokoro"),
                "language": str(self.voice_language.currentData() or ""),
                "gender": self.voice_gender.currentText(),
                "voice": str(self.voice_name.currentData() or ""),
                "speed": float(self.voice_speed.value()),
                "volume": float(self.voice_volume.value()),
            },
            "audio": {
                "narration_enabled": self.narration_enabled.isChecked(),
                "narration_volume": float(self.narration_volume.value()),
                "ducking_enabled": self.ducking_enabled.isChecked(),
                "ducking_level": float(self.ducking_level.value()),
                "normalize_enabled": self.normalize_enabled.isChecked(),
                "target_lufs": float(self.target_lufs.value()),
                "sample_rate": int(self.audio_sample_rate.currentText()),
            },
            "theme": {
                "id": self.theme_id.text().strip() or "custom",
                "background": self.bg_color.text().strip(),
                "accent": self.accent_color.text().strip(),
                "typography": {
                    "heading_font": self.heading_font.text().strip(),
                    "body_font": self.body_font.text().strip(),
                    "heading_scale": float(self.heading_scale.value()),
                    "body_scale": float(self.body_scale.value()),
                },
                "subtitle_style": {
                    "enabled": self.subtitles_enabled.isChecked(),
                    "font": self.subtitle_font.text().strip(),
                    "font_size": int(self.subtitle_size.value()),
                },
            },
            "export": {
                "filename_template": self.filename_template.text().strip(),
                "output_dir": self.output_dir.text().strip(),
                "next_sequence_number": int(self.sequence_spin.value()),
            },
        }

    def apply_changes(self) -> None:
        """Push the collected values into the open project (marks it dirty)."""
        if self.project is None:
            return
        changes = self.collect_changes()

        def mutate(current):
            meta = current.project
            for key, value in changes["meta"].items():
                if key == "name" and not value:
                    continue          # a project always keeps a name
                setattr(meta, key, value)

            for key, value in changes["format"].items():
                if hasattr(current.format, key):
                    setattr(current.format, key, value)

            for key, value in changes["voice"].items():
                if hasattr(current.voice, key):
                    setattr(current.voice, key, value)

            for key, value in changes["audio"].items():
                if hasattr(current.audio, key):
                    setattr(current.audio, key, value)

            theme = current.theme
            for key, value in changes["theme"].items():
                if isinstance(value, dict):
                    section = getattr(theme, key)
                    for sub_key, sub_value in value.items():
                        if hasattr(section, sub_key):
                            setattr(section, sub_key, sub_value)
                elif hasattr(theme, key):
                    setattr(theme, key, value)

            for key, value in changes["export"].items():
                if hasattr(current.export, key):
                    setattr(current.export, key, value)

        self.controller.service.edit("Project settings", mutate)
        self.project_changed.emit()
        self.status.setText("Changed - press “Save changes” to write project.json.")


__all__ = ["ProjectSettingsPage", "TABS"]
