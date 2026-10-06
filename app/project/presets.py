"""Reusable presets: formats, resolutions, quality, templates, channels.

Two rules shape this module (directive sections 25, 26, 27):

* **Presets are recipes, not the project.**  A project stores the *resolved*
  numbers (resolution, fps, codec, CRF, pixel format, audio bitrate, ...), never
  just the word "High".  Changing a preset later therefore never silently
  changes an existing project.
* **Nothing fake is offered.**  Every option listed here is something the CPU
  FFmpeg build on the target machine can actually do, and the tables are also
  used by :mod:`app.project.validation`, so the UI cannot offer a combination
  that validation would then reject.

No I/O happens at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------
# Containers, codecs and the combinations that actually work
# --------------------------------------------------------------------------

#: Output containers offered by the application.
CONTAINERS: tuple[str, ...] = ("mp4", "mkv", "mov", "webm")

#: Video codec ids.  The suffix shows where the encoding happens: the target
#: machine has no dedicated GPU, so the CPU encoders are the defaults.
VIDEO_CODECS: tuple[str, ...] = ("h264_cpu", "hevc_cpu", "vp9_cpu")

#: Codec id -> the real FFmpeg encoder name.
CODEC_ENCODERS: dict[str, str] = {
    "h264_cpu": "libx264",
    "hevc_cpu": "libx265",
    "vp9_cpu": "libvpx-vp9",
}

#: Codec id -> the codec name a finished file reports.  FFprobe names the stream
#: ("h264"), not the encoder that made it ("libx264"), so verifying a render
#: needs this translation.  Without it a correct render would look wrong.
CODEC_STREAM_NAMES: dict[str, str] = {
    "h264_cpu": "h264",
    "hevc_cpu": "hevc",
    "vp9_cpu": "vp9",
}

#: Encoders that accept ``-pass 1`` / ``-pass 2``.  Asking an encoder that does
#: not support multi-pass to do two passes fails expensively, so it is checked
#: before any frame is drawn.
TWO_PASS_ENCODERS: tuple[str, ...] = ("libx264", "libx265", "libvpx-vp9")

CODEC_LABELS: dict[str, str] = {
    "h264_cpu": "H.264 (CPU, x264) - plays everywhere",
    "hevc_cpu": "H.265 / HEVC (CPU) - smaller files, slower",
    "vp9_cpu": "VP9 (CPU) - for WebM, slow without a GPU",
}

#: Which video codecs may live in which container.
CODECS_BY_CONTAINER: dict[str, tuple[str, ...]] = {
    "mp4": ("h264_cpu", "hevc_cpu"),
    "mkv": ("h264_cpu", "hevc_cpu", "vp9_cpu"),
    "mov": ("h264_cpu", "hevc_cpu"),
    "webm": ("vp9_cpu",),
}

#: Pixel formats per codec.  ``yuv420p`` first = the safe default.
PIXEL_FORMATS_BY_CODEC: dict[str, tuple[str, ...]] = {
    "h264_cpu": ("yuv420p", "yuv422p", "yuv444p"),
    "hevc_cpu": ("yuv420p", "yuv420p10le", "yuv422p10le"),
    "vp9_cpu": ("yuv420p", "yuv444p"),
}

#: x264/x265 speed presets, fastest first.
ENCODER_PRESETS: tuple[str, ...] = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)

#: CRF range per codec (constant quality mode).  Lower is better quality.
CRF_RANGES: dict[str, tuple[int, int]] = {
    "h264_cpu": (0, 51),
    "hevc_cpu": (0, 51),
    "vp9_cpu": (0, 63),
}

#: Audio codecs per container.
AUDIO_CODECS_BY_CONTAINER: dict[str, tuple[str, ...]] = {
    "mp4": ("aac", "mp3"),
    "mkv": ("aac", "mp3", "opus", "flac"),
    "mov": ("aac", "mp3"),
    "webm": ("opus",),
}

#: Sample rates FFmpeg accepts without resampling complaints.
SAMPLE_RATES: tuple[int, ...] = (22050, 24000, 32000, 44100, 48000)

#: Bitrate range in kbps that makes sense for stereo narration + music.
AUDIO_BITRATE_RANGE: tuple[int, int] = (64, 512)

#: Video bitrate range in kbps for the explicit-bitrate (non CRF) mode.
VIDEO_BITRATE_RANGE: tuple[int, int] = (300, 200000)

#: Keyframe interval in seconds (GOP).  ``0`` lets the encoder decide.
KEYFRAME_INTERVAL_RANGE: tuple[int, int] = (0, 300)


def codec_in_container(codec: str, container: str) -> bool:
    return codec in CODECS_BY_CONTAINER.get(container, ())


def audio_codec_in_container(audio_codec: str, container: str) -> bool:
    return audio_codec in AUDIO_CODECS_BY_CONTAINER.get(container, ())


def default_audio_codec(container: str) -> str:
    return AUDIO_CODECS_BY_CONTAINER.get(container, ("aac",))[0]


def stream_codec_name(codec_id: str) -> str:
    """The codec name a finished file reports for one of our codec ids."""
    return CODEC_STREAM_NAMES.get(str(codec_id), str(codec_id))


def default_codec(container: str) -> str:
    return CODECS_BY_CONTAINER.get(container, ("h264_cpu",))[0]


def default_pixel_format(codec: str) -> str:
    return PIXEL_FORMATS_BY_CODEC.get(codec, ("yuv420p",))[0]


# --------------------------------------------------------------------------
# Aspect ratios and resolutions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AspectPreset:
    """One selectable aspect ratio and the resolutions that belong to it."""

    key: str
    label: str
    ratio: tuple[int, int]
    resolutions: tuple[tuple[int, int], ...]
    note: str = ""

    @property
    def default_resolution(self) -> tuple[int, int]:
        return self.resolutions[0]


ASPECT_PRESETS: tuple[AspectPreset, ...] = (
    AspectPreset(
        key="16:9",
        label="Landscape 16:9",
        ratio=(16, 9),
        resolutions=((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)),
        note="YouTube, desktop players, most websites.",
    ),
    AspectPreset(
        key="9:16",
        label="Vertical 9:16",
        ratio=(9, 16),
        resolutions=((720, 1280), (1080, 1920)),
        note="YouTube Shorts, Instagram Reels, TikTok.",
    ),
    AspectPreset(
        key="1:1",
        label="Square 1:1",
        ratio=(1, 1),
        resolutions=((720, 720), (1080, 1080)),
        note="Instagram feed posts.",
    ),
    AspectPreset(
        key="4:5",
        label="Portrait 4:5",
        ratio=(4, 5),
        resolutions=((864, 1080), (1080, 1350)),
        note="Instagram feed, more screen than a square.",
    ),
    AspectPreset(
        key="4:3",
        label="Classic 4:3",
        ratio=(4, 3),
        resolutions=((1024, 768), (1440, 1080)),
        note="Retro look and some presentation exports.",
    ),
)

ASPECT_KEYS: tuple[str, ...] = tuple(preset.key for preset in ASPECT_PRESETS)

#: Custom sizes are always allowed; this is the range validation accepts.
MIN_DIMENSION = 256
MAX_DIMENSION = 7680


def aspect_preset(key: str) -> Optional[AspectPreset]:
    for preset in ASPECT_PRESETS:
        if preset.key == key:
            return preset
    return None


def aspect_ratio_label(width: int, height: int) -> str:
    """Return the matching preset label, or the reduced ratio, or ``custom``."""
    if width <= 0 or height <= 0:
        return "custom"
    for preset in ASPECT_PRESETS:
        rw, rh = preset.ratio
        if width * rh == height * rw:
            return preset.key
    from math import gcd

    divisor = gcd(width, height) or 1
    return f"{width // divisor}:{height // divisor}"


FPS_OPTIONS: tuple[int, ...] = (24, 25, 30, 50, 60)

FPS_LABELS: dict[int, str] = {
    24: "24 fps - film look",
    25: "25 fps - PAL regions",
    30: "30 fps - standard for the web",
    50: "50 fps - smooth, PAL",
    60: "60 fps - very smooth, larger files",
}


# --------------------------------------------------------------------------
# Quality presets (resolved settings, never just a name)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityPreset:
    """A quality level expanded into the settings that reproduce it."""

    key: str
    label: str
    encoder_preset: str
    crf: int
    #: Explicit bitrate in kbps; ``0`` means "use CRF (constant quality)".
    bitrate_kbps: int = 0
    audio_bitrate_kbps: int = 192
    pixel_format: str = "yuv420p"
    note: str = ""


QUALITY_PRESETS: tuple[QualityPreset, ...] = (
    QualityPreset("draft", "Draft", "veryfast", 30, 0, 128, note="Fastest. For checking timing and layout."),
    QualityPreset("low", "Low", "faster", 28, 0, 128, note="Quick review copies."),
    QualityPreset("medium", "Medium", "medium", 23, 0, 160, note="Good balance for most uploads."),
    QualityPreset("high", "High", "slow", 20, 0, 192, note="Recommended final quality."),
    QualityPreset("very_high", "Very High", "slower", 18, 0, 256, note="Noticeably slower encode, slightly better."),
    QualityPreset("ultra", "Ultra", "veryslow", 15, 0, 320, note="Maximum quality. Slow on CPU - allow time."),
)

QUALITY_KEYS: tuple[str, ...] = tuple(preset.key for preset in QUALITY_PRESETS) + ("custom",)

QUALITY_LABELS: dict[str, str] = {preset.key: preset.label for preset in QUALITY_PRESETS}
QUALITY_LABELS["custom"] = "Custom"


def quality_preset(key: str) -> Optional[QualityPreset]:
    for preset in QUALITY_PRESETS:
        if preset.key == key:
            return preset
    return None


def resolve_quality(
    key: str,
    container: str = "mp4",
    codec: str = "h264_cpu",
    custom: Optional[dict] = None,
) -> dict:
    """Expand a quality key into concrete encoder settings.

    ``custom`` (a partial dict) fills in the values the user changed, so a
    "Custom" project still stores real numbers rather than the word "custom".
    """
    preset = quality_preset(key)
    audio_codec = default_audio_codec(container)
    values: dict = {
        "quality_preset": key if preset is not None else "custom",
        "codec": codec,
        "encoder_preset": preset.encoder_preset if preset else "medium",
        "crf": preset.crf if preset else 23,
        "bitrate_kbps": preset.bitrate_kbps if preset else 0,
        "pixel_format": preset.pixel_format if preset else default_pixel_format(codec),
        "audio_codec": audio_codec,
        "audio_bitrate_kbps": preset.audio_bitrate_kbps if preset else 192,
        "sample_rate": 48000,
        "container": container,
    }
    if preset is None or key == "custom":
        values.update({k: v for k, v in (custom or {}).items() if v is not None})
    return values


# --------------------------------------------------------------------------
# Project templates (presets, never content - directive section 41)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectTemplate:
    """Starting point for a new project.  Contains settings, never media."""

    key: str
    label: str
    description: str
    aspect: str = "16:9"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    quality: str = "high"
    background: str = "#101014"
    accent: str = "#4c8dff"
    heading_font: str = "DejaVu Sans"
    body_font: str = "DejaVu Sans"
    subtitles_enabled: bool = False
    subtitle_font_size: int = 44
    filename_template: str = "{name}_{seq}"
    script_note: str = ""


PROJECT_TEMPLATES: tuple[ProjectTemplate, ...] = (
    ProjectTemplate(
        key="blank",
        label="Blank",
        description="An empty project. Nothing is added for you.",
    ),
    ProjectTemplate(
        key="youtube",
        label="YouTube",
        description="16:9 landscape, 1080p, 30 fps, subtitles off.",
        quality="high",
        filename_template="{name}_{seq}",
        script_note="Write one idea per paragraph. Each paragraph becomes a scene later.",
    ),
    ProjectTemplate(
        key="shorts",
        label="Shorts / Reels",
        description="9:16 vertical, 1080x1920, 30 fps, larger subtitles.",
        aspect="9:16",
        width=1080,
        height=1920,
        quality="high",
        subtitle_font_size=58,
        filename_template="{name}_short_{seq}",
        script_note="Hook in the first line. Keep it under 60 seconds.",
    ),
    ProjectTemplate(
        key="educational",
        label="Educational",
        description="16:9, 1080p, 25 fps, subtitles on for accessibility.",
        fps=25,
        subtitles_enabled=True,
        filename_template="{name}_lesson_{seq}",
        script_note="Start with the question this video answers.",
    ),
    ProjectTemplate(
        key="finance",
        label="Finance",
        description="16:9, 1080p, 30 fps, subtitles on, high quality for charts.",
        quality="very_high",
        subtitles_enabled=True,
        accent="#19b36b",
        filename_template="{name}_finance_{seq}",
        script_note="State the numbers plainly, then explain what they mean.",
    ),
    ProjectTemplate(
        key="storytelling",
        label="Storytelling",
        description="16:9, 1080p, 24 fps for a film look.",
        fps=24,
        quality="high",
        background="#0d0f14",
        accent="#e0a458",
        filename_template="{name}_story_{seq}",
        script_note="Open in the middle of the action.",
    ),
    ProjectTemplate(
        key="custom",
        label="Custom",
        description="Choose every setting yourself on the next pages.",
    ),
)


def project_template(key: str) -> Optional[ProjectTemplate]:
    for template in PROJECT_TEMPLATES:
        if template.key == key:
            return template
    return None


# --------------------------------------------------------------------------
# Export filename templates
# --------------------------------------------------------------------------

FILENAME_TEMPLATE_FIELDS: tuple[str, ...] = ("name", "channel", "date", "time", "seq", "quality", "resolution")

FILENAME_TEMPLATE_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("{name}_{seq}", "My Video_1.mp4"),
    ("{channel}_{name}_{date}", "MyChannel_My Video_2026-10-05.mp4"),
    ("{name}_{quality}_{seq}", "My Video_high_1.mp4"),
)

#: Characters Windows refuses in a file name.
INVALID_FILENAME_CHARACTERS = '<>:"/\\|?*'


def preview_filename(template: str, name: str = "My Video", channel: str = "My Channel", seq: int = 1) -> str:
    """Render a filename template with example values (for the UI preview)."""
    from datetime import datetime

    now = datetime.now()
    values = {
        "name": name or "Untitled",
        "channel": channel or "Channel",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H%M"),
        "seq": str(seq),
        "quality": "high",
        "resolution": "1920x1080",
    }
    text = template or "{name}_{seq}"
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    for character in INVALID_FILENAME_CHARACTERS:
        text = text.replace(character, "_")
    return f"{text.strip()}.mp4"


# --------------------------------------------------------------------------
# Channel profiles (defaults only - a project never depends on one)
# --------------------------------------------------------------------------

@dataclass
class ChannelProfile:
    """Reusable defaults for one channel/brand.

    A project stores the channel *id* plus its own resolved settings, so a
    channel can be edited or deleted without breaking finished projects
    (directive section 6).
    """

    id: str = ""
    name: str = ""
    logo_path: str = ""
    watermark_path: str = ""
    background: str = "#101014"
    accent: str = "#4c8dff"
    heading_font: str = "DejaVu Sans"
    body_font: str = "DejaVu Sans"
    theme_id: str = "clean-dark"
    voice: str = ""
    language: str = "en-us"
    music_path: str = ""
    subtitles_enabled: bool = False
    subtitle_font_size: int = 44
    aspect: str = "16:9"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    quality: str = "high"
    filename_template: str = "{channel}_{name}_{seq}"
    notes: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelProfile":
        from dataclasses import fields

        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


BUILT_IN_CHANNEL = ChannelProfile(
    id="default",
    name="Default channel",
    notes="Used when no channel profile is selected.",
)



# --------------------------------------------------------------------------
# Explanations shown next to the options in the interface
# --------------------------------------------------------------------------

#: Why a container is offered.  Shown as a tooltip, so it must be plain words.
CONTAINER_NOTES: dict[str, str] = {
    "mp4": "Widest compatibility - plays on phones, browsers and editors.",
    "mov": "Apple editing software prefers this. Larger than MP4.",
    "webm": "Smaller files for the web, but fewer players support it.",
    "mkv": "Flexible, but not all social platforms accept it.",
}

#: Honest expectations for each quality level on a CPU-only machine.
QUALITY_NOTES: dict[str, str] = {
    "draft": "Fastest encode. Use it while you are still editing.",
    "standard": "A reasonable balance for most videos.",
    "high": "Recommended for uploads. Noticeably slower on a CPU.",
    "ultra": "Largest files and the slowest encode. Only for final delivery.",
    "custom": "You set the encoder values yourself - nothing is adjusted silently.",
}

#: Brand colours offered on the Theme page.  These are starting points only;
#: a project always keeps its own copy.
CHANNEL_COLORS: dict[str, str] = {
    "Blue": "#5b9cff",
    "Red": "#e5484d",
    "Green": "#30a46c",
    "Amber": "#f5a623",
    "Violet": "#8e4ec6",
    "Slate": "#6b7280",
}

__all__ = [
    "QUALITY_NOTES",
    "CONTAINER_NOTES",
    "CHANNEL_COLORS",
    "ASPECT_KEYS",
    "ASPECT_PRESETS",
    "AUDIO_BITRATE_RANGE",
    "AUDIO_CODECS_BY_CONTAINER",
    "AspectPreset",
    "BUILT_IN_CHANNEL",
    "ChannelProfile",
    "CODECS_BY_CONTAINER",
    "CODEC_ENCODERS",
    "CODEC_LABELS",
    "CODEC_STREAM_NAMES",
    "TWO_PASS_ENCODERS",
    "CONTAINERS",
    "CRF_RANGES",
    "ENCODER_PRESETS",
    "FILENAME_TEMPLATE_EXAMPLES",
    "FILENAME_TEMPLATE_FIELDS",
    "FPS_LABELS",
    "FPS_OPTIONS",
    "INVALID_FILENAME_CHARACTERS",
    "KEYFRAME_INTERVAL_RANGE",
    "MAX_DIMENSION",
    "MIN_DIMENSION",
    "PIXEL_FORMATS_BY_CODEC",
    "PROJECT_TEMPLATES",
    "ProjectTemplate",
    "QUALITY_KEYS",
    "QUALITY_LABELS",
    "QUALITY_PRESETS",
    "QualityPreset",
    "SAMPLE_RATES",
    "VIDEO_BITRATE_RANGE",
    "VIDEO_CODECS",
    "aspect_preset",
    "aspect_ratio_label",
    "audio_codec_in_container",
    "codec_in_container",
    "default_audio_codec",
    "default_codec",
    "default_pixel_format",
    "preview_filename",
    "project_template",
    "quality_preset",
    "resolve_quality",
]
