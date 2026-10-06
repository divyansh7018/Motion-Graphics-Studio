"""Narration-driven timing (Stage D).

The timeline decides how long each scene runs.  The rule is the one from the
Stage C brief, carried forward: **measured narration wins**.  If a scene has a
narration track whose duration was measured from a real WAV file, that duration
is authoritative; a typed-in number is a fallback for scenes with no voiceover.

There is deliberately **no maximum video length** anywhere here.  A ten-second
Short and a three-hour documentary use the same code path, and the tests cover
both.

Padding is opt-in and explicit: ``head`` and ``tail`` add a beat of silence
before and after the voice so the words are not cut off by a scene change.
Nothing is added silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

__all__ = [
    "DURATION_SOURCES",
    "MIN_SCENE_DURATION",
    "DEFAULT_SCENE_DURATION",
    "TimingOptions",
    "SceneTiming",
    "Timeline",
    "build_timeline",
]

#: Where a scene's length came from.  Reported to the user, never guessed.
DURATION_SOURCES: tuple[str, ...] = ("narration", "manual", "default")

#: A scene shorter than this is a single frame at any sane frame rate and is
#: almost always a mistake, so it is raised to this floor and reported.
MIN_SCENE_DURATION = 0.04

#: Used only when a scene has neither narration nor a manual duration.
DEFAULT_SCENE_DURATION = 3.0


@dataclass
class TimingOptions:
    """Knobs for building a timeline.  All are explicit; nothing is global."""

    #: Silence added before the narration in each scene.
    head: float = 0.0
    #: Silence added after the narration in each scene.
    tail: float = 0.5
    #: Length for scenes with no narration and no manual duration.
    default_duration: float = DEFAULT_SCENE_DURATION
    #: Raise every scene to at least this many seconds.
    min_duration: float = MIN_SCENE_DURATION
    #: Let a transition overlap into the next scene instead of adding time.
    overlap_transitions: bool = True

    def clamp(self) -> "TimingOptions":
        return TimingOptions(
            head=max(0.0, self.head),
            tail=max(0.0, self.tail),
            default_duration=max(0.0, self.default_duration),
            min_duration=max(0.0, self.min_duration),
            overlap_transitions=self.overlap_transitions,
        )


@dataclass
class SceneTiming:
    """One scene's place on the timeline."""

    scene_id: str = ""
    index: int = 0
    name: str = ""
    start: float = 0.0
    duration: float = 0.0
    source: str = "default"
    #: The measured narration length, kept so the UI can show why this is the length.
    narration_duration: float = 0.0
    #: What the user typed, kept for the same reason.
    manual_duration: float = 0.0
    #: True when the requested length was raised to the minimum.
    raised: bool = False
    transition_in: str = "none"
    transition_out: str = "none"
    transition_in_duration: float = 0.0
    transition_out_duration: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    def local_time(self, absolute: float) -> float:
        """Seconds into this scene for an absolute timeline position."""
        return max(0.0, min(self.duration, absolute - self.start))

    def contains(self, absolute: float) -> bool:
        return self.start <= absolute < self.end

    def source_label(self) -> str:
        return {
            "narration": "From the narration file",
            "manual": "Set manually",
            "default": "Default length",
        }.get(self.source, self.source)

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "index": self.index,
            "name": self.name,
            "start": round(self.start, 4),
            "duration": round(self.duration, 4),
            "end": round(self.end, 4),
            "source": self.source,
            "narration_duration": round(self.narration_duration, 4),
            "manual_duration": round(self.manual_duration, 4),
            "raised": self.raised,
            "transition_in": self.transition_in,
            "transition_out": self.transition_out,
        }


