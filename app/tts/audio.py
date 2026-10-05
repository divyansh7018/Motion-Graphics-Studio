"""Narration audio files: writing, validating and measuring (sections 23-24, 40).

A narration file is only accepted when it is a real, readable, non-zero WAV with
a sane sample rate, channel count and duration.  Anything less fails the job
instead of being reported as success, because a silently broken narration track
is far worse than a visible error.

Writes go through the project's atomic writer, so a crash mid-write cannot leave
a half-written WAV that later looks valid.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.core.errors import AppError, Severity
from app.core.logging_setup import get_logger

LOGGER = get_logger(__name__)

#: Narration is written as 16-bit PCM WAV: universally readable by FFmpeg,
#: lossless, and small enough for narration (24 kHz mono ≈ 2.8 MB/minute).
SAMPLE_WIDTH_BYTES = 2
SUBTYPE = "PCM_16"

#: Minimum bytes of header before the audio data in a canonical WAV.  Extra
#: chunks (LIST, fact) only make the file larger, so this stays a safe floor.
WAV_HEADER_BYTES = 44

#: Sanity limits for a narration file.
MIN_DURATION_SECONDS = 0.05
MAX_DURATION_SECONDS = 3 * 60 * 60
VALID_SAMPLE_RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000)


class AudioError(AppError):
    """A narration file could not be written or is not valid."""

    default_title = "Narration audio problem"
    default_severity = Severity.ERROR


@dataclass
class WavInfo:
    """Measured facts about a narration WAV."""

    path: Path
    exists: bool = False
    readable: bool = False
    sample_rate: int = 0
    channels: int = 0
    frames: int = 0
    sample_width: int = 0
    format_name: str = ""
    subtype: str = ""
    size_bytes: int = 0
    #: Problems found; empty means the file is usable.
    problems: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.frames / float(self.sample_rate)

    @property
    def valid(self) -> bool:
        return self.exists and self.readable and not self.problems

    def duration_label(self) -> str:
        return format_seconds(self.duration_seconds)

    def describe(self) -> str:
        if not self.exists:
            return "The narration file does not exist."
        if not self.readable:
            return "The narration file could not be read."
        if self.problems:
            return "; ".join(self.problems)
        return (
            f"{self.duration_label()} · {self.sample_rate} Hz · "
            f"{self.channels} channel{'s' if self.channels != 1 else ''} · "
            f"{self.size_bytes / (1024 * 1024):.2f} MB"
        )


def format_seconds(seconds: float) -> str:
    """A short, human duration: 1:23 or 12.4s."""
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "0s"
    if value < 10:
        return f"{value:.2f}s"
    minutes, secs = divmod(int(round(value)), 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def float_to_pcm16(samples: np.ndarray) -> bytes:
    """Convert float32 samples in [-1, 1] to 16-bit PCM bytes with clipping."""
    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0:
        return b""
    peak = float(np.max(np.abs(array)))
    if peak > 1.0:
        array = array / peak
    clipped = np.clip(array, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def write_wav(path: Path,
              samples: np.ndarray,
              sample_rate: int,
              channels: int = 1,
              *,
              overwrite: bool = True) -> WavInfo:
    """Write narration samples to *path* atomically.

    Writes to a temporary sibling then replaces the target, so an interrupted
    write never leaves a truncated file that looks like valid narration.
    """
    from app.core.atomicio import atomic_write_bytes

    target = Path(path)
    data = float_to_pcm16(samples)
    if not data:
        raise AudioError(
            "The engine produced no audio samples, so there is nothing to save.",
            actions=((
                "Check the script has speakable text and a voice is selected, "
                "then generate again."
            ),),
            title="The generated narration is empty"
        )
    if target.exists() and not overwrite:
        raise AudioError(
            f"{target.name} is already present in the project.",
            actions=((
                "Choose a different output name, or allow this file to be replaced."
            ),),
            title="The narration file already exists"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    frame_count = len(data) // (SAMPLE_WIDTH_BYTES * max(1, channels))
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(max(1, int(channels)))
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(int(sample_rate))
        handle.writeframes(data)
    atomic_write_bytes(target, buffer.getvalue())
    LOGGER.info("narration wav written",
                extra={"path": str(target), "frames": frame_count, "rate": sample_rate})
    return validate_wav(target)


# --------------------------------------------------------------------------
# Validating
# --------------------------------------------------------------------------

def validate_wav(path: Path, *, require_audio: bool = True) -> WavInfo:
    """Measure and validate a narration file.

    Uses the standard library ``wave`` module first so validation does not depend
    on soundfile being importable; falls back to soundfile for formats ``wave``
    cannot parse (float WAV, for example).
    """
    target = Path(path)
    info = WavInfo(path=target)
    if not target.exists():
        info.problems.append("The narration file is missing.")
        return info
    info.exists = True
    try:
        info.size_bytes = target.stat().st_size
    except OSError:
        info.size_bytes = 0
    if info.size_bytes == 0:
        info.problems.append("The narration file is empty (0 bytes).")
        return info

    try:
        with wave.open(str(target), "rb") as handle:
            info.channels = handle.getnchannels()
            info.sample_width = handle.getsampwidth()
            info.sample_rate = handle.getframerate()
            info.frames = handle.getnframes()
            info.format_name = "WAV"
            info.subtype = f"PCM_{info.sample_width * 8}"
            if info.frames == 0:
                info.problems.append("The narration file contains no audio frames.")
                return info
            if info.channels and info.sample_width:
                expected = info.frames * info.channels * info.sample_width
                # A header can claim more data than the file holds when a write
                # was interrupted, so compare the claim against the real size.
                if info.size_bytes < expected + WAV_HEADER_BYTES:
                    info.problems.append(
                        "The narration file is truncated: its header claims "
                        f"{info.frames} frames but the file only holds about "
                        f"{max(0, info.size_bytes - WAV_HEADER_BYTES)} bytes of audio."
                    )
                    return info
                # Read the LAST frames, not the first: a file can have a valid
                # start and still be cut short at the end.
                handle.setpos(max(0, info.frames - 512))
                tail = handle.readframes(min(info.frames, 512))
                if not tail:
                    info.problems.append(
                        "The narration file has a header but no readable audio data."
                    )
                    return info
        info.readable = True
    except Exception as error:  # noqa: BLE001 - fall back, then report
        info = _validate_with_soundfile(target, info, str(error))

    if not info.readable:
        return info

    if info.sample_rate not in VALID_SAMPLE_RATES:
        info.problems.append(
            f"The sample rate {info.sample_rate} Hz is not one the pipeline uses."
        )
    if info.channels < 1 or info.channels > 2:
        info.problems.append(f"The channel count {info.channels} is unexpected for narration.")
    duration = info.duration_seconds
    if require_audio and duration <= MIN_DURATION_SECONDS:
        info.problems.append(
            f"The narration is only {duration:.3f} seconds long, which means generation failed."
        )
    if duration > MAX_DURATION_SECONDS:
        info.problems.append("The narration is longer than three hours, which is not plausible.")
    return info


def _validate_with_soundfile(target: Path, info: WavInfo, wave_error: str) -> WavInfo:
    """Second opinion for WAV variants the standard library rejects."""
    try:
        import soundfile as sf
    except Exception:  # noqa: BLE001 - soundfile is optional for validation
        info.problems.append(f"The narration file could not be read: {wave_error}")
        return info
    try:
        probe = sf.info(str(target))
        info.channels = int(probe.channels)
        info.sample_rate = int(probe.samplerate)
        info.frames = int(probe.frames)
        info.sample_width = max(1, int(probe.samplerate and 2))
        info.format_name = probe.format or "WAV"
        info.subtype = probe.subtype or ""
        info.readable = True
    except Exception as error:  # noqa: BLE001
        info.problems.append(f"The narration file could not be read: {wave_error} / {error}")
    return info


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a narration file back as float32 samples plus its sample rate."""
    try:
        import soundfile as sf

        samples, rate = sf.read(str(path), dtype="float32", always_2d=False)
        array = np.asarray(samples, dtype=np.float32)
        if array.ndim > 1:
            array = array.reshape(-1)
        return array, int(rate)
    except Exception:  # noqa: BLE001 - fall back to the standard library
        pass
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise AudioError(
            f"Sample width is {width} bytes; 16-bit PCM was expected.",
            actions=("Regenerate the narration so it is written in the standard format.",),
            title="The narration file uses an unsupported sample format"
        )
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, int(rate)


def duration_seconds(path: Path) -> float:
    """Measured duration in seconds, or 0.0 when the file is unusable."""
    info = validate_wav(path, require_audio=False)
    return info.duration_seconds if info.readable else 0.0


def estimate_size_bytes(duration: float, sample_rate: int = 24000, channels: int = 1) -> int:
    """Approximate file size for planning (never used for validation)."""
    return int(max(0.0, duration) * sample_rate * channels * SAMPLE_WIDTH_BYTES)


__all__ = [
    "AudioError",
    "SAMPLE_WIDTH_BYTES",
    "SUBTYPE",
    "VALID_SAMPLE_RATES",
    "WAV_HEADER_BYTES",
    "WavInfo",
    "duration_seconds",
    "estimate_size_bytes",
    "float_to_pcm16",
    "format_seconds",
    "read_wav",
    "validate_wav",
    "write_wav",
]
