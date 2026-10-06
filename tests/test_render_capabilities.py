"""Export validation, codec detection and estimates (Stage E, sections 24-31, 47).

These are pure tests: the capability object is built by hand so the validation
logic is exercised without depending on whichever FFmpeg happens to be
installed.  A separate test in ``test_render_engine.py`` checks detection
against the real binary.
"""

from __future__ import annotations

import pytest

from app.project.model import build_project
from app.render.capabilities import (
    EncoderCapabilities,
    detect_capabilities,
    estimate_file_size,
    estimate_render_time,
    rate_control_modes,
    validate_export_settings,
)


def caps_with(*encoders: str, audio=("aac",)) -> EncoderCapabilities:
    return EncoderCapabilities(encoders=list(encoders), audio_encoders=list(audio),
                               detected=True, ffmpeg_version="test")


def settings(**overrides):
    project = build_project("Caps")
    for key, value in overrides.items():
        setattr(project.format, key, value)
    return project.format


def codes(issues):
    return [issue.code for issue in issues]


# -- capabilities ----------------------------------------------------------

def test_capabilities_answer_from_the_reported_list():
    caps = caps_with("libx264", "libx265")
    assert caps.supports_codec("h264_cpu")
    assert caps.supports_codec("hevc_cpu")
    assert not caps.supports_codec("vp9_cpu")
    assert caps.available_codecs("mp4") == ["h264_cpu", "hevc_cpu"]
    assert caps.available_codecs("webm") == []


def test_capabilities_say_when_an_encoder_is_missing():
    caps = caps_with("libx264")
    assert "NOT AVAILABLE" in caps.codec_label("vp9_cpu")
    assert "NOT AVAILABLE" not in caps.codec_label("h264_cpu")


def test_capabilities_are_honest_when_nothing_was_detected():
    caps = EncoderCapabilities()
    assert caps.detected is False
    assert caps.encoders == []
    # Nothing is claimed to work when FFmpeg was never asked.
    assert not caps.supports_codec("h264_cpu")


def test_detect_capabilities_without_ffmpeg_is_not_a_guess():
    class NoFFmpeg:
        class discovery:
            has_ffmpeg = False

    caps = detect_capabilities(NoFFmpeg())
    assert caps.detected is False
    assert caps.encoders == []


# -- resolution, fps, container -------------------------------------------

def test_default_settings_are_valid():
    assert validate_export_settings(settings(), caps_with("libx264"), duration=10.0) == []


@pytest.mark.parametrize("width,height,expected", [
    (1920, 1080, None),
    (1080, 1920, None),
    (1921, 1080, "RESOLUTION_ODD"),
    (32, 1080, "RESOLUTION_INVALID"),
    (1920, 99999, "RESOLUTION_INVALID"),
])
def test_resolution_is_checked(width, height, expected):
    found = codes(validate_export_settings(
        settings(width=width, height=height), caps_with("libx264"), duration=5.0))
    if expected is None:
        assert "RESOLUTION_ODD" not in found and "RESOLUTION_INVALID" not in found
    else:
        assert expected in found


def test_fps_must_be_positive():
    assert "FPS_INVALID" in codes(validate_export_settings(
        settings(fps=0), caps_with("libx264"), duration=5.0))


def test_codec_must_fit_the_container():
    found = codes(validate_export_settings(
        settings(container="webm", codec="h264_cpu"), caps_with("libx264", "libvpx-vp9"),
        duration=5.0))
    assert "CODEC_CONTAINER_MISMATCH" in found


def test_a_codec_the_machine_lacks_blocks_the_render():
    found = validate_export_settings(
        settings(codec="hevc_cpu"), caps_with("libx264"), duration=5.0)
    issue = next(item for item in found if item.code == "CODEC_UNAVAILABLE")
    assert issue.severity == "error"
    assert "libx265" in issue.message
    assert issue.what_to_do


def test_a_codec_is_not_rejected_when_ffmpeg_was_never_asked():
    """No FFmpeg means "unknown", not "unavailable" - the UI says so separately."""
    found = codes(validate_export_settings(settings(codec="hevc_cpu"),
                                           EncoderCapabilities(), duration=5.0))
    assert "CODEC_UNAVAILABLE" not in found


