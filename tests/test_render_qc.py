"""Quality control on finished files (Stage E, sections 39-40, 57-59, 78).

Every test here inspects a real file with the real encoder, because the whole
point of QC is that it looks at what was written rather than at what was asked
for.  A verdict of FAIL must never come back as success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.render.qc import FAIL, PASS, QCService, WARNING
from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

DISCOVERY = discover_ffmpeg()
pytestmark = pytest.mark.skipif(
    not DISCOVERY.has_ffmpeg, reason="FFmpeg is not installed, so nothing can be checked")


@pytest.fixture(scope="module")
def tools() -> FFmpegTools:
    return FFmpegTools(DISCOVERY)


@pytest.fixture(scope="module")
def qc(tools) -> QCService:
    return QCService(tools, deep_checks=True)


def _make(tools, path: Path, *, seconds=2.0, width=320, height=240, fps=25,
          audio=False, source="testsrc", audio_source=None) -> Path:
    args = ["-hide_banner", "-loglevel", "error", "-y"]
    if source == "black":
        args += ["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={seconds}"]
    else:
        args += ["-f", "lavfi", "-i", f"{source}=size={width}x{height}:rate={fps}:duration={seconds}"]
    if audio:
        tone = audio_source or f"sine=frequency=440:duration={seconds}"
        args += ["-f", "lavfi", "-i", tone]
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-shortest", str(path)]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    result = tools.run(args, timeout=180.0)
    assert result.ok, result.describe_failure()
    return path


# -- the file itself -------------------------------------------------------

def test_a_missing_file_fails(qc, tmp_path: Path):
    report = qc.check(tmp_path / "gone.mp4")
    assert report.verdict == FAIL
    assert report.has_fail
    assert not report.ok
    assert "FILE_MISSING" in [issue.code for issue in report.errors]
    assert report.errors[0].what_to_do


def test_an_empty_file_fails(qc, tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    report = qc.check(empty)
    assert report.verdict == FAIL
    assert "FILE_EMPTY" in [issue.code for issue in report.errors]


def test_junk_that_is_not_a_video_fails(qc, tmp_path: Path):
    junk = tmp_path / "junk.mp4"
    junk.write_text("this is not a video", encoding="utf-8")
    report = qc.check(junk)
    assert report.verdict == FAIL


# -- a good file -----------------------------------------------------------

def test_a_good_video_passes(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "good.mp4", seconds=2.0)
    report = qc.check(video, expected_duration=2.0, expected_width=320,
                      expected_height=240, expected_fps=25, expect_audio=False)
    assert report.verdict == PASS, report.describe()
    assert report.ok
    assert report.measured["width"] == 320
    assert report.measured["video_codec"] == "h264"


def test_a_good_video_with_audio_passes(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "withaudio.mp4", seconds=2.0, audio=True)
    report = qc.check(video, expected_duration=2.0, expected_width=320,
                      expected_height=240, expected_fps=25, expect_audio=True)
    assert report.verdict == PASS, report.describe()
    assert report.measured["has_audio"]


# -- measurements that must not match --------------------------------------

def test_the_wrong_width_is_an_error(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "wide.mp4", width=320)
    report = qc.check(video, expected_width=640, expected_height=240, expect_audio=False)
    assert report.verdict == FAIL
    assert "WIDTH_MISMATCH" in [issue.code for issue in report.errors]


def test_the_wrong_frame_rate_is_an_error(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "fps.mp4", fps=25)
    report = qc.check(video, expected_fps=30, expect_audio=False)
    assert "FPS_MISMATCH" in [issue.code for issue in report.errors]


def test_a_video_shorter_than_the_timeline_is_an_error(qc, tools, tmp_path: Path):
    """The end of the video is missing - that must never pass."""
    video = _make(tools, tmp_path / "short.mp4", seconds=2.0)
    report = qc.check(video, expected_duration=10.0, expect_audio=False)
    assert report.verdict == FAIL
    assert "VIDEO_TOO_SHORT" in [issue.code for issue in report.errors]


def test_a_slightly_longer_video_is_a_warning_not_a_failure(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "long.mp4", seconds=4.0)
    report = qc.check(video, expected_duration=2.0, expect_audio=False)
    assert "VIDEO_TOO_LONG" in [issue.code for issue in report.warnings]
    assert report.verdict != FAIL


def test_missing_audio_is_an_error_when_the_project_has_voice(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "silent.mp4", audio=False)
    report = qc.check(video, expect_audio=True)
    assert "NO_AUDIO_STREAM" in [issue.code for issue in report.errors]
    assert report.verdict == FAIL


# -- deep scans ------------------------------------------------------------

def test_a_black_video_is_reported(qc, tools, tmp_path: Path):
    """Real black-frame detection, not a guess from the scene model."""
    video = _make(tools, tmp_path / "black.mp4", seconds=2.0, source="black")
    report = qc.check(video, expect_audio=False)
    assert "BLACK_FRAMES" in [issue.code for issue in report.issues]


def test_a_video_with_a_picture_is_not_called_black(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "colour.mp4", seconds=2.0, source="testsrc")
    report = qc.check(video, expect_audio=False)
    assert "BLACK_FRAMES" not in [issue.code for issue in report.issues]


def test_silent_audio_is_reported(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "silence.mp4", seconds=2.0, audio=True,
                  audio_source="anullsrc=channel_layout=stereo:sample_rate=48000")
    report = qc.check(video, expect_audio=True)
    codes = [issue.code for issue in report.issues]
    assert "AUDIO_SILENT" in codes or "AUDIO_LONG_SILENCE" in codes


def test_loud_audio_is_not_called_silent(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "loud.mp4", seconds=2.0, audio=True)
    report = qc.check(video, expect_audio=True)
    assert "AUDIO_SILENT" not in [issue.code for issue in report.issues]


def test_skipping_the_deep_checks_says_so(qc, tools, tmp_path: Path):
    """A skipped scan is reported, never implied to have passed."""
    shallow = QCService(tools, deep_checks=False)
    video = _make(tools, tmp_path / "shallow.mp4", seconds=2.0, source="black")
    report = shallow.check(video, expect_audio=False)
    assert "DEEP_CHECKS_SKIPPED" in [issue.code for issue in report.issues]
    assert report.verdict == WARNING


# -- inherited findings ----------------------------------------------------

def test_scene_warnings_are_carried_into_the_report(qc, tools, tmp_path: Path):
    class Finding:
        code = "TEXT_OVERFLOW"
        message = "The heading does not fit on two lines."
        what_to_do = "Shorten the heading."
        severity = "warning"

    video = _make(tools, tmp_path / "inherit.mp4", seconds=2.0)
    report = qc.check(video, expect_audio=False, inherited=[Finding()])
    codes = [issue.code for issue in report.issues]
    assert "TEXT_OVERFLOW" in codes
    assert report.verdict == WARNING
    assert "Shorten the heading." in report.describe()


def test_an_inherited_error_fails_the_report(qc, tools, tmp_path: Path):
    class Finding:
        code = "SCENE_BROKEN"
        message = "A scene could not be drawn."
        what_to_do = "Fix the scene."
        severity = "error"

    video = _make(tools, tmp_path / "inheritbad.mp4", seconds=2.0)
    report = qc.check(video, expect_audio=False, inherited=[Finding()])
    assert report.verdict == FAIL


# -- the report ------------------------------------------------------------

def test_the_report_explains_itself(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "report.mp4", seconds=2.0, audio=True)
    report = qc.check(video, expected_duration=2.0, expected_width=320,
                      expected_height=240, expected_fps=25, expect_audio=True)
    text = report.describe()
    assert "Quality check:" in text
    assert "320x240" in text
    assert report.to_dict()["verdict"] == report.verdict
    assert report.path == video


def test_a_fail_is_never_reported_as_ok(qc, tmp_path: Path):
    report = qc.check(tmp_path / "nothing.mp4")
    assert report.verdict == FAIL
    assert report.ok is False
    assert report.has_fail is True
    assert report.errors


def test_subtitle_length_is_compared_with_the_video(qc, tools, tmp_path: Path):
    video = _make(tools, tmp_path / "subs.mp4", seconds=2.0)
    report = qc.check(video, expect_audio=False, expect_subtitles=9.0)
    assert "SUBTITLES_LONGER_THAN_VIDEO" in [issue.code for issue in report.issues]
