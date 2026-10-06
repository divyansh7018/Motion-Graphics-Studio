"""Stage D (second pass) - the gaps the full directive named.

Covers the model/service operations (z-order, element duplication, scene
copy/paste, enable/disable, lock), the new element kinds (group, progress), the
value animations (count-up, progress-fill), the extra transitions and image
backgrounds, the expanded scene registry, the shared :class:`SceneService` and
the ``motion-studio scene`` CLI.  Every test drives the real code path.
"""

from __future__ import annotations


import pytest
from PIL import Image

from app.project.model import ElementSpec, Project, SceneSpec
from app.project.service import CreateRequest, ProjectService
from app.scene.animation import (
    ANIMATION_PRESETS,
    evaluate,
    parse_animation,
    value_progress,
)
from app.scene.canvas import Canvas
from app.scene.compose import render_scene
from app.scene.elements import LayoutContext, layout_scene
from app.scene.templates import (
    create_scene_from_template,
    default_templates_registered,
    template_keys,
)
from app.scene.transitions import ENGINE_TRANSITIONS, blend
from app.scene.timing import build_timeline


# --------------------------------------------------------------------------
# Model: z-order, duplication, copy/paste, enable/disable
# --------------------------------------------------------------------------

def _project_with_scene() -> Project:
    project = Project()
    project.add_scene(SceneSpec(id="s1", name="Scene", type="blank"))
    for i in range(3):
        project.add_element("s1", ElementSpec(id=f"e{i}", kind="text", text=f"el {i}"))
    return project


def test_add_element_syncs_z_index_with_list_order():
    project = _project_with_scene()
    scene = project.scenes[0]
    assert [el.z_index for el in scene.elements] == [1, 2, 3]


def test_move_element_forward_and_back_reorders_and_resyncs():
    project = _project_with_scene()
    scene = project.scenes[0]
    assert project.move_element("s1", "e0", 1)
    assert [el.id for el in scene.elements] == ["e1", "e0", "e2"]
    assert [el.z_index for el in scene.elements] == [1, 2, 3]
    assert project.move_element("s1", "e0", -5)  # clamps to the back
    assert scene.elements[0].id == "e0"


def test_element_to_front_and_back():
    project = _project_with_scene()
    scene = project.scenes[0]
    project.element_to_front("s1", "e0")
    assert scene.elements[-1].id == "e0"
    project.element_to_back("s1", "e0")
    assert scene.elements[0].id == "e0"


def test_duplicate_element_is_independent():
    project = _project_with_scene()
    scene = project.scenes[0]
    copy = project.duplicate_element("s1", "e0")
    assert copy is not None
    assert copy.id != "e0"
    copy.text = "changed"
    assert project.element_by_id("s1", "e0").text == "el 0"  # original untouched
    assert len(scene.elements) == 4


def test_copy_paste_scene_is_independent():
    project = _project_with_scene()
    clip = project.copy_scene("s1")
    assert clip and clip["__clip__"] == "scene"
    pasted = project.paste_scene(clip)
    assert pasted is not None
    assert pasted.id != "s1"
    assert {el.id for el in pasted.elements}.isdisjoint({el.id for el in project.scenes[0].elements})
    # Editing the paste never touches the original.
    pasted.elements[0].text = "paste edit"
    assert project.scenes[0].elements[0].text == "el 0"


def test_paste_rejects_a_non_scene_clipboard():
    project = _project_with_scene()
    assert project.paste_scene({"__clip__": "element"}) is None
    assert project.paste_scene(None) is None


def test_disabled_scene_is_dropped_from_the_timeline():
    project = Project()
    a = SceneSpec(id="a", name="A", duration=4.0)
    b = SceneSpec(id="b", name="B", duration=6.0)
    project.add_scene(a)
    project.add_scene(b)
    assert len(project.timeline()) == 2
    a.enabled = False
    rows = project.timeline()
    assert [row["id"] for row in rows] == ["b"]
    assert rows[0]["start"] == 0.0  # the disabled scene consumes no time


def test_build_timeline_skips_disabled_scenes():
    a = SceneSpec(id="a", duration=4.0)
    b = SceneSpec(id="b", duration=6.0)
    a.enabled = False
    timeline = build_timeline([a, b])
    assert timeline.scene_count == 1
    assert timeline.total_duration == pytest.approx(6.0)


