"""What an image backend can actually do (Stage F, sections 37, 4, 50).

Every capability here is a claim the backend makes about *itself*, and the UI
reads these flags to decide what to show.  A feature that is not reported is
disabled with an explanation, never faked: a control that appears to inpaint
when the model cannot is worse than a greyed-out box.

The names are stable strings rather than an enum so they survive a round trip
through a job payload or a log line unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ImageCapabilities",
    "FEATURES",
    "TEXT_TO_IMAGE",
    "IMAGE_TO_IMAGE",
    "INPAINT",
    "OUTPAINT",
    "UPSCALE",
    "CONTROL",
    "REFERENCE",
    "LORA",
    "FEATURE_LABELS",
]

TEXT_TO_IMAGE = "text_to_image"
IMAGE_TO_IMAGE = "image_to_image"
INPAINT = "inpaint"
OUTPAINT = "outpaint"
UPSCALE = "upscale"
CONTROL = "control"
REFERENCE = "reference_image"
LORA = "lora"

#: The feature names the directive names, in the order a user meets them.
FEATURES: tuple[str, ...] = (
    TEXT_TO_IMAGE,
    IMAGE_TO_IMAGE,
    INPAINT,
    OUTPAINT,
    UPSCALE,
    CONTROL,
    REFERENCE,
    LORA,
)

FEATURE_LABELS: dict[str, str] = {
    TEXT_TO_IMAGE: "Text to image",
    IMAGE_TO_IMAGE: "Image to image",
    INPAINT: "Inpainting",
    OUTPAINT: "Outpainting",
    UPSCALE: "Upscaling",
    CONTROL: "ControlNet-style conditioning",
    REFERENCE: "Reference image",
    LORA: "LoRA weights",
}

#: Settings a backend may or may not expose.  The UI hides what is unsupported
#: rather than sending a value that will be ignored.
SETTING_FLAGS: tuple[str, ...] = (
    "negative_prompt",
    "seed_control",
    "steps",
    "guidance",
    "sampler",
    "batch",
    "strength",
    "style_reference",
)


@dataclass
class ImageCapabilities:
    """One backend's honest description of itself.

    Everything defaults to ``False``/``0``: a backend must opt in to a feature,
    so a new adapter cannot accidentally advertise something it has not
    implemented.
    """

    # -- generation features ------------------------------------------------
    text_to_image: bool = False
    image_to_image: bool = False
    inpaint: bool = False
    outpaint: bool = False
    upscale: bool = False
    control: bool = False
    reference_image: bool = False
    lora: bool = False

    # -- settings the UI may offer -----------------------------------------
    negative_prompt: bool = False
    seed_control: bool = False
    steps: bool = False
    guidance: bool = False
    sampler: bool = False
    batch: bool = False
    strength: bool = False
    style_reference: bool = False

    # -- limits ------------------------------------------------------------
    #: Largest side the backend will produce.  0 means "unknown", which the UI
    #: reports as unknown rather than assuming a number.
    max_dimension: int = 0
    min_dimension: int = 0
    #: Multiples of this the width and height must be (8 or 64 for most
    #: diffusion models).  0 means no constraint.
    dimension_multiple: int = 0
    supports_alpha: bool = False
    #: Largest batch the backend will accept in one call.
    max_batch: int = 1

    #: Anything else the backend wants to say about itself.
    notes: str = ""
    extra: dict = field(default_factory=dict)

    # -- queries -----------------------------------------------------------

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, str(feature), False))

    def supports_setting(self, name: str) -> bool:
        return bool(getattr(self, str(name), False))

    def features(self) -> list[str]:
        """The features this backend really has, for the model manager."""
        return [name for name in FEATURES if self.supports(name)]

    def missing_features(self) -> list[str]:
        return [name for name in FEATURES if not self.supports(name)]

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
        return "; ".join(parts) if parts else "no limits reported"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {name: self.supports(name) for name in FEATURES}
        for name in SETTING_FLAGS:
            data[name] = self.supports_setting(name)
        data.update({
            "max_dimension": int(self.max_dimension or 0),
            "min_dimension": int(self.min_dimension or 0),
            "dimension_multiple": int(self.dimension_multiple or 0),
            "supports_alpha": bool(self.supports_alpha),
            "max_batch": int(self.max_batch or 1),
            "notes": str(self.notes or ""),
        })
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ImageCapabilities":
        """Rebuild capabilities from a dict, ignoring unknown keys."""
        known = {name for name in FEATURES} | set(SETTING_FLAGS) | {
            "max_dimension", "min_dimension", "dimension_multiple",
            "supports_alpha", "max_batch", "notes",
        }
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in known}
        return cls(**payload)
