"""Qt worker that runs one job on a thread-pool thread.

Thread-safety rules implemented here (directive sections 7, 8, 44, 45):

* the job body never touches a widget - it only calls the cancel token and the
  progress reporter,
* every UI notification travels through a Qt signal, so Qt marshals it onto the
  main thread for us (no ``QMetaObject.invokeMethod`` string juggling, no direct
  widget access from a worker),
* **one job body executes exactly once**: :class:`JobWorker` has no retry logic
  and no loop; the completion signal is emitted in a ``finally`` block so the UI
  can never be left waiting,
* the worker is a plain ``QRunnable`` (not a ``QThread`` subclass and certainly
  not ``multiprocessing``), which is the Windows-safe model: no fork, no
  re-import of ``__main__``, no recursive process startup.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThread, Signal, Slot

from .spec import Job, JobResult, execute_job


class JobSignals(QObject):
    """Signals emitted by a worker (a plain ``QObject`` because ``QRunnable``
    cannot own signals itself)."""

    started = Signal(str)                  # job_id
    progress = Signal(str, object)         # job_id, Progress
    finished = Signal(str, object)         # job_id, JobResult


class JobWorker(QRunnable):
    """Runs a single :class:`Job` on a thread-pool thread."""

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.signals = JobSignals()
        # The manager owns workers for as long as they run; Qt must not delete
        # the runnable while Python still holds a reference to it.
        self.setAutoDelete(False)

    # -- entry point -------------------------------------------------------

    @Slot()
    def run(self) -> None:  # noqa: D102 - QRunnable API
        job = self.job
        result: JobResult
        try:
            # Progress updates are forwarded as signals; the reporter throttles
            # them so a fast loop cannot flood the GUI event queue.
            job.progress.set_callback(self._emit_progress)
            # Name the pool thread after the job so log lines are readable
            # ("tjob-system.check" instead of "tDummy-1").
            try:
                QThread.currentThread().setObjectName(f"job-{job.key}"[:24])
            except Exception:  # pragma: no cover - naming is cosmetic
                pass
            self.signals.started.emit(job.id)
            result = execute_job(job)
        except BaseException as exc:  # noqa: BLE001 - last line of defence
            # execute_job already converts failures into a result; reaching this
            # point means something failed in the plumbing itself. Still report
            # exactly one completion so the UI is never left hanging.
            result = job.finalize(error=exc)
        finally:
            job.progress.set_callback(None)

        try:
            self.signals.finished.emit(job.id, result)
        except RuntimeError:  # pragma: no cover - Qt object already destroyed
            # The application is shutting down; the manager's shutdown() path
            # records the outcome instead.
            pass

    # -- helpers -----------------------------------------------------------

    def _emit_progress(self, progress) -> None:
        """Called on the worker thread by the progress reporter."""
        try:
            self.signals.progress.emit(self.job.id, progress)
        except RuntimeError:  # pragma: no cover - Qt object already destroyed
            pass

    # -- lifecycle ---------------------------------------------------------

    def request_cancel(self, reason: str = "Cancelled by the user.") -> None:
        self.job.cancel.cancel(reason)

    def dispose(self) -> None:
        """Drop the connection between the job and any Qt objects."""
        try:
            self.signals.blockSignals(True)
        except RuntimeError:  # pragma: no cover
            pass
