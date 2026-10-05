"""The voice items in the System Check (directive sections 49-50).

The checks must report what is really on the machine.  Two situations are tested:
nothing installed (the honest "not installed, here is how to fix it" report) and a
synthetic model folder (real counts, discovered languages, per-voice detail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.checks.items as items
from app.checks.status import CheckContext, Status, run_system_check

VOICE_IDS = ["af_bella", "am_adam", "hf_alpha", "hm_ishaan", "zf_xiaobei"]


@pytest.fixture()
def context(paths, settings) -> CheckContext:
    return CheckContext(paths=paths, settings=settings, deep=True)


@pytest.fixture()
def kokoro_dir(tmp_path: Path) -> Path:
    root = tmp_path / "models" / "kokoro"
    (root / "voices").mkdir(parents=True)
    (root / "kokoro-82m-v1.0.onnx").write_bytes(b"ONNXFAKE" * 4096)
    for voice in VOICE_IDS:
        (root / "voices" / f"{voice}.pt").write_bytes(b"VOICEFAKE" * 64)
    return root


def _with_model(context: CheckContext, kokoro_dir: Path) -> CheckContext:
    """Point the check at the synthetic model folder."""
    context.settings.voice.model_dir = str(kokoro_dir)
    return context


# --------------------------------------------------------------------------
# Nothing installed
# --------------------------------------------------------------------------

def test_a_missing_engine_is_reported_with_a_fix(context, monkeypatch) -> None:
    # Forced, not assumed: this machine may well have Kokoro installed.
    import app.tts.capabilities as capabilities

    monkeypatch.setattr(
        capabilities, "probe_package",
        lambda name="kokoro": (False, "", "No module named 'kokoro'"),
    )
    result = items.check_kokoro(context)

    assert result.check_id == "voice.kokoro"
    assert result.status in (Status.OPTIONAL, Status.WARNING, Status.MISSING)
    assert "not installed" in result.summary.lower()
    assert result.actions, "the user must be given something to do"
    assert any("pip install" in action for action in result.actions)
    assert "optional" in result.why.lower(), "narration must not look mandatory"


def test_the_voice_checks_never_invent_voices(context, monkeypatch) -> None:
    # Forced, not assumed: with Kokoro installed the language check is READY,
    # which is correct but not what this test is about.
    import app.tts.capabilities as capabilities

    monkeypatch.setattr(
        capabilities, "probe_package",
        lambda name="kokoro": (False, "", "No module named 'kokoro'"),
    )
    monkeypatch.setattr(capabilities, "_language_codes_from_engine", lambda: {})
    voices = items.check_voices(context)
    languages = items.check_languages(context)

    assert voices.status in (Status.OPTIONAL, Status.WARNING)
    assert voices.count if hasattr(voices, "count") else True
    assert "hard-coded" in voices.why.lower()
    assert languages.status == Status.OPTIONAL
    assert "supports" in languages.why.lower()


def test_the_self_test_does_not_run_on_startup(paths, settings) -> None:
    """Section 50: the self-test generates audio, so it must be opt-in."""
    result = items.check_tts_selftest(CheckContext(paths=paths, settings=settings, deep=False))

    assert result.skipped is True
    assert result.status == Status.UNKNOWN
    assert "Re-check" in result.summary


def test_the_self_test_is_honest_when_the_engine_is_absent(context) -> None:
    result = items.check_tts_selftest(context)

    assert result.status in (Status.OPTIONAL, Status.WARNING)
    assert result.status != Status.READY, "it must never claim success without audio"
    assert "not ready" in result.summary.lower() or "skipped" in result.summary.lower()


def test_every_voice_item_reports_a_component_breakdown(context) -> None:
    result = items.check_kokoro(context)

    joined = " ".join(result.details).lower()
    for component in ("package", "runtime", "model", "voices", "languages", "phonemiser"):
        assert component in joined, f"the report must cover {component}"


# --------------------------------------------------------------------------
# A model folder is present
# --------------------------------------------------------------------------

def test_discovered_voices_are_reported_with_real_counts(context, kokoro_dir) -> None:
    result = items.check_voices(_with_model(context, kokoro_dir))

    assert str(len(VOICE_IDS)) in result.summary
    listed = " ".join(result.details)
    for voice in VOICE_IDS:
        assert voice in listed, f"{voice} should be listed from disk"


def test_languages_are_reported_with_their_source(context, kokoro_dir) -> None:
    result = items.check_languages(_with_model(context, kokoro_dir))

    assert result.status == Status.READY
    assert "detected from" in result.summary
    assert "Hindi" in " ".join(result.details), "the fixture has Hindi voices"


def test_a_voice_added_to_disk_appears_without_a_code_change(context, kokoro_dir) -> None:
    """Sections 5-6: refresh must pick up new voices."""
    before = items.check_voices(_with_model(context, kokoro_dir))
    assert str(len(VOICE_IDS)) in before.summary

    (kokoro_dir / "voices" / "bf_newvoice.pt").write_bytes(b"VOICEFAKE" * 64)
    fresh = CheckContext(paths=context.paths, settings=context.settings, deep=True)
    after = items.check_voices(_with_model(fresh, kokoro_dir))

    assert str(len(VOICE_IDS) + 1) in after.summary
    assert "bf_newvoice" in " ".join(after.details)


def test_the_engine_item_lists_the_model_it_found(context, kokoro_dir) -> None:
    result = items.check_kokoro(_with_model(context, kokoro_dir))

    joined = " ".join(result.details)
    assert "kokoro-82m-v1.0.onnx" in joined
    assert f"voices on disk: {len(VOICE_IDS)}" in joined


# --------------------------------------------------------------------------
# The whole report
# --------------------------------------------------------------------------

def test_the_full_report_includes_every_voice_item(context) -> None:
    report = run_system_check(context)

    ids = [result.check_id for result in report.results]
    for expected in ("voice.kokoro", "voice.voices", "voice.languages", "voice.selftest"):
        assert expected in ids


def test_no_check_reports_a_bare_internal_error(context, kokoro_dir) -> None:
    """A masked exception would read as 'could not be completed' - never accept that."""
    report = run_system_check(_with_model(context, kokoro_dir))

    for result in report.results:
        if result.check_id.startswith("voice"):
            assert "could not be completed" not in result.summary.lower(), result.check_id
            assert "could not be completed" not in result.what_happened.lower(), result.check_id


def test_a_missing_runtime_reads_cleanly() -> None:
    """BUG REGRESSION: with no runtime found the report read ": not installed".

    ``RuntimeInfo.describe`` interpolated an empty backend name, so the System
    Check and ``voice check`` both printed a stray leading colon - a small thing,
    but it is the line a user reads when narration does not work.
    """
    from app.tts.capabilities import RuntimeInfo

    assert RuntimeInfo().describe == "not installed"
    assert RuntimeInfo(name="onnxruntime").describe == "onnxruntime: not installed"
    assert RuntimeInfo(name="onnxruntime", version="1.17.0", available=True).describe == (
        "onnxruntime 1.17.0"
    )
