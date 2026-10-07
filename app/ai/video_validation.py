"""Checking a video request, and checking what a backend actually wrote
(sections 21, 23, 46, 48, 100).

Half of the honesty rules in Stage G live in this module:

* a request a backend cannot honour is **refused with a reason**, never
  quietly trimmed (section 45);
* a clip is only reported as a success after the file has been read back and
  measured, and the numbers in the result come from the file rather than from
  the request (sections 46, 48).

When the container disagrees with the request - the model produced 4 seconds
when 5 were asked for, or 512x512 when the backend said it would do 768x768 -
the disagreement is recorded in ``mismatch`` and shown to the user.  A clip
that is different from the request is legitimate; a clip that pretends to be
what was asked for is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .capabilities import FEATURE_LABELS
from .types import DeviceRequirement
from .video import (CAMERA_LABELS, CAMERA_MOVES, MODE_FEATURE, MODE_LABELS,
                    VIDEO_MODES, VideoIssue, VideoModel, VideoRequest)

__all__ = [
    "VideoOutputCheck",
    "validate_video_request",
    "validate_video_file",
    "describe_mismatch",
    "DEFAULT_DURATION_TOLERANCE",
]

#: How far a clip's measured duration may differ from the request before it is
#: worth mentioning.  One frame at 24 fps is 0.042 s, and encoders round.
DEFAULT_DURATION_TOLERANCE = 0.20

#: Frame rate is compared loosely: 29.97 is 30 as far as a user is concerned,
#: and profiles differ by fractions of a frame per second.
FPS_TOLERANCE = 0.51


#: Every way a produced clip can fail its check.  Named here so the interface,
#: the reports and the tests all quote the same words (sections 46, 48).
VIDEO_OUTPUT_MISSING = "VIDEO_OUTPUT_MISSING"
VIDEO_OUTPUT_UNREADABLE = "VIDEO_OUTPUT_UNREADABLE"
VIDEO_OUTPUT_EMPTY = "VIDEO_OUTPUT_EMPTY"
VIDEO_OUTPUT_TOO_SMALL = "VIDEO_OUTPUT_TOO_SMALL"
VIDEO_OUTPUT_INVALID = "VIDEO_OUTPUT_INVALID"
VIDEO_OUTPUT_NO_STREAM = "VIDEO_OUTPUT_NO_STREAM"
VIDEO_OUTPUT_NO_DIMENSIONS = "VIDEO_OUTPUT_NO_DIMENSIONS"
VIDEO_OUTPUT_NO_DURATION = "VIDEO_OUTPUT_NO_DURATION"
VIDEO_CHECK_NOT_AVAILABLE = "VIDEO_CHECK_NOT_AVAILABLE"
CLIP_UNMEASURED = "CLIP_UNMEASURED"
VIDEO_OUTPUT_MISSING_END = "VIDEO_OUTPUT_MISSING_END"

#: The codes an output check can return, for the tests and the reports.
OUTPUT_CODES: tuple[str, ...] = (
    VIDEO_OUTPUT_MISSING, VIDEO_OUTPUT_UNREADABLE, VIDEO_OUTPUT_EMPTY,
    VIDEO_OUTPUT_TOO_SMALL, VIDEO_OUTPUT_INVALID, VIDEO_OUTPUT_NO_STREAM,
    VIDEO_OUTPUT_NO_DIMENSIONS, VIDEO_OUTPUT_NO_DURATION,
    VIDEO_CHECK_NOT_AVAILABLE, CLIP_UNMEASURED,
)


def _issue(code: str, message: str, what_to_do: str,
           severity: str = "error") -> VideoIssue:
    return VideoIssue(code=code, message=message, what_to_do=what_to_do,
                      severity=severity)


def validate_video_request(request: VideoRequest, capabilities: Any,
                           model: Optional[VideoModel] = None) -> list[VideoIssue]:
    """Everything wrong with this request, before a model is loaded.

    Returns every problem rather than the first, because fixing one thing at a
    time is a bad way to spend an afternoon.
    """
    issues: list[VideoIssue] = []
    mode = str(getattr(request, "mode", "") or "")

    if mode not in VIDEO_MODES:
        return [_issue("VIDEO_MODE_UNKNOWN",
                       f"'{mode}' is not a video mode this application offers.",
                       "Pick one of: " + ", ".join(MODE_LABELS.get(item, item)
                                                   for item in VIDEO_MODES) + ".")]

    feature = MODE_FEATURE.get(mode, "")
    if feature == "storyboard_to_video" and mode == "storyboard_to_video":
        # A plan is generated clip by clip, so either of these is enough.
        if not (capabilities.supports("text_to_video")
                or capabilities.supports("image_to_video")):
            issues.append(_issue(
                "VIDEO_STORYBOARD_UNSUPPORTED",
                "This backend cannot draw from text or move a picture, so it "
                "cannot make a storyboard's clips.",
                "Choose a backend that supports text to video or image to "
                "video."))
        feature = ""
    if feature and not capabilities.supports(feature):
        issues.append(_issue(
            f"VIDEO_{feature.upper()}_UNSUPPORTED",
            f"This backend cannot do {MODE_LABELS.get(mode, mode).lower()}: "
            f"{FEATURE_LABELS.get(feature, feature).lower()} is not among its "
            f"capabilities.",
            "Choose a backend that supports it, or change the mode."))

    # -- inputs the mode cannot work without -------------------------------
    if mode in ("text_to_video", "storyboard_to_video") and not str(
            request.prompt or "").strip() and not str(request.scene_id or "").strip():
        issues.append(_issue(
            "VIDEO_PROMPT_REQUIRED",
            "A prompt is needed: this backend draws the clip from text.",
            "Describe what should happen in the clip."))
    if mode == "image_to_video" and not str(request.source_image or "").strip():
        issues.append(_issue(
            "VIDEO_SOURCE_IMAGE_REQUIRED",
            "Image to video needs a source image.",
            "Choose the image the clip should move."))
    if mode == "video_to_video" and not str(request.source_video or "").strip():
        issues.append(_issue(
            "VIDEO_SOURCE_VIDEO_REQUIRED",
            "Video to video needs a source video.",
            "Choose the clip to restyle."))
    if mode == "storyboard_to_video":
        plan = list((getattr(request, "extra", {}) or {}).get("storyboard") or [])
        if not plan:
            issues.append(_issue(
                "VIDEO_STORYBOARD_REQUIRED",
                "Storyboard to video needs the storyboard's own scenes, and none "
                "were given.",
                "Build the plan from the storyboard first, review it, then "
                "generate - nothing is sent from a storyboard automatically."))
        unapproved = [item for item in plan
                      if not bool((item or {}).get("approved"))]
        if unapproved:
            issues.append(_issue(
                "VIDEO_STORYBOARD_NOT_APPROVED",
                f"{len(unapproved)} of the plan's prompts have not been approved "
                f"yet.",
                "Review the prompts in the plan and approve them - the studio "
                "does not send prompts you have not seen."))
    if mode == "extend" and not str(request.extend_from or "").strip():
        issues.append(_issue(
            "VIDEO_EXTEND_SOURCE_REQUIRED",
            "Extending a clip needs the clip to continue.",
            "Choose the clip to extend."))

    for field_name, label, code in (
            ("source_image", "source image", "VIDEO_SOURCE_IMAGE_MISSING"),
            ("source_video", "source video", "VIDEO_SOURCE_VIDEO_MISSING"),
            ("extend_from", "clip to extend", "VIDEO_EXTEND_SOURCE_MISSING"),
            ("reference_image", "reference image", "VIDEO_REFERENCE_MISSING")):
        value = str(getattr(request, field_name, "") or "").strip()
        if value and not Path(value).is_file():
            issues.append(_issue(
                code, f"The {label} was not found: {value}",
                "Choose the file again - it may have been moved or deleted."))

    # -- numbers -----------------------------------------------------------
    duration = float(request.duration or 0.0)
    if duration < 0:
        issues.append(_issue(
            "VIDEO_DURATION_NEGATIVE", "A clip cannot have a negative duration.",
            "Enter a duration of zero or more - zero means the backend's default."))
    if duration and capabilities.min_duration and duration < capabilities.min_duration:
        issues.append(_issue(
            "VIDEO_DURATION_TOO_SHORT",
            f"{duration:g}s is shorter than this backend can make "
            f"({capabilities.min_duration:g}s minimum).",
            f"Ask for at least {capabilities.min_duration:g}s."))
    if duration and capabilities.max_duration and duration > capabilities.max_duration:
        issues.append(_issue(
            "VIDEO_DURATION_TOO_LONG",
            f"{duration:g}s is longer than this backend can make in one clip "
            f"({capabilities.max_duration:g}s maximum).",
            f"Ask for at most {capabilities.max_duration:g}s, or generate two "
            f"clips and extend one with the other."))
    if not capabilities.supports_setting("duration") and duration:
        issues.append(_issue(
            "VIDEO_DURATION_UNSUPPORTED",
            "This backend does not let the duration be chosen.",
            "Clear the duration to accept whatever the backend produces.",
            severity="warning"))

    fps = int(request.fps or 0)
    if fps < 0:
        issues.append(_issue("VIDEO_FPS_NEGATIVE", "The frame rate cannot be negative.",
                             "Enter a frame rate of zero or more."))
    if fps and capabilities.max_fps and fps > capabilities.max_fps:
        issues.append(_issue(
            "VIDEO_FPS_TOO_HIGH",
            f"{fps} fps is above what this backend produces "
            f"({capabilities.max_fps} fps maximum).",
            f"Ask for at most {capabilities.max_fps} fps."))

    for name, value, label in (("width", int(request.width or 0), "width"),
                               ("height", int(request.height or 0), "height")):
        if value < 0:
            issues.append(_issue(f"VIDEO_{name.upper()}_NEGATIVE",
                                 f"The {label} cannot be negative.",
                                 "Enter a size of zero or more."))
        elif value and capabilities.min_dimension and value < capabilities.min_dimension:
            issues.append(_issue(
                f"VIDEO_{name.upper()}_TOO_SMALL",
                f"{value} px is below this backend's minimum of "
                f"{capabilities.min_dimension} px.",
                f"Use at least {capabilities.min_dimension} px."))
        elif value and capabilities.max_dimension and value > capabilities.max_dimension:
            issues.append(_issue(
                f"VIDEO_{name.upper()}_TOO_LARGE",
                f"{value} px is above this backend's maximum of "
                f"{capabilities.max_dimension} px.",
                f"Use at most {capabilities.max_dimension} px."))
        elif value and capabilities.dimension_multiple \
                and value % capabilities.dimension_multiple:
            issues.append(_issue(
                f"VIDEO_{name.upper()}_NOT_MULTIPLE",
                f"{value} is not a multiple of {capabilities.dimension_multiple}.",
                f"Use a multiple of {capabilities.dimension_multiple} px "
                f"(for example {round(value / capabilities.dimension_multiple + 0.5) * capabilities.dimension_multiple})."))

    if request.strength and not 0.0 < float(request.strength) <= 1.0:
        issues.append(_issue(
            "VIDEO_STRENGTH_OUT_OF_RANGE",
            f"Strength must be above 0 and at most 1 "
            f"({float(request.strength):g} was given).",
            "Use a value such as 0.6 - higher keeps more of the source."))

    # -- camera (section 21) ----------------------------------------------
    camera = str(request.camera or "").strip()
    if camera:
        if camera not in CAMERA_MOVES:
            issues.append(_issue(
                "VIDEO_CAMERA_UNKNOWN",
                f"'{camera}' is not a camera move this application knows.",
                "Pick one of: " + ", ".join(CAMERA_LABELS.get(item, item)
                                            for item in CAMERA_MOVES) + "."))
        elif not capabilities.supports("camera_control"):
            issues.append(_issue(
                "VIDEO_CAMERA_UNSUPPORTED",
                "This backend has no camera-move control, so the clip would "
                "ignore the instruction.",
                "Clear the camera move, or choose a backend that supports one."))
        elif camera not in [str(item) for item in capabilities.camera_moves]:
            supported = ", ".join(CAMERA_LABELS.get(str(item), str(item))
                                  for item in capabilities.camera_moves)
            issues.append(_issue(
                "VIDEO_CAMERA_NOT_OFFERED",
                f"This backend does not offer '{CAMERA_LABELS.get(camera, camera)}'.",
                f"It offers: {supported}." if supported
                else "It offers no camera moves."))

    # -- the model itself --------------------------------------------------
    if model is not None:
        if not bool(getattr(model, "enabled", True)):
            issues.append(_issue(
                "VIDEO_MODEL_DISABLED",
                f"The model '{getattr(model, 'name', '') or getattr(model, 'id', '')}' "
                f"is turned off in the settings.",
                "Enable it in the model list, or pick another model."))
        requirement = str(getattr(model, "requirement", "") or "")
        if requirement == DeviceRequirement.GPU_REQUIRED:
            issues.append(_issue(
                "VIDEO_GPU_REQUIRED",
                "This model requires a supported GPU, and none was detected.",
                "Choose a model that runs on CPU, or run this on a machine "
                "with a supported GPU."))
    return issues


@dataclass
class VideoOutputCheck:
    """What reading a finished clip back actually proved (section 48)."""

    ok: bool = False
    path: str = ""
    error: str = ""
    why: str = ""
    what_to_do: str = ""
    code: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    frames: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    size_bytes: int = 0
    #: Where the measurements came from - never blank when a check ran.
    measured_with: str = ""
    mismatch: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok), "path": self.path, "error": self.error,
            "why": self.why, "what_to_do": self.what_to_do, "code": self.code,
            "width": int(self.width or 0), "height": int(self.height or 0),
            "fps": round(float(self.fps or 0.0), 3),
            "duration": round(float(self.duration or 0.0), 3),
            "frames": int(self.frames or 0), "video_codec": self.video_codec,
            "audio_codec": self.audio_codec, "has_audio": bool(self.has_audio),
            "size_bytes": int(self.size_bytes or 0),
            "measured_with": self.measured_with,
            "mismatch": list(self.mismatch), "notes": list(self.notes),
        }


def _discover_tools() -> Any:
    """FFmpeg, discovered once per process, or None when it is not installed."""
    global _TOOLS
    if _TOOLS is None:
        try:
            from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

            _TOOLS = FFmpegTools(discover_ffmpeg())
        except Exception:  # noqa: BLE001 - no FFmpeg is a normal machine state
            _TOOLS = False
    return _TOOLS or None


_TOOLS: Any = None


def _fail(code: str, error: str, what_to_do: str, *, why: str = "") -> VideoOutputCheck:
    return VideoOutputCheck(ok=False, code=code, error=error, why=why,
                            what_to_do=what_to_do)


def validate_video_file(path: Any, *, tools: Any = None,
                        expected_width: int = 0, expected_height: int = 0,
                        expected_fps: float = 0.0,
                        expected_duration: float = 0.0,
                        duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
                        expect_audio: bool = False,
                        min_bytes: int = 128) -> VideoOutputCheck:
    """Read a generated clip back and measure it.

    This is the only place a clip is allowed to be called valid.  A file that
    does not exist, is empty, is not a container at all, or has no video stream
    fails - regardless of what the backend returned in its result object
    (sections 48, 99).
    """
    target = Path(path)
    check = VideoOutputCheck(path=str(target))
    if not target.is_file():
        return _fail(VIDEO_OUTPUT_MISSING,
                     f"The backend reported success but {target.name} is not there.",
                     "Run the generation again; if it repeats, the backend is "
                     "not writing where it claims.")
    try:
        size = target.stat().st_size
    except OSError as exc:
        return _fail(VIDEO_OUTPUT_UNREADABLE,
                     f"{target.name} could not be read: {exc}",
                     "Check the folder permissions and free space.")
    check.size_bytes = int(size)
    if size <= 0:
        return _fail(VIDEO_OUTPUT_EMPTY, f"{target.name} is 0 bytes long.",
                     "Run the generation again - the write did not complete.")
    if size < max(0, int(min_bytes)):
        return _fail(VIDEO_OUTPUT_TOO_SMALL,
                     f"{target.name} is only {size} bytes, which is too small to "
                     f"hold a video.",
                     "Run the generation again; the file was truncated.")

    if tools is None:
        # No tools were handed in: look for FFmpeg rather than assuming it is
        # missing.  A clip that exists on a machine with FFmpeg must be
        # measured, not reported as unmeasurable (section 46).
        tools = _discover_tools()
    if not getattr(getattr(tools, "discovery", None), "has_ffmpeg", False):
        # Without FFmpeg nothing can be measured.  That is CHECK NOT AVAILABLE,
        # never a pass - and the caller decides whether to keep the file.
        check.code = VIDEO_CHECK_NOT_AVAILABLE
        check.error = ("The clip exists, but FFmpeg is not available, so it "
                       "could not be measured.")
        check.what_to_do = "Install FFmpeg, then check the clip again."
        return check

    from ..media.probe import probe_media

    info = probe_media(target, tools)
    check.measured_with = info.source_label
    if not info.ok:
        return _fail(VIDEO_OUTPUT_INVALID,
                     f"{target.name} could not be read as a video: {info.error}",
                     "The backend's output is not a usable video. Run the "
                     "generation again, or check the backend's own log.")
    if not info.has_video:
        return _fail(VIDEO_OUTPUT_NO_STREAM,
                     f"{target.name} has no video stream in it.",
                     "The backend wrote a file, but not a video.")
    if int(getattr(info, "size_bytes", 0) or 0) != 0:
        check.size_bytes = int(info.size_bytes)

    check.ok = True
    check.width = int(info.width or 0)
    check.height = int(info.height or 0)
    check.fps = float(info.fps or 0.0)
    check.duration = float(info.duration or 0.0)
    check.video_codec = str(info.video_codec or "")
    check.audio_codec = str(info.audio_codec or "")
    check.has_audio = bool(info.has_audio)
    if check.fps > 0 and check.duration > 0:
        check.frames = int(round(check.fps * check.duration))

    if check.width <= 0 or check.height <= 0:
        return _fail(VIDEO_OUTPUT_NO_DIMENSIONS,
                     f"{target.name} reports no picture size, so it cannot be "
                     f"used in a timeline.",
                     "Run the generation again, or check the backend's settings.")
    if check.duration <= 0:
        return _fail(VIDEO_OUTPUT_NO_DURATION,
                     f"{target.name} reports no duration.",
                     "Run the generation again; a clip with no length cannot be "
                     "placed on the timeline.")

    check.mismatch = describe_mismatch(
        check, expected_width=expected_width, expected_height=expected_height,
        expected_fps=expected_fps, expected_duration=expected_duration,
        duration_tolerance=duration_tolerance)
    if expect_audio and not check.has_audio:
        check.notes.append("The backend was asked for audio and produced none.")
    return check


def describe_mismatch(check: VideoOutputCheck, *, expected_width: int = 0,
                      expected_height: int = 0, expected_fps: float = 0.0,
                      expected_duration: float = 0.0,
                      duration_tolerance: float = DEFAULT_DURATION_TOLERANCE) -> list[str]:
    """The differences between what was asked for and what came out (section 46).

    Reported, not corrected: silently resampling a clip to the requested size
    would hide the fact that the model could not make it.
    """
    lines: list[str] = []
    if expected_width and check.width and int(check.width) != int(expected_width):
        lines.append(f"width {check.width} (asked for {expected_width})")
    if expected_height and check.height and int(check.height) != int(expected_height):
        lines.append(f"height {check.height} (asked for {expected_height})")
    if expected_fps and check.fps and abs(float(check.fps) - float(expected_fps)) > FPS_TOLERANCE:
        lines.append(f"frame rate {check.fps:g} fps (asked for {expected_fps:g})")
    if expected_duration and check.duration \
            and abs(float(check.duration) - float(expected_duration)) > float(duration_tolerance):
        lines.append(f"duration {check.duration:.2f}s "
                     f"(asked for {expected_duration:g}s)")
    return lines
