"""The image-generation contract (Stage F, sections 2, 34, 36, 37, 69).

One abstract provider, and every backend - Diffusers, ComfyUI, a local command,
a local HTTP endpoint, an ONNX pipeline - implements the same six methods.  The
rest of the application only ever talks to this contract, so no part of it is
wired to one particular model.

Two rules are built into the shapes here rather than left to callers:

* a result is **never** a success unless a real file was written and verified;
* a failure carries *what happened / why / what to do*, because "generation
  failed" on its own tells the user nothing they can act on.

Nothing in this module imports a model library.  Importing it is free.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .capabilities import ImageCapabilities

__all__ = [
    "ImageProvider",
    "GenerationRequest",
    "GenerationResult",
    "ProviderStatus",
    "ImageModel",
    "ImageIssue",
    "GenerationMode",
    "MODES",
    "MODE_LABELS",
    "GenerationState",
    "STATES",
]


class GenerationMode:
    """The six things the left-hand column of Image Studio offers."""

    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    INPAINT = "inpaint"
    OUTPAINT = "outpaint"
    VARIATION = "variation"
    UPSCALE = "upscale"


MODES: tuple[str, ...] = (
    GenerationMode.TEXT_TO_IMAGE,
    GenerationMode.IMAGE_TO_IMAGE,
    GenerationMode.INPAINT,
    GenerationMode.OUTPAINT,
    GenerationMode.VARIATION,
    GenerationMode.UPSCALE,
)

MODE_LABELS: dict[str, str] = {
    GenerationMode.TEXT_TO_IMAGE: "Text to Image",
    GenerationMode.IMAGE_TO_IMAGE: "Image to Image",
    GenerationMode.INPAINT: "Inpaint",
    GenerationMode.OUTPAINT: "Outpaint",
    GenerationMode.VARIATION: "Variation",
    GenerationMode.UPSCALE: "Upscale",
}

#: The capability each mode needs.  A mode whose flag is False is disabled in
#: the UI with an explanation rather than failing after a model has loaded.
MODE_FEATURE: dict[str, str] = {
    GenerationMode.TEXT_TO_IMAGE: "text_to_image",
    GenerationMode.IMAGE_TO_IMAGE: "image_to_image",
    GenerationMode.INPAINT: "inpaint",
    GenerationMode.OUTPAINT: "outpaint",
    GenerationMode.VARIATION: "image_to_image",
    # Upscaling has a non-AI path, so it is checked separately by the caller.
    GenerationMode.UPSCALE: "upscale",
}


class GenerationState:
    """Job states, in the order a generation moves through them."""

    QUEUED = "QUEUED"
    LOADING_MODEL = "LOADING MODEL"
    GENERATING = "GENERATING"
    PROCESSING = "PROCESSING"
    SAVING = "SAVING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


STATES: tuple[str, ...] = (
    GenerationState.QUEUED,
    GenerationState.LOADING_MODEL,
    GenerationState.GENERATING,
    GenerationState.PROCESSING,
    GenerationState.SAVING,
    GenerationState.COMPLETED,
    GenerationState.FAILED,
    GenerationState.CANCELLED,
)


@dataclass
class ImageIssue:
    """One problem with a request or a result."""

    code: str
    message: str
    what_to_do: str = ""
    severity: str = "error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


@dataclass
class ImageModel:
    """One model a backend knows about.

    ``loaded`` is a fact about this process, not a claim: the model manager uses
    it to avoid loading the same weights twice and to unload what is idle.
    """

    id: str = ""
    name: str = ""
    backend: str = ""
    kind: str = ""                 # "diffusion" | "upscale" | "inpaint" | ...
    path: str = ""
    size_bytes: int = 0
    capabilities: ImageCapabilities = field(default_factory=ImageCapabilities)
    device: str = "cpu"            # "cpu" | "cuda" | "directml" | ...
    enabled: bool = True
    status: str = "available"      # "available" | "missing" | "incomplete"
    notes: str = ""
    loaded: bool = False

    @property
    def size_label(self) -> str:
        size = int(self.size_bytes or 0)
        if size <= 0:
            return "size unknown"
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} GiB"

    def describe(self) -> str:
        parts = [self.name or self.id or "unnamed model"]
        if self.backend:
            parts.append(self.backend)
        if self.kind:
            parts.append(self.kind)
        parts.append(self.size_label)
        parts.append(self.device)
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "backend": self.backend,
            "kind": self.kind, "path": self.path,
            "size_bytes": int(self.size_bytes or 0), "size_label": self.size_label,
            "capabilities": self.capabilities.to_dict(),
            "device": self.device, "enabled": bool(self.enabled),
            "status": self.status, "notes": self.notes, "loaded": bool(self.loaded),
        }


@dataclass
class GenerationRequest:
    """One generation.  Every field is optional except the mode.

    Fields the active backend does not support are ignored *by the caller* - the
    provider validates and reports rather than silently changing what the user
    asked for.
    """

    mode: str = GenerationMode.TEXT_TO_IMAGE
    #: Which adapter runs this.  Empty means "the one the user selected"; the
    #: service refuses to pick a different backend on the user's behalf.
    backend: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    width: int = 0
    height: int = 0
    #: 0 means "random"; the result always reports the seed actually used.
    seed: int = 0
    steps: int = 0
    guidance: float = 0.0
    sampler: str = ""
    #: How many images to produce.  1 is the normal Generate action; a batch is
    #: an explicit, separate action (sections 10, 70).
    batch: int = 1
    #: 0.0-1.0 for image-to-image; how much of the source to keep.
    strength: float = 0.0
    source_image: str = ""
    mask_image: str = ""
    reference_image: str = ""
    style_reference: str = ""
    lora: str = ""
    control_image: str = ""
    #: Extra pixels to add when outpainting.
    extend: dict = field(default_factory=dict)   # left/right/top/bottom
    #: Upscale factor for mode=upscale (0 means "use the backend default").
    scale: float = 0.0
    output_format: str = "png"
    output_dir: str = ""
    name_stem: str = "image"
    #: Where this came from, for the variant graph.
    parent_asset: str = ""
    project: str = ""
    collection: str = ""
    tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "backend": self.backend, "prompt": self.prompt,
            "negative_prompt": self.negative_prompt, "model": self.model,
            "width": int(self.width or 0), "height": int(self.height or 0),
            "seed": int(self.seed or 0), "steps": int(self.steps or 0),
            "guidance": float(self.guidance or 0.0), "sampler": self.sampler,
            "batch": int(self.batch or 1), "strength": float(self.strength or 0.0),
            "source_image": self.source_image, "mask_image": self.mask_image,
            "reference_image": self.reference_image,
            "style_reference": self.style_reference, "lora": self.lora,
            "control_image": self.control_image, "extend": dict(self.extend or {}),
            "output_format": self.output_format, "output_dir": self.output_dir,
            "name_stem": self.name_stem, "parent_asset": self.parent_asset,
            "project": self.project, "collection": self.collection,
            "tags": list(self.tags or []),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationRequest":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in known}
        return cls(**payload)


@dataclass
class GenerationResult:
    """What one generation produced.

    ``ok`` is only ever True with a verified file behind it.  ``quality`` is the
    report the UI shows after a generation (section 47) and holds measured
    values only.
    """

    ok: bool = False
    paths: list = field(default_factory=list)
    state: str = GenerationState.FAILED
    #: The seeds actually used, one per output image.
    seeds: list = field(default_factory=list)
    model: str = ""
    backend: str = ""
    mode: str = ""
    width: int = 0
    height: int = 0
    output_format: str = ""
    seconds: float = 0.0
    #: What happened / why / what to do.
    error: str = ""
    why: str = ""
    what_to_do: str = ""
    code: str = ""
    cancelled: bool = False
    issues: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    #: Whatever the backend wants to record; stored as image metadata.
    metadata: dict = field(default_factory=dict)

    @property
    def path(self) -> Optional[Path]:
        return Path(self.paths[0]) if self.paths else None

    @property
    def failed(self) -> bool:
        return self.state == GenerationState.FAILED

    def summary(self) -> str:
        if self.ok:
            count = len(self.paths)
            label = Path(self.paths[0]).name if self.paths else "image"
            return (f"{count} image(s) generated: {label}"
                    + (f" and {count - 1} more" if count > 1 else ""))
        if self.cancelled:
            return "Generation cancelled."
        return self.error or "Generation failed."

    def describe(self) -> str:
        lines = [self.summary()]
        if self.quality:
            for key in ("model", "backend", "resolution", "seed", "format",
                        "size_bytes", "seconds", "status"):
                if self.quality.get(key) not in (None, ""):
                    lines.append(f"  {key}: {self.quality[key]}")
        if self.error:
            lines.append(f"  what happened: {self.error}")
        if self.why:
            lines.append(f"  why: {self.why}")
        if self.what_to_do:
            lines.append(f"  what to do: {self.what_to_do}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok), "paths": [str(item) for item in self.paths],
            "state": self.state, "seeds": [int(item) for item in self.seeds],
            "model": self.model, "backend": self.backend, "mode": self.mode,
            "width": int(self.width or 0), "height": int(self.height or 0),
            "output_format": self.output_format,
            "seconds": round(float(self.seconds or 0.0), 3),
            "error": self.error, "why": self.why, "what_to_do": self.what_to_do,
            "code": self.code, "cancelled": bool(self.cancelled),
            "issues": [issue.to_dict() for issue in self.issues],
            "quality": dict(self.quality), "metadata": dict(self.metadata),
        }


@dataclass
class ProviderStatus:
    """Whether a backend can be used right now, and why not if it cannot."""

    available: bool = False
    #: "available" | "not_installed" | "not_supported" | "error"
    state: str = "not_installed"
    reason: str = ""
    #: What the user can do about it.
    instructions: list = field(default_factory=list)
    version: str = ""
    device: str = "cpu"

    def to_dict(self) -> dict:
        return {"available": bool(self.available), "state": self.state,
                "reason": self.reason,
                "instructions": [str(item) for item in self.instructions],
                "version": self.version, "device": self.device}


class ImageProvider(ABC):
    """One local image backend.

    Implementations must be cheap to construct and must not load a model in
    ``__init__``: discovery happens when Image Studio opens, and weights are
    loaded only when the user asks for a generation (section 5).
    """

    #: Stable id, used in settings and logs.
    id: str = ""
    #: Human name.
    label: str = ""
    #: Backend category: "diffusers" | "comfyui" | "python" | "command" |
    #: "http" | "onnx" | "local-resize".
    kind: str = ""

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    def status(self) -> ProviderStatus:
        """Is this backend usable here?  Must not load a model."""

    @abstractmethod
    def capabilities(self) -> ImageCapabilities:
        """What this backend can do.  Report False, never guess True."""

    @abstractmethod
    def models(self) -> list[ImageModel]:
        """The models this backend can see.  Must not load a model."""

    # -- generation --------------------------------------------------------

    def load(self, model: ImageModel) -> None:
        """Prepare a model for use.  Default: nothing to do."""

    def unload(self) -> None:
        """Release a model.  Default: nothing held."""

    @abstractmethod
    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        """Run one generation and verify what it wrote.

        ``progress`` is called with ``(state, fraction)`` so the caller can show
        real progress.  ``cancel`` is a cancel token; a backend that cannot stop
        mid-generation must still check it between images and return a
        cancelled result rather than carrying on.
        """

    # -- helpers -----------------------------------------------------------

    def validate(self, request: GenerationRequest,
                 model: Optional[ImageModel] = None) -> list[ImageIssue]:
        """Reject a request this backend cannot honour, before it loads.

        The default implementation checks the mode against the capabilities and
        the resolution against the reported limits.  A backend with stricter
        rules overrides this and calls ``super()`` first.
        """
        from .validation import validate_request

        caps = self.capabilities()
        return validate_request(request, caps, model=model)

    def _started(self) -> float:
        return time.monotonic()
