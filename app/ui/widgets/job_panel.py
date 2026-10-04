"""Progress panel for background jobs (directive sections 7, 42, 55, 71).

Shows, for the current job:

* the operation name,
* a **real** progress bar (indeterminate while the total is unknown),
* elapsed time and an honest remaining-time estimate,
* a Cancel button that is only enabled while cancelling is possible,
* the last few finished jobs, so "did it run?" is always answerable.

The panel never invents progress and never hides a job: every job the manager
starts appears here, and disappears only when it reaches a terminal state.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...jobs.progress import Progress, describe_eta, format_duration
from ...jobs.states import JobState
from ..theme import METRICS, mark_danger


class JobProgressWidget(QWidget):
    """Compact progress display for one job."""

    cancel_requested = Signal(str)   # job_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._job_id: str = ""
        self._state: JobState = JobState.PENDING
        self._last_progress: Optional[Progress] = None
        self._started_at: Optional[float] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.sm)

        self.title_label = QLabel("")
        self.title_label.setMinimumWidth(140)
        layout.addWidget(self.title_label, 0)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMinimumWidth(180)
        self.progress_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.progress_bar, 1)

        self.time_label = QLabel("")
        self.time_label.setObjectName("Hint")
        self.time_label.setMinimumWidth(190)
        layout.addWidget(self.time_label, 0)

        self.cancel_button = QPushButton("Cancel")
        mark_danger(self.cancel_button)
        self.cancel_button.setMinimumWidth(80)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self.cancel_button, 0)

        self._ticker = QTimer(self)
        self._ticker.setInterval(500)
        self._ticker.timeout.connect(self._refresh_time_labels)
        self._ticker.start()

        self.setVisible(False)

    # -- lifecycle ---------------------------------------------------------

    def bind_job(self, job) -> None:
        """Attach to a job from the manager."""
        self._job_id = job.id
        self._state = job.state
        self._started_at = job.started_at or None
        self.title_label.setText(job.title)
        self._last_progress = None
        self.progress_bar.setRange(0, 0)  # busy until real progress arrives
        self.progress_bar.setFormat("starting...")
        self.cancel_button.setEnabled(job.spec.cancellable)
        self.setVisible(True)
        self._refresh_time_labels()

    def update_progress(self, progress: Progress) -> None:
        self._last_progress = progress
        percent = progress.percent
        if percent is None:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(self._busy_text(progress))
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            detail = progress.text()
            self.progress_bar.setFormat(f"{percent}%  {detail}" if detail else f"{percent}%")
        self._refresh_time_labels()

    def set_state(self, state: JobState, message: str = "") -> None:
        self._state = state
        if state is JobState.CANCELLING:
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
            self.progress_bar.setFormat("cancelling...")
        elif state.is_terminal:
            self.cancel_button.setEnabled(False)

    def finish(self, result) -> None:
        self._state = result.state
        self._job_id = ""
        self.setVisible(False)
        self.cancel_button.setText("Cancel")

    # -- internals ---------------------------------------------------------

    def _busy_text(self, progress: Progress) -> str:
        text = progress.text()
        return text or "working..."

    def _refresh_time_labels(self) -> None:
        if self._started_at is None or self._state.is_terminal:
            self.time_label.setText("")
            return
        elapsed = max(0.0, time.time() - self._started_at)
        parts = [f"Elapsed {format_duration(elapsed)}"]
        if self._last_progress is not None:
            parts.append(describe_eta(self._last_progress))
        self.time_label.setText("  -  ".join(parts))

    def _on_cancel_clicked(self) -> None:
        if self._job_id:
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
            self.cancel_requested.emit(self._job_id)


class JobListWidget(QWidget):
    """Recent job history: one line per finished job."""

    def __init__(self, max_entries: int = 6, theme: str = "dark", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._max_entries = max(1, int(max_entries))
        self._entries: deque[QWidget] = deque(maxlen=self._max_entries)
        self.theme = theme
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._empty = QLabel("No tasks have run yet in this session.")
        self._empty.setObjectName("Hint")
        self._layout.addWidget(self._empty)

    def add_result(self, result) -> None:
        from ..theme import scheme_for

        scheme = scheme_for(self.theme)
        colour = {
            JobState.SUCCEEDED: scheme.ok,
            JobState.FAILED: scheme.bad,
            JobState.CANCELLED: scheme.warn,
        }.get(result.state, scheme.text_muted)

        glyph = {JobState.SUCCEEDED: "✓", JobState.FAILED: "✗", JobState.CANCELLED: "•"}.get(result.state, "•")
        text = f"{glyph}  {result.title} - {result.state.label}"
        if result.duration_seconds:
            text += f" ({format_duration(result.duration_seconds)})"
        if result.failed and result.error is not None:
            text += f" - {result.error.what_happened.splitlines()[0]}"

        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {colour};")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if result.error is not None:
            label.setToolTip(result.error.to_message())

        self._empty.setVisible(False)
        self._layout.insertWidget(0, label)
        if len(self._entries) >= self._max_entries:
            oldest = self._entries.popleft()
            self._layout.removeWidget(oldest)
            oldest.deleteLater()
        self._entries.append(label)

    def set_theme(self, theme: str) -> None:
        self.theme = theme

    def clear(self) -> None:
        for entry in list(self._entries):
            # Take the label out of the layout first: deleting a widget that is
            # still in a layout leaves an empty row until Qt processes the
            # deletion, which looks like a stuck panel.
            self._layout.removeWidget(entry)
            entry.deleteLater()
        self._entries.clear()
        self._empty.setVisible(True)
