"""Tests for the Stage E service objects (directive section 68).

``TimelineService`` and ``SubtitleService`` are the classes the GUI, the CLI and
the render engine all go through.  These tests cover them directly, so a
behaviour change in either shows up here rather than only in a screenshot.

The point of having them is that every entry point judges a project the same
way, so several tests assert that the engine agrees with the service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.project.model import (
    Project,
    SceneSpec,
    ScriptSection,
    SubtitleCue,
    build_project,
)
from app.scene.service import ERROR, TimelineService
from app.scene.timing import TimingOptions, build_timeline
from app.subtitles.service import SubtitleService


def _wav(path: Path, seconds: float, frequency: int = 220) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={frequency}:duration={seconds}",
         "-ar", "22050", "-ac", "1", str(path)], check=True)
    return path


def _project(tmp_path: Path, *, lengths=(2.0, 1.6, 1.2), transitions=True,
             subtitles=False) -> Project:
    project = build_project("Service Test")
    project.format.width, project.format.height, project.format.fps = 512, 288, 25
    for index, length in enumerate(lengths):
        scene = project.add_scene(SceneSpec(id=f"s{index + 1}",
                                            name=f"Scene {index + 1}", type="text"))
        scene.narration.text = f"Narration for scene {index + 1}."
        scene.narration.file = f"audio/s{index + 1}.wav"
        scene.narration.duration = length
        _wav(tmp_path / "audio" / f"s{index + 1}.wav", length, 220 + index * 60)
        project.script.sections.append(ScriptSection(
            id=f"sec{index + 1}", text=scene.narration.text, order=index + 1))
    if transitions and len(project.scenes) > 1:
        for first, second in zip(project.scenes, project.scenes[1:]):
            first.transition_out.type = "fade"
            first.transition_out.duration = 0.4
            second.transition_in.type = "fade"
            second.transition_in.duration = 0.4
    project.subtitles.enabled = subtitles
    return project


@pytest.fixture()
def timeline_service(tmp_path: Path) -> TimelineService:
    return TimelineService(project_dir=tmp_path)


@pytest.fixture()
def subtitle_service() -> SubtitleService:
    return SubtitleService()


# --------------------------------------------------------------------------
# TimelineService: building
# --------------------------------------------------------------------------

def test_the_timeline_comes_from_the_real_scenes(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    timeline = timeline_service.build(project)

    assert timeline.scene_count == 3
    assert timeline.total_duration == pytest.approx(5.5, abs=0.01)
    assert timeline.timings[0].source == "narration"


def test_a_crossfade_is_not_reported_as_out_of_order(timeline_service, tmp_path) -> None:
    """A scene starting before the previous one ends is how a crossfade works.

    This was a real bug: the first version of the check treated the normal
    0.4s transition overlap as a fatal ordering error, which would have blocked
    every project with a dissolve in it.
    """
    project = _project(tmp_path)
    report = timeline_service.check(project)

    assert [issue.code for issue in report.errors] == []
    assert report.ok is True
    # The overlap really is there, so the test is proving something.
    assert report.timeline.timings[1].start < report.timeline.timings[0].end


def test_the_same_service_builds_the_same_timeline_as_the_renderer(tmp_path) -> None:
    project = _project(tmp_path)
    from_service = TimelineService(project_dir=tmp_path).build(project)
    from_module = build_timeline(project.scenes)

    assert from_service.total_duration == pytest.approx(from_module.total_duration)
    assert [t.scene_id for t in from_service.timings] == \
           [t.scene_id for t in from_module.timings]


def test_options_change_the_timeline(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    default = timeline_service.build(project)
    padded = timeline_service.build(project, TimingOptions(head=1.0, tail=2.0))

    assert padded.total_duration > default.total_duration


# --------------------------------------------------------------------------
# TimelineService: validating
# --------------------------------------------------------------------------

def test_an_empty_project_is_a_fatal_error(timeline_service) -> None:
    report = timeline_service.check(build_project("Empty"))

    assert report.ok is False
    codes = [issue.code for issue in report.errors]
    assert "NO_SCENES" in codes
    assert "Add at least one scene" in report.errors[0].what_to_do


def test_every_scene_disabled_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    for scene in project.scenes:
        scene.enabled = False

    report = timeline_service.check(project)
    assert "ALL_SCENES_DISABLED" in [issue.code for issue in report.errors]


def test_a_duplicate_scene_id_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    project.scenes[1].id = project.scenes[0].id

    report = timeline_service.check(project)
    assert "DUPLICATE_SCENE_ID" in [issue.code for issue in report.errors]


def test_a_negative_transition_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    project.scenes[0].transition_out.duration = -1.0

    report = timeline_service.check(project)
    issues = [issue for issue in report.errors if issue.code == "TRANSITION_NEGATIVE"]
    assert issues, [issue.code for issue in report.errors]
    assert issues[0].severity == ERROR


def test_a_transition_longer_than_its_scene_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, lengths=(1.0, 1.0))
    project.scenes[0].transition_out.duration = 9.0
    project.scenes[1].transition_in.duration = 9.0

    report = timeline_service.check(project)
    assert "TRANSITION_LONGER_THAN_SCENE" in [issue.code for issue in report.errors]


def test_narration_text_without_a_file_is_a_warning_not_a_failure(timeline_service,
                                                                  tmp_path) -> None:
    """A silent scene is the user's choice; it must not block a render."""
    project = _project(tmp_path)
    project.scenes[0].narration.file = ""

    report = timeline_service.check(project)
    codes = [issue.code for issue in report.issues]
    assert "NARRATION_NOT_GENERATED" in codes
    assert report.ok is True


