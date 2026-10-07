"""Validating an AI request and its output (sections 44, 45, 46, 48).

Image requests are validated by Stage F's own validator - there is no second
opinion to disagree with it - and video requests by
:mod:`app.ai.video_validation`.  This module is the single entry point that
picks the right one, so a caller cannot accidentally skip validation by using
the "other" path.

It also holds the small helpers the interfaces ask for: what a request needs
before it can run, whether an operation is available on a backend, and how to
describe a list of problems.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .capabilities import FEATURE_LABELS
from .types import (AIOperation, IMAGE_OPERATIONS, OPERATION_KIND,
                    OPERATION_LABELS, VIDEO_OPERATIONS)
from .video import VideoIssue, VideoRequest
from .video_validation import validate_video_request

__all__ = ["validate_request", "validate_output", "describe_issues",
           "required_inputs", "missing_inputs", "operation_available",
           "operations_for", "Issue"]

#: A plain problem record, so the image and video validators look the same to
#: whoever is showing them.
Issue = VideoIssue


def validate_request(operation: str, request: Any, capabilities: Any,
                     model: Any = None) -> list:
    """Every problem with this request, whichever kind of work it is."""
    kind = OPERATION_KIND.get(str(operation), "")
    if kind == "video" or isinstance(request, VideoRequest):
        return validate_video_request(request, capabilities, model=model)
    try:
        from ..image.validation import validate_request as validate_image

        return list(validate_image(request, capabilities))
    except Exception as exc:  # noqa: BLE001 - never crash a check
        return [Issue(code="VALIDATION_UNAVAILABLE",
                      message=f"The request could not be checked: {exc}",
                      what_to_do="Use another backend, or check the log.")]


def validate_output(operation: str, path: Any, *, tools: Any = None,
                    request: Any = None, model: Any = None) -> Any:
    """Validate whatever was produced: an image or a clip.

    Returns a result with ``ok`` and, when it failed, what to do - the same
    shape either way, so a caller does not have to know which kind ran.
    """
    kind = OPERATION_KIND.get(str(operation), "video")
    if kind == "video":
        from .video_validation import validate_video_file

        return validate_video_file(
            path, tools=tools,
            expected_width=int(getattr(request, "width", 0) or 0),
            expected_height=int(getattr(request, "height", 0) or 0),
            expected_fps=float(getattr(request, "fps", 0) or 0.0),
            expected_duration=float(getattr(request, "duration", 0) or 0.0))
    try:
        from ..image.backends.base import verify_output

        return verify_output(path, request=request, model=model)
    except Exception as exc:  # noqa: BLE001
        return Issue(code="OUTPUT_CHECK_UNAVAILABLE",
                     message=f"The output could not be checked: {exc}",
                     what_to_do="Check the file yourself, or install FFmpeg "
                                "and Pillow.")


def describe_issues(issues: Iterable[Any], *, limit: int = 4) -> str:
    """A short list a person can act on, most serious first."""
    items = list(issues or [])
    if not items:
        return ""
    errors = [item for item in items if getattr(item, "severity", "") == "error"]
    chosen = errors or items
    lines = [str(getattr(item, "message", item)) for item in chosen[:limit]]
    if len(chosen) > limit:
        lines.append(f"... and {len(chosen) - limit} more.")
    return "\n".join(lines)


#: What each operation needs before it can run.  Used to grey out a button and
#: say why, rather than failing after the user has pressed it (section 36).
REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    AIOperation.TEXT_TO_IMAGE: ("prompt",),
    AIOperation.IMAGE_TO_IMAGE: ("source image",),
    AIOperation.INPAINT: ("source image", "mask"),
    AIOperation.OUTPAINT: ("source image",),
    AIOperation.VARIATION: ("source image",),
    AIOperation.UPSCALE: ("source image",),
    AIOperation.BACKGROUND_REMOVAL: ("source image",),
    AIOperation.TEXT_TO_VIDEO: ("prompt",),
    AIOperation.IMAGE_TO_VIDEO: ("source image",),
    AIOperation.VIDEO_TO_VIDEO: ("source video",),
    AIOperation.VIDEO_EXTEND: ("clip to extend",),
    AIOperation.STORYBOARD_TO_VIDEO: ("storyboard",),
}


def required_inputs(operation: str) -> tuple[str, ...]:
    return REQUIRED_INPUTS.get(str(operation), ())


def missing_inputs(operation: str, *, prompt: str = "", source_image: str = "",
                   source_video: str = "", mask: str = "",
                   extend_from: str = "", scene_id: str = "",
                   storyboard: bool = False) -> list[str]:
    """Which of the things this operation needs are not there yet."""
    available = {
        "prompt": bool(str(prompt or "").strip()),
        "source image": bool(str(source_image or "").strip()),
        "source video": bool(str(source_video or "").strip()),
        "mask": bool(str(mask or "").strip()),
        "clip to extend": bool(str(extend_from or "").strip()),
        "scene": bool(str(scene_id or "").strip()),
        "storyboard": bool(storyboard),
    }
    return [name for name in required_inputs(operation) if not available.get(name)]


def operation_available(operation: str, capabilities: Any) -> bool:
    """Whether a backend's capabilities allow an operation."""
    feature = {
        AIOperation.TEXT_TO_IMAGE: "text_to_image",
        AIOperation.IMAGE_TO_IMAGE: "image_to_image",
        AIOperation.INPAINT: "inpaint", AIOperation.OUTPAINT: "outpaint",
        AIOperation.VARIATION: "image_to_image",
        AIOperation.UPSCALE: "upscale",
        AIOperation.BACKGROUND_REMOVAL: "background_removal",
        AIOperation.TEXT_TO_VIDEO: "text_to_video",
        AIOperation.IMAGE_TO_VIDEO: "image_to_video",
        AIOperation.VIDEO_TO_VIDEO: "video_to_video",
        AIOperation.VIDEO_EXTEND: "video_extend",
        AIOperation.STORYBOARD_TO_VIDEO: "storyboard_to_video",
        AIOperation.MODEL_CHECK: "", AIOperation.BACKEND_TEST: "",
    }.get(str(operation), "")
    if not feature or capabilities is None:
        return True
    try:
        return bool(capabilities.supports(feature))
    except Exception:  # noqa: BLE001
        return False


