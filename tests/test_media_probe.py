"""Media probing: reading back what a file actually contains (Stage E, sections
39, 57).

The parsing tests use the exact banner FFmpeg prints, so they run anywhere; the
probe tests use a real file and skip when FFmpeg is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.media.probe import MediaInfo, format_timecode, parse_ffmpeg_info, probe_media
from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

DISCOVERY = discover_ffmpeg()

BANNER = """ffmpeg version 7.0.2-static Copyright (c) 2000-2024 the FFmpeg developers
  built with gcc 13.2.0
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/probe_test.mp4':
  Metadata:
    major_brand     : isom
  Duration: 00:00:02.00, start: 0.000000, bitrate: 181 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 320x240 [SAR 1:1 DAR 4:3], 45 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, mono, fltp, 122 kb/s (default)
At least one output file must be specified"""


def test_the_real_banner_is_parsed_completely():
    info = parse_ffmpeg_info(BANNER, path="/tmp/probe_test.mp4", size_bytes=45409)
    assert info.ok
    assert info.duration == pytest.approx(2.0)
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(25.0)
    assert info.video_codec == "h264"
    assert info.pixel_format == "yuv420p"
    assert info.audio_codec == "aac"
    assert info.sample_rate == 44100
    assert info.channels == 1
    assert info.bitrate_kbps == 181
    assert info.size_bytes == 45409


def test_both_streams_are_recorded():
    info = parse_ffmpeg_info(BANNER, path="x.mp4")
    kinds = [stream.kind for stream in info.streams]
    assert "video" in kinds and "audio" in kinds
    assert info.has_video and info.has_audio


def test_a_video_only_file_has_no_audio():
    text = BANNER.split("Stream #0:1")[0]
    info = parse_ffmpeg_info(text, path="x.mp4")
    assert info.has_video
    assert not info.has_audio          # derived from the stream list, not a field
    assert not info.audio_codec


def test_an_audio_only_file_reports_no_video():
    text = """Input #0, wav, from 'a.wav':
  Duration: 00:00:10.00, start: 0.000000, bitrate: 1536 kb/s
  Stream #0:0[0x1](und): Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, stereo, s16, 1536 kb/s"""
    info = parse_ffmpeg_info(text, path="a.wav")
    assert info.has_audio and not info.has_video
    assert info.duration == pytest.approx(10.0)
    assert info.channels == 2


def test_a_fractional_frame_rate_is_kept():
    text = """  Duration: 00:00:01.00, start: 0.000000, bitrate: 100 kb/s
  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 640x360, 29.97 fps, 29.97 tbr"""
    info = parse_ffmpeg_info(text, path="x.mp4")
    assert info.fps == pytest.approx(29.97, abs=0.01)


def test_a_duration_of_over_an_hour_is_parsed():
    text = """  Duration: 01:02:03.50, start: 0.000000, bitrate: 100 kb/s
  Stream #0:0[0x1](und): Video: h264, yuv420p, 1920x1080, 30 fps"""
    info = parse_ffmpeg_info(text, path="x.mp4")
    assert info.duration == pytest.approx(3723.5)


def test_garbage_output_is_reported_as_a_failure_not_a_guess():
    info = parse_ffmpeg_info("this is not ffmpeg output at all", path="x.mp4")
    assert not info.ok
    assert info.error
    assert info.duration == 0.0


def test_the_aspect_ratio_is_derived_not_stored():
    """Computed from the real dimensions, so it can never disagree with them."""
    info = parse_ffmpeg_info(BANNER, path="x.mp4")
    assert info.aspect_ratio == pytest.approx(4 / 3)
    assert info.resolution == "320x240"


def test_a_square_frame_has_a_ratio_of_one():
    text = """  Duration: 00:00:01.00, start: 0.000000, bitrate: 100 kb/s
  Stream #0:0[0x1](und): Video: h264, yuv420p, 1080x1080, 30 fps"""
    info = parse_ffmpeg_info(text, path="x.mp4")
    assert info.aspect_ratio == pytest.approx(1.0)


def test_a_zero_height_never_divides_by_zero():
    info = MediaInfo(ok=True, width=1920, height=0)
    assert info.aspect_ratio == 0.0


def test_the_summary_is_readable():
    info = parse_ffmpeg_info(BANNER, path="x.mp4")
    summary = info.summary()
    assert "320x240" in summary and "h264" in summary and "aac" in summary


def test_timecodes_are_formatted_for_humans():
    assert format_timecode(0) == "0:00:00.00"
    assert format_timecode(2.0) == "0:00:02.00"
    assert format_timecode(3723.5) == "1:02:03.50"


def test_to_dict_round_trips_the_measured_values():
    info = parse_ffmpeg_info(BANNER, path="x.mp4", size_bytes=100)
    data = info.to_dict()
    assert data["duration"] == pytest.approx(2.0)
    assert data["width"] == 320
    assert data["size_bytes"] == 100


# -- against a real file ---------------------------------------------------

@pytest.mark.skipif(not DISCOVERY.has_ffmpeg, reason="FFmpeg is not installed")
def test_a_real_file_is_probed(tmp_path: Path):
    tools = FFmpegTools(DISCOVERY)
    target = tmp_path / "real.mp4"
    result = tools.run([
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(target),
    ], timeout=120.0)
    assert result.ok, result.describe_failure()

    info = probe_media(target, tools)
    assert info.ok
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(25.0, abs=0.5)
    assert info.duration == pytest.approx(2.0, abs=0.3)
    assert info.video_codec == "h264"
    assert not info.has_audio


@pytest.mark.skipif(not DISCOVERY.has_ffmpeg, reason="FFmpeg is not installed")
def test_a_missing_file_is_reported_not_raised(tmp_path: Path):
    info = probe_media(tmp_path / "gone.mp4", FFmpegTools(DISCOVERY))
    assert not info.ok
    assert info.error


@pytest.mark.skipif(not DISCOVERY.has_ffmpeg, reason="FFmpeg is not installed")
def test_an_empty_file_is_reported(tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    info = probe_media(empty, FFmpegTools(DISCOVERY))
    assert not info.ok


@pytest.mark.skipif(not DISCOVERY.has_ffmpeg, reason="FFmpeg is not installed")
def test_a_truncated_file_is_not_reported_as_good(tmp_path: Path):
    tools = FFmpegTools(DISCOVERY)
    target = tmp_path / "whole.mp4"
    tools.run(["-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
               "-c:v", "libx264", "-preset", "ultrafast", str(target)], timeout=120.0)
    truncated = tmp_path / "cut.mp4"
    truncated.write_bytes(target.read_bytes()[: target.stat().st_size // 3])

    info = probe_media(truncated, tools)
    # Either it cannot be read at all, or the duration it reports is short.
    assert (not info.ok) or (info.duration is None) or (info.duration < 3.0)


def test_an_empty_mediainfo_is_never_ok():
    info = MediaInfo(source="nothing")
    assert not info.ok
    assert not info.has_video
    assert info.duration == 0.0
