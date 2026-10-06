"""Image Studio core tests (Stage F, sections 71, 82).

These cover everything that works with **no model installed**: validation,
metadata, editing, saving, the library, history, upscaling, background removal
and the version graph.  Nothing here needs a backend, because on a fresh machine
none of the AI backends exists - and the studio still has to work.

Tests that would need a real diffusion model do not exist, because a test that
skips on the machine it was written for proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.image.capabilities import (IMAGE_TO_IMAGE, INPAINT, TEXT_TO_IMAGE,
                                    UPSCALE, ImageCapabilities)
from app.image.editor import ImageEditSession, apply_operations
from app.image.history import ImageHistory, PromptLibrary
from app.image.library import (ImageLibrary, LibraryQuery, ThumbnailCache,
                               file_checksum)
from app.image.metadata import (ImageMetadata, read_metadata, sidecar_path,
                                write_metadata)
from app.image.provider import (GenerationMode, GenerationRequest,
                                GenerationResult, GenerationState, ImageIssue,
                                ImageModel)
from app.image.saving import (atomic_write_bytes, convert_image, normalise_format,
                              save_image, unique_path)
from app.image.validation import (MAX_DIMENSION, validate_batch,
                                  validate_image_file, validate_prompt,
                                  validate_request, validate_resolution)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_image(path: Path, size=(64, 48), colour=(120, 60, 30), mode="RGB"):
    from PIL import Image

    image = Image.new(mode, size, colour if mode != "RGBA" else (*colour, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def make_capabilities(**overrides) -> ImageCapabilities:
    values = dict(text_to_image=True, image_to_image=True, seed_control=True,
                  max_batch=4, max_dimension=2048, min_dimension=64,
                  dimension_multiple=8)
    values.update(overrides)
    return ImageCapabilities(**values)


# --------------------------------------------------------------------------
# Capabilities (section 37)
# --------------------------------------------------------------------------

def test_capabilities_default_to_nothing():
    """A new adapter must opt in; it cannot advertise by accident."""
    caps = ImageCapabilities()
    assert caps.features() == []
    assert caps.supports(TEXT_TO_IMAGE) is False
    assert caps.supports(INPAINT) is False


def test_only_reported_features_are_listed():
    caps = ImageCapabilities(text_to_image=True, upscale=True)
    assert set(caps.features()) == {TEXT_TO_IMAGE, UPSCALE}
    assert IMAGE_TO_IMAGE in caps.missing_features()


def test_capabilities_survive_a_round_trip():
    caps = make_capabilities(inpaint=True, notes="local only")
    restored = ImageCapabilities.from_dict(caps.to_dict())
    assert restored.inpaint is True
    assert restored.max_batch == caps.max_batch
    assert restored.supports(TEXT_TO_IMAGE) is True


def test_unknown_capability_keys_are_ignored():
    """An older or newer record must not break loading."""
    restored = ImageCapabilities.from_dict(
        {"text_to_image": True, "warp_drive": True, "notes": "x"})
    assert restored.text_to_image is True
    assert not hasattr(restored, "warp_drive")


# --------------------------------------------------------------------------
# Prompt validation (section 43)
# --------------------------------------------------------------------------

def test_an_empty_prompt_is_refused_when_one_is_needed():
    issues = validate_prompt("   ")
    assert [issue.code for issue in issues] == ["PROMPT_EMPTY"]


def test_an_empty_prompt_is_allowed_when_not_needed():
    assert validate_prompt("", required=False) == []


def test_the_prompt_is_never_rewritten():
    """Validation reports; it must not tidy, trim or translate the text."""
    original = "  A  fox   in the SNOW!  "
    issues = validate_prompt(original)
    assert issues == []


def test_an_overlong_prompt_is_reported():
    issues = validate_prompt("x" * 5000)
    assert [issue.code for issue in issues] == ["PROMPT_TOO_LONG"]


def test_a_null_byte_in_a_prompt_is_reported():
    issues = validate_prompt("a\x00b")
    assert [issue.code for issue in issues] == ["PROMPT_INVALID_CHARACTERS"]


# --------------------------------------------------------------------------
# Resolution validation (sections 7, 8, 36)
# --------------------------------------------------------------------------

def test_a_requested_size_is_never_silently_changed():
    """The issue says the size is wrong; the request keeps the user's numbers."""
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                width=4096, height=4096)
    issues = validate_resolution(4096, 4096, make_capabilities(max_dimension=1024))
    assert [issue.code for issue in issues] == ["RESOLUTION_ABOVE_MODEL_MAXIMUM"]
    assert (request.width, request.height) == (4096, 4096)
    assert "not changed for you" in issues[0].what_to_do


def test_a_size_below_the_model_minimum_is_reported():
    """128 is a legal size, but this model's smallest side is 512."""
    issues = validate_resolution(128, 128, make_capabilities(min_dimension=512))
    assert "RESOLUTION_BELOW_MODEL_MINIMUM" in [i.code for i in issues]


