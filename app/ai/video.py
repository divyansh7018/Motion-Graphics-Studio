"""The video-generation contract (sections 20-31, 46, 48, 49).

One abstract provider, and every video backend - a local diffusion model, a
ComfyUI workflow, a command line tool, a local HTTP server, the deterministic
test backend - implements the same methods.  The Video Studio only ever talks to
this contract.

The shapes mirror :mod:`app.image.provider` deliberately: a user who has met one
half of the studio has met the other, and the two share the job queue, the
history and the asset pipeline.  They are separate types because a video result
carries facts an image result cannot - frame rate, duration, frame count - and
folding those into the image contract as unused fields would be a lie told in
the type system.

Two rules are built in here rather than left to callers:

* a result is **never** a success unless a real file was written and measured;
* a failure carries *what happened / why / what to do*.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .capabilities import AICapabilities
from .types import BackendKind, DeviceRequirement

__all__ = [
    "VideoMode",
    "VIDEO_MODES",
    "MODE_LABELS",
    "VideoModel",
    "VideoRequest",
    "VideoResult",
    "VideoIssue",
    "VideoState",
    "STATES",
    "CameraMove",
    "CAMERA_MOVES",
    "CAMERA_LABELS",
    "VideoProvider",
]


class VideoMode:
    """The five ways a clip can be made (section 20)."""

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    VIDEO_TO_VIDEO = "video_to_video"
    STORYBOARD_TO_VIDEO = "storyboard_to_video"
    EXTEND = "extend"


VIDEO_MODES: tuple[str, ...] = (
    VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO, VideoMode.VIDEO_TO_VIDEO,
    VideoMode.STORYBOARD_TO_VIDEO, VideoMode.EXTEND,
)

MODE_LABELS: dict[str, str] = {
    VideoMode.TEXT_TO_VIDEO: "Text to video",
    VideoMode.IMAGE_TO_VIDEO: "Image to video",
    VideoMode.VIDEO_TO_VIDEO: "Video to video",
    VideoMode.STORYBOARD_TO_VIDEO: "Storyboard to video",
    VideoMode.EXTEND: "Extend a clip",
}

#: Which capability a mode needs before the UI will offer it (section 21).
MODE_FEATURE: dict[str, str] = {
    VideoMode.TEXT_TO_VIDEO: "text_to_video",
    VideoMode.IMAGE_TO_VIDEO: "image_to_video",
    VideoMode.VIDEO_TO_VIDEO: "video_to_video",
    #: A storyboard is generated as one clip per scene, so the backend only has
    #: to be able to draw from text or move a picture (section 30).
    VideoMode.STORYBOARD_TO_VIDEO: "storyboard_to_video",
    VideoMode.EXTEND: "video_extend",
}


class CameraMove:
    """The optional camera instructions of section 21.

    A backend reports which of these it understands (``camera_moves`` on its
    capabilities) and the UI only offers those.  An unsupported move is refused
    by :meth:`VideoProvider.validate` rather than silently dropped.
    """

    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    ZOOM = "zoom"
    DOLLY = "dolly"
    ORBIT = "orbit"
    HANDHELD = "handheld"
    CUSTOM = "custom"


CAMERA_MOVES: tuple[str, ...] = (
    CameraMove.STATIC, CameraMove.PAN, CameraMove.TILT, CameraMove.ZOOM,
    CameraMove.DOLLY, CameraMove.ORBIT, CameraMove.HANDHELD, CameraMove.CUSTOM,
)

CAMERA_LABELS: dict[str, str] = {
    CameraMove.STATIC: "Static",
    CameraMove.PAN: "Pan",
    CameraMove.TILT: "Tilt",
    CameraMove.ZOOM: "Zoom",
    CameraMove.DOLLY: "Dolly",
    CameraMove.ORBIT: "Orbit",
    CameraMove.HANDHELD: "Handheld",
    CameraMove.CUSTOM: "Custom",
}


class VideoState:
    """How a generation ended.  The same three words an image uses."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


