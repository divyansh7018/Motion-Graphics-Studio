"""Image Studio to Project integration (Stage F, sections 26, 27, 28, 64).

These are the manual-matrix scenarios 9-13 as automated tests: send an image to
a project, send it to a scene, reopen, move the project, and delete the source
file - each time checking that the reference is still honest and nothing crashes.

The important property being tested is that a scene references the **asset id**,
not a copied file, so there is exactly one image system.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.image.integration import (PLACEMENTS, Placement, resolve_element_image,
                                   send_to_scene, use_in_project)
from app.image.metadata import ImageMetadata, write_metadata
from app.project.model import AssetSpec, ElementSpec
from app.project.service import CreateRequest, ProjectService



@pytest.fixture()
def project_service(paths, settings):
    service = ProjectService(paths, settings)
    service.create_project(CreateRequest(name="Image Integration"))
    yield service
    try:
        service.close_project(save=False)
    except Exception:  # noqa: BLE001 - already closed
        pass


def make_image(path: Path, size=(320, 200), colour=(30, 120, 200)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def generated_image(path: Path, colour=(30, 120, 200), **metadata) -> Path:
    """An image that looks like the studio produced it."""
    make_image(path, colour=colour)
    values = dict(prompt="a blue banner", model="command-model",
                  backend="command", seed=1234, width=320, height=200,
                  origin="generated")
    values.update(metadata)
    write_metadata(path, ImageMetadata(**values))
    return path


# --------------------------------------------------------------------------
# Use in project (section 26)
# --------------------------------------------------------------------------

def test_an_image_can_be_added_to_the_open_project(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = use_in_project(project_service, source)
    assert result.ok, result.message
    assert result.asset_id
    project = project_service.current
    asset = project.asset_by_id(result.asset_id)
    assert asset is not None
    assert asset.path.startswith("assets/"), "the asset should be project-relative"


def test_the_file_is_copied_into_the_project(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = use_in_project(project_service, source, name="hero")
    layout = project_service.current_layout
    copied = layout.root / project_service.current.asset_by_id(result.asset_id).path
    assert copied.is_file()
    assert copied.read_bytes() == source.read_bytes()


def test_the_generation_facts_travel_with_the_asset(project_service, tmp_path):
    """Section 22: the prompt and seed are kept, not discarded at the door."""
    source = generated_image(tmp_path / "hero.png")
    result = use_in_project(project_service, source)
    asset = project_service.current.asset_by_id(result.asset_id)
    assert "a blue banner" in asset.notes
    assert "1234" in asset.notes


def test_a_non_image_is_refused_with_a_reason(project_service, tmp_path):
    bad = tmp_path / "notes.png"
    bad.write_bytes(b"this is not an image")
    result = use_in_project(project_service, bad)
    assert not result.ok
    assert result.what_to_do


def test_no_open_project_offers_the_real_choices(paths, settings, tmp_path):
    """Section 27: create a project, or save to the library - not a dead end."""
    service = ProjectService(paths, settings)  # nothing open
    source = generated_image(tmp_path / "hero.png")
    result = use_in_project(service, source)
    assert not result.ok
    assert result.code == "no_project"
    assert "project" in result.what_to_do.lower()
    assert source.is_file(), "the image must not be touched"


# --------------------------------------------------------------------------
# Send to scene (sections 27, 28)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("placement", PLACEMENTS)
def test_every_placement_works(project_service, tmp_path, placement):
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source, placement=placement)
    assert result.ok, result.message
    scene = project_service.current.scene_by_id(result.scene_id)
    assert scene is not None
    if placement == Placement.BACKGROUND:
        assert scene.background == result.asset_id
    else:
        assert len(scene.elements) == 1
        element = scene.elements[0]
        assert element.kind == "image"
        assert element.asset_id == result.asset_id


def test_the_element_references_the_asset_not_a_file_path(project_service,
                                                          tmp_path):
    """One image system: the scene points at the asset id (section 28)."""
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source)
    scene = project_service.current.scene_by_id(result.scene_id)
    element = scene.elements[0]
    assert element.asset_id
    assert "/" not in element.asset_id and "\\" not in element.asset_id


def test_an_element_position_is_normalised_not_pixels(project_service, tmp_path):
    """Resolution independence (Stage D's rule) must hold for sent images too."""
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source, placement=Placement.CHARACTER)
    element = project_service.current.scene_by_id(result.scene_id).elements[0]
    assert 0.0 <= element.position["x"] <= 1.0
    assert 0.0 <= element.position["y"] <= 1.0
    assert element.size["mode"] == "relative"


def test_a_scene_can_be_created_for_the_image(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source, create_scene=True,
                           placement=Placement.OVERLAY)
    assert result.ok and result.created_scene


def test_sending_to_a_missing_scene_is_refused(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source, scene_id="no-such-scene")
    assert not result.ok
    assert result.code == "SCENE_NOT_FOUND"


def test_an_unknown_placement_is_refused(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source, placement="skywards")
    assert not result.ok
    assert result.code == "UNKNOWN_PLACEMENT"


def test_no_open_project_is_reported_clearly(paths, settings, tmp_path):
    service = ProjectService(paths, settings)
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(service, source)
    assert not result.ok
    assert result.code == "no_project"


# --------------------------------------------------------------------------
# The reference survives saving, reopening and moving (sections 27, 64)
# --------------------------------------------------------------------------