def test_scene_and_element_lock_flags_round_trip_through_dict():
    scene = SceneSpec(id="s", name="S", locked=True)
    scene.elements.append(ElementSpec(id="e", kind="text", locked=True, z_index=1))
    restored = SceneSpec.from_dict(scene.to_dict())
    assert restored.locked is True
    assert restored.elements[0].locked is True
    assert restored.elements[0].z_index == 1


# --------------------------------------------------------------------------
# Service: the same operations, undoable
# --------------------------------------------------------------------------

@pytest.fixture()
def service(paths, settings):
    svc = ProjectService(paths, settings)
    svc.create_project(CreateRequest(name="Ops"))
    return svc


def test_service_element_ops_are_undoable(service):
    scene = service.add_scene_from_template("body", {"title": "Hi", "text": "Body"})
    first = scene.elements[0]
    copy = service.duplicate_element(scene.id, first.id)
    assert copy is not None and copy.id != first.id
    assert len(service.current.scene_by_id(scene.id).elements) == len(scene.elements)
    service.undo()
    # After undo the duplicate is gone.
    assert len(service.current.scene_by_id(scene.id).elements) == len(scene.elements) - 1


def test_service_enable_disable_and_lock(service):
    scene = service.add_scene("blank", "Toggle")
    assert service.set_scene_enabled(scene.id, False)
    assert service.current.scene_by_id(scene.id).enabled is False
    assert service.set_scene_locked(scene.id, True)
    assert service.current.scene_by_id(scene.id).locked is True
    service.undo()  # undo the lock
    assert service.current.scene_by_id(scene.id).locked is False


def test_service_copy_paste_scene(service):
    scene = service.add_scene_from_template("title", {"title": "Original"})
    clip = service.copy_scene(scene.id)
    pasted = service.paste_scene(clip)
    assert pasted is not None and pasted.id != scene.id
    assert len(service.current.scenes) == 2


def test_service_update_scene_and_element_fields(service):
    scene = service.add_scene_from_template("body", {"title": "T", "text": "B"})
    assert service.update_scene_field(scene.id, name="Renamed", duration=7.5)
    assert service.current.scene_by_id(scene.id).name == "Renamed"
    assert service.current.scene_by_id(scene.id).duration == 7.5
    element = scene.elements[0]
    assert service.update_element_field(scene.id, element.id, text="New text")
    assert service.current.element_by_id(scene.id, element.id).text == "New text"
    # Unknown fields are ignored, not applied.
    assert not service.update_scene_field(scene.id, bogus=1)


# --------------------------------------------------------------------------
# Elements: group and progress
# --------------------------------------------------------------------------

def _ctx(width=1280, height=720) -> LayoutContext:
    canvas = Canvas(width, height)
    return LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), palette={})


def test_group_lays_out_children_without_a_background():
    scene = SceneSpec(id="g", type="blank")
    scene.elements.append(ElementSpec(
        id="grp", kind="group", anchor="center", position={"x": 0.5, "y": 0.5},
        size={"width": {"mode": "fraction", "value": 0.6}, "height": {"mode": "fraction", "value": 0.4}},
        extra={"children": [
            {"id": "c1", "kind": "text", "text": "Inside", "anchor": "center",
             "position": {"x": 0.5, "y": 0.5}, "size": {"mode": "relative", "value": 0.2}},
        ]},
    ))
    layout = layout_scene(scene.elements, _ctx())
    group = layout.elements[0]
    assert group.kind == "group"
    assert group.background is None  # transparent by default
    assert len(group.children) == 1
    # The child is translated into scene space, not left in group-local coords.
    assert group.children[0].rect.x >= group.rect.x


def test_progress_layout_computes_a_fraction():
    scene = SceneSpec(id="p", type="blank")
    scene.elements.append(ElementSpec(
        id="bar", kind="progress", anchor="center", position={"x": 0.5, "y": 0.5},
        size={"width": {"mode": "fraction", "value": 0.6}, "height": {"mode": "design", "value": 0.05}},
        extra={"value": 30, "max": 120, "fill": "theme"},
    ))
    layout = layout_scene(scene.elements, _ctx())
    bar = layout.elements[0]
    assert bar.kind == "progress"
    assert bar.progress_fraction == pytest.approx(0.25)
    assert bar.fill_color is not None


