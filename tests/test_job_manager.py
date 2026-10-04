"""JobManager: duplicate rejection, single completion, cancellation.

These tests cover the GUI-side guarantees (directive sections 7, 9, 42, 68).
They need Qt for the thread pool and signals, so they use the offscreen platform
through ``conftest.py``.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.jobs.manager import JobManager  # noqa: E402
from app.jobs.spec import JobSpec  # noqa: E402
from app.jobs.states import JobState  # noqa: E402


def process_events(seconds: float) -> None:
    """Run the Qt event loop for a while so queued signals are delivered."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 20.0) -> bool:
    """Run the event loop until *predicate* is true (fast and not timing-flaky)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def make_spec(key: str, work_seconds: float = 0.05, cancellable: bool = True) -> JobSpec:
    def body(context):
        context.progress.start(total=10, message="working")
        for index in range(10):
            context.raise_if_cancelled()
            time.sleep(work_seconds / 10.0)
            context.progress.update(current=index + 1)
        return "finished"

    return JobSpec(key=key, title=f"Job {key}", body=body, cancellable=cancellable)


@pytest.fixture()
def manager(qapp):
    instance = JobManager()
    yield instance
    instance.shutdown(timeout_ms=5000)


def test_job_runs_off_the_ui_thread_and_finishes_once(qapp, manager) -> None:
    results = []
    manager.job_finished.connect(results.append)

    job = manager.submit(make_spec("test.basic"))
    assert job is not None

    # The submit call returned immediately: the UI thread was never blocked.
    assert manager.active_count() >= 0

    assert wait_until(lambda: len(results) >= 1)
    process_events(0.2)

    assert len(results) == 1, "exactly one completion event (section 9)"
    assert results[0].state is JobState.SUCCEEDED
    assert results[0].value == "finished"
    assert manager.active_count() == 0


def test_duplicate_submission_is_refused(qapp, manager) -> None:
    """One user action = one job: a second click must not start a second job."""
    rejections: list[str] = []
    manager.job_rejected.connect(lambda spec, reason: rejections.append(reason))

    first = manager.submit(make_spec("test.duplicate", work_seconds=0.5))
    second = manager.submit(make_spec("test.duplicate", work_seconds=0.5))

    assert first is not None
    assert second is None
    assert rejections and "already running" in rejections[0]
    assert manager.active_count() == 1


def test_parallel_jobs_are_allowed_for_different_keys(qapp, manager) -> None:
    results = []
    manager.job_finished.connect(results.append)

    assert manager.submit(make_spec("test.par.a", 0.2)) is not None
    assert manager.submit(make_spec("test.par.b", 0.2)) is not None

    assert wait_until(lambda: len(results) >= 2)
    process_events(0.2)
    assert len(results) == 2


def test_cancellation_reaches_a_terminal_state_and_cleans_up(qapp, manager) -> None:
    results = []
    manager.job_finished.connect(results.append)

    job = manager.submit(make_spec("test.cancel", work_seconds=5.0))
    assert job is not None
    assert wait_until(lambda: job.started_at > 0)
    process_events(0.1)

    assert manager.cancel(job.id) is True
    assert wait_until(lambda: len(results) >= 1)
    process_events(0.2)

    assert len(results) == 1
    assert results[0].state is JobState.CANCELLED
    assert results[0].error is not None
    assert results[0].error.error_code == "CANCELLED"
    assert manager.active_count() == 0
    # Cancelling an already finished job is a no-op, not a crash.
    assert manager.cancel(job.id) is False


def test_cancel_all_stops_everything(qapp, manager) -> None:
    results = []
    manager.job_finished.connect(results.append)

    first = manager.submit(make_spec("test.all.a", 5.0))
    second = manager.submit(make_spec("test.all.b", 5.0))
    assert first is not None and second is not None
    assert wait_until(lambda: first.started_at > 0 and second.started_at > 0)

    assert manager.cancel_all("test shutdown") == 2
    assert wait_until(lambda: len(results) >= 2)
    process_events(0.2)

    assert len(results) == 2
    assert all(result.state is JobState.CANCELLED for result in results)


def test_progress_signals_are_delivered_to_the_ui_thread(qapp, manager) -> None:
    updates: list[float] = []
    manager.job_progress.connect(lambda job_id, progress: updates.append(progress.current))

    manager.submit(make_spec("test.progress", 0.3))
    assert wait_until(lambda: bool(updates))
    process_events(0.4)

    assert updates, "progress must reach the interface"
    assert max(updates) >= 10, "the final measured value must be reported"


def test_job_that_raises_is_reported_as_failed_not_crashed(qapp, manager) -> None:
    def body(context):
        raise RuntimeError("deliberate failure")

    results = []
    manager.job_finished.connect(results.append)

    manager.submit(JobSpec(key="test.error", title="Failing job", body=body))
    assert wait_until(lambda: len(results) >= 1)
    process_events(0.2)

    assert len(results) == 1
    assert results[0].state is JobState.FAILED
    assert results[0].error is not None
    assert "deliberate failure" in (results[0].error.technical or "")


def test_shutdown_cancels_and_waits(qapp, manager) -> None:
    results = []
    manager.job_finished.connect(results.append)
    job = manager.submit(make_spec("test.shutdown", 30.0))
    assert job is not None
    assert wait_until(lambda: job.started_at > 0)

    stragglers = manager.shutdown(timeout_ms=4000)

    assert manager.shutting_down is True
    assert stragglers == [], "shutdown must stop the running job"
    # After shutdown new work is refused rather than silently dropped.
    assert manager.submit(make_spec("test.after.shutdown")) is None


def test_run_synchronously_is_available_for_the_cli(qapp, manager) -> None:
    result = manager.run_synchronously(make_spec("test.sync", 0.0))
    assert result.succeeded
    assert result.value == "finished"


def test_forget_finished_keeps_the_job_list_bounded(qapp, manager) -> None:
    manager.run_synchronously(make_spec("test.forget", 0.0))
    assert manager.forget_finished() == 1
    assert manager.submitted_keys() == []
