"""Preset tables (directive sections 25, 26, 27).

The tables drive the wizard *and* validation, so they are checked for internal
consistency here: an option the UI can offer must never be one that validation
rejects, and no "fake" option may appear.
"""

from __future__ import annotations

from app.project.presets import (
    ASPECT_PRESETS,
    AUDIO_CODECS_BY_CONTAINER,
    CODECS_BY_CONTAINER,
    CONTAINERS,
    CRF_RANGES,
    ENCODER_PRESETS,
    FPS_OPTIONS,
    PIXEL_FORMATS_BY_CODEC,
    PROJECT_TEMPLATES,
    QUALITY_PRESETS,
    VIDEO_CODECS,
    aspect_preset,
    aspect_ratio_label,
    audio_codec_in_container,
    codec_in_container,
    default_audio_codec,
    default_codec,
    default_pixel_format,
    preview_filename,
    project_template,
    resolve_quality,
)
from app.project.validation import validate_project
from app.project.model import build_project


def test_every_quality_preset_resolves_to_real_numbers() -> None:
    """'High' alone cannot reproduce a render; the numbers must be there."""
    for preset in QUALITY_PRESETS:
        resolved = resolve_quality(preset.key)
        assert resolved["crf"] >= 0
        assert resolved["encoder_preset"] in ENCODER_PRESETS
        assert resolved["pixel_format"] in PIXEL_FORMATS_BY_CODEC[resolved["codec"]]
        assert resolved["audio_codec"] in AUDIO_CODECS_BY_CONTAINER[resolved["container"]]
        assert resolved["sample_rate"] > 0


def test_quality_levels_are_ordered_from_fast_to_best() -> None:
    crfs = [preset.crf for preset in QUALITY_PRESETS]
    assert crfs == sorted(crfs, reverse=True), "each level must improve quality"
    assert [preset.key for preset in QUALITY_PRESETS][0] == "draft"
    assert [preset.key for preset in QUALITY_PRESETS][-1] == "ultra"


def test_custom_quality_keeps_the_users_numbers() -> None:
    custom = {"crf": 27, "encoder_preset": "fast", "bitrate_kbps": 8000, "codec": "hevc_cpu"}

    resolved = resolve_quality("custom", container="mkv", codec="hevc_cpu", custom=custom)

    assert resolved["crf"] == 27
    assert resolved["encoder_preset"] == "fast"
    assert resolved["bitrate_kbps"] == 8000
    assert resolved["quality_preset"] == "custom"


def test_codec_tables_are_self_consistent() -> None:
    for container in CONTAINERS:
        assert CODECS_BY_CONTAINER[container], f"{container} offers no video codec"
        assert AUDIO_CODECS_BY_CONTAINER[container], f"{container} offers no audio codec"
        assert default_codec(container) in CODECS_BY_CONTAINER[container]
        assert default_audio_codec(container) in AUDIO_CODECS_BY_CONTAINER[container]
        for codec in CODECS_BY_CONTAINER[container]:
            assert codec in VIDEO_CODECS
            assert codec_in_container(codec, container)
            assert CRF_RANGES[codec][0] < CRF_RANGES[codec][1]
            assert default_pixel_format(codec) in PIXEL_FORMATS_BY_CODEC[codec]
        for audio_codec in AUDIO_CODECS_BY_CONTAINER[container]:
            assert audio_codec_in_container(audio_codec, container)


def test_webm_only_offers_what_a_cpu_can_do() -> None:
    assert CODECS_BY_CONTAINER["webm"] == ("vp9_cpu",)
    assert AUDIO_CODECS_BY_CONTAINER["webm"] == ("opus",)


def test_every_template_produces_a_valid_project() -> None:
    """A template must never seed a project that fails validation."""
    for template in PROJECT_TEMPLATES:
        quality = resolve_quality(template.quality)
        project = build_project(
            f"{template.label} project",
            template_key=template.key,
            width=template.width,
            height=template.height,
            fps=template.fps,
            quality=quality,
            background=template.background,
            accent=template.accent,
            heading_font=template.heading_font,
            body_font=template.body_font,
            subtitles_enabled=template.subtitles_enabled,
            subtitle_font_size=template.subtitle_font_size,
            filename_template=template.filename_template,
            script_note=template.script_note,
        )
        report = validate_project(project)
        assert report.ok, f"{template.key}: {report.to_text()}"
        assert aspect_ratio_label(template.width, template.height) == template.aspect


def test_templates_are_presets_not_content() -> None:
    """Directive section 41: a template must not invent scenes or media."""
    for template in PROJECT_TEMPLATES:
        project = build_project("T", template_key=template.key, width=template.width, height=template.height, fps=template.fps)
        assert project.scenes == []
        assert project.assets == []


def test_template_lookup_handles_unknown_keys() -> None:
    assert project_template("youtube") is not None
    assert project_template("does-not-exist") is None


def test_aspect_presets_cover_the_required_formats() -> None:
    keys = {preset.key for preset in ASPECT_PRESETS}
    assert {"16:9", "9:16", "1:1", "4:5", "4:3"} <= keys
    for preset in ASPECT_PRESETS:
        assert preset.resolutions
        width, height = preset.default_resolution
        assert width >= 256 and height >= 256
        assert width % 2 == 0 and height % 2 == 0, "encoders need even dimensions"
        assert aspect_ratio_label(width, height) == preset.key


def test_the_required_resolutions_are_available() -> None:
    offered = {resolution for preset in ASPECT_PRESETS for resolution in preset.resolutions}
    for width, height in ((1280, 720), (1920, 1080), (1080, 1920), (1080, 1080), (1080, 1350), (2560, 1440), (3840, 2160)):
        assert (width, height) in offered, f"{width}x{height} must be selectable"


def test_unknown_aspect_ratios_are_labelled_honestly() -> None:
    assert aspect_ratio_label(1920, 1080) == "16:9"
    assert aspect_ratio_label(1000, 500) == "2:1"
    assert aspect_ratio_label(0, 0) == "custom"
    assert aspect_preset("16:9") is not None
    assert aspect_preset("3:2") is None


def test_fps_options_match_the_supported_list() -> None:
    assert FPS_OPTIONS == (24, 25, 30, 50, 60)


def test_filename_previews_are_windows_safe() -> None:
    from app.project.presets import INVALID_FILENAME_CHARACTERS

    for template, _example in (("{name}_{seq}", ""), ("{channel}_{name}_{date}", ""), ("bad<>:|?*", "")):
        name = preview_filename(template, name="My: Video?", channel="Chan/nel", seq=3)
        assert name.endswith(".mp4")
        assert not any(character in name for character in INVALID_FILENAME_CHARACTERS)


def test_filename_preview_uses_the_sequence_number() -> None:
    assert preview_filename("{name}_{seq}", name="Clip", seq=7).startswith("Clip_7")
