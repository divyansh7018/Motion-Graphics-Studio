"""The Kokoro self-test must never blur two different states (section 3).

The Stage E gate requires an explicit ``KOKORO VERIFIED`` or
``KOKORO NOT VERIFIED - TEST FALLBACK USED``, and nothing in between.  These
tests pin the exact strings and prove that a machine without model weights
reports the honest answer rather than a soft pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tts.selftest import (
    NOT_VERIFIED,
    VERIFIED,
    KokoroSelfTest,
    status_headline,
    verify_kokoro,
)


def test_there_are_exactly_two_headlines() -> None:
    assert status_headline(True) == VERIFIED == "KOKORO VERIFIED"
    assert status_headline(False) == NOT_VERIFIED
    assert NOT_VERIFIED == "KOKORO NOT VERIFIED - TEST FALLBACK USED"


def test_a_machine_without_weights_is_not_verified(tmp_path: Path) -> None:
    """This is the real state of the Stage E test machine."""
    from app.tts.capabilities import probe_kokoro

    if probe_kokoro(deep_init_check=False).ready:
        pytest.skip("Kokoro is installed here, so the fallback path cannot be shown.")

    result = verify_kokoro(tmp_path)

    assert result.verified is False
    assert result.headline == NOT_VERIFIED
    assert result.fallback_used is True
    assert result.audio_path is None
    assert result.reasons, "a not-verified result must say why"
    assert result.instructions, "a not-verified result must say what to do"


def test_the_description_names_the_state_and_the_reason(tmp_path: Path) -> None:
    result = KokoroSelfTest(verified=False, headline=NOT_VERIFIED,
                            reasons=["No model weights found."],
                            instructions=["Download the weights."])
    text = result.describe()

    assert text.startswith(NOT_VERIFIED)
    assert "No model weights found." in text
    assert "Download the weights." in text
    assert "cloud or paid voice service" in text, "must state there is no fallback"


def test_a_verified_result_reports_the_measured_audio(tmp_path: Path) -> None:
    result = KokoroSelfTest(verified=True, headline=VERIFIED,
                            audio_path=tmp_path / "selftest.wav",
                            duration_seconds=2.5, sample_rate=24000, channels=1,
                            voice="af_sky", language="en-us", model="kokoro-82M")
    text = result.describe()

    assert text.startswith(VERIFIED)
    assert "2.50s" in text
    assert "24000 Hz" in text
    assert "af_sky" in text
    assert result.fallback_used is False


def test_the_result_serialises_for_a_report(tmp_path: Path) -> None:
    result = KokoroSelfTest(verified=False, headline=NOT_VERIFIED,
                            reasons=["missing weights"])
    data = result.to_dict()

    assert data["verified"] is False
    assert data["headline"] == NOT_VERIFIED
    assert data["reasons"] == ["missing weights"]
    assert data["audio_path"] == ""