STATES: tuple[str, ...] = (VideoState.COMPLETED, VideoState.FAILED,
                           VideoState.CANCELLED)


@dataclass
class VideoIssue:
    """One problem with a request, before or after it ran."""

    code: str = ""
    message: str = ""
    what_to_do: str = ""
    severity: str = "error"          # error | warning

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


@dataclass
class VideoModel:
    """One video model a backend knows about (sections 6, 26).

    ``loaded`` is a fact about this process, not a claim.
    """

    id: str = ""
    name: str = ""
    backend: str = ""
    kind: str = "video"
    path: str = ""
    size_bytes: int = 0
    capabilities: AICapabilities = field(default_factory=AICapabilities)
    device: str = "cpu"
    requirement: str = DeviceRequirement.UNKNOWN
    enabled: bool = True
    status: str = "available"
    version: str = ""
    #: Frame budget the model was trained for, when it reports one.
    max_frames: int = 0
    notes: str = ""
    loaded: bool = False

    @property
    def size_label(self) -> str:
        return human_size(self.size_bytes)

    def describe(self) -> str:
        parts = [self.name or self.id or "unnamed model"]
        if self.backend:
            parts.append(self.backend)
        parts.append(self.size_label)
        from .types import REQUIREMENT_LABELS

        parts.append(REQUIREMENT_LABELS.get(self.requirement, self.requirement))
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "backend": self.backend,
            "kind": self.kind, "path": self.path,
            "size_bytes": int(self.size_bytes or 0), "size_label": self.size_label,
            "capabilities": self.capabilities.to_dict(), "device": self.device,
            "requirement": self.requirement, "enabled": bool(self.enabled),
            "status": self.status, "version": self.version,
            "max_frames": int(self.max_frames or 0),
            "notes": self.notes, "loaded": bool(self.loaded),
        }


def human_size(size: Any) -> str:
    """Bytes as a readable size; zero is reported as unknown, not as 0 B."""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "size unknown"
    if value <= 0:
        return "size unknown"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB"  # pragma: no cover - unreachable


@dataclass
class VideoRequest:
    """One clip generation.

    Every field is optional except the mode.  A field a backend does not
    support is *refused with an explanation* by
    :meth:`VideoProvider.validate`, never quietly ignored: silently dropping a
    duration or a camera move would give the user a clip they did not ask for
    (sections 45, 62).
    """

    mode: str = VideoMode.TEXT_TO_VIDEO
    #: Which adapter runs this.  Empty means "the one the user selected"; the
    #: service never picks a different backend on the user's behalf (section 100).
    backend: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    duration: float = 0.0
    fps: int = 0
    width: int = 0
    height: int = 0
    seed: int = 0
    #: How much of the source to keep (video to video).
    strength: float = 0.0
    source_image: str = ""
    source_video: str = ""
    reference_image: str = ""
    style_reference: str = ""
    #: Camera instruction, from :class:`CameraMove`.
    camera: str = ""
    #: Free text for a custom camera move, plus the distance in 0..1.
    camera_note: str = ""
    camera_amount: float = 0.0
    #: Extend mode: the clip to continue and how much to add.
    extend_from: str = ""
    output_format: str = "mp4"
    output_dir: str = ""
    name_stem: str = "clip"
    quality: str = ""
    #: How many clips to make.  1 is the Generate action; a batch is always an
    #: explicit, separate action (sections 10, 70), never something the studio
    #: decides on its own.
    batch: int = 1
    #: Where this came from, for the version graph.
    parent_asset: str = ""
    project: str = ""
    collection: str = ""
    tags: list = field(default_factory=list)
    #: Scene this clip is for, when generated from a scene or the storyboard.
    scene_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "backend": self.backend, "prompt": self.prompt,
            "negative_prompt": self.negative_prompt, "model": self.model,
            "duration": float(self.duration or 0.0), "fps": int(self.fps or 0),
            "width": int(self.width or 0), "height": int(self.height or 0),
            "seed": int(self.seed or 0), "strength": float(self.strength or 0.0),
            "source_image": self.source_image, "source_video": self.source_video,
            "reference_image": self.reference_image,
            "style_reference": self.style_reference,
            "camera": self.camera, "camera_note": self.camera_note,
            "camera_amount": float(self.camera_amount or 0.0),
            "extend_from": self.extend_from, "batch": int(self.batch or 1),
            "output_format": self.output_format,
            "output_dir": self.output_dir, "name_stem": self.name_stem,
            "quality": self.quality, "parent_asset": self.parent_asset,
            "project": self.project, "collection": self.collection,
            "tags": list(self.tags or []), "scene_id": self.scene_id,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VideoRequest":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in known}
        return cls(**payload)

    @property
    def feature(self) -> str:
        """The capability this request needs."""
        return MODE_FEATURE.get(self.mode, "")


