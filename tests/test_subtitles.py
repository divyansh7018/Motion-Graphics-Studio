"""Subtitles: generation, timing, export, styling, editing, safe areas
(Stage E, directive sections 14-18).

The rule at the centre of these tests: captions come from *measured* narration
windows.  A scene with no narration audio produces a warning, never an invented
cue with made-up word timings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.project.model import SceneSpec, SubtitleCue, build_project
from app.scene.timing import build_timeline
from app.subtitles import (
    delete_cue,
    edit_cue,
    generate_cues,
    merge_cues,
    split_cue,
    split_sentences,
    timecode_srt,
    timecode_vtt,
    to_ass,
    to_srt,
    to_vtt,
    validate_cues,
    wrap_lines,
    write_subtitle_file,
)


def _project(specs):
    """specs = [(text, narration_duration_or_None, scene_duration)]"""
    project = build_project("Captions")
    for index, (text, narration, duration) in enumerate(specs, start=1):
        scene = project.add_scene(SceneSpec(id=f"s{index}", name=f"Scene {index}", type="text"))
        scene.narration.text = text
        if narration:
            scene.narration.duration = float(narration)
            scene.narration.file = f"audio/s{index}.wav"
        if duration:
            scene.duration = float(duration)
    return project


# -- text handling ---------------------------------------------------------

def test_split_sentences_keeps_the_punctuation():
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_sentences_handles_no_punctuation():
    assert split_sentences("a single line") == ["a single line"]


def test_split_sentences_ignores_blank_input():
    assert split_sentences("   ") == []


def test_wrap_lines_respects_the_width():
    lines = wrap_lines("the quick brown fox jumps over the lazy dog", 20)
    assert all(len(line) <= 20 for line in lines)
    assert " ".join(lines).split() == "the quick brown fox jumps over the lazy dog".split()


def test_wrap_lines_keeps_a_long_word_whole():
    assert wrap_lines("antidisestablishmentarianism", 10) == ["antidisestablishmentarianism"]


# -- generation ------------------------------------------------------------

def test_cues_are_timed_to_the_measured_narration():
    project = _project([("First line here.", 3.0, None), ("Second line.", 2.0, None)])
    timeline = build_timeline(project.scenes)
    cues, issues = generate_cues(project, timeline)

    assert cues
    assert cues[0].start == pytest.approx(0.0)
    # A cue never runs past the narration window it came from.
    window_end = timeline.timings[0].end
    assert cues[0].end <= window_end + 1e-6


def test_a_scene_without_narration_produces_no_cue():
    """No invented timings: a silent scene gets a warning, not a caption."""
    project = _project([("Spoken.", 2.0, None), ("Never spoken.", None, 4.0)])
    cues, issues = generate_cues(project, build_timeline(project.scenes))
    assert len(cues) == 1
    assert [issue.code for issue in issues] == ["SUBTITLE_NO_NARRATION_TIMING"]


def test_long_narration_is_split_into_readable_cues():
    text = ("This is a fairly long piece of narration that should not sit on "
            "screen as one enormous block of text for six whole seconds.")
    project = _project([(text, 6.0, None)])
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    assert len(cues) > 1
    assert all(cue.duration <= 7.0 + 1e-6 for cue in cues)


def test_cues_do_not_overlap():
    project = _project([("One two three.", 3.0, None), ("Four five six.", 3.0, None)])
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    for previous, current in zip(cues, cues[1:]):
        assert current.start >= previous.end - 1e-6


def test_cues_are_ordered_and_unique():
    project = _project([("A.", 2.0, None), ("B.", 2.0, None)])
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    assert [cue.start for cue in cues] == sorted(cue.start for cue in cues)
    assert len({cue.id for cue in cues}) == len(cues)


def test_disabled_scenes_produce_no_cues():
    project = _project([("Spoken.", 2.0, None), ("Also spoken.", 2.0, None)])
    project.scenes[1].enabled = False
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    assert len(cues) == 1


def test_cues_can_be_regenerated_after_an_edit():
    project = _project([("Original text.", 3.0, None)])
    first, _ = generate_cues(project, build_timeline(project.scenes))
    project.scenes[0].narration.text = "Completely different words now."
    second, _ = generate_cues(project, build_timeline(project.scenes))
    assert second[0].text != first[0].text


# -- export formats --------------------------------------------------------

def _cues():
    return [SubtitleCue(id="c1", start=0.0, end=1.5, text="First line"),
            SubtitleCue(id="c2", start=2.0, end=4.25, text="Second line")]


def test_timecodes_use_the_right_separators():
    assert timecode_srt(3661.5) == "01:01:01,500"
    assert timecode_vtt(3661.5) == "01:01:01.500"


def test_srt_is_numbered_and_comma_separated():
    text = to_srt(_cues())
    lines = text.strip().splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:01,500"
    assert lines[2] == "First line"
    assert "4" in text and "00:00:02,000 --> 00:00:04,250" in text


def test_vtt_starts_with_its_header_and_uses_dots():
    text = to_vtt(_cues())
    assert text.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in text
    assert "," not in text.splitlines()[2]


def test_empty_cue_list_still_produces_a_valid_file():
    assert to_srt([]).strip() == ""
    assert to_vtt([]).startswith("WEBVTT")


def test_ass_carries_the_project_styling():
    project = _project([("Styled.", 2.0, None)])
    project.theme.subtitle_style.font = "Verdana"
    project.theme.subtitle_style.font_size = 64
    project.theme.subtitle_style.color = "#ff0000"
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    ass = to_ass(cues, project.theme.subtitle_style, width=1920, height=1080,
                 spec=project.subtitles)

    assert ass.startswith("[Script Info]")
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "Verdana" in ass
    assert "[Events]" in ass and "Dialogue:" in ass


def test_ass_position_follows_the_project_setting():
    project = _project([("Top.", 2.0, None)])
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    bottom = to_ass(cues, project.theme.subtitle_style, width=1920, height=1080,
                    spec=project.subtitles)
    project.subtitles.position = "top"
    top = to_ass(cues, project.theme.subtitle_style, width=1920, height=1080,
                 spec=project.subtitles)
    # ASS alignment 2 = bottom centre, 8 = top centre.
    assert ",2," in bottom
    assert ",8," in top


def test_write_subtitle_file_creates_the_file(tmp_path: Path):
    target = tmp_path / "nested" / "out.srt"
    written = write_subtitle_file(target, to_srt(_cues()))
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("1\n")


# -- editing ---------------------------------------------------------------

def _spec_with_cues():
    project = build_project("Edit")
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=4.0, text="One two three four."),
        SubtitleCue(id="c2", start=4.0, end=8.0, text="Five six."),
    ]
    return project.subtitles


def test_edit_cue_changes_text_and_times():
    spec = _spec_with_cues()
    assert edit_cue(spec, "c1", text="Changed.", start=0.5, end=3.0)
    cue = spec.cues[0]
    assert cue.text == "Changed."
    assert cue.start == 0.5 and cue.end == 3.0


def test_edit_cue_reports_an_unknown_id():
    spec = _spec_with_cues()
    assert edit_cue(spec, "nope", text="x") is False


def test_split_cue_divides_at_the_requested_time():
    spec = _spec_with_cues()
    new_cue = split_cue(spec, "c1", 2.0)
    assert new_cue is not None
    assert len(spec.cues) == 3
    first = next(cue for cue in spec.cues if cue.id == "c1")
    assert first.end == pytest.approx(2.0)
    assert new_cue.start == pytest.approx(2.0)
    assert new_cue.end == pytest.approx(4.0)


def test_split_cue_refuses_a_time_outside_the_cue():
    spec = _spec_with_cues()
    assert split_cue(spec, "c1", 99.0) is None
    assert len(spec.cues) == 2


def test_merge_cues_joins_adjacent_cues():
    spec = _spec_with_cues()
    assert merge_cues(spec, "c1", "c2") is True
    assert len(spec.cues) == 1
    merged = spec.cues[0]
    assert merged.start == pytest.approx(0.0)
    assert merged.end == pytest.approx(8.0)
    assert "One two three four." in merged.text and "Five six." in merged.text


def test_merge_cues_refuses_cues_that_are_not_neighbours():
    spec = _spec_with_cues()
    spec.cues.append(SubtitleCue(id="c3", start=20.0, end=24.0, text="Later."))
    assert merge_cues(spec, "c1", "c3") is False
    assert len(spec.cues) == 3


def test_delete_cue_removes_it():
    spec = _spec_with_cues()
    assert delete_cue(spec, "c2") is True
    assert [cue.id for cue in spec.cues] == ["c1"]
    assert delete_cue(spec, "c2") is False


# -- validation ------------------------------------------------------------

def test_validate_accepts_a_clean_cue_list():
    project = _project([("Fine.", 2.0, None)])
    cues, _ = generate_cues(project, build_timeline(project.scenes))
    from app.scene.canvas import Canvas

    assert validate_cues(cues, canvas=Canvas(1920, 1080),
                         style=project.theme.subtitle_style, spec=project.subtitles) == []


def test_validate_reports_overlapping_cues():
    cues = [SubtitleCue(id="a", start=0.0, end=5.0, text="A"),
            SubtitleCue(id="b", start=4.0, end=6.0, text="B")]
    assert "SUBTITLE_OVERLAP" in [issue.code for issue in validate_cues(cues, canvas=None, style=None)]


def test_validate_reports_an_empty_cue():
    cues = [SubtitleCue(id="a", start=0.0, end=2.0, text="   ")]
    assert "SUBTITLE_EMPTY" in [issue.code for issue in validate_cues(cues, canvas=None, style=None)]


def test_validate_reports_a_cue_that_ends_before_it_starts():
    cues = [SubtitleCue(id="a", start=5.0, end=2.0, text="Backwards")]
    issues = validate_cues(cues, canvas=None, style=None)
    assert any(issue.code == "SUBTITLE_ZERO_LENGTH" and issue.severity == "error"
               for issue in issues)


def test_validate_reports_a_caption_block_taller_than_the_frame():
    from app.scene.canvas import Canvas

    project = build_project("Big")
    project.subtitles.cues = [SubtitleCue(id="a", start=0.0, end=2.0, text="A")]
    project.theme.subtitle_style.font_size = 900
    project.subtitles.margin_percent = 6.0
    issues = validate_cues(project.subtitles.cues, canvas=Canvas(1920, 1080),
                           style=project.theme.subtitle_style, spec=project.subtitles)
    assert "SUBTITLE_TOO_LARGE" in [issue.code for issue in issues]


def test_a_safe_area_violation_is_reported_never_auto_fixed():
    """Directive section 16: warn, do not move the user's captions."""
    from app.scene.canvas import Canvas

    class Rect:
        x, y, width, height = 0.0, 400.0, 1080.0, 1120.0

    project = build_project("Safe")
    cues = [SubtitleCue(id="a", start=0.0, end=2.0, text="A")]
    project.theme.subtitle_style.font_size = 700
    project.theme.subtitle_style.max_lines = 2
    project.subtitles.margin_percent = 2.0
    project.subtitles.position = "bottom"
    issues = validate_cues(cues, canvas=Canvas(1080, 1920),
                           style=project.theme.subtitle_style,
                           spec=project.subtitles, safe_area=Rect())
    assert "SUBTITLE_OUTSIDE_SAFE_AREA" in [issue.code for issue in issues]
    # The cue itself is untouched.
    assert cues[0].start == 0.0 and cues[0].end == 2.0