def operations_for(capabilities: Any, *, kind: str = "") -> list[dict]:
    """The operations this backend can do, with their labels.

    Every operation is listed with ``available`` set honestly, so the interface
    can show what is possible rather than hiding it (section 6).
    """
    wanted = (IMAGE_OPERATIONS if kind == "image" else
              VIDEO_OPERATIONS if kind == "video" else None)
    items: list[dict] = []
    for operation in (wanted or (IMAGE_OPERATIONS + VIDEO_OPERATIONS)):
        items.append({
            "operation": str(operation),
            "label": OPERATION_LABELS.get(str(operation), str(operation)),
            "available": operation_available(str(operation), capabilities),
            "needs": list(required_inputs(str(operation))),
        })
    return items


def unavailable_reason(operation: str, capabilities: Any, backend_name: str) -> str:
    """Why an operation is greyed out, in one sentence."""
    feature = {
        AIOperation.TEXT_TO_VIDEO: "text_to_video",
        AIOperation.IMAGE_TO_VIDEO: "image_to_video",
        AIOperation.VIDEO_TO_VIDEO: "video_to_video",
        AIOperation.VIDEO_EXTEND: "video_extend",
        AIOperation.STORYBOARD_TO_VIDEO: "storyboard_to_video",
        AIOperation.TEXT_TO_IMAGE: "text_to_image",
        AIOperation.IMAGE_TO_IMAGE: "image_to_image",
        AIOperation.INPAINT: "inpaint", AIOperation.OUTPAINT: "outpaint",
        AIOperation.UPSCALE: "upscale",
        AIOperation.BACKGROUND_REMOVAL: "background_removal",
    }.get(str(operation), "")
    label = FEATURE_LABELS.get(feature, str(operation))
    return (f"{backend_name} cannot do {str(operation).replace('_', ' ')}: "
            f"{label.lower()} is not among its capabilities.")


def settings_support(capabilities: Any) -> dict:
    """Which settings a backend honours, for greying out the form."""
    keys = ("negative_prompt", "seed_control", "steps", "guidance", "sampler",
            "batch", "strength", "duration", "fps", "resolution_control",
            "quality", "device_choice", "dtype_choice", "offload",
            "camera_control")
    supported: dict[str, bool] = {}
    for key in keys:
        try:
            supported[key] = bool(capabilities.supports_setting(key))
        except Exception:  # noqa: BLE001
            supported[key] = False
    return supported


def coerce_operation(value: str) -> str:
    """Accept a mode or an operation name and return the operation."""
    text = str(value or "").strip()
    if text in IMAGE_OPERATIONS or text in VIDEO_OPERATIONS:
        return text
    for operation in IMAGE_OPERATIONS + VIDEO_OPERATIONS:
        if operation.replace("_", "-") == text.replace("_", "-"):
            return str(operation)
    return text


def optional_float(value: Any, default: float = 0.0) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