def test_progress_fraction_clamps_to_zero_one():
    scene = SceneSpec(id="p", type="blank")
    scene.elements.append(ElementSpec(
        id="bar", kind="progress", anchor="center", position={"x": 0.5, "y": 0.5},
        size={"width": {"mode": "fraction", "value": 0.6}, "height": {"mode": "design", "value": 0.05}},
        extra={"value": 999, "max": 100},
    ))
    bar = layout_scene(scene.elements, _ctx()).elements[0]
    assert bar.progress_fraction == 1.0


# --------------------------------------------------------------------------
# Animation: value presets, direction, stagger, shake determinism
# --------------------------------------------------------------------------

def test_value_presets_are_registered_and_parse_to_a_value_mode():
    assert "count up" in ANIMATION_PRESETS and "progress fill" in ANIMATION_PRESETS
    spec = parse_animation({"preset": "count up", "duration": 1.0})
    assert spec.value_mode == "count_up"
    assert spec.enter  # it still gets a clock


def test_value_progress_eases_from_zero_to_one():
    spec = parse_animation({"preset": "count up", "duration": 1.0})
    assert value_progress(spec, 0.0) == pytest.approx(0.0, abs=1e-6)
    assert value_progress(spec, 1.0) == pytest.approx(1.0)
    assert 0.0 < value_progress(spec, 0.5) < 1.0


def test_value_progress_is_one_for_a_non_value_spec():
    spec = parse_animation({"preset": "fade", "duration": 1.0})
    assert value_progress(spec, 0.0) == 1.0


def test_direction_resolves_a_generic_slide():
    up = parse_animation({"preset": "slide", "direction": "up", "duration": 0.6})
    assert up.preset == "slide up"
    right = parse_animation({"preset": "slide", "direction": "right", "duration": 0.6})
    assert right.preset == "slide from left"


def test_stagger_grows_the_delay_with_index():
    base = parse_animation({"preset": "fade", "duration": 0.6, "stagger": 0.2, "index": 0})
    later = parse_animation({"preset": "fade", "duration": 0.6, "stagger": 0.2, "index": 3})
    assert later.enter[0].start == pytest.approx(base.enter[0].start + 0.6)


def test_repeat_is_parsed():
    spec = parse_animation({"preset": "fade", "duration": 0.6, "repeat": 2})
    assert spec.repeat == 2


def test_shake_easing_is_deterministic_and_settles():
    spec = parse_animation({"preset": "shake", "duration": 0.6, "intensity": 0.05})
    first = evaluate(spec, 0.3).x
    second = evaluate(spec, 0.3).x
    assert first == second  # no randomness
    assert evaluate(spec, 10.0).x == pytest.approx(0.0, abs=1e-6)


def test_new_presets_all_exist():
    for name in ("slide up", "slide down", "scale out", "fade out", "shake"):
        assert name in ANIMATION_PRESETS, name


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def test_dip_white_is_a_known_transition():
    assert "dip white" in ENGINE_TRANSITIONS


def test_dip_white_passes_through_white_at_the_midpoint():
    from PIL import Image as _Image

    black = _Image.new("RGBA", (16, 16), (0, 0, 0, 255))
    mid = blend(black, black, 0.5, "dip white")
    pixel = mid.getpixel((8, 8))
    assert pixel[0] > 200 and pixel[1] > 200 and pixel[2] > 200


# --------------------------------------------------------------------------
# Backgrounds: image support
# --------------------------------------------------------------------------

def test_image_background_covers_the_frame(tmp_path):
    picture = tmp_path / "bg.png"
    Image.new("RGB", (400, 200), (200, 30, 30)).save(picture)
    scene = SceneSpec(id="bg", type="blank", background=str(picture))
    ctx = LayoutContext(canvas=Canvas(640, 360), safe_area=Canvas(640, 360).safe_area(), palette={})
    frame = render_scene(scene, ctx, time=1.0, scene_duration=2.0)
    # The red background image should dominate the frame.
    small = frame.convert("RGB").resize((8, 8))
    pixels = small.load()
    reds = sum(1 for y in range(8) for x in range(8)
               if pixels[x, y][0] > 120 and pixels[x, y][0] > pixels[x, y][2])
    assert reds > 40


