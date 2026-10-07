"""Drawing the frames of a render (Stage E, sections 32-37, 41).

This is the only place in Stage E that draws a picture, and it draws with the
same Stage D scene engine the preview uses - :func:`app.scene.compose.render_scene`
and :func:`app.scene.transitions.blend`.  There is deliberately no second,
"simpler" renderer for the final video: if it looks right in the preview it is
the same code that produced the file (directive section 12).

Frames are produced lazily, one at a time, so the caller can pipe them straight
to the encoder without ever holding more than one in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from PIL import Image

from ..scene.compose import render_scene
from ..scene.transitions import blend

__all__ = ["FrameSource", "FrameError", "VIDEO_SCENE_TYPE", "video_spec_for"]

#: A scene whose content is a video clip rather than drawn elements (Stage G).
#: The clip is decoded frame by frame while rendering, so a generated clip
#: becomes an ordinary part of the timeline and the final video.
VIDEO_SCENE_TYPE = "video"


class FrameError(Exception):
    """Raised when a frame cannot be drawn.

    Carries the friendly explanation so the render job can report what happened,
    why, and what to do about it - never a bare traceback.
    """

    def __init__(self, message: str, *, what_to_do: str = "", scene_id: str = "",
                 technical: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.what_to_do = what_to_do
        self.scene_id = scene_id
        self.technical = technical


@dataclass
class _SceneEntry:
    scene_id: str
    name: str
    scene: Any
    duration: float
    #: The clip this scene plays, when it is a video scene (Stage G).
    video_path: str = ""
    video_spec: dict = field(default_factory=dict)


class FrameSource:
    """Turns a segment plan into pixels, using the shared scene engine."""

    def __init__(self, project: Any, *, canvas: Any, ctx: Any, fps: int = 30,
                 background: Any = None, tools: Any = None,
                 cancel: Any = None) -> None:
        self.project = project
        self.canvas = canvas
        self.ctx = ctx
        self.fps = max(1, int(fps))
        self.background = background
        #: Used to decode video scenes; discovered lazily so nothing is probed
        #: until a clip actually has to be read (section 41).
        self.tools = tools
        self.cancel = cancel
        #: Notes about what really happened (a held frame, a short clip), so a
        #: render can report them instead of hiding them.
        self.notes: list[str] = []
        self._scenes: dict[str, _SceneEntry] = {}
        for scene in list(getattr(project, "scenes", []) or []):
            scene_id = str(getattr(scene, "id", "") or "")
            if not scene_id:
                continue
            spec = video_spec_for(scene, ctx)
            self._scenes[scene_id] = _SceneEntry(
                scene_id=scene_id,
                name=str(getattr(scene, "name", "") or scene_id),
                scene=scene,
                duration=float(getattr(scene, "effective_duration", 0.0) or 0.0),
                video_path=str(spec.get("path", "") or ""),
                video_spec=spec,
            )
        self.frames_drawn = 0
        self._last_video_frame: dict[str, Any] = {}

    # -- lookup ----------------------------------------------------------

    def scene_for(self, scene_id: str) -> _SceneEntry:
        entry = self._scenes.get(str(scene_id))
        if entry is None:
            raise FrameError(
                f"The timeline refers to a scene ('{scene_id}') that is not in the project.",
                what_to_do="This usually means the project file was edited outside the "
                           "application. Reload the project and try again.",
                scene_id=str(scene_id),
            )
        return entry

    # -- frames ----------------------------------------------------------

    def segment_frames(self, segment: Any) -> Iterator[Image.Image]:
        """Yield every frame of one segment, in order.

        Frames are addressed by their *global* index divided by the frame rate, so
        a segment that starts part-way through the video still lines up exactly
        with the audio - no accumulated rounding error.
        """
        fps = self.fps
        start = segment.frame_start
        count = max(0, int(segment.frame_count))
        if count <= 0:
            return

        if segment.is_transition:
            yield from self._transition_frames(segment, start, count, fps)
            return

        entry = self.scene_for(segment.scene_id)
        offset = float(segment.start) - float(segment.local_start)
        if entry.video_path:
            local_start = max(0.0, (start / fps) - offset)
            yield from self._video_segment_frames(entry, local_start, count)
            return
        for index in range(count):
            absolute = (start + index) / fps
            local = max(0.0, absolute - offset)
            yield self._scene_frame(entry, local)

    # -- video scenes -----------------------------------------------------

    def _tools(self) -> Any:
        if self.tools is None:
            from ..tools.ffmpeg import FFmpegTools, discover_ffmpeg

            self.tools = FFmpegTools(discover_ffmpeg())
        return self.tools

    def _video_segment_frames(self, entry: _SceneEntry, local_start: float,
                              count: int) -> Iterator[Image.Image]:
        """Decode a scene's clip, frame by frame, at this project's frame rate.

        One decoder serves the whole segment, so a clip of any length costs one
        frame of memory.  When the clip is shorter than the scene the last frame
        is held, and that is recorded in :attr:`notes` rather than passed off as
        a longer clip.
        """
        from ..ai.video_backends.frames import video_frames

        width = int(getattr(self.canvas, "width", 0) or 0)
        height = int(getattr(self.canvas, "height", 0) or 0)
        if width <= 0 or height <= 0:
            raise FrameError(
                f"The scene '{entry.name}' cannot be drawn: the canvas size is "
                f"unknown.", what_to_do="Set the project's resolution and "
                "render again.", scene_id=entry.scene_id)
        if not Path(entry.video_path).is_file():
            raise FrameError(
                f"The clip for the scene '{entry.name}' is missing: "
                f"{entry.video_path}",
                what_to_do="Relink the clip in the Asset Manager, or generate "
                           "it again.",
                scene_id=entry.scene_id)
        try:
            stream = video_frames(entry.video_path, width=width, height=height,
                                  fps=self.fps, limit_frames=int(count),
                                  tools=self._tools(), cancel=self.cancel,
                                  start=float(local_start))
            produced = 0
            last = None
            for chunk in stream:
                if self.cancel is not None and self.cancel.is_cancelled():
                    break
                image = Image.frombytes("RGB", (width, height), chunk)
                last = image
                produced += 1
                self.frames_drawn += 1
                yield image
            while produced < int(count):
                if last is None:
                    raise FrameError(
                        f"The clip for the scene '{entry.name}' produced no "
                        f"frames.", what_to_do="Check the clip plays in a "
                        "player, then render again.", scene_id=entry.scene_id)
                note = (f"The clip for '{entry.name}' is shorter than the scene; "
                        f"the last frame was held for the rest.")
                if note not in self.notes:
                    self.notes.append(note)
                produced += 1
                self.frames_drawn += 1
                yield last
        except FrameError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise FrameError(
                f"The clip for the scene '{entry.name}' could not be read.",
                what_to_do="Check FFmpeg is installed and the clip opens in a "
                           "player, then render again.",
                scene_id=entry.scene_id,
                technical=f"{type(exc).__name__}: {exc}") from exc
        self._last_video_frame[entry.scene_id] = last

    def _video_frame_at(self, entry: _SceneEntry, local_time: float) -> Image.Image:
        """One frame of a clip at a given time (used inside transitions)."""
        for frame in self._video_segment_frames(entry, max(0.0, local_time), 1):
            return frame
        raise FrameError(
            f"The clip for the scene '{entry.name}' has no frame at "
            f"{local_time:.2f}s.",
            what_to_do="Trim the scene to the clip's length, or use a cut "
                       "instead of a transition.",
            scene_id=entry.scene_id)

    def _scene_frame(self, entry: _SceneEntry, local_time: float) -> Image.Image:
        try:
            image = render_scene(entry.scene, self.ctx, time=local_time,
                                 scene_duration=entry.duration, background=self.background)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise FrameError(
                f"The scene '{entry.name}' could not be drawn.",
                what_to_do="Check the scene's elements for a missing image or an invalid "
                           "value, then render again. The technical detail is in the log.",
                scene_id=entry.scene_id, technical=f"{type(exc).__name__}: {exc}",
            ) from exc
        self.frames_drawn += 1
        return image.convert("RGB")

    def _transition_frames(self, segment: Any, start: int, count: int,
                           fps: int) -> Iterator[Image.Image]:
        first = self.scene_for(segment.scene_id)
        second = self.scene_for(segment.next_scene_id)
        span = max(1e-6, float(segment.duration))
        offset_first = float(segment.start) - float(segment.local_start)
        # The incoming scene's own clock started when the overlap began.
        offset_second = float(segment.start)
        kind = str(getattr(segment, "transition", "") or "fade")

        for index in range(count):
            absolute = (start + index) / fps
            fraction = min(1.0, max(0.0, (absolute - float(segment.start)) / span))
            try:
                image_a = self._frame_for(first, max(0.0, absolute - offset_first))
                image_b = self._frame_for(second, max(0.0, absolute - offset_second))
                frame = blend(image_a, image_b, fraction, kind, canvas=self.canvas)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                raise FrameError(
                    f"The transition between '{first.name}' and '{second.name}' could not "
                    f"be drawn.",
                    what_to_do="Try a different transition, or set the transition to "
                               "'Cut'. The technical detail is in the log.",
                    scene_id=first.scene_id, technical=f"{type(exc).__name__}: {exc}",
                ) from exc
            self.frames_drawn += 1
            yield frame.convert("RGB")

    def _frame_for(self, entry: _SceneEntry, local_time: float) -> Image.Image:
        """One frame of a scene, whether it draws elements or plays a clip."""
        if entry.video_path:
            return self._video_frame_at(entry, local_time)
        return self._scene_frame(entry, local_time)

    # -- streaming helpers ----------------------------------------------

    @staticmethod
    def frame_bytes(image: Image.Image, *, width: int, height: int) -> bytes:
        """Resize if needed and return raw RGB24 bytes for the encoder.

        Resizing only happens when the drawn frame does not match the requested
        output size - normally the canvas already matches, so the picture is not
        resampled at all (directive section 41).
        """
        if image.size != (int(width), int(height)):
            image = image.resize((int(width), int(height)), Image.LANCZOS)
        return image.tobytes("raw", "RGB")

    def stream(self, segment: Any, *, width: int, height: int) -> Iterator[bytes]:
        """Yield raw frame bytes for a whole segment."""
        for image in self.segment_frames(segment):
            yield self.frame_bytes(image, width=width, height=height)


def video_spec_for(scene: Any, ctx: Any = None) -> dict:
    """The clip a scene plays, if it is a video scene.

    The clip is named in ``scene.extra["video"]`` (that is what "Send to Scene"
    writes) and resolved through the project's own asset table, so the scene
    keeps working when the project folder is moved - the stored path is relative
    to the project, exactly like an image element's.
    """
    if str(getattr(scene, "type", "")) != VIDEO_SCENE_TYPE:
        return {}
    extra = dict(getattr(scene, "extra", {}) or {})
    spec = extra.get("video")
    if not isinstance(spec, dict):
        return {}
    resolved = dict(spec)
    asset_id = str(spec.get("asset_id", "") or "")
    paths = getattr(ctx, "asset_paths", None) or {}
    if asset_id and asset_id in paths:
        resolved["path"] = str(paths[asset_id])
    elif not resolved.get("path") and str(spec.get("path", "")).strip():
        resolved["path"] = str(spec.get("path", "")).strip()
    return resolved


def describe_source(source: FrameSource) -> str:
    """A short human summary of what will be drawn (for logs and the UI)."""
    clips = sum(1 for entry in source._scenes.values() if entry.video_path)  # noqa: SLF001
    text = f"{len(source._scenes)} scene(s) at {source.fps} fps"  # noqa: SLF001
    if clips:
        text += f", {clips} of them video"
    return text
