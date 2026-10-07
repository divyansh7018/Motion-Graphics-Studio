"""Running child processes safely, once (Stage G, sections 35, 36, 65, 89).

Every part of the application that runs an external program - the image
adapters, the video adapters, FFmpeg - needs the same four things:

* start a process **without a console window** on Windows;
* give it to a cancel token so cancelling really stops it;
* never wait forever (a timeout that ends the process, not the request);
* collect bounded output, so a runaway program cannot exhaust memory.

They live here rather than in one adapter package because two packages needing
them must not each grow their own copy: a fix applied to one copy and not the
other is exactly how the "cancelled, but the child kept running" defect happened
in Stage F.

Nothing here executes anything the user did not configure: the caller passes an
argument list, and this module runs it.  No shell, ever - a shell would turn a
prompt containing ``&`` into a second command (section 65).
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any, Optional, Sequence

__all__ = [
    "ProcessOutcome",
    "run_process",
    "terminate_process",
    "no_window_flags",
    "register_child",
    "unregister_child",
    "bounded_text",
    "MAX_OUTPUT_BYTES",
]

#: How much stdout/stderr one process may return.  A chatty tool must not be
#: able to fill memory; the tail is what matters for a diagnosis anyway.
MAX_OUTPUT_BYTES = 256 * 1024


class ProcessOutcome:
    """What a child process did: code, output, and whether it was cancelled."""

    __slots__ = ("returncode", "stdout", "stderr", "cancelled", "timed_out",
                 "seconds", "error")

    def __init__(self, *, returncode: int = -1, stdout: str = "", stderr: str = "",
                 cancelled: bool = False, timed_out: bool = False,
                 seconds: float = 0.0, error: str = "") -> None:
        self.returncode = int(returncode)
        self.stdout = stdout
        self.stderr = stderr
        self.cancelled = bool(cancelled)
        self.timed_out = bool(timed_out)
        self.seconds = float(seconds)
        self.error = str(error or "")

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled and not self.timed_out \
            and not self.error

    @property
    def output(self) -> str:
        """stdout and stderr together - the usual way a tool reports problems."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()

    def describe(self) -> str:
        if self.cancelled:
            return "The command was cancelled."
        if self.timed_out:
            return "The command did not finish in time and was stopped."
        if self.error:
            return self.error
        if self.returncode != 0:
            tail = self.output[-400:].strip()
            return (f"The command exited with code {self.returncode}."
                    + (f" Output: {tail}" if tail else ""))
        return "The command finished."

    def as_tuple(self) -> tuple[int, str, str, bool]:
        """The three-tuple-plus-cancel shape the older callers expect."""
        return (self.returncode, self.stdout, self.stderr, self.cancelled)


def no_window_flags() -> int:
    """Hide the console window on Windows; a no-op elsewhere."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def register_child(cancel: Any, process: subprocess.Popen) -> None:
    """Hand a child process to a cancel token so a cancel really stops it.

    :class:`app.jobs.cancel.CancelToken` names this ``register_process``; the
    older FFmpeg token names it ``register``.  Both spellings are accepted so a
    mismatch can never silently leave a child untracked - that mistake is what
    left a cancelled generation running in the background.
    """
    for name in ("register_process", "register"):
        method = getattr(cancel, name, None)
        if callable(method):
            try:
                method(process)
            except Exception:  # noqa: BLE001 - tracking must never break a run
                return
            return


def unregister_child(cancel: Any, process: subprocess.Popen) -> None:
    """Stop tracking a child process once it has been dealt with."""
    for name in ("unregister_process", "unregister"):
        method = getattr(cancel, name, None)
        if callable(method):
            try:
                method(process)
            except Exception:  # noqa: BLE001
                return
            return


def terminate_process(process: Any, grace_seconds: float = 10.0) -> None:
    """Force a process to stop.  Never raises; a dead process is a fine result."""
    try:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=grace_seconds)
    except (OSError, subprocess.SubprocessError, ValueError):
        try:
            process.kill()
        except (OSError, subprocess.SubprocessError):
            pass


def bounded_text(data: Optional[bytes], limit: int = MAX_OUTPUT_BYTES) -> str:
    """Decode output, keeping the end when it is too long to keep whole."""
    raw = data or b""
    if len(raw) > limit:
        raw = raw[-limit:]
    return raw.decode("utf-8", "replace")


def run_process(argv: Sequence[str], *, timeout: float = 3600.0,
                cancel: Any = None, cwd: Any = None,
                stdin_data: Optional[bytes] = None,
                env: Optional[dict] = None) -> ProcessOutcome:
    """Run one process, cancellable, window-free, and never unbounded.

    ``timeout`` is a real limit: on expiry the child is killed and the outcome
    says so.  A backend that hangs is reported as a timeout rather than hanging
    the whole application (section 36).
    """
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [str(part) for part in argv],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            cwd=str(cwd) if cwd else None,
            env=env,
            creationflags=no_window_flags(),
        )
    except OSError as exc:
        return ProcessOutcome(
            returncode=-1, seconds=time.monotonic() - started,
            error=f"The program could not be started: {exc}")

    if cancel is not None:
        register_child(cancel, process)
    try:
        try:
            out, err = process.communicate(input=stdin_data, timeout=timeout or None)
        except subprocess.TimeoutExpired:
            terminate_process(process)
            return ProcessOutcome(
                returncode=-1, timed_out=True, seconds=time.monotonic() - started,
                error=(f"The program did not finish within {timeout:.0f} seconds "
                       f"and was stopped."))
        outcome = ProcessOutcome(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=bounded_text(out), stderr=bounded_text(err),
            seconds=time.monotonic() - started)
    finally:
        if cancel is not None:
            unregister_child(cancel, process)

    if cancel is not None and cancel.is_cancelled():
        terminate_process(process)
        outcome.cancelled = True
    return outcome
