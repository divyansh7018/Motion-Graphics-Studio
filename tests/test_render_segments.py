"""Timeline -> render segment planning (Stage E, directive sections 32-37).

Pure planning: no files, no subprocess.  The point of these tests is that the
segments always tile the timeline exactly, whatever the scene lengths and
transitions are, because a gap or an overlap here is a video that is the wrong
length.
"""

from __future__ import annotations

import pytest

from app.project.model import SceneSpec, build_project
from app.render.segments import frame_times, plan_segments, total_frames, validate_plan
from app.scene.timing import build_timeline


def _project(lengths, transitions=None):
    """A project whose scenes have the given narration lengths."""
    project = build_project("Segments")
    scenes = []
    for index, length in enumerate(lengths, start=1):
        scene = project.add_scene(SceneSpec(id=f"s{index}", name=f"Scene {index}", type="text"))
        if length:
            scene.narration.duration = float(length)
            scene.narration.file = f"audio/s{index}.wav"
        else:
            scene.duration = 2.0
        scenes.append(scene)
    if transitions:
        for position, (kind, duration) in enumerate(transitions):
            scenes[position].transition_out.type = kind
            scenes[position].transition_out.duration = duration
            scenes[position + 1].transition_in.type = kind
            scenes[position + 1].transition_in.duration = duration
    return project


# -- frame maths -----------------------------------------------------------

def test_total_frames_rounds_once():
    assert total_frames(1.0, 25) == 25
    assert total_frames(10.0, 30) == 300
    # 0.1s at 24fps is 2.4 frames: rounding happens once, here, not per scene.
    assert total_frames(0.1, 24) == 2


def test_total_frames_never_negative():
    assert total_frames(-5.0, 30) == 0


def test_total_frames_clamps_a_nonsense_frame_rate():
    """An fps of 0 must not become a division by zero half a render later."""
    assert total_frames(10.0, 0) == 10       # fps clamps to 1
    assert total_frames(10.0, -30) == 10


def test_frame_times_are_evenly_spaced():
    times = frame_times(5, 25)
    assert times == [0.0, 0.04, 0.08, 0.12, 0.16]
    assert frame_times(0, 30) == []


# -- plain scenes ----------------------------------------------------------

def test_segments_cover_the_timeline_exactly():
    project = _project([3.0, 2.0, 4.0])
    timeline = build_timeline(project.scenes)
    plan = plan_segments(timeline, fps=25)

    assert plan.issues == []
    assert plan.frames_covered() == plan.total_frames
    assert plan.total_frames == total_frames(timeline.total_duration, 25)
    # No gaps and no frame drawn twice.
    for previous, current in zip(plan.segments, plan.segments[1:]):
        assert current.frame_start == previous.frame_start + previous.frame_count
        assert current.start == pytest.approx(previous.end)


def test_empty_timeline_is_reported_not_guessed():
    project = build_project("Empty")
    plan = plan_segments(build_timeline(project.scenes), fps=30)
    assert plan.segments == []
    assert [issue.code for issue in plan.issues] == ["TIMELINE_EMPTY"]
    assert plan.total_frames == 0


def test_disabled_scenes_are_not_rendered():
    project = _project([3.0, 2.0, 4.0])
    project.scenes[1].enabled = False
    timeline = build_timeline(project.scenes)
    plan = plan_segments(timeline, fps=25)
    rendered = {segment.scene_id for segment in plan.segments}
    assert "s2" not in rendered
    assert plan.total_frames == total_frames(timeline.total_duration, 25)


# -- transitions -----------------------------------------------------------

def test_transition_becomes_its_own_segment():
    project = _project([4.0, 3.0], transitions=[("fade", 1.0)])
    plan = plan_segments(build_timeline(project.scenes), fps=25)

    kinds = [segment.kind for segment in plan.segments]
    assert kinds == ["scene", "transition", "scene"]
    transition = plan.segments[1]
    assert transition.transition == "fade"
    assert transition.duration == pytest.approx(1.0, abs=0.05)
    assert transition.scene_id == "s1"
    assert transition.next_scene_id == "s2"


def test_transition_frames_still_total_the_timeline():
    project = _project([4.0, 3.0, 2.5], transitions=[("fade", 1.0), ("wipe", 0.6)])
    timeline = build_timeline(project.scenes)
    plan = plan_segments(timeline, fps=30)
    assert plan.issues == []
    assert plan.frames_covered() == plan.total_frames
    assert sum(1 for segment in plan.segments if segment.is_transition) == 2


def test_incoming_scene_clock_starts_at_the_overlap():
    """The second scene's local clock must account for the overlap.

    If it did not, the incoming scene would replay its first second after the
    transition instead of continuing where the overlap began.
    """
    project = _project([4.0, 3.0], transitions=[("fade", 1.0)])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    incoming = plan.segments[2]
    assert incoming.local_start > 0.0
    # At the first frame of the plain part, the scene's own clock reads local_start.
    first_frame_time = incoming.frame_start / 25
    assert first_frame_time - incoming.start + incoming.local_start == pytest.approx(
        incoming.local_start, abs=0.05)


def test_cut_transition_keeps_its_hard_cut_kind():
    """A cut still owns the overlap frames - and stays a hard cut.

    The timing model gives a cut a duration like any other transition, so the
    planner must hand those frames over as a transition segment.  What makes it
    a cut rather than a fade is the kind the segment carries, which the scene
    engine's blend() turns into a hard switch at the midpoint.
    """
    project = _project([3.0, 2.0], transitions=[("cut", 0.5)])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    transition = next(segment for segment in plan.segments if segment.is_transition)
    assert transition.transition == "cut"
    assert plan.issues == []
    assert plan.frames_covered() == plan.total_frames


def test_no_transition_means_no_transition_segment():
    project = _project([3.0, 2.0])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    assert all(segment.kind == "scene" for segment in plan.segments)


# -- validation ------------------------------------------------------------

def test_validate_plan_accepts_a_good_plan():
    project = _project([3.0, 2.0])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    assert validate_plan(plan) == []


def test_validate_plan_rejects_a_frame_count_mismatch():
    project = _project([3.0, 2.0])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    plan.segments[0].frame_count += 5      # deliberately corrupt the plan
    codes = [issue.code for issue in validate_plan(plan)]
    assert "PLAN_FRAME_MISMATCH" in codes
    assert "PLAN_GAP" in codes


def test_plan_describe_is_human_readable():
    project = _project([3.0])
    plan = plan_segments(build_timeline(project.scenes), fps=25)
    text = plan.describe()
    assert "segment" in text and "frames" in text and "fps" in text
    assert plan.to_dict()["segments"][0]["scene_id"] == "s1"