def test_dict_background_with_overlay(tmp_path):
    picture = tmp_path / "bg.png"
    Image.new("RGB", (200, 200), (250, 250, 250)).save(picture)
    scene = SceneSpec(id="bg", type="blank",
                      background={"image": str(picture), "overlay": "#000000"})
    ctx = LayoutContext(canvas=Canvas(320, 320), safe_area=Canvas(320, 320).safe_area(), palette={})
    frame = render_scene(scene, ctx, time=1.0, scene_duration=2.0)
    px = frame.convert("RGB").getpixel((160, 160))
    assert px[0] < 60  # the black overlay washed the white image out


# --------------------------------------------------------------------------
# Templates: the expanded registry
# --------------------------------------------------------------------------

def test_new_templates_are_registered():
    default_templates_registered()
    keys = set(template_keys())
    for expected in ("hook", "paragraph", "list", "counter", "progress", "comparison",
                     "before_after", "timeline", "bento", "collage", "end_screen", "logo"):
        assert expected in keys, expected


@pytest.mark.parametrize("key", [
    "hook", "paragraph", "list", "counter", "progress", "comparison",
    "before_after", "timeline", "bento", "collage", "end_screen", "logo",
])
def test_every_new_template_renders_at_two_orientations(key):
    default_templates_registered()
    content = {
        "title": "Heading", "text": "Some body copy", "subtitle": "Sub", "value": 4200,
        "unit": "subs", "label": "Growth", "left": "Old", "right": "New",
        "items": ["One", "Two", "Three", "Four"],
        "events": [{"when": "2020", "what": "Started"}, {"when": "2024", "what": "Grew"}],
        "asset_id": "", "asset_ids": ["", "", "", ""],
    }
    scene = create_scene_from_template(key, content)
    for size in ((1280, 720), (720, 1280)):
        canvas = Canvas(*size)
        ctx = LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), palette={})
        frame = render_scene(scene, ctx, time=1.0, scene_duration=3.0)
        assert frame.size == size


def test_counter_template_uses_the_count_up_animation():
    default_templates_registered()
    scene = create_scene_from_template("counter", {"value": 1000})
    number = next(el for el in scene.elements if el.kind == "number")
    assert number.animation.get("preset") == "count up"


def test_progress_template_uses_the_progress_fill_animation():
    default_templates_registered()
    scene = create_scene_from_template("progress", {"value": 50, "max": 100})
    bar = next(el for el in scene.elements if el.kind == "progress")
    assert bar.animation.get("preset") == "progress fill"


# --------------------------------------------------------------------------
# Count-up and progress-fill actually animate pixels
# --------------------------------------------------------------------------

def test_count_up_changes_the_rendered_number_over_time():
    default_templates_registered()
    scene = create_scene_from_template("counter", {"value": 9000, "duration": 2.0})
    canvas = Canvas(640, 360)
    ctx = LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), palette={})
    early = render_scene(scene, ctx, time=0.05, scene_duration=2.0)
    late = render_scene(scene, ctx, time=2.0, scene_duration=2.0)
    # The two frames must differ: the digits are counting up.
    assert early.tobytes() != late.tobytes()


def test_progress_fill_grows_the_bar_over_time():
    default_templates_registered()
    scene = create_scene_from_template("progress", {"value": 100, "max": 100, "duration": 2.0})
    canvas = Canvas(640, 360)
    ctx = LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), palette={})
    early = render_scene(scene, ctx, time=0.05, scene_duration=2.0)
    late = render_scene(scene, ctx, time=2.0, scene_duration=2.0)
    assert early.tobytes() != late.tobytes()


# --------------------------------------------------------------------------
# SceneService and the CLI
# --------------------------------------------------------------------------

@pytest.fixture()
def scene_project(paths, settings):
    svc = ProjectService(paths, settings)
    svc.create_project(CreateRequest(name="Scene CLI"))
    svc.add_scene_from_template("hook", {"title": "Stop"})
    svc.add_scene_from_template("counter", {"value": 100})
    svc.set_scene_enabled(svc.current.scenes[0].id, False)
    svc.save(reason="test")
    return svc