def test_a_scene_is_never_shorter_than_its_narration(timeline_service, tmp_path) -> None:
    """The safety property behind section 44: narration cannot be cut off.

    ``build_timeline`` derives the scene's length from the measured narration, so
    a scene is always at least as long as the voice in it - even when a short
    manual duration is set, because the narration wins.  That is why the
    timeline needs no "narration too long" error: the comparison could not fire.
    """
    project = _project(tmp_path)
    project.scenes[0].duration = 0.5   # deliberately too short
    report = timeline_service.check(project)

    for timing in report.timeline.timings:
        assert timing.duration >= timing.narration_duration, (
            f"{timing.name}: {timing.duration:.2f}s scene holding "
            f"{timing.narration_duration:.2f}s of narration")
    assert report.timeline.timings[0].source == "narration"


def test_a_caption_past_the_end_of_the_video_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, subtitles=True)
    project.subtitles.cues = [SubtitleCue(id="c1", start=0.0, end=99.0, text="Too long")]

    report = timeline_service.check(project)
    assert "SUBTITLE_PAST_END" in [issue.code for issue in report.errors]


def test_overlapping_captions_are_reported(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, subtitles=True)
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=4.0, text="One"),
        SubtitleCue(id="c2", start=2.0, end=5.0, text="Two"),
    ]

    report = timeline_service.check(project)
    assert "SUBTITLE_OVERLAP" in [issue.code for issue in report.warnings]
    assert report.ok is True, "overlapping captions should warn, not block"


def test_subtitles_enabled_with_no_cues_is_a_warning(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, subtitles=True)
    report = timeline_service.check(project)

    assert "NO_SUBTITLE_CUES" in [issue.code for issue in report.warnings]
    assert report.ok is True


def test_a_reversed_caption_is_fatal(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, subtitles=True)
    project.subtitles.cues = [SubtitleCue(id="c1", start=5.0, end=2.0, text="Backwards")]

    report = timeline_service.check(project)
    assert "SUBTITLE_REVERSED" in [issue.code for issue in report.errors]


def test_the_report_separates_errors_from_warnings(timeline_service, tmp_path) -> None:
    project = _project(tmp_path, subtitles=True)
    project.scenes[0].transition_out.duration = -2.0

    report = timeline_service.check(project)
    assert report.ok is False
    assert all(issue.severity == ERROR for issue in report.errors)
    assert all(issue.severity != ERROR for issue in report.warnings)
    assert "error(s)" in report.summary()
    assert report.describe().startswith(report.summary())
    assert report.to_dict()["ok"] is False


def test_the_summary_is_readable(timeline_service, tmp_path) -> None:
    project = _project(tmp_path)
    summary = timeline_service.summary(project)

    assert summary["scenes"] == 3
    assert summary["ok"] is True
    assert summary["duration"] == pytest.approx(5.5, abs=0.01)
    assert summary["label"]


# --------------------------------------------------------------------------
# The render engine must agree with the service
# --------------------------------------------------------------------------

