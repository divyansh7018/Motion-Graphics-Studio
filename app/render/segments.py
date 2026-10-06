"""Timeline -> render segments (Stage E, sections 32-37).

One timeline, one representation.  The preview, the timeline panel, the audio
mix and the final render all read the same :class:`~app.scene.timing.Timeline`;
this module only slices it into the pieces the encoder wants, so a scene cannot
be one length on screen and another in the finished file.

Nothing here touches a file or a subprocess - it is pure planning, which is why
it is directly testable and why a 60-minute project costs no more to plan than a
30-second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RenderSegment",
    "SegmentPlan",
    "plan_segments",
    "total_frames",
    "frame_times",
    "RenderPlanIssue",
    "validate_plan",
]


@dataclass
class RenderPlanIssue:
    code: str
    message: str
    what_to_do: str = ""
    severity: str = "error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


@dataclass
class RenderSegment:
    """A contiguous run of frames the encoder can work on without backtracking."""

    index: int = 0
    kind: str = "scene"          # "scene" | "transition"
    scene_id: str = ""
    #: Second scene, only for transitions.
    next_scene_id: str = ""
    name: str = ""
    transition: str = "none"
    #: Absolute position on the project timeline, in seconds.
    start: float = 0.0
    end: float = 0.0
    #: Where this segment's own clock starts (scene-local time for frame 0).
    local_start: float = 0.0
    #: Global frame range.  Computed once so segments cannot drift out of step.
    frame_start: int = 0
    frame_count: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def local_time(self):
        """Convert an absolute time to this segment's scene-local clock."""
        return lambda absolute: max(0.0, float(absolute) - self.start + self.local_start)

    @property
    def is_transition(self) -> bool:
        return self.kind == "transition"

    def to_dict(self) -> dict:
        return {
            "index": self.index, "kind": self.kind, "scene_id": self.scene_id,
            "next_scene_id": self.next_scene_id, "name": self.name,
            "transition": self.transition,
            "start": round(self.start, 4), "end": round(self.end, 4),
            "frame_start": self.frame_start, "frame_count": self.frame_count,
        }

    def describe(self) -> str:
        if self.is_transition:
            return (f"#{self.index + 1} transition {self.transition} "
                    f"{self.start:.3f}-{self.end:.3f}s ({self.frame_count} frames)")
        return (f"#{self.index + 1} {self.name or self.scene_id} "
                f"{self.start:.3f}-{self.end:.3f}s ({self.frame_count} frames)")


@dataclass
class SegmentPlan:
    """Every segment, plus the global frame maths they share."""

    segments: list = field(default_factory=list)
    fps: int = 30
    total_frames: int = 0
    duration: float = 0.0
    issues: list = field(default_factory=list)

    @property
    def has_frames(self) -> bool:
        return self.total_frames > 0

    def frames_covered(self) -> int:
        return sum(segment.frame_count for segment in self.segments)

    def describe(self) -> str:
        return (f"{len(self.segments)} segment(s), {self.total_frames} frames at "
                f"{self.fps} fps ({self.duration:.3f}s)")

    def to_dict(self) -> dict:
        return {
            "fps": self.fps, "total_frames": self.total_frames,
            "duration": round(self.duration, 4),
            "segments": [segment.to_dict() for segment in self.segments],
        }


def total_frames(duration: float, fps: int) -> int:
    """Frame count for a duration, rounded once and reused everywhere.

    Rounding per scene is how renders end up a few frames long or short of the
    timeline; rounding the whole thing once and then splitting it cannot drift.
    """
    fps = max(1, int(fps))
    duration = max(0.0, float(duration or 0.0))
    return int(round(duration * fps))


def frame_times(count: int, fps: int) -> list[float]:
    """The timestamp of every frame of a segment."""
    fps = max(1, int(fps))
    return [index / fps for index in range(max(0, int(count)))]


