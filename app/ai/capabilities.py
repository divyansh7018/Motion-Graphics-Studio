"""One capability vocabulary for every AI backend (sections 5, 20, 45).

Image backends, video backends, upscalers and background removers all answer
the same question - *what can you do?* - so they answer it with the same shape.
The GUI reads this object and hides what is not there; a control that pretends a
feature works is worse than a greyed-out box (section 5).

The flags are ``False`` by default.  A backend has to opt in, so a new adapter
cannot accidentally advertise something it has not implemented, and the UI's
default state for an unknown backend is "cannot do that" with an explanation
rather than a button that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..image.capabilities import ImageCapabilities

__all__ = [
    "AICapabilities",
    "FEATURE_ORDER",
    "FEATURE_LABELS",
    "SETTING_ORDER",
    "SETTING_LABELS",
]

#: Every feature a backend can report, in the order a user meets them.  The
#: names match the ones in the directive's example capability object exactly, so
#: a report printed from this code and a report printed from the directive read
#: the same.
FEATURE_ORDER: tuple[str, ...] = (
    "text_to_image",
    "image_to_image",
    "inpaint",
    "outpaint",
    "upscale",
    "background_removal",
    "text_to_video",
    "image_to_video",
    "video_to_video",
    "video_extend",
    "storyboard_to_video",
    "reference_image",
    "style_reference",
    "control",
    "lora",
    "camera_control",
    "audio_track",
)

FEATURE_LABELS: dict[str, str] = {
    "text_to_image": "Text to image",
    "image_to_image": "Image to image",
    "inpaint": "Inpainting",
    "outpaint": "Outpainting",
    "upscale": "Upscaling",
    "background_removal": "Background removal",
    "text_to_video": "Text to video",
    "image_to_video": "Image to video",
    "video_to_video": "Video to video",
    "video_extend": "Extend a clip",
    "storyboard_to_video": "Storyboard to video",
    "reference_image": "Reference image",
    "style_reference": "Style reference",
    "control": "ControlNet-style conditioning",
    "lora": "LoRA weights",
    "camera_control": "Camera movement control",
    "audio_track": "Generated audio",
}

#: Settings a backend may or may not expose.  The UI hides what is unsupported
#: rather than sending a value that will be ignored (section 5).
SETTING_ORDER: tuple[str, ...] = (
    "negative_prompt", "seed_control", "steps", "guidance", "sampler", "batch",
    "strength", "duration", "fps", "resolution_control", "quality",
    "device_choice", "dtype_choice", "offload",
)

SETTING_LABELS: dict[str, str] = {
    "negative_prompt": "Negative prompt",
    "seed_control": "Seed control",
    "steps": "Steps",
    "guidance": "Guidance / CFG",
    "sampler": "Sampler",
    "batch": "Batch count",
    "strength": "Strength",
    "duration": "Duration",
    "fps": "Frame rate",
    "resolution_control": "Resolution",
    "quality": "Quality",
    "device_choice": "Device choice",
    "dtype_choice": "Precision choice",
    "offload": "CPU / sequential offload",
}


@dataclass
class AICapabilities:
    """What one backend can do, for every kind of backend."""

    # -- image -------------------------------------------------------------
    text_to_image: bool = False
    image_to_image: bool = False
    inpaint: bool = False
    outpaint: bool = False
    upscale: bool = False
    background_removal: bool = False

    # -- video -------------------------------------------------------------
    text_to_video: bool = False
    image_to_video: bool = False
    video_to_video: bool = False
    video_extend: bool = False
    storyboard_to_video: bool = False
    camera_control: bool = False
    audio_track: bool = False

    # -- conditioning ------------------------------------------------------
    reference_image: bool = False
    style_reference: bool = False
    control: bool = False
    lora: bool = False

    # -- settings the UI may offer -----------------------------------------
    negative_prompt: bool = False
    seed_control: bool = False
    steps: bool = False
    guidance: bool = False
    sampler: bool = False
    batch: bool = False
    strength: bool = False
    duration: bool = False
    fps: bool = False
    resolution_control: bool = False
    quality: bool = False
    device_choice: bool = False
    dtype_choice: bool = False
    offload: bool = False

    # -- limits ------------------------------------------------------------
    #: Camera movements this backend understands, e.g. ["pan", "zoom"].
    camera_moves: list = field(default_factory=list)
    max_dimension: int = 0
    min_dimension: int = 0
    dimension_multiple: int = 0
    max_batch: int = 1
    #: Video only; 0 means "no limit reported".
    max_duration: float = 0.0
    min_duration: float = 0.0
    max_fps: int = 0
    supports_alpha: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict)

    # -- queries -----------------------------------------------------------

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, str(feature), False))

    def supports_setting(self, name: str) -> bool:
        return bool(getattr(self, str(name), False))

    def features(self) -> list[str]:
        return [name for name in FEATURE_ORDER if self.supports(name)]

    def missing_features(self) -> list[str]:
        return [name for name in FEATURE_ORDER if not self.supports(name)]

    def settings(self) -> list[str]:
        return [name for name in SETTING_ORDER if self.supports_setting(name)]

    def unsupported_note(self, feature: str) -> str:
        """Why a control is disabled - the sentence the UI shows beside it."""
        label = FEATURE_LABELS.get(feature, feature)
        if self.supports(feature):
            return ""
        return (f"{label} is not available from this backend. "
                f"{self.notes}".strip())

    def describe(self) -> str:
        found = self.features()
        if not found:
            return "No generation features reported."
        return ", ".join(FEATURE_LABELS.get(name, name) for name in found)

    def limits_label(self) -> str:
        parts: list[str] = []
        if self.min_dimension and self.max_dimension:
            parts.append(f"{self.min_dimension}-{self.max_dimension} px")
        elif self.max_dimension:
            parts.append(f"up to {self.max_dimension} px")
        if self.dimension_multiple:
            parts.append(f"multiples of {self.dimension_multiple}")
        if self.max_batch > 1:
            parts.append(f"batch up to {self.max_batch}")
        if self.max_duration:
            parts.append(f"up to {self.max_duration:g}s")
        if self.max_fps:
            parts.append(f"up to {self.max_fps} fps")
        return "; ".join(parts) if parts else "no limits reported"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {name: self.supports(name) for name in FEATURE_ORDER}
        for name in SETTING_ORDER:
            data[name] = self.supports_setting(name)
        data.update({
            "camera_moves": [str(item) for item in self.camera_moves],
            "max_dimension": int(self.max_dimension or 0),
            "min_dimension": int(self.min_dimension or 0),
            "dimension_multiple": int(self.dimension_multiple or 0),
            "max_batch": int(self.max_batch or 1),
            "max_duration": float(self.max_duration or 0.0),
            "min_duration": float(self.min_duration or 0.0),
            "max_fps": int(self.max_fps or 0),
            "supports_alpha": bool(self.supports_alpha),
            "notes": str(self.notes or ""),
        })
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AICapabilities":
        known = set(FEATURE_ORDER) | set(SETTING_ORDER) | {
            "camera_moves", "max_dimension", "min_dimension", "dimension_multiple",
            "max_batch", "max_duration", "min_duration", "max_fps",
            "supports_alpha", "notes", "extra",
        }
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in known}
        return cls(**payload)

    # -- construction from the two lower-level contracts -------------------

    @classmethod
    def from_image(cls, capabilities: ImageCapabilities,
                   *, notes: str = "") -> "AICapabilities":
        """The image contract, widened into the AI contract.

        Nothing is added that the image backend did not report: the fields the
        image layer has no concept of (video, camera) stay ``False``.
        """
        return cls(
            text_to_image=bool(capabilities.text_to_image),
            image_to_image=bool(capabilities.image_to_image),
            inpaint=bool(capabilities.inpaint),
            outpaint=bool(capabilities.outpaint),
            upscale=bool(capabilities.upscale),
            reference_image=bool(capabilities.reference_image),
            style_reference=bool(capabilities.style_reference),
            control=bool(capabilities.control),
            lora=bool(capabilities.lora),
            negative_prompt=bool(capabilities.negative_prompt),
            seed_control=bool(capabilities.seed_control),
            steps=bool(capabilities.steps),
            guidance=bool(capabilities.guidance),
            sampler=bool(capabilities.sampler),
            batch=bool(capabilities.batch),
            strength=bool(capabilities.strength),
            max_dimension=int(capabilities.max_dimension or 0),
            min_dimension=int(capabilities.min_dimension or 0),
            dimension_multiple=int(capabilities.dimension_multiple or 0),
            max_batch=int(capabilities.max_batch or 1),
            supports_alpha=bool(capabilities.supports_alpha),
            notes=notes or str(capabilities.notes or ""),
            extra=dict(capabilities.extra or {}),
        )
