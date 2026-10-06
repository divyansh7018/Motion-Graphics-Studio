"""Stage D - animation, transitions and narration-driven timing.

These are mostly pure-function tests: the whole motion system is evaluated as a
function of time so a preview can scrub freely.  The assertions are about the
*shapes* of the curves and the *rules* of the timeline, not about pixels.
"""

from __future__ import annotations

import pytest

from app.project.model import NarrationSpec, SceneSpec, TransitionSpec
from app.scene.animation import (
    ANIMATION_PRESETS,
    ANIMATION_PROPERTIES,
    EASINGS,
    IDENTITY,
    AnimationSpec,
    AnimationTrack,
    easing,
    evaluate,
    parse_animation,
    preset_names,
)
from app.scene.timing import (
    DURATION_SOURCES,
    MIN_SCENE_DURATION,
    TimingOptions,
    build_timeline,
    format_duration,
)
from app.scene.transitions import (
    ENGINE_TRANSITIONS,
    blend,
    crossfade,
    is_known_transition,
    transition_duration,
)


# --------------------------------------------------------------------------
# Easing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(EASINGS))
def test_every_easing_starts_at_0_and_ends_at_1(name):
    curve = EASINGS[name]
    assert curve(0.0) == pytest.approx(0.0, abs=1e-6)
    assert curve(1.0) == pytest.approx(1.0, abs=1e-6)


def test_easing_lookup_falls_back_to_linear():
    assert easing("not-a-curve") is EASINGS["linear"]
    assert easing("") is EASINGS["linear"]
    assert easing(None) is EASINGS["linear"]


def test_ease_out_is_front_loaded():
    assert EASINGS["ease_out"](0.5) > 0.5
    assert EASINGS["ease_in"](0.5) < 0.5


def test_linear_easing_is_identity():
    assert EASINGS["linear"](0.37) == pytest.approx(0.37)


def test_back_easing_overshoots():
    # "pop" reads as a pop because it passes 1.0 on the way in.
    assert max(EASINGS["back"](i / 200) for i in range(201)) > 1.0


# --------------------------------------------------------------------------
# Tracks
# --------------------------------------------------------------------------

def test_track_moves_between_its_values():
    track = AnimationTrack(name="opacity", start=0.0, duration=1.0, value_from=0.0, value_to=1.0,
                           easing="linear")
    assert track.value_at(0.0) == pytest.approx(0.0)
    assert track.value_at(0.5) == pytest.approx(0.5)
    assert track.value_at(1.0) == pytest.approx(1.0)


def test_track_holds_its_end_value_after_finishing():
    track = AnimationTrack(name="opacity", start=0.0, duration=0.5, value_from=0.0, value_to=1.0,
                           hold=True)
    assert track.value_at(10.0) == 1.0


def test_track_with_zero_duration_jumps():
    track = AnimationTrack(name="opacity", start=0.0, duration=0.0, value_from=0.0, value_to=1.0)
    assert track.value_at(0.0) == 1.0  # finished immediately


def test_track_before_its_start_returns_the_from_value():
    track = AnimationTrack(name="opacity", start=2.0, duration=1.0, value_from=0.0, value_to=1.0)
    assert track.value_at(0.0) == 0.0
    assert not track.is_active(0.0)
    assert track.is_active(2.5)


def test_track_rejects_an_unknown_property():
    assert AnimationTrack.from_dict({"property": "colour", "from": 0, "to": 1}) is None
    assert AnimationTrack.from_dict({"property": "opacity", "from": 0, "to": 1}).name == "opacity"
    assert AnimationTrack.from_dict(None) is None


def test_track_round_trip():
    track = AnimationTrack(name="y", start=0.2, duration=0.6, value_from=0.1, value_to=0.0,
                           easing="ease_out_cubic")
    restored = AnimationTrack.from_dict(track.to_dict())
    assert restored.name == track.name
    assert (restored.start, restored.duration) == pytest.approx((0.2, 0.6))


# --------------------------------------------------------------------------
# Parsing and evaluation
# --------------------------------------------------------------------------

