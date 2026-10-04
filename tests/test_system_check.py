"""System check framework and report aggregation (directive sections 46, 64, 65)."""

from __future__ import annotations


import pytest

from app.checks import items as check_items
from app.checks.status import (
    CheckContext,
    CheckReport,
    CheckResult,
    Status,
    clear_registry,
    register_check,
    registered_checks,
    run_check,
    run_system_check,
)


def _context(paths, settings, deep: bool = False) -> CheckContext:
    return check_items.build_context(paths, settings, deep=deep)


def test_status_glyphs_and_semantics() -> None:
    assert Status.READY.glyph == "✓"
    assert Status.MISSING.glyph == "✗"
    assert Status.OPTIONAL.glyph == "⚠"
    assert Status.BLOCKED.blocks_startup
    assert not Status.WARNING.is_problem
    assert Status.MISSING.is_problem


def test_built_in_checks_are_registered_in_display_order(paths, settings) -> None:
    specs = registered_checks(check_items.DISPLAY_ORDER)
    ids = [spec.check_id for spec in specs]
    assert "python.version" in ids
    assert "filesystem.data_root" in ids
    assert "media.ffmpeg" in ids
    assert "voice.kokoro" in ids
    assert len(ids) == len(set(ids))


def test_full_check_produces_a_report_for_every_item(paths, settings) -> None:
    report = run_system_check(_context(paths, settings), order=check_items.DISPLAY_ORDER)

    assert report.results
    assert report.duration_seconds >= 0
    assert report.headline()
    assert all(result.status is not Status.UNKNOWN or result.skipped for result in report.results)
    assert report.by_id("filesystem.data_root") is not None


def test_core_checks_pass_in_a_healthy_temporary_folder(paths, settings) -> None:
    report = run_system_check(_context(paths, settings), order=check_items.DISPLAY_ORDER)

    for check_id in ("python.version", "filesystem.data_root", "filesystem.directories", "storage.output"):
        result = report.by_id(check_id)
        assert result is not None and result.status is Status.READY, f"{check_id}: {result.summary if result else 'missing'}"


def test_missing_ffmpeg_is_reported_as_missing_not_as_ready(paths, settings, tmp_path) -> None:
    """A machine without FFmpeg must say so, with instructions (sections 29, 40)."""
    settings.media.use_bundled_tools = False
    context = _context(paths, settings)

    result = run_check(report_spec(context, "media.ffmpeg"), context)

    if result.status is Status.READY:  # a real ffmpeg is on PATH on this machine
        pytest.skip("FFmpeg is installed on this machine")
    assert result.status is Status.MISSING
    assert result.actions, "the user must be told what to do"
    assert "FFmpeg" in result.what_happened


def report_spec(context: CheckContext, check_id: str):
    for spec in registered_checks():
        if spec.check_id == check_id:
            return spec
    raise AssertionError(f"unknown check {check_id}")


def test_check_error_boundary_keeps_the_report_complete(paths, settings) -> None:
    """A broken check must not stop the others (section 41)."""
    clear_registry()

    @register_check("test.explodes", "Exploding check", "Tests")
    def exploding(context: CheckContext) -> CheckResult:  # pragma: no cover - raised on purpose
        raise RuntimeError("kaboom")

    @register_check("test.fine", "Working check", "Tests")
    def fine(context: CheckContext) -> CheckResult:
        return CheckResult(
            check_id="test.fine",
            title="Working check",
            status=Status.READY,
            summary="all good",
            required_for="Tests",
        )

    try:
        report = run_system_check(CheckContext(paths=paths, settings=settings))
        assert len(report.results) == 2
        broken = report.by_id("test.explodes")
        assert broken is not None
        # The failure is contained and reported as a warning, not as a crash.
        assert broken.status is Status.WARNING
        assert "could not be completed" in broken.what_happened
        assert report.by_id("test.fine").status is Status.READY
    finally:
        clear_registry()
        # Re-register the built-in checks for the rest of the session.
        import importlib

        importlib.reload(check_items)


def test_report_aggregation_helpers() -> None:
    report = CheckReport(
        results=[
            CheckResult("a", "A", Status.READY, "ok"),
            CheckResult("b", "B", Status.WARNING, "hmm"),
            CheckResult("c", "C", Status.MISSING, "no"),
            CheckResult("d", "D", Status.BLOCKED, "stop"),
        ]
    )
    assert report.ready_count == 1
    assert len(report.warnings) == 1
    assert len(report.missing) == 1
    assert len(report.blockers) == 1
    assert not report.core_ready
    assert "blocker" in report.headline().lower()

    text = report.to_text()
    assert "✓ A" in text
    assert "✗ C" in text

    payload = report.as_dict()
    assert payload["counts"]["ready"] == 1


def test_report_with_only_optional_items_is_core_ready() -> None:
    report = CheckReport(
        results=[
            CheckResult("a", "A", Status.READY, "ok"),
            CheckResult("b", "B", Status.OPTIONAL, "voice not installed"),
        ]
    )
    assert report.core_ready
    assert "optional note" in report.headline().lower()


def test_check_result_serialisation() -> None:
    result = CheckResult(
        check_id="x",
        title="X",
        status=Status.MISSING,
        summary="missing",
        actions=("do this",),
        details=("line",),
        required_for="Rendering",
    )
    payload = result.as_dict()
    assert payload["status"] == "missing"
    assert payload["actions"] == ["do this"]
    assert payload["required_for"] == "Rendering"
    assert result.to_line().startswith("✗ X")


def test_deep_check_runs_the_ffmpeg_self_test_only_when_asked(paths, settings) -> None:
    shallow = run_check(report_spec(None, "media.ffmpeg_quick_test"), _context(paths, settings, deep=False))
    assert shallow.skipped or shallow.status is not Status.READY