def plan_segments(timeline: Any, *, fps: int = 30) -> SegmentPlan:
    """Split a timeline into scene and transition segments.

    Consecutive scenes may overlap - that overlap *is* the transition, and the
    frames in it belong to both scenes.  So the plan is: the plain part of each
    scene, then the blended part where it hands over to the next one.  The
    segments tile the timeline exactly, with no gap and no frame drawn twice.
    """
    plan = SegmentPlan(fps=max(1, int(fps)))
    timings = list(getattr(timeline, "timings", []) or [])
    plan.duration = float(getattr(timeline, "total_duration", 0.0) or 0.0)
    plan.total_frames = total_frames(plan.duration, plan.fps)
    if not timings:
        plan.issues.append(RenderPlanIssue(
            "TIMELINE_EMPTY", "The timeline has no scenes, so there is nothing to render.",
            "Add at least one scene to the project.",
        ))
        return plan

    count = len(timings)
    cursor = float(timings[0].start)

    for position, timing in enumerate(timings):
        scene_start = float(timing.start)
        scene_end = float(timing.end)
        # A scene never starts before the previous one finished.
        plain_start = max(cursor, scene_start)

        next_start = None
        if position + 1 < count:
            next_start = float(timings[position + 1].start)

        handover = scene_end
        if next_start is not None and next_start < scene_end:
            handover = next_start

        if handover > plain_start:
            plan.segments.append(RenderSegment(
                index=len(plan.segments), kind="scene",
                scene_id=str(getattr(timing, "scene_id", "") or ""),
                name=str(getattr(timing, "name", "") or ""),
                start=plain_start, end=handover,
                local_start=max(0.0, plain_start - scene_start),
            ))

        if next_start is not None and next_start < scene_end:
            kind = str(getattr(timing, "transition_out", "") or "none")
            if kind in ("", "none"):
                kind = str(getattr(timings[position + 1], "transition_in", "") or "none")
            plan.segments.append(RenderSegment(
                index=len(plan.segments), kind="transition",
                scene_id=str(getattr(timing, "scene_id", "") or ""),
                next_scene_id=str(getattr(timings[position + 1], "scene_id", "") or ""),
                name=f"{getattr(timing, 'name', '')} -> {getattr(timings[position + 1], 'name', '')}",
                transition=kind,
                start=next_start, end=scene_end,
                local_start=max(0.0, next_start - scene_start),
            ))

        cursor = max(cursor, scene_end)

    _assign_frames(plan)
    plan.issues.extend(validate_plan(plan))
    return plan


def _assign_frames(plan: SegmentPlan) -> None:
    """Give every segment its slice of the global frame count.

    Frames are allocated by index so the total always equals
    :func:`total_frames` for the whole timeline - the sum of the parts cannot
    disagree with the whole.
    """
    fps = max(1, plan.fps)
    if not plan.segments:
        return
    plan.total_frames = total_frames(plan.segments[-1].end, fps)
    cursor = 0
    for index, segment in enumerate(plan.segments):
        segment.frame_start = cursor
        # The last segment takes whatever is left, so rounding lands in one place.
        if index == len(plan.segments) - 1:
            segment.frame_count = max(0, plan.total_frames - cursor)
        else:
            boundary = int(round(segment.end * fps))
            segment.frame_count = max(0, boundary - cursor)
            cursor = boundary
        if index == len(plan.segments) - 1:
            cursor += segment.frame_count


def validate_plan(plan: SegmentPlan) -> list:
    """Structural checks on a plan before a single frame is drawn."""
    issues: list[RenderPlanIssue] = []
    if not plan.segments:
        return issues

    covered = plan.frames_covered()
    if covered != plan.total_frames:
        issues.append(RenderPlanIssue(
            "PLAN_FRAME_MISMATCH",
            f"The segments cover {covered} frames but the timeline needs "
            f"{plan.total_frames}.",
            "This is an internal timing bug - please report it with the log.",
        ))

    expected = 0
    for segment in plan.segments:
        if segment.frame_start != expected:
            issues.append(RenderPlanIssue(
                "PLAN_GAP",
                f"Segment {segment.index + 1} starts at frame {segment.frame_start} "
                f"but the previous one ended at {expected}.",
                "This is an internal timing bug - please report it with the log.",
            ))
            break
        if segment.duration <= 0:
            issues.append(RenderPlanIssue(
                "PLAN_EMPTY_SEGMENT",
                f"Segment {segment.index + 1} ({segment.name}) has no length.",
                "Give the scene a duration greater than zero.",
            ))
        if segment.frame_count <= 0:
            issues.append(RenderPlanIssue(
                "PLAN_NO_FRAMES",
                f"Segment {segment.index + 1} ({segment.name}) is shorter than one "
                f"frame at {plan.fps} fps.",
                "Lengthen the scene or lower the frame rate.",
            ))
        expected += segment.frame_count

    if plan.segments[-1].end < plan.duration - (1.0 / max(1, plan.fps)):
        issues.append(RenderPlanIssue(
            "PLAN_SHORT_OF_TIMELINE",
            f"The segments stop at {plan.segments[-1].end:.3f}s but the timeline is "
            f"{plan.duration:.3f}s long.",
            "This is an internal timing bug - please report it with the log.",
        ))
    return issues
