"""Real, measurable progress reporting (directive sections 7, 55, 71).

Progress in this application is always derived from *actual work*:

* ``current`` / ``total`` counts of real units (scenes, frames, files), or
* a byte count for file operations.

There is no timer that animates a bar from 0% to 100%.  When the total number
of units is not known yet, the job reports an "indeterminate" progress with a
truthful message ("Reading file...") so the UI shows a busy indicator instead
of a fake percentage.

Estimated time remaining is computed from the observed rate, and is only shown
once enough work has been measured for the estimate to mean anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Progress:
    """A progress snapshot for one job."""

    current: float = 0.0
    total: float = 0.0
    message: str = ""
    unit: str = "items"
    started_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    #: Set when a phase of work finished; used for phase-aware display.
    phase: str = ""

    # -- derived -----------------------------------------------------------

    @property
    def is_indeterminate(self) -> bool:
        return self.total <= 0

    @property
    def fraction(self) -> Optional[float]:
        if self.is_indeterminate:
            return None
        return max(0.0, min(1.0, self.current / self.total))

    @property
    def percent(self) -> Optional[int]:
        fraction = self.fraction
        return None if fraction is None else int(round(fraction * 100))

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.updated_at - self.started_at)

    @property
    def rate(self) -> Optional[float]:
        """Units per second, or ``None`` when it cannot be measured yet."""
        elapsed = self.elapsed_seconds
        if elapsed < 0.25 or self.current <= 0 or self.is_indeterminate:
            return None
        return self.current / elapsed

    @property
    def eta_seconds(self) -> Optional[float]:
        """Remaining seconds, only once the measurement is meaningful."""
        rate = self.rate
        if rate is None or rate <= 0:
            return None
        remaining = max(0.0, self.total - self.current)
        if self.current < 1 or self.elapsed_seconds < 1.0:
            return None  # too early for an honest estimate
        return remaining / rate

    # -- display -----------------------------------------------------------

    def text(self) -> str:
        parts: list[str] = []
        if self.phase:
            parts.append(self.phase)
        if self.message:
            parts.append(self.message)
        if not self.is_indeterminate:
            parts.append(f"{int(self.current)}/{int(self.total)} {self.unit}")
        return " - ".join(part for part in parts if part)

    def percent_text(self) -> str:
        percent = self.percent
        if percent is None:
            return "working..."
        return f"{percent}%"

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "phase": self.phase,
            "unit": self.unit,
            "percent": self.percent,
            "indeterminate": self.is_indeterminate,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "eta_seconds": None if self.eta_seconds is None else round(self.eta_seconds, 1),
        }


def format_duration(seconds: Optional[float]) -> str:
    """Format a duration for the UI: ``1h 02m``, ``3m 12s``, ``8.4s``."""
    if seconds is None:
        return "-"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def describe_eta(progress: Progress) -> str:
    """Human readable "about 2m 30s remaining", or an honest alternative."""
    eta = progress.eta_seconds
    if eta is None:
        return "Estimating..."
    if eta < 1:
        return "Almost done"
    return f"About {format_duration(eta)} remaining"


class ProgressReporter:
    """Helper used by job bodies to report progress safely.

    * updates are throttled (the UI does not need 2000 updates per second),
    * every call is a single immutable snapshot handed to a callback,
    * the reporter can be replaced by the worker with one that queues updates
      onto the Qt thread - the job body never needs to know which it is.
    """

    def __init__(self, callback=None, throttle_seconds: float = 0.08, unit: str = "items") -> None:
        self._callback = callback
        self._throttle = max(0.0, throttle_seconds)
        self._last_emit = 0.0
        self.unit = unit
        self.progress = Progress(unit=unit)
        self._force_next = True

    # -- configuration -----------------------------------------------------

    def set_callback(self, callback) -> None:
        self._callback = callback

    def start(self, total: float = 0.0, message: str = "", phase: str = "", unit: Optional[str] = None) -> None:
        """Begin a new phase of work."""
        now = time.monotonic()
        self.progress = Progress(
            current=0.0,
            total=float(total or 0),
            message=message,
            unit=unit or self.unit,
            started_at=now,
            updated_at=now,
            phase=phase,
        )
        self._force_next = True
        self.emit(force=True)

    def set_total(self, total: float, message: Optional[str] = None) -> None:
        self.progress.total = float(total)
        if message is not None:
            self.progress.message = message
        self.progress.updated_at = time.monotonic()
        self.emit(force=True)

    def set_message(self, message: str, phase: Optional[str] = None) -> None:
        self.progress.message = message
        if phase is not None:
            self.progress.phase = phase
        self.progress.updated_at = time.monotonic()
        self.emit()

    def update(self, current: Optional[float] = None, message: Optional[str] = None, advance: float = 0.0) -> None:
        """Advance progress by *advance* units, or set it to *current*."""
        if current is not None:
            self.progress.current = float(current)
        if advance:
            self.progress.current += float(advance)
        if message is not None:
            self.progress.message = message
        self.progress.updated_at = time.monotonic()
        self.emit()

    def advance(self, amount: float = 1.0, message: Optional[str] = None) -> None:
        self.update(advance=amount, message=message)

    def finish(self, message: str = "Finished") -> None:
        if not self.progress.is_indeterminate:
            self.progress.current = self.progress.total
        self.progress.message = message
        self.progress.updated_at = time.monotonic()
        self.emit(force=True)

    # -- emission ----------------------------------------------------------

    def emit(self, force: bool = False) -> None:
        """Hand a snapshot to the listener.

        Updates are throttled so a fast loop cannot flood the UI queue, but a
        *completed* step (``current == total``) and explicit ``force`` calls are
        always delivered - the user must not see a bar stuck just below 100%.
        """
        if self._callback is None:
            return
        now = time.monotonic()
        finished_step = (not self.progress.is_indeterminate) and (self.progress.current >= self.progress.total)
        if not force and not finished_step and (now - self._last_emit) < self._throttle:
            return
        self._last_emit = now
        self._force_next = False
        snapshot = Progress(
            current=self.progress.current,
            total=self.progress.total,
            message=self.progress.message,
            unit=self.progress.unit,
            started_at=self.progress.started_at,
            updated_at=self.progress.updated_at,
            phase=self.progress.phase,
        )
        self._callback(snapshot)


def stages_reporter(callback, stage_names: list[str], unit: str = "steps") -> ProgressReporter:
    """Progress reporter for a fixed list of named stages."""
    reporter = ProgressReporter(callback=callback, unit=unit)
    reporter.start(total=len(stage_names), message=stage_names[0] if stage_names else "")
    return reporter
