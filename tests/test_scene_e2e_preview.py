"""Stage D - a real multi-scene preview test against an actual project.

This is the acceptance test the Stage D brief demands: create a project on disk,
give it several scenes (title, image, stat, chart), attach a real image asset,
save it, reopen it in a *second* service instance, and render a preview of every
scene at several aspect ratios - proving the engine is responsive, that scenes
persist, and that narration drives timing.  No mocks: real pixels, real files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.project.service import CreateRequest, ProjectService
from app.scene import (
    Canvas,
    build_context,
    build_rows,
    build_timeline,
    render_scene,
    validate_project_scenes,
)
from app.scene.timing import TimingOptions

ASPECTS = ((1920, 1080), (1080, 1920), (1080, 1080), (1080, 1350), (1280, 720))


def _coverage(image: Image.Image, threshold: int = 40) -> float:
    small = image.convert("L").resize((96, 96))
    pixels = list(small.tobytes())
    return sum(1 for value in pixels if value > threshold) / len(pixels)


@pytest.fixture()
def live_project(paths, settings, tmp_path):
    """A real project on disk with four scenes and one image asset."""
    from app.project.model import AssetSpec

    service = ProjectService(paths, settings)
    service.create_project(CreateRequest(name="E2E Preview"))

    project = service.current
    assets_dir = Path(service.current_layout.root) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (960, 540), (40, 90, 180))
    image.save(assets_dir / "hero.png")
    project.assets.append(AssetSpec(id="hero", name="hero.png", kind="image", path="assets/hero.png"))

    service.add_scene_from_template("title", {"title": "Opening title", "subtitle": "a subtitle"})
    service.add_scene_from_template("image", {"asset_id": "hero", "text": "The hero shot"})
    service.add_scene_from_template("stat", {"value": 98.6, "unit": "%", "label": "on-time"})
    service.add_scene_from_template("chart", {"values": [4, 9, 2, 7], "labels": ["a", "b", "c", "d"]})

    # Give the second scene a measured narration so timing follows it.
    project.scenes[1].narration.duration = 8.25
    project.scenes[1].narration.file = "audio/narration/scene.wav"
    service.save(reason="e2e setup")
    return service


def test_the_project_has_four_scenes_with_elements(live_project):
    project = live_project.current
    assert len(project.scenes) == 4
    kinds = {element.kind for scene in project.scenes for element in scene.elements}
    assert {"text", "image", "number", "chart"} <= kinds


def test_scenes_survive_a_save_and_reopen(live_project, paths, settings):
    from app.project.service import ProjectService

    location = Path(live_project.current_layout.root)
    live_project.close_project(save=False)

    reopened = ProjectService(paths, settings)
    reopened.open_project(location)
    project = reopened.current
    assert len(project.scenes) == 4
    assert project.scenes[0].type == "title"
    first_element_kinds = [element.kind for element in project.scenes[1].elements]
    assert "image" in first_element_kinds
    # Normalised positions survive the round trip.
    assert all(0.0 <= element.position.get("x", 0.5) <= 1.0
               for scene in project.scenes for element in scene.elements)


def test_multi_scene_preview_renders_at_every_aspect_ratio(live_project):
    """The same project renders a sensible frame at each aspect ratio."""
    project = live_project.current
    project_dir = Path(live_project.current_layout.root)

    for width, height in ASPECTS:
        canvas = Canvas(width, height)
        ctx = build_context(project, canvas=canvas, project_dir=project_dir)
        for scene in project.scenes:
            image = render_scene(scene, ctx, time=1.5, scene_duration=5.0)
            assert image.size == (width, height), (width, height, scene.id)
            assert _coverage(image) > 0.01, (width, height, scene.id)


def test_image_scene_shows_the_real_asset_pixels(live_project):
    project = live_project.current
    project_dir = Path(live_project.current_layout.root)
    scene = project.scenes[1]
    ctx = build_context(project, canvas=Canvas(1920, 1080), project_dir=project_dir)
    image = render_scene(scene, ctx, time=1.5, scene_duration=5.0)
    colours = set()
    for x in range(0, image.width, 24):
        for y in range(0, image.height, 24):
            colours.add(image.getpixel((x, y))[:3])
    # The asset's blue background must appear somewhere in the frame.
    assert any(b > 140 and r < 90 for r, g, b in colours)


def test_narration_drives_the_timeline(live_project):
    project = live_project.current
    timeline = build_timeline(project.scenes, TimingOptions(head=0.0, tail=0.5))
    timings = {t.scene_id: t for t in timeline.timings}
    narrated = timings[project.scenes[1].id]
    assert narrated.duration == pytest.approx(8.25 + 0.5)
    assert narrated.source == "narration"
    # The following scene starts after the narrated one ends.
    third = timings[project.scenes[2].id]
    assert third.start == pytest.approx(narrated.end)


def test_storyboard_rows_match_the_scenes(live_project):
    project = live_project.current
    rows, timeline = build_rows(project)
    assert len(rows) == 4
    assert timeline.scene_count == 4
    names = [row.name for row in rows]
    assert all(names)


def test_the_whole_project_validates_cleanly(live_project):
    project = live_project.current
    project_dir = Path(live_project.current_layout.root)
    validation = validate_project_scenes(project, project_dir=project_dir)
    assert validation.errors == [], [issue.message for issue in validation.errors]


def test_transition_between_two_rendered_scenes(live_project):
    from app.scene.transitions import blend

    project = live_project.current
    project_dir = Path(live_project.current_layout.root)
    ctx = build_context(project, canvas=Canvas(640, 360), project_dir=project_dir)
    first = render_scene(project.scenes[0], ctx, time=1.5, scene_duration=5.0)
    second = render_scene(project.scenes[1], ctx, time=1.5, scene_duration=5.0)
    for fraction in (0.0, 0.5, 1.0):
        frame = blend(first, second, fraction, "fade")
        assert frame.size == (640, 360)