def test_scene_service_lists_enabled_and_disabled(scene_project):
    from app.project.scene_service import SceneService

    scenes = SceneService(scene_project)
    rows = scenes.list_scenes()
    assert len(rows) == 2
    disabled = next(r for r in rows if not r.enabled)
    assert disabled.narration_status == "none"


def test_scene_service_info_reports_elements(scene_project):
    from app.project.scene_service import SceneService

    scenes = SceneService(scene_project)
    detail = scenes.scene_info(scene_project.current.scenes[1].id)
    assert detail is not None
    assert detail.elements and detail.elements[0]["kind"] == "number"
    assert scenes.scene_info("does-not-exist") is None


def test_scene_service_validate_is_clean(scene_project):
    from app.project.scene_service import SceneService

    validation = SceneService(scene_project).validate()
    assert validation.ok


def test_cli_scene_list_runs(paths, settings, capsys):
    from app.cli.scene import command_scene_list

    svc = ProjectService(paths, settings)
    svc.create_project(CreateRequest(name="CLI"))
    svc.add_scene_from_template("title", {"title": "Hello"})
    svc.save(reason="test")
    project_file = str(svc.current_layout.root / "project.json")
    svc.close_project()  # release the lock so the CLI can open it

    class Args:
        project = project_file

    code = command_scene_list(Args(), paths, settings)
    out = capsys.readouterr().out
    assert code == 0
    assert "Title card" in out  # the scene's name
    assert "1 scene(s)" in out


def test_cli_scene_info_runs(paths, settings, capsys):
    from app.cli.scene import command_scene_info

    svc = ProjectService(paths, settings)
    svc.create_project(CreateRequest(name="CLI"))
    scene = svc.add_scene_from_template("title", {"title": "Hello"})
    svc.save(reason="test")
    project_file = str(svc.current_layout.root / "project.json")
    scene_id = scene.id
    svc.close_project()  # release the lock so the CLI can open it

    class Args:
        project = project_file

    args = Args()
    args.scene = scene_id
    code = command_scene_info(args, paths, settings)
    out = capsys.readouterr().out
    assert code == 0
    assert "Hello" in out


# --------------------------------------------------------------------------
# Long-form: 50 scenes stay usable and correct (directive section 68)
# --------------------------------------------------------------------------

def test_long_form_50_scenes_save_reload_and_render(paths, settings):
    default_templates_registered()
    svc = ProjectService(paths, settings)
    svc.create_project(CreateRequest(name="Long Form"))

    rotating = ["title", "body", "stat", "chart", "quote", "bullets", "counter", "progress"]
    for i in range(50):
        key = rotating[i % len(rotating)]
        svc.add_scene_from_template(key, {
            "title": f"Scene {i + 1}", "text": f"Body copy for scene {i + 1}",
            "value": (i + 1) * 100, "unit": "", "label": f"Metric {i + 1}",
            "values": [i + 1, i + 2, i + 3], "labels": ["a", "b", "c"],
        })
    svc.save(reason="long form")
    project_file = svc.current_layout.root / "project.json"
    assert len(svc.current.scenes) == 50
    svc.close_project()

    # Reopen in a fresh service: every scene and id survives the round trip.
    second = ProjectService(paths, settings)
    second.open_project(project_file)
    reloaded = second.current
    assert len(reloaded.scenes) == 50
    ids = [scene.id for scene in reloaded.scenes]
    assert len(set(ids)) == 50  # unique ids

    # The timeline covers all 50 scenes with no cap on length.
    timeline = build_timeline(reloaded.scenes)
    assert timeline.scene_count == 50
    assert timeline.total_duration > 0

    # A sample of scenes still renders at two orientations.
    for scene in (reloaded.scenes[0], reloaded.scenes[24], reloaded.scenes[49]):
        for size in ((1280, 720), (720, 1280)):
            canvas = Canvas(*size)
            ctx = LayoutContext(canvas=canvas, safe_area=canvas.safe_area(), palette={})
            frame = render_scene(scene, ctx, time=1.0, scene_duration=3.0)
            assert frame.size == size