def test_a_size_below_the_absolute_floor_is_out_of_range():
    """Anything under 64 pixels is refused before a model is even considered."""
    issues = validate_resolution(32, 32, make_capabilities(min_dimension=512))
    assert [i.code for i in issues] == ["RESOLUTION_OUT_OF_RANGE"]


def test_a_size_that_is_not_a_multiple_is_reported_with_a_suggestion():
    issues = validate_resolution(100, 100, make_capabilities(dimension_multiple=8))
    assert [issue.code for issue in issues] == ["RESOLUTION_NOT_A_MULTIPLE"]
    assert "96x96" in issues[0].what_to_do


def test_a_missing_size_is_reported_when_required():
    assert [i.code for i in validate_resolution(0, 0, make_capabilities())] == \
        ["RESOLUTION_MISSING"]


def test_an_absurd_size_is_out_of_range():
    issues = validate_resolution(MAX_DIMENSION + 1, 512, make_capabilities())
    assert [issue.code for issue in issues] == ["RESOLUTION_OUT_OF_RANGE"]


def test_a_valid_size_produces_no_issues():
    assert validate_resolution(1024, 1024,
                               make_capabilities(max_dimension=2048)) == []


def test_batch_beyond_the_backend_limit_is_refused():
    issues = validate_batch(99, make_capabilities(max_batch=4))
    assert [issue.code for issue in issues] == ["BATCH_TOO_LARGE"]
    assert "4" in issues[0].what_to_do


# --------------------------------------------------------------------------
# Request validation (section 36)
# --------------------------------------------------------------------------

def test_an_unsupported_mode_is_refused_with_the_alternatives():
    request = GenerationRequest(mode=GenerationMode.INPAINT, prompt="x",
                                width=512, height=512)
    issues = validate_request(request, make_capabilities(inpaint=False))
    unsupported = [i for i in issues if i.code == "FEATURE_UNSUPPORTED"]
    assert unsupported
    assert "not switched for you" in unsupported[0].what_to_do


def test_a_supported_mode_passes_validation():
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                width=512, height=512)
    assert [i for i in validate_request(request, make_capabilities())
            if i.severity == "error"] == []


def test_an_unknown_mode_is_refused():
    request = GenerationRequest(mode="teleport", prompt="x")
    issues = validate_request(request, make_capabilities())
    assert [issue.code for issue in issues] == ["MODE_UNKNOWN"]


def test_a_missing_source_image_is_refused_for_image_to_image():
    request = GenerationRequest(mode=GenerationMode.IMAGE_TO_IMAGE, prompt="x",
                                width=512, height=512, source_image="")
    issues = validate_request(request, make_capabilities())
    assert "SOURCE_IMAGE_INVALID" in [issue.code for issue in issues]


def test_a_negative_prompt_on_a_backend_without_one_only_warns(tmp_path):
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="x",
                                negative_prompt="blur", width=512, height=512)
    issues = validate_request(request, make_capabilities(negative_prompt=False))
    warn = [i for i in issues if i.code == "NEGATIVE_PROMPT_UNSUPPORTED"]
    assert warn and warn[0].severity == "warning"


def test_error_issues_are_listed_before_warnings():
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="",
                                negative_prompt="blur", width=512, height=512)
    issues = validate_request(request, make_capabilities(negative_prompt=False))
    severities = [issue.severity for issue in issues]
    assert severities == sorted(severities, key=lambda value: value != "error")


# --------------------------------------------------------------------------
# Image validation (section 46)
# --------------------------------------------------------------------------

def test_a_real_image_validates_and_reports_its_size(tmp_path):
    path = make_image(tmp_path / "ok.png", size=(120, 90))
    check = validate_image_file(path)
    assert check.ok
    assert (check.width, check.height) == (120, 90)
    assert check.format == "png"


def test_a_missing_file_is_reported_with_what_to_do(tmp_path):
    check = validate_image_file(tmp_path / "nope.png")
    assert not check.ok
    assert check.what_to_do


def test_an_empty_file_is_reported(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    check = validate_image_file(path)
    assert not check.ok
    assert "0 bytes" in check.error


def test_a_corrupt_image_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"this is not an image at all")
    check = validate_image_file(path)
    assert not check.ok
    assert "header" in check.error


