"""Readiness check framework (directive sections 46, 64, 65).

The system check answers one question: *"Can this machine run the workflow?"*
It reports per-item status instead of a single pass/fail so the user always
knows what is broken and what is merely optional:

    ✓ Ready      - works now
    ⚠ Warning    - works, but something should be improved
    ⚠ Optional   - not installed; a later stage needs it (e.g. Kokoro)
    ✗ Missing    - required now and absent (e.g. the config folder)
    ✗ Blocked    - a hard blocker (e.g. unwritable data folder)

Checks run in a background job, so nothing here may touch Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional, Sequence


class Status(str, Enum):
    """Readiness of one checked item."""

    READY = "ready"        # ✓
    WARNING = "warning"    # ⚠ something is not ideal
    OPTIONAL = "optional"  # ⚠ not installed, needed by a later stage
    MISSING = "missing"    # ✗ required now, absent
    BLOCKED = "blocked"    # ✗ hard blocker (cannot continue)
    UNKNOWN = "unknown"    # ? check did not run

    @property
    def glyph(self) -> str:
        return {
            Status.READY: "✓",
            Status.WARNING: "⚠",
            Status.OPTIONAL: "⚠",
            Status.MISSING: "✗",
            Status.BLOCKED: "✗",
            Status.UNKNOWN: "?",
        }[self]

    @property
    def label(self) -> str:
        return {
            Status.READY: "Ready",
            Status.WARNING: "Warning",
            Status.OPTIONAL: "Optional",
            Status.MISSING: "Missing",
            Status.BLOCKED: "Blocked",
            Status.UNKNOWN: "Not checked",
        }[self]

    @property
    def is_problem(self) -> bool:
        return self in (Status.MISSING, Status.BLOCKED)

    @property
    def blocks_startup(self) -> bool:
        return self is Status.BLOCKED


@dataclass
class CheckResult:
    """Outcome of one readiness check item."""

    check_id: str
    title: str
    status: Status
    summary: str
    what_happened: str = ""
    why: str = ""
    actions: Sequence[str] = field(default_factory=tuple)
    details: Sequence[str] = field(default_factory=tuple)
    technical: str = ""
    required_for: str = ""      # "Stage A", "Rendering", "Voice" ...
    duration_ms: float = 0.0
    skipped: bool = False

    def to_line(self) -> str:
        return f"{self.status.glyph} {self.title}: {self.summary}"

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.check_id,
            "title": self.title,
            "status": self.status.value,
            "summary": self.summary,
            "required_for": self.required_for,
            "details": list(self.details),
            "actions": list(self.actions),
            "technical": self.technical,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class CheckReport:
    """All results of one system check run."""

    results: list[CheckResult] = field(default_factory=list)
    started_at: float = 0.0
    duration_seconds: float = 0.0
    environment: Optional[object] = None     # EnvironmentInfo, kept loose to avoid a cycle
    error: str = ""

    # -- aggregate ---------------------------------------------------------

    @property
    def ready_count(self) -> int:
        return sum(1 for r in self.results if r.status is Status.READY)

    @property
    def problems(self) -> list[CheckResult]:
        return [r for r in self.results if r.status.is_problem]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status in (Status.WARNING, Status.OPTIONAL)]

    @property
    def blockers(self) -> list[CheckResult]:
        return [r for r in self.results if r.status.blocks_startup]

    @property
    def missing(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is Status.MISSING]

    @property
    def core_ready(self) -> bool:
        """True when nothing that is required *now* is missing."""
        return not self.problems

    @property
    def render_ready(self) -> bool:
        """True when the machine can already produce a finished video."""
        needed = {"media.ffmpeg", "media.ffprobe", "storage.output"}
        return all(
            r.status is Status.READY for r in self.results if r.check_id in needed
        ) and bool(self.results)

    @property
    def voice_ready(self) -> bool:
        return any(r.check_id == "voice.kokoro" and r.status is Status.READY for r in self.results)

    def headline(self) -> str:
        if self.error:
            return self.error
        if self.blockers:
            return f"{len(self.blockers)} blocker(s) must be fixed before the app can work."
        if self.missing:
            return f"{len(self.missing)} required component(s) are missing."
        if self.warnings:
            return f"Everything required is ready. {len(self.warnings)} optional note(s)."
        return "All checks passed - this machine is ready."

    def summary_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        return counts

    def by_id(self, check_id: str) -> Optional[CheckResult]:
        for result in self.results:
            if result.check_id == check_id:
                return result
        return None

    # -- rendering ---------------------------------------------------------

    def to_text(self) -> str:
        """Plain-text report (used by the CLI and the "Copy report" button)."""
        lines = [self.headline(), ""]
        for result in self.results:
            lines.append(result.to_line())
            for detail in result.details:
                lines.append(f"      {detail}")
            if result.status.is_problem or result.status is Status.WARNING:
                if result.why:
                    lines.append(f"      Why: {result.why}")
                for action in result.actions:
                    lines.append(f"      → {action}")
        if self.environment is not None and hasattr(self.environment, "summary_lines"):
            lines.extend(["", "Machine:", *[f"  {line}" for line in self.environment.summary_lines()]])
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "headline": self.headline(),
            "core_ready": self.core_ready,
            "render_ready": self.render_ready,
            "voice_ready": self.voice_ready,
            "counts": self.summary_counts(),
            "duration_seconds": round(self.duration_seconds, 3),
            "results": [r.as_dict() for r in self.results],
            "environment": self.environment.as_dict() if hasattr(self.environment, "as_dict") else None,
        }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

@dataclass
class CheckSpec:
    """A registered check item."""

    check_id: str
    title: str
    required_for: str
    function: Callable[[CheckContext], CheckResult]
    description: str = ""


@dataclass
class CheckContext:
    """Everything a check function may need.  Passed in - no globals."""

    paths: object                        # AppPaths (typed loosely to avoid an import cycle)
    settings: object                     # Settings
    ffmpeg_discovery: object = None      # FFmpegDiscovery
    kokoro_status: object = None         # KokoroStatus
    environment: object = None           # EnvironmentInfo
    deep: bool = False                   # True when the user pressed "Re-check" manually


_REGISTRY: dict[str, CheckSpec] = {}


def register_check(
    check_id: str,
    title: str,
    required_for: str,
    description: str = "",
) -> Callable[[Callable[[CheckContext], CheckResult]], Callable[[CheckContext], CheckResult]]:
    """Decorator registering a check function under a stable id."""

    def decorator(function: Callable[[CheckContext], CheckResult]):
        if check_id in _REGISTRY:
            raise ValueError(f"Duplicate check id: {check_id}")
        _REGISTRY[check_id] = CheckSpec(
            check_id=check_id,
            title=title,
            required_for=required_for,
            function=function,
            description=description,
        )
        return function

    return decorator


def registered_checks(order: Optional[Iterable[str]] = None) -> list[CheckSpec]:
    """Return registered checks, optionally in a preferred display order."""
    if order is None:
        return list(_REGISTRY.values())
    specs: list[CheckSpec] = []
    for check_id in order:
        spec = _REGISTRY.get(check_id)
        if spec is not None:
            specs.append(spec)
    for check_id, spec in _REGISTRY.items():  # anything not listed comes last
        if check_id not in order:
            specs.append(spec)
    return specs


def clear_registry() -> None:
    """Testing helper - forget all registered checks."""
    _REGISTRY.clear()


def run_check(spec_or_id: CheckSpec | str, context: CheckContext) -> CheckResult:
    """Run one check, converting an unexpected exception into a failed result.

    This is the error boundary for the system check: one broken check can never
    stop the others from reporting (section 41).
    """
    import time

    spec = _REGISTRY[spec_or_id] if isinstance(spec_or_id, str) else spec_or_id
    started = time.perf_counter()
    try:
        result = spec.function(context)
    except Exception as exc:  # pragma: no cover - defensive by design
        from ..core.errors import to_friendly

        friendly = to_friendly(exc, context=f"The check '{spec.title}' could not be completed.")
        result = CheckResult(
            check_id=spec.check_id,
            title=spec.title,
            status=Status.WARNING,
            summary="Check could not be completed",
            what_happened=friendly.what_happened,
            why=friendly.why,
            actions=friendly.actions,
            technical=friendly.technical or "",
            required_for=spec.required_for,
        )
    if not isinstance(result, CheckResult):
        result = CheckResult(
            check_id=spec.check_id,
            title=spec.title,
            status=Status.UNKNOWN,
            summary="Check returned no result",
            technical=f"{type(result).__name__}",
            required_for=spec.required_for,
        )
    result.duration_ms = (time.perf_counter() - started) * 1000.0
    return result


def run_system_check(
    context: CheckContext,
    order: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
    only: Optional[Sequence[str]] = None,
) -> CheckReport:
    """Run every registered check and collect a report.

    *progress* receives ``(done, total, title)`` so a job can report real
    progress (section 55) rather than a fake animation.
    """
    import time

    from ..core.events import Event
    from ..core.logging_setup import get_logger, log_event

    logger = get_logger("system_check")
    specs = registered_checks(order)
    if only is not None:
        wanted = set(only)
        specs = [spec for spec in specs if spec.check_id in wanted]

    report = CheckReport(started_at=time.time(), environment=context.environment)
    total = len(specs)
    log_event(Event.SYSTEM_CHECK_START, "System check started", logger=logger, items=total)

    for index, spec in enumerate(specs, start=1):
        if progress is not None:
            progress(index - 1, total, spec.title)
        result = run_check(spec, context)
        report.results.append(result)
        log_event(
            Event.SYSTEM_CHECK_ITEM,
            result.to_line(),
            logger=logger,
            check=result.check_id,
            status=result.status.value,
            ms=round(result.duration_ms, 1),
        )
    if progress is not None:
        progress(total, total, "Finished")

    report.duration_seconds = time.time() - report.started_at
    log_event(
        Event.SYSTEM_CHECK_COMPLETE,
        report.headline(),
        level=40 if report.blockers else 20,
        logger=logger,
        ready=report.ready_count,
        missing=len(report.missing),
        warnings=len(report.warnings),
        seconds=round(report.duration_seconds, 2),
    )
    return report
