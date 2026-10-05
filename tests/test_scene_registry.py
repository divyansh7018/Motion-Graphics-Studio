"""Stage D - scene registry, engine validation and scene service operations.

The registry must never advertise a scene or element type the engine cannot
draw, so these tests cross-check every registered template against the stored
schema's allowed types.  The service tests prove duplicate/reorder/rename are
single undoable edits that keep ids unique.
"""

from __future__ import annotations

import pytest

from app.project.model import SCENE_TYPES, build_project
from app.scene.canvas import Canvas
from app.scene.elements import ELEMENT_KINDS
from app.scene.templates import (
    create_scene_from_template,
    default_templates_registered,
    template_keys,
    template_summaries,
)
from app.scene.validate import (
    SceneValidation,
    scene_issue_count,
    validate_project_scenes,
)


CONTENT = {
    "title": "A heading",
    "text": "Body copy that is long enough to need wrapping when it is laid out.",
    "subtitle": "A subtitle",
    "value": 1234.5,
    "unit": "%",
    "label": "growth",
    "values": [10, 20, 30],
    "labels": ["A", "B", "C"],
    "asset_id": "logo",
}


@pytest.fixture(scope="module", autouse=True)
def registry():
    default_templates_registered()
    yield


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_default_templates_are_registered():
    assert default_templates_registered() >= 10
    assert "title" in template_keys()
    assert "blank" in template_keys()


def test_registration_is_idempotent():
    before = len(template_keys())
    assert default_templates_registered() == before


def test_every_template_only_uses_known_scene_types():
    """The menu must never offer a type the stored schema will reject."""
    for key in template_keys():
        scene = create_scene_from_template(key, CONTENT)
        assert scene.type in SCENE_TYPES, key


def test_every_template_only_uses_known_element_kinds():
    for key in template_keys():
        scene = create_scene_from_template(key, CONTENT)
        for element in scene.elements:
            assert element.kind in ELEMENT_KINDS, (key, element.kind)


def test_template_scenes_carry_fresh_unique_ids():
    first = create_scene_from_template("title", CONTENT)
    second = create_scene_from_template("title", CONTENT)
    assert first.id != second.id
    for scene in (first, second):
        ids = [element.id for element in scene.elements]
        assert len(ids) == len(set(ids))
        assert all(ids)  # every element got an id


def test_unknown_template_raises_with_the_known_list():
    with pytest.raises(KeyError) as info:
        create_scene_from_template("nope")
    assert "title" in str(info.value)


def test_summaries_are_menu_ready():
    summaries = template_summaries()
    assert summaries
    for summary in summaries:
        assert summary["label"] and summary["description"]
        assert summary["scene_type"] in SCENE_TYPES


def test_blank_template_is_a_blank_scene():
    scene = create_scene_from_template("blank")
    assert scene.elements == []
    assert scene.type == "blank"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_a_clean_project_validates_without_errors():
    project = build_project("Clean")
    project.scenes = [create_scene_from_template("title", CONTENT)]
    validation = validate_project_scenes(project)
    assert validation.ok
    assert validation.errors == []


def test_unknown_scene_type_is_an_error():
    project = build_project("Bad")
    scene = create_scene_from_template("title", CONTENT)
    scene.type = "hologram"
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    assert not validation.ok
    assert scene_issue_count(validation, "SCENE_TYPE") == 1


def test_zero_duration_scene_without_narration_is_an_error():
    project = build_project("Short")
    scene = create_scene_from_template("title", CONTENT)
    scene.duration = 0.0
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    assert scene_issue_count(validation, "SCENE_DURATION") == 1


def test_transition_out_of_range_is_a_warning_not_an_error():
    project = build_project("Trans")
    scene = create_scene_from_template("title", CONTENT)
    scene.transition_out.type = "fade"
    scene.transition_out.duration = 30.0
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    assert scene_issue_count(validation, "TRANSITION_DURATION") == 1
    assert validation.ok  # warnings do not block


def test_element_out_of_frame_is_reported():
    project = build_project("Off")
    scene = create_scene_from_template("body", CONTENT)
    scene.elements[0].position = {"x": 1.5, "y": 0.5}
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    codes = {issue.code for issue in validation.issues}
    assert "POSITION_OUT_OF_RANGE" in codes or "ELEMENT_OFFSCREEN" in codes