def test_a_truncated_png_is_caught(tmp_path):
    """A file with a valid header but missing data must not pass."""
    path = make_image(tmp_path / "full.png", size=(64, 64))
    data = path.read_bytes()
    truncated = tmp_path / "cut.png"
    truncated.write_bytes(data[: len(data) // 3])
    check = validate_image_file(truncated)
    assert not check.ok


def test_a_folder_is_not_an_image(tmp_path):
    folder = tmp_path / "folder.png"
    folder.mkdir()
    check = validate_image_file(folder)
    assert not check.ok
    assert "folder" in check.error


def test_transparency_is_reported(tmp_path):
    path = make_image(tmp_path / "alpha.png", mode="RGBA")
    check = validate_image_file(path)
    assert check.ok and check.has_alpha


# --------------------------------------------------------------------------
# Saving (sections 19, 20, 21, 45)
# --------------------------------------------------------------------------

def test_saving_writes_a_file_and_verifies_it(tmp_path):
    from PIL import Image

    report = save_image(Image.new("RGB", (80, 60), (10, 20, 30)),
                        tmp_path / "out.png")
    assert report.ok and report.verified
    assert (report.width, report.height) == (80, 60)
    assert report.path.is_file()


def test_saving_never_overwrites_an_existing_file(tmp_path):
    from PIL import Image

    first = save_image(Image.new("RGB", (40, 40), (1, 2, 3)), tmp_path / "x.png")
    original = first.path.read_bytes()
    second = save_image(Image.new("RGB", (40, 40), (9, 9, 9)), tmp_path / "x.png")
    assert second.ok
    assert second.path != first.path
    assert first.path.read_bytes() == original, "the first file was modified"


def test_transparency_is_not_flattened_into_jpeg_quietly(tmp_path):
    from PIL import Image

    report = save_image(Image.new("RGBA", (40, 40), (255, 0, 0, 100)),
                        tmp_path / "a.jpg", requested_format="jpg")
    assert report.ok
    assert report.format == "png", "alpha must not be lost to a JPEG"
    assert report.format_changed, "the change must be reported"


def test_an_explicit_overwrite_is_honoured(tmp_path):
    from PIL import Image

    target = tmp_path / "same.png"
    save_image(Image.new("RGB", (20, 20), (1, 1, 1)), target)
    report = save_image(Image.new("RGB", (20, 20), (2, 2, 2)), target,
                        overwrite=True)
    assert report.ok and report.path == target


def test_a_half_written_file_is_never_left_behind(tmp_path):
    """No .part file survives a successful save."""
    from PIL import Image

    save_image(Image.new("RGB", (30, 30), (5, 5, 5)), tmp_path / "clean.png")
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".part"]
    assert leftovers == []


def test_atomic_write_replaces_the_target_in_one_step(tmp_path):
    target = tmp_path / "data.bin"
    target.write_bytes(b"old")
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert [p for p in tmp_path.iterdir() if p.suffix == ".part"] == []


def test_unique_path_steps_around_existing_files(tmp_path):
    (tmp_path / "img.png").write_bytes(b"x")
    (tmp_path / "img_1.png").write_bytes(b"x")
    assert unique_path(tmp_path, "img", ".png").name == "img_2.png"


def test_formats_are_normalised():
    assert normalise_format(".JPEG") == "jpg"
    assert normalise_format("PNG") == "png"


def test_converting_does_not_resample(tmp_path):
    """A format change keeps the pixel dimensions exactly."""
    from PIL import Image

    source = make_image(tmp_path / "src.png", size=(123, 77))
    report = convert_image(source, tmp_path / "dst.webp", requested_format="webp")
    assert report.ok
    assert (report.width, report.height) == (123, 77)
    with Image.open(report.path) as check:
        assert check.size == (123, 77)


def test_converting_a_corrupt_file_is_reported(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"nope")
    report = convert_image(bad, tmp_path / "out.png")
    assert not report.ok and report.what_to_do


# --------------------------------------------------------------------------
# Metadata (sections 22, 32, 33)
# --------------------------------------------------------------------------

def test_only_present_fields_are_stored():
    """An absent sampler must not become an empty string in the record."""
    record = ImageMetadata(prompt="a fox", model="m")
    data = record.to_dict()
    assert "prompt" in data and "model" in data
    assert "sampler" not in data
    assert "seed" not in data


def test_metadata_round_trips():
    record = ImageMetadata(prompt="a fox", seed=42, width=512, height=512,
                           model="m", backend="command", steps=20, guidance=7.5,
                           origin="variation", parent="/tmp/a.png")
    restored = ImageMetadata.from_dict(record.to_dict())
    assert restored.seed == 42
    assert restored.guidance == 7.5
    assert restored.origin == "variation"


def test_metadata_is_written_beside_the_image(tmp_path):
    path = make_image(tmp_path / "shot.png")
    write_metadata(path, ImageMetadata(prompt="a fox", seed=7))
    assert sidecar_path(path).is_file()
    back = read_metadata(path)
    assert back is not None and back.prompt == "a fox" and back.seed == 7


def test_a_png_carries_its_prompt_inside_the_file(tmp_path):
    """Copy the image alone and the prompt must still be there."""
    path = make_image(tmp_path / "embed.png")
    write_metadata(path, ImageMetadata(prompt="a blue whale", seed=99))
    sidecar_path(path).unlink()
    back = read_metadata(path)
    assert back is not None
    assert back.prompt == "a blue whale"
    assert back.seed == 99


def test_an_image_with_no_record_returns_nothing(tmp_path):
    assert read_metadata(make_image(tmp_path / "plain.png")) is None


def test_edit_history_is_recorded_in_order(tmp_path):
    record = ImageMetadata(prompt="x")
    record.add_edit("crop", {"left": 1})
    record.add_edit("rotate", {"angle": 90})
    data = record.to_dict()
    assert [step["operation"] for step in data["edits"]] == ["crop", "rotate"]
    restored = ImageMetadata.from_dict(data)
    assert [step.operation for step in restored.edits] == ["crop", "rotate"]


def test_metadata_from_a_generation_records_the_seed_actually_used():
    request = GenerationRequest(mode=GenerationMode.TEXT_TO_IMAGE, prompt="a fox",
                                seed=1234, width=64, height=64)
    result = GenerationResult(ok=True, state=GenerationState.COMPLETED,
                              paths=["/tmp/a.png"], seeds=[1234], model="m",
                              backend="command", width=64, height=64,
                              output_format="png", seconds=2.5)
    record = ImageMetadata.from_generation(request, result)
    assert record.seed == 1234
    assert record.generation_seconds == 2.5
    assert record.model == "m"


# --------------------------------------------------------------------------
# Editing (sections 16, 17, 53)
# --------------------------------------------------------------------------

def test_an_edit_leaves_the_original_alone(tmp_path):
    source = make_image(tmp_path / "src.png", size=(200, 100))
    before = source.read_bytes()
    session = ImageEditSession(source=source)
    session.add("crop", left=10, top=10, right=110, bottom=90)
    report = session.save_as(tmp_path / "edited.png")
    assert report.ok
    assert source.read_bytes() == before, "the original was modified"


def test_undo_and_redo_walk_the_stack(tmp_path):
    session = ImageEditSession(source=make_image(tmp_path / "s.png"))
    session.add("brightness", factor=1.2)
    session.add("contrast", factor=1.3)
    assert len(session.operations) == 2
    assert session.undo().operation == "contrast"
    assert session.can_redo
    assert session.redo().operation == "contrast"
    assert len(session.operations) == 2


def test_a_new_edit_after_an_undo_clears_the_redo_stack(tmp_path):
    session = ImageEditSession(source=make_image(tmp_path / "s.png"))
    session.add("brightness", factor=1.2)
    session.undo()
    session.add("grayscale")
    assert not session.can_redo


def test_reset_empties_the_stack(tmp_path):
    session = ImageEditSession(source=make_image(tmp_path / "s.png"))
    session.add("blur", radius=2.0)
    session.reset()
    assert session.operations == [] and not session.can_redo


def test_every_offered_operation_really_runs(tmp_path):
    """A menu entry that cannot execute would be a dead control."""
    from app.image.editor import OPERATIONS

    source = make_image(tmp_path / "ops.png", size=(120, 80))
    mask = make_image(tmp_path / "mask.png", size=(120, 80), colour=(255, 255, 255))

    parameters = {
        "crop": {"left": 10, "top": 5, "right": 110, "bottom": 75},
        "resize": {"width": 60, "height": 40},
        "rotate": {"angle": 90.0},
        "brightness": {"factor": 1.1},
        "contrast": {"factor": 1.1},
        "saturation": {"factor": 1.1},
        "exposure": {"factor": 1.1},
        "sharpen": {"factor": 1.5},
        "blur": {"radius": 1.5},
        "opacity": {"opacity": 0.5},
        "canvas": {"width": 140, "height": 100},
        "rounded_corners": {"radius": 8},
        "temperature": {"shift": 0.2},
        "mask": {"path": str(mask), "feather": 2.0},
    }
    for operation in OPERATIONS:
        session = ImageEditSession(source=source)
        session.add(operation, **parameters.get(operation, {}))
        image = session.preview()
        assert image is not None, f"{operation} produced nothing"
        report = session.save_as(tmp_path / f"{operation}.png")
        assert report.ok, f"{operation} failed: {report.error}"


def test_an_operation_with_bad_parameters_is_reported(tmp_path):
    session = ImageEditSession(source=make_image(tmp_path / "s.png"))
    session.add("resize", width=0, height=0)
    report = session.save_as(tmp_path / "bad.png")
    assert not report.ok and report.error


def test_an_unknown_operation_is_refused_immediately(tmp_path):
    session = ImageEditSession(source=make_image(tmp_path / "s.png"))
    with pytest.raises(ValueError):
        session.add("teleport")


def test_overwriting_the_original_requires_confirmation(tmp_path):
    from app.image.editor import apply_to_source

    source = make_image(tmp_path / "keep.png")
    before = source.read_bytes()
    session = ImageEditSession(source=source)
    session.add("grayscale")
    report = apply_to_source(session, confirmed=False)
    assert not report.ok
    assert source.read_bytes() == before


def test_a_confirmed_overwrite_keeps_a_backup(tmp_path):
    from app.image.editor import apply_to_source

    source = make_image(tmp_path / "keep.png")
    session = ImageEditSession(source=source)
    session.add("grayscale")
    report = apply_to_source(session, confirmed=True)
    assert report.ok
    assert source.with_suffix(source.suffix + ".bak").is_file()


def test_alpha_survives_an_edit(tmp_path):
    from PIL import Image

    source = make_image(tmp_path / "a.png", size=(50, 50), mode="RGBA")
    session = ImageEditSession(source=source)
    session.add("brightness", factor=1.2)
    report = session.save_as(tmp_path / "out.png")
    assert report.ok
    with Image.open(report.path) as image:
        assert "A" in image.mode


def test_operations_can_be_replayed_from_plain_data(tmp_path):
    """The edit list is data, so it can be stored and replayed."""
    from PIL import Image

    source = make_image(tmp_path / "s.png", size=(80, 60))
    operations = [{"operation": "grayscale"},
                  {"operation": "resize", "params": {"width": 40, "height": 30}}]
    with Image.open(source) as image:
        result = apply_operations(image, operations)
    assert result.size == (40, 30)
    assert result.mode in ("L", "RGB", "RGBA")


# --------------------------------------------------------------------------
# Upscaling (section 15)
# --------------------------------------------------------------------------

def test_standard_resize_enlarges_to_the_exact_size(tmp_path):
    from app.image.upscale import upscale

    source = make_image(tmp_path / "s.png", size=(100, 50))
    result = upscale(source, tmp_path / "big.png", scale=2.0)
    assert result.ok
    assert result.output_size == (200, 100)
    assert result.method == "standard"
    assert result.method_label == "Standard Resize"


def test_an_ai_upscale_with_no_model_is_refused_honestly(tmp_path):
    """No fake result, and no pretending the resize was AI."""
    from app.image.upscale import detect_upscaler, upscale

    if detect_upscaler().ai_available:
        pytest.skip("an AI upscaler is installed on this machine")
    source = make_image(tmp_path / "s.png", size=(60, 60))
    result = upscale(source, tmp_path / "ai.png", scale=2.0, method="ai")
    assert not result.ok
    assert "none is installed" in result.error.lower()
    assert not (tmp_path / "ai.png").exists(), "a file was written anyway"
    assert result.what_to_do


def test_a_permitted_fallback_says_that_it_fell_back(tmp_path):
    from app.image.upscale import detect_upscaler, upscale

    if detect_upscaler().ai_available:
        pytest.skip("an AI upscaler is installed on this machine")
    source = make_image(tmp_path / "s.png", size=(40, 40))
    result = upscale(source, tmp_path / "fb.png", scale=2.0, method="ai",
                     allow_fallback=True)
    assert result.ok
    assert result.method == "standard"
    assert result.downgraded, "the fallback must be visible to the user"
    assert "standard resize was used" in result.describe()


def test_automatic_never_claims_ai_without_a_model(tmp_path):
    from app.image.upscale import detect_upscaler, upscale

    if detect_upscaler().ai_available:
        pytest.skip("an AI upscaler is installed on this machine")
    source = make_image(tmp_path / "s.png", size=(30, 30))
    result = upscale(source, tmp_path / "auto.png", scale=2.0, method="auto")
    assert result.ok and result.method == "standard"


def test_an_upscale_never_overwrites_its_source(tmp_path):
    from app.image.upscale import upscale

    source = make_image(tmp_path / "s.png", size=(50, 50))
    before = source.read_bytes()
    upscale(source, tmp_path / "s.png", scale=2.0)
    assert source.read_bytes() == before


def test_a_scale_outside_the_range_is_refused(tmp_path):
    from app.image.upscale import upscale

    source = make_image(tmp_path / "s.png")
    result = upscale(source, tmp_path / "x.png", scale=99.0)
    assert not result.ok and "outside the supported range" in result.error


# --------------------------------------------------------------------------
# Background removal (section 18)
# --------------------------------------------------------------------------

def test_background_removal_reports_an_honest_state():
    from app.image.background_removal import (STATES, detect_background_removal)

    status = detect_background_removal()
    assert status.state in STATES
    assert status.reason


def test_no_cut_out_is_invented_when_no_model_exists(tmp_path):
    from app.image.background_removal import (detect_background_removal,
                                              remove_background)

    if detect_background_removal().available:
        pytest.skip("a background-removal model is installed on this machine")
    source = make_image(tmp_path / "s.png")
    report = remove_background(source, tmp_path / "cut.png")
    assert not report.ok
    assert not (tmp_path / "cut.png").exists()
    assert "mask" in report.what_to_do.lower()


def test_a_manual_mask_cuts_a_real_hole(tmp_path):
    """The fallback is a real tool, not a placeholder."""
    from PIL import Image

    from app.image.background_removal import apply_mask

    source = Image.new("RGBA", (40, 40), (200, 50, 50, 255))
    # A black mask keeps nothing; a white one keeps everything.
    black = Image.new("L", (40, 40), 0)
    white = Image.new("L", (40, 40), 255)
    cut = apply_mask(source, black)
    kept = apply_mask(source, white)
    assert cut.getchannel("A").getextrema() == (0, 0)
    assert kept.getchannel("A").getextrema() == (255, 255)


def test_masking_twice_does_not_bring_invisible_pixels_back():
    from PIL import Image

    from app.image.background_removal import apply_mask

    source = Image.new("RGBA", (20, 20), (10, 10, 10, 0))
    white = Image.new("L", (20, 20), 255)
    assert apply_mask(source, white).getchannel("A").getextrema() == (0, 0)


# --------------------------------------------------------------------------
# Library (sections 55, 56, 57, 58, 59)
# --------------------------------------------------------------------------

def test_a_scan_indexes_every_image(tmp_path):
    for index in range(5):
        make_image(tmp_path / f"lib_{index}.png")
    (tmp_path / "notes.txt").write_text("not an image")
    library = ImageLibrary(tmp_path)
    assert library.scan() == 5
    assert library.query().total == 5


def test_the_index_reloads_without_rescanning(tmp_path):
    make_image(tmp_path / "one.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    again = ImageLibrary(tmp_path)
    again._load_index()
    assert again.query().total == 1


def test_metadata_is_read_during_the_scan(tmp_path):
    path = make_image(tmp_path / "shot.png")
    write_metadata(path, ImageMetadata(prompt="a red fox", seed=11, model="m"))
    library = ImageLibrary(tmp_path)
    library.scan()
    entry = library.query().entries[0]
    assert entry.prompt == "a red fox" and entry.seed == 11


def test_search_finds_by_prompt_and_by_name(tmp_path):
    path = make_image(tmp_path / "sunset.png")
    write_metadata(path, ImageMetadata(prompt="a calm lake"))
    make_image(tmp_path / "other.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    assert library.query(LibraryQuery(text="lake")).total == 1
    assert library.query(LibraryQuery(text="sunset")).total == 1
    assert library.query(LibraryQuery(text="nothing")).total == 0


def test_tags_can_be_added_and_searched(tmp_path):
    path = make_image(tmp_path / "tagged.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    assert library.add_tag(path, "background")
    assert library.query(LibraryQuery(tags=["background"])).total == 1
    assert library.query(LibraryQuery(tags=["missing"])).total == 0


def test_tags_are_written_to_the_image_record(tmp_path):
    path = make_image(tmp_path / "tagged.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    library.add_tag(path, "logo")
    record = read_metadata(path)
    assert record is not None and "logo" in record.tags


def test_collections_are_counted(tmp_path):
    first = make_image(tmp_path / "a.png")
    second = make_image(tmp_path / "b.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    library.set_collection(first, "Backgrounds")
    library.set_collection(second, "Backgrounds")
    assert library.collections()["Backgrounds"] == 2


def test_paging_never_returns_the_whole_library(tmp_path):
    for index in range(12):
        make_image(tmp_path / f"page_{index}.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    page = library.query(LibraryQuery(limit=5))
    assert len(page.entries) == 5
    assert page.total == 12
    assert page.has_more
    second = library.query(LibraryQuery(limit=5, offset=10))
    assert len(second.entries) == 2
    assert not second.has_more


def test_identical_files_are_reported_as_duplicates(tmp_path):
    first = make_image(tmp_path / "dup.png")
    (tmp_path / "dup_copy.png").write_bytes(first.read_bytes())
    library = ImageLibrary(tmp_path)
    library.scan()
    groups = library.duplicates()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_duplicates_are_never_deleted_automatically(tmp_path):
    first = make_image(tmp_path / "dup.png")
    (tmp_path / "dup_copy.png").write_bytes(first.read_bytes())
    library = ImageLibrary(tmp_path)
    library.scan()
    library.duplicates()
    assert first.is_file()
    assert (tmp_path / "dup_copy.png").is_file()


def test_removing_from_the_library_can_keep_the_file(tmp_path):
    path = make_image(tmp_path / "keep.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    assert library.remove(path, delete_file=False)
    assert path.is_file()


def test_removing_with_delete_really_removes_the_file(tmp_path):
    path = make_image(tmp_path / "gone.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    assert library.remove(path, delete_file=True)
    assert not path.exists()


def test_a_deleted_file_is_marked_missing_not_crashed(tmp_path):
    path = make_image(tmp_path / "vanish.png")
    library = ImageLibrary(tmp_path)
    library.scan()
    path.unlink()
    library.scan()
    assert library.query().entries[0].missing


def test_a_checksum_is_stable(tmp_path):
    path = make_image(tmp_path / "sum.png")
    assert file_checksum(path) == file_checksum(path)
    assert len(file_checksum(path)) == 64


def test_a_corrupt_file_does_not_stop_the_scan(tmp_path):
    make_image(tmp_path / "good.png")
    (tmp_path / "bad.png").write_bytes(b"rubbish")
    library = ImageLibrary(tmp_path)
    assert library.scan() == 2
    entry = [item for item in library.query().entries if item.name == "bad"][0]
    assert entry.width == 0, "a broken file should be indexed without a size"


# --------------------------------------------------------------------------
# Thumbnails (sections 55, 56)
# --------------------------------------------------------------------------

def test_a_thumbnail_is_created_and_cached(tmp_path):
    source = make_image(tmp_path / "big.png", size=(1200, 900))
    cache = ThumbnailCache(tmp_path / "thumbs", size=128)
    first = cache.get(source)
    assert first is not None and first.is_file()
    second = cache.get(source)
    assert second == first, "the cached thumbnail should be reused"
    from PIL import Image

    with Image.open(first) as image:
        assert max(image.size) <= 128


def test_changing_the_image_invalidates_its_thumbnail(tmp_path):
    """A stale thumbnail would show the wrong picture."""
    import time

    source = make_image(tmp_path / "swap.png", size=(100, 100),
                        colour=(255, 0, 0))
    cache = ThumbnailCache(tmp_path / "thumbs", size=64)
    assert cache.get(source) is not None
    first_key = cache.key_for(source)

    time.sleep(0.01)
    make_image(source, size=(100, 100), colour=(0, 0, 255))
    assert cache.key_for(source) != first_key, "the key ignored the new file"


def test_a_corrupt_image_has_no_thumbnail(tmp_path):
    path = tmp_path / "bad.png"
    path.write_bytes(b"not an image")
    cache = ThumbnailCache(tmp_path / "thumbs", size=64)
    assert cache.get(path) is None


def test_a_thumbnail_of_a_transparent_image_is_visible(tmp_path):
    """A transparent thumbnail would be invisible in a light interface."""
    from PIL import Image

    source = tmp_path / "clear.png"
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(source)
    cache = ThumbnailCache(tmp_path / "thumbs", size=32)
    thumb = cache.get(source)
    assert thumb is not None
    with Image.open(thumb) as image:
        assert image.mode == "RGB"


def test_clearing_the_cache_removes_the_files(tmp_path):
    source = make_image(tmp_path / "a.png", size=(200, 200))
    cache = ThumbnailCache(tmp_path / "thumbs", size=64)
    cache.get(source)
    assert cache.clear() >= 1


# --------------------------------------------------------------------------
# History and prompts (sections 9, 23, 24)
# --------------------------------------------------------------------------

def test_a_result_is_recorded_with_its_seed(tmp_path):
    history = ImageHistory(tmp_path / "history.json")
    request = GenerationRequest(prompt="a fox", seed=5, mode="text_to_image")
    result = GenerationResult(ok=True, state=GenerationState.COMPLETED,
                              paths=["/tmp/a.png"], seeds=[5], model="m",
                              backend="b", width=64, height=64)
    entries = history.add_result(result, request)
    assert len(entries) == 1
    assert entries[0].seed == 5
    assert entries[0].prompt == "a fox"


def test_a_replay_uses_the_stored_settings(tmp_path):
    history = ImageHistory(tmp_path / "history.json")
    request = GenerationRequest(prompt="a fox", seed=5, width=64, height=64,
                                mode="text_to_image", model="m", steps=12)
    result = GenerationResult(ok=True, paths=["/tmp/a.png"], seeds=[5],
                              model="m", backend="b")
    entry = history.add_result(result, request)[0]
    replay = GenerationRequest.from_dict(entry.request)
    assert replay.seed == 5
    assert replay.steps == 12
    assert replay.prompt == "a fox"


def test_a_batch_is_recorded_one_entry_per_image(tmp_path):
    history = ImageHistory(tmp_path / "history.json")
    request = GenerationRequest(prompt="x", batch=3, mode="text_to_image")
    result = GenerationResult(ok=True, paths=["/tmp/a.png", "/tmp/b.png",
                                              "/tmp/c.png"],
                              seeds=[1, 2, 3], model="m", backend="b")
    entries = history.add_result(result, request, batch_id="batch1")
    assert len(entries) == 3
    assert [entry.batch_index for entry in entries] == [1, 2, 3]
    assert all(entry.batch_id == "batch1" for entry in entries)


def test_a_failure_is_recorded_with_its_reason(tmp_path):
    history = ImageHistory(tmp_path / "history.json")
    request = GenerationRequest(prompt="x", mode="text_to_image")
    result = GenerationResult(ok=False, error="No model is installed.")
    entry = history.add_failure(request, result)
    assert entry.status == "FAILED"
    assert entry.error == "No model is installed."


def test_find_by_seed_returns_every_generation_that_used_it(tmp_path):
    history = ImageHistory(tmp_path / "history.json")
    request = GenerationRequest(prompt="x", seed=7, mode="text_to_image")
    result = GenerationResult(ok=True, paths=["/tmp/a.png"], seeds=[7])
    history.add_result(result, request)
    assert len(history.same_seed(7)) == 1
    assert history.same_seed(8) == []


def test_history_survives_a_reload(tmp_path):
    path = tmp_path / "history.json"
    history = ImageHistory(path)
    request = GenerationRequest(prompt="persist me", mode="text_to_image")
    history.add_result(GenerationResult(ok=True, paths=["/tmp/a.png"], seeds=[1]),
                       request)
    assert ImageHistory(path).all()[0].prompt == "persist me"


def test_prompts_are_remembered_and_counted(tmp_path):
    prompts = PromptLibrary(tmp_path / "prompts.json")
    prompts.record_use("a fox")
    entry = prompts.record_use("a fox")
    assert entry.used == 2
    assert len(prompts.all()) == 1, "the same prompt must not be stored twice"


def test_an_empty_prompt_is_not_recorded(tmp_path):
    prompts = PromptLibrary(tmp_path / "prompts.json")
    assert prompts.record_use("   ") is None
    assert prompts.all() == []


def test_a_favourite_sorts_first(tmp_path):
    prompts = PromptLibrary(tmp_path / "prompts.json")
    prompts.record_use("first")
    prompts.record_use("second")
    prompts.save("first", name="Keep me")
    assert prompts.recent()[0].favourite


def test_a_prompt_is_never_changed_by_recording_it(tmp_path):
    prompts = PromptLibrary(tmp_path / "prompts.json")
    text = "  A  FOX in snow  "
    prompts.record_use(text)
    assert prompts.all()[0].text == text


def test_editing_a_prompt_is_explicit(tmp_path):
    prompts = PromptLibrary(tmp_path / "prompts.json")
    entry = prompts.record_use("old text")
    assert prompts.edit(entry.id, "new text")
    assert prompts.find(entry.id).text == "new text"


# --------------------------------------------------------------------------
# Version graph (sections 32, 33)
# --------------------------------------------------------------------------

def test_a_variation_records_its_parent(tmp_path):
    source = make_image(tmp_path / "original.png")
    child = make_image(tmp_path / "original_variation_1.png")
    write_metadata(child, ImageMetadata(origin="variation", parent=str(source)))
    from app.image.variants import VersionGraph

    graph = VersionGraph([source, child])
    roots = graph.roots()
    assert [node.name for node in roots] == ["original.png"]
    assert [node.name for node in graph.children_of(source)] == \
        ["original_variation_1.png"]


def test_a_lineage_can_be_walked_to_the_original(tmp_path):
    original = make_image(tmp_path / "a.png")
    variation = make_image(tmp_path / "b.png")
    edited = make_image(tmp_path / "c.png")
    write_metadata(variation, ImageMetadata(origin="variation", parent=str(original)))
    write_metadata(edited, ImageMetadata(origin="edit", parent=str(variation)))
    from app.image.variants import VersionGraph

    graph = VersionGraph([original, variation, edited])
    chain = [node.name for node in graph.lineage(edited)]
    assert chain == ["a.png", "b.png", "c.png"]
    assert graph.nodes[str(edited)].depth == 2


def test_a_missing_parent_is_reported_not_hidden(tmp_path):
    orphan = make_image(tmp_path / "orphan.png")
    write_metadata(orphan, ImageMetadata(origin="variation",
                                         parent=str(tmp_path / "gone.png")))
    from app.image.variants import VersionGraph

    graph = VersionGraph([orphan])
    assert len(graph.orphans()) == 1
    assert "not" in graph.describe()


def test_next_name_never_collides_with_a_sibling(tmp_path):
    original = make_image(tmp_path / "photo.png")
    make_image(tmp_path / "photo_variation_1.png")
    from app.image.variants import VersionGraph

    graph = VersionGraph([original, tmp_path / "photo_variation_1.png"])
    assert graph.next_name(original, "variation") == "photo_variation_2.png"


def test_an_image_with_no_parent_is_a_root(tmp_path):
    from app.image.variants import VersionGraph

    graph = VersionGraph([make_image(tmp_path / "solo.png")])
    assert len(graph.roots()) == 1
    assert graph.nodes[str(tmp_path / "solo.png")].is_root


# --------------------------------------------------------------------------
# Result and issue shapes
# --------------------------------------------------------------------------

def test_a_result_is_only_successful_with_a_file():
    result = GenerationResult(ok=True, paths=["/tmp/a.png"], seeds=[1])
    assert result.summary()
    failed = GenerationResult(ok=False, error="nothing happened")
    assert failed.summary() == "nothing happened"


def test_a_cancelled_result_says_so():
    result = GenerationResult(cancelled=True, state=GenerationState.CANCELLED)
    assert result.summary() == "Generation cancelled."


def test_a_failure_describes_what_to_do():
    result = GenerationResult(ok=False, error="broken", why="because",
                              what_to_do="try this")
    text = result.describe()
    assert "what happened: broken" in text
    assert "why: because" in text
    assert "what to do: try this" in text


def test_an_issue_carries_a_code_and_a_fix():
    issue = ImageIssue("X", "it broke", "do this")
    assert issue.to_dict()["code"] == "X"
    assert issue.to_dict()["what_to_do"] == "do this"


def test_a_model_reports_its_size_readably():
    model = ImageModel(name="m", size_bytes=5 * 1024 ** 3)
    assert "GiB" in model.size_label
    assert ImageModel(name="m").size_label == "size unknown"


def test_generation_states_are_the_documented_ones():
    from app.image.provider import STATES

    assert STATES == ("QUEUED", "LOADING MODEL", "GENERATING", "PROCESSING",
                      "SAVING", "COMPLETED", "FAILED", "CANCELLED")
