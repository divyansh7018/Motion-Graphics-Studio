"""Job wrapper for the system check (directive sections 7, 46, 55).

The system check touches the disk and runs FFmpeg ``-version``, so it must
never run on the GUI thread.  It is submitted like any other job, which also
gives it progress reporting and cancellation for free.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec
from . import items
from .status import CheckReport, run_system_check


def run_system_check_job(context: JobContext, deep: bool = False) -> CheckReport:
    """Job body: build a check context and run every check.

    Progress is real: one step per check item (section 55).
    """
    check_context = items.build_context(
        context.paths,
        context.settings,
        deep=deep,
        environment=context.get("environment"),
    )

    def on_progress(done: int, total: int, title: str) -> None:
        context.progress.update(current=done, message=title)
        if done == 0:
            context.progress.set_total(total)

    context.progress.start(total=0, message="Starting system check", unit="checks")
    report = run_system_check(
        check_context,
        order=items.DISPLAY_ORDER,
        progress=on_progress,
    )
    context.progress.finish("System check finished")
    return report


def make_system_check_spec(
    paths,
    settings,
    deep: bool = False,
    environment=None,
    on_result: Optional[Callable[[CheckReport], None]] = None,
) -> JobSpec:
    """Build the :class:`JobSpec` for a system check."""

    def body(context: JobContext) -> CheckReport:
        report = run_system_check_job(context, deep=deep)
        if on_result is not None:
            on_result(report)
        return report

    return JobSpec(
        key=JobKeys.SYSTEM_CHECK,
        title="System check",
        description="Checking this computer for the tools needed to make videos.",
        body=body,
        cancellable=True,
        allow_parallel=False,
        settings=settings,
        paths=paths,
        payload={"deep": deep, "environment": environment},
    )


def make_quick_check_spec(paths, settings, environment=None) -> JobSpec:
    """A fast check used at startup (fewer items, no self-tests)."""
    return make_system_check_spec(paths, settings, deep=False, environment=environment)