def test_parse_animation_accepts_an_explicit_track_list():
    spec = parse_animation({"enter": [{"property": "opacity", "from": 0, "to": 1,
                                       "duration": 0.5}]})
    assert len(spec.enter) == 1
    assert spec.enter[0].name == "opacity"


def test_empty_animation_gives_the_kind_default():
    assert parse_animation({}, kind="number").preset == "pop"
    assert parse_animation({}, kind="chart").preset == "grow"
    assert parse_animation({}, kind="text").preset == "fade up"


def test_explicit_none_disables_animation():
    assert parse_animation({"preset": "none"}, kind="text").is_empty


def test_unknown_preset_is_ignored_not_crashed():
    assert parse_animation({"preset": "does-not-exist"}, kind="text").is_empty


def test_delay_pushes_tracks_later():
    spec = parse_animation({"preset": "fade", "duration": 0.5, "delay": 0.3})
    assert min(track.start for track in spec.enter) == pytest.approx(0.3)


def test_evaluate_is_identity_when_there_is_no_animation():
    assert evaluate(None, 0.5) is IDENTITY
    assert evaluate(AnimationSpec(), 0.5) is IDENTITY


def test_evaluate_returns_identity_at_animation_end():
    spec = parse_animation({"preset": "fade up", "duration": 0.5})
    assert evaluate(spec, 10.0, scene_duration=20.0) is IDENTITY or \
        evaluate(spec, 10.0, scene_duration=20.0).opacity == pytest.approx(1.0)


def test_evaluate_is_invisible_before_a_fade_in_starts():
    spec = parse_animation({"preset": "fade", "duration": 0.5})
    assert evaluate(spec, 0.0).opacity == pytest.approx(0.0, abs=1e-6)
    assert evaluate(spec, 0.0).is_invisible


def test_exit_animation_runs_at_the_end_of_the_scene():
    spec = parse_animation({"enter": [{"property": "opacity", "from": 0, "to": 1,
                                       "duration": 0.5, "start": 0}]})
    spec.exit = [AnimationTrack(name="opacity", start=0.0, duration=0.5, value_from=1.0, value_to=0.0,
                                easing="linear")]
    # Scene lasts 5s; the exit (0.5s) plays from 4.5s to 5s.
    assert evaluate(spec, 0.0, scene_duration=5.0).opacity == pytest.approx(0.0)  # before enter
    assert evaluate(spec, 2.0, scene_duration=5.0).opacity == pytest.approx(1.0)  # mid scene
    assert evaluate(spec, 4.75, scene_duration=5.0).opacity == pytest.approx(0.5, abs=0.1)


def test_evaluate_clamps_opacity_and_scale_to_sane_ranges():
    spec = AnimationSpec(enter=[AnimationTrack(name="opacity", value_from=-5, value_to=5),
                                AnimationTrack(name="scale", value_from=-5, value_to=50)])
    transform = evaluate(spec, 10.0)
    assert 0.0 <= transform.opacity <= 1.0
    assert 0.0 <= transform.scale <= 8.0


def test_rotation_wraps_into_degrees():
    spec = AnimationSpec(enter=[AnimationTrack(name="rotation", value_from=0, value_to=400)])
    transform = evaluate(spec, 10.0)
    assert 0.0 <= transform.rotation < 360.0


def test_preset_names_lists_none_first():
    names = preset_names()
    assert names[0] == "none"
    assert set(ANIMATION_PRESETS) == set(names)


def test_every_preset_builds_tracks_that_end_at_identity():
    from app.scene.animation import VALUE_PRESETS

    # "fade out" is an exit-style preset: it settles *invisible*, so it is the
    # one preset that deliberately does not end at full opacity.
    exit_style = {"fade out"}
    for name in preset_names():
        if name == "none":
            continue
        # Value presets (count up / progress fill) drive a number, not a
        # transform, so they build no transform track by design.
        if name in VALUE_PRESETS:
            continue
        builder = ANIMATION_PRESETS[name]["build"]
        tracks = builder(0.6)
        assert tracks, name
        spec = AnimationSpec(enter=tracks)
        expected = 0.0 if name in exit_style else 1.0
        assert evaluate(spec, 10.0).opacity == pytest.approx(expected)


