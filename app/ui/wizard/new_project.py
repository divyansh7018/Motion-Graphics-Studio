"""The New Project wizard (directive section 5).

Nine short steps, plain language, no dead ends:

1. name · 2. channel · 3. template · 4. format · 5. resolution · 6. fps ·
7. quality · 8. voice · 9. create

Two rules matter here.  The wizard shows **only real choices**: the voice step
lists voices discovered from the installed engine, and when none are installed
it says so instead of inventing names.  And every step feeds a single
:class:`CreateRequest`, which the service turns into a project - the wizard
never writes a file itself (section 34).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from ...core.settings import Settings
from ...project.presets import (
    ASPECT_PRESETS,
    CODECS_BY_CONTAINER,
    CODEC_LABELS,
    CONTAINERS,
    CRF_RANGES,
    ENCODER_PRESETS,
    FPS_LABELS,
    FPS_OPTIONS,
    PIXEL_FORMATS_BY_CODEC,
    PROJECT_TEMPLATES,
    QUALITY_PRESETS,
    aspect_preset,
    preview_filename,
    resolve_quality,
)
from ...project.service import CreateRequest, validate_project_name
from ...tools.kokoro import VoiceCatalogue
from ..theme import METRICS
from ..widgets.common import HintLabel

# Wizard field ids
FIELD_NAME = 1
FIELD_DESCRIPTION = 2
FIELD_CHANNEL = 3
FIELD_TEMPLATE = 4
FIELD_ASPECT = 5
FIELD_WIDTH = 6
FIELD_HEIGHT = 7
FIELD_FPS = 8
FIELD_QUALITY = 9
FIELD_LANGUAGE = 10
FIELD_GENDER = 11
FIELD_VOICE = 12


class _RadioGroup(QWidget):
    """A vertical list of radio buttons built from (key, label, note) tuples."""

    def __init__(self, options, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.xs)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QRadioButton] = {}
        for key, label, note in options:
            button = QRadioButton(label)
            button.setToolTip(note)
            self.group.addButton(button)
            self.buttons[key] = button
            layout.addWidget(button)
            if note:
                hint = HintLabel(note)
                hint.setContentsMargins(METRICS.xl, 0, 0, 0)
                layout.addWidget(hint)

    def set_current(self, key: str) -> None:
        button = self.buttons.get(key)
        if button is not None:
            button.setChecked(True)

    def current(self) -> str:
        button = self.group.checkedButton()
        for key, candidate in self.buttons.items():
            if candidate is button:
                return key
        return next(iter(self.buttons), "")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

class NamePage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("1. Name your project")
        self.setSubTitle("This is the name you will see everywhere in the application.")

        layout = QVBoxLayout(self)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example: Product launch video")
        self.name_edit.setMaxLength(120)
        self.registerField("name*", self.name_edit)
        layout.addWidget(QLabel("Project name"))
        layout.addWidget(self.name_edit)

        self.folder_hint = HintLabel("")
        layout.addWidget(self.folder_hint)

        layout.addWidget(QLabel("Description (optional)"))
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("What is this video about?")
        self.description_edit.setMaximumHeight(80)
        layout.addWidget(self.description_edit)

        self.problem = HintLabel("")
        layout.addWidget(self.problem)
        layout.addStretch(1)

        self.name_edit.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        name = self.name_edit.text().strip()
        problems = validate_project_name(name)
        self.problem.setText(" ".join(problems))
        if name:
            from ...core.paths import safe_filename

            self.folder_hint.setText(f"It will be saved in a folder called “{safe_filename(name)}”.")
        else:
            self.folder_hint.setText("")
        self.completeChanged.emit()

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        return not validate_project_name(self.name_edit.text().strip())

    def values(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.description_edit.toPlainText().strip()


class ChannelPage(QWizardPage):
    def __init__(self, channels, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("2. Choose a channel")
        self.setSubTitle("A channel keeps your brand defaults - logo, colours, fonts, voice.")
        self.channels = channels

        layout = QVBoxLayout(self)
        self.combo = QComboBox()
        for profile in channels:
            self.combo.addItem(profile.name or profile.id, profile.id)
        layout.addWidget(self.combo)

        self.detail = HintLabel("")
        layout.addWidget(self.detail)
        layout.addWidget(
            HintLabel(
                "The project stores the settings it needs, so changing a channel later "
                "never alters a finished project."
            )
        )
        layout.addStretch(1)
        self.combo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        profile = self.channels[self.combo.currentIndex()] if self.combo.currentIndex() >= 0 else None
        if profile is None:
            self.detail.setText("")
            return
        self.detail.setText(
            f"Default format: {profile.width}x{profile.height} @ {profile.fps} fps\n"
            f"Default quality: {profile.quality}\n"
            f"Voice: {profile.voice or 'first available'}\n"
            f"Output name: {preview_filename(profile.filename_template, 'My Video', profile.name)}"
        )

    def channel_id(self) -> str:
        return str(self.combo.currentData() or "")

    def channel_name(self) -> str:
        return self.combo.currentText()


class TemplatePage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("3. What kind of video is this?")
        self.setSubTitle("A template only sets starting values - nothing is added to your project.")

        layout = QVBoxLayout(self)
        self.radios = _RadioGroup(
            [(template.key, template.label, template.description) for template in PROJECT_TEMPLATES]
        )
        self.radios.set_current("youtube")
        layout.addWidget(self.radios)
        layout.addStretch(1)

    def template_key(self) -> str:
        return self.radios.current()

    def template(self):
        from ...project.presets import project_template

        return project_template(self.template_key()) or PROJECT_TEMPLATES[0]


class FormatPage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("4. Video format")
        self.setSubTitle("The shape of the video. You can change this later in Project settings.")

        layout = QVBoxLayout(self)
        self.radios = _RadioGroup(
            [(preset.key, f"{preset.label}  ({preset.key})", preset.note) for preset in ASPECT_PRESETS]
        )
        self.radios.set_current("16:9")
        layout.addWidget(self.radios)
        layout.addStretch(1)

    def aspect(self) -> str:
        return self.radios.current()


class ResolutionPage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("5. Resolution")
        self.setSubTitle("How many pixels wide and tall the video is.")

        layout = QVBoxLayout(self)
        self.combo = QComboBox()
        layout.addWidget(self.combo)

        custom_box = QGroupBox("Custom size")
        form = QFormLayout(custom_box)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(256, 7680)
        self.width_spin.setSingleStep(2)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(256, 7680)
        self.height_spin.setSingleStep(2)
        form.addRow("Width", self.width_spin)
        form.addRow("Height", self.height_spin)
        layout.addWidget(custom_box)

        self.note = HintLabel("Even numbers only - the H.264 encoder requires it.")
        layout.addWidget(self.note)
        layout.addStretch(1)

        self.combo.currentIndexChanged.connect(self._on_choice)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        wizard = self.wizard()
        aspect_key = wizard.format_page.aspect() if hasattr(wizard, "format_page") else "16:9"
        preset = aspect_preset(aspect_key)
        self.combo.blockSignals(True)
        self.combo.clear()
        for width, height in (preset.resolutions if preset else ((1920, 1080),)):
            self.combo.addItem(f"{width} x {height}", (width, height))
        self.combo.addItem("Custom…", None)
        self.combo.blockSignals(False)
        # Prefer the biggest sensible option, which is the second entry for
        # most presets (1080p rather than 720p).
        if self.combo.count() > 2:
            self.combo.setCurrentIndex(1)
        self._on_choice()

    def _on_choice(self) -> None:
        data = self.combo.currentData()
        is_custom = data is None
        self.width_spin.setEnabled(is_custom)
        self.height_spin.setEnabled(is_custom)
        if not is_custom:
            self.width_spin.setValue(int(data[0]))
            self.height_spin.setValue(int(data[1]))
            self.note.setText("Even numbers only - the H.264 encoder requires it.")
        else:
            self.note.setText("Enter an even width and height between 256 and 7680 pixels.")

    def resolution(self) -> tuple[int, int]:
        width = int(self.width_spin.value())
        height = int(self.height_spin.value())
        return width - (width % 2), height - (height % 2)


class FpsPage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("6. Frame rate")
        self.setSubTitle("How many pictures per second. 30 fps is the usual choice.")

        layout = QVBoxLayout(self)
        self.radios = _RadioGroup([(str(fps), FPS_LABELS[fps], "") for fps in FPS_OPTIONS])
        self.radios.set_current("30")
        layout.addWidget(self.radios)
        layout.addStretch(1)

    def fps(self) -> int:
        return int(self.radios.current())


class QualityPage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("7. Quality")
        self.setSubTitle("Higher quality means a slower encode on a CPU-only machine.")

        layout = QVBoxLayout(self)
        self.radios = _RadioGroup(
            [(preset.key, preset.label, preset.note) for preset in QUALITY_PRESETS]
            + [("custom", "Custom", "Choose the encoder settings yourself.")]
        )
        self.radios.set_current("high")
        layout.addWidget(self.radios)

        advanced = QGroupBox("Advanced (only shown settings are real encoder options)")
        form = QFormLayout(advanced)
        self.container_combo = QComboBox()
        self.container_combo.addItems(list(CONTAINERS))
        self.codec_combo = QComboBox()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(ENCODER_PRESETS))
        self.preset_combo.setCurrentText("slow")
        self.pixel_combo = QComboBox()
        self.crf_spin = QSpinBox()
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(0, 200000)
        self.bitrate_spin.setSpecialValueText("Use CRF instead")
        self.audio_bitrate_spin = QSpinBox()
        self.audio_bitrate_spin.setRange(64, 512)
        self.audio_bitrate_spin.setValue(192)
        form.addRow("Container", self.container_combo)
        form.addRow("Video codec", self.codec_combo)
        form.addRow("Encoder speed", self.preset_combo)
        form.addRow("Pixel format", self.pixel_combo)
        form.addRow("CRF (lower is better)", self.crf_spin)
        form.addRow("Bitrate kbps (0 = CRF)", self.bitrate_spin)
        form.addRow("Audio bitrate kbps", self.audio_bitrate_spin)
        advanced.setVisible(False)
        self.advanced_box = advanced
        layout.addWidget(advanced)

        self.show_advanced = QCheckBox("Show advanced encoder settings")
        self.show_advanced.toggled.connect(advanced.setVisible)
        layout.addWidget(self.show_advanced)
        layout.addStretch(1)

        self.container_combo.currentIndexChanged.connect(self._refresh_codecs)
        self.codec_combo.currentIndexChanged.connect(self._refresh_codec_options)
        self.radios.group.buttonToggled.connect(self._refresh_preset)
        self._refresh_codecs()
        self._refresh_preset()

    def _refresh_codecs(self) -> None:
        container = self.container_combo.currentText()
        current = self.codec_combo.currentText()
        self.codec_combo.blockSignals(True)
        self.codec_combo.clear()
        for codec in CODECS_BY_CONTAINER.get(container, ()):
            self.codec_combo.addItem(CODEC_LABELS.get(codec, codec), codec)
        self.codec_combo.blockSignals(False)
        for index in range(self.codec_combo.count()):
            if self.codec_combo.itemData(index) == current:
                self.codec_combo.setCurrentIndex(index)
                break
        self._refresh_codec_options()

    def _refresh_codec_options(self) -> None:
        codec = str(self.codec_combo.currentData() or "h264_cpu")
        current_pixel = self.pixel_combo.currentText()
        self.pixel_combo.blockSignals(True)
        self.pixel_combo.clear()
        self.pixel_combo.addItems(list(PIXEL_FORMATS_BY_CODEC.get(codec, ("yuv420p",))))
        self.pixel_combo.blockSignals(False)
        if current_pixel:
            index = self.pixel_combo.findText(current_pixel)
            if index >= 0:
                self.pixel_combo.setCurrentIndex(index)
        low, high = CRF_RANGES.get(codec, (0, 51))
        self.crf_spin.setRange(low, high)
        self.crf_spin.setValue(min(max(20, low), high))

    def _refresh_preset(self) -> None:
        key = self.radios.current()
        preset = None
        for candidate in QUALITY_PRESETS:
            if candidate.key == key:
                preset = candidate
        if preset is not None:
            self.preset_combo.setCurrentText(preset.encoder_preset)
            self.crf_spin.setValue(preset.crf)
            self.bitrate_spin.setValue(preset.bitrate_kbps)
            self.audio_bitrate_spin.setValue(preset.audio_bitrate_kbps)
        self.advanced_box.setEnabled(key == "custom" or self.show_advanced.isChecked())

    def quality(self) -> dict:
        key = self.radios.current()
        container = self.container_combo.currentText()
        codec = str(self.codec_combo.currentData() or "h264_cpu")
        resolved = resolve_quality(key, container=container, codec=codec)
        resolved["codec"] = codec
        resolved["container"] = container
        resolved["encoder_preset"] = self.preset_combo.currentText()
        resolved["pixel_format"] = self.pixel_combo.currentText()
        resolved["crf"] = int(self.crf_spin.value())
        resolved["bitrate_kbps"] = int(self.bitrate_spin.value())
        resolved["audio_bitrate_kbps"] = int(self.audio_bitrate_spin.value())
        if key != "custom":
            preset = None
            for candidate in QUALITY_PRESETS:
                if candidate.key == key:
                    preset = candidate
            if preset is not None:
                resolved.update(
                    {
                        "encoder_preset": preset.encoder_preset,
                        "crf": preset.crf,
                        "bitrate_kbps": preset.bitrate_kbps,
                        "audio_bitrate_kbps": preset.audio_bitrate_kbps,
                    }
                )
        return resolved


class VoicePage(QWizardPage):
    def __init__(self, catalogue: VoiceCatalogue, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.catalogue = catalogue
        self.setTitle("8. Narration voice")
        self.setSubTitle("Language, then gender, then the voice itself.")

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.language_combo = QComboBox()
        self.gender_combo = QComboBox()
        self.voice_combo = QComboBox()
        form.addRow("Language", self.language_combo)
        form.addRow("Gender", self.gender_combo)
        form.addRow("Voice", self.voice_combo)
        layout.addLayout(form)

        self.status = HintLabel(catalogue.headline())
        layout.addWidget(self.status)

        if not catalogue.available:
            layout.addWidget(
                HintLabel(
                    "The project will use the first voice the engine offers once Kokoro is "
                    "installed (Stage C). You can pick a specific voice later in Project "
                    "settings - no fake voice names are shown here."
                )
            )
        layout.addStretch(1)

        self.language_combo.currentIndexChanged.connect(self._refresh_genders)
        self.gender_combo.currentIndexChanged.connect(self._refresh_voices)
        self._refresh_languages()

    def _refresh_languages(self) -> None:
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        self.language_combo.addItem("Any language", "")
        for language in self.catalogue.languages():
            self.language_combo.addItem(language, language)
        self.language_combo.blockSignals(False)
        self._refresh_genders()

    def _refresh_genders(self) -> None:
        language = str(self.language_combo.currentData() or "")
        self.gender_combo.blockSignals(True)
        self.gender_combo.clear()
        self.gender_combo.addItem("Any gender", "")
        for gender in self.catalogue.genders(language):
            self.gender_combo.addItem(gender, gender)
        self.gender_combo.blockSignals(False)
        self._refresh_voices()

    def _refresh_voices(self) -> None:
        language = str(self.language_combo.currentData() or "")
        gender = str(self.gender_combo.currentData() or "")
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("First available voice", "")
        for voice in self.catalogue.by_language_and_gender(language, gender):
            self.voice_combo.addItem(voice.id, voice.id)
        self.voice_combo.blockSignals(False)
        count = self.voice_combo.count() - 1
        self.status.setText(
            f"{count} matching voice(s)." if count else self.catalogue.headline()
        )

    def selection(self) -> tuple[str, str, str]:
        """Language, gender and voice id.

        When a specific voice is chosen its own gender is recorded, even if the
        gender filter was left on "Any" - the project stores the truth about the
        voice rather than about the filter that was on screen.
        """
        language = str(self.language_combo.currentData() or "")
        gender = str(self.gender_combo.currentData() or "")
        voice = str(self.voice_combo.currentData() or "")
        if voice:
            for candidate in self.catalogue.voices:
                if candidate.id == voice:
                    gender = candidate.gender or gender
                    language = candidate.language or language
                    break
        return language, gender, voice


class SummaryPage(QWizardPage):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitle("9. Create the project")
        self.setSubTitle("Check the settings below, then press Finish.")

        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        wizard = self.wizard()
        request = wizard.create_request()
        width, height = request.width, request.height
        quality = request.quality if isinstance(request.quality, dict) else resolve_quality("high")
        lines = [
            f"Name:        {request.name}",
            f"Channel:     {request.channel_name or '(none)'}",
            f"Template:    {request.template}",
            f"Format:      {width}x{height} @ {request.fps} fps",
            f"Quality:     {quality.get('quality_preset')} - {quality.get('codec')}, "
            f"{quality.get('encoder_preset')}, CRF {quality.get('crf')}",
            f"Container:   {quality.get('container')}",
            f"Voice:       {request.voice or 'first available'} ({request.language or 'any'})",
            "",
            "The folder will contain project.json plus assets/, audio/, scenes/,",
            "generated/, previews/, renders/, backups/ and autosave/.",
        ]
        self.summary.setText("\n".join(lines))


# --------------------------------------------------------------------------
# The wizard
# --------------------------------------------------------------------------

class NewProjectWizard(QWizard):
    """Collects everything :class:`CreateRequest` needs."""

    def __init__(
        self,
        channels,
        catalogue: VoiceCatalogue,
        settings: Settings,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("New project")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setMinimumSize(620, 520)

        self.name_page = NamePage(self)
        self.channel_page = ChannelPage(channels, self)
        self.template_page = TemplatePage(self)
        self.format_page = FormatPage(self)
        self.resolution_page = ResolutionPage(self)
        self.fps_page = FpsPage(self)
        self.quality_page = QualityPage(self)
        self.voice_page = VoicePage(catalogue, self)
        self.summary_page = SummaryPage(self)

        for page in (
            self.name_page,
            self.channel_page,
            self.template_page,
            self.format_page,
            self.resolution_page,
            self.fps_page,
            self.quality_page,
            self.voice_page,
            self.summary_page,
        ):
            self.addPage(page)

        # Sensible starting values from the application settings.
        defaults = settings.project_defaults
        self.name_page.name_edit.setText(defaults.title if defaults.title != "Untitled Project" else "")

    # -- result ------------------------------------------------------------

    def create_request(self) -> CreateRequest:
        template = self.template_page.template()
        width, height = self.resolution_page.resolution()
        language, gender, voice = self.voice_page.selection()
        request = CreateRequest(
            name=self.name_page.values()[0],
            description=self.name_page.values()[1],
            channel_id=self.channel_page.channel_id(),
            channel_name=self.channel_page.channel_name(),
            template=template.key,
            width=width,
            height=height,
            fps=self.fps_page.fps(),
            quality=self.quality_page.quality(),
            background=template.background,
            accent=template.accent,
            heading_font=template.heading_font,
            body_font=template.body_font,
            subtitles_enabled=template.subtitles_enabled,
            subtitle_font_size=template.subtitle_font_size,
            filename_template=template.filename_template,
            language=language or "en-us",
            gender=gender,
            voice=voice,
            script_note=template.script_note,
        )
        return request


__all__ = ["NewProjectWizard"]
