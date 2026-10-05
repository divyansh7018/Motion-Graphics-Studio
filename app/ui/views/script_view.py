"""The Script page (directive sections 11-19, 44-47).

A beginner's path is: paste a script, pick a language and a voice, preview, then
generate.  Everything else on this page is optional.

Rules this page follows:

* The script text is stored **exactly as typed**.  Nothing here reformats it, and
  ``script.txt`` in the project folder is written from the same text.
* Counts and the duration estimate are clearly labelled as estimates.  The
  measured duration of generated audio is the authoritative number.
* Import never overwrites the editor without asking; export never overwrites a
  file on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from app.script.io import import_script, strip_markdown
from app.script.parser import is_structured, parse, plain_to_structured
from app.script.stats import stats_for_text
from app.tts.narration import status_explanation

from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page


class ScriptPage(Page):
    """Write, import and export the narration script."""

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Script",
            "Write or import the narration script. Your words are stored exactly as typed.",
            parent,
        )
        self.context = context
        self._structured = False
        self._loading = False

        self._build_editor_card()
        self._build_counts_card()
        self._build_narration_card()
        self.refresh()

    # -- access ------------------------------------------------------------

    @property
    def controller(self):
        return self.context.projects

    @property
    def project(self):
        return self.controller.project if self.controller is not None else None

    # -- cards -------------------------------------------------------------

    def _build_editor_card(self) -> None:
        card = self.add_card("Script text")

        mode_row = QHBoxLayout()
        mode_row.setSpacing(METRICS.sm)
        mode_row.addWidget(QLabel("View:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Plain text", "plain")
        self.mode_combo.addItem("Structured scenes", "structured")
        self.mode_combo.setToolTip(
            "Plain text is one continuous narration block. Structured scenes split "
            "the script into [SCENE 01] blocks with narration, on-screen text, "
            "visual direction, music and sound effects."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        self.convert_button = QPushButton("Convert to scenes")
        self.convert_button.setToolTip(
            "Splits the current text into one scene per paragraph. Your original "
            "wording is kept; only the layout changes."
        )
        self.convert_button.clicked.connect(self._convert_to_scenes)
        mode_row.addWidget(self.convert_button)
        card.body().addLayout(mode_row)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "Paste or write your script here.\n\n"
            "For a structured script use:\n"
            "[SCENE 01]\n\nNarration:\nWhat the voice says.\n\n"
            "On Screen:\nWhat appears on screen."
        )
        self.editor.setMinimumHeight(280)
        self.editor.textChanged.connect(self._on_text_changed)
        card.add(self.editor)

        button_row = QHBoxLayout()
        button_row.setSpacing(METRICS.sm)
        self.apply_button = QPushButton("Apply to project")
        mark_primary(self.apply_button)
        self.apply_button.clicked.connect(self._apply)
        self.revert_button = QPushButton("Revert")
        self.revert_button.setToolTip("Discards edits and reloads the saved script.")
        self.revert_button.clicked.connect(self._revert)
        self.import_button = QPushButton("Import…")
        self.import_button.clicked.connect(self._import)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self._export)
        for button in (self.apply_button, self.revert_button, self.import_button, self.export_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        card.body().addLayout(button_row)

        self.status = HintLabel("")
        card.add(self.status)

    def _build_counts_card(self) -> None:
        card = self.add_card("Length")
        card.add(
            HintLabel(
                "The duration below is an estimate from the word count. Once narration "
                "is generated, the measured length of the audio file is what the "
                "project uses."
            )
        )
        self.counts_grid = KeyValueGrid()
        card.add(self.counts_grid)

    def _build_narration_card(self) -> None:
        card = self.add_card("Narration")
        self.narration_grid = KeyValueGrid()
        card.add(self.narration_grid)
        self.narration_hint = HintLabel("")
        card.add(self.narration_hint)

    # -- behaviour ---------------------------------------------------------

    def refresh(self) -> None:
        """Reload everything from the project."""
        self._loading = True
        try:
            project = self.project
            has_project = project is not None
            for widget in (self.editor, self.mode_combo, self.convert_button,
                           self.apply_button, self.revert_button,
                           self.import_button, self.export_button):
                widget.setEnabled(has_project)
            if not has_project:
                self.editor.setPlainText("")
                self.status.setText("No project is open. Create or open one first.")
                self._clear_counts()
                self._clear_narration()
                return

            text = project.script.source_text or ""
            self.editor.setPlainText(text)
            self._structured = is_structured(text)
            self.mode_combo.setCurrentIndex(1 if self._structured else 0)
            self.convert_button.setEnabled(not self._structured)
            self.status.setText("Loaded from the project.")
            self._refresh_counts()
            self._refresh_narration()
        finally:
            self._loading = False

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._refresh_counts()
        self.status.setText("Edited - press “Apply to project” to store it.")

    def _on_mode_changed(self) -> None:
        if self._loading:
            return
        structured = self.mode_combo.currentData() == "structured"
        if structured == self._structured:
            return
        if structured:
            self._convert_to_scenes()
        else:
            self._flatten_to_plain()

    def _convert_to_scenes(self) -> None:
        text = self.editor.toPlainText()
        if not text.strip():
            self.status.setText("There is no text to split into scenes.")
            return
        converted = plain_to_structured(text)
        self._loading = True
        try:
            self._structured = True
            self.mode_combo.setCurrentIndex(1)
            self.editor.setPlainText(converted)
        finally:
            self._loading = False
        self.convert_button.setEnabled(False)
        self.status.setText(
            "Split into scenes. Your wording is unchanged - only the layout was "
            "reorganised. Press “Apply to project” to keep it."
        )

    def _flatten_to_plain(self) -> None:
        """Return to one continuous narration block, keeping every word."""
        parsed = parse(self.editor.toPlainText())
        narration = parsed.narration_text()
        self._loading = True
        try:
            self._structured = False
            self.mode_combo.setCurrentIndex(0)
            self.editor.setPlainText(narration)
        finally:
            self._loading = False
        self.convert_button.setEnabled(True)
        self.status.setText(
            "Flattened to plain narration. On-screen text and visual directions "
            "were removed from this view - apply only if that is what you want."
        )

    def _apply(self) -> None:
        controller = self.controller
        if controller is None or not controller.is_open:
            self.status.setText("No project is open.")
            return
        text = self.editor.toPlainText()
        controller.service.set_script_text(text)
        controller.refresh_dirty()
        self._structured = is_structured(text)
        self.convert_button.setEnabled(not self._structured)
        self.status.setText("Script stored in the project.")
        self._refresh_narration()

    def _revert(self) -> None:
        self.refresh()

    def _import(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import script",
            str(Path.home()),
            "Scripts (*.txt *.md *.markdown *.text *.script);;All files (*)",
        )
        if not path:
            return
        result = import_script(Path(path))
        if not result.ok:
            QMessageBox.warning(
                self,
                "The script could not be imported",
                f"{result.error}\n\nNothing was changed.",
            )
            return
        if self.editor.toPlainText().strip():
            answer = QMessageBox.question(
                self,
                "Replace the current script?",
                "Importing replaces the text in the editor. Your project is not "
                "changed until you press “Apply to project”.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        text = strip_markdown(result.text) if Path(path).suffix.lower() in (".md", ".markdown") else result.text
        self._loading = True
        try:
            self.editor.setPlainText(text)
            self._structured = is_structured(text)
            self.mode_combo.setCurrentIndex(1 if self._structured else 0)
        finally:
            self._loading = False
        notes = "; ".join(result.notes) if result.notes else f"read as {result.encoding}"
        self.status.setText(f"Imported {Path(path).name} ({notes}). Press “Apply to project” to store it.")
        self._refresh_counts()

    def _export(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        controller = self.controller
        if controller is None or not controller.is_open:
            self.status.setText("No project is open.")
            return
        default_name = f"{controller.display_name()}.txt"
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export script",
            str(Path.home() / default_name),
            "Plain text (*.txt);;Markdown (*.md);;Structured script (*.script)",
        )
        if not path:
            return
        target = "md" if selected.startswith("Markdown") or path.lower().endswith(".md") else (
            "script" if path.lower().endswith(".script") else "txt"
        )
        # Export the text in the editor, so what the user sees is what is written.
        text = self.editor.toPlainText()
        if target == "script" and not is_structured(text):
            text = plain_to_structured(text)
        elif target == "md":
            from app.script.io import to_markdown

            text = to_markdown(text, title=controller.display_name())

        from app.script.io import export_script

        result = export_script(Path(path), text, target=target, title=controller.display_name())
        if not result.ok:
            QMessageBox.warning(self, "The script could not be exported", result.error or "Unknown problem.")
            return
        self.status.setText(f"Exported to {result.path} ({result.bytes_written} bytes).")

    # -- panels ------------------------------------------------------------

    def _refresh_counts(self) -> None:
        text = self.editor.toPlainText()
        stats = stats_for_text(text)
        self.counts_grid.clear()
        self.counts_grid.add("Words", f"{stats.words:,}")
        self.counts_grid.add("Characters", f"{stats.characters:,}")
        self.counts_grid.add("Characters (no spaces)", f"{stats.characters_no_spaces:,}")
        self.counts_grid.add("Sentences", f"{stats.sentences:,}")
        self.counts_grid.add("Paragraphs", f"{stats.paragraphs:,}")
        self.counts_grid.add("Estimated spoken length", stats.duration_label())

    def _clear_counts(self) -> None:
        self.counts_grid.clear()
        self.counts_grid.add("Words", "-")
        self.counts_grid.add("Estimated spoken length", "-")

    def _refresh_narration(self) -> None:
        project = self.project
        self.narration_grid.clear()
        if project is None:
            self._clear_narration()
            return
        plan = project.narration
        self.narration_grid.add("Status", plan.status.replace("_", " ").title())
        self.narration_grid.add("Mode", plan.mode_label())
        self.narration_grid.add("Voice", project.voice.voice or "not selected")
        self.narration_grid.add("Language", project.voice.language or "not selected")
        self.narration_grid.add("Generated files", str(len(plan.tracks)))
        if plan.tracks:
            measured = plan.total_duration_seconds()
            self.narration_grid.add("Audio length (measured)", f"{measured:.2f}s")
        self.narration_hint.setText(status_explanation(project))

    def _clear_narration(self) -> None:
        self.narration_grid.clear()
        self.narration_grid.add("Status", "-")
        self.narration_hint.setText("No project is open.")