def test_animation_properties_are_all_known():
    assert set(ANIMATION_PROPERTIES) == {"opacity", "x", "y", "scale", "rotation"}


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def test_known_transitions_include_the_model_set():
    from app.project.model import TRANSITION_TYPES

    assert set(TRANSITION_TYPES) <= set(ENGINE_TRANSITIONS)


def test_none_and_cut_resolve_to_zero_duration():
    assert transition_duration("none", 1.0) == 0.0
    assert transition_duration("cut", 1.0) == 0.0


def test_cross_fade_duration_is_clamped():
    assert transition_duration("fade", 0.0) == 0.0
    assert transition_duration("fade", 10.0) <= 2.0
    assert transition_duration("fade", 0.8) == pytest.approx(0.8)


def test_is_known_transition():
    assert is_known_transition("fade")
    assert not is_known_transition("teleport")


def _frame(colour):
    from PIL import Image

    return Image.new("RGBA", (64, 64), colour)


@pytest.mark.parametrize("kind", ENGINE_TRANSITIONS)
def test_blend_returns_the_source_frames_at_the_extremes(kind):
    a = _frame((255, 0, 0, 255))
    b = _frame((0, 0, 255, 255))
    assert blend(a, b, 0.0, kind).getpixel((32, 32)) == (255, 0, 0, 255)
    assert blend(a, b, 1.0, kind).getpixel((32, 32)) == (0, 0, 255, 255)


@pytest.mark.parametrize("kind", ENGINE_TRANSITIONS)
def test_blend_handles_mismatched_sizes_without_crashing(kind):
    a = _frame((10, 10, 10, 255))
    b = _frame((200, 200, 200, 255)).resize((32, 32))
    out = blend(a, b, 0.5, kind)
    assert out.size == a.size


def test_crossfade_is_a_straight_blend():
    a = _frame((0, 0, 0, 255))
    b = _frame((255, 255, 255, 255))
    half = crossfade(a, b, 0.5)
    assert half.getpixel((32, 32))[0] == pytest.approx(127, abs=2)


def test_dip_passes_through_black():
    a = _frame((255, 255, 255, 255))
    b = _frame((255, 255, 255, 255))
    middle = blend(a, b, 0.5, "dip")
    assert middle.getpixel((32, 32))[0] < 20  # darkest at the midpoint


def test_slide_moves_the_incoming_frame_across():
    a = _frame((255, 0, 0, 255))
    b = _frame((0, 255, 0, 255))
    early = blend(a, b, 0.25, "slide")
    assert early.getpixel((4, 32)) == (255, 0, 0, 255)     # left still red
    assert early.getpixel((60, 32)) == (0, 255, 0, 255)    # right already green


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

def _scene(narration=0.0, manual=0.0, transition_out=0.0, transition_in=0.0, scene_id=""):
    return SceneSpec(
        id=scene_id,
        duration=manual,
        narration=NarrationSpec(duration=narration),
        transition_out=TransitionSpec(type="fade", duration=transition_out),
        transition_in=TransitionSpec(type="fade", duration=transition_in),
    )


def test_narration_is_authoritative_over_manual_duration():
    timeline = build_timeline([_scene(narration=12.5, manual=4.0)], TimingOptions(tail=0.0, head=0.0))
    assert timeline.timings[0].duration == pytest.approx(12.5)
    assert timeline.timings[0].source == "narration"


def test_head_and_tail_padding_are_added_to_narration():
    timeline = build_timeline([_scene(narration=10.0)], TimingOptions(head=0.2, tail=0.3))
    assert timeline.timings[0].duration == pytest.approx(10.5)


def test_manual_duration_when_there_is_no_narration():
    timeline = build_timeline([_scene(manual=5.0)], TimingOptions(head=0.0, tail=0.0))
    assert timeline.timings[0].duration == pytest.approx(5.0)
    assert timeline.timings[0].source == "manual"


