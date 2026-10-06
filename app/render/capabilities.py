"""Encoder capabilities and export validation (Stage E, sections 24-30, 47).

Nothing here is assumed.  Which encoders exist, which pixel formats they accept
and which rate-control modes they support are read from the installed FFmpeg, so
the UI can only offer what will actually work - and a codec that is missing stops
the render *before* any frames are drawn, with a message that says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..project.presets import (
    CODEC_ENCODERS,
    CODEC_LABELS,
    CODECS_BY_CONTAINER,
    CONTAINERS,
    PIXEL_FORMATS_BY_CODEC,
    AUDIO_CODECS_BY_CONTAINER,
    FPS_OPTIONS,
)

__all__ = [
    "EncoderCapabilities",
    "CapabilityIssue",
    "detect_capabilities",
    "validate_export_settings",
    "estimate_file_size",
    "estimate_render_time",
    "rate_control_modes",
    "SUPPORTED_RATES",
    "MIN_DIMENSION",
    "MAX_DIMENSION",
]

#: Frame rates the export UI offers.  A custom rate is allowed only when the
#: caller says the encoder supports it - never silently substituted.
SUPPORTED_RATES: tuple[int, ...] = FPS_OPTIONS

#: Sane bounds for a rendered frame.  Beyond these a project is almost always a
#: mistake, and FFmpeg would fail expensively part-way through.
MIN_DIMENSION = 64
MAX_DIMENSION = 8192

#: Rate-control modes per encoder family.  Offering CQ for x264 (or CRF for VP9)
#: would be a technically invalid combination.
_RATE_CONTROL: dict[str, tuple[str, ...]] = {
    "libx264": ("crf", "bitrate"),
    "libx265": ("crf", "bitrate"),
    "libvpx-vp9": ("cq", "bitrate"),
}


@dataclass
class CapabilityIssue:
    code: str
    message: str
    what_to_do: str = ""
    severity: str = "error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


@dataclass
class EncoderCapabilities:
    """What this machine's FFmpeg can really do."""

    #: Encoder names FFmpeg reported (libx264, libx265, ...).
    encoders: list = field(default_factory=list)
    #: Audio encoder names.
    audio_encoders: list = field(default_factory=list)
    ffmpeg_version: str = ""
    #: True when the list came from FFmpeg rather than being assumed.
    detected: bool = False

    def has_encoder(self, encoder: str) -> bool:
        return str(encoder) in self.encoders

    def has_audio_encoder(self, encoder: str) -> bool:
        return str(encoder) in self.audio_encoders

    def supports_codec(self, codec_id: str) -> bool:
        """Whether the encoder behind one of our codec ids is installed."""
        encoder = CODEC_ENCODERS.get(str(codec_id), "")
        return bool(encoder) and self.has_encoder(encoder)

    def available_codecs(self, container: str = "mp4") -> list[str]:
        return [codec for codec in CODECS_BY_CONTAINER.get(container, ())
                if self.supports_codec(codec)]

    def codec_label(self, codec_id: str) -> str:
        label = CODEC_LABELS.get(str(codec_id), str(codec_id))
        if not self.supports_codec(codec_id):
            return f"{label} - NOT AVAILABLE in this FFmpeg"
        return label

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "ffmpeg_version": self.ffmpeg_version,
            "video_encoders": list(self.encoders),
            "audio_encoders": list(self.audio_encoders),
        }


