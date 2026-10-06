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

from dataclasses import dataclass
from typing import Any, Iterator

from PIL import Image

from ..scene.compose import render_scene
from ..scene.transitions import blend

__all__ = ["FrameSource", "FrameError"]


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


class FrameSource:
    """Turns a segment plan into pixels, using the shared scene engine."""

    def __init__(self, project: Any, *, canvas: Any, ctx: Any, fps: int = 30,
                 background: Any = None) -> None:
        self.project = project
        self.canvas = canvas
        self.ctx = ctx
        self.fps = max(1, int(fps))
        self.background = background
        self._scenes: dict[str, _SceneEntry] = {}
        for scene in list(getattr(project, "scenes", []) or []):
            scene_id = str(getattr(scene, "id", "") or "")
            if not scene_id:
                continue
            self._scenes[scene_id] = _SceneEntry(
                scene_id=scene_id,
                name=str(getattr(scene, "name", "") or scene_id),
                scene=scene,
                duration=float(getattr(scene, "effective_duration", 0.0) or 0.0),
            )
        self.frames_drawn = 0

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
        for index in range(count):
            absolute = (start + index) / fps
            local = max(0.0, absolute - offset)
            yield self._scene_frame(entry, local)

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
                image_a = render_scene(first.scene, self.ctx,
                                       time=max(0.0, absolute - offset_first),
                                       scene_duration=first.duration, background=self.background)
                image_b = render_scene(second.scene, self.ctx,
                                       time=max(0.0, absolute - offset_second),
                                       scene_duration=second.duration, background=self.background)
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


def describe_source(source: FrameSource) -> str:
    """A short human summary of what will be drawn (for logs and the UI)."""
    return f"{len(source._scenes)} scene(s) at {source.fps} fps"  # noqa: SLF001
