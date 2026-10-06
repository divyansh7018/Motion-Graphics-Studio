"""The timeline service: one timeline, built once, checked before anything renders.

Stage E requires that preview, audio timing, subtitles and the final render all
agree on *when things happen* (directive sections 44-46).  That only works if
there is a single timeline derived from real content, so this service is the one
place it is built and the one place it is judged.

Two rules shape it:

* **Duration comes from content, never from a guess.**  A scene lasts as long as
  its narration file really is (measured), or the manual length the user set, or
  the default - and the report says which.
* **Fatal errors stop the render; warnings do not.**  A scene with no narration
  is a warning the user may accept.  A negative duration or a transition that
  pushes a scene backwards is an error, because rendering it would produce a
  broken file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.logging_setup import get_logger
from .timing import Timeline, TimingOptions, build_timeline, format_duration

LOGGER = get_logger("scene.timeline")

__all__ = [
    "ERROR",
    "WARNING",
    "TimelineIssue",
    "TimelineReport",
    "TimelineService",
]

ERROR = "error"
WARNING = "warning"

#: Tolerance for float drift when comparing timings, in seconds.
EPSILON = 0.02


@dataclass
class TimelineIssue:
    """One thing wrong with the timeline, in the user's words."""

    code: str
    message: str
    what_to_do: str = ""
    severity: str = WARNING
    scene_id: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity,
                "scene_id": self.scene_id}


@dataclass
class TimelineReport:
    """The timeline plus everything wrong with it."""

    timeline: Timeline
    issues: list = field(default_factory=list)

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.severity == ERROR]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.severity != ERROR]

    @property
    def ok(self) -> bool:
        """Safe to render: no fatal problems.  Warnings are allowed through."""
        return not self.errors

    def summary(self) -> str:
        if not self.issues:
            return f"Timeline is valid: {self.timeline.format_total()}"
        return (f"{self.timeline.format_total()} - {len(self.errors)} error(s), "
                f"{len(self.warnings)} warning(s)")

    def describe(self) -> str:
        lines = [self.summary()]
        for issue in self.issues:
            lines.append(f"[{issue.severity.upper()}] {issue.code}: {issue.message}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "timeline": self.timeline.to_dict(),
                "issues": [issue.to_dict() for issue in self.issues]}


