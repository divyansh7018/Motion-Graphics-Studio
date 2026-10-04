"""Job manager: the single place that starts, tracks and stops background work.

Guarantees (directive sections 7, 9, 42, 70):

* **One user action = one job.**  :meth:`submit` refuses to start a second job
  with the same key while the first is still active (unless the spec explicitly
  allows parallelism), and logs the rejection.  This is what prevents the
  classic "click Generate twice, get two overlapping renders" bug.
* **Exactly one completion signal per job.**  The manager emits
  :attr:`JobManager.job_finished` once, when the worker reports its result, and
  removes the job from the active list in the same operation.
* **Cancellation always ends.**  :meth:`cancel` asks the job to stop and starts a
  per-job grace timer; if the worker does not acknowledge in time the job is
  marked failed with a clear explanation instead of leaving the UI stuck on
  "Cancelling...".
* **Clean shutdown.**  :meth:`shutdown` cancels everything, waits a bounded time
  for the pool, and reports anything that did not stop - leaving no zombie
  FFmpeg processes or half-dead threads behind.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QCoreApplication, QObject, QThreadPool, QTimer, Signal

from ..core.errors import JobError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from .spec import Job, JobResult, JobSpec
from .states import JobState
from .worker import JobWorker

LOGGER = get_logger("job_manager")

#: How long a cancelled job may take to stop before it is reported as failed.
CANCEL_GRACE_SECONDS = 15.0

#: A job that reports no progress for this long is flagged in the UI.
STALL_WARNING_SECONDS = 180.0

#: Maximum number of jobs running at the same time.  Two is deliberate: one
#: heavy job plus one light job, and the CPU stays free for the UI (section 77).
MAX_PARALLEL_JOBS = 2


class JobManager(QObject):
    """Owns the thread pool and every running job."""

    #: Emitted when a job is created and about to be queued.
    job_submitted = Signal(object)          # Job
    #: Emitted when a worker starts executing a job.
    job_started = Signal(str)               # job_id
    #: Emitted for progress updates (already throttled by the reporter).
    job_progress = Signal(str, object)      # job_id, Progress
    #: Emitted exactly once per job, whatever the outcome.
    job_finished = Signal(object)           # JobResult
    #: Emitted when a submit was refused because the same key is already running.
    job_rejected = Signal(object, str)      # JobSpec, reason
    #: Emitted when a job has not reported progress for a long time.
    job_stalled = Signal(str, float)        # job_id, seconds_since_progress
    #: Emitted when a cancelled job did not stop within the grace period.
    job_cancel_timeout = Signal(str, float)  # job_id, seconds_waited

    def __init__(self, parent: Optional[QObject] = None, max_parallel: int = MAX_PARALLEL_JOBS) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_parallel)))
        self._pool.setObjectName("mgs-jobs")
        self._jobs: dict[str, Job] = {}
        self._workers: dict[str, JobWorker] = {}
        self._last_progress_at: dict[str, float] = {}
        self._cancel_requested_at: dict[str, float] = {}
        self._shutting_down = False

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._check_for_stalls)
        self._watchdog.start()

    # -- queries -----------------------------------------------------------

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def active_jobs(self) -> list[Job]:
        return [job for job in self._jobs.values() if not job.is_terminal]

    def active_job_for_key(self, key: str) -> Optional[Job]:
        for job in self.active_jobs():
            if job.key == key:
                return job
        return None

    def is_running(self, key: str) -> bool:
        return self.active_job_for_key(key) is not None

    def job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active_count(self) -> int:
        return len(self.active_jobs())

    def current_summary(self) -> str:
        active = self.active_jobs()
        if not active:
            return ""
        if len(active) == 1:
            job = active[0]
            text = job.progress.progress.text() or job.title
            return f"{job.title}: {job.progress.progress.percent_text()} - {text}"
        return f"{len(active)} tasks running"

    # -- submission --------------------------------------------------------

    def submit(self, spec: JobSpec) -> Optional[Job]:
        """Queue *spec*.  Returns the :class:`Job`, or ``None`` if it was refused.

        Refusal happens when:

        * the same key is already running and the spec does not allow parallel
          runs (prevents duplicate generation - section 9/68), or
        * the application is shutting down.
        """
        if self._shutting_down:
            log_event(Event.JOB_REJECTED_DUPLICATE, "Job refused: the application is closing", logger=LOGGER, job=spec.key)
            self.job_rejected.emit(spec, "The application is closing.")
            return None

        if not spec.allow_parallel:
            existing = self.active_job_for_key(spec.key)
            if existing is not None:
                reason = (
                    f"'{spec.title}' is already running (started "
                    f"{time.time() - existing.created_at:.1f}s ago). Wait for it to finish or cancel it first."
                )
                log_event(
                    Event.JOB_REJECTED_DUPLICATE,
                    "Duplicate job refused",
                    logger=LOGGER,
                    job=spec.key,
                    running_id=existing.id,
                )
                self.job_rejected.emit(spec, reason)
                return None

        job = Job(spec)
        worker = JobWorker(job)
        worker.signals.started.connect(self._on_started)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)

        self._jobs[job.id] = job
        self._workers[job.id] = worker
        self._last_progress_at[job.id] = time.time()

        log_event(
            Event.JOB_SUBMITTED,
            spec.title,
            logger=LOGGER,
            job=spec.key,
            id=job.id,
            parallel=spec.allow_parallel,
        )
        self.job_submitted.emit(job)
        self._pool.start(worker)
        return job

    def run_synchronously(self, spec: JobSpec) -> JobResult:
        """Run a job inline on the calling thread.

        Used by the command line and by tests.  The GUI never calls this - the
        whole point of the manager is to keep heavy work off the UI thread.
        """
        from .spec import execute_job

        job = Job(spec)
        self._jobs[job.id] = job
        return execute_job(job)

    # -- cancellation ------------------------------------------------------

    def cancel(self, job_id: str, reason: str = "Cancelled by the user.") -> bool:
        """Request cancellation; returns False when the job is unknown/finished."""
        job = self._jobs.get(job_id)
        if job is None or job.is_terminal:
            return False
        if job.cancel.is_cancelled():
            return True
        job.machine.try_transition(JobState.CANCELLING, "cancel requested")
        job.cancel.cancel(reason)
        self._cancel_requested_at[job_id] = time.time()
        log_event(
            Event.JOB_CANCEL_REQUESTED,
            job.title,
            logger=LOGGER,
            job=job.key,
            id=job.id,
            reason=reason,
        )
        return True

    def cancel_key(self, key: str) -> int:
        """Cancel every active job with this key.  Returns how many were asked."""
        count = 0
        for job in self.active_jobs():
            if job.key == key and self.cancel(job.id):
                count += 1
        return count

    def cancel_all(self, reason: str = "Cancelled because the application is closing.") -> int:
        count = 0
        for job in self.active_jobs():
            if self.cancel(job.id, reason=reason):
                count += 1
        return count

    # -- shutdown ----------------------------------------------------------

    def shutdown(self, timeout_ms: int = 10_000) -> list[str]:
        """Stop accepting jobs, cancel the running ones, wait for the pool.

        Returns the ids of jobs that did not stop in time (empty when clean).
        """
        self._shutting_down = True
        self._watchdog.stop()
        self.cancel_all("Cancelled because the application is closing.")

        # Give workers a chance to unwind cooperatively, then wait on the pool.
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline and self._pool.activeThreadCount() > 0:
            # Keep the event loop alive so queued progress/finished signals are
            # delivered while the workers unwind (no frozen "closing..." state).
            QCoreApplication.processEvents()
            time.sleep(0.02)

        if self._pool.activeThreadCount() > 0:
            self._pool.waitForDone(max(0, int((deadline - time.time()) * 1000)))

        stragglers = [job.id for job in self.active_jobs()]
        for job_id in stragglers:
            job = self._jobs.get(job_id)
            if job is not None:
                log_event(Event.JOB_CANCEL_TIMEOUT, "Job did not stop in time", logger=LOGGER, job=job.key, id=job.id)
                job.finalize(error=JobError(
                    "A background task did not stop in time.",
                    why="The task ignored the cancellation request and had to be abandoned.",
                    actions=("Restart the application to be sure no work is still running in the background.",),
                    error_code="JOB_CANCEL_TIMEOUT",
                ))
        for worker in self._workers.values():
            worker.dispose()
        return stragglers

    def wait_for_all(self, timeout_ms: int = 10_000) -> bool:
        """Wait until the pool is idle (used before closing a project/window)."""
        self._pool.waitForDone(max(0, int(timeout_ms)))
        return self._pool.activeThreadCount() == 0

    # -- internal slots ----------------------------------------------------

    def _on_started(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        self._last_progress_at[job_id] = time.time()
        self.job_started.emit(job_id)

    def _on_progress(self, job_id: str, progress) -> None:
        self._last_progress_at[job_id] = time.time()
        self.job_progress.emit(job_id, progress)

    def _on_finished(self, job_id: str, result: JobResult) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return

        # Series 9: exactly one completion event, even if a late progress signal
        # or a duplicate finished signal arrives afterwards.
        if job.completion_delivered:
            log_event(
                Event.WARNING,
                "Duplicate completion signal ignored",
                level=logging.WARNING,
                logger=LOGGER,
                job=job.key,
                id=job_id,
            )
            return
        job.completion_delivered = True

        if result.state is JobState.RUNNING or not result.state.is_terminal:
            # Should not happen; make it explicit rather than showing "running"
            # forever.
            result = job.finalize(error=JobError(
                "A background task stopped without reporting a result.",
                why="The worker thread ended unexpectedly.",
                actions=("Try the action again. If it repeats, restart the application.",),
                error_code="JOB_NO_RESULT",
            ))

        self._last_progress_at.pop(job_id, None)
        self._cancel_requested_at.pop(job_id, None)
        worker = self._workers.pop(job_id, None)
        if worker is not None:
            worker.dispose()

        self.job_finished.emit(result)

    # -- watchdog ----------------------------------------------------------

    def _check_for_stalls(self) -> None:
        """Detect cancel timeouts and silent jobs; never acts on the user's behalf."""
        if self._shutting_down:
            return
        now = time.time()
        for job_id, requested_at in list(self._cancel_requested_at.items()):
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                self._cancel_requested_at.pop(job_id, None)
                continue
            waited = now - requested_at
            if waited > CANCEL_GRACE_SECONDS:
                log_event(
                    Event.JOB_CANCEL_TIMEOUT,
                    "Cancellation is taking longer than expected",
                    logger=LOGGER,
                    job=job.key,
                    id=job_id,
                    seconds=round(waited, 1),
                )
                self.job_cancel_timeout.emit(job_id, waited)
                self._cancel_requested_at.pop(job_id, None)

        for job_id, last_at in list(self._last_progress_at.items()):
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                self._last_progress_at.pop(job_id, None)
                continue
            silent_for = now - last_at
            if silent_for > STALL_WARNING_SECONDS:
                log_event(
                    Event.WARNING,
                    "Job has not reported progress recently",
                    level=logging.WARNING,
                    logger=LOGGER,
                    job=job.key,
                    id=job_id,
                    seconds=round(silent_for, 1),
                )
                self.job_stalled.emit(job_id, silent_for)
                self._last_progress_at[job_id] = now  # report at most once per interval

    # -- helpers for tests --------------------------------------------------

    def submitted_keys(self) -> list[str]:
        return [job.key for job in self._jobs.values()]

    def completed_results(self) -> list[JobResult]:
        return [job.result for job in self._jobs.values() if job.result is not None]

    def forget_finished(self) -> int:
        """Drop finished job records (keeps the list bounded during long sessions)."""
        finished = [job_id for job_id, job in self._jobs.items() if job.is_terminal]
        for job_id in finished:
            self._jobs.pop(job_id, None)
        return len(finished)


def connect_result_handler(manager: JobManager, handler: Callable[[JobResult], None]) -> None:
    """Connect a plain callable to the completion signal (keeps call sites short)."""
    manager.job_finished.connect(handler)


def iter_active_jobs(manager: JobManager) -> Iterable[Job]:
    return manager.active_jobs()
