"""Project validation (directive sections 14, 27 and 36-9).

Two things matter here: **every** problem is reported in one pass (not one error
at a time), and impossible encoder combinations are caught before anything is
rendered.
"""

from __future__ import annotations

from app.project.model import AssetSpec, SceneSpec, SoundEffect, build_project
from app.project.presets import resolve_quality
from app.project.validation import (
    LEVEL_ERROR,
    LEVEL_WARNING,
    validate_for_render,
    validate_project,
)


def test_a_well_formed_project_has_no_errors() -> None:
    report = validate_project(build_project("Clean", quality=resolve_quality("high")))

    assert report.ok
    assert report.errors == []
    assert report.summary() == "No issues found."


def test_every_problem_is_collected_in_one_pass(tmp_path) -> None:
    """The user must see '7 issues found', not fix them one dialog at a time."""
    project = build_project("", quality=resolve_quality("high"))
    project.project.id = "bad id with spaces"
    project.format.fps = 90
    project.format.codec = "vp9_cpu"          # not allowed inside mp4
    project.format.crf = 99
    project.voice.engine = "elevenlabs"
    project.export.filename_template = "name/with/slash"
    project.add_asset(AssetSpec(id="a1", name="gone.png", kind="image", path="assets/gone.png"))

    report = validate_project(project, project_dir=tmp_path, check_files=True)

    assert not report.ok
    codes = {issue.code for issue in report.errors}
    assert {"NAME_EMPTY", "ID_INVALID", "FPS", "CODEC_CONTAINER", "CRF", "ENGINE", "TEMPLATE_CHARACTERS"} <= codes
    assert len(report.errors) >= 7
    assert "issue(s) found" in report.summary()


def test_invalid_encoder_combinations_are_caught_before_render() -> None:
    """Directive 36-9: impossible settings must be reported, never attempted."""
    cases = [
        ("odd width", lambda project: setattr(project.format, "width", 1921), "ODD_WIDTH"),
        ("odd height", lambda project: setattr(project.format, "height", 1081), "ODD_HEIGHT"),
        ("unsupported fps", lambda project: setattr(project.format, "fps", 45), "FPS"),
        ("codec/container mismatch", lambda project: setattr(project.format, "codec", "vp9_cpu"), "CODEC_CONTAINER"),
        ("crf out of range", lambda project: setattr(project.format, "crf", 200), "CRF"),
        ("10-bit into h264", lambda project: setattr(project.format, "pixel_format", "yuv420p10le"), "PIXEL_FORMAT"),
        ("opus into mp4", lambda project: setattr(project.format, "audio_codec", "opus"), "AUDIO_CODEC"),
        ("weird sample rate", lambda project: setattr(project.format, "sample_rate", 12345), "SAMPLE_RATE"),
        ("unknown container", lambda project: setattr(project.format, "container", "avi"), "CONTAINER"),
        ("unknown encoder preset", lambda project: setattr(project.format, "encoder_preset", "instant"), "ENCODER_PRESET"),
    ]

    for label, change, expected_code in cases:
        project = build_project("Combos")
        change(project)
        report = validate_project(project)
        assert not report.ok, f"{label} should have been rejected"
        assert expected_code in {issue.code for issue in report.errors}, f"{label} reported: {report.to_text()}"


def test_webm_requires_vp9_and_opus() -> None:
    project = build_project("WebM")
    project.format.container = "webm"

    report = validate_project(project)
    codes = {issue.code for issue in report.errors}
    assert "CODEC_CONTAINER" in codes
    assert "AUDIO_CODEC" in codes

    project.format.codec = "vp9_cpu"
    project.format.audio_codec = "opus"
    project.format.crf = 32
    project.export.container = "webm"
    project.export.codec = "vp9_cpu"
    project.export.audio_codec = "opus"
    project.export.crf = 32

    assert validate_project(project).ok


def test_heavy_but_valid_settings_warn_instead_of_failing() -> None:
    """4K/60/Ultra is allowed - the user is told it will be slow, not blocked."""
    project = build_project("Heavy", quality=resolve_quality("ultra"))
    project.format.width, project.format.height, project.format.fps = 3840, 2160, 60

    report = validate_project(project)

    assert report.ok, report.to_text()
    assert report.warnings, "the user must be warned about the encode cost"
    codes = {issue.code for issue in report.warnings}
    assert "SLOW_4K" in codes and "HEAVY_4K60" in codes
    assert all(issue.level != LEVEL_ERROR for issue in report.issues)


def test_quality_presets_are_never_silently_downgraded() -> None:
    project = build_project("Ultra", quality=resolve_quality("ultra"))

    report = validate_project(project)

    assert report.ok
    assert project.format.crf == 15
    assert project.format.encoder_preset == "veryslow"


