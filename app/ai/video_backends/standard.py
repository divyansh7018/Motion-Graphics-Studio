"""The built-in clip writer - a TEST BACKEND, not a model (sections 79, 99).

This backend exists so the video half of the studio can be exercised end to
end on a machine with no AI video model: it writes a **real** MP4 with FFmpeg
from frames it computes, and every measurement of that file is real.  What it
does *not* do is understand a prompt.

It is therefore labelled everywhere as ``TEST BACKEND (fixture - not an AI
model)``.  Its results are never presented as AI generation, its metadata says
``test_backend``, and the AI Studio shows the label next to any clip it made.

The four operations it implements are genuine transformations, not placeholders:

* ``text_to_video`` - a deterministic moving picture derived from the seed;
* ``image_to_video`` - the source image is moved (pan, tilt, zoom or dolly),
  which is what image-to-video means;
* ``video_to_video`` - the source clip's frames are re-graded with a
  deterministic transform, preserving its timing exactly;
* ``extend`` - the source clip is followed by generated continuation, so the
  original frames are kept and the clip gets longer.

Each one is the same shape a real model's implementation must have, so the job
queue, the validator, the history and the asset pipeline are all exercised for
real by a test that runs on any machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ...image.saving import unique_path
from ..capabilities import AICapabilities
from ..provider import ProviderStatus
from ..types import BackendKind, DeviceRequirement, ProviderState
from ..video import (CAMERA_LABELS, VideoMode, VideoModel, VideoProvider,
                     VideoRequest, VideoResult, VideoState)
from ..video_validation import validate_video_file
from . import frames as frame_tools

__all__ = ["StandardVideoBackend", "TEST_BACKEND_LABEL", "TEST_BACKEND_NOTE"]

#: The label that must appear wherever this backend's output is described.
TEST_BACKEND_LABEL = "TEST BACKEND (fixture - not an AI model)"

TEST_BACKEND_NOTE = (
    "This is the built-in test clip writer. It produces a real video file with "
    "FFmpeg, but it is not an AI model: it does not understand the prompt, it "
    "computes a deterministic picture from the prompt text and the seed. "
    "Use it to try the workflow; install a real backend for real generation.")

TEST_MODEL_ID = "test-clip-writer"


class StandardVideoBackend(VideoProvider):
    """Writes a real clip without a model, and says so (section 79)."""

    id = "standard_video"
    name = TEST_BACKEND_LABEL
    kind = BackendKind.VIDEO
    transport = "builtin"
    version = "1"
    device_requirement = DeviceRequirement.CPU_SUPPORTED
    #: Never True: this backend has no model and does not pretend to.
    is_model = False
    licence = "Part of this application"
    homepage = ""

    def __init__(self, tools: Any = None, *, default_fps: int = 24) -> None:
        self.tools = tools
        self.default_fps = max(1, int(default_fps or 24))
        self._loaded = ""

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        return TEST_BACKEND_NOTE

    def location(self) -> str:
        return "Built in - no files needed beyond FFmpeg"

    def install_hint(self) -> str:
        return "Nothing to install; FFmpeg is required to write the file."

    def uses_network(self) -> bool:
        return False

    def settings_schema(self) -> list:
        from ..provider import SettingField

        return [
            SettingField(name="default_fps", label="Default frame rate",
                         kind="int", default=self.default_fps,
                         help="Used when a request does not ask for one."),
        ]

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if self.tools is None:
            from ...tools.ffmpeg import FFmpegTools, discover_ffmpeg

            self.tools = FFmpegTools(discover_ffmpeg())
        has_ffmpeg = bool(getattr(getattr(self.tools, "discovery", None),
                                  "has_ffmpeg", False))
        if not has_ffmpeg:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=("The test clip writer needs FFmpeg to write a video "
                        "file, and FFmpeg was not found."),
                instructions=["Open the system check to see what is missing.",
                              "Install FFmpeg, or point the application at it "
                              "in Settings."],
                device="cpu")
        return ProviderStatus(
            state=ProviderState.AVAILABLE,
            reason=(f"{TEST_BACKEND_LABEL} - it writes a real file with FFmpeg "
                    f"and does not use a model."),
            instructions=["Install a local video model for real generation."],
            version=self.version, device="cpu")

    def capabilities(self) -> AICapabilities:
        return AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=True,
            video_extend=True, camera_control=True, reference_image=False,
            negative_prompt=False, seed_control=True, duration=True, fps=True,
            resolution_control=True, quality=True, batch=False, strength=True,
            camera_moves=list(frame_tools.CAMERA_MOVES_SUPPORTED),
            min_dimension=64, max_dimension=1920, dimension_multiple=2,
            min_duration=0.5, max_duration=30.0, max_fps=60,
            notes=TEST_BACKEND_NOTE)

    def models(self) -> list[VideoModel]:
        """One entry, so the manager has something concrete to show.

        It is named as a test fixture, and ``is_model`` is False so a report can
        tell it apart from a real model without parsing the name.
        """
        return [VideoModel(
            id=TEST_MODEL_ID, name=TEST_BACKEND_LABEL, backend=self.id,
            kind="fixture", path="", size_bytes=0,
            capabilities=self.capabilities(), device="cpu",
            requirement=DeviceRequirement.CPU_SUPPORTED, status="available",
            version=self.version,
            notes=TEST_BACKEND_NOTE)]

    # -- generation --------------------------------------------------------

    def load(self, model: Any = None) -> None:
        self._loaded = str(getattr(model, "id", "") or TEST_MODEL_ID)

    def unload(self) -> None:
        self._loaded = ""

    def loaded_model(self) -> str:
        return self._loaded

    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        started = self.started()
        report = progress or (lambda _state, _fraction: None)

        if self.tools is None:
            self.status()
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if issues:
            first = issues[0]
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=self.id, model=request.model,
                error=first.message, why="The request cannot be honoured.",
                what_to_do=first.what_to_do, code=first.code,
                seconds=self.started() - started)

        fps = int(request.fps or self.default_fps)
        width, height = frame_tools.resolution_for(
            request.width or 640, request.height or 360)
        frames = self._frame_count(request, fps)
        seed = int(request.seed or 0)
        if seed == 0:
            seed = self._fresh_seed(request)
        report("INITIALIZING", 0.02)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{request.output_format or 'mp4'}"
        target = unique_path(output_dir, request.name_stem or "clip", suffix)

        try:
            stream = self._stream(request, width=width, height=height, fps=fps,
                                  frames=frames, seed=seed, cancel=cancel)
        except (OSError, RuntimeError, ValueError) as exc:
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=self.id, model=request.model,
                error=f"The clip could not be prepared: {exc}",
                why="A source file or the encoder is not usable.",
                what_to_do=("Check the source file, and that FFmpeg is "
                            "installed."),
                code="CLIP_PREPARE_FAILED", seconds=self.started() - started)

        from ...render.encode import stream_encode

        report("RUNNING", 0.05)
        encoded = stream_encode(
            tools=self.tools, frames=stream, output=target, width=width,
            height=height, fps=fps, settings=self._settings(request),
            cancel_token=cancel, timeout=3600.0,
            progress=lambda written: report(
                "PROCESSING", min(0.95, 0.05 + 0.9 * (written / max(1, frames)))))
        if encoded.cancelled:
            _remove_quietly(target)
            return self._cancelled(request, started)
        if not encoded.ok:
            _remove_quietly(target)
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=self.id, model=request.model, error=encoded.error,
                why="The encoder did not produce a file.",
                what_to_do=encoded.what_to_do or
                "Check that FFmpeg is installed and the output folder is writable.",
                code="CLIP_ENCODE_FAILED", seconds=self.started() - started)

        report("SAVING", 0.97)
        check = validate_video_file(
            target, tools=self.tools, expected_width=request.width,
            expected_height=request.height, expected_fps=request.fps,
            expected_duration=request.duration)
        if not check.ok and check.code != "VIDEO_CHECK_NOT_AVAILABLE":
            _remove_quietly(target)
            return VideoResult(
                ok=False, state=VideoState.FAILED, mode=request.mode,
                backend=self.id, model=request.model, error=check.error,
                why=check.why, what_to_do=check.what_to_do, code=check.code,
                seconds=self.started() - started)

        log_event("AI_CLIP_WRITTEN",
                  f"{TEST_BACKEND_LABEL} wrote {target.name}",
                  backend=self.id, mode=request.mode, seed=seed,
                  frames=frames, seconds=round(self.started() - started, 3))
        report("SAVING", 1.0)
        result = VideoResult(
            ok=True, path=target, state=VideoState.COMPLETED, seed=seed,
            model=request.model or TEST_MODEL_ID, backend=self.id,
            mode=request.mode, width=check.width or width,
            height=check.height or height, fps=check.fps or float(fps),
            duration=check.duration or (frames / fps), frames=frames,
            output_format=request.output_format or "mp4",
            has_audio=bool(check.has_audio), requested=request.to_dict(),
            seconds=self.started() - started, mismatch=list(check.mismatch))
        result.quality = {
            "model": result.model, "backend": f"{self.id} ({TEST_BACKEND_LABEL})",
            "resolution": f"{result.width}x{result.height}",
            "fps": round(result.fps, 3), "duration": round(result.duration, 3),
            "seed": result.seed,
            "size_bytes": check.size_bytes,
            "measured_with": check.measured_with,
            "status": ("CHECK NOT AVAILABLE: the clip could not be measured."
                       if check.code == "VIDEO_CHECK_NOT_AVAILABLE"
                       else "measured"),
        }
        result.metadata = {
            "generator": "test-backend",
            "is_ai_model": False,
            "label": TEST_BACKEND_LABEL,
            "prompt": request.prompt,
            "seed": seed, "mode": request.mode,
            "camera": request.camera,
            "note": TEST_BACKEND_NOTE,
        }
        return result

    # -- internals ---------------------------------------------------------

    def _cancelled(self, request: VideoRequest, started: float) -> VideoResult:
        return VideoResult(ok=False, state=VideoState.CANCELLED, cancelled=True,
                           mode=request.mode, backend=self.id,
                           model=request.model,
                           error="Clip generation was cancelled.",
                           seconds=self.started() - started)

    @staticmethod
    def _fresh_seed(request: VideoRequest) -> int:
        """A seed nobody chose is still recorded, so the clip can be remade."""
        import hashlib
        import time as _time

        raw = f"{request.prompt}|{request.mode}|{_time.time()}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") or 1

    def _frame_count(self, request: VideoRequest, fps: int) -> int:
        duration = float(request.duration or 0.0) or min(
            4.0, self.capabilities().max_duration or 4.0)
        return max(1, int(round(duration * fps)))

    def _settings(self, request: VideoRequest) -> Any:
        """The encode settings, in the shape the render encoder expects."""
        from types import SimpleNamespace

        quality = str(request.quality or "medium")
        crf = {"draft": 30, "medium": 24, "high": 20, "ultra": 16}.get(quality, 24)
        preset = {"draft": "veryfast", "medium": "medium", "high": "slow",
                  "ultra": "veryslow"}.get(quality, "medium")
        return SimpleNamespace(
            codec="h264_cpu", encoder_preset=preset, crf=crf, bitrate_kbps=0,
            pixel_format="yuv420p", keyframe_interval=0,
            fps=int(request.fps or self.default_fps), container="mp4",
            audio_codec="aac", audio_bitrate_kbps=192, sample_rate=48000,
            two_pass=False)

    def _stream(self, request: VideoRequest, *, width: int, height: int,
                fps: int, frames: int, seed: int, cancel: Any) -> Any:
        """The frames for this request, as one lazy iterator."""
        mode = request.mode
        if mode == VideoMode.IMAGE_TO_VIDEO:
            return frame_tools.image_frames(
                request.source_image, width, height, frames,
                camera=request.camera, amount=request.camera_amount)
        if mode == VideoMode.TEXT_TO_VIDEO:
            return frame_tools.synthetic_frames(
                request.prompt, seed, width, height, frames,
                camera=request.camera, amount=request.camera_amount)
        if mode == VideoMode.VIDEO_TO_VIDEO:
            source = request.source_video or request.extend_from
            return frame_tools.transform_frames(
                frame_tools.video_frames(source, width=width, height=height,
                                         fps=fps, limit_frames=frames,
                                         tools=self.tools, cancel=cancel),
                seed, width, height)
        if mode == VideoMode.STORYBOARD_TO_VIDEO:
            # A storyboard is generated clip by clip, so each request carries
            # one scene's plan: no storyboard data means nothing to draw.
            entry = dict((getattr(request, "extra", {}) or {}).get(
                "storyboard_entry") or {})
            if not entry:
                raise ValueError(
                    "A storyboard is generated one clip per scene; this request "
                    "has no scene to draw, so nothing was produced.")
            if str(entry.get("source_image") or ""):
                return frame_tools.image_frames(
                    str(entry["source_image"]), width, height, frames,
                    camera=request.camera, amount=request.camera_amount)
            return frame_tools.synthetic_frames(
                str(entry.get("prompt") or request.prompt), seed, width, height,
                frames, camera=request.camera, amount=request.camera_amount)
        if mode == VideoMode.EXTEND:
            return self._extend_stream(request, width=width, height=height,
                                       fps=fps, frames=frames, seed=seed,
                                       cancel=cancel)
        raise ValueError(f"'{mode}' is not a mode this backend implements.")

    def _extend_stream(self, request: VideoRequest, *, width: int, height: int,
                       fps: int, frames: int, seed: int, cancel: Any) -> Any:
        """The original clip, followed by the continuation that was asked for.

        Nothing about the original frames is changed: extending a clip adds to
        it rather than replacing it (section 28).
        """
        original = frame_tools.video_frames(
            request.extend_from, width=width, height=height, fps=fps,
            tools=self.tools, cancel=cancel)
        continuation = frame_tools.synthetic_frames(
            request.prompt or f"continuation {seed}", seed, width, height,
            frames, camera=request.camera, amount=request.camera_amount)

        def combined():
            for chunk in original:
                yield chunk
            for chunk in continuation:
                yield chunk

        return combined()


def _remove_quietly(path: Path) -> None:
    """Delete a rejected output; a missing file is not an error here."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked file is not worth raising
        pass


def camera_labels() -> list[tuple[str, str]]:
    """The camera moves this backend offers, for the UI's list."""
    return [(str(item), CAMERA_LABELS.get(str(item), str(item)))
            for item in frame_tools.CAMERA_MOVES_SUPPORTED]
