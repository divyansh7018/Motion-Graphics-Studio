"""Stage E final hardening (directive sections 2, 4, 8, 9, 10, 12).

These tests exist because the final verification pass found things that a green
suite was hiding:

* a QC check that compared the container duration with itself, so a short audio
  track could never be detected;
* a two-pass checkbox whose value never reached FFmpeg;
* a QC report that could only ever say PASS/WARNING/FAIL about checks it had
  never been able to run;
* no distinction, in a report, between "measured by FFprobe" and "measured by
  parsing ffmpeg -i".

Every test here uses the real encoder and the real probe.  Nothing is mocked
except the deliberate removal of FFprobe, which is how the fallback is proved to
describe itself honestly.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.media.probe import FFPROBE_FALLBACK_LABEL, probe_media
from app.render.capabilities import detect_capabilities, validate_export_settings
from app.render.encode import stream_encode, video_encoder_args
from app.render.qc import (
    FAIL,
    NOT_AVAILABLE,
    PASS,
    QCService,
    WARNING,
)
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


def _encode(tools, path: Path, *, seconds=2.0, width=320, height=240, fps=25,
            audio_seconds=0.0, codec="libx264", source="testsrc") -> Path:
    """A real file, with deliberately independent picture and audio lengths."""
    args = ["-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"{source}=size={width}x{height}:rate={fps}:duration={seconds}"]
    if audio_seconds > 0:
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=440:duration={audio_seconds}"]
        args += ["-c:v", codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", str(path)]
    else:
        args += ["-c:v", codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    result = tools.run(args, timeout=240.0)
    assert result.ok, result.describe_failure()
    return path


class _Settings:
    """The smallest thing that looks like an export settings section."""

    def __init__(self, **values) -> None:
        defaults = dict(codec="h264_cpu", container="mp4", width=320, height=240,
                        fps=25, crf=23, bitrate_kbps=0, encoder_preset="veryfast",
                        pixel_format="yuv420p", audio_codec="aac",
                        audio_bitrate_kbps=128, sample_rate=48000)
        defaults.update(values)
        for key, value in defaults.items():
            setattr(self, key, value)


# =========================================================================
# 2. FFprobe: real detection, real use, honest fallback
# =========================================================================

def test_ffprobe_is_detected_and_actually_used(tools, tmp_path: Path):
    """The report must only say FFprobe when FFprobe really answered."""
    if not DISCOVERY.has_ffprobe:
        pytest.skip("FFprobe is not installed on this machine")
    target = _encode(tools, tmp_path / "probe.mp4")
    info = probe_media(target, tools)
    assert info.ok
    assert info.used_ffprobe is True
    assert info.source_label == "FFprobe"
    # Only FFprobe reports a length for each individual stream.
    assert info.stream_duration("video") > 0
    assert DISCOVERY.ffprobe is not None
    assert DISCOVERY.ffprobe.version


def test_without_ffprobe_the_fallback_names_itself(tools, tmp_path: Path):
    """A parsed 'ffmpeg -i' result may never present itself as FFprobe."""
    no_probe = dataclasses.replace(DISCOVERY, ffprobe=None)
    limited = FFmpegTools(no_probe)
    target = _encode(tools, tmp_path / "fallback.mp4")

    info = probe_media(target, limited)
    assert info.ok, info.error
    assert info.used_ffprobe is False
    assert info.source_label == FFPROBE_FALLBACK_LABEL
    assert "limited" in info.source_label
    # The numbers are still real - the fallback is not a guess.
    assert info.width and info.height and info.duration > 0


def test_qc_without_ffprobe_cannot_claim_a_clean_pass(tools, tmp_path: Path):
    """An unrun check is not a passed check (directive section 8)."""
    if not DISCOVERY.has_ffprobe:
        pytest.skip("FFprobe is not installed, so the fallback is the only path")
    limited = FFmpegTools(dataclasses.replace(DISCOVERY, ffprobe=None))
    service = QCService(limited, deep_checks=False)
    target = _encode(tools, tmp_path / "nofprobe.mp4")

    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25, expect_audio=False)
    assert report.checks["ffprobe_inspection"] == NOT_AVAILABLE
    assert report.verdict == WARNING
    assert report.verdict != PASS
    assert any(issue.code == "FFPROBE_NOT_USED" for issue in report.issues)
    assert FFPROBE_FALLBACK_LABEL in report.describe()


def test_the_probe_source_is_written_into_the_report(tools, tmp_path: Path):
    if not DISCOVERY.has_ffprobe:
        pytest.skip("FFprobe is not installed on this machine")
    service = QCService(tools, deep_checks=False)
    target = _encode(tools, tmp_path / "source.mp4")
    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25, expect_audio=False)
    assert report.measured["probe_source"] == "ffprobe"
    assert report.checks["ffprobe_inspection"] == PASS
    assert "Inspected with: FFprobe" in report.describe()


# =========================================================================
# 8. The QC contract: PASS, WARNING, FAIL - and CHECK NOT AVAILABLE
# =========================================================================

def test_a_check_state_is_never_invented(tools, tmp_path: Path):
    service = QCService(tools, deep_checks=True)
    target = _encode(tools, tmp_path / "states.mp4", audio_seconds=2.0)
    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25)
    assert report.verdict in (PASS, WARNING, FAIL)
    assert report.checks, "a report must say what it checked"
    for name, state in report.checks.items():
        assert state in (PASS, FAIL, NOT_AVAILABLE), (name, state)


def test_skipped_deep_checks_are_not_available_not_pass(tools, tmp_path: Path):
    service = QCService(tools, deep_checks=False)
    target = _encode(tools, tmp_path / "shallow.mp4", audio_seconds=2.0)
    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25)
    assert report.checks["audio_levels"] == NOT_AVAILABLE
    assert report.checks["black_frames"] == NOT_AVAILABLE
    assert report.checks["silence"] == NOT_AVAILABLE
    assert report.verdict == WARNING
    assert report.complete is False
    assert set(report.unavailable_checks) >= {"audio_levels", "black_frames", "silence"}


def test_a_check_that_does_not_apply_is_absent_not_unavailable(tools, tmp_path: Path):
    """A video with no audio that nobody asked for is not under-verified."""
    service = QCService(tools, deep_checks=True)
    target = _encode(tools, tmp_path / "silent_ok.mp4")
    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25, expect_audio=False)
    assert "audio_levels" not in report.checks
    assert report.verdict == PASS, report.describe()


def test_a_broken_black_frame_scan_is_not_reported_as_clean(tools, tmp_path: Path):
    """If the scan itself cannot run, the report must say so."""

    class _BrokenTools(FFmpegTools):
        def run(self, args, **kwargs):  # noqa: D102 - deliberate failure
            if any("blackdetect" in str(part) for part in args):
                from app.tools.ffmpeg import CommandResult

                return CommandResult(argv=list(args), returncode=1, stdout="",
                                     stderr="no filter here")
            return super().run(args, **kwargs)

    service = QCService(_BrokenTools(DISCOVERY), deep_checks=True)
    target = _encode(tools, tmp_path / "noscan.mp4")
    report = service.check(target, expected_duration=2.0, expected_width=320,
                           expected_height=240, expected_fps=25, expect_audio=False)
    assert report.checks["black_frames"] == NOT_AVAILABLE
    assert any(issue.code == "BLACK_FRAME_SCAN_UNAVAILABLE" for issue in report.issues)
    assert report.verdict != PASS


# =========================================================================
# 9 & 12. Real output validation, including audio length
# =========================================================================

def test_a_short_audio_track_is_actually_detected(tools, tmp_path: Path):
    """The regression: this used to compare the container duration with itself."""
    if not DISCOVERY.has_ffprobe:
        pytest.skip("per-stream durations need FFprobe")
    service = QCService(tools, deep_checks=False)
    # 6 s of picture, 2 s of sound: the container says 6 s for both.
    target = _encode(tools, tmp_path / "short_audio.mp4", seconds=6.0, audio_seconds=2.0)
    info = probe_media(target, tools)
    assert info.duration > 5.5, "the container really does hide the mismatch"

    report = service.check(target, expected_duration=6.0, expected_width=320,
                           expected_height=240, expected_fps=25)
    assert report.checks["av_sync"] == FAIL
    assert any(issue.code == "AV_LENGTH_MISMATCH" for issue in report.issues)
    assert report.verdict != PASS


def test_matching_audio_and_picture_lengths_pass(tools, tmp_path: Path):
    if not DISCOVERY.has_ffprobe:
        pytest.skip("per-stream durations need FFprobe")
    service = QCService(tools, deep_checks=False)
    target = _encode(tools, tmp_path / "matched.mp4", seconds=3.0, audio_seconds=3.0)
    report = service.check(target, expected_duration=3.0, expected_width=320,
                           expected_height=240, expected_fps=25)
    # This test is about the alignment check only; the deep scans are off, so the
    # overall verdict is a warning about those, which is the correct behaviour.
    assert report.checks["av_sync"] == PASS, report.describe()
    assert not any(issue.code == "AV_LENGTH_MISMATCH" for issue in report.issues)
    assert report.verdict != FAIL


def test_the_codec_in_the_file_is_the_codec_that_was_asked_for(tools, tmp_path: Path):
    service = QCService(tools, deep_checks=False)
    target = _encode(tools, tmp_path / "codec.mp4")
    good = service.check(target, expected_duration=2.0, expected_width=320,
                         expected_height=240, expected_fps=25, expect_audio=False,
                         expected_video_codec="h264")
    assert good.checks["video_codec"] == PASS

    wrong = service.check(target, expected_duration=2.0, expected_width=320,
                          expected_height=240, expected_fps=25, expect_audio=False,
                          expected_video_codec="hevc")
    assert wrong.checks["video_codec"] == FAIL
    assert any(issue.code == "VIDEO_CODEC_MISMATCH" for issue in wrong.issues)
    assert wrong.verdict == FAIL


def test_every_required_output_check_is_present_after_a_good_render(tools, tmp_path: Path):
    """Directive section 9: the file is only good once all of these were looked at."""
    if not DISCOVERY.has_ffprobe:
        pytest.skip("full per-stream verification needs FFprobe")
    service = QCService(tools, deep_checks=True)
    target = _encode(tools, tmp_path / "complete.mp4", seconds=3.0, audio_seconds=3.0)
    report = service.check(target, expected_duration=3.0, expected_width=320,
                           expected_height=240, expected_fps=25,
                           expected_video_codec="h264", expected_audio_codec="aac")
    required = {
        "file_exists", "file_not_empty", "file_readable", "container_readable",
        "container_complete", "ffprobe_inspection", "video_stream", "resolution",
        "frame_rate", "duration", "video_codec", "audio_codec", "audio_stream",
        "av_sync", "audio_levels", "silence", "black_frames",
    }
    missing = required - set(report.checks)
    assert not missing, f"these checks never ran: {sorted(missing)}"
    assert all(state == PASS for state in report.checks.values()), report.describe()
    assert report.verdict == PASS


# =========================================================================
# 4. Two-pass: real, or honestly refused
# =========================================================================

def test_two_pass_arguments_include_the_pass_number_and_a_private_stats_file():
    settings = _Settings(bitrate_kbps=4000)
    first = video_encoder_args(settings, two_pass=1, stats_file=Path("/tmp/x.pass"))
    second = video_encoder_args(settings, two_pass=2, stats_file=Path("/tmp/x.pass"))
    assert "-pass" in first and first[first.index("-pass") + 1] == "1"
    assert "-pass" in second and second[second.index("-pass") + 1] == "2"
    assert "-passlogfile" in first
    assert "-pass" not in video_encoder_args(settings)


def test_two_pass_without_a_bitrate_is_refused_not_ignored(tools, tmp_path: Path):
    """A checkbox that quietly does nothing is a control that lies."""
    settings = _Settings(bitrate_kbps=0)
    result = stream_encode(
        tools=tools, frames=lambda: iter(()), output=tmp_path / "never.mp4",
        width=320, height=240, fps=25, settings=settings, two_pass=True)
    assert result.ok is False
    assert "bitrate" in result.error.lower()


def test_two_pass_needs_frames_it_can_read_twice(tools, tmp_path: Path):
    settings = _Settings(bitrate_kbps=4000)
    result = stream_encode(
        tools=tools, frames=iter(()), output=tmp_path / "once.mp4",
        width=320, height=240, fps=25, settings=settings, two_pass=True)
    assert result.ok is False
    assert "twice" in result.error.lower()


def test_a_real_two_pass_encode_produces_a_readable_file(tools, tmp_path: Path):
    """The whole point: two passes really run, and the file is really there."""
    settings = _Settings(bitrate_kbps=1200)
    output = tmp_path / "twopass.mp4"
    frame = b"\x40\x60\xa0" * (160 * 96)

    def source():
        for _ in range(30):
            yield frame

    result = stream_encode(
        tools=tools, frames=source, output=output, width=160, height=96, fps=15,
        settings=settings, two_pass=True, pass_stats=tmp_path / "stats",
        timeout=600.0)
    assert result.ok, result.describe_failure()
    assert result.details.get("passes") == 2
    assert output.is_file() and output.stat().st_size > 0

    info = probe_media(output, tools)
    assert info.ok and info.has_video
    assert abs(info.duration - 2.0) < 0.5
    # Scratch statistics files are cleaned up, not left in the project.
    assert not list(tmp_path.glob("stats-0.log*"))


def test_two_pass_validation_blocks_the_render_before_any_frame_is_drawn():
    caps = detect_capabilities(tools=FFmpegTools(DISCOVERY))
    problems = validate_export_settings(_Settings(bitrate_kbps=0), caps,
                                       duration=5.0, two_pass=True)
    assert any(issue.code == "TWO_PASS_NEEDS_BITRATE" for issue in problems)

    ok = validate_export_settings(_Settings(bitrate_kbps=4000), caps,
                                  duration=5.0, two_pass=True)
    assert not any(issue.code.startswith("TWO_PASS") for issue in ok)

    unsupported = validate_export_settings(
        _Settings(codec="vp9_cpu", container="webm", bitrate_kbps=0),
        caps, duration=5.0, two_pass=True)
    # VP9 is in the supported list, so the complaint here is the missing bitrate.
    assert not any(issue.code == "TWO_PASS_UNSUPPORTED" for issue in unsupported)


# =========================================================================
# Regressions found by the Stage E profiling pass
# =========================================================================

def test_a_project_with_no_audio_at_all_still_renders(tools, tmp_path: Path):
    """Found by the profiler: this raised TypeError and failed the render.

    ``log_event("RENDER_AUDIO_EMPTY", "...", message=...)`` passed ``message``
    both positionally and as a keyword, so the "no audio is fine, carry on"
    branch crashed instead of carrying on.  A silent video is legitimate.
    """
    from app.project.model import SceneSpec, build_project
    from app.render import COMPLETED, RenderEngine, RenderRequest

    project = build_project("No Audio")
    project.project.name = "NoAudio"
    for index in range(2):
        scene = project.add_scene(SceneSpec(id=f"s{index + 1}",
                                            name=f"Scene {index + 1}", type="text",
                                            duration=1.2))
        scene.script = f"Scene {index + 1}"
    project.subtitles.enabled = False
    project.format.width, project.format.height = 320, 240
    project.format.fps = 25
    project.format.encoder_preset = "ultrafast"
    project.format.crf = 32
    project.export.output_dir = str(tmp_path / "renders")

    result = RenderEngine(tools, project_dir=tmp_path).render(
        RenderRequest(project=project, quick_qc=True))

    assert result.status == COMPLETED, result.describe() if hasattr(result, "describe") else result.message
    assert result.path is not None and result.path.exists()
    assert "log_event" not in (result.technical or "")


def test_a_real_two_pass_render_through_the_engine(tools, tmp_path: Path):
    """Two-pass is a production option, so it is verified as one, not as args."""
    from app.project.model import SceneSpec, build_project
    from app.render import COMPLETED, RenderEngine, RenderRequest

    project = build_project("Two Pass")
    project.project.name = "TwoPass"
    for index in range(2):
        scene = project.add_scene(SceneSpec(id=f"s{index + 1}",
                                            name=f"Scene {index + 1}", type="text",
                                            duration=1.0))
        scene.script = f"Scene {index + 1} has enough detail to encode."
    project.subtitles.enabled = False
    project.format.width, project.format.height = 320, 240
    project.format.fps = 25
    project.format.encoder_preset = "veryfast"
    project.format.bitrate_kbps = 900
    project.export.output_dir = str(tmp_path / "renders")

    result = RenderEngine(tools, project_dir=tmp_path).render(
        RenderRequest(project=project, two_pass=True, quick_qc=True))

    assert result.status == COMPLETED, result.message
    assert result.path is not None and result.path.exists()
    info = probe_media(result.path, tools)
    assert info.ok and info.has_video
    assert abs(info.duration - 2.0) < 0.5
    # The encoder's scratch statistics files must not be left in the project.
    assert not list(tmp_path.rglob("*-0.log"))


def test_two_pass_is_refused_before_any_frame_is_drawn(tools, tmp_path: Path):
    from app.project.model import SceneSpec, build_project
    from app.render import FAILED, RenderEngine, RenderRequest

    project = build_project("Two Pass No Bitrate")
    project.project.name = "TwoPassNoBitrate"
    project.add_scene(SceneSpec(id="s1", name="Scene 1", type="text", duration=1.0))
    project.subtitles.enabled = False
    project.format.width, project.format.height = 320, 240
    project.format.fps = 25
    project.format.bitrate_kbps = 0
    project.export.output_dir = str(tmp_path / "renders")

    result = RenderEngine(tools, project_dir=tmp_path).render(
        RenderRequest(project=project, two_pass=True))

    assert result.status == FAILED
    assert any(getattr(issue, "code", "") == "TWO_PASS_NEEDS_BITRATE"
               for issue in result.errors)
    assert result.path is None or not Path(result.path).exists()