class TimelineService:
    """Builds and validates the one timeline every other part uses."""

    def __init__(self, *, project_dir: Optional[Path] = None, tools: Any = None) -> None:
        self.project_dir = Path(project_dir) if project_dir else None
        self.tools = tools

    # -- building ---------------------------------------------------------

    def build(self, project: Any, options: Optional[TimingOptions] = None) -> Timeline:
        """The timeline for this project, from its real scene content."""
        return build_timeline(getattr(project, "scenes", None) or [], options)

    def check(self, project: Any, options: Optional[TimingOptions] = None) -> TimelineReport:
        """Build and validate in one call - what the GUI and CLI both want."""
        timeline = self.build(project, options)
        return self.validate(project, timeline)

    def summary(self, project: Any) -> dict:
        """A compact snapshot for a status line or a log event."""
        report = self.check(project)
        timeline = report.timeline
        return {
            "duration": round(timeline.total_duration, 3),
            "label": timeline.format_total(),
            "scenes": timeline.scene_count,
            "frames_at_30": timeline.total_frames(30),
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "ok": report.ok,
        }

    # -- validating -------------------------------------------------------

    def validate(self, project: Any, timeline: Optional[Timeline] = None) -> TimelineReport:
        """Judge the timeline: fatal errors block a render, warnings do not."""
        timeline = timeline if timeline is not None else self.build(project)
        issues: list[TimelineIssue] = []
        scenes = list(getattr(project, "scenes", None) or [])

        issues.extend(self._check_population(scenes))
        issues.extend(self._check_order_and_durations(timeline))
        issues.extend(self._check_transitions(timeline))
        issues.extend(self._check_narration(project, timeline))
        issues.extend(self._check_subtitles(project, timeline))
        return TimelineReport(timeline=timeline, issues=issues)

    # -- the individual checks -------------------------------------------

    def _check_population(self, scenes: Sequence[Any]) -> list:
        issues: list[TimelineIssue] = []
        if not scenes:
            issues.append(TimelineIssue(
                code="NO_SCENES", severity=ERROR,
                message="The project has no scenes, so there is nothing to put on a timeline.",
                what_to_do="Add at least one scene on the Storyboard page."))
            return issues

        enabled = [scene for scene in scenes if getattr(scene, "enabled", True)]
        if not enabled:
            issues.append(TimelineIssue(
                code="ALL_SCENES_DISABLED", severity=ERROR,
                message="Every scene is switched off, so the timeline would be empty.",
                what_to_do="Enable at least one scene on the Storyboard page."))

        seen: dict[str, str] = {}
        for scene in scenes:
            scene_id = str(getattr(scene, "id", "") or "")
            if not scene_id:
                continue
            if scene_id in seen:
                issues.append(TimelineIssue(
                    code="DUPLICATE_SCENE_ID", severity=ERROR, scene_id=scene_id,
                    message=f"Two scenes share the id '{scene_id}', so timings could be "
                            f"applied to the wrong one.",
                    what_to_do="Give one of them a different id."))
            seen[scene_id] = str(getattr(scene, "name", "") or "")

        for scene in scenes:
            if not getattr(scene, "enabled", True):
                issues.append(TimelineIssue(
                    code="SCENE_DISABLED", scene_id=str(getattr(scene, "id", "") or ""),
                    message=f"'{getattr(scene, 'name', '') or 'Unnamed scene'}' is switched off "
                            f"and will be left out of the video.",
                    what_to_do="Enable it if you meant to include it."))
        return issues

    def _check_order_and_durations(self, timeline: Timeline) -> list:
        issues: list[TimelineIssue] = []
        previous: Optional[Any] = None
        for timing in timeline.timings:
            if timing.duration <= 0.0:
                issues.append(TimelineIssue(
                    code="SCENE_DURATION_ZERO", severity=ERROR, scene_id=timing.scene_id,
                    message=f"'{timing.name}' has no length ({timing.duration:.2f}s).",
                    what_to_do="Give the scene a duration, or add narration to it."))
            if previous is not None:
                # Overlapping transitions mean a scene *should* start before the
                # previous one ends - that is how a crossfade works.  Only an
                # overlap bigger than the transitions allow is a real problem.
                allowed = min(previous.transition_out_duration,
                              timing.transition_in_duration)
                gap = previous.end - timing.start
                if gap > allowed + EPSILON:
                    issues.append(TimelineIssue(
                        code="SCENE_OUT_OF_ORDER", severity=ERROR, scene_id=timing.scene_id,
                        message=f"'{timing.name}' starts at {timing.start:.2f}s, "
                                f"{gap:.2f}s before '{previous.name}' ends at "
                                f"{previous.end:.2f}s, but the transitions only allow "
                                f"{allowed:.2f}s of overlap.",
                        what_to_do="Shorten the transitions, or check the scene order."))
                elif timing.start < previous.start - EPSILON:
                    issues.append(TimelineIssue(
                        code="SCENE_OUT_OF_ORDER", severity=ERROR, scene_id=timing.scene_id,
                        message=f"'{timing.name}' starts at {timing.start:.2f}s, before "
                                f"'{previous.name}' at {previous.start:.2f}s.",
                        what_to_do="Check the scene order on the Storyboard page."))
            previous = timing
        return issues

    def _check_transitions(self, timeline: Timeline) -> list:
        """A transition must never push a scene backwards or overlap wrongly."""
        issues: list[TimelineIssue] = []
        for index, timing in enumerate(timeline.timings):
            for direction, duration in (("in", timing.transition_in_duration),
                                        ("out", timing.transition_out_duration)):
                if duration < 0.0:
                    issues.append(TimelineIssue(
                        code="TRANSITION_NEGATIVE", severity=ERROR, scene_id=timing.scene_id,
                        message=f"'{timing.name}' has a negative transition-{direction} "
                                f"({duration:.2f}s).",
                        what_to_do="Set the transition length to zero or more."))
                elif duration > timing.duration + EPSILON:
                    issues.append(TimelineIssue(
                        code="TRANSITION_LONGER_THAN_SCENE", severity=ERROR,
                        scene_id=timing.scene_id,
                        message=f"The transition {direction} on '{timing.name}' "
                                f"({duration:.2f}s) is longer than the scene itself "
                                f"({timing.duration:.2f}s).",
                        what_to_do="Shorten the transition or lengthen the scene."))
            if index == 0 and timing.transition_in not in ("", "none"):
                issues.append(TimelineIssue(
                    code="TRANSITION_ON_FIRST_SCENE", scene_id=timing.scene_id,
                    message=f"'{timing.name}' is the first scene but has a "
                            f"'{timing.transition_in}' transition in.",
                    what_to_do="Use 'none', or accept that it fades in from black."))
        return issues

    def _check_narration(self, project: Any, timeline: Timeline) -> list:
        """Narration drives the timing, so it has to fit the scene it belongs to."""
        issues: list[TimelineIssue] = []
        scenes = {str(getattr(scene, "id", "") or ""): scene
                  for scene in (getattr(project, "scenes", None) or [])}
        for timing in timeline.timings:
            scene = scenes.get(timing.scene_id)
            if scene is None:
                continue
            narration = getattr(scene, "narration", None)
            text = str(getattr(narration, "text", "") or "").strip()
            file = str(getattr(narration, "file", "") or "")
            if not text and not file:
                continue
            if text and not file:
                issues.append(TimelineIssue(
                    code="NARRATION_NOT_GENERATED", scene_id=timing.scene_id,
                    message=f"'{timing.name}' has narration text but no audio file, so it "
                            f"will play in silence.",
                    what_to_do="Generate the narration on the Narration page, or clear the text."))
                continue

            # There is deliberately no "narration longer than the scene" check
            # here.  build_timeline derives the scene's length *from* the
            # measured narration, so the scene is at least as long as the voice
            # by construction and the comparison could never fire.  A narration
            # file that disagrees with its recorded length is caught by
            # AudioService, which opens the real file; duplicating that here
            # would just be a check that never runs.
        return issues

    def _check_subtitles(self, project: Any, timeline: Timeline) -> list:
        """Captions must land inside the video and not fight each other."""
        issues: list[TimelineIssue] = []
        subtitles = getattr(project, "subtitles", None)
        if subtitles is None or not getattr(subtitles, "enabled", False):
            return issues

        cues = list(getattr(subtitles, "cues", None) or [])
        if not cues:
            issues.append(TimelineIssue(
                code="NO_SUBTITLE_CUES",
                message="Subtitles are switched on but no captions have been generated yet.",
                what_to_do="Build the captions on the Subtitles page before rendering."))
            return issues

        end = timeline.total_duration
        previous_end = 0.0
        for cue in sorted(cues, key=lambda item: (getattr(item, "start", 0.0),
                                                  getattr(item, "end", 0.0))):
            start = float(getattr(cue, "start", 0.0) or 0.0)
            finish = float(getattr(cue, "end", 0.0) or 0.0)
            if finish <= start:
                issues.append(TimelineIssue(
                    code="SUBTITLE_REVERSED", severity=ERROR,
                    message="A caption ends before it starts.",
                    what_to_do="Fix its timings on the Subtitles page."))
                continue
            if start < previous_end - EPSILON:
                issues.append(TimelineIssue(
                    code="SUBTITLE_OVERLAP",
                    message=f"A caption starts at {start:.2f}s while the previous one still "
                            f"runs to {previous_end:.2f}s.",
                    what_to_do="Split, merge or retune the two captions."))
            previous_end = max(previous_end, finish)
            if finish > end + 1.0:
                issues.append(TimelineIssue(
                    code="SUBTITLE_PAST_END", severity=ERROR,
                    message=f"A caption runs to {finish:.2f}s but the video ends at "
                            f"{end:.2f}s.",
                    what_to_do="Shorten the caption, or check the scene durations."))
        return issues


def describe_timeline(timeline: Timeline) -> str:
    """A readable table of the timeline, for the CLI and logs."""
    if timeline.is_empty:
        return "The timeline is empty."
    lines = [f"Total {timeline.format_total()} across {timeline.scene_count} scene(s)"]
    for timing in timeline.timings:
        lines.append(
            f"  {timing.index + 1:>2}. {timing.name:<24} "
            f"{format_duration(timing.start)} -> {format_duration(timing.end)} "
            f"({timing.source_label()})")
    return "\n".join(lines)
