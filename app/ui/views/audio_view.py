"""The Audio page: narration, music, effects and the mix (directive sections 5-8).

Every control here writes to a real field on ``project.audio`` and every button
starts a real job.  There is no placeholder action on this page:

* **Check audio** runs :class:`AudioService.validate` on a worker thread, so it
  really opens each file with FFmpeg and measures it.
* **Mix preview** renders the master mix to a WAV the user can listen to, without
  rendering any video (section 7).
* Sliders and boxes change the mix chain the renderer will use, and the page
  marks the project dirty so the change is saved.

Volumes are shown as percentages because that is what people expect, but they are
stored as the 0.0-1.0 fractions the model and the FFmpeg filter graph use.
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
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from app.project.model import SoundEffect, new_id
from app.render.jobs import audio_mix_spec, audio_validate_spec

from ..theme import mark_primary
from ..widgets.common import ButtonRow, HintLabel, Page, Row

#: Volume is a percentage on screen, a fraction in the project file.
_PERCENT = 100.0


def _spin(*, minimum: float, maximum: float, value: float, step: float = 0.05,
          suffix: str = "", decimals: int = 2) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setValue(value)
    return box


class AudioPage(Page):
    """Narration, music, effects and the mix - all bound to the project."""

    #: Emitted whenever the user changes something, so the window can mark the
    #: project dirty and offer to save.
    project_changed = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Audio",
            "Set the narration, music and effects levels. The mix is built by the "
            "same service the final render uses.",
            parent,
        )
        self.context = context
        self._loading = False
        self._validate_job: Optional[str] = None
        self._mix_job: Optional[str] = None
        self._last_mix_path = ""

        self._build_narration_card()
        self._build_music_card()
        self._build_sfx_card()
        self._build_mix_card()
        self._build_status_card()
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
    def audio(self):
        project = self.project
        return getattr(project, "audio", None) if project is not None else None

    # -- cards ------------------------------------------------------------

    def _build_narration_card(self) -> None:
        card = self.add_card("Narration")
        self.narration_enabled = QCheckBox("Include the narration in the mix")
        self.narration_enabled.toggled.connect(self._on_changed)
        card.add(Row("Narration", self.narration_enabled))

        self.narration_volume = _spin(minimum=0.0, maximum=200.0, value=100.0,
                                      step=5.0, suffix=" %", decimals=0)
        self.narration_volume.valueChanged.connect(self._on_changed)
        card.add(Row("Narration volume", self.narration_volume,
                     "100% leaves the generated voice untouched."))

        self.narration_hint = HintLabel("Narration files are created on the Narration page.")
        card.add(self.narration_hint)

    def _build_music_card(self) -> None:
        card = self.add_card("Music bed")
        row = QHBoxLayout()
        self.music_path = QLabel("No music file")
        self.music_path.setObjectName("Hint")
        self.music_path.setWordWrap(True)
        row.addWidget(self.music_path, 1)
        self.music_browse = QPushButton("Choose file…")
        self.music_browse.clicked.connect(self._choose_music)
        self.music_clear = QPushButton("Remove")
        self.music_clear.clicked.connect(self._clear_music)
        row.addWidget(self.music_browse)
        row.addWidget(self.music_clear)
        picker = QWidget()
        picker.setLayout(row)
        card.add(Row("Music file", picker,
                     "Copied into the project folder so the project stays portable."))

        self.music_volume = _spin(minimum=0.0, maximum=200.0, value=18.0,
                                  step=1.0, suffix=" %", decimals=0)
        self.music_volume.valueChanged.connect(self._on_changed)
        card.add(Row("Volume", self.music_volume,
                     "Music sits under the voice, so a low value is normal."))

        self.music_loop = QCheckBox("Loop the music to fill the video")
        self.music_loop.toggled.connect(self._on_changed)
        card.add(Row("Loop", self.music_loop))

        self.music_fade_in = _spin(minimum=0.0, maximum=30.0, value=1.0, suffix=" s")
        self.music_fade_in.valueChanged.connect(self._on_changed)
        self.music_fade_out = _spin(minimum=0.0, maximum=30.0, value=2.0, suffix=" s")
        self.music_fade_out.valueChanged.connect(self._on_changed)
        card.add(Row("Fade in / out", _pair(self.music_fade_in, self.music_fade_out)))

        self.music_start = _spin(minimum=0.0, maximum=86400.0, value=0.0, step=0.5, suffix=" s")
        self.music_start.valueChanged.connect(self._on_changed)
        self.music_end = _spin(minimum=0.0, maximum=86400.0, value=0.0, step=0.5, suffix=" s")
        self.music_end.valueChanged.connect(self._on_changed)
        card.add(Row("Start / stop", _pair(self.music_start, self.music_end),
                     "0s for 'stop' means run to the end of the video."))

    def _build_sfx_card(self) -> None:
        card = self.add_card("Sound effects")
        self.sfx_table = QTableWidget(0, 6)
        self.sfx_table.setHorizontalHeaderLabels(
            ["File", "Volume", "Anchor", "At", "Fade in", "Fade out"])
        self.sfx_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sfx_table.verticalHeader().setVisible(False)
        self.sfx_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.sfx_table.setSelectionMode(QTableWidget.SingleSelection)
        self.sfx_table.setMinimumHeight(150)
        card.add(self.sfx_table)

        self.sfx_add = QPushButton("Add effect…")
        self.sfx_add.clicked.connect(self._add_sfx)
        self.sfx_remove = QPushButton("Remove selected")
        self.sfx_remove.clicked.connect(self._remove_sfx)
        self.sfx_scene = QComboBox()
        self.sfx_scene.setMinimumWidth(180)
        card.add(ButtonRow([self.sfx_scene, self.sfx_add, self.sfx_remove]))
        card.add(HintLabel(
            "Anchor 'Scene' places the effect at the start of the scene chosen in the "
            "dropdown; 'Project' places it at an absolute time on the timeline."))

    def _build_mix_card(self) -> None:
        card = self.add_card("Mix")
        self.master_volume = _spin(minimum=0.0, maximum=200.0, value=100.0,
                                   step=5.0, suffix=" %", decimals=0)
        self.master_volume.valueChanged.connect(self._on_changed)
        card.add(Row("Master volume", self.master_volume,
                     "Applies to everything after the narration, music and effects "
                     "have been mixed."))

        self.normalize = QCheckBox("Normalise the finished mix")
        self.normalize.toggled.connect(self._on_changed)
        card.add(Row("Normalise", self.normalize))

        self.target_lufs = _spin(minimum=-40.0, maximum=0.0, value=-16.0, step=0.5,
                                 suffix=" LUFS")
        self.target_lufs.valueChanged.connect(self._on_changed)
        card.add(Row("Target loudness", self.target_lufs,
                     "-16 LUFS suits most online video; -14 is common for podcasts."))

        self.duck_enabled = QCheckBox("Lower the music while the voice is speaking")
        self.duck_enabled.toggled.connect(self._on_changed)
        card.add(Row("Ducking", self.duck_enabled))

        self.duck_level = _spin(minimum=0.0, maximum=100.0, value=35.0,
                                step=5.0, suffix=" %", decimals=0)
        self.duck_level.valueChanged.connect(self._on_changed)
        card.add(Row("Music level while speaking", self.duck_level,
                     "Kept gentle by default so the music never disappears."))

        self.duck_attack = _spin(minimum=0.0, maximum=5.0, value=0.25, step=0.05, suffix=" s")
        self.duck_attack.valueChanged.connect(self._on_changed)
        self.duck_release = _spin(minimum=0.0, maximum=10.0, value=0.75, step=0.05, suffix=" s")
        self.duck_release.valueChanged.connect(self._on_changed)
        card.add(Row("Duck in / out", _pair(self.duck_attack, self.duck_release)))

    def _build_status_card(self) -> None:
        card = self.add_card("Check and preview")
        self.check_button = QPushButton("Check audio")
        self.check_button.clicked.connect(self._check_audio)
        self.mix_button = QPushButton("Mix preview")
        mark_primary(self.mix_button)
        self.mix_button.clicked.connect(self._mix_preview)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_job)
        self.cancel_button.setEnabled(False)
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self._open_preview_folder)
        card.add(ButtonRow([self.check_button, self.mix_button,
                            self.cancel_button, self.open_button]))

        self.status_hint = HintLabel("Nothing checked yet.")
        card.add(self.status_hint)

        self.issues_view = QPlainTextEdit()
        self.issues_view.setReadOnly(True)
        self.issues_view.setMaximumHeight(170)
        self.issues_view.setPlaceholderText("Problems found by the audio check appear here.")
        card.add(self.issues_view)

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> None:
        """Reload every control from the project."""
        self._loading = True
        try:
            audio = self.audio
            project = self.project
            enabled = audio is not None
            self.set_controls_enabled(enabled)

            self.sfx_scene.clear()
            self.sfx_scene.addItem("Project timeline", "")
            if project is not None:
                for scene in project.scenes:
                    if getattr(scene, "enabled", True):
                        self.sfx_scene.addItem(scene.name or scene.id, scene.id)

            if audio is None:
                self.narration_enabled.setChecked(True)
                self.narration_volume.setValue(100.0)
                self.music_path.setText("No project is open.")
                self.sfx_table.setRowCount(0)
                return

            self.narration_enabled.setChecked(bool(audio.narration_enabled))
            self.narration_volume.setValue(round(float(audio.narration_volume) * _PERCENT, 0))

            music = audio.music
            self.music_path.setText(music.path or "No music file")
            self.music_volume.setValue(round(float(music.volume) * _PERCENT, 0))
            self.music_loop.setChecked(bool(music.loop))
            self.music_fade_in.setValue(float(music.fade_in))
            self.music_fade_out.setValue(float(music.fade_out))
            self.music_start.setValue(float(music.start))
            self.music_end.setValue(float(music.end))

            self.master_volume.setValue(round(float(audio.master_volume) * _PERCENT, 0))
            self.normalize.setChecked(bool(audio.normalize_enabled))
            self.target_lufs.setValue(float(audio.target_lufs))
            self.duck_enabled.setChecked(bool(audio.ducking_enabled))
            self.duck_level.setValue(round(float(audio.ducking_level) * _PERCENT, 0))
            self.duck_attack.setValue(float(audio.ducking_attack))
            self.duck_release.setValue(float(audio.ducking_release))

            self._reload_sfx_table()
            self._refresh_narration_hint()
        finally:
            self._loading = False

    def set_controls_enabled(self, enabled: bool) -> None:
        """Grey the page out when no project is open, rather than leaving
        controls that look usable but write to nothing."""
        for widget in (self.narration_enabled, self.narration_volume,
                       self.music_browse, self.music_clear, self.music_volume,
                       self.music_loop, self.music_fade_in, self.music_fade_out,
                       self.music_start, self.music_end, self.sfx_add,
                       self.sfx_remove, self.sfx_scene, self.master_volume,
                       self.normalize, self.target_lufs, self.duck_enabled,
                       self.duck_level, self.duck_attack, self.duck_release):
            widget.setEnabled(enabled)

    def _refresh_narration_hint(self) -> None:
        project = self.project
        if project is None:
            self.narration_hint.setText("Narration files are created on the Narration page.")
            return
        ready = sum(1 for scene in project.scenes
                    if str(getattr(scene.narration, "file", "") or ""))
        total = len(project.scenes)
        self.narration_hint.setText(
            f"{ready} of {total} scene(s) have a narration file. Scenes without one "
            f"play in silence." if total else "The project has no scenes yet.")

    def _reload_sfx_table(self) -> None:
        audio = self.audio
        self.sfx_table.setRowCount(0)
        if audio is None:
            return
        for effect in audio.sfx:
            row = self.sfx_table.rowCount()
            self.sfx_table.insertRow(row)
            name = Path(str(effect.path or "")).name or "(no file)"
            anchor = effect.anchor or "project"
            if anchor == "scene" and effect.scene_id:
                anchor = f"scene:{effect.scene_id}"
            cells = [
                QTableWidgetItem(name),
                QTableWidgetItem(f"{round(float(effect.volume) * _PERCENT)}%"),
                QTableWidgetItem(anchor),
                QTableWidgetItem(f"{float(effect.at_seconds):.2f}s"),
                QTableWidgetItem(f"{float(effect.fade_in):.2f}s"),
                QTableWidgetItem(f"{float(effect.fade_out):.2f}s"),
            ]
            for column, cell in enumerate(cells):
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    cell.setData(Qt.UserRole, effect.id)
                    cell.setToolTip(str(effect.path or ""))
                self.sfx_table.setItem(row, column, cell)

    # -- edits ------------------------------------------------------------

    def _on_changed(self, *_args) -> None:
        """Push the controls into the project.  One handler for all of them."""
        if self._loading:
            return
        audio = self.audio
        if audio is None:
            return
        audio.narration_enabled = self.narration_enabled.isChecked()
        audio.narration_volume = float(self.narration_volume.value()) / _PERCENT
        audio.master_volume = float(self.master_volume.value()) / _PERCENT
        audio.normalize_enabled = self.normalize.isChecked()
        audio.target_lufs = float(self.target_lufs.value())
        audio.ducking_enabled = self.duck_enabled.isChecked()
        audio.ducking_level = float(self.duck_level.value()) / _PERCENT
        audio.ducking_attack = float(self.duck_attack.value())
        audio.ducking_release = float(self.duck_release.value())

        music = audio.music
        music.volume = float(self.music_volume.value()) / _PERCENT
        music.loop = self.music_loop.isChecked()
        music.fade_in = float(self.music_fade_in.value())
        music.fade_out = float(self.music_fade_out.value())
        music.start = float(self.music_start.value())
        music.end = float(self.music_end.value())
        self.project_changed.emit()

    def _choose_music(self) -> None:
        if self.audio is None:
            self.status_hint.setText("Open a project first.")
            return
        folder = str(self.project_dir or self.context.paths.data_root)
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose a music file", folder,
            "Audio files (*.mp3 *.wav *.m4a *.aac *.ogg *.flac)")
        if not chosen:
            return
        target = self._import_file(Path(chosen), "assets")
        if target is None:
            return
        self.audio.music.path = target
        self.audio.music.id = self.audio.music.id or new_id("music")
        self.music_path.setText(target)
        self.status_hint.setText(f"Using {Path(target).name}.")
        self.project_changed.emit()

    def _clear_music(self) -> None:
        if self.audio is None:
            return
        self.audio.music.path = ""
        self.music_path.setText("No music file")
        self.status_hint.setText("Music removed. The mix will be voice and effects only.")
        self.project_changed.emit()

    def _import_file(self, source: Path, subdir: str) -> Optional[str]:
        """Copy a chosen file into the project so the project stays portable."""
        root = self.project_dir
        if root is None:
            self.status_hint.setText("Open a project first.")
            return None
        folder = root / subdir
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / source.name
            if source.resolve() != target.resolve():
                target.write_bytes(source.read_bytes())
        except OSError as exc:
            self.status_hint.setText(
                f"The file could not be copied into the project: {exc}")
            return None
        return f"{subdir}/{source.name}"

    def _add_sfx(self) -> None:
        audio = self.audio
        if audio is None:
            self.status_hint.setText("Open a project first.")
            return
        folder = str(self.project_dir or self.context.paths.data_root)
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose a sound effect", folder,
            "Audio files (*.mp3 *.wav *.m4a *.aac *.ogg *.flac)")
        if not chosen:
            return
        stored = self._import_file(Path(chosen), "assets")
        if stored is None:
            return
        scene_id = str(self.sfx_scene.currentData() or "")
        effect = SoundEffect(
            id=new_id("sfx"), path=stored,
            anchor="scene" if scene_id else "project",
            scene_id=scene_id, at_seconds=0.0 if scene_id else 0.5,
            volume=0.6, fade_in=0.02, fade_out=0.15)
        audio.sfx.append(effect)
        self._reload_sfx_table()
        self.status_hint.setText(f"Added {Path(stored).name}.")
        self.project_changed.emit()

    def _remove_sfx(self) -> None:
        audio = self.audio
        row = self.sfx_table.currentRow()
        if audio is None or row < 0:
            self.status_hint.setText("Select a sound effect to remove first.")
            return
        cell = self.sfx_table.item(row, 0)
        effect_id = cell.data(Qt.UserRole) if cell is not None else None
        audio.sfx = [effect for effect in audio.sfx if effect.id != effect_id]
        self._reload_sfx_table()
        self.status_hint.setText("Sound effect removed.")
        self.project_changed.emit()

    # -- jobs -------------------------------------------------------------

    def _payload(self) -> dict:
        return {"project": self.project, "project_dir": str(self.project_dir or "")}

    def _submit(self, spec) -> Optional[object]:
        spec.paths = self.context.paths
        return self.context.jobs.submit(spec)

    def _check_audio(self) -> None:
        if self.project is None or self.project_dir is None:
            self.status_hint.setText("Open a project first.")
            return
        if self._validate_job is not None:
            self.status_hint.setText("An audio check is already running.")
            return
        job = self._submit(audio_validate_spec(self._payload()))
        if job is None:
            self.status_hint.setText("An audio check is already running. "
                                     "Wait for it to finish.")
            return
        self._validate_job = job.id
        self.check_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_hint.setText("Checking the audio tracks…")

    def _mix_preview(self) -> None:
        if self.project is None or self.project_dir is None:
            self.status_hint.setText("Open a project first.")
            return
        if self._mix_job is not None:
            self.status_hint.setText("A mix is already running.")
            return
        output = self.project_dir / "cache" / "previews" / "audio_preview.wav"
        job = self._submit(audio_mix_spec(self._payload(), output=str(output)))
        if job is None:
            self.status_hint.setText("A mix is already running. Wait for it to finish.")
            return
        self._mix_job = job.id
        self.mix_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status_hint.setText("Mixing the audio preview…")

    def _cancel_job(self) -> None:
        job_id = self._mix_job or self._validate_job
        if job_id:
            self.context.jobs.cancel(job_id, "Stopped by the user.")
            self.status_hint.setText("Cancelling…")

    def _open_preview_folder(self) -> None:
        from ..notifications import open_folder  # local import: keeps startup light

        folder = self.project_dir / "cache" / "previews" if self.project_dir else None
        if folder is None or not folder.exists():
            self.status_hint.setText("Mix a preview first, then the folder will exist.")
            return
        open_folder(self, folder)

    # -- job results ------------------------------------------------------

    def on_validate_finished(self, result) -> None:
        self._validate_job = None
        self.check_button.setEnabled(True)
        self._sync_cancel_state()
        if result.cancelled:
            self.status_hint.setText("The audio check was cancelled.")
            return
        if result.error is not None:
            self.status_hint.setText(f"The audio check failed: {result.error.title}")
            return
        value = result.value or {}
        errors = value.get("errors", [])
        warnings = value.get("warnings", [])
        placements = value.get("placements", [])
        if errors:
            self.status_hint.setText(
                f"{len(errors)} problem(s) must be fixed before this will render.")
        elif warnings:
            self.status_hint.setText(f"Audio is usable, with {len(warnings)} warning(s).")
        else:
            self.status_hint.setText("Audio is ready to render.")
        self._show_issues(errors, warnings, placements)

    def on_mix_finished(self, result) -> None:
        self._mix_job = None
        self.mix_button.setEnabled(True)
        self._sync_cancel_state()
        if result.cancelled:
            self.status_hint.setText("The mix was cancelled.")
            return
        if result.error is not None:
            self.status_hint.setText(f"The mix failed: {result.error.title}")
            return
        value = result.value or {}
        if not value.get("ok", False):
            self.status_hint.setText(value.get("message", "The mix could not be built."))
            self._show_issues(value.get("issues", []), [], [])
            return
        self._last_mix_path = str(value.get("path", ""))
        duration = float(value.get("duration", 0.0) or 0.0)
        self.status_hint.setText(
            f"Preview ready: {duration:.2f}s at {self._last_mix_path}")

    def _sync_cancel_state(self) -> None:
        self.cancel_button.setEnabled(bool(self._mix_job or self._validate_job))

    def _show_issues(self, errors, warnings, placements=()) -> None:
        lines: list[str] = []
        for issue in errors:
            lines.append(f"[ERROR] {_issue_text(issue)}")
        for issue in warnings:
            lines.append(f"[WARNING] {_issue_text(issue)}")
        for placement in placements:
            lines.append(
                f"  {placement.get('scene_name') or placement.get('scene_id')}: "
                f"{placement.get('start', 0):.2f}s -> {placement.get('end', 0):.2f}s "
                f"({Path(str(placement.get('path', ''))).name})")
        self.issues_view.setPlainText("\n".join(lines) if lines else "No problems found.")


def _issue_text(issue) -> str:
    if isinstance(issue, dict):
        code = issue.get("code", "")
        message = issue.get("message", "")
        advice = issue.get("what_to_do", "")
        return f"{code}: {message}" + (f"  ({advice})" if advice else "")
    return str(issue)


def _pair(left: QWidget, right: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(left)
    layout.addWidget(right)
    layout.addStretch(1)
    return holder