def test_pixel_format_must_match_the_codec():
    assert "PIXEL_FORMAT_INVALID" in codes(validate_export_settings(
        settings(codec="vp9_cpu", container="webm", pixel_format="yuv420p10le"),
        caps_with("libvpx-vp9"), duration=5.0))


def test_audio_codec_must_fit_the_container():
    assert "AUDIO_CODEC_INVALID" in codes(validate_export_settings(
        settings(container="webm", codec="vp9_cpu", audio_codec="aac"),
        caps_with("libvpx-vp9"), duration=5.0))


# -- rate control ----------------------------------------------------------

def test_rate_control_modes_are_per_encoder():
    assert rate_control_modes("h264_cpu") == ("crf", "bitrate")
    assert rate_control_modes("vp9_cpu") == ("cq", "bitrate")


def test_an_out_of_range_crf_is_rejected():
    assert "CRF_OUT_OF_RANGE" in codes(validate_export_settings(
        settings(crf=99), caps_with("libx264"), duration=5.0))


def test_a_bitrate_that_is_too_low_is_rejected():
    assert "BITRATE_TOO_LOW" in codes(validate_export_settings(
        settings(bitrate_kbps=10), caps_with("libx264"), duration=5.0))


def test_nothing_to_render_is_an_error_not_a_silent_success():
    found = validate_export_settings(settings(), caps_with("libx264"), duration=0.0)
    issue = next(item for item in found if item.code == "NOTHING_TO_RENDER")
    assert issue.severity == "error"
    assert issue.what_to_do


# -- estimates -------------------------------------------------------------

def test_size_estimate_from_a_target_bitrate():
    estimate = estimate_file_size(settings(bitrate_kbps=8000, audio_bitrate_kbps=192), 60.0)
    # 8192 kbps for 60s is about 60 MiB.
    assert 55 < estimate["mib"] < 65
    assert estimate["basis"] == "target bitrate"
    assert "estimated" in estimate["label"]


def test_size_estimate_from_constant_quality_is_labelled_approximate():
    estimate = estimate_file_size(settings(bitrate_kbps=0, crf=20), 60.0)
    assert estimate["confidence"] == "rough"
    assert "approximation" in estimate["label"]


def test_size_estimate_scales_with_length():
    short = estimate_file_size(settings(bitrate_kbps=4000), 10.0)["bytes"]
    long = estimate_file_size(settings(bitrate_kbps=4000), 60.0)["bytes"]
    assert long == pytest.approx(short * 6, rel=0.01)


def test_size_estimate_handles_an_empty_timeline():
    assert estimate_file_size(settings(), 0.0)["bytes"] == 0


@pytest.mark.parametrize("bytes_total,expected", [
    (500, "B"), (200_000, "KiB"), (5_000_000, "MiB"), (5_000_000_000, "GiB"),
])
def test_size_label_picks_a_readable_scale(bytes_total, expected):
    from app.render.capabilities import _size_label

    assert _size_label(bytes_total).endswith(expected)


def test_time_estimate_uses_the_measured_rate_when_there_is_one():
    estimate = estimate_render_time(frames=600, width=1920, height=1080, fps=30,
                                    measured_fps=30.0)
    assert estimate["encode_fps"] == 30.0
    assert estimate["seconds"] == pytest.approx(20.0)
    assert estimate["basis"] == "measured this run"


def test_time_estimate_says_when_it_is_guessing():
    estimate = estimate_render_time(frames=900, width=1920, height=1080, fps=30,
                                    encoder_preset="veryslow")
    assert "estimated" in estimate["basis"]
    assert estimate["seconds"] > 0


def test_time_estimate_is_not_absurd_for_small_frames():
    """A tiny frame must not produce an estimate of 8000 fps."""
    estimate = estimate_render_time(frames=100, width=320, height=240, fps=25,
                                    encoder_preset="ultrafast")
    assert estimate["encode_fps"] <= 240.0
