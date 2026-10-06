"""Shared plumbing for image backend adapters (Stage F, sections 2, 35, 67).

Three things every adapter needs and should not reimplement:

* running a subprocess with a cancel token that really kills it;
* turning a cancel into a cancelled result rather than a failure;
* verifying what a backend wrote, so a backend cannot report success for a file
  that does not exist.

Security note: adapters only ever run a program the *user* configured, or one
that is already installed and discoverable on PATH.  Nothing here downloads or
executes code, and no adapter invents a command line (section 67).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from ...core.logging_setup import log_event
from ..provider import GenerationResult, GenerationState
from ..validation import validate_image_file

__all__ = ["run_process", "verify_output", "cancelled_result", "failed_result",
           "seed_from_request"]


def run_process(argv: Sequence[str], *, timeout: float = 3600.0,
                cancel: Any = None, cwd: Any = None,
                stdin_data: Optional[bytes] = None) -> tuple[int, str, str, bool]:
    """Run one subprocess, cancellable and window-free on Windows.

    Returns ``(returncode, stdout, stderr, cancelled)``.  The child is killed on
    cancel rather than left running, which is what stops a cancelled generation
    from leaving a zombie process behind (section 35).
    """
    cancelled = False
    stdout_text = ""
    stderr_text = ""
    try:
        process = subprocess.Popen(
            [str(part) for part in argv],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            cwd=str(cwd) if cwd else None,
            creationflags=_no_window_flags(),
        )
    except OSError as exc:
        return -1, "", f"The program could not be started: {exc}", False

    if cancel is not None and hasattr(cancel, "register"):
        cancel.register(process)
    try:
        try:
            out, err = process.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            return -1, "", f"The program did not finish within {timeout:.0f} seconds.", False
        stdout_text = (out or b"").decode("utf-8", "replace")
        stderr_text = (err or b"").decode("utf-8", "replace")
    finally:
        if cancel is not None and hasattr(cancel, "unregister"):
            cancel.unregister(process)

    if cancel is not None and cancel.is_cancelled():
        _kill(process)
        cancelled = True
    return (process.returncode if process.returncode is not None else -1,
            stdout_text, stderr_text, cancelled)


def _kill(process: Any) -> None:
    try:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _no_window_flags() -> int:
    """Hide the console window on Windows; a no-op elsewhere."""
    import sys

    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def verify_output(path: Any, *, expected_width: int = 0,
                  expected_height: int = 0) -> tuple[bool, str]:
    """Prove a backend really wrote the image it claims to have written.

    A backend that says "done" for a missing, empty or wrongly sized file is
    wrong, and the caller must not record it as a success (section 46).
    """
    target = Path(path)
    if not target.is_file():
        return False, f"The backend reported success but {target.name} was not written."
    try:
        if target.stat().st_size <= 0:
            return False, f"{target.name} was written but is 0 bytes long."
    except OSError as exc:
        return False, f"{target.name} could not be read: {exc}"
    check = validate_image_file(target)
    if not check.ok:
        return False, f"{target.name} is not a valid image: {check.error}"
    if expected_width and check.width != int(expected_width):
        return False, (f"{target.name} is {check.width} pixels wide but "
                       f"{expected_width} was requested.")
    if expected_height and check.height != int(expected_height):
        return False, (f"{target.name} is {check.height} pixels tall but "
                       f"{expected_height} was requested.")
    return True, ""


def cancelled_result(request: Any, message: str = "Generation cancelled.") -> GenerationResult:
    return GenerationResult(
        ok=False, state=GenerationState.CANCELLED, cancelled=True,
        error=message, mode=str(getattr(request, "mode", "") or ""),
        model=str(getattr(request, "model", "") or ""))


def failed_result(request: Any, error: str, *, why: str = "",
                  what_to_do: str = "", code: str = "GENERATION_FAILED",
                  backend: str = "") -> GenerationResult:
    """A failure with what happened, why, and what to do about it."""
    log_event("IMAGE_GENERATION_FAILED", error, code=code,
              mode=str(getattr(request, "mode", "") or ""))
    return GenerationResult(
        ok=False, state=GenerationState.FAILED, error=error, why=why,
        what_to_do=what_to_do, code=code,
        mode=str(getattr(request, "mode", "") or ""),
        model=str(getattr(request, "model", "") or ""), backend=backend)


def seed_from_request(request: Any) -> int:
    """The seed to use: the one asked for, or a fresh random one.

    The chosen value is always returned so the result can report it - a seed
    nobody recorded cannot be reused (section 9).
    """
    import random

    requested = int(getattr(request, "seed", 0) or 0)
    if requested != 0:
        return requested
    return random.randint(1, 2 ** 31 - 1)