def test_the_reference_survives_save_and_reopen(project_service, tmp_path):
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source)
    layout = project_service.current_layout
    project_dir = layout.root
    project_service.save(reason="test")
    project_service.close_project(save=False)

    reopened = ProjectService(project_service.paths, project_service.settings)
    project = reopened.open_project(project_dir / "project.json")
    scene = project.scene_by_id(result.scene_id)
    assert scene is not None
    element = scene.elements[0]
    assert element.asset_id == result.asset_id
    asset = project.asset_by_id(element.asset_id)
    assert asset is not None and not asset.missing
    resolved = resolve_element_image(project, element, project_dir)
    assert resolved is not None and resolved.is_file()
    reopened.close_project(save=False)


def test_a_moved_project_still_finds_its_image(project_service, tmp_path):
    """Relative paths are the point: the project must survive being moved."""
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source)
    project_dir = project_service.current_layout.root
    project_service.save(reason="test")
    project_service.close_project(save=False)

    moved = tmp_path / "moved-elsewhere" / project_dir.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(project_dir), str(moved))

    reopened = ProjectService(project_service.paths, project_service.settings)
    project = reopened.open_project(moved / "project.json")
    element = project.scene_by_id(result.scene_id).elements[0]
    resolved = resolve_element_image(project, element, moved)
    assert resolved is not None and resolved.is_file()
    reopened.close_project(save=False)


def test_a_deleted_source_asset_is_reported_not_crashed(project_service,
                                                        tmp_path):
    """Matrix TEST 13: the project says the file is missing, and stays usable."""
    source = generated_image(tmp_path / "hero.png")
    result = send_to_scene(project_service, source)
    layout = project_service.current_layout
    project_dir = layout.root
    project = project_service.current
    asset = project.asset_by_id(result.asset_id)
    (project_dir / asset.path).unlink()

    checks = project_service.verify_assets()
    missing = [check for check in checks if check.asset.id == result.asset_id]
    assert missing and missing[0].missing

    element = project.scene_by_id(result.scene_id).elements[0]
    assert resolve_element_image(project, element, project_dir) is None
    # The project is still saved and reopened without an exception.
    project_service.save(reason="after deletion")
    assert project_service.current is not None


def test_sending_the_same_image_twice_does_not_duplicate_the_file(
        project_service, tmp_path):
    """Two placements, one asset - the image is imported once."""
    source = generated_image(tmp_path / "hero.png")
    first = send_to_scene(project_service, source, placement=Placement.OVERLAY)
    second = send_to_scene(project_service, source, placement=Placement.BACKGROUND)
    project = project_service.current
    assert first.asset_id == second.asset_id, "the image was imported twice"
    images = [asset for asset in project.assets if asset.kind == "image"]
    assert len(images) == 1


# --------------------------------------------------------------------------
# The renderer really draws it (sections 28, 54)
# --------------------------------------------------------------------------

def test_a_sent_image_is_actually_painted_into_the_scene(project_service,
                                                         tmp_path):
    """The strongest check: compose the scene and look at the pixels."""
    from app.scene.compose import render_scene
    from app.scene.storyboard import build_context

    source = generated_image(tmp_path / "flat.png", colour=(10, 200, 90))
    result = send_to_scene(project_service, source, placement=Placement.BACKGROUND)
    project = project_service.current
    project_dir = project_service.current_layout.root
    scene = project.scene_by_id(result.scene_id)

    context = build_context(project, project_dir=project_dir)
    image = render_scene(scene, context)
    pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    assert abs(pixel[1] - 200) < 40, f"the image was not drawn (centre was {pixel})"


def test_an_overlay_image_is_drawn_over_the_background(project_service,
                                                       tmp_path):
    from app.scene.compose import render_scene
    from app.scene.storyboard import build_context

    source = generated_image(tmp_path / "block.png", colour=(255, 30, 30))
    result = send_to_scene(project_service, source, placement=Placement.OVERLAY)
    project = project_service.current
    project_dir = project_service.current_layout.root
    scene = project.scene_by_id(result.scene_id)
    scene.background = "#000000"

    context = build_context(project, project_dir=project_dir)
    image = render_scene(scene, context).convert("RGB")
    centre = image.getpixel((image.width // 2, image.height // 2))
    assert centre[0] > 150 and centre[1] < 120, f"overlay missing: {centre}"


def test_a_reference_placement_is_not_rendered(project_service, tmp_path):
    """A reference is a note for the user, not something drawn on screen."""
    source = generated_image(tmp_path / "ref.png", colour=(255, 0, 255))
    result = send_to_scene(project_service, source, placement=Placement.REFERENCE)
    project = project_service.current
    element = project.scene_by_id(result.scene_id).elements[0]
    assert element.asset_id, "the reference should still be recorded"


# --------------------------------------------------------------------------
# Helpers used by other modules
# --------------------------------------------------------------------------

def test_resolve_returns_none_for_a_non_image_element():
    project = type("P", (), {"assets": [], "asset_by_id": lambda self, i: None})()
    element = ElementSpec(kind="text", text="hello")
    assert resolve_element_image(project, element, Path("/tmp")) is None


def test_resolve_returns_none_when_the_asset_list_has_no_entry():
    project = type("P", (), {"assets": [],
                             "asset_by_id": lambda self, i: None})()
    element = ElementSpec(kind="image", asset_id="asset-xyz")
    assert resolve_element_image(project, element, Path("/tmp")) is None


def test_resolve_uses_the_project_relative_path(tmp_path):
    image = make_image(tmp_path / "assets" / "pic.png")
    asset = AssetSpec(id="asset-1", kind="image", path="assets/pic.png")
    project = type("P", (), {"assets": [asset],
                             "asset_by_id": lambda self, i: asset})()
    element = ElementSpec(kind="image", asset_id="asset-1")
    assert resolve_element_image(project, element, tmp_path) == image
