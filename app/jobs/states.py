"""Job state machine (directive sections 7, 18, 42).

Every background operation in the application is a *job* with exactly one of
these terminal states: ``SUCCEEDED``, ``FAILED`` or ``CANCELLED``.  There is no
"stuck on generating" state - a job that does not reach a terminal state is a
bug, and the UI shows a watchdog warning if one ever happens.

The state machine is intentionally tiny and pure so it can be unit tested
without Qt or threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class JobState(str, Enum):
    """Lifecycle of a single job."""

    PENDING = "pending"          # created, not handed to a worker yet
    STARTING = "starting"        # worker picked it up
    RUNNING = "running"          # work in progress
    CANCELLING = "cancelling"    # cancel requested, worker has not acknowledged yet
    SUCCEEDED = "succeeded"      # finished, result produced
    FAILED = "failed"            # finished with an error
    CANCELLED = "cancelled"      # stopped at the user's request

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (JobState.PENDING, JobState.STARTING, JobState.RUNNING, JobState.CANCELLING)

    @property
    def can_cancel(self) -> bool:
        return self in (JobState.PENDING, JobState.STARTING, JobState.RUNNING)

    @property
    def label(self) -> str:
        return {
            JobState.PENDING: "Waiting",
            JobState.STARTING: "Starting",
            JobState.RUNNING: "Running",
            JobState.CANCELLING: "Cancelling",
            JobState.SUCCEEDED: "Finished",
            JobState.FAILED: "Failed",
            JobState.CANCELLED: "Cancelled",
        }[self]


#: Allowed transitions.  Anything not listed here is rejected, which is what
#: prevents double-completion events (directive section 9).
ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.STARTING, JobState.CANCELLED, JobState.FAILED}),
    JobState.STARTING: frozenset({JobState.RUNNING, JobState.CANCELLING, JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.CANCELLING, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}),
    JobState.CANCELLING: frozenset({JobState.CANCELLED, JobState.FAILED, JobState.SUCCEEDED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class InvalidStateTransition(Exception):
    """Raised when code tries to move a job through an impossible transition."""

    def __init__(self, current: JobState, requested: JobState) -> None:
        super().__init__(f"Cannot change a job from {current.value} to {requested.value}.")
        self.current = current
        self.requested = requested


def can_transition(current: JobState, requested: JobState) -> bool:
    return requested in ALLOWED_TRANSITIONS.get(current, frozenset())


@dataclass
class JobStateMachine:
    """Tracks one job's state and guarantees exactly one terminal transition."""

    state: JobState = JobState.PENDING
    history: list[tuple[JobState, str]] = field(default_factory=list)

    def transition(self, requested: JobState, reason: str = "") -> JobState:
        """Move to *requested*, raising :class:`InvalidStateTransition` when illegal.

        A transition to the same terminal state twice is a no-op instead of an
        error: the worker and the watchdog may both observe a finish, and the
        UI must still receive exactly one completion event.
        """
        if requested == self.state:
            return self.state
        if self.state.is_terminal and requested == self.state:
            return self.state
        if not can_transition(self.state, requested):
            raise InvalidStateTransition(self.state, requested)
        self.state = requested
        self.history.append((requested, reason))
        return self.state

    def try_transition(self, requested: JobState, reason: str = "") -> bool:
        """Non-raising variant used by cleanup paths."""
        try:
            self.transition(requested, reason)
            return True
        except InvalidStateTransition:
            return False

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def describe(self) -> str:
        if not self.history:
            return self.state.value
        trail = " -> ".join(state.value for state, _reason in self.history)
        return f"{self.state.value} ({trail})"
