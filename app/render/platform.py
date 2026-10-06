"""Export platform presets (Stage E, section 29).

A platform preset is a starting point, not a rule: applying one fills in the
export settings and the user can then change any of them.  Presets live in the
project file, so a project keeps the preset it was built for even if this table
changes in a later version - and a preset the user has edited is never silently
reset (directive sections 26, 29).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "PlatformPreset",
    "PLATFORM_PRESETS",
    "PLATFORM_KEYS",
    "platform_preset",
    "platform_from_dict",
    "platforms_from_project",
    "apply_platform",
    "describe_platform",
]


@dataclass
class PlatformPreset:
    """One export target and the settings that suit it."""

    key: str
    label: str
    width: int
    height: int
    fps: int = 30
    quality_preset: str = "high"
    codec: str = "h264_cpu"
    container: str = "mp4"
    #: Explicit bitrate in kbps, or 0 to use the quality preset's CRF.
    bitrate_kbps: int = 0
    audio_bitrate_kbps: int = 192
    pixel_format: str = "yuv420p"
    encoder_preset: str = "slow"
    note: str = ""
    #: True when the user has changed this preset in this project.
    edited: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "width": self.width,
            "height": self.height, "fps": self.fps,
            "quality_preset": self.quality_preset, "codec": self.codec,
            "container": self.container, "bitrate_kbps": self.bitrate_kbps,
            "audio_bitrate_kbps": self.audio_bitrate_kbps,
            "pixel_format": self.pixel_format,
            "encoder_preset": self.encoder_preset, "note": self.note,
            "edited": self.edited,
        }

    @property
    def resolution_label(self) -> str:
        return f"{self.width}x{self.height}"

    def describe(self) -> str:
        rate = f"{self.bitrate_kbps} kbps" if self.bitrate_kbps else self.quality_preset
        return f"{self.label} - {self.resolution_label} @ {self.fps} fps, {rate}"


PLATFORM_PRESETS: tuple[PlatformPreset, ...] = (
    PlatformPreset(
        key="youtube_1080", label="YouTube 1080p", width=1920, height=1080, fps=30,
        quality_preset="high", bitrate_kbps=0,
        note="The default for most uploads. Good quality at a reasonable size.",
    ),
    PlatformPreset(
        key="youtube_4k", label="YouTube 4K", width=3840, height=2160, fps=30,
        quality_preset="very_high",
        note="Maximum quality. Slow to encode on a CPU and produces large files.",
    ),
    PlatformPreset(
        key="shorts", label="YouTube Shorts", width=1080, height=1920, fps=30,
        quality_preset="high",
        note="Vertical video for Shorts. Keep important content away from the edges.",
    ),
    PlatformPreset(
        key="reels", label="Instagram Reels", width=1080, height=1350, fps=30,
        quality_preset="high",
        note="Portrait 4:5, which fills more of the feed than a square.",
    ),
    PlatformPreset(
        key="tiktok", label="TikTok", width=1080, height=1920, fps=30,
        quality_preset="high",
        note="Vertical video for TikTok.",
    ),
    PlatformPreset(
        key="square", label="Square 1080", width=1080, height=1080, fps=30,
        quality_preset="high",
        note="Square video for feed posts.",
    ),
    PlatformPreset(
        key="master", label="Master archive", width=1920, height=1080, fps=30,
        quality_preset="ultra", bitrate_kbps=0,
        note="The highest quality this project can produce. Keep this as your archive copy.",
    ),
    PlatformPreset(
        key="draft", label="Draft check", width=1280, height=720, fps=30,
        quality_preset="draft",
        note="Fast, small and rough - for checking timing and layout.",
    ),
)

PLATFORM_KEYS: tuple[str, ...] = tuple(preset.key for preset in PLATFORM_PRESETS)


def platform_preset(key: str) -> Optional[PlatformPreset]:
    for preset in PLATFORM_PRESETS:
        if preset.key == str(key):
            return preset
    return None


def platform_from_dict(data: Any) -> PlatformPreset:
    """Rebuild a preset from the project file, keeping the user's edits."""
    if not isinstance(data, dict):
        return PlatformPreset(key="custom", label="Custom", width=1920, height=1080)
    base = platform_preset(str(data.get("key", "")))
    values = base.to_dict() if base is not None else {
        "key": "custom", "label": "Custom", "width": 1920, "height": 1080,
        "fps": 30, "quality_preset": "high", "codec": "h264_cpu",
        "container": "mp4", "bitrate_kbps": 0, "audio_bitrate_kbps": 192,
        "pixel_format": "yuv420p", "encoder_preset": "slow", "note": "",
        "edited": True,
    }
    for name in ("key", "label", "note", "quality_preset", "codec", "container",
                 "pixel_format", "encoder_preset"):
        if data.get(name) is not None:
            values[name] = str(data[name])
    for name in ("width", "height", "fps", "bitrate_kbps", "audio_bitrate_kbps"):
        if data.get(name) is not None:
            try:
                values[name] = int(data[name])
            except (TypeError, ValueError):
                pass
    values["edited"] = bool(data.get("edited", base is None))
    return PlatformPreset(**values)


def platforms_from_project(project: Any) -> list[PlatformPreset]:
    """The project's preset list, falling back to the built-in table."""
    extra = getattr(getattr(project, "export", None), "extra", None)
    stored = (extra or {}).get("platform_presets") if isinstance(extra, dict) else None
    if isinstance(stored, list) and stored:
        return [platform_from_dict(item) for item in stored]
    return [platform_from_dict(preset.to_dict()) for preset in PLATFORM_PRESETS]


def apply_platform(project: Any, preset: PlatformPreset) -> dict:
    """Copy a preset's settings into the project's export format.

    Returns what changed so the UI can say so, instead of quietly rewriting the
    user's settings.
    """
    settings = project.format
    changes: dict[str, tuple[Any, Any]] = {}
    for name in ("width", "height", "fps", "quality_preset", "codec", "container",
                 "bitrate_kbps", "audio_bitrate_kbps", "pixel_format",
                 "encoder_preset"):
        new_value = getattr(preset, name)
        old_value = getattr(settings, name, None)
        if old_value != new_value:
            changes[name] = (old_value, new_value)
            setattr(settings, name, new_value)
    return changes


def describe_platform(preset: PlatformPreset) -> str:
    return preset.describe()
