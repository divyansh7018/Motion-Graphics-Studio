"""Cancellation tokens (directive sections 18, 42).

A job owns exactly one :class:`CancelToken`.  Long operations poll it, and any
child process spawned on behalf of the job is registered with it so that
cancelling really stops the work instead of leaving an orphan FFmpeg process
behind (section 70).

The token is thread-safe: the UI thread calls :meth:`cancel` while a worker
thread is inside a loop calling :meth:`is_cancelled`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

from ..core.errors import JobCancelled


class CancelToken:
    """Cooperative cancellation plus child-process tracking."""

    def __init__(self, job_id: str = "") -> None:
        self.job_id = job_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes: list[subprocess.Popen] = []
        self._cancelled_at: Optional[float] = None
        #: Optional callbacks invoked (in the cancelling thread) when cancelled.
        self._on_cancel: list = []

    # -- state -------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled_at(self) -> Optional[float]:
        return self._cancelled_at

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until cancelled (used by polling code that would rather sleep)."""
        return self._event.wait(timeout)

    # -- requesting ---------------------------------------------------------

    def cancel(self, reason: str = "Cancelled by the user.") -> None:
        """Request cancellation.  Safe to call repeatedly and from any thread."""
        if self._event.is_set():
            return
        self._cancelled_at = time.time()
        self._event.set()
        callbacks = list(self._on_cancel)
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:  # pragma: no cover - a callback must never break cancellation
                pass

    def on_cancel(self, callback) -> None:
        """Register a callback invoked when cancellation is requested."""
        self._on_cancel.append(callback)
        if self._event.is_set():  # already cancelled - fire immediately
            try:
                callback("Cancelled by the user.")
            except Exception:  # pragma: no cover
                pass

    # -- cooperative checks -------------------------------------------------

    def raise_if_cancelled(self) -> None:
        """Raise :class:`JobCancelled` when cancellation was requested.

        Workers call this at safe points (between scenes, before each file write
        and inside render loops) so that cancelling is prompt but never leaves
        half-written data.
        """
        if self._event.is_set():
            raise JobCancelled("The operation was cancelled.")

    def sleep(self, seconds: float) -> bool:
        """Interruptible sleep.  Returns ``True`` if it was interrupted by cancel."""
        return self._event.wait(max(0.0, seconds))

    # -- child processes ----------------------------------------------------

    def register_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.append(process)
        if self._event.is_set():
            # Cancelled between spawning and registering: stop immediately.
            self.terminate_process(process)

    def unregister_process(self, process: subprocess.Popen) -> None:
        with self._lock:
            if process in self._processes:
                self._processes.remove(process)

    def active_process_count(self) -> int:
        with self._lock:
            return len(self._processes)

    def terminate_children(self, grace_seconds: float = 5.0) -> int:
        """Terminate every registered child process.  Returns how many were stopped."""
        with self._lock:
            processes = list(self._processes)
        stopped = 0
        for process in processes:
            if self.terminate_process(process, grace_seconds=grace_seconds):
                stopped += 1
        return stopped

    @staticmethod
    def terminate_process(process: subprocess.Popen, grace_seconds: float = 5.0) -> bool:
        """Ask a child process to stop, then force it.  Never raises."""
        if process.poll() is not None:
            return False
        try:
            process.terminate()
        except OSError:
            return False
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=grace_seconds)
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                return False
        # Close pipes so no handles leak into the next job (section 42).
        for stream in (process.stdout, process.stderr, process.stdin):
            try:
                if stream is not None:
                    stream.close()
            except OSError:  # pragma: no cover
                pass
        return True


class NullCancelToken(CancelToken):
    """A token that can never be cancelled (used by tests and by synchronous code)."""

    def cancel(self, reason: str = "") -> None:  # noqa: D102 - intentionally ignored
        return
