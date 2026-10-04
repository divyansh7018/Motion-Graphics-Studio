"""Undo/redo over project snapshots, and cached thumbnails."""

from __future__ import annotations

from app.project.history import DEFAULT_LIMIT, ProjectHistory
from app.project.model import build_project
from app.project.thumbnails import (
    clear_thumbnail_cache,
    generate_thumbnail,
    is_fresh,
    thumbnail_cache_dir,
    thumbnail_path,
)


# --------------------------------------------------------------------------
# Undo / redo
# --------------------------------------------------------------------------

def test_undo_restores_the_previous_state() -> None:
    history = ProjectHistory()
    project = build_project("Undo")

    history.record("Edit script", project)
    project.script.source_text = "Changed."

    outcome = history.undo(project)

    assert outcome is not None
    assert outcome.label == "Edit script"
    assert outcome.project.script.source_text == ""


def test_redo_reapplies_the_undone_change() -> None:
    history = ProjectHistory()
    project = build_project("Redo")

    history.record("Edit script", project)
    project.script.source_text = "Changed."
    history.undo(project)

    outcome = history.redo(project)

    assert outcome is not None
    assert outcome.project.script.source_text == "Changed."


def test_a_new_edit_clears_the_redo_stack() -> None:
    history = ProjectHistory()
    project = build_project("Branch")

    history.record("First", project)
    project.script.source_text = "One."
    history.undo(project)
    assert history.can_redo

    history.record("Second", project)

    assert history.can_redo is False
    assert history.redo(project) is None


def test_several_steps_unwind_in_order() -> None:
    history = ProjectHistory()
    project = build_project("Steps")

    for index in range(3):
        history.record(f"Step {index}", project)
        project.script.source_text = f"State {index}"

    labels = []
    while history.can_undo:
        outcome = history.undo(project)
        project = outcome.project
        labels.append(outcome.label)

    assert labels == ["Step 2", "Step 1", "Step 0"]
    assert project.script.source_text == ""


def test_the_history_is_bounded() -> None:
    history = ProjectHistory(limit=5)
    project = build_project("Bounded")

    for index in range(20):
        history.record(f"Edit {index}", project)
        project.script.source_text = str(index)

    assert len(history) == 5
    assert history.undo_labels[0] == "Edit 19"
    assert DEFAULT_LIMIT > 5


def test_undo_with_nothing_recorded_is_a_no_op() -> None:
    history = ProjectHistory()
    project = build_project("Empty")

    assert history.undo(project) is None
    assert history.redo(project) is None
    assert history.can_undo is False
    assert "undo: 0" in history.describe()


def test_clear_empties_both_stacks() -> None:
    history = ProjectHistory()
    project = build_project("Clear")
    history.record("Edit", project)

    history.clear()

    assert history.can_undo is False and history.can_redo is False


def test_snapshots_are_deep_copies() -> None:
    """Undo must not be affected by later in-place edits."""
    history = ProjectHistory()
    project = build_project("Deep")
    project.add_scene()

    history.record("Add scene", project)
    project.scenes[0].name = "Renamed later"

    restored = history.undo(project).project
    assert restored.scenes[0].name != "Renamed later"


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------

def test_a_thumbnail_is_generated_without_rendering(paths, tmp_path) -> None:
    project = build_project("Thumbnail")
    project.add_scene()
    project.scenes[0].script = "The opening line of the video."
    target = thumbnail_path(paths, project.project.id)

    created = generate_thumbnail(project, target)

    assert created == target
    assert target.exists() and target.stat().st_size > 0
    assert target.read_bytes().startswith(b"\x89PNG")
    assert is_fresh(target, 0.0) is True


def test_thumbnails_live_in_the_cache_never_in_the_project(paths, tmp_path) -> None:
    from app.project.layout import ProjectLayout

    layout = ProjectLayout(tmp_path / "Project")
    layout.ensure()
    project = build_project("Cached")
    project.project.id = "cached-1"

    generate_thumbnail(project, thumbnail_path(paths, project.project.id), layout=layout)

    assert thumbnail_cache_dir(paths).is_dir()
    assert str(paths.cache_dir) in str(thumbnail_path(paths, "cached-1"))
    assert list(layout.root.rglob("*.png")) == [], "no thumbnail inside the project folder"


def test_thumbnail_paths_are_sanitised(paths) -> None:
    path = thumbnail_path(paths, "../../etc/passwd")

    assert path.parent == thumbnail_cache_dir(paths)
    assert ".." not in path.name


def test_clearing_the_cache_removes_only_thumbnails(paths) -> None:
    directory = thumbnail_cache_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.png").write_bytes(b"x")
    (directory / "keep.txt").write_text("keep")

    assert clear_thumbnail_cache(paths) == 1
    assert (directory / "keep.txt").exists()


def test_a_project_without_scenes_still_gets_a_thumbnail(paths) -> None:
    project = build_project("No Scenes")

    created = generate_thumbnail(project, thumbnail_path(paths, project.project.id))

    assert created is not None and created.exists()
