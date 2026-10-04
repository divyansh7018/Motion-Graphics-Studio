"""Job core: state machine, cancellation, progress and single execution.

These tests encode the hardest requirements of the directive:

* section 9 - one action runs one job body exactly once and produces exactly
  one result,
* section 18 - every job reaches SUCCESS, FAILED or CANCELLED,
* section 42 - cancellation always ends,
* section 55 - progress reflects real work.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.errors import JobCancelled
from app.jobs.cancel import CancelToken
from app.jobs.progress import Progress, ProgressReporter, format_duration
from app.jobs.spec import Job, JobSpec, execute_job
from app.jobs.states import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransition,
    JobState,
    JobStateMachine,
    can_transition,
)


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

def test_state_machine_happy_path() -> None:
    machine = JobStateMachine()
    machine.transition(JobState.STARTING)
    machine.transition(JobState.RUNNING)
    machine.transition(JobState.SUCCEEDED)
    assert machine.state is JobState.SUCCEEDED
    assert machine.is_terminal


def test_terminal_states_are_final() -> None:
    for terminal in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        machine = JobStateMachine(state=terminal)
        with pytest.raises(InvalidStateTransition):
            machine.transition(JobState.RUNNING)


def test_repeating_the_same_terminal_state_is_a_noop() -> None:
    """The worker and the watchdog may both report a finish; that must not error."""
    machine = JobStateMachine(state=JobState.SUCCEEDED)
    assert machine.transition(JobState.SUCCEEDED) is JobState.SUCCEEDED
    assert machine.try_transition(JobState.CANCELLED) is False


def test_cannot_skip_from_pending_to_succeeded() -> None:
    machine = JobStateMachine()
    with pytest.raises(InvalidStateTransition):
        machine.transition(JobState.SUCCEEDED)


def test_state_helpers() -> None:
    assert JobState.RUNNING.is_active
    assert JobState.RUNNING.can_cancel
    assert not JobState.SUCCEEDED.can_cancel
    assert can_transition(JobState.RUNNING, JobState.CANCELLING)
    assert not can_transition(JobState.PENDING, JobState.SUCCEEDED)
    assert JobState.FAILED.label == "Failed"


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

def test_cancel_token_is_idempotent_and_thread_safe() -> None:
    token = CancelToken()
    assert not token.is_cancelled()
    token.cancel()
    token.cancel()
    assert token.is_cancelled()
    assert token.cancelled_at is not None


def test_cancel_token_raises_job_cancelled() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        token.raise_if_cancelled()


def test_cancel_token_sleep_is_interruptible() -> None:
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    started = time.monotonic()
    interrupted = token.sleep(5.0)
    assert interrupted is True
    assert time.monotonic() - started < 1.0


def test_cancel_token_callbacks_fire_once() -> None:
    token = CancelToken()
    calls: list[str] = []
    token.on_cancel(lambda reason: calls.append(reason))
    token.cancel("stop")
    token.cancel("stop again")
    assert calls == ["stop"]


def test_registering_a_process_after_cancel_stops_it_immediately() -> None:
    import subprocess
    import sys

    token = CancelToken()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        token.cancel()
        token.register_process(process)
        process.wait(timeout=10)
        assert process.poll() is not None
    finally:
        if process.poll() is None:  # pragma: no cover - safety net
            process.kill()


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

def test_progress_is_indeterminate_without_a_total() -> None:
    progress = Progress(current=5, total=0)
    assert progress.is_indeterminate
    assert progress.percent is None
    assert progress.percent_text() == "working..."


def test_progress_percentage_and_eta_are_measured() -> None:
    progress = Progress(current=0, total=10, unit="frames")
    progress.started_at = time.monotonic() - 10.0
    progress.updated_at = time.monotonic()
    progress.current = 5

    assert progress.percent == 50
    assert progress.rate == pytest.approx(0.5, rel=0.2)
    assert progress.eta_seconds == pytest.approx(10.0, rel=0.25)
    assert "5/10 frames" in progress.text()


def test_progress_reporter_throttles_but_always_reports_start_and_finish() -> None:
    snapshots: list[Progress] = []
    reporter = ProgressReporter(callback=snapshots.append, throttle_seconds=10.0)

    reporter.start(total=100, message="working")
    for index in range(50):
        reporter.update(current=index + 1)
    reporter.finish("done")

    assert len(snapshots) < 10, "updates must be throttled"
    assert snapshots[0].message == "working"
    assert snapshots[-1].current == 100
    assert snapshots[-1].message == "done"


def test_format_duration() -> None:
    assert format_duration(None) == "-"
    assert format_duration(0.25) == "250 ms"
    assert format_duration(8.4) == "8.4s"
    assert format_duration(125) == "2m 05s"
    assert format_duration(3725) == "1h 02m"


# --------------------------------------------------------------------------
# Job execution
# --------------------------------------------------------------------------

def _spec(body, key: str = "test.job", cancellable: bool = True) -> JobSpec:
    return JobSpec(key=key, title="Test job", body=body, cancellable=cancellable)


def test_job_body_runs_exactly_once() -> None:
    calls: list[int] = []
    job = Job(_spec(lambda context: calls.append(1) or "ok"))

    result = execute_job(job)

    assert calls == [1]
    assert result.succeeded
    assert result.value == "ok"
    assert result.state is JobState.SUCCEEDED


def test_job_failure_is_converted_to_a_friendly_result() -> None:
    def body(context):
        raise ValueError("boom")

    result = execute_job(Job(_spec(body)))

    assert result.failed
    assert result.error is not None
    assert "unexpected problem" in result.error.title.lower()
    assert "boom" in (result.error.technical or "")


def test_job_body_returning_after_cancel_is_reported_as_cancelled() -> None:
    """A partial result must never be reported as success (section 36)."""

    def body(context):
        context.cancel.cancel()
        return "partial"

    result = execute_job(Job(_spec(body)))

    assert result.cancelled
    assert result.state is JobState.CANCELLED
    assert result.value is None


def test_job_cancelled_before_start_never_runs_the_body() -> None:
    calls: list[int] = []

    def body(context):
        calls.append(1)

    job = Job(_spec(body))
    job.cancel.cancel()
    result = execute_job(job)

    assert calls == []
    assert result.cancelled
    assert result.error is not None
    assert result.error.error_code == "CANCELLED"


def test_job_cancelled_while_running_stops_promptly() -> None:
    def body(context):
        for _ in range(2000):
            context.raise_if_cancelled()
            time.sleep(0.001)
        return "finished"

    job = Job(_spec(body))
    threading.Timer(0.1, job.cancel.cancel).start()

    started = time.monotonic()
    result = execute_job(job)
    elapsed = time.monotonic() - started

    assert result.state is JobState.CANCELLED
    assert elapsed < 5.0
    assert result.duration_seconds > 0


def test_job_reports_progress_through_the_context() -> None:
    seen: list[Progress] = []

    def body(context):
        context.progress.set_callback(seen.append)
        context.progress.start(total=3, unit="steps")
        for index in range(3):
            context.progress.update(current=index + 1)
        return "done"

    result = execute_job(Job(_spec(body)))

    assert result.succeeded
    assert seen, "progress must be reported"
    assert result.final_progress is not None
    # The last measurement always reflects the final state, even when the
    # intermediate updates were throttled (section 55).
    assert result.final_progress.current == 3.0
    assert result.final_progress.percent == 100


def test_job_result_reports_a_terminal_state_for_every_outcome() -> None:
    """Section 18: no job may end up 'still running'."""

    def ok(context):
        return 1

    def fail(context):
        raise RuntimeError("nope")

    def cancel(context):
        context.cancel.cancel()
        raise JobCancelled("cancelled")

    for body, expected in ((ok, JobState.SUCCEEDED), (fail, JobState.FAILED), (cancel, JobState.CANCELLED)):
        result = execute_job(Job(_spec(body, key=f"test.{expected.value}")))
        assert result.state is expected
        assert result.state.is_terminal


def test_job_summary_and_serialisation() -> None:
    result = execute_job(Job(_spec(lambda context: "value")))
    payload = result.as_dict()
    assert payload["state"] == "succeeded"
    assert payload["error"] is None
    assert "finished" in result.summary()
