"""The Narration page: choose a voice, preview it, generate audio (sections 8-10, 44-47).

The beginner path is five controls: language, voice, preview, speed, generate.
Everything else - gender filter, search, favourites, voice identifiers, model
details, preprocessing - is available but out of the way.

Two hard rules shape this page:

* **Preview is not generation.**  Previewing speaks the preview text into the
  application's preview folder.  It never creates a narration file, never touches
  the project and never starts a render.
* **Nothing is faked.**  Voices come from the installed model; a voice that cannot
  generate is listed as unavailable with the reason, and a missing engine disables
  the buttons with an explanation rather than letting them fail mysteriously.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.tts.engine import MAX_SPEED, MIN_SPEED
from app.tts.narration import status_explanation
from app.tts.preprocess import PreprocessOptions
from app.tts.voices import (
    GENDERS,
    GENDER_LABELS,
    filter_voices,
    language_label,
    validate_voice_choice,
)

from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page

#: Sensible default preview text (section 10).
DEFAULT_PREVIEW_TEXT = "Hello! This is a preview of the selected voice."

#: Speed presets offered to the user (section 25).
SPEED_PRESETS: tuple[tuple[str, float], ...] = (
    ("0.75x - slow", 0.75),
    ("0.90x - relaxed", 0.90),
    ("1.00x - normal", 1.00),
    ("1.10x - brisk", 1.10),
    ("1.25x - fast", 1.25),
    ("1.50x - very fast", 1.50),
)


class NarrationPage(Page):
    """Pick a voice, hear it, and generate the narration."""

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Narration",
            "Choose a local Kokoro voice, preview it, then generate the narration audio.",
            parent,
        )
        self.context = context
        self.catalogue = None
        self._loading = False
        #: Version of the model found by the last scan.  It is part of the
        #: staleness hash, so a model upgrade is reported as STALE rather than
        #: silently reused (section 43).
        self.model_version = ""
        self._scan_job_id: Optional[str] = None
        self._scanned = False

        self._build_engine_card()
        self._build_voice_card()
        self._build_preview_card()
        self._build_generation_card()
        self.refresh()

    # -- access ------------------------------------------------------------

    @property
    def controller(self):
        return self.context.projects

    @property
    def project(self):
        return self.controller.project if self.controller is not None else None

    # -- cards -------------------------------------------------------------

    def _build_engine_card(self) -> None:
        card = self.add_card("Voice engine")
        card.add(
            HintLabel(
                "Narration is generated on this computer by Kokoro 82M. No text is "
                "sent anywhere and no account is needed."
            )
        )
        self.engine_grid = KeyValueGrid()
        card.add(self.engine_grid)
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.refresh_voices_button = QPushButton("Refresh voices")
        self.refresh_voices_button.setToolTip("Re-reads the voice catalogue from the installed model.")
        self.refresh_voices_button.clicked.connect(self.refresh_catalogue)
        self.open_check_button = QPushButton("Open System check")
        self.open_check_button.clicked.connect(lambda: self.context.main_window.show_page("system_check")
                                               if getattr(self.context, "main_window", None) else None)
        row.addWidget(self.refresh_voices_button)
        row.addWidget(self.open_check_button)
        row.addStretch(1)
        card.body().addLayout(row)
        self.engine_hint = HintLabel("")
        card.add(self.engine_hint)

    def _build_voice_card(self) -> None:
        card = self.add_card("Voice")

        filters = QHBoxLayout()
        filters.setSpacing(METRICS.sm)
        filters.addWidget(QLabel("Language:"))
        self.language_combo = QComboBox()
        self.language_combo.setMinimumWidth(180)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        filters.addWidget(self.language_combo)

        filters.addWidget(QLabel("Gender:"))
        self.gender_combo = QComboBox()
        self.gender_combo.addItem("Any", "all")
        for gender in GENDERS:
            self.gender_combo.addItem(GENDER_LABELS[gender], gender)
        self.gender_combo.currentIndexChanged.connect(self._refresh_voice_table)
        filters.addWidget(self.gender_combo)

        self.favourites_only = QCheckBox("Favourites only")
        self.favourites_only.toggled.connect(self._refresh_voice_table)
        filters.addWidget(self.favourites_only)
        filters.addStretch(1)
        card.body().addLayout(filters)

        search_row = QHBoxLayout()
        search_row.setSpacing(METRICS.sm)
        search_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Type part of a voice name")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._refresh_voice_table)
        search_row.addWidget(self.search_edit, 1)
        card.body().addLayout(search_row)

        self.voice_table = QTableWidget(0, 4)
        self.voice_table.setHorizontalHeaderLabels(["Voice", "Language", "Gender", "Status"])
        self.voice_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.voice_table.verticalHeader().setVisible(False)
        self.voice_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.voice_table.setSelectionMode(QTableWidget.SingleSelection)
        self.voice_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.voice_table.setMinimumHeight(200)
        self.voice_table.itemSelectionChanged.connect(self._on_voice_selected)
        self.voice_table.itemDoubleClicked.connect(lambda _item: self._toggle_favourite())
        card.add(self.voice_table)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.use_voice_button = QPushButton("Use this voice")
        mark_primary(self.use_voice_button)
        self.use_voice_button.clicked.connect(self._apply_voice)
        self.favourite_button = QPushButton("Toggle favourite")
        self.favourite_button.setToolTip("Double-click a voice to do the same thing.")
        self.favourite_button.clicked.connect(self._toggle_favourite)
        buttons.addWidget(self.use_voice_button)
        buttons.addWidget(self.favourite_button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

        self.voice_hint = HintLabel("")
        card.add(self.voice_hint)

        self.advanced_box = QCheckBox("Show technical details")
        self.advanced_box.setToolTip("Shows the raw voice identifier and model information.")
        self.advanced_box.toggled.connect(self._refresh_advanced)
        card.add(self.advanced_box)
        self.advanced_grid = KeyValueGrid()
        self.advanced_grid.setVisible(False)
        card.add(self.advanced_grid)

    def _build_preview_card(self) -> None:
        card = self.add_card("Preview")
        card.add(
            HintLabel(
                "Preview speaks the text below into a temporary file. It does not "
                "create narration, does not change the project and does not start "
                "any rendering."
            )
        )
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setPlainText(DEFAULT_PREVIEW_TEXT)
        self.preview_edit.setMinimumHeight(80)
        card.add(self.preview_edit)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        self.preview_button = QPushButton("Preview voice")
        self.preview_button.clicked.connect(self._preview)
        self.stop_preview_button = QPushButton("Stop")
        self.stop_preview_button.setEnabled(False)
        self.stop_preview_button.clicked.connect(self._stop_preview)
        row.addWidget(self.preview_button)
        row.addWidget(self.stop_preview_button)
        row.addStretch(1)
        card.body().addLayout(row)
        self.preview_hint = HintLabel("")
        card.add(self.preview_hint)

    def _build_generation_card(self) -> None:
        card = self.add_card("Generate")

        settings_row = QHBoxLayout()
        settings_row.setSpacing(METRICS.sm)
        settings_row.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        for label, value in SPEED_PRESETS:
            self.speed_combo.addItem(label, value)
        self.speed_combo.setCurrentIndex(2)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        settings_row.addWidget(self.speed_combo)

        self.custom_speed = QDoubleSpinBox()
        self.custom_speed.setRange(MIN_SPEED, MAX_SPEED)
        self.custom_speed.setSingleStep(0.05)
        self.custom_speed.setDecimals(2)
        self.custom_speed.setValue(1.00)
        self.custom_speed.setSuffix("x")
        self.custom_speed.setToolTip(f"Kokoro supports {MIN_SPEED}x to {MAX_SPEED}x.")
        self.custom_speed.valueChanged.connect(self._on_custom_speed)
        settings_row.addWidget(self.custom_speed)

        settings_row.addWidget(QLabel("Volume:"))
        self.volume_spin = QDoubleSpinBox()
        self.volume_spin.setRange(0, 125)
        self.volume_spin.setSingleStep(5)
        self.volume_spin.setSuffix("%")
        self.volume_spin.setValue(100)
        self.volume_spin.setToolTip("0% to 125%. Loudness above 100% is limited so it cannot clip.")
        self.volume_spin.valueChanged.connect(self._on_volume_changed)
        settings_row.addWidget(self.volume_spin)

        settings_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Full script as one file", "full_script")
        self.mode_combo.addItem("One file per scene", "section_scene")
        self.mode_combo.setToolTip(
            "Scenes are not rendered as video yet; per-scene narration only writes "
            "one audio file per script section."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        settings_row.addWidget(self.mode_combo)
        settings_row.addStretch(1)
        card.body().addLayout(settings_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.generate_button = QPushButton("Generate narration")
        mark_primary(self.generate_button)
        self.generate_button.clicked.connect(self._generate)
        self.regenerate_button = QPushButton("Regenerate")
        self.regenerate_button.setToolTip("Replaces the existing narration files.")
        self.regenerate_button.clicked.connect(self._generate)
        buttons.addWidget(self.generate_button)
        buttons.addWidget(self.regenerate_button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

        self.status_grid = KeyValueGrid()
        card.add(self.status_grid)
        self.status_hint = HintLabel("")
        card.add(self.status_hint)

    # -- engine state ------------------------------------------------------

    def refresh(self) -> None:
        """Reload the project's stored choices.

        Deliberately does *not* scan for voices: section 32 wants discovery to
        happen when the user opens the Voice panel, not while the application is
        starting, so :meth:`ensure_catalogue` is called by the window instead.
        """
        self._load_project_settings()

    def ensure_catalogue(self) -> None:
        """Scan once, the first time the user actually looks at this page."""
        if self._scanned or self._scan_job_id is not None:
            return
        self.refresh_catalogue()

    def refresh_catalogue(self) -> None:
        """Ask a background job to re-read the installed catalogue.

        Probing imports ``kokoro`` and an inference runtime, which can take
        several seconds on a cold start.  That must not happen on the Qt thread
        (sections 30 and 32), so this submits the Stage A scan job and fills the
        panel in :meth:`on_scan_finished`.
        """
        from app.tts.jobs import scan_spec

        self._scan_job_id = None
        settings = self.context.settings
        favourites = list(getattr(settings.voice, "favourite_voices", []) or [])
        spec = scan_spec(
            settings=settings,
            paths=self.context.paths,
            favourites=favourites,
        )
        spec.payload["model_dir"] = getattr(settings.voice, "model_dir", "") or ""

        self._scanned = True
        self.refresh_voices_button.setEnabled(False)
        self.engine_hint.setText("Scanning the installed Kokoro model for voices…")
        job = self.context.jobs.submit(spec)
        if job is None:
            # A scan is already running; its result will arrive shortly.
            self.engine_hint.setText("A voice scan is already running.")
            return
        self._scan_job_id = job.id

    def on_scan_finished(self, result) -> None:
        """Apply a finished voice scan.  Called by the window, on the Qt thread."""
        from app.tts.voices import VoiceCatalogue, VoiceInfo

        self.refresh_voices_button.setEnabled(True)
        if result.cancelled:
            self.engine_hint.setText("Voice scan cancelled.")
            return
        if result.error is not None:
            self.engine_hint.setText(f"Voice scan failed: {result.error.title}")
            return

        value = result.value or {}
        favourites = set(getattr(self.context.settings.voice, "favourite_voices", []) or [])
        voices = [
            VoiceInfo(
                id=entry["id"],
                language=entry.get("language", ""),
                gender=entry.get("gender", "unknown"),
                label=entry.get("label", ""),
                available=bool(entry.get("available", False)),
                note=entry.get("note", ""),
                favorite=entry["id"] in favourites,
            )
            for entry in value.get("voices", [])
        ]
        self.catalogue = VoiceCatalogue(
            voices=voices,
            languages=list(value.get("languages", [])),
            language_source=value.get("language_source", "") or "none",
        )
        self.model_version = value.get("model_name", "") or ""

        self.engine_grid.clear()
        self.engine_grid.add("Engine", "Kokoro 82M (local)")
        self.engine_grid.add(
            "Package",
            f"installed {value.get('package_version', '')}".strip()
            if value.get("installed") else "not installed",
        )
        self.engine_grid.add("Runtime", value.get("runtime", ""))
        self.engine_grid.add("Model", value.get("model", ""))
        self.engine_grid.add("Voices discovered", str(len(voices)))
        languages = list(value.get("languages", []))
        self.engine_grid.add(
            "Languages",
            f"{len(languages)} (from {value.get('language_source', 'none')})"
            if languages else "none detected",
        )
        blocker = value.get("blocker", "") or self.catalogue.blocker()
        self.engine_hint.setText(
            blocker or "Voices are read from the installed model. Add voice files and press Refresh."
        )

        self._rebuild_language_combo(languages)
        self._refresh_voice_table()
        self._update_buttons()
        self._load_project_settings()

    def _rebuild_language_combo(self, languages: list) -> None:
        """One entry per language, not one per voice prefix.

        Kokoro reports codes such as ``af`` and ``am`` (American English female /
        male).  Listing both would show "English (US)" twice, so they are grouped
        under the shared language and filtering matches the group.
        """
        self._loading = True
        try:
            current = self.language_combo.currentData()
            self.language_combo.clear()
            self.language_combo.addItem("All languages", "all")
            seen: list[str] = []
            for code in languages:
                family = code[:1] if len(code) >= 2 else code
                if family in seen:
                    continue
                seen.append(family)
                self.language_combo.addItem(language_label(family), family)
            if current is not None:
                index = self.language_combo.findData(current)
                if index >= 0:
                    self.language_combo.setCurrentIndex(index)
        finally:
            self._loading = False

    def _refresh_voice_table(self) -> None:
        if self.catalogue is None:
            return
        language = self.language_combo.currentData() or "all"
        gender = self.gender_combo.currentData() or "all"
        voices = filter_voices(
            self.catalogue,
            language="" if language == "all" else language,
            gender="" if gender == "all" else gender,
            search=self.search_edit.text(),
            favourites_only=self.favourites_only.isChecked(),
        )

        self._loading = True
        try:
            self.voice_table.setRowCount(len(voices))
            for row, voice in enumerate(voices):
                star = "★ " if voice.favorite else ""
                self.voice_table.setItem(row, 0, QTableWidgetItem(f"{star}{voice.label}"))
                self.voice_table.setItem(row, 1, QTableWidgetItem(language_label(voice.language)))
                self.voice_table.setItem(row, 2, QTableWidgetItem(GENDER_LABELS.get(voice.gender, voice.gender)))
                state = "Available" if voice.available else "Unavailable"
                item = QTableWidgetItem(state)
                if not voice.available:
                    item.setToolTip(voice.note)
                self.voice_table.setItem(row, 3, item)
                self.voice_table.item(row, 0).setData(Qt.UserRole, voice.id)
        finally:
            self._loading = False

        if not voices:
            self.voice_hint.setText(
                "No voices match these filters. Clear the search or choose another language."
            )
        else:
            unavailable = len([v for v in voices if not v.available])
            text = f"{len(voices)} voice(s) listed."
            if unavailable:
                text += f" {unavailable} cannot generate yet - hover the status for the reason."
            self.voice_hint.setText(text)

    def _selected_voice_id(self) -> str:
        row = self.voice_table.currentRow()
        if row < 0:
            return ""
        item = self.voice_table.item(row, 0)
        return item.data(Qt.UserRole) if item else ""

    def _on_voice_selected(self) -> None:
        if self._loading:
            return
        self._refresh_advanced()

    def _refresh_advanced(self, *_args) -> None:
        show = self.advanced_box.isChecked()
        self.advanced_grid.setVisible(show)
        if not show:
            return
        self.advanced_grid.clear()
        voice_id = self._selected_voice_id()
        voice = self.catalogue.find(voice_id) if self.catalogue and voice_id else None
        if voice is None:
            self.advanced_grid.add("Voice id", "(none selected)")
            return
        self.advanced_grid.add("Voice id", voice.id)
        self.advanced_grid.add("Language code", voice.language or "?")
        self.advanced_grid.add("Discovered from", voice.source)
        self.advanced_grid.add("Available", "yes" if voice.available else "no")
        if voice.note:
            self.advanced_grid.add("Note", voice.note)
        if self.catalogue is not None:
            self.advanced_grid.add("Catalogue source", self.catalogue.source)
            self.advanced_grid.add("Language source", self.catalogue.language_source)

    def _on_language_changed(self) -> None:
        """Changing language records the choice and clears an incompatible voice.

        Recording matters: generation validates the stored language against the
        chosen voice, so a language that was only ever held in the combo would
        leave the user unable to generate - told to "change the language" they
        had already changed (section 7).
        """
        if self._loading:
            return
        self._refresh_voice_table()
        self._refresh_advanced()
        controller = self.controller
        if controller is None or not controller.is_open:
            return
        language = self.language_combo.currentData() or ""
        if language and language != "all":
            controller.service.set_voice_settings(language=language)
            controller.refresh_dirty()
        current_voice = self.project.voice.voice if self.project else ""
        if not current_voice or language in ("", "all"):
            return
        ok, _message = validate_voice_choice(self.catalogue, current_voice, language)
        if not ok:
            controller.service.set_voice_settings(voice="", language=language)
            self.voice_hint.setText(
                f"The language changed to {language_label(language)}, so the previous "
                "voice was cleared. Choose a voice for this language."
            )
            self._update_buttons()

    def _toggle_favourite(self) -> None:
        voice_id = self._selected_voice_id()
        if not voice_id or self.catalogue is None:
            return
        voice = self.catalogue.find(voice_id)
        if voice is None:
            return
        voice.favorite = not voice.favorite
        settings = self.context.settings
        current = list(getattr(settings.voice, "favourite_voices", []) or [])
        if voice.favorite and voice_id not in current:
            current.append(voice_id)
        elif not voice.favorite and voice_id in current:
            current.remove(voice_id)
        settings.voice.favourite_voices = current
        self.context.mark_settings_dirty()
        self._refresh_voice_table()

    def _load_project_settings(self) -> None:
        project = self.project
        self._loading = True
        try:
            if project is not None:
                speed = float(project.voice.speed or 1.0)
                self.custom_speed.setValue(speed)
                index = self.speed_combo.findData(speed)
                self.speed_combo.setCurrentIndex(index if index >= 0 else 2)
                self.volume_spin.setValue(int(round(float(project.voice.volume or 1.0) * 100)))
                mode_index = self.mode_combo.findData(project.narration.mode)
                self.mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
                language = project.voice.language
                if language:
                    found = self.language_combo.findData(language)
                    if found < 0:
                        # Match on the leading code, e.g. "hi" for "hf".
                        for index in range(self.language_combo.count()):
                            data = self.language_combo.itemData(index)
                            if data and str(data).startswith(language[:2]):
                                found = index
                                break
                    if found >= 0:
                        self.language_combo.setCurrentIndex(found)
            preview = getattr(self.context.settings.voice, "preview_text", "") or DEFAULT_PREVIEW_TEXT
            self.preview_edit.setPlainText(preview)
        finally:
            self._loading = False
        self._refresh_status()
        self._update_buttons()

    def _on_speed_changed(self) -> None:
        if self._loading:
            return
        value = self.speed_combo.currentData()
        if value is not None:
            self._loading = True
            try:
                self.custom_speed.setValue(float(value))
            finally:
                self._loading = False
        self._store_settings()

    def _on_custom_speed(self, _value: float) -> None:
        if self._loading:
            return
        self._store_settings()

    def _on_volume_changed(self, _value: float) -> None:
        if self._loading:
            return
        self._store_settings()

    def _on_mode_changed(self) -> None:
        if self._loading:
            return
        self._store_settings()

    def _store_settings(self) -> None:
        controller = self.controller
        if controller is None or not controller.is_open:
            return
        controller.service.set_voice_settings(
            speed=round(float(self.custom_speed.value()), 2),
            volume=round(float(self.volume_spin.value()) / 100.0, 3),
        )
        mode = self.mode_combo.currentData()
        if mode and self.project is not None and self.project.narration.mode != mode:
            controller.service.set_narration_mode(mode)
        controller.refresh_dirty()
        self._refresh_status()

    # -- actions -----------------------------------------------------------

    def _apply_voice(self) -> None:
        controller = self.controller
        if controller is None or not controller.is_open:
            self.voice_hint.setText("Open a project before choosing a voice.")
            return
        voice_id = self._selected_voice_id()
        if not voice_id:
            self.voice_hint.setText("Select a voice in the list first.")
            return
        language = self.language_combo.currentData() or ""
        ok, message = validate_voice_choice(self.catalogue, voice_id, "" if language == "all" else language)
        voice = self.catalogue.find(voice_id)
        # With "All languages" selected there is no language to validate against,
        # so the voice's own language becomes the narration language.  Without
        # this the project would keep its previous language and refuse to
        # generate a voice that does not match it.
        chosen_language = "" if language in ("", "all") else language
        if not chosen_language and voice is not None and voice.language:
            chosen_language = voice.language[:1].lower()
        controller.service.set_voice_settings(
            voice=voice_id,
            gender=voice.gender if voice else "",
            language=chosen_language or None,
        )
        controller.refresh_dirty()
        if voice is not None and not voice.available:
            # The choice is stored, but the user is told plainly that it cannot
            # generate yet - and Generate stays disabled until it can.
            self.voice_hint.setText(
                f"“{voice_id}” is selected but cannot generate audio yet: {voice.note}"
            )
        elif not ok:
            self.voice_hint.setText(message)
        else:
            self.voice_hint.setText(f"Voice set to {voice_id}. Preview it, then generate.")
        self._refresh_status()
        self._update_buttons()

    def _preview(self) -> None:
        """Speak the preview text.  Creates no project data (section 10)."""
        from app.tts.jobs import preview_spec

        voice_id = self._selected_voice_id() or (self.project.voice.voice if self.project else "")
        if not voice_id:
            self.preview_hint.setText("Choose a voice first.")
            return
        text = self.preview_edit.toPlainText().strip()
        if not text:
            self.preview_hint.setText("Type something to hear first.")
            return

        self.context.settings.voice.preview_text = text
        self.preview_button.setEnabled(False)
        self.stop_preview_button.setEnabled(True)
        self.preview_hint.setText(f"Speaking with {voice_id}…")
        spec = preview_spec(
            text,
            voice_id,
            language=self.language_combo.currentData() or "",
            speed=float(self.custom_speed.value()),
            volume=float(self.volume_spin.value()) / 100.0,
            preprocess_options=PreprocessOptions(),
            settings=self.context.settings,
            paths=self.context.paths,
        )
        job = self.context.jobs.submit(spec)
        if job is None:
            self.preview_button.setEnabled(True)
            self.stop_preview_button.setEnabled(False)
            self.preview_hint.setText(
                "A preview is already running. Wait for it to finish, or press Stop."
            )
            return
        self._preview_job_id = job.id

    def _stop_preview(self) -> None:
        job_id = getattr(self, "_preview_job_id", "")
        if job_id:
            self.context.jobs.cancel(job_id, "Stopped by the user.")
        self.preview_hint.setText("Stopping the preview…")

    def on_preview_finished(self, result) -> None:
        """Called by the window when the preview job ends."""
        self.preview_button.setEnabled(True)
        self.stop_preview_button.setEnabled(False)
        if result.cancelled:
            self.preview_hint.setText("Preview cancelled.")
            return
        if result.error is not None:
            self.preview_hint.setText(f"Preview failed: {result.error.title}")
            return
        value = result.value or {}
        self.preview_hint.setText(
            f"Spoke {value.get('duration_seconds', 0):.2f}s of audio "
            f"({value.get('sample_rate', 0)} Hz). Nothing was added to the project."
        )

    def _generate(self) -> None:
        """Start exactly one narration job (section 28)."""
        from PySide6.QtWidgets import QMessageBox

        from app.tts.jobs import narration_spec

        controller = self.controller
        if controller is None or not controller.is_open:
            self.status_hint.setText("Open a project before generating narration.")
            return
        project = self.project
        voice_id = project.voice.voice
        ok, message = validate_voice_choice(self.catalogue, voice_id, project.voice.language)
        if not ok and (voice_id or not self.catalogue or not self.catalogue.available):
            QMessageBox.warning(self, "Narration cannot be generated yet", message)
            return
        if not (project.script.source_text or "").strip():
            QMessageBox.information(
                self, "There is nothing to narrate",
                "Write or import a script on the Script page first.",
            )
            return

        self._store_settings()
        # The flag lives on the service, not the controller: calling it on the
        # controller raised AttributeError the moment Generate was pressed.
        controller.service.mark_narration_generating()
        self.generate_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        self._refresh_status()

        spec = narration_spec(
            project,
            controller.layout.root,
            voice=voice_id,
            language=project.voice.language,
            speed=float(self.custom_speed.value()),
            volume=float(self.volume_spin.value()) / 100.0,
            model_version=self.model_version,
            preprocessing=dict(project.narration.preprocessing or {}),
            preprocess_options=PreprocessOptions(),
            section_ids=[section.id for section in project.script.sections],
            settings=self.context.settings,
            paths=self.context.paths,
        )
        job = self.context.jobs.submit(spec)
        if job is None:
            self.generate_button.setEnabled(True)
            self.regenerate_button.setEnabled(True)
            self.status_hint.setText(
                "A narration job is already running. One generation runs at a time."
            )
            return
        self._narration_job_id = job.id

    def on_narration_finished(self, result) -> None:
        """Called by the window when the narration job ends."""
        controller = self.controller
        self.generate_button.setEnabled(True)
        self.regenerate_button.setEnabled(True)
        if result.cancelled:
            self.status_hint.setText("Generation cancelled. The project was not changed.")
            self._refresh_status()
            return
        if result.error is not None:
            self.status_hint.setText(f"{result.error.title} - {result.error.what_happened}")
            self._refresh_status()
            return
        value = result.value or {}
        if controller is not None and controller.is_open:
            controller.service.refresh_narration_statuses(model_version=self.model_version)
            controller.refresh_dirty()
        self.status_hint.setText(value.get("summary", "Narration generated."))
        notes = value.get("preprocessing_notes") or []
        if notes:
            self.status_hint.setText(self.status_hint.text() + " Preprocessing: " + "; ".join(notes))
        self._refresh_status()

    # -- status ------------------------------------------------------------

    def _refresh_status(self) -> None:
        project = self.project
        self.status_grid.clear()
        if project is None:
            self.status_grid.add("Status", "No project open")
            self.status_hint.setText("Open a project to generate narration.")
            return
        plan = project.narration
        self.status_grid.add("Status", plan.status.replace("_", " ").title())
        self.status_grid.add("Voice", project.voice.voice or "not selected")
        self.status_grid.add("Language", language_label(project.voice.language) if project.voice.language else "not selected")
        self.status_grid.add("Speed", f"{float(project.voice.speed or 1.0):.2f}x")
        self.status_grid.add("Volume", f"{int(round(float(project.voice.volume or 1.0) * 100))}%")
        self.status_grid.add("Mode", plan.mode_label())
        self.status_grid.add("Files", str(len(plan.tracks)))
        if plan.tracks:
            self.status_grid.add("Audio length (measured)", f"{plan.total_duration_seconds():.2f}s")
            first = plan.tracks[0]
            self.status_grid.add("Output", first.path or "-")
            self.status_grid.add("Generated", first.generated_at or "-")
        self.status_hint.setText(status_explanation(project))

    def _update_buttons(self) -> None:
        """Disable actions that cannot work, and say why (section 54)."""
        has_project = self.controller is not None and self.controller.is_open
        usable = bool(self.catalogue and self.catalogue.available)
        blocker = self.catalogue.blocker() if self.catalogue else "No voices were discovered."

        self.generate_button.setEnabled(has_project and usable)
        self.regenerate_button.setEnabled(has_project and usable)
        self.preview_button.setEnabled(usable)
        self.use_voice_button.setEnabled(has_project and usable)

        if not usable:
            reason = blocker or "No voices are available."
            self.generate_button.setToolTip(reason)
            self.preview_button.setToolTip(reason)
        else:
            self.generate_button.setToolTip("Generates the narration audio for this project.")
            self.preview_button.setToolTip("Speaks the preview text; nothing is saved to the project.")