def test_the_engine_uses_the_timeline_service(tmp_path) -> None:
    """One judgement, not two that could disagree (section 45)."""
    from app.render.engine import RenderEngine
    from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

    project = _project(tmp_path)
    project.scenes[0].transition_out.duration = -1.0

    engine = RenderEngine(FFmpegTools(discover_ffmpeg()), project_dir=tmp_path)
    assert isinstance(engine.timeline_service, TimelineService)

    errors, _warnings = engine.validate(project, duration=5.5)
    codes = [getattr(issue, "code", "") for issue in errors]
    assert "TRANSITION_NEGATIVE" in codes


# --------------------------------------------------------------------------
# SubtitleService
# --------------------------------------------------------------------------

def test_generate_stores_the_cues_on_the_project(subtitle_service, tmp_path) -> None:
    project = _project(tmp_path)
    plan = subtitle_service.generate(project)

    assert plan.ok is True
    assert len(plan.cues) > 0
    assert project.subtitles.cues == plan.cues
    assert project.subtitles.timing_source == "narration"
    assert "caption(s)" in plan.summary()


def test_regenerate_replaces_the_old_captions(subtitle_service, tmp_path) -> None:
    project = _project(tmp_path)
    project.subtitles.cues = [SubtitleCue(id="stale", start=0.0, end=1.0, text="Old")]

    plan = subtitle_service.regenerate(project)
    assert all(cue.id != "stale" for cue in project.subtitles.cues)
    assert len(plan.cues) > 0


def test_generate_without_storing_leaves_the_project_alone(subtitle_service,
                                                           tmp_path) -> None:
    project = _project(tmp_path)
    plan = subtitle_service.generate(project, store=False)

    assert len(plan.cues) > 0
    assert project.subtitles.cues == []


def test_editing_through_the_service_updates_the_project(subtitle_service,
                                                         tmp_path) -> None:
    project = _project(tmp_path)
    subtitle_service.generate(project)
    cue_id = project.subtitles.cues[0].id

    assert subtitle_service.edit(project, cue_id, text="Changed") is True
    assert project.subtitles.cues[0].text == "Changed"
    assert subtitle_service.edit(project, "no-such-cue", text="X") is False


def test_delete_removes_one_caption(subtitle_service, tmp_path) -> None:
    project = _project(tmp_path)
    subtitle_service.generate(project)
    before = len(project.subtitles.cues)
    cue_id = project.subtitles.cues[0].id

    assert subtitle_service.delete(project, cue_id) is True
    assert len(project.subtitles.cues) == before - 1


def test_export_writes_the_requested_formats(subtitle_service, tmp_path) -> None:
    project = _project(tmp_path)
    subtitle_service.generate(project)
    target = tmp_path / "out"

    written = subtitle_service.export(project, target, formats=("srt", "vtt", "ass"))

    assert set(written) == {"srt", "vtt", "ass"}
    for path in written.values():
        assert Path(path).is_file()
        assert Path(path).read_text(encoding="utf-8").strip()
    assert Path(written["vtt"]).read_text(encoding="utf-8").startswith("WEBVTT")


def test_exporting_with_no_captions_writes_nothing(subtitle_service, tmp_path) -> None:
    """A 0-byte .srt would look like a success and is not one."""
    project = _project(tmp_path)
    target = tmp_path / "empty"

    assert subtitle_service.export(project, target) == {}
    assert not target.exists()


def test_validate_reports_overlaps_without_changing_them(subtitle_service,
                                                         tmp_path) -> None:
    project = _project(tmp_path)
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=5.0, text="One"),
        SubtitleCue(id="c2", start=2.0, end=7.0, text="Two"),
    ]

    issues = subtitle_service.validate(project)
    assert "SUBTITLE_OVERLAP" in [issue.code for issue in issues]
    assert len(project.subtitles.cues) == 2, "validation must not edit anything"


def test_ass_content_is_available_for_burn_in(subtitle_service, tmp_path) -> None:
    project = _project(tmp_path)
    subtitle_service.generate(project)

    content = subtitle_service.ass_content(project)
    assert "[Script Info]" in content
    assert "[Events]" in content
    assert "Dialogue:" in content


def test_a_project_without_subtitle_settings_is_refused(subtitle_service) -> None:
    class Bare:
        pass

    with pytest.raises(ValueError):
        subtitle_service.edit(Bare(), "c1", text="X")