@dataclass
class VideoResult:
    """What one clip generation produced.

    ``ok`` is only ever True with a measured file behind it: the validator
    reads the file back and fills in ``width``/``height``/``fps``/``duration``
    from the real container rather than from what was requested (section 46).
    """

    ok: bool = False
    path: Optional[Path] = None
    state: str = VideoState.FAILED
    seed: int = 0
    model: str = ""
    backend: str = ""
    mode: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frames: int = 0
    output_format: str = ""
    has_audio: bool = False
    #: What was asked for, so a mismatch is visible rather than silent (46).
    requested: dict = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""
    why: str = ""
    what_to_do: str = ""
    code: str = ""
    cancelled: bool = False
    issues: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    #: Set when the backend produced different properties than requested.
    mismatch: list = field(default_factory=list)
    #: The precise provider state behind a failure (NOT_INSTALLED and so on),
    #: so the interface can show the allowed word rather than a vague one.
    state_detail: str = ""
    #: What the user may do next, chosen from a fixed vocabulary so a button is
    #: only offered when it can really work (section 36).
    options: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.state == VideoState.FAILED

    def summary(self) -> str:
        if self.ok:
            name = self.path.name if self.path else "clip"
            return (f"Clip generated: {name} - {self.width}x{self.height}, "
                    f"{self.fps:g} fps, {self.duration:.2f}s"
                    + (f", seed {self.seed}" if self.seed else ""))
        if self.cancelled:
            return "Clip generation cancelled."
        return self.error or "Clip generation failed."

    def describe(self) -> str:
        lines = [self.summary()]
        if self.mismatch:
            lines.append("  the file differs from the request:")
            for item in self.mismatch:
                lines.append(f"    - {item}")
        if self.error and not self.ok:
            lines.append(f"  what happened: {self.error}")
        if self.why:
            lines.append(f"  why: {self.why}")
        if self.what_to_do:
            lines.append(f"  what to do: {self.what_to_do}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "path": str(self.path) if self.path else "",
            "state": self.state, "seed": int(self.seed or 0), "model": self.model,
            "backend": self.backend, "mode": self.mode,
            "width": int(self.width or 0), "height": int(self.height or 0),
            "fps": round(float(self.fps or 0.0), 3),
            "duration": round(float(self.duration or 0.0), 3),
            "frames": int(self.frames or 0), "output_format": self.output_format,
            "has_audio": bool(self.has_audio), "requested": dict(self.requested),
            "seconds": round(float(self.seconds or 0.0), 3),
            "error": self.error, "why": self.why, "what_to_do": self.what_to_do,
            "code": self.code, "cancelled": bool(self.cancelled),
            "issues": [issue.to_dict() for issue in self.issues],
            "quality": dict(self.quality), "metadata": dict(self.metadata),
            "mismatch": list(self.mismatch),
            "state_detail": self.state_detail, "options": list(self.options),
        }