def test_default_duration_for_an_empty_scene():
    timeline = build_timeline([_scene()], TimingOptions(head=0.0, tail=0.0, default_duration=4.0))
    assert timeline.timings[0].duration == pytest.approx(4.0)
    assert timeline.timings[0].source == "default"


def test_starts_are_cumulative():
    timeline = build_timeline([_scene(manual=2.0), _scene(manual=3.0), _scene(manual=5.0)],
                              TimingOptions(head=0.0, tail=0.0))
    starts = [t.start for t in timeline.timings]
    assert starts == [0.0, 2.0, 5.0]
    assert timeline.total_duration == pytest.approx(10.0)


def test_a_scene_raised_to_the_minimum_is_flagged():
    timeline = build_timeline([_scene(manual=0.0001)], TimingOptions(head=0.0, tail=0.0))
    assert timeline.timings[0].raised
    assert timeline.timings[0].duration == pytest.approx(MIN_SCENE_DURATION)
    assert timeline.scenes_raised_to_minimum()


def test_transition_overlap_never_adds_time():
    overlapping = build_timeline(
        [_scene(manual=4.0, scene_id="a", transition_out=1.0),
         _scene(manual=4.0, scene_id="b", transition_in=1.0)],
        TimingOptions(head=0.0, tail=0.0, overlap_transitions=True))
    assert overlapping.total_duration == pytest.approx(7.0)  # 8 - 1 overlap

    additive = build_timeline(
        [_scene(manual=4.0, scene_id="a", transition_out=1.0),
         _scene(manual=4.0, scene_id="b", transition_in=1.0)],
        TimingOptions(head=0.0, tail=0.0, overlap_transitions=False))
    assert additive.total_duration == pytest.approx(8.0)


def test_overlap_is_capped_by_half_of_each_scene():
    timeline = build_timeline(
        [_scene(manual=1.0, transition_out=5.0), _scene(manual=1.0, transition_in=5.0)],
        TimingOptions(head=0.0, tail=0.0, overlap_transitions=True))
    # Overlap can never exceed half a scene.
    assert timeline.total_duration >= 1.5


def test_at_returns_the_scene_and_local_time():
    timeline = build_timeline([_scene(manual=2.0), _scene(manual=3.0)],
                              TimingOptions(head=0.0, tail=0.0))
    timing, local = timeline.at(2.5)
    assert timing.index == 1
    assert local == pytest.approx(0.5)


def test_at_past_the_end_stays_on_the_last_scene():
    timeline = build_timeline([_scene(manual=2.0)], TimingOptions(head=0.0, tail=0.0))
    timing, local = timeline.at(99.0)
    assert timing.index == 0
    assert local == pytest.approx(2.0)


def test_no_maximum_video_duration_is_imposed():
    """The brief forbids any 30/45/60 second cap - prove it with a long one."""
    scenes = [_scene(narration=20.0) for _ in range(540)]  # three hours
    timeline = build_timeline(scenes, TimingOptions(head=0.0, tail=0.0))
    assert timeline.total_duration == pytest.approx(540 * 20.0)
    assert timeline.scene_count == 540


def test_total_frames_never_stored_only_derived():
    timeline = build_timeline([_scene(manual=2.5)], TimingOptions(head=0.0, tail=0.0))
    assert timeline.total_frames(30) == 75
    assert timeline.total_frames(60) == 150


def test_format_duration_scales_to_hours():
    assert format_duration(65.4) == "1:05.4"
    assert format_duration(3 * 3600 + 2 * 60 + 5.0) == "3:02:05.0"
    assert format_duration(-5) == "0:00.0"


def test_duration_sources_are_reported_not_guessed():
    assert set(DURATION_SOURCES) == {"narration", "manual", "default"}
    timeline = build_timeline([_scene(narration=1.0), _scene(manual=1.0), _scene()],
                              TimingOptions(head=0.0, tail=0.0))
    assert [t.source for t in timeline.timings] == ["narration", "manual", "default"]


def test_empty_project_is_an_empty_timeline_not_an_error():
    timeline = build_timeline([], TimingOptions())
    assert timeline.is_empty
    assert timeline.total_duration == 0.0
    assert timeline.at(0.0) is None
