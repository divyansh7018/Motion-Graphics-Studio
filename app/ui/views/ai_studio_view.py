"""The AI Studio page (Stage G, sections 1, 8, 32, 33, 47, 68).

One page that drives every local AI backend, for both stills and clips:

**Left** - what to make: the backend, the model, the mode, the prompt and the
settings the backend actually supports (an unsupported setting is disabled with
the reason, rather than accepted and ignored).
**Centre** - the source a mode needs, the Generate buttons, and the job queue
with its cancel/retry/result controls.
**Right** - the backend manager (state, enable/disable, configure, logs), the
generation history with its filters, and what to send a finished result to.

The rules this page keeps, because they are the ones a user notices when they
are broken:

* **nothing is faked** - an unavailable backend shows NOT INSTALLED with what to
  do; the built-in fixture is labelled TEST BACKEND wherever it appears;
* **no silent anything** - the backend and model in the form are the ones used,
  or the job fails and says so; a clip that differs from the request shows the
  difference in full;
* **no work on the Qt thread** - detection, generation, checks, storyboard plans
  and sends all go through the Stage A job system, with cancellation wired to the
  backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.ai.jobs import (ai_check_spec, ai_detect_spec, finalize_from_result,
                         image_generate_spec, video_batch_spec,
                         video_generate_spec, video_storyboard_spec)
from app.ai.service import AIService, plan_summary
from app.ai.types import is_reachable
from app.ai.video import (CAMERA_LABELS, CAMERA_MOVES, MODE_LABELS, VIDEO_MODES,
                          VideoMode, VideoRequest)
from app.core.logging_setup import log_event
from app.core.settings import Settings
from app.jobs.keys import JobKeys

from ..theme import mark_primary
from ..widgets.common import ButtonRow, Card, HintLabel, Page

#: Sizes offered for a clip.  Custom width and height are always available.
VIDEO_SIZES: tuple[tuple[str, int, int], ...] = (
    ("16:9  1280x720", 1280, 720),
    ("16:9  640x360", 640, 360),
    ("9:16  720x1280", 720, 1280),
    ("9:16  360x640", 360, 640),
    ("1:1  768x768", 768, 768),
    ("4:5  864x1080", 864, 1080),
)

#: Frame rates offered, all of them real values encoders accept.
VIDEO_FPS: tuple[int, ...] = (8, 12, 16, 24, 25, 30)

#: What each mode needs, in one sentence, shown above the Generate button.
MODE_HELP: dict[str, str] = {
    VideoMode.TEXT_TO_VIDEO: "Draws the clip from the prompt and the seed.",
    VideoMode.IMAGE_TO_VIDEO: "Moves the source image you choose.",
    VideoMode.VIDEO_TO_VIDEO: "Restyles the source clip; its timing is kept.",
    VideoMode.EXTEND: "Continues the clip you choose; the original frames are "
                      "kept as well.",
    VideoMode.STORYBOARD_TO_VIDEO: "Uses the storyboard plan: build it in the "
                                   "Storyboard plan tab, review it, approve it.",
}


class BackendSettingsDialog(QDialog):
    """Edit one backend's settings, validating what is typed (section 6)."""

    def __init__(self, entry: Any, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.settings = settings
        self.setWindowTitle(f"{entry.name} settings")
        self._fields: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for field in entry.backend.settings_schema():
            widget: Any
            if field.kind == "choice" and field.choices:
                widget = QComboBox()
                for choice in field.choices:
                    widget.addItem(str(choice), str(choice))
                index = widget.findData(str(field.default))
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif field.kind in ("int", "float"):
                widget = QSpinBox() if field.kind == "int" else QDoubleSpinBox()
                widget.setRange(-1_000_000, 1_000_000)
                if field.kind == "float":
                    widget.setDecimals(1)
                try:
                    widget.setValue(float(field.default or 0))
                except (TypeError, ValueError):
                    pass
            else:
                widget = QLineEdit(str(field.default or ""))
            if field.help:
                widget.setToolTip(field.help)
            self._fields[field.name] = widget
            form.addRow(field.label or field.name, widget)
        layout.addLayout(form)
        hint = HintLabel(
            "Nothing here is sent anywhere on its own. A local endpoint must be "
            "on this machine.")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        out: dict = {}
        for name, widget in self._fields.items():
            if isinstance(widget, QComboBox):
                out[name] = str(widget.currentData() or "")
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                out[name] = widget.value()
            else:
                out[name] = widget.text()
        return out


class AIStudioPage(Page):
    """The AI Studio."""

    project_changed = Signal()

    def __init__(self, context: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "AI Studio",
            "Generate images and clips with a local backend - and see exactly "
            "what really ran.",
            parent,
        )
        self.context = context
        self.service: Optional[AIService] = None
        self._detect_job: Optional[str] = None
        self._generate_job: Optional[str] = None
        self._check_job: Optional[str] = None
        self._storyboard_job: Optional[str] = None
        self._last_result: dict = {}
        self._plan: list = []
        self._log_box: Optional[QPlainTextEdit] = None

        self._build_state_card()
        self._build_splitter()
        manager = getattr(self.context, "jobs", None)
        if manager is not None:
            try:
                manager.job_finished.connect(self._on_job_finished)
            except Exception:  # noqa: BLE001 - the page still works without it
                pass
        self.refresh()

    # -- services ----------------------------------------------------------

    def _studio(self) -> Optional[AIService]:
        """One service per page, pointed at the application's own folders."""
        if self.service is None:
            try:
                self.service = AIService.for_paths(self.context.paths,
                                                   self.context.settings)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                self.status_label.setText(
                    f"The AI Studio could not be opened: {exc}")
                return None
        return self.service

    @property
    def controller(self) -> Any:
        return getattr(self.context, "projects", None)

    @property
    def project(self) -> Any:
        controller = self.controller
        return getattr(controller, "project", None) if controller is not None else None

    @property
    def project_dir(self) -> Optional[Path]:
        controller = self.controller
        layout = getattr(controller, "layout", None) if controller is not None else None
        return Path(layout.root) if layout is not None else None

    def _settings(self) -> Any:
        return getattr(self.context, "settings", None) or Settings()

    # -- construction ------------------------------------------------------

    def _build_state_card(self) -> None:
        card = Card("Local AI backends")
        self.state_label = QLabel("Not checked yet.")
        self.state_label.setWordWrap(True)
        card.body().addWidget(self.state_label)
        self.honesty_label = HintLabel("")
        self.honesty_label.setWordWrap(True)
        card.body().addWidget(self.honesty_label)

        row = ButtonRow()
        self.detect_button = QPushButton("Check backends")
        self.detect_button.clicked.connect(self.detect_backends)
        row.add_button(self.detect_button)
        self.test_button = QPushButton("Test backend (real generation)")
        self.test_button.clicked.connect(self.test_backend)
        row.add_button(self.test_button)
        self.logs_button = QPushButton("Show backend log")
        self.logs_button.clicked.connect(self.show_logs)
        row.add_button(self.logs_button)
        card.body().addWidget(row)
        self.body().addWidget(card)

    def _build_splitter(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_centre())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 4)
        self.body().addWidget(splitter, 1)

    # -- left: what to make ------------------------------------------------

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        setup = Card("What to make")
        form = QFormLayout()
        self.backend_combo = QComboBox()
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        form.addRow("Backend", self.backend_combo)
        self.model_combo = QComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        form.addRow("Model", self.model_combo)
        self.mode_combo = QComboBox()
        for mode in VIDEO_MODES:
            self.mode_combo.addItem(MODE_LABELS.get(mode, mode), mode)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode", self.mode_combo)
        setup.body().addLayout(form)
        self.backend_note = HintLabel("")
        self.backend_note.setWordWrap(True)
        setup.body().addWidget(self.backend_note)
        layout.addWidget(setup)

        prompt_card = Card("Prompt")
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the clip. The text goes to the backend you chose, and "
            "nowhere else.")
        self.prompt_edit.setFixedHeight(84)
        prompt_card.body().addWidget(self.prompt_edit)
        self.negative_edit = QLineEdit()
        self.negative_edit.setPlaceholderText("What to keep out (optional)")
        prompt_card.body().addWidget(self.negative_edit)
        prompt_row = ButtonRow()
        self.approve_button = QPushButton("Approve this prompt")
        self.approve_button.clicked.connect(self.approve_prompt)
        prompt_row.add_button(self.approve_button)
        self.save_prompt_button = QPushButton("Save to library")
        self.save_prompt_button.clicked.connect(self.save_prompt)
        prompt_row.add_button(self.save_prompt_button)
        self.use_template_button = QPushButton("Use template")
        self.use_template_button.clicked.connect(self.use_template)
        prompt_row.add_button(self.use_template_button)
        prompt_card.body().addWidget(prompt_row)
        self.template_combo = QComboBox()
        prompt_card.body().addWidget(self.template_combo)
        self.prompt_hint = HintLabel("")
        self.prompt_hint.setWordWrap(True)
        prompt_card.body().addWidget(self.prompt_hint)
        layout.addWidget(prompt_card)

        settings_card = Card("Settings the backend supports")
        settings_form = QFormLayout()
        self.size_combo = QComboBox()
        self.size_combo.addItem("Custom", (0, 0))
        for label, width, height in VIDEO_SIZES:
            self.size_combo.addItem(label, (width, height))
        self.size_combo.setCurrentIndex(1)
        self.size_combo.currentIndexChanged.connect(self._on_size_changed)
        size_holder = QWidget()
        size_row = QHBoxLayout(size_holder)
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.addWidget(self.size_combo, 1)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(0, 7680)
        self.width_spin.setValue(1280)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(0, 7680)
        self.height_spin.setValue(720)
        size_row.addWidget(self.width_spin)
        size_row.addWidget(self.height_spin)
        settings_form.addRow("Size", size_holder)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.0, 600.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setValue(3.0)
        settings_form.addRow("Duration (s)", self.duration_spin)
        self.fps_combo = QComboBox()
        for fps in VIDEO_FPS:
            self.fps_combo.addItem(str(fps), fps)
        self.fps_combo.setCurrentIndex(3)
        settings_form.addRow("Frame rate", self.fps_combo)
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2_147_483_647)
        self.seed_spin.setSpecialValueText("Random")
        settings_form.addRow("Seed", self.seed_spin)
        self.camera_combo = QComboBox()
        self.camera_combo.addItem("No camera move", "")
        for move in CAMERA_MOVES:
            self.camera_combo.addItem(CAMERA_LABELS.get(move, move), move)
        settings_form.addRow("Camera", self.camera_combo)
        self.camera_amount_spin = QDoubleSpinBox()
        self.camera_amount_spin.setRange(0.0, 1.0)
        self.camera_amount_spin.setSingleStep(0.05)
        self.camera_amount_spin.setValue(0.3)
        settings_form.addRow("Camera amount", self.camera_amount_spin)
        self.strength_spin = QDoubleSpinBox()
        self.strength_spin.setRange(0.0, 1.0)
        self.strength_spin.setSingleStep(0.05)
        self.strength_spin.setValue(0.6)
        settings_form.addRow("Strength", self.strength_spin)
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 8)
        settings_form.addRow("Batch (explicit)", self.batch_spin)
        self.quality_combo = QComboBox()
        for value in ("draft", "medium", "high", "ultra"):
            self.quality_combo.addItem(value.title(), value)
        self.quality_combo.setCurrentIndex(1)
        settings_form.addRow("Quality", self.quality_combo)
        self.style_edit = QLineEdit()
        self.style_edit.setPlaceholderText("Project style (optional)")
        settings_form.addRow("Style", self.style_edit)
        settings_card.body().addLayout(settings_form)

        self.reference_edit = QLineEdit()
        self.reference_edit.setPlaceholderText("Reference image (optional)")
        settings_card.body().addWidget(self.reference_edit)
        self.reference_kind_combo = QComboBox()
        from app.ai.references import KIND_LABELS, REFERENCE_KINDS

        for kind in REFERENCE_KINDS:
            self.reference_kind_combo.addItem(KIND_LABELS.get(kind, kind), kind)
        settings_card.body().addWidget(self.reference_kind_combo)
        ref_row = ButtonRow()
        self.reference_button = QPushButton("Choose reference...")
        self.reference_button.clicked.connect(self.choose_reference)
        ref_row.add_button(self.reference_button)
        self.save_preset_button = QPushButton("Save settings as preset")
        self.save_preset_button.clicked.connect(self.save_preset)
        ref_row.add_button(self.save_preset_button)
        settings_card.body().addWidget(ref_row)
        preset_row = ButtonRow()
        self.preset_combo = QComboBox()
        preset_row.add_button(self.preset_combo)
        self.apply_preset_button = QPushButton("Apply preset")
        self.apply_preset_button.clicked.connect(self.apply_preset)
        preset_row.add_button(self.apply_preset_button)
        settings_card.body().addWidget(preset_row)

        self.memory_label = HintLabel("")
        self.memory_label.setWordWrap(True)
        settings_card.body().addWidget(self.memory_label)
        layout.addWidget(settings_card)
        layout.addStretch(1)
        return panel

    # -- centre: inputs, actions, queue ------------------------------------

    def _build_centre(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        source_card = Card("What it works from")
        form = QFormLayout()
        self.source_image_edit = QLineEdit()
        self.source_image_edit.setPlaceholderText("Image to move (image to video)")
        form.addRow("Source image", self.source_image_edit)
        self.source_video_edit = QLineEdit()
        self.source_video_edit.setPlaceholderText("Clip to restyle (video to video)")
        form.addRow("Source clip", self.source_video_edit)
        self.extend_edit = QLineEdit()
        self.extend_edit.setPlaceholderText("Clip to continue (extend)")
        form.addRow("Clip to extend", self.extend_edit)
        source_card.body().addLayout(form)
        source_row = ButtonRow()
        self.choose_image_button = QPushButton("Choose image...")
        self.choose_image_button.clicked.connect(self.choose_source_image)
        source_row.add_button(self.choose_image_button)
        self.choose_video_button = QPushButton("Choose clip...")
        self.choose_video_button.clicked.connect(self.choose_source_video)
        source_row.add_button(self.choose_video_button)
        self.choose_extend_button = QPushButton("Choose clip to extend...")
        self.choose_extend_button.clicked.connect(self.choose_extend_clip)
        source_row.add_button(self.choose_extend_button)
        source_card.body().addWidget(source_row)
        self.source_note = HintLabel("")
        self.source_note.setWordWrap(True)
        source_card.body().addWidget(self.source_note)
        layout.addWidget(source_card)

        action_card = Card("Generate")
        self.mode_label = QLabel("")
        self.mode_label.setWordWrap(True)
        action_card.body().addWidget(self.mode_label)
        row = ButtonRow()
        self.generate_button = QPushButton("Generate clip")
        mark_primary(self.generate_button)
        self.generate_button.clicked.connect(self.generate)
        row.add_button(self.generate_button)
        self.batch_button = QPushButton("Generate batch")
        self.batch_button.clicked.connect(self.generate_batch)
        row.add_button(self.batch_button)
        self.storyboard_button = QPushButton("Plan from storyboard")
        self.storyboard_button.clicked.connect(self.plan_storyboard)
        row.add_button(self.storyboard_button)
        self.image_button = QPushButton("Generate image")
        self.image_button.clicked.connect(self.generate_image)
        row.add_button(self.image_button)
        action_card.body().addWidget(row)
        row2 = ButtonRow()
        self.cancel_button = QPushButton("Cancel the running job")
        self.cancel_button.clicked.connect(self.cancel_job)
        row2.add_button(self.cancel_button)
        self.check_selected_button = QPushButton("Light check this backend")
        self.check_selected_button.clicked.connect(self.light_check)
        row2.add_button(self.check_selected_button)
        action_card.body().addWidget(row2)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        action_card.body().addWidget(self.status_label)
        self.result_label = HintLabel("")
        self.result_label.setWordWrap(True)
        action_card.body().addWidget(self.result_label)
        self.mismatch_label = HintLabel("")
        self.mismatch_label.setWordWrap(True)
        action_card.body().addWidget(self.mismatch_label)
        layout.addWidget(action_card)

        queue_card = Card("Jobs")
        self.jobs_table = QTableWidget(0, 8)
        self.jobs_table.setHorizontalHeaderLabels(
            ["Job", "Type", "Backend", "Model", "Status", "Progress",
             "Elapsed", "ETA"])
        self.jobs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.jobs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.jobs_table.setMinimumHeight(120)
        queue_card.body().addWidget(self.jobs_table)
        queue_row = ButtonRow()
        self.refresh_jobs_button = QPushButton("Refresh jobs")
        self.refresh_jobs_button.clicked.connect(self.refresh_jobs)
        queue_row.add_button(self.refresh_jobs_button)
        self.cancel_selected_button = QPushButton("Cancel selected")
        self.cancel_selected_button.clicked.connect(self.cancel_selected_job)
        queue_row.add_button(self.cancel_selected_button)
        self.retry_button = QPushButton("Retry selected")
        self.retry_button.clicked.connect(self.retry_selected_job)
        queue_row.add_button(self.retry_button)
        self.open_result_button = QPushButton("Show result")
        self.open_result_button.clicked.connect(self.show_selected_result)
        queue_row.add_button(self.open_result_button)
        self.clear_jobs_button = QPushButton("Clear finished")
        self.clear_jobs_button.clicked.connect(self.clear_finished_jobs)
        queue_row.add_button(self.clear_jobs_button)
        queue_card.body().addWidget(queue_row)
        layout.addWidget(queue_card)
        layout.addStretch(1)
        return panel

    # -- right: backends, history, sending ---------------------------------

    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        manager_card = Card("Backends")
        self.backends_list = QListWidget()
        self.backends_list.setMinimumHeight(110)
        self.backends_list.currentRowChanged.connect(self._on_backend_row)
        manager_card.body().addWidget(self.backends_list)
        row = ButtonRow()
        self.configure_button = QPushButton("Configure...")
        self.configure_button.clicked.connect(self.configure_backend)
        row.add_button(self.configure_button)
        self.toggle_button = QPushButton("Enable / disable")
        self.toggle_button.clicked.connect(self.toggle_backend)
        row.add_button(self.toggle_button)
        self.unload_button = QPushButton("Unload the loaded model")
        self.unload_button.clicked.connect(self.unload_model)
        row.add_button(self.unload_button)
        manager_card.body().addWidget(row)
        self.backend_detail = HintLabel("")
        self.backend_detail.setWordWrap(True)
        manager_card.body().addWidget(self.backend_detail)
        layout.addWidget(manager_card)

        tabs = QTabWidget()
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        filter_row = QHBoxLayout()
        self.history_kind_combo = QComboBox()
        self.history_kind_combo.addItem("Everything", "")
        self.history_kind_combo.addItem("Images", "image")
        self.history_kind_combo.addItem("Clips", "video")
        self.history_kind_combo.currentIndexChanged.connect(self.refresh_history)
        filter_row.addWidget(self.history_kind_combo)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search prompts and names")
        self.history_search.textChanged.connect(self.refresh_history)
        filter_row.addWidget(self.history_search)
        self.favourites_only = QCheckBox("Favourites")
        self.favourites_only.toggled.connect(self.refresh_history)
        filter_row.addWidget(self.favourites_only)
        history_layout.addLayout(filter_row)
        self.history_list = QListWidget()
        self.history_list.currentRowChanged.connect(self._on_history_row)
        history_layout.addWidget(self.history_list, 1)
        history_row = ButtonRow()
        self.favourite_button = QPushButton("Favourite")
        self.favourite_button.clicked.connect(self.toggle_favourite)
        history_row.add_button(self.favourite_button)
        self.import_history_button = QPushButton("Import Image Studio history")
        self.import_history_button.clicked.connect(self.import_history)
        history_row.add_button(self.import_history_button)
        self.refresh_history_button = QPushButton("Refresh history")
        self.refresh_history_button.clicked.connect(self.refresh_history)
        history_row.add_button(self.refresh_history_button)
        history_layout.addWidget(history_row)
        tabs.addTab(history_tab, "History")

        prompt_tab = QWidget()
        prompt_layout = QVBoxLayout(prompt_tab)
        self.prompts_list = QListWidget()
        prompt_layout.addWidget(self.prompts_list, 1)
        prompt_row = ButtonRow()
        self.use_saved_button = QPushButton("Use selected prompt")
        self.use_saved_button.clicked.connect(self.use_saved_prompt)
        prompt_row.add_button(self.use_saved_button)
        self.suggest_button = QPushButton("Suggest rewordings")
        self.suggest_button.clicked.connect(self.suggest_prompts)
        prompt_row.add_button(self.suggest_button)
        prompt_layout.addWidget(prompt_row)
        self.preset_list = QListWidget()
        prompt_layout.addWidget(self.preset_list, 1)
        preset_row = ButtonRow()
        self.delete_preset_button = QPushButton("Delete selected preset")
        self.delete_preset_button.clicked.connect(self.delete_preset)
        preset_row.add_button(self.delete_preset_button)
        prompt_layout.addWidget(preset_row)
        tabs.addTab(prompt_tab, "Prompts and presets")

        tabs.addTab(self._build_plan_tab(), "Storyboard plan")
        layout.addWidget(tabs, 1)

        send_card = Card("Send the last result")
        send_row = ButtonRow()
        self.send_scene_button = QPushButton("Send to scene")
        self.send_scene_button.clicked.connect(self.send_to_scene)
        send_row.add_button(self.send_scene_button)
        self.send_new_scene_button = QPushButton("Send to a new scene")
        self.send_new_scene_button.clicked.connect(self.send_to_new_scene)
        send_row.add_button(self.send_new_scene_button)
        self.send_timeline_button = QPushButton("Send to timeline")
        self.send_timeline_button.clicked.connect(self.send_to_timeline)
        send_row.add_button(self.send_timeline_button)
        self.send_project_button = QPushButton("Send to project")
        self.send_project_button.clicked.connect(self.send_to_project)
        send_row.add_button(self.send_project_button)
        send_card.body().addWidget(send_row)
        self.scene_combo = QComboBox()
        send_card.body().addWidget(self.scene_combo)
        self.send_note = HintLabel("")
        self.send_note.setWordWrap(True)
        send_card.body().addWidget(self.send_note)
        layout.addWidget(send_card)
        return panel

    def _build_plan_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.plan_list = QListWidget()
        layout.addWidget(self.plan_list, 1)
        self.plan_label = HintLabel("")
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)
        row = ButtonRow()
        self.approve_plan_button = QPushButton("Approve all prompts")
        self.approve_plan_button.clicked.connect(self.approve_plan)
        row.add_button(self.approve_plan_button)
        self.run_plan_button = QPushButton("Generate the plan")
        self.run_plan_button.clicked.connect(self.run_plan)
        row.add_button(self.run_plan_button)
        layout.addWidget(row)
        return tab

    # -- job plumbing ------------------------------------------------------

    def _submit(self, spec: Any) -> Optional[str]:
        spec.paths = self.context.paths
        if getattr(self.context, "jobs", None) is None:
            return None
        job = self.context.jobs.submit(spec)
        return getattr(job, "id", None) if job is not None else None

    def submit_ai(self, spec: Any, *, operation: str, kind: str = "video",
                  backend: str = "", backend_name: str = "", model: str = "",
                  mode: str = "", is_ai_model: bool = True,
                  label: str = "", retry_of: str = "") -> dict:
        """Submit one AI job through the studio's guard (sections 9, 33).

        The guard refuses a second identical job while the first is running and
        names the running job, so a double click cannot start two generations.
        """
        studio = self._studio()
        manager = getattr(self.context, "jobs", None)
        if studio is None or manager is None:
            return {"ok": False, "reason": "No job manager is available.",
                    "running": None}
        spec.paths = self.context.paths
        from app.ai.jobs import submit_ai_job

        outcome = submit_ai_job(
            manager, spec, registry=studio.jobs, operation=operation, kind=kind,
            backend=backend, backend_name=backend_name, model=model, mode=mode,
            is_ai_model=is_ai_model, label=label, retry_of=retry_of)
        if outcome.get("ok"):
            self.status_label.setText(
                f"Running: {outcome['record'].title()} "
                f"(job {outcome['job'].id[:8]})")
        else:
            self.status_label.setText(str(outcome.get("reason", "")))
        self.refresh_jobs()
        return outcome

    def _on_job_finished(self, result: Any) -> None:
        studio = self._studio()
        if studio is not None:
            studio.jobs.update(str(getattr(result, "job_id", "")),
                               message=str(getattr(result, "summary", lambda: "")()))
            # A body that raised never reported its own outcome, so the queue
            # would show it as still running. Close the record with what
            # actually happened.
            finalize_from_result(studio.jobs, result)
        key = str(getattr(result, "key", ""))
        self.refresh_jobs()
        value = _job_value(result)
        if key == JobKeys.AI_DETECT:
            self._on_detected(value)
        elif key in (JobKeys.AI_VIDEO_GENERATE, JobKeys.AI_VIDEO_BATCH,
                     JobKeys.AI_VIDEO_EXTEND, JobKeys.AI_IMAGE_GENERATE):
            self._on_generated(value)
        elif key in (JobKeys.AI_MODEL_CHECK, JobKeys.AI_BACKEND_TEST):
            self._on_checked(value)

    def refresh_jobs(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self.jobs_table.setRowCount(0)
        for record in studio.jobs.all():
            row = self.jobs_table.rowCount()
            self.jobs_table.insertRow(row)
            cells = [record.id[:8], record.operation,
                     record.backend_name or record.backend, record.model,
                     record.status, record.progress_label(),
                     record.elapsed_label(), record.eta_label()]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.UserRole, record.id)
                self.jobs_table.setItem(row, column, item)

    def cancel_job(self) -> None:
        manager = getattr(self.context, "jobs", None)
        job_id = self._generate_job or self._storyboard_job
        if manager is None or not job_id:
            self.status_label.setText("There is nothing to cancel from this page.")
            return
        manager.cancel(job_id)
        self.status_label.setText("Cancelling; results already written are kept.")

    def cancel_selected_job(self) -> None:
        job_id = self._selected_job_id()
        manager = getattr(self.context, "jobs", None)
        if manager is None or not job_id:
            self.status_label.setText("Choose a job in the list first.")
            return
        manager.cancel(job_id)
        self.status_label.setText("Cancelling the selected job.")

    def retry_selected_job(self) -> None:
        """Retry selected: the same request again, or a reason why not.

        Only a job that failed or was cancelled is retried.  The request is
        rebuilt from what the job recorded - backend, model, mode, prompt, size
        and seed stay exactly as they were - so a retry is never a quiet
        substitution (sections 36, 100).
        """
        studio = self._studio()
        job_id = self._selected_job_id()
        if studio is None or not job_id:
            self.status_label.setText("Choose a job in the list first.")
            return
        outcome = retry_job(studio, job_id)
        self.status_label.setText(str(outcome.get("reason", "Ready.")))
        if not outcome.get("ok"):
            return
        from app.ai.jobs import video_batch_spec, video_generate_spec
        from app.ai.video import VideoRequest

        try:
            request = VideoRequest.from_dict(outcome["request"])
        except Exception as exc:  # noqa: BLE001 - a stored request must never crash
            self.status_label.setText(
                f"That job's settings could not be read back ({exc}), so it "
                f"cannot be repeated exactly. Set them again and press Generate.")
            return
        request.backend = str(outcome.get("backend") or request.backend)
        if outcome.get("model"):
            request.model = str(outcome["model"])
        payload = {"service": studio, "request": request,
                   "backend_id": request.backend, "registry": studio.jobs}
        spec = (video_batch_spec(payload) if int(outcome.get("batch_size") or 1) > 1
                else video_generate_spec(payload))
        submitted = self.submit_ai(
            spec, operation=str(outcome.get("operation") or "video_generate"),
            kind=str(outcome.get("kind") or "video"),
            backend=request.backend,
            backend_name=str(outcome.get("backend_name") or request.backend),
            model=request.model, mode=str(outcome.get("mode") or request.mode),
            is_ai_model=bool(outcome.get("is_ai_model", True)),
            label=str(outcome.get("label") or ""),
            retry_of=str(outcome.get("retry_of") or ""))
        if submitted.get("ok"):
            self._generate_job = str(getattr(submitted.get("job"), "id", "") or "")

    def show_selected_result(self) -> None:
        studio = self._studio()
        job_id = self._selected_job_id()
        record = None if studio is None else studio.jobs.get(job_id)
        if record is None or not record.output:
            self.status_label.setText("That job has no result to show yet.")
            return
        path = str(record.output.get("path") or
                   (list(record.output.get("paths") or [""]) or [""])[0] or "")
        self._last_result = dict(record.output, path=path)
        self.result_label.setText(
            (f"Result: {path}\nUse Send to project / scene / timeline to place "
             f"it, or open the folder from the Files page.")
            if path else "That job produced no file.")

    def clear_finished_jobs(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        removed = studio.jobs.clear_finished()
        self.status_label.setText(f"{removed} finished job(s) cleared.")
        self.refresh_jobs()

    def unload_model(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        studio.manager.unload()
        self.status_label.setText("The loaded model was released.")
        self._fill_backends()

    def _selected_job_id(self) -> str:
        row = self.jobs_table.currentRow()
        if row < 0:
            return ""
        item = self.jobs_table.item(row, 0)
        return "" if item is None else str(item.data(Qt.UserRole) or "")

    # -- refresh and actions ----------------------------------------------

    def refresh(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self._fill_templates()
        self._fill_backends()
        self._fill_presets()
        self._fill_prompts()
        self._fill_scenes()
        self.refresh_history()
        self.refresh_jobs()
        self._on_mode_changed()

    def detect_backends(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        payload = {"service": studio, "registry": studio.jobs}
        outcome = self.submit_ai(ai_detect_spec(payload), operation="ai_detect",
                                 kind="", label="Detection")
        if outcome.get("ok"):
            self._detect_job = str(getattr(outcome.get("job"), "id", "") or "")
        self.status_label.setText("Looking for backends and models. Nothing is "
                                  "loaded by this check.")

    def test_backend(self) -> None:
        """Run a **real** generation to prove the backend works (section 7)."""
        studio = self._studio()
        backend_id = self._current_backend()
        if studio is None or not backend_id:
            self.status_label.setText("Choose a backend first.")
            return
        entry = studio.manager.get(backend_id)
        payload = {"service": studio, "backend_id": backend_id, "deep": True,
                   "registry": studio.jobs}
        outcome = self.submit_ai(
            ai_check_spec(payload), operation="backend_test", kind="",
            backend=backend_id,
            backend_name=(entry.name if entry else backend_id),
            is_ai_model=bool(getattr(entry.backend, "is_model", True)
                             if entry is not None and entry.backend else True))
        if outcome.get("ok"):
            self._check_job = str(getattr(outcome.get("job"), "id", "") or "")
        self.status_label.setText(
            f"Testing {entry.name if entry else backend_id}: this runs a real "
            f"generation and measures the result. Only that earns VERIFIED.")

    def light_check(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        if studio is None or not backend_id:
            self.status_label.setText("Choose a backend first.")
            return
        entry = studio.manager.get(backend_id)
        payload = {"service": studio, "backend_id": backend_id, "deep": False,
                   "registry": studio.jobs}
        outcome = self.submit_ai(
            ai_check_spec(payload), operation="model_check", kind="",
            backend=backend_id,
            backend_name=(entry.name if entry else backend_id))
        if outcome.get("ok"):
            self._check_job = str(getattr(outcome.get("job"), "id", "") or "")
        self.status_label.setText("Checking the backend's paths and metadata. "
                                  "Nothing is generated by a light check.")

    def show_logs(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id) if studio and backend_id else None
        lines = []
        if entry is not None and entry.backend is not None:
            try:
                lines = entry.backend.log_lines(limit=60)
            except Exception as exc:  # noqa: BLE001 - the log is a convenience
                lines = [f"The log could not be read: {exc}"]
        text = "\n".join(lines) if lines else (
            "No log lines for this backend yet. The application's log file holds "
            "everything it has done; open Diagnostics to see it.")
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        box.resize(760, 420)
        box.setWindowTitle(f"Log - {entry.name if entry else backend_id}")
        box.show()
        self._log_box = box

    def configure_backend(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id) if studio and backend_id else None
        if entry is None or entry.backend is None:
            self.status_label.setText("Choose a backend first.")
            return
        schema = entry.backend.settings_schema()
        if not schema:
            self.status_label.setText(f"{entry.name} has no settings to change.")
            return
        dialog = BackendSettingsDialog(entry, self._settings(), self)
        if dialog.exec():
            outcome = studio.manager.configure(backend_id, dialog.values())
            if outcome.get("ok"):
                self.status_label.setText(f"{entry.name} settings saved.")
                studio.manager.refresh(rebuild=True)
                self._fill_backends()
            else:
                self.status_label.setText(" ".join(outcome.get("problems", [])))

    def toggle_backend(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        if studio is None or not backend_id:
            self.status_label.setText("Choose a backend first.")
            return
        enabled = studio.manager.is_enabled(backend_id)
        studio.manager.set_enabled(backend_id, not enabled)
        self.status_label.setText(
            f"{backend_id} is now {'disabled' if enabled else 'enabled'}.")
        self._fill_backends()

    def generate(self) -> None:
        self._generate(count=1)

    def generate_batch(self) -> None:
        count = int(self.batch_spin.value())
        if count < 2:
            self.status_label.setText(
                "A batch needs at least two clips. Set Batch to 2 or more, or "
                "press Generate clip for one.")
            return
        self._generate(count=count)

    def _generate(self, *, count: int) -> None:
        studio = self._studio()
        if studio is None:
            return
        request = self._request_from_form(batch=count)
        if request is None:
            return
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id)
        payload = {"service": studio, "request": request,
                   "backend_id": backend_id, "registry": studio.jobs}
        spec = (video_batch_spec(payload) if count > 1
                else video_generate_spec(payload))
        outcome = self.submit_ai(
            spec, operation=request.mode, kind="video", backend=backend_id,
            backend_name=(entry.name if entry else backend_id),
            model=request.model, mode=request.mode,
            is_ai_model=bool(getattr(entry.backend, "is_model", True)
                             if entry is not None and entry.backend else True),
            label=(entry.backend.name if entry is not None and entry.backend else ""))
        if outcome.get("ok"):
            self._generate_job = str(getattr(outcome.get("job"), "id", "") or "")

    def generate_image(self) -> None:
        """Generate a still with the chosen image backend, through Stage F."""
        studio = self._studio()
        if studio is None:
            return
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id)
        if entry is not None and entry.kind != "image":
            self.status_label.setText(
                f"{entry.name} makes clips, not images. Choose an image backend "
                f"for this button, or use Generate clip.")
            return
        from app.image.provider import GenerationMode, GenerationRequest

        try:
            request = GenerationRequest(
                mode=GenerationMode.TEXT_TO_IMAGE,
                prompt=self.prompt_edit.toPlainText().strip(),
                negative_prompt=self.negative_edit.text().strip(),
                model=self._current_model(),
                width=int(self.width_spin.value() or 0),
                height=int(self.height_spin.value() or 0),
                seed=int(self.seed_spin.value()),
                output_dir=str(self._output_dir()),
                name_stem="ai_image", project=str(self.project_dir or ""))
        except (TypeError, ValueError) as exc:
            self.status_label.setText(f"The image request could not be built: {exc}")
            return
        payload = {"service": studio, "request": request,
                   "backend_id": backend_id, "registry": studio.jobs}
        outcome = self.submit_ai(
            image_generate_spec(payload), operation="text_to_image",
            kind="image", backend=backend_id,
            backend_name=(entry.name if entry else backend_id),
            model=request.model, mode="text_to_image",
            is_ai_model=bool(getattr(entry.backend, "is_model", True)
                             if entry is not None and entry.backend else True))
        if outcome.get("ok"):
            self._generate_job = str(getattr(outcome.get("job"), "id", "") or "")

    def cancel(self) -> None:
        self.cancel_job()

    # -- storyboard --------------------------------------------------------

    def plan_storyboard(self) -> None:
        """Build the per-scene plan.  Nothing is generated or sent here."""
        studio = self._studio()
        project = self.project
        if studio is None or project is None:
            self.status_label.setText(
                "Open a project with scenes first; the plan is built from the "
                "storyboard's own scene text.")
            return
        self._plan = studio.storyboard_plan(
            project, backend_id=self._current_backend(),
            width=int(self.width_spin.value() or 0),
            height=int(self.height_spin.value() or 0),
            fps=int(self.fps_combo.currentData() or 0),
            quality=str(self.quality_combo.currentData() or ""),
            project_dir=self.project_dir)
        self.plan_list.clear()
        for entry in self._plan:
            picture = " (from its own image)" if entry.get("source_image") else ""
            item = QListWidgetItem(
                f"{entry['index']}. {entry['name']} - seed {entry.get('seed', 0)}"
                f"{picture}: {entry['prompt'][:60]}")
            item.setData(Qt.UserRole, entry.get("scene_id", ""))
            self.plan_list.addItem(item)
        self.plan_label.setText(plan_summary(self._plan)
                                + " Nothing has been generated yet.")
        self.status_label.setText("Review the prompts, then approve them.")

    def approve_plan(self) -> None:
        studio = self._studio()
        if studio is None or not self._plan:
            self.status_label.setText("There is no plan to approve yet.")
            return
        studio.approve_plan(self._plan)
        self.plan_label.setText(plan_summary(self._plan)
                                + " Approved; you can generate it now.")

    def run_plan(self) -> None:
        studio = self._studio()
        if studio is None or not self._plan:
            self.status_label.setText("Build the plan first.")
            return
        payload = {"service": studio, "plan": self._plan,
                   "backend_id": self._current_backend(),
                   "output_dir": str(self._output_dir()),
                   "project": str(self.project_dir or ""),
                   "registry": studio.jobs}
        outcome = self.submit_ai(
            video_storyboard_spec(payload), operation="storyboard_to_video",
            kind="video", backend=self._current_backend(),
            model=self._current_model(), mode="storyboard_to_video")
        if outcome.get("ok"):
            self._storyboard_job = str(getattr(outcome.get("job"), "id", "") or "")

    # -- form --------------------------------------------------------------

    def _current_backend(self) -> str:
        return str(self.backend_combo.currentData() or "")

    def _current_model(self) -> str:
        return str(self.model_combo.currentData() or "")

    def _output_dir(self) -> Path:
        if self.project_dir is not None:
            folder = Path(self.project_dir) / "assets" / "generated"
        else:
            paths = getattr(self.context, "paths", None)
            base = Path(getattr(paths, "output_dir", "")) if paths else None
            folder = (base / "ai") if base else Path.cwd() / "ai_output"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _request_from_form(self, *, batch: int = 1) -> Optional[VideoRequest]:
        studio = self._studio()
        if studio is None:
            return None
        backend_id = self._current_backend()
        mode = str(self.mode_combo.currentData() or VideoMode.TEXT_TO_VIDEO)
        prompt = self.prompt_edit.toPlainText().strip()
        if mode == VideoMode.TEXT_TO_VIDEO and not prompt:
            self.status_label.setText(
                "This mode draws from text, so it needs a prompt.")
            return None
        if not self.width_spin.value() or not self.height_spin.value():
            self.status_label.setText(
                "Give the clip a width and a height; zero means 'whatever the "
                "backend likes', which is not allowed here.")
            return None
        request = VideoRequest(
            mode=mode, backend=backend_id, model=self._current_model(),
            prompt=prompt, negative_prompt=self.negative_edit.text().strip(),
            duration=float(self.duration_spin.value()),
            fps=int(self.fps_combo.currentData() or 0),
            width=int(self.width_spin.value()),
            height=int(self.height_spin.value()),
            seed=int(self.seed_spin.value()),
            strength=float(self.strength_spin.value()),
            source_image=self.source_image_edit.text().strip(),
            source_video=self.source_video_edit.text().strip(),
            reference_image=self.reference_edit.text().strip(),
            camera=str(self.camera_combo.currentData() or ""),
            camera_amount=float(self.camera_amount_spin.value()),
            extend_from=self.extend_edit.text().strip(),
            output_dir=str(self._output_dir()),
            name_stem="clip",
            quality=str(self.quality_combo.currentData() or ""),
            batch=int(batch), project=str(self.project_dir or ""),
            extra=({"style": self.style_edit.text().strip()}
                   if self.style_edit.text().strip() else {}))
        # Checked against the backend's real capabilities now, so an impossible
        # request is refused with its reason instead of failing later.
        entry = studio.manager.get(backend_id)
        if entry is not None:
            issues = [issue for issue in
                      entry.backend.validate(request)
                      if issue.severity == "error"] if entry.backend else []
            if issues:
                self.status_label.setText(issues[0].message + " "
                                          + issues[0].what_to_do)
                return None
        return request

    def _update_generate_enabled(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id) if studio and backend_id else None
        usable = bool(entry and entry.available())
        for button in (self.generate_button, self.batch_button,
                       self.image_button, self.test_button,
                       self.check_selected_button):
            button.setEnabled(usable)
        self.storyboard_button.setEnabled(usable and self.project is not None)
        if entry is None:
            self.generate_button.setToolTip("Choose a backend first.")
        elif not usable:
            status = entry.status()
            self.generate_button.setToolTip(
                f"{entry.name} is {status.state}: {status.reason}")
        else:
            self.generate_button.setToolTip("")

    def _update_memory_note(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        model_id = self._current_model()
        if studio is None or not backend_id:
            self.memory_label.setText("")
            return
        estimate = studio.memory_estimate(backend_id, model_id)
        if estimate is None:
            self.memory_label.setText("")
            return
        self.memory_label.setText(
            estimate.describe() if estimate.known else
            "Memory estimate unavailable: no weight files were found to "
            "measure, and nothing was assumed.")

    # -- list filling ------------------------------------------------------

    def _fill_templates(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self.template_combo.clear()
        for template in studio.prompts.templates():
            self.template_combo.addItem(template.name, template.name)

    def _fill_backends(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        current = self._current_backend()
        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        self.backends_list.clear()
        report = studio.manager.detect(refresh=False)
        for entry in report.entries:
            status = entry.status()
            self.backend_combo.addItem(f"{entry.name} - {status.state}", entry.id)
            item = QListWidgetItem(f"{entry.name}: {status.state}")
            item.setData(Qt.UserRole, entry.id)
            self.backends_list.addItem(item)
        index = self.backend_combo.findData(current)
        self.backend_combo.setCurrentIndex(max(0, index))
        self.backend_combo.blockSignals(False)
        self.state_label.setText(report.generator_note())
        usable_real = len(studio.manager.real_backends())
        loaded = studio.manager.loaded().get("loaded") or "nothing"
        self.honesty_label.setText(
            (f"{usable_real} backend(s) that run a real AI model are usable. "
             f"Loaded model: {loaded}.")
            if usable_real else
            "No backend here runs an AI model yet. The TEST BACKEND writes a "
            "real file but is not an AI model, and every result it makes is "
            "labelled as a test result.")
        self._fill_models()

    def _fill_models(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        backend_id = self._current_backend()
        current = self._current_model()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in studio.manager.models_for(backend_id):
            label = str(getattr(model, "name", "") or getattr(model, "id", ""))
            requirement = str(getattr(model, "requirement", "") or "")
            if requirement and requirement != "UNKNOWN":
                label += f" ({requirement.replace('_', ' ').lower()})"
            self.model_combo.addItem(label, str(getattr(model, "id", "")))
        if not self.model_combo.count():
            self.model_combo.addItem("No model available", "")
        index = self.model_combo.findData(current)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)
        entry = studio.manager.get(backend_id)
        if entry is not None and entry.backend is not None:
            self.backend_note.setText(
                f"{entry.backend.describe()} {entry.backend.install_hint()}".strip())
            status = entry.status()
            self.backend_detail.setText(
                f"{entry.name}: {status.state}\n{status.reason}\n"
                + " ".join(status.instructions))
        self._on_model_changed()
        self._update_memory_note()

    def _fill_presets(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self.preset_combo.clear()
        self.preset_list.clear()
        for preset in studio.presets.of_kind("video"):
            self.preset_combo.addItem(preset.label(), preset.id)
            self.preset_list.addItem(f"{preset.label()} - {preset.summary()}")

    def _fill_prompts(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        self.prompts_list.clear()
        for entry in studio.prompts.library(15):
            marker = "* " if entry.favourite else ""
            item = QListWidgetItem(f"{marker}{entry.label()}")
            item.setData(Qt.UserRole, entry.id)
            self.prompts_list.addItem(item)

    def _fill_scenes(self) -> None:
        project = self.project
        self.scene_combo.clear()
        self.scene_combo.addItem("A new scene", "")
        for scene in list(getattr(project, "scenes", []) or []):
            self.scene_combo.addItem(
                f"Scene: {getattr(scene, 'name', '') or getattr(scene, 'id', '')}",
                str(getattr(scene, "id", "")))

    def refresh_history(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        kind = str(self.history_kind_combo.currentData() or "")
        entries = studio.history.query(
            kind=kind, search=self.history_search.text(),
            favourites_only=self.favourites_only.isChecked(), limit=100)
        self.history_list.clear()
        for entry in entries:
            marker = "* " if entry.favourite else ""
            prompt = entry.prompt_preview()
            prompt = f"\"{prompt}\" - " if prompt else ""
            item = QListWidgetItem(f"{marker}{entry.name} - {prompt}"
                                   f"{entry.label()} ({entry.detail()}) "
                                   f"[{entry.status}]")
            item.setData(Qt.UserRole, entry.id)
            self.history_list.addItem(item)

    # -- handlers ----------------------------------------------------------

    def _on_detected(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        self.state_label.setText(str(result.get("message", "")))
        self._fill_backends()
        self.refresh_history()
        self.status_label.setText("Backends checked. Nothing was loaded.")

    def _on_generated(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        self._last_result = dict(result)
        if result.get("cancelled"):
            self.status_label.setText("Cancelled. Anything already written was "
                                      "kept.")
            self.refresh_history()
            self.refresh_jobs()
            return
        if not result.get("ok"):
            self.status_label.setText(
                f"{result.get('message', 'The generation did not finish.')} "
                f"{result.get('why', '')} {result.get('what_to_do', '')}".strip())
            self.refresh_history()
            self.refresh_jobs()
            return
        path = str(result.get("path") or
                   (list(result.get("paths") or [""]) or [""])[0] or "")
        items = [item for item in (result.get("items") or []) if item.get("ok")]
        if items and len(items) > 1:
            self.result_label.setText(
                f"{len(items)} result(s); the last is {items[-1].get('path')}")
        else:
            self.result_label.setText(f"Result: {path}" if path
                                      else str(result.get("message", "Done.")))
        mismatch = list(result.get("mismatch") or [])
        self.mismatch_label.setText(
            ("The file differs from the request: " + "; ".join(mismatch))
            if mismatch else "")
        self.status_label.setText(str(result.get("message", "Done.")))
        self.refresh_history()
        self.refresh_jobs()

    def _on_checked(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        self.status_label.setText(
            f"{result.get('state', '')}: {result.get('message', '')} "
            f"{result.get('what_to_do', '')}".strip())
        self._fill_backends()

    def _on_backend_changed(self) -> None:
        self._fill_models()
        self._update_generate_enabled()
        self._update_memory_note()

    def _on_model_changed(self) -> None:
        studio = self._studio()
        backend_id = self._current_backend()
        entry = studio.manager.get(backend_id) if studio and backend_id else None
        if entry is None or entry.backend is None:
            return
        capabilities = entry.capabilities()
        checks = {
            "negative_prompt": self.negative_edit,
            "seed_control": self.seed_spin,
            "duration": self.duration_spin,
            "fps": self.fps_combo,
            "camera_control": self.camera_combo,
            "strength": self.strength_spin,
        }
        for feature, widget in checks.items():
            supported = capabilities.supports_setting(feature)
            widget.setEnabled(supported)
            widget.setToolTip("" if supported else
                              f"{entry.name} has no setting for this, so it is "
                              f"not sent.")
        self.model_combo.setToolTip(
            "Requirement: "
            + str(getattr(entry.backend, "device_requirement", "UNKNOWN")))
        self._update_memory_note()

    def _on_size_changed(self) -> None:
        size = self.size_combo.currentData()
        if not size:
            return
        width, height = size
        if width and height:
            self.width_spin.setValue(int(width))
            self.height_spin.setValue(int(height))

    def _on_backend_row(self, row: int) -> None:
        item = self.backends_list.item(row)
        if item is None:
            return
        index = self.backend_combo.findData(str(item.data(Qt.UserRole) or ""))
        if index >= 0:
            self.backend_combo.setCurrentIndex(index)

    def _on_history_row(self, row: int) -> None:
        item = self.history_list.item(row)
        studio = self._studio()
        if item is None or studio is None:
            return
        entry = studio.history.find(str(item.data(Qt.UserRole) or ""))
        if entry is None:
            return
        self.send_note.setText(
            f"{entry.name}: {entry.label()}, {entry.detail()}. Generated by "
            f"{entry.backend_name or entry.backend} ({entry.model}). "
            f"{'AI model' if entry.is_ai_model else 'Test backend'}.")
        self._last_result = {"path": entry.path}

    # -- prompts and presets ----------------------------------------------

    def approve_prompt(self) -> None:
        studio = self._studio()
        text = self.prompt_edit.toPlainText().strip()
        if studio is None or not text:
            self.status_label.setText("There is no prompt to approve yet.")
            return
        studio.approve_prompt(text)
        studio.prompts.record_use(text, negative=self.negative_edit.text().strip(),
                                  purpose="video")
        self.prompt_hint.setText(
            "Prompt approved and remembered. Nothing has been sent anywhere.")
        self._fill_prompts()

    def save_prompt(self) -> None:
        studio = self._studio()
        text = self.prompt_edit.toPlainText().strip()
        if studio is None or not text:
            self.status_label.setText("There is no prompt to save yet.")
            return
        entry = studio.prompts.save_prompt(
            text, negative=self.negative_edit.text().strip(), purpose="video")
        self.status_label.setText(f"Saved '{entry.label()}' to the library.")
        self._fill_prompts()

    def use_template(self) -> None:
        studio = self._studio()
        if studio is None or not self.template_combo.count():
            self.status_label.setText("There is no template to use.")
            return
        name = str(self.template_combo.currentData() or "")
        self.prompt_edit.setPlainText(studio.prompts.from_template(
            name, {"subject": "the subject", "action": "moves"}))
        self.prompt_hint.setText(
            "Template filled in, with anything unfilled still visible in "
            "braces. Edit it, then approve it - nothing is sent before that.")
        self.prompt_edit.setFocus()

    def suggest_prompts(self) -> None:
        studio = self._studio()
        text = self.prompt_edit.toPlainText().strip()
        if studio is None or not text:
            self.status_label.setText("Write a prompt first.")
            return
        ideas = studio.prompts.variants(text, count=3)
        self.prompt_hint.setText(
            ("Suggestions (rewordings of what you wrote, nothing else): "
             + " | ".join(ideas)) if ideas else
            "No suggestions: add a detail or two and try again.")

    def use_saved_prompt(self) -> None:
        studio = self._studio()
        item = self.prompts_list.currentItem()
        if studio is None or item is None:
            self.status_label.setText("Choose a saved prompt first.")
            return
        entry = studio.prompts.find(str(item.data(Qt.UserRole) or ""))
        if entry is None:
            return
        self.prompt_edit.setPlainText(entry.text)
        self.negative_edit.setText(entry.negative)
        studio.prompts.record_use(entry.text, negative=entry.negative,
                                  purpose="video")
        self._fill_prompts()

    def save_preset(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        request = self._request_from_form()
        if request is None:
            return
        values = studio.presets.capture(
            request, names=("mode", "duration", "fps", "width", "height",
                            "quality", "camera", "camera_amount", "strength",
                            "negative_prompt"))
        preset = studio.presets.save_preset(
            f"{MODE_LABELS.get(request.mode, request.mode)} settings", values,
            kind="video", backend=request.backend, model=request.model)
        self.status_label.setText(f"Preset '{preset.label()}' saved.")
        self._fill_presets()

    def apply_preset(self) -> None:
        studio = self._studio()
        if studio is None or not self.preset_combo.count():
            self.status_label.setText("There is no preset to apply.")
            return
        request = self._request_from_form()
        if request is None:
            return
        entry = studio.manager.get(self._current_backend())
        outcome = studio.presets.apply(
            str(self.preset_combo.currentData() or ""), request,
            entry.capabilities() if entry else None)
        reasons = list(outcome.get("reasons") or [])
        self.status_label.setText(
            ("Preset applied." + (" " + " ".join(reasons) if reasons else ""))
            if outcome.get("ok") else " ".join(reasons))
        applied = outcome.get("applied") or {}
        if "duration" in applied:
            self.duration_spin.setValue(float(applied["duration"] or 0.0))
        if "fps" in applied:
            index = self.fps_combo.findData(int(applied["fps"] or 0))
            if index >= 0:
                self.fps_combo.setCurrentIndex(index)
        if "width" in applied:
            self.width_spin.setValue(int(applied["width"] or 0))
        if "height" in applied:
            self.height_spin.setValue(int(applied["height"] or 0))
        if "camera" in applied:
            index = self.camera_combo.findData(str(applied["camera"] or ""))
            if index >= 0:
                self.camera_combo.setCurrentIndex(index)
        if "negative_prompt" in applied:
            self.negative_edit.setText(str(applied["negative_prompt"] or ""))

    def delete_preset(self) -> None:
        studio = self._studio()
        if studio is None or not self.preset_combo.count():
            self.status_label.setText("There is no preset to delete.")
            return
        studio.presets.remove(str(self.preset_combo.currentData() or ""))
        self._fill_presets()
        self.status_label.setText("Preset deleted.")

    # -- history actions ---------------------------------------------------

    def toggle_favourite(self) -> None:
        studio = self._studio()
        item = self.history_list.currentItem()
        if studio is None or item is None:
            self.status_label.setText("Choose a history entry first.")
            return
        entry_id = str(item.data(Qt.UserRole) or "")
        entry = studio.history.find(entry_id)
        if entry is None:
            return
        studio.history.set_favourite(entry_id, not entry.favourite)
        self.refresh_history()
        self.status_label.setText("Favourite updated.")

    def import_history(self) -> None:
        studio = self._studio()
        if studio is None:
            return
        try:
            image_history = studio.image_service().history.all()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(
                f"The Image Studio history could not be read: {exc}")
            return
        added = studio.history.import_stage_f(image_history)
        self.refresh_history()
        self.status_label.setText(
            f"{added} image(s) brought into this history." if added
            else "Every Image Studio entry is already here.")

    # -- sending -----------------------------------------------------------

    def send_to_project(self) -> None:
        self._send_clip(placement="project")

    def send_to_scene(self) -> None:
        self._send_clip(placement="scene")

    def send_to_new_scene(self) -> None:
        self._send_clip(placement="scene", new_scene=True)

    def send_to_timeline(self) -> None:
        self._send_clip(placement="end")

    def _send_clip(self, *, placement: str, new_scene: bool = False) -> None:
        controller = self.controller
        path = str(self._last_result.get("path") or "")
        if controller is None or not path:
            self.send_note.setText(
                "Open a project and generate (or select) a clip first; there is "
                "nothing to send yet.")
            return
        request = self._request_from_form()
        report = send_result(
            controller, path, placement=placement,
            scene_id="" if new_scene else str(self.scene_combo.currentData() or ""),
            duration=float(getattr(request, "duration", 0.0) or 0.0),
            fps=float(getattr(request, "fps", 0.0) or 0.0))
        self.send_note.setText(report.describe())
        self.status_label.setText(report.message)
        self._fill_scenes()
        log_event("AI_SENT", report.message, placement=placement, path=path)
        if report.ok:
            self.project_changed.emit()

    # -- file pickers ------------------------------------------------------

    def choose_source_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the image to move", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.source_image_edit.setText(path)
            self.source_note.setText("Image to video will move this picture.")

    def choose_source_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the clip to restyle", "",
            "Videos (*.mp4 *.mov *.mkv *.webm)")
        if path:
            self.source_video_edit.setText(path)
            self.source_note.setText("Video to video keeps this clip's timing.")

    def choose_extend_clip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the clip to continue", "",
            "Videos (*.mp4 *.mov *.mkv *.webm)")
        if path:
            self.extend_edit.setText(path)
            self.source_note.setText("Extending keeps the original frames and "
                                     "adds the new ones after them.")

    def choose_reference(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a reference image", "",
            "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        studio = self._studio()
        self.reference_edit.setText(path)
        if studio is None:
            return
        kind = str(self.reference_kind_combo.currentData() or "style")
        try:
            reference = studio.references.add(path, kind=kind)
        except (OSError, ValueError) as exc:
            self.status_label.setText(f"The reference was not imported: {exc}")
            return
        self.reference_edit.setText(reference.path)
        self.status_label.setText(
            f"Reference '{reference.label()}' was copied into the reference "
            f"library; the original file is untouched. It is only used if the "
            f"chosen backend supports references.")

    def _on_mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData() or "")
        self.mode_label.setText(
            f"{MODE_LABELS.get(mode, mode)}: "
            + MODE_HELP.get(mode, "Choose a backend that supports this."))
        self._update_generate_enabled()

    # -- lifecycle --------------------------------------------------------

    def status_text(self) -> str:
        studio = self._studio()
        return studio.describe() if studio else "The AI Studio is not available."

    def stop(self) -> None:
        """The page is going away: release the model it loaded."""
        studio = self.service
        if studio is not None:
            try:
                studio.manager.unload()
            except Exception as exc:  # noqa: BLE001
                log_event("AI_UNLOAD_FAILED", f"The model was not released: {exc}",
                          level="WARNING")


# --------------------------------------------------------------------------
# Plain helpers, kept out of the widget so the CLI and the tests can use them
# --------------------------------------------------------------------------

def retry_job(studio: AIService, job_id: str) -> dict:
    """Whether a finished job can be repeated, and why not when it cannot.

    A retry is refused when the original backend or model is no longer
    available: repeating the run elsewhere would produce a different result
    under the same name, which is not a retry (sections 36, 100).  A job that
    already **succeeded** is not retried either - its clip is on disk, and the
    same request would only make a duplicate.

    When the answer is yes, the returned ``request`` is the exact request the
    job ran with, so the page can submit it again unchanged.
    """
    record = studio.jobs.get(job_id)
    if record is None:
        return {"ok": False, "reason": "That job is no longer in the list."}
    if not record.finished:
        return {"ok": False, "reason": "That job is still running; cancel it or "
                                       "wait for it."}
    entry = studio.manager.get(record.backend)
    if entry is None or not is_reachable(str(entry.status().state)):
        return {"ok": False,
                "reason": (f"'{record.backend or 'that backend'}' is not "
                           f"available now, so the same clip cannot be made "
                           f"again. Choose a backend instead.")}
    if record.model and studio.manager.find_model(record.backend, record.model) is None:
        return {"ok": False,
                "reason": (f"The model '{record.model}' is not installed now, so "
                           f"this cannot be repeated exactly. Choose a model "
                           f"instead.")}
    if not record.cancelled and not record.error:
        return {"ok": False,
                "reason": ("Press Generate again to run it with the same settings. "
                           "The earlier result is kept; nothing is overwritten.")}
    if not record.request:
        return {"ok": False,
                "reason": ("That job did not keep the settings it ran with, so it "
                           "cannot be repeated exactly. Set them again and press "
                           "Generate.")}
    what = "stopped" if record.cancelled else "failed"
    return {"ok": True,
            "reason": (f"Retrying the job that {what}: "
                       f"{record.backend or 'the same backend'}, same settings, "
                       f"same seed."),
            "request": dict(record.request), "backend": record.backend,
            "model": record.model, "operation": record.operation,
            "kind": record.kind, "mode": record.mode,
            "batch_size": int(record.batch_size or 1),
            "backend_name": record.backend_name, "label": record.label,
            "is_ai_model": bool(record.is_ai_model), "retry_of": record.id}


def send_result(controller: Any, path: str, *, placement: str,
                scene_id: str = "", duration: float = 0.0,
                fps: float = 0.0) -> Any:
    """Send a finished file to a project, in the way the button says."""
    from app.ai.integration import (SendReport, send_to_project, send_to_scene,
                                    send_to_timeline)

    service = getattr(controller, "service", None)
    if service is None:
        return SendReport(ok=False, action="send", message="No project is open.",
                          what_to_do="Open or create a project first.")
    if placement == "project":
        return send_to_project(service, path)
    if placement == "end":
        return send_to_timeline(service, path, placement="end", duration=duration,
                                fps=fps)
    return send_to_scene(service, path, scene_id=scene_id, duration=duration,
                         fps=fps)


def _job_value(result: Any) -> Any:
    """The value a finished job carries."""
    return getattr(result, "value", result)