class VideoProvider(ABC):
    """One local video backend.

    Like the image contract, construction must be cheap: video models are the
    largest things this application touches, and describing one must never load
    one (section 42).
    """

    #: Stable id, used in settings and logs.
    id: str = ""
    #: Human name.
    name: str = ""
    #: Always :class:`app.ai.types.BackendKind.VIDEO`; the registry checks it.
    kind: str = BackendKind.VIDEO
    #: "python" | "diffusers" | "comfyui" | "http" | "command" | "onnx" | ...
    transport: str = ""
    version: str = ""
    #: What this backend needs from the machine (section 40).
    device_requirement: str = DeviceRequirement.UNKNOWN

    # -- discovery ---------------------------------------------------------

    @abstractmethod
    def status(self) -> Any:
        """Is this backend usable here?  Returns an AI-level ProviderStatus."""

    @abstractmethod
    def capabilities(self) -> AICapabilities:
        """What this backend can do.  Report False, never guess True."""

    @abstractmethod
    def models(self) -> list[VideoModel]:
        """The models this backend can see.  Must not load a model."""

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        """Prepare a model for use.  Default: nothing to do."""

    def unload(self) -> None:
        """Release a model.  Default: nothing held."""

    @abstractmethod
    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        """Run one generation and verify what it wrote.

        ``progress`` is called with ``(state, fraction)`` so the queue can show
        real progress, and ``cancel`` is a cancel token: a backend that cannot
        stop mid-generation must still check it between steps and return a
        cancelled result rather than carrying on (section 35).
        """

    # -- helpers -----------------------------------------------------------

    def validate(self, request: VideoRequest,
                 model: Optional[VideoModel] = None) -> list[VideoIssue]:
        """Reject a request this backend cannot honour, before it loads."""
        from .video_validation import validate_video_request

        return validate_video_request(request, self.capabilities(), model=model)

    def describe(self) -> str:
        return ""

    # -- the AIProvider-shaped description the managers use ----------------

    def location(self) -> str:
        return ""

    def install_hint(self) -> str:
        return ""

    def settings_schema(self) -> list:
        return []

    def uses_network(self) -> bool:
        return False

    def loaded_model(self) -> str:
        return ""

    def health(self, *, level: str = "light", model: Any = None,
               service: Any = None) -> Any:
        """Light or deep check, in the same shape the image side returns.

        A deep check without a service reports CHECK NOT AVAILABLE rather than
        borrowing another route - a check that did not run must never look like
        one that passed (section 7).
        """
        from .health import check_provider

        return check_provider(self, level=level, model=model, service=service)

    def check(self, *, deep: bool = False, service: Any = None) -> Any:
        """The same entry point the image-shaped backends expose."""
        return self.health(level="deep" if deep else "light", service=service)

    def model_info(self) -> dict:
        """A summary of this backend's models, in the manager's own shape."""
        from .backend import describe_models

        return describe_models(self)

    def log_lines(self, limit: int = 200) -> list[str]:
        """The tail of this backend's log, from the application's log file."""
        from .backend import log_lines_for

        return log_lines_for(self, limit=limit)

    def to_dict(self, *, deep: bool = False) -> dict:
        """Everything the manager shows about this backend (section 6)."""
        from .backend import describe_provider

        return describe_provider(self, deep=deep)

    # -- capability questions ---------------------------------------------

    #: Whether this backend runs a real model.  False for a test fixture, so a
    #: report can tell them apart without reading the backend's name.
    is_model: bool = True
    #: Licence and source, recorded even when the backend is not installed.
    licence: str = ""
    homepage: str = ""

    def supports(self, feature: str) -> bool:
        """Whether a capability is claimed by this backend."""
        try:
            return bool(self.capabilities().supports(feature))
        except Exception:  # noqa: BLE001 - an unclear answer is a no
            return False

    def supports_operation(self, operation: str) -> bool:
        """Whether this backend can do an operation, by its mode/name."""
        from .capabilities import FEATURE_LABELS  # noqa: F401 - documented above
        from .video import MODE_FEATURE

        feature = MODE_FEATURE.get(str(operation), "")
        if not feature:
            return False
        return self.supports(feature)

    def supports_setting(self, name: str) -> bool:
        try:
            return bool(self.capabilities().supports_setting(name))
        except Exception:  # noqa: BLE001
            return False

    def started(self) -> float:
        return time.monotonic()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"
