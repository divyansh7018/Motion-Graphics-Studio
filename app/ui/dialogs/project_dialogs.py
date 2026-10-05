"""Dialogs that ask the user to decide, never to guess.

Each of these exists because the directive forbids a silent default:

* unsaved changes   -> Save / Discard / Cancel            (section 29)
* recovery data     -> Restore / Open original / Ignore   (section 13)
* external change   -> Reload / Keep editing / Save as    (section 30)
* missing asset     -> Relink / Replace / Ignore          (section 21)

They are thin: the widgets collect a choice and return it.  All the logic lives
in :mod:`app.project.service` (section 34).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.errors import FriendlyError
from ...project.store import RecoveryCandidate
from ..theme import METRICS
from ..widgets.common import HintLabel


class UnsavedChoice(str, Enum):
    SAVE = "save"
    DISCARD = "discard"
    CANCEL = "cancel"


class RecoveryChoice(str, Enum):
    RESTORE = "restore"
    OPEN_ORIGINAL = "open_original"
    IGNORE = "ignore"


class ConflictChoice(str, Enum):
    RELOAD = "reload"
    KEEP = "keep"
    COMPARE = "compare"
    SAVE_AS = "save_as"
    CANCEL = "cancel"


class MissingAssetChoice(str, Enum):
    RELINK = "relink"
    REPLACE = "replace"
    IGNORE = "ignore"
    CANCEL = "cancel"


# --------------------------------------------------------------------------
# Unsaved changes
# --------------------------------------------------------------------------

class UnsavedChangesDialog(QDialog):
    """Save / Discard / Cancel."""

    def __init__(self, project_name: str, detail: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Unsaved changes")
        self.choice = UnsavedChoice.CANCEL

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.xl, METRICS.lg, METRICS.xl, METRICS.lg)
        layout.setSpacing(METRICS.md)

        title = QLabel(f"“{project_name}” has changes that are not saved.")
        font = title.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        layout.addWidget(title)

        layout.addWidget(HintLabel(detail or "If you close now, those changes are lost."))

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self._finish(UnsavedChoice.SAVE))
        self.discard_button = QPushButton("Discard changes")
        self.discard_button.clicked.connect(lambda: self._finish(UnsavedChoice.DISCARD))
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.discard_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def _finish(self, choice: UnsavedChoice) -> None:
        self.choice = choice
        self.accept()


def ask_unsaved_changes(parent: Optional[QWidget], project_name: str, detail: str = "") -> UnsavedChoice:
    dialog = UnsavedChangesDialog(project_name, detail, parent)
    dialog.exec()
    return dialog.choice


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------

class RecoveryDialog(QDialog):
    """Restore / Open original / Ignore."""

    def __init__(self, candidate: RecoveryCandidate, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recovered project available")
        self.choice = RecoveryChoice.IGNORE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.xl, METRICS.lg, METRICS.xl, METRICS.lg)
        layout.setSpacing(METRICS.md)

        title = QLabel(candidate.headline())
        font = title.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        detail = QLabel(candidate.detail())
        detail.setWordWrap(True)
        layout.addWidget(detail)

        facts = HintLabel(
            f"Project: {candidate.project_name or '(unnamed)'}\n"
            f"Recovery file: {candidate.path}\n"
            f"Size: {candidate.size_bytes} bytes"
        )
        facts.setTextInteractionFlags(facts.textInteractionFlags() | 0x1)  # selectable
        layout.addWidget(facts)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        restore = QPushButton("Restore recovered version")
        restore.setDefault(True)
        restore.clicked.connect(lambda: self._finish(RecoveryChoice.RESTORE))
        original = QPushButton("Open original")
        original.clicked.connect(lambda: self._finish(RecoveryChoice.OPEN_ORIGINAL))
        ignore = QPushButton("Ignore")
        ignore.setToolTip("Keeps the recovery file on disk; it is not offered again.")
        ignore.clicked.connect(lambda: self._finish(RecoveryChoice.IGNORE))
        buttons.addWidget(restore)
        buttons.addWidget(original)
        buttons.addStretch(1)
        buttons.addWidget(ignore)
        layout.addLayout(buttons)

        layout.addWidget(
            HintLabel(
                "Restore keeps a backup of the current project file first, so nothing is lost either way."
            )
        )

    def _finish(self, choice: RecoveryChoice) -> None:
        self.choice = choice
        self.accept()


def ask_recovery(parent: Optional[QWidget], candidate: RecoveryCandidate) -> RecoveryChoice:
    dialog = RecoveryDialog(candidate, parent)
    dialog.exec()
    return dialog.choice


# --------------------------------------------------------------------------
# External change
# --------------------------------------------------------------------------

class CompareDialog(QDialog):
    """What this copy holds, next to what the file on disk holds."""

    def __init__(self, mine: str, theirs: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare with the version on disk")
        self.setMinimumSize(860, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.lg, METRICS.lg, METRICS.lg, METRICS.lg)
        layout.setSpacing(METRICS.sm)

        import difflib

        diff = list(
            difflib.unified_diff(
                theirs.splitlines(), mine.splitlines(), fromfile="on disk", tofile="this copy", lineterm=""
            )
        )
        changed = sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        layout.addWidget(
            HintLabel(
                f"{changed} line(s) differ. Nothing has been written; choose what to do next."
                if changed
                else "The two copies are identical."
            )
        )

        columns = QHBoxLayout()
        columns.setSpacing(METRICS.md)
        for heading, text in (("On disk", theirs), ("This copy (unsaved)", mine)):
            column = QVBoxLayout()
            label = QLabel(heading)
            font = label.font()
            font.setBold(True)
            label.setFont(font)
            column.addWidget(label)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setLineWrapMode(QPlainTextEdit.NoWrap)
            view.setPlainText(text)
            column.addWidget(view, 1)
            wrapper = QWidget()
            wrapper.setLayout(column)
            columns.addWidget(wrapper, 1)
        layout.addLayout(columns, 1)

        close_button = QPushButton("Back to the choice")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignRight)


def show_compare(parent: Optional[QWidget], mine: str, theirs: str) -> None:
    CompareDialog(mine, theirs, parent).exec()


class ExternalChangeDialog(QDialog):
    """Reload / Keep editing / Compare / Save as / Cancel."""

    def __init__(self, friendly: FriendlyError, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("The project changed outside this application")
        self.choice = ConflictChoice.CANCEL

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.xl, METRICS.lg, METRICS.xl, METRICS.lg)
        layout.setSpacing(METRICS.md)

        title = QLabel(friendly.what_happened)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        layout.addWidget(title)

        why = QLabel(friendly.why)
        why.setWordWrap(True)
        layout.addWidget(why)
        layout.addWidget(HintLabel("\n".join(f"• {action}" for action in friendly.actions)))

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        reload_button = QPushButton("Reload from disk")
        reload_button.clicked.connect(lambda: self._finish(ConflictChoice.RELOAD))
        keep_button = QPushButton("Keep editing this copy")
        keep_button.clicked.connect(lambda: self._finish(ConflictChoice.KEEP))
        compare_button = QPushButton("Compare…")
        compare_button.setToolTip("See the two versions side by side before deciding.")
        compare_button.clicked.connect(lambda: self._finish(ConflictChoice.COMPARE))
        save_as_button = QPushButton("Save as…")
        save_as_button.setDefault(True)
        save_as_button.clicked.connect(lambda: self._finish(ConflictChoice.SAVE_AS))
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        for button in (reload_button, keep_button, compare_button, save_as_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def _finish(self, choice: ConflictChoice) -> None:
        self.choice = choice
        self.accept()


def ask_external_change(parent: Optional[QWidget], friendly: FriendlyError) -> ConflictChoice:
    dialog = ExternalChangeDialog(friendly, parent)
    dialog.exec()
    return dialog.choice


# --------------------------------------------------------------------------
# Missing asset
# --------------------------------------------------------------------------

class MissingAssetDialog(QDialog):
    """Relink / Replace / Ignore."""

    def __init__(self, asset_name: str, expected: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Missing asset")
        self.choice = MissingAssetChoice.CANCEL
        self.new_path: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(METRICS.xl, METRICS.lg, METRICS.xl, METRICS.lg)
        layout.setSpacing(METRICS.md)

        title = QLabel(f"“{asset_name}” could not be found.")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        layout.addWidget(QLabel(f"Expected location:\n{expected}"))
        layout.addWidget(
            HintLabel(
                "Relink points the project at the file's new location.\n"
                "Replace uses a different file for the same asset.\n"
                "Ignore keeps the project usable; the asset stays marked as missing."
            )
        )

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        relink = QPushButton("Relink…")
        relink.setDefault(True)
        relink.clicked.connect(lambda: self._pick(MissingAssetChoice.RELINK))
        replace = QPushButton("Replace…")
        replace.clicked.connect(lambda: self._pick(MissingAssetChoice.REPLACE))
        ignore = QPushButton("Ignore")
        ignore.clicked.connect(lambda: self._finish(MissingAssetChoice.IGNORE))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for button in (relink, replace):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(ignore)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _pick(self, choice: MissingAssetChoice) -> None:
        start = str(Path.home())
        path, _selected = QFileDialog.getOpenFileName(self, "Choose the file", start)
        if not path:
            return
        self.new_path = Path(path)
        self._finish(choice)

    def _finish(self, choice: MissingAssetChoice) -> None:
        self.choice = choice
        self.accept()


def ask_missing_asset(parent: Optional[QWidget], asset_name: str, expected: str) -> tuple[MissingAssetChoice, Optional[Path]]:
    dialog = MissingAssetDialog(asset_name, expected, parent)
    dialog.exec()
    return dialog.choice, dialog.new_path


__all__ = [
    "CompareDialog",
    "ConflictChoice",
    "ExternalChangeDialog",
    "MissingAssetChoice",
    "MissingAssetDialog",
    "RecoveryChoice",
    "RecoveryDialog",
    "UnsavedChangesDialog",
    "UnsavedChoice",
    "ask_external_change",
    "ask_missing_asset",
    "ask_recovery",
    "ask_unsaved_changes",
    "show_compare",
]