def test_project_relative_paths_are_required_for_portability() -> None:
    project = build_project("Portable")
    project.add_asset(AssetSpec(id="escape", name="up.png", kind="image", path="../../outside.png"))
    project.add_asset(AssetSpec(id="abs", name="win.png", kind="image", path="C:/Users/me/win.png"))

    report = validate_project(project)
    codes = {issue.code: issue for issue in report.issues}

    assert codes["PATH_ESCAPES"].level == LEVEL_ERROR
    assert codes["PATH_ESCAPES"].field == "assets[0].path"
    assert codes["PATH_ABSOLUTE"].level == LEVEL_WARNING
    assert codes["PATH_ABSOLUTE"].field == "assets[1].path"
    assert "another PC" in codes["PATH_ABSOLUTE"].message
    assert "assets" in codes["PATH_ABSOLUTE"].fix


def test_a_missing_asset_is_reported_with_its_location(tmp_path) -> None:
    project = build_project("Missing")
    project.add_asset(AssetSpec(id="a1", name="logo.png", kind="logo", path="assets/logo.png"))

    report = validate_project(project, project_dir=tmp_path, check_files=True)

    issue = next(issue for issue in report.issues if issue.code == "FILE_MISSING")
    assert "assets" in issue.message
    assert "Relink" in issue.fix


def test_validation_can_run_without_touching_the_disk() -> None:
    """The dirty-state check runs often, so it must not need file access."""
    project = build_project("No disk")
    project.add_asset(AssetSpec(id="a1", name="logo.png", kind="logo", path="assets/logo.png"))

    report = validate_project(project)  # no project_dir, no check_files

    assert report.ok


def test_scene_problems_are_reported_per_scene() -> None:
    project = build_project("Scenes")
    project.add_scene(SceneSpec(id="dup", name="One", type="title", duration=2.0))
    project.add_scene(SceneSpec(id="dup", name="Two", type="unknown-type", duration=-1.0))

    report = validate_project(project)
    codes = {issue.code for issue in report.errors}

    assert {"SCENE_ID_DUPLICATE", "SCENE_TYPE", "SCENE_DURATION"} <= codes
    assert any(issue.field.startswith("scenes[1]") for issue in report.issues)


def test_elements_must_stay_inside_the_frame() -> None:
    from app.project.model import ElementSpec

    project = build_project("Elements")
    scene = project.add_scene(SceneSpec(id="s1", name="One"))
    scene.elements.append(ElementSpec(id="el-1", kind="text", text="Off screen", position={"x": 1.7, "y": 0.5}))

    report = validate_project(project)

    assert "POSITION" in {issue.code for issue in report.errors}


def test_an_element_cannot_reference_an_unknown_asset() -> None:
    from app.project.model import ElementSpec

    project = build_project("Refs")
    scene = project.add_scene(SceneSpec(id="s1", name="One"))
    scene.elements.append(ElementSpec(id="el-1", kind="image", asset_id="ghost"))

    report = validate_project(project)

    assert "ASSET_MISSING_REF" in {issue.code for issue in report.errors}


def test_output_filenames_must_not_collide() -> None:
    project = build_project("Export")
    project.export.filename_template = "{name}_final"
    project.export.overwrite_policy = "overwrite"
    project.export.next_sequence_number = 0
    project.export.output_dir = "assets/videos"

    report = validate_project(project)
    codes = {issue.code for issue in report.issues}

    assert "TEMPLATE_NO_SEQ" in codes
    assert "OVERWRITE_DANGEROUS" in codes
    assert "SEQUENCE" in codes
    assert "OUTPUT_DIR_COLLISION" in codes


def test_duplicate_sound_effect_ids_are_caught() -> None:
    project = build_project("SFX")
    project.audio.sfx = [SoundEffect(id="click", path="assets/a.wav"), SoundEffect(id="click", path="assets/b.wav")]

    report = validate_project(project)

    assert "SFX_ID_DUPLICATE" in {issue.code for issue in report.errors}


def test_render_validation_is_stricter_than_editing_validation(tmp_path) -> None:
    project = build_project("Render")
    project.add_asset(AssetSpec(id="a1", name="bg.png", kind="image", path="assets/bg.png"))

    editing = validate_project(project, project_dir=tmp_path, check_files=True)
    assert editing.ok, editing.to_text()

    rendering = validate_for_render(project, project_dir=tmp_path)
    codes = {issue.code: issue.level for issue in rendering.issues}
    assert codes["FILE_MISSING"] == LEVEL_ERROR
    assert codes["NO_SCENES"] == LEVEL_ERROR
    assert not rendering.ok


def test_the_report_groups_issues_by_section() -> None:
    project = build_project("Groups")
    project.format.fps = 90
    project.voice.engine = "other"

    report = validate_project(project)

    assert report.issues_for("format")
    assert report.issues_for("voice")
    assert report.issues_for("nothing") == []
    assert "Validation:" in report.to_text()
    assert report.headline()
