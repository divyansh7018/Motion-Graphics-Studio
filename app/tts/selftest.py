"""An honest Kokoro self-test (directive section 3, and the Stage E gate).

The Stage E report has to say one of two things and nothing in between:

``KOKORO VERIFIED``
    The local Kokoro pipeline really produced a playable WAV on this machine,
    and the file was measured afterwards.
``KOKORO NOT VERIFIED - TEST FALLBACK USED``
    Kokoro could not run here.  Any audio used by tests or scripts is labelled
    synthetic, and the render pipeline's verification is reported separately.

There is no third state, no partial credit and no fallback to a cloud or paid
voice service: Kokoro is the only TTS engine in this product, so when it cannot
run the answer is "not verified", full stop.

Importing this module does nothing - no model is loaded until
:func:`verify_kokoro` is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger, log_event
from .capabilities import probe_kokoro

LOGGER = get_logger("tts.selftest")

__all__ = [
    "VERIFIED",
    "NOT_VERIFIED",
    "KokoroSelfTest",
    "verify_kokoro",
    "status_headline",
]

#: The only two states this module ever reports.
VERIFIED = "KOKORO VERIFIED"
NOT_VERIFIED = "KOKORO NOT VERIFIED - TEST FALLBACK USED"

#: What the self-test asks the voice to say.
SELF_TEST_TEXT = "This is the Kokoro self test. Local narration is working."


@dataclass
class KokoroSelfTest:
    """The result of trying to generate real narration on this machine."""

    verified: bool = False
    headline: str = NOT_VERIFIED
    #: What was actually produced, when it was.
    audio_path: Optional[Path] = None
    duration_seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    voice: str = ""
    language: str = ""
    model: str = ""
    package_version: str = ""
    #: Why it could not be verified - the real reason, not a guess.
    reasons: list = field(default_factory=list)
    #: What the user can do about it.
    instructions: list = field(default_factory=list)

    @property
    def fallback_used(self) -> bool:
        return not self.verified

    def to_dict(self) -> dict:
        return {
            "verified": self.verified,
            "headline": self.headline,
            "audio_path": str(self.audio_path) if self.audio_path else "",
            "duration_seconds": round(self.duration_seconds, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "voice": self.voice,
            "language": self.language,
            "model": self.model,
            "package_version": self.package_version,
            "reasons": [str(item) for item in self.reasons],
            "instructions": [str(item) for item in self.instructions],
        }

    def describe(self) -> str:
        lines = [self.headline]
        if self.verified:
            lines.append(
                f"Voice {self.voice} ({self.language}) produced "
                f"{self.duration_seconds:.2f}s of audio at {self.sample_rate} Hz, "
                f"{self.channels} channel(s).")
            lines.append(f"File: {self.audio_path}")
            if self.model:
                lines.append(f"Model: {self.model}")
            if self.package_version:
                lines.append(f"Kokoro package: {self.package_version}")
        else:
            for reason in self.reasons:
                lines.append(f"  reason: {reason}")
            for step in self.instructions:
                lines.append(f"  to do : {step}")
            lines.append(
                "Audio used by tests or scripts on this machine is explicitly "
                "labelled synthetic. It is never presented as Kokoro output, and "
                "there is no cloud or paid voice service to fall back to.")
        return "\n".join(lines)


def status_headline(verified: bool) -> str:
    """The exact string to print, so the two states cannot be blurred."""
    return VERIFIED if verified else NOT_VERIFIED


def verify_kokoro(scratch: Path, *, text: str = SELF_TEST_TEXT,
                  deep: bool = True) -> KokoroSelfTest:
    """Try to generate one real narration file and measure it.

    ``scratch`` is a folder the test may write into.  Nothing here touches the
    user's projects, and no network access is attempted: if the model weights
    are not on disk, the answer is simply "not verified".
    """
    result = KokoroSelfTest()
    status = probe_kokoro(deep_init_check=deep)
    result.package_version = status.package_version
    result.model = str(status.model.path) if getattr(status, "model", None) and \
        getattr(status.model, "path", None) else ""

    if not status.ready:
        result.reasons = list(status.problems) or ["Kokoro is not usable here."]
        result.instructions = list(status.instructions)
        result.headline = status_headline(False)
        log_event("KOKORO_SELFTEST", result.headline, verified=False,
                  reasons=len(result.reasons))
        return result

    # Kokoro reports itself ready; now prove it by making a real file.
    try:
        from .audio import write_wav
        from .engine import GenerationRequest, KokoroEngine
        from .voices import discover_voices

        catalogue = discover_voices(status)
        if not catalogue.available:
            result.reasons = ["Kokoro is installed but reports no usable voice."]
            result.instructions = list(status.instructions)
            result.headline = status_headline(False)
            return result

        voice = catalogue.available[0]
        result.voice = str(voice.id)
        result.language = str(voice.language)

        folder = Path(scratch)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "kokoro_selftest.wav"

        engine = KokoroEngine()
        engine.load()
        generated = engine.synthesize(GenerationRequest(
            text=text, voice=result.voice, language=result.language,
            speed=1.0, volume=1.0))
        # synthesize() returns samples in memory; the file is what the rest of
        # the application consumes, so write it and measure what landed.
        info = write_wav(target, generated.samples, generated.sample_rate,
                         overwrite=True)
    except Exception as exc:  # noqa: BLE001 - the reason matters more than the type
        result.reasons = [f"Generation failed: {type(exc).__name__}: {exc}"]
        result.instructions = [
            "Check the model weights are present and readable.",
            "Run 'motion-studio voice check' for the full engine report.",
        ]
        result.headline = status_headline(False)
        log_event("KOKORO_SELFTEST", result.headline, verified=False, error=str(exc))
        return result

    # Measure what came out.  A file that exists is not the same as a file that
    # contains the right amount of audio.
    problems = list(getattr(info, "problems", []) or [])
    if problems:
        result.reasons = [f"The generated file is not valid: {problem}"
                          for problem in problems]
        result.instructions = ["Delete the file and try again, or reinstall the model."]
        result.headline = status_headline(False)
        return result

    rate = int(getattr(info, "sample_rate", 0) or 0)
    frames = int(getattr(info, "frames", 0) or 0)
    duration = frames / float(rate) if rate > 0 else 0.0
    if duration <= 0.1:
        result.reasons = [
            f"The generated file holds only {duration:.3f}s of audio, which is "
            f"too short to be the test sentence."]
        result.instructions = ["Reinstall the Kokoro model and try again."]
        result.headline = status_headline(False)
        return result

    result.verified = True
    result.headline = status_headline(True)
    result.audio_path = Path(target)
    result.duration_seconds = duration
    result.sample_rate = rate
    result.channels = int(getattr(info, "channels", 0) or 0)
    log_event("KOKORO_SELFTEST", result.headline, verified=True,
              voice=result.voice, seconds=round(result.duration_seconds, 2))
    return result
