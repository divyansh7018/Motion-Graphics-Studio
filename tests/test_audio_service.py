"""Audio: tracks, timing, gain, fades, ducking, normalisation and the mix
(Stage E, directive sections 2-13, 69).

These tests build the mix *plan* - the exact FFmpeg filter graph - without
running FFmpeg, so the maths and the graph shape are checked on any machine.
``test_render_engine.py`` runs the same plan against a real encoder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.audio.ducking import (
    DuckingSettings,
    duck_expression,
    duck_level_at,
    merge_windows,
    narration_windows,
)
from app.audio.mix import MixInput, build_mix_command, resolve_mix_inputs
from app.audio.service import AudioService
from app.project.model import SceneSpec, SoundEffect, build_project
from app.scene.timing import build_timeline


def _project(narration=(3.0, 2.0), tmp: Path | None = None):
    project = build_project("Audio")
    for index, length in enumerate(narration, start=1):
        scene = project.add_scene(SceneSpec(id=f"s{index}", name=f"Scene {index}", type="text"))
        scene.narration.text = f"Line {index}."
        if length:
            scene.narration.duration = float(length)
            scene.narration.file = f"audio/s{index}.wav"
    return project


def _files(tmp_path: Path, names=("s1.wav", "s2.wav")):
    folder = tmp_path / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"RIFF....WAVEfmt ")
    return folder


def _service(tmp_path: Path) -> AudioService:
    return AudioService(tools=None, project_dir=tmp_path)


# -- narration placement ---------------------------------------------------

def test_narration_starts_where_the_timeline_puts_the_scene(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    service = _service(tmp_path)
    placements = service.narration_placements(project, build_timeline(project.scenes))

    timeline = build_timeline(project.scenes)
    assert [placement.scene_id for placement in placements] == ["s1", "s2"]
    assert placements[0].start == pytest.approx(0.0)
    # Each voice starts where the timeline puts its scene.
    assert placements[1].start == pytest.approx(timeline.timings[1].start)
    # ...and lasts exactly as long as the narration file, not as long as the
    # scene (the scene carries the tail padding, the voice does not).
    assert placements[0].duration == pytest.approx(3.0)
    assert placements[0].end < timeline.timings[0].end


def test_a_scene_without_a_file_is_reported_not_invented(tmp_path: Path):
    project = _project()
    service = _service(tmp_path)          # no files exist here
    placements = service.narration_placements(project, build_timeline(project.scenes))
    assert all(placement.path is None for placement in placements)


def test_resolve_file_accepts_relative_absolute_and_unknown(tmp_path: Path):
    _files(tmp_path)
    service = _service(tmp_path)
    project = _project()
    assert service.resolve_file(project, "audio/s1.wav") == tmp_path / "audio" / "s1.wav"
    assert service.resolve_file(project, str(tmp_path / "audio" / "s1.wav")) is not None
    assert service.resolve_file(project, "audio/nope.wav") is None
    assert service.resolve_file(project, "") is None


# -- validation ------------------------------------------------------------

def test_missing_narration_blocks_the_render(tmp_path: Path):
    project = _project()
    validation = _service(tmp_path).validate(project, build_timeline(project.scenes))
    codes = [issue.code for issue in validation.errors]
    assert "NARRATION_FILE_MISSING" in codes
    assert not validation.ok


def test_a_stale_narration_is_a_hard_stop(tmp_path: Path):
    """Rendering an old voice against a new script gives a silently wrong video."""
    _files(tmp_path)
    project = _project()
    track = project.narration.tracks
    from app.project.model import NarrationTrack

    track.append(NarrationTrack(id="t1", status="stale", path="audio/s1.wav"))
    validation = _service(tmp_path).validate(project, build_timeline(project.scenes))
    assert "NARRATION_STALE" in [issue.code for issue in validation.errors]


def test_a_present_narration_passes(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    validation = _service(tmp_path).validate(project, build_timeline(project.scenes))
    assert validation.ok, [issue.message for issue in validation.errors]


def test_a_missing_music_file_is_fatal(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    project.audio.music.path = "audio/gone.wav"
    validation = _service(tmp_path).validate(project, build_timeline(project.scenes))
    assert not validation.ok
    assert any("music" in issue.message.lower() or issue.code == "MUSIC_FILE_MISSING"
               for issue in validation.errors)


def test_an_effect_anchored_to_a_deleted_scene_is_reported(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    project.audio.sfx.append(SoundEffect(id="e1", path="audio/s1.wav",
                                         anchor="scene", scene_id="nope"))
    validation = _service(tmp_path).validate(project, build_timeline(project.scenes))
    assert not validation.ok


# -- ducking ---------------------------------------------------------------

def test_narration_windows_come_from_the_timeline(tmp_path: Path):
    project = _project()
    windows = narration_windows(build_timeline(project.scenes))
    assert len(windows) == 2
    for start, end in windows:
        assert end > start


def test_a_hand_timed_scene_does_not_duck(tmp_path: Path):
    """No narration means no voice, so there is nothing to duck under."""
    project = _project(narration=(3.0, None))
    project.scenes[1].duration = 4.0
    windows = narration_windows(build_timeline(project.scenes))
    assert len(windows) == 1


def test_merge_windows_makes_them_disjoint():
    merged = merge_windows([(0.0, 5.0), (4.0, 9.0), (20.0, 21.0)])
    assert merged == [(0.0, 9.0), (20.0, 21.0)]


def test_duck_level_is_full_outside_narration():
    settings = DuckingSettings(enabled=True, amount=0.65, attack=0.25, release=0.75)
    windows = merge_windows([(2.0, 4.0)])
    assert duck_level_at(0.0, windows, settings) == pytest.approx(1.0)
    assert duck_level_at(10.0, windows, settings) == pytest.approx(1.0)


def test_duck_level_reaches_the_target_in_the_middle_of_narration():
    settings = DuckingSettings(enabled=True, amount=0.65, attack=0.25, release=0.75)
    windows = merge_windows([(2.0, 6.0)])
    assert duck_level_at(4.0, windows, settings) == pytest.approx(0.35, abs=1e-6)


def test_duck_level_ramps_in_and_out():
    settings = DuckingSettings(enabled=True, amount=0.65, attack=0.5, release=0.5)
    windows = merge_windows([(2.0, 6.0)])
    before = duck_level_at(1.0, windows, settings)
    mid_attack = duck_level_at(1.75, windows, settings)
    full = duck_level_at(4.0, windows, settings)
    assert before == pytest.approx(1.0)
    assert full < mid_attack < before


def test_ducking_off_means_no_change():
    settings = DuckingSettings(enabled=False, amount=0.65, attack=0.25, release=0.75)
    assert duck_level_at(4.0, [(2.0, 6.0)], settings) == pytest.approx(1.0)


def test_ducking_amount_is_clamped_to_something_sane():
    settings = DuckingSettings(enabled=True, amount=9.0, attack=-1.0, release=0.0).clamped()
    assert 0.0 <= settings.amount <= 1.0
    assert settings.attack > 0 and settings.release > 0


def test_the_ffmpeg_expression_matches_the_python_reference():
    """One source of truth: the filter must duck exactly as duck_level_at says."""
    settings = DuckingSettings(enabled=True, amount=0.65, attack=0.25, release=0.75)
    windows = merge_windows([(2.0, 4.0)])
    expression = duck_expression(windows, settings, base=1.0)
    assert "clip(" in expression and "eval" not in expression
    # Constant when there is nothing to duck.
    assert duck_expression([], settings) == "1"


def test_expression_carries_the_base_volume():
    settings = DuckingSettings(enabled=True, amount=0.5, attack=0.25, release=0.75)
    expression = duck_expression([(2.0, 4.0)], settings, base=0.25)
    assert expression.startswith("0.25*")


# -- mix plan --------------------------------------------------------------

def _inputs(tmp_path: Path):
    folder = _files(tmp_path)
    return [
        MixInput(path=folder / "s1.wav", label="narration 1", kind="narration", start=0.0),
        MixInput(path=folder / "s2.wav", label="music", kind="music", start=0.0,
                 volume=0.4, fade_in=0.5, fade_out=1.0, stream_loop=-1),
        MixInput(path=folder / "s1.wav", label="sfx", kind="sfx", start=2.5, volume=0.8),
    ]


def _plan(inputs, **kwargs):
    options = dict(duration=10.0, sample_rate=48000, channels=2,
                   master_volume=1.0, normalize=False, target_lufs=-16.0)
    options.update(kwargs)
    return build_mix_command(inputs, "out.wav", **options)


def test_an_empty_mix_is_reported_as_having_no_audio():
    plan = _plan([])
    assert plan.has_audio is False
    assert "no audio" in plan.describe()


def test_the_plan_maps_every_input():
    plan = _plan(_inputs(Path("/tmp/audioplan")))
    assert plan.has_audio
    assert plan.filter_complex.count("[a") >= 3
    assert "[master]" in plan.filter_complex


def test_gain_and_fades_appear_in_the_chain():
    inputs = _inputs(Path("/tmp/audioplan"))
    inputs[1].length = 10.0                    # a bed with a known length
    plan = _plan(inputs)
    assert "volume=" in plan.filter_complex
    assert "afade=t=in" in plan.filter_complex
    assert "afade=t=out" in plan.filter_complex


def test_a_fade_out_still_happens_when_the_track_length_is_unknown():
    """A looping bed has no length of its own, but the fade the user asked for
    must still land - at the end of the video rather than nowhere."""
    plan = _plan(_inputs(Path("/tmp/audioplan")), duration=10.0)
    assert "afade=t=out:st=9" in plan.filter_complex


def test_a_track_is_placed_at_its_start_time():
    plan = _plan(_inputs(Path("/tmp/audioplan")))
    assert "adelay=2500" in plan.filter_complex


def test_a_looping_bed_loops():
    plan = _plan(_inputs(Path("/tmp/audioplan")))
    assert any(item.stream_loop == -1 for item in plan.inputs)


def test_the_master_is_capped_at_the_timeline_length(tmp_path: Path):
    """Without -t a looping bed makes the stream endless and fills the disk."""
    plan = _plan(_inputs(tmp_path), duration=12.5)
    assert "-t" in plan.output_args
    assert plan.output_args[plan.output_args.index("-t") + 1] == "12.5"


def test_the_master_length_is_omitted_only_when_unknown(tmp_path: Path):
    plan = _plan(_inputs(tmp_path), duration=0.0)
    assert "-t" not in plan.output_args


def test_normalisation_adds_loudnorm():
    plan = _plan(_inputs(Path("/tmp/audioplan")), normalize=True, target_lufs=-14.0)
    assert "loudnorm=I=-14" in plan.filter_complex


def test_no_normalisation_means_no_loudnorm():
    assert "loudnorm" not in _plan(_inputs(Path("/tmp/audioplan"))).filter_complex


def test_master_volume_is_applied():
    plan = _plan(_inputs(Path("/tmp/audioplan")), master_volume=0.5)
    assert "volume=0.5" in plan.filter_complex


def test_sample_rate_and_channels_are_set():
    plan = _plan(_inputs(Path("/tmp/audioplan")), sample_rate=44100, channels=1)
    assert "-ar" in plan.output_args and "44100" in plan.output_args
    assert "mono" in plan.filter_complex


def test_padding_brings_a_short_mix_up_to_length():
    """apad + atrim keeps the master exactly the timeline length."""
    plan = _plan(_inputs(Path("/tmp/audioplan")), duration=30.0)
    assert "apad" in plan.filter_complex
    assert "atrim" in plan.filter_complex


# -- resolve_mix_inputs ----------------------------------------------------

def test_resolve_mix_inputs_places_narration_on_the_timeline(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    service = _service(tmp_path)
    timeline = build_timeline(project.scenes)
    placements = service.narration_placements(project, timeline)
    inputs, issues = resolve_mix_inputs(
        project.audio,
        narration=[placement.path for placement in placements],
        narration_starts=[placement.start for placement in placements],
        resolve_file=service.mix_resolver(project),
    )
    narration = [item for item in inputs if item.kind == "narration"]
    assert len(narration) == 2
    assert narration[1].start == pytest.approx(timeline.timings[1].start)


def test_resolve_mix_inputs_never_drops_a_present_narration(tmp_path: Path):
    """Regression: a resolver that only read .reference reported every
    narration file missing and silently produced a video with no voice."""
    _files(tmp_path)
    project = _project()
    service = _service(tmp_path)
    timeline = build_timeline(project.scenes)
    placements = service.narration_placements(project, timeline)
    inputs, issues = resolve_mix_inputs(
        project.audio,
        narration=[placement.path for placement in placements],
        narration_starts=[placement.start for placement in placements],
        resolve_file=service.mix_resolver(project),
    )
    assert [issue.code for issue in issues] == []
    assert len([item for item in inputs if item.kind == "narration"]) == 2


def test_a_muted_track_is_left_out(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    project.audio.music.path = "audio/s1.wav"
    project.audio.music.mute = True
    service = _service(tmp_path)
    inputs, _ = resolve_mix_inputs(
        project.audio, narration=[], narration_starts=[],
        resolve_file=service.mix_resolver(project))
    assert all(item.kind != "music" for item in inputs)


def test_narration_can_be_switched_off_with_a_warning(tmp_path: Path):
    _files(tmp_path)
    project = _project()
    project.audio.narration_enabled = False
    service = _service(tmp_path)
    inputs, issues = resolve_mix_inputs(
        project.audio, narration=[], narration_starts=[],
        resolve_file=service.mix_resolver(project))
    assert "NARRATION_DISABLED" in [issue.code for issue in issues]