@dataclass
class Timeline:
    """The whole sequence, with lookups the preview and renderer both need."""

    timings: list = field(default_factory=list)
    options: TimingOptions = field(default_factory=TimingOptions)

    @property
    def total_duration(self) -> float:
        """The timeline's real length: the end of the last scene.

        With overlapping transitions a scene can start before the previous one
        ends, so summing durations would overstate the length.  The last scene's
        end is the truth the player and renderer both use.
        """
        return max((timing.end for timing in self.timings), default=0.0)

    @property
    def scene_count(self) -> int:
        return len(self.timings)

    @property
    def is_empty(self) -> bool:
        return not self.timings

    def total_frames(self, fps: int) -> int:
        """Total frames at a given frame rate (never stored, always derived)."""
        rate = max(1, int(fps))
        return int(round(self.total_duration * rate))

    def at(self, absolute: float) -> Optional[tuple]:
        """``(timing, local_time)`` for a timeline position, or None past the end."""
        if not self.timings:
            return None
        moment = max(0.0, float(absolute))
        for timing in self.timings:
            if timing.contains(moment):
                return timing, timing.local_time(moment)
        if moment >= self.total_duration:
            last = self.timings[-1]
            return last, last.duration
        return None

    def index_at(self, absolute: float) -> int:
        found = self.at(absolute)
        return found[0].index if found else -1

    def timing_for(self, scene_id: str) -> Optional[SceneTiming]:
        for timing in self.timings:
            if timing.scene_id == scene_id:
                return timing
        return None

    def scenes_raised_to_minimum(self) -> list:
        return [timing for timing in self.timings if timing.raised]

    def format_total(self) -> str:
        """``1:02:03.5`` - readable however long the video is."""
        return format_duration(self.total_duration)

    def to_dict(self) -> dict:
        return {
            "total_duration": round(self.total_duration, 4),
            "scene_count": self.scene_count,
            "scenes": [timing.to_dict() for timing in self.timings],
            "options": {
                "head": self.options.head,
                "tail": self.options.tail,
                "default_duration": self.options.default_duration,
                "min_duration": self.options.min_duration,
                "overlap_transitions": self.options.overlap_transitions,
            },
        }


def format_duration(seconds: float) -> str:
    """``H:MM:SS.t`` for long videos, ``M:SS.t`` for short ones."""
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        total = 0.0
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes}:{secs:04.1f}"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def build_timeline(scenes: Sequence[Any], options: Optional[TimingOptions] = None) -> Timeline:
    """Work out when every scene starts and how long it runs.

    ``scenes`` is any sequence of :class:`app.project.model.SceneSpec`.  The
    function does not mutate them, so it is safe to call from a background job
    while the UI still holds the project.
    """
    opts = (options or TimingOptions()).clamp()
    timeline = Timeline(options=opts)
    cursor = 0.0

    for index, scene in enumerate(scenes):
        # Disabled scenes are dropped from the cut and consume no timeline
        # (directive section 34).  They stay in the project.
        if not getattr(scene, "enabled", True):
            continue
        narration = getattr(scene, "narration", None)
        narration_duration = _number(getattr(narration, "duration", 0.0))
        manual = _number(getattr(scene, "duration", 0.0))

        if narration_duration > 0:
            duration = narration_duration + opts.head + opts.tail
            source = "narration"
        elif manual > 0:
            duration = manual
            source = "manual"
        else:
            duration = opts.default_duration
            source = "default"

        raised = False
        if duration < opts.min_duration:
            duration = opts.min_duration
            raised = True

        transition_in = str(getattr(getattr(scene, "transition_in", None), "type", "none") or "none")
        transition_out = str(getattr(getattr(scene, "transition_out", None), "type", "none") or "none")
        transition_in_duration = _number(getattr(getattr(scene, "transition_in", None), "duration", 0.0))
        transition_out_duration = _number(getattr(getattr(scene, "transition_out", None), "duration", 0.0))

        # Standard editor behaviour: the incoming scene starts while the
        # outgoing one is still on screen, so a cross-fade costs no extra time.
        # The overlap can never exceed half of either scene.
        overlap = 0.0
        if index and opts.overlap_transitions and timeline.timings:
            previous = timeline.timings[-1]
            overlap = min(previous.transition_out_duration, transition_in_duration)
            overlap = max(0.0, min(overlap, previous.duration * 0.5, duration * 0.5))

        start = max(0.0, cursor - overlap)
        timeline.timings.append(SceneTiming(
            scene_id=str(getattr(scene, "id", "") or ""),
            index=index,
            name=str(getattr(scene, "name", "") or ""),
            start=start,
            duration=duration,
            source=source,
            narration_duration=narration_duration,
            manual_duration=manual,
            raised=raised,
            transition_in=transition_in,
            transition_out=transition_out,
            transition_in_duration=transition_in_duration,
            transition_out_duration=transition_out_duration,
        ))
        cursor = start + duration

    return timeline