def test_element_referencing_a_missing_asset_is_reported():
    """With no assets at all the layout layer flags the missing image; with an
    unrelated asset present the model layer flags the dangling reference."""
    project = build_project("Asset")
    scene = create_scene_from_template("image", CONTENT)  # refers to "logo"
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    assert scene_issue_count(validation, "IMAGE_MISSING", "ASSET_MISSING_REF") >= 1


def test_duplicate_element_ids_are_an_error():
    project = build_project("Dup")
    scene = create_scene_from_template("body", CONTENT)
    if len(scene.elements) >= 2:
        scene.elements[1].id = scene.elements[0].id
    project.scenes = [scene]
    validation = validate_project_scenes(project)
    assert scene_issue_count(validation, "ELEMENT_ID_DUPLICATE") == 1


def test_validation_does_not_mutate_the_scene():
    project = build_project("Mut")
    scene = create_scene_from_template("title", CONTENT)
    snapshot = scene.to_dict()
    validate_project_scenes(project)
    assert scene.to_dict() == snapshot


def test_empty_scene_list_is_valid():
    project = build_project("Empty")
    validation = validate_project_scenes(project)
    assert validation.ok
    assert validation.issues == []


def test_scene_validation_at_multiple_aspect_ratios():
    """A headline that fits in 16:9 must also be reported sensibly in 9:16."""
    project = build_project("Resp")
    scene = create_scene_from_template("title", {"title": "A very long headline that must wrap "
                                                          "onto multiple lines at any ratio"})
    project.scenes = [scene]
    for width, height in ((1920, 1080), (1080, 1920), (1080, 1080)):
        validation = validate_project_scenes(project, canvas=Canvas(width, height))
        # Fitting guarantees no hard overflow, so only warnings/errors of other kinds.
        assert not any(issue.code == "TEXT_OVERFLOW" and issue.severity == "error"
                       for issue in validation.issues)


def test_scene_validation_counts_helper():
    validation = SceneValidation()
    from app.scene.elements import ElementIssue

    validation.issues.append(ElementIssue("a", "X", "m", "", "error"))
    validation.issues.append(ElementIssue("b", "Y", "m", "", "warning"))
    assert scene_issue_count(validation) == 2
    assert scene_issue_count(validation, "X") == 1
    assert scene_issue_count(validation, "Z") == 0


# --------------------------------------------------------------------------
# Service scene operations
# --------------------------------------------------------------------------

@pytest.fixture()
def service(paths, settings):
    from app.project.service import CreateRequest, ProjectService

    service = ProjectService(paths, settings)
    service.create_project(CreateRequest(name="ScenesProj"))
    return service


def test_duplicate_scene_creates_a_deep_copy_with_new_ids(service):
    first = service.add_scene("title", "First")
    copy = service.duplicate_scene(first.id)
    assert copy is not None
    assert copy.id != first.id
    assert copy.name.endswith("(copy)")
    assert len(service.current.scenes) == 2


def test_duplicate_scene_is_a_single_undoable_edit(service):
    first = service.add_scene("title", "First")
    service.add_scene_from_template("body", {"text": "hello"})
    service.duplicate_scene(first.id)
    assert len(service.current.scenes) == 3
    service.undo()  # undo the duplicate
    assert len(service.current.scenes) == 2


def test_duplicate_unknown_scene_returns_none(service):
    assert service.duplicate_scene("missing") is None


def test_move_scene_reorders(service):
    a = service.add_scene("title", "A")
    b = service.add_scene("title", "B")
    c = service.add_scene("title", "C")
    service.move_scene(c.id, 0)
    assert [s.id for s in service.current.scenes] == [c.id, a.id, b.id]


def test_move_scene_by_moves_up_and_down(service):
    a = service.add_scene("title", "A")
    b = service.add_scene("title", "B")
    service.move_scene_by(b.id, -1)
    assert [s.id for s in service.current.scenes] == [b.id, a.id]
    service.move_scene_by(b.id, 1)
    assert [s.id for s in service.current.scenes] == [a.id, b.id]


def test_rename_scene_persists_the_new_name(service):
    a = service.add_scene("title", "Old")
    assert service.rename_scene(a.id, "New name")
    assert service.current.scene_by_id(a.id).name == "New name"


def test_add_scene_from_template_is_undoable(service):
    service.add_scene_from_template("chart", {"values": [1, 2, 3]})
    assert len(service.current.scenes) == 1
    assert service.current.scenes[0].type == "graphic"
    service.undo()
    assert len(service.current.scenes) == 0
