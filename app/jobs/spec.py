"""Job definitions and results (directive sections 7, 8, 9, 28).

A job is described by plain data so that the same definition can be executed

* in the GUI (Qt thread pool, with signals),
* from the command line (synchronously),
* from tests (with a fake clock and no threads).

The execution function receives a :class:`JobContext` and returns any value.
It must not touch Qt widgets.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.errors import FriendlyError, JobCancelled, to_friendly
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from .cancel import CancelToken
from .progress import Progress, ProgressReporter
from .states import JobState, JobStateMachine

LOGGER = get_logger("jobs")

#: Job body: ``run(context) -> Any``.
JobBody = Callable[["JobContext"], Any]


@dataclass
class JobContext:
    """Everything a job body is allowed to use.

    Providing these explicitly (never through globals) is what makes jobs
    testable and keeps worker threads away from GUI state.
    """

    job_id: str
    key: str
    cancel: CancelToken
    progress: ProgressReporter
    settings: Any = None
    paths: Any = None
    #: Free-form per-job values (project, scene, voice ...) supplied by callers.
    payload: dict = field(default_factory=dict)

    # -- convenience -------------------------------------------------------

    def is_cancelled(self) -> bool:
        return self.cancel.is_cancelled()

    def raise_if_cancelled(self) -> None:
        self.cancel.raise_if_cancelled()

    def log(self, event: str, message: str = "", **fields: Any) -> None:
        log_event(event, message, logger=LOGGER, job=self.key, id=self.job_id, **fields)

    def get(self, name: str, default: Any = None) -> Any:
        return self.payload.get(name, default)


@dataclass
class JobSpec:
    """A request to run one unit of work, exactly once (section 9)."""

    key: str
    title: str
    body: JobBody
    description: str = ""
    cancellable: bool = True
    #: When False, submitting a second job with the same key while one is
    #: running is rejected instead of silently starting a duplicate.
    allow_parallel: bool = False
    #: Set to False for short jobs that must not appear in the job list.
    visible: bool = True
    payload: dict = field(default_factory=dict)
    #: Optional per-job callback invoked on the worker thread with progress.
    settings: Any = None
    paths: Any = None


@dataclass
class JobResult:
    """Outcome of a job: exactly one of ``value`` / ``error`` / cancellation."""

    job_id: str
    key: str
    title: str
    state: JobState
    value: Any = None
    error: Optional[FriendlyError] = None
    started_at: float = 0.0
    finished_at: float = 0.0
    final_progress: Optional[Progress] = None

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.SUCCEEDED

    @property
    def cancelled(self) -> bool:
        return self.state is JobState.CANCELLED

    @property
    def failed(self) -> bool:
        return self.state is JobState.FAILED

    @property
    def duration_seconds(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return max(0.0, self.finished_at - self.started_at)

    def summary(self) -> str:
        if self.succeeded:
            return f"{self.title}: finished in {self.duration_seconds:.1f}s"
        if self.cancelled:
            return f"{self.title}: cancelled"
        title = self.error.title if self.error else "failed"
        return f"{self.title}: {title}"

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "key": self.key,
            "title": self.title,
            "state": self.state.value,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error.as_dict() if self.error else None,
        }


class Job:
    """Live record of one running or finished job."""

    def __init__(self, spec: JobSpec, job_id: Optional[str] = None) -> None:
        self.spec = spec
        self.id = job_id or uuid.uuid4().hex[:12]
        self.machine = JobStateMachine()
        self.cancel = CancelToken(job_id=self.id)
        self.progress = ProgressReporter(unit="items")
        self.result: Optional[JobResult] = None
        self.created_at = time.time()
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        #: True once the completion signal has been delivered (guards duplicates).
        self.completion_delivered = False

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> JobState:
        return self.machine.state

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def is_terminal(self) -> bool:
        return self.machine.is_terminal

    def describe(self) -> str:
        return f"Job #{self.id} '{self.spec.key}' [{self.machine.describe()}]"

    # -- context -----------------------------------------------------------

    def make_context(self) -> JobContext:
        context = JobContext(
            job_id=self.id,
            key=self.spec.key,
            cancel=self.cancel,
            progress=self.progress,
            settings=self.spec.settings,
            paths=self.spec.paths,
            payload=dict(self.spec.payload),
        )
        return context

    def mark_started(self) -> None:
        self.started_at = time.time()
        if self.machine.state is JobState.PENDING:
            self.machine.transition(JobState.STARTING, "worker picked up the job")
        if self.machine.state is JobState.STARTING:
            self.machine.transition(JobState.RUNNING, "work started")

    def finalize(self, value: Any = None, error: Optional[BaseException] = None) -> JobResult:
        """Produce the single result for this job and move it to a terminal state."""
        self.finished_at = time.time()

        if isinstance(error, JobCancelled) or (error is None and self.cancel.is_cancelled()):
            self.machine.try_transition(JobState.CANCELLED, "cancelled")
            friendly = to_friendly(JobCancelled("The operation was cancelled."), self.spec.title)
            state = JobState.CANCELLED
        elif error is not None:
            self.machine.try_transition(JobState.FAILED, "failed")
            friendly = to_friendly(error, self.spec.title)
            state = JobState.FAILED
        else:
            self.machine.try_transition(JobState.SUCCEEDED, "finished")
            friendly = None
            state = JobState.SUCCEEDED

        if self.machine.state.is_terminal:
            state = self.machine.state

        # Only a successful job may expose a value.  A partial result from a
        # cancelled or failed job must never be treated as the finished output
        # (directive sections 18 and 36).
        if state is not JobState.SUCCEEDED:
            value = None

        self.result = JobResult(
            job_id=self.id,
            key=self.spec.key,
            title=self.spec.title,
            state=state,
            value=value,
            error=friendly,
            started_at=self.started_at,
            finished_at=self.finished_at,
            final_progress=self.progress.progress,
        )
        return self.result


def execute_job(job: Job) -> JobResult:
    """Run a job body once, converting any outcome into exactly one result.

    This function is the single place where jobs are executed, so the rules of
    section 9 (one job, one output, one completion event) are enforced in one
    place for both the GUI and the command line.
    """
    context = job.make_context()
    job.mark_started()
    log_event(
        Event.JOB_START,
        job.spec.title,
        logger=LOGGER,
        job=job.spec.key,
        id=job.id,
        description=job.spec.description or None,
    )
    value: Any = None
    error: Optional[BaseException] = None
    try:
        context.raise_if_cancelled()
        value = job.spec.body(context)
        # A body that returns while cancelled is treated as cancelled, not as a
        # success - the user asked to stop, and a partial result must not be
        # reported as complete (sections 18, 36).
        if job.cancel.is_cancelled():
            raise JobCancelled("The operation was cancelled.")
    except JobCancelled as exc:
        error = exc
    except BaseException as exc:  # noqa: BLE001 - a job must never kill the app
        error = exc

    result = job.finalize(value=value, error=error)

    if result.state is JobState.SUCCEEDED:
        log_event(
            Event.JOB_SUCCEEDED,
            job.spec.title,
            logger=LOGGER,
            job=job.spec.key,
            id=job.id,
            seconds=round(result.duration_seconds, 3),
        )
    elif result.state is JobState.CANCELLED:
        log_event(
            Event.JOB_CANCELLED,
            job.spec.title,
            logger=LOGGER,
            job=job.spec.key,
            id=job.id,
            seconds=round(result.duration_seconds, 3),
        )
    else:
        log_event(
            Event.JOB_FAILED,
            job.spec.title,
            level=40,
            logger=LOGGER,
            job=job.spec.key,
            id=job.id,
            code=result.error.error_code if result.error else None,
            reason=result.error.why if result.error else "-",
        )
        if result.error and result.error.technical:
            LOGGER.debug("Job %s technical detail: %s", job.id, result.error.technical)
    return result