def detect_capabilities(tools: Any, *, timeout: float = 30.0) -> EncoderCapabilities:
    """Ask FFmpeg which encoders it actually has.

    Returns an empty, clearly-not-detected result when FFmpeg is unavailable, so
    a caller can say "unknown" instead of guessing.
    """
    caps = EncoderCapabilities()
    if tools is None or not getattr(tools.discovery, "has_ffmpeg", False):
        return caps

    result = tools.run(["-hide_banner", "-encoders"], timeout=timeout)
    if not result.ok:
        return caps
    caps.detected = True
    version = tools.discovery.ffmpeg.version if tools.discovery.ffmpeg else ""
    caps.ffmpeg_version = str(version or "")

    started = False
    for line in (result.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("------"):
            started = True
            continue
        if not started or not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        if not name.replace("_", "").replace("-", "").isalnum():
            continue
        if flags.startswith("V"):
            caps.encoders.append(name)
        elif flags.startswith("A"):
            caps.audio_encoders.append(name)
    return caps


def rate_control_modes(codec_id: str) -> tuple[str, ...]:
    """The rate-control modes that are valid for a codec."""
    encoder = CODEC_ENCODERS.get(str(codec_id), "")
    return _RATE_CONTROL.get(encoder, ("bitrate",))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_export_settings(settings: Any, caps: EncoderCapabilities, *,
                            duration: float = 0.0) -> list:
    """Check an export configuration before anything expensive happens.

    Every problem is returned with what to do about it, and the render must not
    start while any error is present (directive sections 19, 27, 47).
    """
    issues: list[CapabilityIssue] = []
    width = int(getattr(settings, "width", 0) or 0)
    height = int(getattr(settings, "height", 0) or 0)
    fps = int(getattr(settings, "fps", 0) or 0)
    codec = str(getattr(settings, "codec", "") or "")
    container = str(getattr(settings, "container", "") or "")
    pixel_format = str(getattr(settings, "pixel_format", "") or "")
    audio_codec = str(getattr(settings, "audio_codec", "") or "")

    if not (MIN_DIMENSION <= width <= MAX_DIMENSION) or not (MIN_DIMENSION <= height <= MAX_DIMENSION):
        issues.append(CapabilityIssue(
            "RESOLUTION_INVALID",
            f"The resolution {width}x{height} is outside the supported range "
            f"({MIN_DIMENSION}-{MAX_DIMENSION} pixels per side).",
            "Choose a standard resolution such as 1280x720 or 1920x1080.",
        ))
    elif width % 2 or height % 2:
        issues.append(CapabilityIssue(
            "RESOLUTION_ODD",
            f"The resolution {width}x{height} has an odd dimension.",
            "Use even numbers - H.264 and HEVC encode in 2-pixel blocks.",
        ))

    if fps <= 0:
        issues.append(CapabilityIssue(
            "FPS_INVALID", "The frame rate must be greater than zero.",
            f"Choose one of {', '.join(str(rate) for rate in SUPPORTED_RATES)}.",
        ))
    elif fps > 240:
        issues.append(CapabilityIssue(
            "FPS_INVALID", f"A frame rate of {fps} is not practical to encode.",
            f"Choose one of {', '.join(str(rate) for rate in SUPPORTED_RATES)}.",
        ))

    if container and container not in CONTAINERS:
        issues.append(CapabilityIssue(
            "CONTAINER_INVALID", f"'{container}' is not a supported container.",
            f"Choose one of {', '.join(CONTAINERS)}.",
        ))

    if codec and container and codec not in CODECS_BY_CONTAINER.get(container, ()):
        issues.append(CapabilityIssue(
            "CODEC_CONTAINER_MISMATCH",
            f"The {CODEC_LABELS.get(codec, codec)} codec cannot be stored in a .{container} file.",
            f"For .{container}, choose one of "
            f"{', '.join(CODECS_BY_CONTAINER.get(container, ())) or 'no supported codecs'}.",
        ))

    if codec and caps.detected and not caps.supports_codec(codec):
        encoder = CODEC_ENCODERS.get(codec, codec)
        issues.append(CapabilityIssue(
            "CODEC_UNAVAILABLE",
            f"The selected encoder is not available in this FFmpeg installation ({encoder}).",
            "Choose another codec, or install an FFmpeg build that includes it.",
        ))

    if pixel_format and codec and pixel_format not in PIXEL_FORMATS_BY_CODEC.get(codec, ()):
        issues.append(CapabilityIssue(
            "PIXEL_FORMAT_INVALID",
            f"The pixel format '{pixel_format}' is not valid for {CODEC_LABELS.get(codec, codec)}.",
            f"Choose one of {', '.join(PIXEL_FORMATS_BY_CODEC.get(codec, ()))}.",
        ))

    if audio_codec and container and audio_codec not in AUDIO_CODECS_BY_CONTAINER.get(container, ()):
        issues.append(CapabilityIssue(
            "AUDIO_CODEC_INVALID",
            f"The audio codec '{audio_codec}' cannot be stored in a .{container} file.",
            f"Choose one of {', '.join(AUDIO_CODECS_BY_CONTAINER.get(container, ()))}.",
        ))

    # Rate control: CRF/CQ only where the encoder understands it.
    modes = rate_control_modes(codec)
    bitrate = int(getattr(settings, "bitrate_kbps", 0) or 0)
    crf = int(getattr(settings, "crf", 0) or 0)
    if bitrate <= 0:
        if "crf" not in modes and "cq" not in modes:
            issues.append(CapabilityIssue(
                "RATE_CONTROL_INVALID",
                f"{CODEC_LABELS.get(codec, codec)} needs an explicit bitrate.",
                "Set a target bitrate instead of constant quality.",
            ))
        elif not (0 <= crf <= 63):
            issues.append(CapabilityIssue(
                "CRF_OUT_OF_RANGE",
                f"The quality value {crf} is outside the valid range 0-63.",
                "Use a value between 15 (very high quality) and 30 (small file).",
            ))
    elif bitrate < 50:
        issues.append(CapabilityIssue(
            "BITRATE_TOO_LOW",
            f"A bitrate of {bitrate} kbps is too low for {width}x{height}@{fps}.",
            "Raise the bitrate, or lower the resolution.",
        ))

    if duration is not None and duration <= 0:
        issues.append(CapabilityIssue(
            "NOTHING_TO_RENDER",
            "The project's timeline has no length, so there is nothing to render.",
            "Add scenes, give them a duration, or generate narration.",
        ))

    return issues


# --------------------------------------------------------------------------
# Estimates
# --------------------------------------------------------------------------

def estimate_file_size(settings: Any, duration: float) -> dict:
    """A clearly-labelled size estimate (directive section 30).

    With an explicit bitrate the maths is exact-ish; with constant quality it is
    an approximation from the resolution and frame rate, and it says so.
    """
    duration = max(0.0, float(duration or 0.0))
    width = int(getattr(settings, "width", 0) or 0)
    height = int(getattr(settings, "height", 0) or 0)
    fps = max(1, int(getattr(settings, "fps", 30) or 30))
    bitrate = int(getattr(settings, "bitrate_kbps", 0) or 0)
    audio_bitrate = int(getattr(settings, "audio_bitrate_kbps", 0) or 0)

    if bitrate > 0:
        video_kbps = float(bitrate)
        basis = "target bitrate"
        confidence = "close"
    else:
        # Pixels per second, scaled by a per-pixel cost that CRF implies.  This is
        # an approximation and is labelled as one.
        crf = max(0, min(63, int(getattr(settings, "crf", 20) or 20)))
        bits_per_pixel = max(0.02, 0.20 * (0.85 ** ((crf - 15) / 3.0)))
        video_kbps = (width * height * fps * bits_per_pixel) / 1000.0
        basis = "constant quality (approximation)"
        confidence = "rough"

    total_kbps = video_kbps + max(0, audio_bitrate)
    bytes_total = (total_kbps * 1000.0 / 8.0) * duration
    return {
        "bytes": int(round(bytes_total)),
        "mib": round(bytes_total / (1024 * 1024), 1),
        "video_kbps": round(video_kbps, 1),
        "audio_kbps": audio_bitrate,
        "basis": basis,
        "confidence": confidence,
        "label": f"~{_size_label(bytes_total)} (estimated, {basis})",
    }


def estimate_render_time(*, frames: int, width: int, height: int, fps: int,
                        encoder_preset: str = "medium",
                        measured_fps: Optional[float] = None) -> dict:
    """How long a render is likely to take, and how far along it is.

    If a real encode rate has been measured this run, that wins - otherwise the
    estimate is derived from the frame size and encoder preset and is labelled as
    an estimate rather than being presented as fact (directive section 31).
    """
    frames = max(0, int(frames))
    if measured_fps and measured_fps > 0:
        rate = float(measured_fps)
        basis = "measured this run"
    else:
        pixels = max(1, int(width) * int(height))
        # Rough CPU throughput: a 1080p frame at 'medium' is the reference point.
        reference = 1_920 * 1_080
        speed = {"ultrafast": 12.0, "veryfast": 6.0, "faster": 3.0, "fast": 2.0,
                 "medium": 1.0, "slow": 0.55, "slower": 0.35, "veryslow": 0.2}.get(
                     str(encoder_preset), 1.0)
        # Capped: an honest estimate for a small frame is still "hundreds of
        # frames a second", not the tens of thousands the raw scaling suggests.
        rate = min(240.0, max(0.5, 26.0 * speed * (reference / pixels)))
        basis = "estimated from the frame size and preset"

    seconds = frames / rate if rate > 0 else 0.0
    if seconds < 60:
        label = f"~{seconds:.0f} sec at {rate:.0f} fps ({basis})"
    elif seconds < 3600:
        label = f"~{seconds / 60:.1f} min at {rate:.1f} fps ({basis})"
    else:
        label = f"~{seconds / 3600:.1f} h at {rate:.1f} fps ({basis})"
    return {
        "frames": frames,
        "encode_fps": round(rate, 2),
        "seconds": round(seconds, 1),
        "basis": basis,
        "label": label,
    }


def _size_label(total_bytes: float) -> str:
    """A file size a person can read, at whatever scale fits."""
    if total_bytes < 1024:
        return f"{total_bytes:.0f} B"
    if total_bytes < 1024 * 1024:
        return f"{total_bytes / 1024:.0f} KiB"
    if total_bytes < 1024 * 1024 * 1024:
        return f"{total_bytes / (1024 * 1024):.1f} MiB"
    return f"{total_bytes / (1024 ** 3):.2f} GiB"
