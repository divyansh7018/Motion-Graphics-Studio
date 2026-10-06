"""The Image Studio page (Stage F, sections 1, 48, 49, 50, 51, 68).

Layout, as the directive describes it:

**Left** - the mode, the prompts, the model and the settings.
**Centre** - a large preview of the selected image.
**Right** - the generation history and the actions.

Three rules shape this page:

* It **opens and works with nothing installed.**  Importing, editing, upscaling
  and organising images need no model, and the page says so in plain words
  rather than showing dead controls.
* **Nothing pretends.**  A generation that cannot run is refused with the reason,
  a disabled control carries the explanation, and the button that says "Standard
  Resize" is never labelled "AI Upscale".
* **No work on the Qt thread.**  Detection, generation, upscaling, editing and
  thumbnail building all go through the Stage A job system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.core.logging_setup import log_event
from app.image.integration import Placement, send_to_scene
from app.image.jobs import detect_spec, generate_spec, upscale_spec
from app.image.provider import GenerationMode, GenerationRequest, MODE_LABELS, MODES
from app.jobs.keys import JobKeys
from app.image.service import ImageService
from app.image.variants import Origin

from ..theme import mark_primary
from ..widgets.common import ButtonRow, Card, HintLabel, Page

#: Sizes offered in the interface.  Custom width/height is always available.
ASPECT_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("1:1  1024x1024", 1024, 1024),
    ("16:9  1280x720", 1280, 720),
    ("9:16  1080x1920", 1080, 1920),
    ("4:5  1080x1350", 1080, 1350),
    ("4:3  1024x768", 1024, 768),
    ("3:4  768x1024", 768, 1024),
    ("1:1  512x512", 512, 512),
    ("16:9  1920x1080", 1920, 1080),
)

#: Edit operations the interface offers, with the parameters each needs.
EDIT_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("Crop centre 10%", "crop"),
    ("Resize to 512 wide", "resize"),
    ("Rotate 90", "rotate"),
    ("Flip horizontal", "flip_horizontal"),
    ("Brighten", "brightness"),
    ("Contrast", "contrast"),
    ("Saturate", "saturation"),
    ("Sharpen", "sharpen"),
    ("Blur", "blur"),
    ("Grayscale", "grayscale"),
    ("Rounded corners", "rounded_corners"),
)


class ImageStudioPage(Page):
    """The Image Studio."""

    project_changed = Signal()

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Image Studio",
            "Create, import, edit and organise images - with or without a local "
            "model.",
            parent,
        )
        self.context = context
        self.service: Optional[ImageService] = None
        self._detect_job: Optional[str] = None
        self._generate_job: Optional[str] = None
        self._upscale_job: Optional[str] = None
        self._thumbnail_job: Optional[str] = None
        self._current_image: Optional[Path] = None
        self._edit_session: Any = None
        self._all_paths: list[Path] = []

        self._build_state_card()
        self._build_splitter()
        self._build_actions()
        # One handler for every job this page starts, routed by key - the same
        # pattern the other pages use.
        manager = getattr(self.context, "jobs", None)
        if manager is not None:
            try:
                manager.job_finished.connect(self._on_job_finished)
            except Exception:  # noqa: BLE001 - the page still works without it
                pass
        self.refresh()

    def _on_job_finished(self, result: Any) -> None:
        """Route a finished job to the handler that expects it."""
        key = str(getattr(result, "key", ""))
        job_id = str(getattr(result, "job_id", ""))
        if key == JobKeys.IMAGE_DETECT and job_id == self._detect_job:
            self._on_detected(job_value(result))
        elif key in (JobKeys.IMAGE_GENERATE, JobKeys.IMAGE_BATCH) \
                and job_id == self._generate_job:
            self._on_generated(job_value(result))
        elif key == JobKeys.IMAGE_UPSCALE and job_id == self._upscale_job:
            self._on_upscaled(job_value(result))

    # -- construction ------------------------------------------------------

    def _build_state_card(self) -> None:
        card = Card("Local image backends")
        self.state_label = QLabel("Not checked yet.")
        self.state_label.setWordWrap(True)
        card.body().addWidget(self.state_label)

        self.device_label = HintLabel("")
        card.body().addWidget(self.device_label)

        row = ButtonRow()
        self.detect_button = QPushButton("Check backends")
        self.detect_button.clicked.connect(self.detect_backends)
        row.add_button(self.detect_button)
        self.import_button = QPushButton("Import an image...")
        self.import_button.clicked.connect(self.import_image)
        row.add_button(self.import_button)
        card.body().addWidget(row)
        self.body().addWidget(card)

    def _build_splitter(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_centre())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        self.body().addWidget(splitter, 1)

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)

        card = Card("Generate")
        form = QFormLayout()

        self.mode_box = QComboBox()
        for mode in MODES:
            self.mode_box.addItem(MODE_LABELS.get(mode, mode), mode)
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode", self.mode_box)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the image you want. A short sentence is enough.")
        self.prompt_edit.setFixedHeight(80)
        self.prompt_edit.textChanged.connect(self._update_generate_enabled)
        form.addRow("Prompt", self.prompt_edit)

        self.negative_edit = QPlainTextEdit()
        self.negative_edit.setPlaceholderText(
            "What to avoid (only used by backends that support it).")
        self.negative_edit.setFixedHeight(54)
        form.addRow("Negative", self.negative_edit)

        self.backend_box = QComboBox()
        self.backend_box.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow("Backend", self.backend_box)

        self.model_box = QComboBox()
        form.addRow("Model", self.model_box)

        self.size_box = QComboBox()
        for label, width, height in ASPECT_PRESETS:
            self.size_box.addItem(label, (width, height))
        self.size_box.addItem("Custom", (0, 0))
        self.size_box.currentIndexChanged.connect(self._on_size_changed)
        form.addRow("Size", self.size_box)

        size_row = QHBoxLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 8192)
        self.width_spin.setValue(1024)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 8192)
        self.height_spin.setValue(1024)
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)
        form.addRow("Width x Height", _wrap(size_row))

        self.advanced_toggle = QPushButton("Advanced settings")
        self.advanced_toggle.setCheckable(True)
        # A checkable button emits `clicked` after the state has flipped, and
        # the application's convention is that every button connects `clicked`
        # (a test asserts none is left with nothing attached).
        self.advanced_toggle.clicked.connect(
            lambda: self._toggle_advanced(self.advanced_toggle.isChecked()))
        form.addRow("", self.advanced_toggle)

        self.advanced_panel = QWidget()
        advanced = QFormLayout(self.advanced_panel)
        advanced.setContentsMargins(0, 0, 0, 0)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2 ** 31 - 1)
        self.seed_spin.setToolTip("0 picks a random seed; the one used is reported.")
        advanced.addRow("Seed", self.seed_spin)

        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(0, 500)
        self.steps_spin.setSpecialValueText("backend default")
        advanced.addRow("Steps", self.steps_spin)

        self.guidance_spin = QSpinBox()
        self.guidance_spin.setRange(0, 30)
        self.guidance_spin.setSpecialValueText("backend default")
        advanced.addRow("Guidance", self.guidance_spin)

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 16)
        self.batch_spin.setToolTip(
            "A batch is one explicit action. Each image gets its own name.")
        advanced.addRow("Batch", self.batch_spin)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Source image (for edit modes)")
        browse = QPushButton("...")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._pick_source)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(browse)
        advanced.addRow("Source", _wrap(source_row))

        self.advanced_panel.setVisible(False)
        form.addRow(self.advanced_panel)

        card.body().addLayout(form)
        self._on_size_changed()
        self.generate_hint = HintLabel("")
        card.body().addWidget(self.generate_hint)
        layout.addWidget(card)
        layout.addStretch(1)
        return panel

    def _build_centre(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 8, 0)

        card = Card("Preview")
        self.preview_label = QLabel("Nothing selected.")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(420, 320)
        self.preview_label.setStyleSheet("border: 1px solid #303040;")
        card.body().addWidget(self.preview_label, 1)
        self.preview_info = HintLabel("")
        card.body().addWidget(self.preview_info)
        layout.addWidget(card, 1)

        self.edit_card = Card("Edit (creates a new version)")
        self.edit_list = QListWidget()
        self.edit_list.setMaximumHeight(120)
        for label, operation in EDIT_OPERATIONS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, operation)
            self.edit_list.addItem(item)
        self.edit_card.body().addWidget(self.edit_list)
        edit_row = ButtonRow()
        self.apply_edit_button = QPushButton("Apply edit")
        self.apply_edit_button.clicked.connect(self.apply_edit)
        self.reset_edit_button = QPushButton("Reset edits")
        self.reset_edit_button.clicked.connect(self.reset_edits)
        self.save_edit_button = QPushButton("Save as new image")
        self.save_edit_button.clicked.connect(self.save_edited)
        mark_primary(self.save_edit_button)
        for button in (self.apply_edit_button, self.reset_edit_button,
                       self.save_edit_button):
            edit_row.add_button(button)
        self.edit_card.body().addWidget(edit_row)
        self.edit_hint = HintLabel(
            "Edits are applied to the file you selected and saved as a new "
            "image. The original is never modified.")
        self.edit_card.body().addWidget(self.edit_hint)
        layout.addWidget(self.edit_card)
        return panel

    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        panel.setMinimumWidth(300)

        card = Card("History")
        self.history_list = QListWidget()
        self.history_list.currentItemChanged.connect(self._on_history_selected)
        card.body().addWidget(self.history_list)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        card.body().addWidget(refresh)
        layout.addWidget(card, 1)

        info = Card("Details")
        self.details_label = HintLabel("Nothing selected.")
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.body().addWidget(self.details_label)
        layout.addWidget(info)
        return panel

    def _build_actions(self) -> None:
        row = ButtonRow()
        self.generate_button = QPushButton("Generate")
        self.generate_button.clicked.connect(self.generate)
        mark_primary(self.generate_button)
        row.add_button(self.generate_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        row.add_button(self.stop_button)

        self.regenerate_button = QPushButton("Regenerate")
        self.regenerate_button.clicked.connect(self.regenerate)
        row.add_button(self.regenerate_button)

        self.variation_button = QPushButton("Variation")
        self.variation_button.clicked.connect(self.make_variation)
        row.add_button(self.variation_button)

        self.upscale_button = QPushButton("Upscale 2x")
        self.upscale_button.clicked.connect(self.upscale_image)
        row.add_button(self.upscale_button)

        self.use_button = QPushButton("Use in project")
        self.use_button.clicked.connect(self.use_in_project)
        row.add_button(self.use_button)

        self.scene_button = QPushButton("Send to scene")
        self.scene_button.clicked.connect(self.send_to_scene)
        row.add_button(self.scene_button)

        self.metadata_button = QPushButton("Metadata")
        self.metadata_button.clicked.connect(self.show_metadata)
        row.add_button(self.metadata_button)

        self.body().addWidget(row)
        self.status_label = HintLabel("")
        self.body().addWidget(self.status_label)

    # -- access ------------------------------------------------------------

    @property
    def controller(self) -> Any:
        return self.context.projects

    @property
    def project(self) -> Any:
        return self.controller.project if self.controller is not None else None

    @property
    def project_dir(self) -> Optional[Path]:
        layout = self.controller.layout if self.controller is not None else None
        return Path(layout.root) if layout is not None else None

    def _studio(self) -> Optional[ImageService]:
        """One service per page, pointed at the application's folders."""
        if self.service is None:
            try:
                self.service = ImageService.for_paths(self.context.paths,
                                                      self.context.settings)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                self.status_label.setText(
                    f"The image library could not be opened: {exc}")
                return None
        return self.service

    def _submit(self, spec: Any) -> Optional[str]:
        """Queue a job and return its **id** (or None when it was refused).

        ``JobManager.submit`` returns a :class:`Job`, not an id; the page tracks
        ids so the completion handler can tell which job it is looking at.
        """
        spec.paths = self.context.paths
        if self.context.jobs is None:
            return None
        job = self.context.jobs.submit(spec)
        return getattr(job, "id", None) if job is not None else None

    # -- state -------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the history and the backend lists.  Cheap; no model loads."""
        studio = self._studio()
        if studio is None:
            return
        self._fill_backends()
        self._fill_history()
        self._update_generate_enabled()

    def _fill_backends(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        report = studio.registry.report
        if report is None:
            return
        self.backend_box.blockSignals(True)
        # Reopen on the backend and model the user last chose (section 63).
        remembered = self._remembered("active_backend")
        current = self.backend_box.currentData() or remembered
        for entry in report.backends:
            state = entry.status()
            label = entry.label if state.available else f"{entry.label} (not available)"
            self.backend_box.addItem(label, entry.id)
            index = self.backend_box.count() - 1
            self.backend_box.setItemData(index, state.reason, Qt.ToolTipRole)
            if not state.available:
                # Shown, but not selectable: the user can see it exists and why
                # it cannot be used.
                self.backend_box.model().item(index).setEnabled(False)
        if current:
            position = self.backend_box.findData(current)
            if position >= 0 and self.backend_box.model().item(position).isEnabled():
                self.backend_box.setCurrentIndex(position)
        if self.backend_box.currentIndex() < 0 and self.backend_box.count():
            # Fall back to the first backend that actually works.
            for index in range(self.backend_box.count()):
                if self.backend_box.model().item(index).isEnabled():
                    self.backend_box.setCurrentIndex(index)
                    break
        self.backend_box.blockSignals(False)
        self._on_backend_changed()
        self._restore_form_settings()

    def _fill_history(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for entry in studio.history.recent(limit=50):
            text = f"[{entry.status}] {entry.prompt_preview or Path(entry.path).name}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, entry.id)
            item.setToolTip(entry.label())
            self.history_list.addItem(item)
        self.history_list.blockSignals(False)

    def _on_backend_changed(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        backend_id = self.backend_box.currentData() or ""
        entry = studio.registry.get(backend_id)
        self.model_box.clear()
        if entry is None:
            return
        for model in entry.models():
            self.model_box.addItem(f"{model.name} ({model.describe()})", model.id)
        capabilities = entry.capabilities()
        self._apply_capabilities(capabilities)
        self._remember(active_backend=backend_id)

    def _apply_capabilities(self, capabilities: Any) -> None:
        """Enable only what the selected backend can actually do (section 37)."""
        mode = self.mode_box.currentData()
        from app.image.provider import MODE_FEATURE

        feature = MODE_FEATURE.get(mode, "")
        supported = capabilities.supports(feature) if feature else True
        if mode == GenerationMode.UPSCALE and capabilities.upscale:
            supported = True
        self.generate_button.setEnabled(supported)
        if not supported:
            label = MODE_LABELS.get(mode, mode)
            self.generate_hint.setText(
                f"{label} is not supported by this backend. Choose another mode "
                "or another backend - nothing was switched for you.")
        else:
            self.generate_hint.setText("")

        self.negative_edit.setEnabled(bool(capabilities.negative_prompt))
        self.steps_spin.setEnabled(bool(capabilities.steps))
        self.guidance_spin.setEnabled(bool(capabilities.guidance))
        self.seed_spin.setEnabled(bool(capabilities.seed_control))
        self.batch_spin.setEnabled(bool(capabilities.batch))
        if capabilities.max_batch > 1:
            self.batch_spin.setRange(1, int(capabilities.max_batch))
        else:
            self.batch_spin.setRange(1, 1)

    def _remembered(self, name: str, default: Any = "") -> Any:
        section = getattr(self.context.settings, "image", None)
        return getattr(section, name, default) if section is not None else default

    def _remember(self, **values: Any) -> None:
        """Store the last-used choices so the studio reopens where it was.

        Settings persistence failures are reported, never raised: losing a
        remembered preference must not stop a generation.
        """
        section = getattr(self.context.settings, "image", None)
        if section is None:
            return
        changed = False
        for key, value in values.items():
            if hasattr(section, key) and getattr(section, key) != value:
                setattr(section, key, value)
                changed = True
        if not changed:
            return
        apply_settings = getattr(self.context, "apply_settings", None)
        if callable(apply_settings):
            try:
                apply_settings(self.context.settings, save=True,
                               reason="Image Studio choices")
            except Exception as exc:  # noqa: BLE001 - a preference is not fatal
                log_event("IMAGE_PREFS_SAVE_FAILED",
                          "The last-used image settings could not be saved",
                          error=str(exc))

    def _restore_form_settings(self) -> None:
        """Put the form back where the user left it."""
        mode = str(self._remembered("last_mode") or "")
        if mode:
            index = self.mode_box.findData(mode)
            if index >= 0:
                self.mode_box.setCurrentIndex(index)
        width = int(self._remembered("last_width") or 0)
        height = int(self._remembered("last_height") or 0)
        if width and height:
            for index in range(self.size_box.count()):
                if tuple(self.size_box.itemData(index) or ()) == (width, height):
                    self.size_box.setCurrentIndex(index)
                    break
            else:
                self.size_box.setCurrentIndex(self.size_box.count() - 1)
                self.width_spin.setValue(width)
                self.height_spin.setValue(height)

    def _on_mode_changed(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        entry = studio.registry.get(self.backend_box.currentData() or "")
        if entry is not None:
            self._apply_capabilities(entry.capabilities())

    def _on_size_changed(self) -> None:
        self._remember(last_mode=self.mode_box.currentData() or "",
                       last_width=int(self.width_spin.value()),
                       last_height=int(self.height_spin.value()))
        size = self.size_box.currentData()
        if not size:
            return
        width, height = size
        custom = width == 0
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        if not custom:
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)

    def _toggle_advanced(self, shown: bool) -> None:
        self.advanced_panel.setVisible(bool(shown))
        self.advanced_toggle.setText("Hide advanced settings" if shown
                                     else "Advanced settings")

    def _update_generate_enabled(self) -> None:
        studio = self._studio()
        if studio is None or self.backend_box.currentData() is None:
            self.generate_button.setEnabled(False)
            self.generate_hint.setText(
                "No image backend is available yet. Check the backends, then "
                "choose one.")
            return
        mode = self.mode_box.currentData()
        needs_prompt = mode in (GenerationMode.TEXT_TO_IMAGE,
                                GenerationMode.IMAGE_TO_IMAGE,
                                GenerationMode.INPAINT, GenerationMode.OUTPAINT,
                                GenerationMode.VARIATION)
        has_prompt = bool(self.prompt_edit.toPlainText().strip())
        needs_source = mode in (GenerationMode.IMAGE_TO_IMAGE,
                                GenerationMode.INPAINT, GenerationMode.OUTPAINT,
                                GenerationMode.VARIATION, GenerationMode.UPSCALE)
        has_source = bool(self.source_edit.text().strip() or self._current_image)
        entry = studio.registry.get(self.backend_box.currentData() or "")
        allowed = entry.capabilities().supports(
            self._feature_for(mode)) if entry is not None else False
        ready = allowed and (has_prompt or not needs_prompt) \
            and (has_source or not needs_source)
        self.generate_button.setEnabled(ready)
        if ready:
            self.generate_hint.setText("")
        elif not allowed:
            self.generate_hint.setText(
                f"{MODE_LABELS.get(mode, mode)} is not supported by this backend.")
        elif needs_prompt and not has_prompt:
            self.generate_hint.setText("Type a prompt to generate an image.")
        elif needs_source and not has_source:
            self.generate_hint.setText("Choose a source image for this mode.")

    @staticmethod
    def _feature_for(mode: str) -> str:
        from app.image.provider import MODE_FEATURE

        return MODE_FEATURE.get(mode, "")

    # -- actions -----------------------------------------------------------

    def detect_backends(self) -> None:
        """Detect backends and models off the Qt thread."""
        studio = self._studio()
        if studio is None:
            return
        if self._detect_job is not None:
            self.status_label.setText("Backends are already being checked.")
            return
        self.state_label.setText("Looking for local image backends...")
        spec = detect_spec({"service": studio})
        self._detect_job = self._submit(spec)
        if self._detect_job is None:
            self._on_detected(None)
            return
        if self._detect_job is None:
            self._on_detected(None)

    def _on_detected(self, result: Any) -> None:
        self._detect_job = None
        studio = self._studio()
        if studio is None:
            return
        if result is None:
            studio.status()
        self._fill_backends()
        status = studio.status(refresh=False)
        self.state_label.setText(status.generator_note)
        self.device_label.setText(status.device.describe())
        self._update_generate_enabled()

    def import_image(self) -> None:
        """Import an image into the library, validating it first."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import an image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tiff *.gif)")
        if not path:
            return
        studio = self._studio()
        if studio is None:
            return
        source = Path(path)
        destination = (studio.library_root / source.name) if studio.library_root \
            else source
        report = studio.import_image(source, destination=destination)
        if not report.ok:
            self.status_label.setText(
                f"{report.error} {report.what_to_do}".strip())
            return
        self.status_label.setText(f"Imported: {report.describe()}")
        self._select_image(Path(report.path) if report.path else source)
        self._fill_history()

    def generate(self) -> None:
        """Start one generation (or one explicit batch) as a job."""
        studio = self._studio()
        if studio is None:
            return
        if self._generate_job is not None:
            self.status_label.setText("A generation is already running.")
            return
        request = self._request_from_form()
        if request is None:
            return
        problems = [issue for issue in
                    studio.validate(request, backend_id=request.backend)
                    if issue.severity == "error"]
        if problems:
            # Refused *before* a model is loaded, with the reason and the fix.
            text = problems[0].message
            if problems[0].what_to_do:
                text += f" {problems[0].what_to_do}"
            self.status_label.setText(text)
            return

        batch = int(request.batch or 1)
        output_dir = self._output_dir()
        request.output_dir = str(output_dir)
        spec = generate_spec({"service": studio, "request": request})
        self._generate_job = self._submit(spec)
        self.stop_button.setEnabled(self._generate_job is not None)
        self.status_label.setText(
            f"Generating {batch} image(s) with {request.backend}..."
            if batch > 1 else f"Generating with {request.backend}...")
        if self._generate_job is None:
            self._on_generated(None)

    def _request_from_form(self) -> Optional[GenerationRequest]:
        studio = self._studio()
        if studio is None:
            return None
        mode = self.mode_box.currentData() or GenerationMode.TEXT_TO_IMAGE
        source = self.source_edit.text().strip() or \
            (str(self._current_image) if self._current_image else "")
        if mode == GenerationMode.UPSCALE and self._current_image is not None:
            source = source or str(self._current_image)
        return GenerationRequest(
            mode=mode,
            backend=self.backend_box.currentData() or "",
            model=self.model_box.currentData() or "",
            prompt=self.prompt_edit.toPlainText().strip(),
            negative_prompt=self.negative_edit.toPlainText().strip(),
            width=int(self.width_spin.value()),
            height=int(self.height_spin.value()),
            seed=int(self.seed_spin.value()),
            steps=int(self.steps_spin.value()),
            guidance=float(self.guidance_spin.value()),
            batch=int(self.batch_spin.value()),
            source_image=source,
            scale=2.0 if mode == GenerationMode.UPSCALE else 0.0,
            output_format="png",
            output_dir=str(self._output_dir()),
            name_stem="image",
            project=self.project.project.name if self.project else "",
            parent_asset=source,
        )

    def _output_dir(self) -> Path:
        studio = self._studio()
        if studio is not None and studio.library_root is not None:
            studio.library_root.mkdir(parents=True, exist_ok=True)
            return studio.library_root
        return Path(self.context.paths.images_dir)

    def _on_generated(self, result: Any) -> None:
        self._generate_job = None
        self.stop_button.setEnabled(False)
        if not isinstance(result, dict):
            return
        if not result.get("ok"):
            text = result.get("message") or "The generation did not finish."
            if result.get("why"):
                text += f" why: {result['why']}"
            if result.get("what_to_do"):
                text += f" {result['what_to_do']}"
            self.status_label.setText(text)
            self._fill_history()
            return
        paths = [Path(item) for item in result.get("paths") or []]
        self.status_label.setText(
            f"Created {len(paths)} image(s) with seeds "
            f"{', '.join(str(seed) for seed in result.get('seeds') or [])}.")
        if paths:
            self._select_image(paths[0])
        self._fill_history()

    def stop(self) -> None:
        """Cancel the running generation.  The job system kills the child."""
        if self._generate_job is None:
            return
        cancelled = False
        manager = getattr(self.context.jobs, "cancel", None)
        if callable(manager):
            cancelled = bool(manager(self._generate_job))
        self.status_label.setText(
            "Cancelling - the backend process is being stopped."
            if cancelled else "There was nothing to cancel.")
        self.stop_button.setEnabled(False)

    def regenerate(self) -> None:
        """Replay the selected history entry, keeping its seed."""
        studio = self._studio()
        if studio is None:
            return
        entry = self._selected_history_entry()
        if entry is None:
            self.status_label.setText("Choose a history entry first.")
            return
        if not entry.request:
            self.status_label.setText(
                "That history entry has no saved settings, so it cannot be "
                "replayed.")
            return
        request = GenerationRequest.from_dict(entry.request)
        request.output_dir = str(self._output_dir())
        request.name_stem = "image"
        spec = generate_spec({"service": studio, "request": request})
        self._generate_job = self._submit(spec)
        self.stop_button.setEnabled(self._generate_job is not None)
        self.status_label.setText(
            f"Regenerating with seed {entry.seed if entry.seed is not None else 'random'}...")
        if self._generate_job is None:
            self._on_generated(None)

    def make_variation(self) -> None:
        """Variation of the selected image.  The original is left alone."""
        image = self._current_image
        if image is None:
            self.status_label.setText("Select an image first.")
            return
        studio = self._studio()
        if studio is None:
            return
        graph = studio.graph_for([image])
        name = graph.next_name(image, Origin.VARIATION)
        request = studio.variant_of(image)
        request.backend = self.backend_box.currentData() or "standard"
        request.model = self.model_box.currentData() or "standard"
        request.output_dir = str(self._output_dir())
        request.name_stem = Path(name).stem
        problems = [issue for issue in
                    studio.validate(request, backend_id=request.backend)
                    if issue.severity == "error"]
        if problems:
            text = problems[0].message
            if problems[0].what_to_do:
                text += f" {problems[0].what_to_do}"
            self.status_label.setText(text)
            return
        spec = generate_spec({"service": studio, "request": request})
        self._generate_job = self._submit(spec)
        self.stop_button.setEnabled(self._generate_job is not None)
        self.status_label.setText(f"Creating {name}...")
        if self._generate_job is None:
            self._on_generated(None)

    def upscale_image(self) -> None:
        """Upscale the selected image as a job, stating which method runs."""
        image = self._current_image
        if image is None:
            self.status_label.setText("Select an image first.")
            return
        studio = self._studio()
        if studio is None:
            return
        if self._upscale_job is not None:
            self.status_label.setText("An upscale is already running.")
            return
        spec = upscale_spec({"service": studio, "source": str(image),
                             "scale": 2.0, "method": "standard",
                             "output_dir": str(self._output_dir())})
        self._upscale_job = self._submit(spec)
        self.status_label.setText("Upscaling (Standard Resize)...")
        if self._upscale_job is None:
            self._on_upscaled(None)

    def _on_upscaled(self, result: Any) -> None:
        self._upscale_job = None
        if not isinstance(result, dict):
            return
        if not result.get("ok"):
            text = result.get("message") or "The image could not be upscaled."
            if result.get("what_to_do"):
                text += f" {result['what_to_do']}"
            self.status_label.setText(text)
            return
        self.status_label.setText(f"{result.get('label')}: {result.get('message')}")
        if result.get("output_path"):
            self._select_image(Path(result["output_path"]))
        self._fill_history()

    def apply_edit(self) -> None:
        """Apply the chosen edit to the in-memory session (no file yet)."""
        if self._current_image is None:
            self.status_label.setText("Select an image first.")
            return
        item = self.edit_list.currentItem()
        if item is None:
            self.status_label.setText("Choose an edit from the list first.")
            return
        studio = self._studio()
        if studio is None:
            return
        try:
            if self._edit_session is None:
                self._edit_session = studio.open_editor(self._current_image)
            self._add_operation(self._edit_session, item.data(Qt.UserRole),
                                self._current_image)
            preview = self._edit_session.preview()
            self._show_pixmap(_pil_to_pixmap(preview))
            self.edit_hint.setText(
                f"{len(self._edit_session.operations)} edit(s) applied. "
                "Nothing has been written yet - use Save as new image.")
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.edit_hint.setText(f"That edit could not be applied: {exc}")

    def _add_operation(self, session: Any, operation: str, image: Path) -> None:
        """Translate a menu choice into the operation's real parameters."""
        from PIL import Image

        with Image.open(image) as handle:
            width, height = handle.size
        if operation == "crop":
            session.add("crop", left=int(width * 0.1), top=int(height * 0.1),
                        right=int(width * 0.9), bottom=int(height * 0.9))
        elif operation == "resize":
            session.add("resize", width=512,
                        height=max(1, int(round(height * 512 / max(1, width)))))
        elif operation == "rotate":
            session.add("rotate", angle=90.0)
        elif operation == "brightness":
            session.add("brightness", factor=1.15)
        elif operation == "contrast":
            session.add("contrast", factor=1.15)
        elif operation == "saturation":
            session.add("saturation", factor=1.2)
        elif operation == "sharpen":
            session.add("sharpen", factor=1.6)
        elif operation == "blur":
            session.add("blur", radius=2.0)
        elif operation == "rounded_corners":
            session.add("rounded_corners", radius=32)
        else:
            session.add(operation)

    def reset_edits(self) -> None:
        if self._edit_session is not None:
            self._edit_session.reset()
            self._edit_session = None
        self.edit_hint.setText("Edits cleared. Nothing was written.")
        if self._current_image is not None:
            self._show_image(self._current_image)

    def save_edited(self) -> None:
        """Write the edits as a new image.  The original is never touched."""
        if self._edit_session is None:
            self.status_label.setText("Apply an edit first.")
            return
        studio = self._studio()
        if studio is None:
            return
        source = Path(self._current_image)
        stem = f"{source.stem}_{Origin.EDIT}"
        target = studio.unique_output(self._output_dir(), stem,
                                      source.suffix or ".png")
        report = studio.save_edit(self._edit_session, target=target,
                                  requested_format=source.suffix.lstrip(".")
                                  or "png")
        if not report.ok:
            self.status_label.setText(
                f"{report.error} {report.what_to_do}".strip())
            return
        self.status_label.setText(f"Saved {report.describe()}")
        if report.format_changed:
            self.status_label.setText(
                f"{self.status_label.text()} ({report.format_changed})")
        self._edit_session = None
        self._select_image(Path(report.path))
        self._fill_history()

    def use_in_project(self) -> None:
        """Add the selected image to the open project's asset library."""
        image = self._current_image
        if image is None:
            self.status_label.setText("Select an image first.")
            return
        if not _project_open(self.controller):
            self.status_label.setText(
                "No project is open. Create or open a project first, or keep "
                "the image in the asset library.")
            return
        from app.image.integration import use_in_project

        result = use_in_project(self.controller, image)
        self.status_label.setText(
            result.message if result.ok else f"{result.message} {result.what_to_do}")
        if result.ok:
            self.project_changed.emit()

    def send_to_scene(self) -> None:
        """One click: import the image and place it in a scene."""
        image = self._current_image
        if image is None:
            self.status_label.setText("Select an image first.")
            return
        if not _project_open(self.controller):
            self.status_label.setText(
                "No project is open. Create a project, or save the image to the "
                "asset library and add it later.")
            return
        result = send_to_scene(self.controller, image,
                               placement=Placement.OVERLAY)
        if result.ok:
            self.status_label.setText(result.describe())
            self.project_changed.emit()
        else:
            text = result.message
            if result.what_to_do:
                text += f" {result.what_to_do}"
            self.status_label.setText(text)

    def show_metadata(self) -> None:
        """Show exactly what is recorded for the selected image."""
        studio = self._studio()
        if studio is None or self._current_image is None:
            self.status_label.setText("Select an image first.")
            return
        metadata = studio.metadata_for(self._current_image)
        if metadata is None:
            self.details_label.setText(
                "No metadata is recorded for this image. It may have been "
                "imported rather than generated.")
            return
        self.details_label.setText(metadata.describe())

    # -- selection ---------------------------------------------------------

    def _pick_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a source image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tiff *.gif)")
        if path:
            self.source_edit.setText(path)
            self._select_image(Path(path))

    def _select_image(self, path: Path) -> None:
        self._current_image = Path(path)
        self.source_edit.setText(str(path))
        self._edit_session = None
        self._show_image(path)
        self._update_generate_enabled()

    def _show_image(self, path: Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as handle:
                handle.load()
                preview = handle.copy()
            self._show_pixmap(_pil_to_pixmap(preview))
            self.preview_info.setText(
                f"{path.name} - {preview.width}x{preview.height} "
                f"{preview.format or path.suffix.lstrip('.').upper()}")
        except Exception as exc:  # noqa: BLE001 - a bad file is reported
            self.preview_label.setText(f"This image could not be opened: {exc}")
            self.preview_info.setText(str(path))

    def _show_pixmap(self, pixmap: QPixmap) -> None:
        area = self.preview_label.size()
        self.preview_label.setPixmap(pixmap.scaled(
            max(64, area.width() - 8), max(64, area.height() - 8),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _on_history_selected(self, current: Any, _previous: Any) -> None:
        if current is None:
            return
        entry = self._selected_history_entry()
        if entry is None:
            return
        self.details_label.setText(entry.label())
        if entry.path and Path(entry.path).is_file():
            self._select_image(Path(entry.path))

    def _selected_history_entry(self) -> Any:
        item = self.history_list.currentItem()
        if item is None:
            return None
        studio = self._studio()
        if studio is None:
            return None
        return studio.history.find(item.data(Qt.UserRole))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _wrap(layout: Any) -> QWidget:
    """Put a layout inside a widget, for QFormLayout.addRow."""
    widget = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    widget.setLayout(layout)
    return widget


def _project_open(controller: Any) -> bool:
    """Whether a project is open, for either the service or the controller."""
    if controller is None:
        return False
    flag = getattr(controller, "is_open", None)
    if callable(flag):
        return bool(flag())
    if isinstance(flag, bool):
        return flag
    return bool(getattr(controller, "project", None))


def _pil_to_pixmap(image: Any) -> QPixmap:
    """PIL image -> QPixmap, through PNG bytes so no Qt image plugin is needed."""
    import io

    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


def job_value(result: Any) -> Any:
    """The job's value, or a failure dict the handlers already understand."""
    if getattr(result, "failed", False) or getattr(result, "cancelled", False):
        error = getattr(result, "error", None)
        title = getattr(error, "title", "") if error is not None else ""
        return {"ok": False,
                "cancelled": bool(getattr(result, "cancelled", False)),
                "message": title or ("The job was cancelled."
                                     if getattr(result, "cancelled", False)
                                     else "The job did not finish.")}
    return getattr(result, "value", None)
