"""Undo and redo for project edits (directive section 29, roadmap Stage B).

The stack holds **project snapshots**, never widget state, so undo works the
same whether the change came from the editor, the settings page or the command
line.  Snapshots are the serialised model, which keeps them small, deep and
free of live objects.

The history is bounded (default 50 steps): an all-day editing session cannot
grow memory without limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import Project

DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class HistoryStep:
    """One undoable action: a label plus the state that preceded it."""

    label: str
    snapshot: dict


@dataclass(frozen=True)
class UndoResult:
    project: Project
    label: str


class ProjectHistory:
    """A bounded undo/redo stack for one project."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = max(5, int(limit))
        self._undo: list[HistoryStep] = []
        self._redo: list[HistoryStep] = []

    # -- state -------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_labels(self) -> list[str]:
        return [step.label for step in reversed(self._undo)]

    @property
    def redo_labels(self) -> list[str]:
        return [step.label for step in reversed(self._redo)]

    def __len__(self) -> int:
        return len(self._undo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # -- recording ---------------------------------------------------------

    def record(self, label: str, project: Project) -> None:
        """Remember the state *before* an edit labelled *label*.

        Call this before applying the change, so undo can put it back.  Any new
        edit clears the redo stack, which is what users expect.
        """
        self._undo.append(HistoryStep(label=label or "Edit", snapshot=project.to_dict()))
        if len(self._undo) > self.limit:
            del self._undo[0]
        self._redo.clear()

    # -- travelling --------------------------------------------------------

    def undo(self, project: Project) -> Optional[UndoResult]:
        """Step back one edit.  The current state becomes redoable."""
        if not self._undo:
            return None
        step = self._undo.pop()
        self._redo.append(HistoryStep(label=step.label, snapshot=project.to_dict()))
        return UndoResult(project=Project.from_dict(step.snapshot), label=step.label)

    def redo(self, project: Project) -> Optional[UndoResult]:
        """Step forward one edit."""
        if not self._redo:
            return None
        step = self._redo.pop()
        self._undo.append(HistoryStep(label=step.label, snapshot=project.to_dict()))
        return UndoResult(project=Project.from_dict(step.snapshot), label=step.label)

    # -- diagnostics -------------------------------------------------------

    def describe(self) -> str:
        parts = [f"undo: {len(self._undo)}/{self.limit}", f"redo: {len(self._redo)}"]
        if self._undo:
            parts.append(f"next undo: {self._undo[-1].label}")
        return ", ".join(parts)


__all__ = ["DEFAULT_LIMIT", "HistoryStep", "ProjectHistory", "UndoResult"]
