"""The project service: the same code path the GUI and the CLI use.

Covers directive sections 8, 9, 10, 14, 15, 18, 19, 29, 30 and 33.
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import ProjectConflictError, ProjectError
from app.project.service import CreateRequest, ProjectService, validate_project_name


@pytest.fixture()
def service(paths, settings):
    return ProjectService(paths, settings)


def create(service, name="My Video", **kwargs):
    return service.create_project(CreateRequest(name=name, **kwargs))


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------

def test_creating_a_project_builds_the_folder_and_file(service) -> None:
    project = create(service, "Launch Video", template="youtube", description="first")

    layout = service.current_layout
    assert layout.project_file.exists()
    assert layout.assets_dir.is_dir() and layout.renders_dir.is_dir()
    assert project.project.name == "Launch Video"
    assert project.project.description == "first"
    assert json.loads(layout.project_file.read_text(encoding="utf-8"))["project"]["name"] == "Launch Video"


def test_a_new_project_is_registered_as_recent(service) -> None:
    create(service, "Recent One")

    entries = service.list_recent()
    assert [entry.name for entry in entries] == ["Recent One"]
    assert entries[0].status == "ok"
    assert entries[0].resolution_label() == "1920x1080"


def test_a_blank_project_has_no_invented_content(service) -> None:
    project = create(service, "Blank", template="blank")

    assert project.scenes == []
    assert project.assets == []
    assert project.script.source_text == ""


def test_a_template_seeds_settings_but_not_media(service) -> None:
    project = create(service, "Short", template="shorts")

    assert (project.format.width, project.format.height) == (1080, 1920)
    assert project.format.aspect_ratio == "9:16"
    assert project.scenes == [] and project.assets == []


@pytest.mark.parametrize(
    "name",
    ["", "   ", ".", "..", "CON", "a" * 121],
)
def test_invalid_names_are_refused_before_anything_is_created(service, name) -> None:
    with pytest.raises(ProjectError):
        create(service, name)
    assert service.list_recent() == []


def test_a_duplicate_folder_name_is_refused_with_a_way_forward(service, paths) -> None:
    create(service, "Same Name")
    service.close_project()

    with pytest.raises(ProjectError) as caught:
        create(service, "Same Name")

    friendly = caught.value.friendly()
    assert "already exists" in friendly.what_happened
    assert any("open the existing project" in action.lower() for action in friendly.actions)


def test_a_failed_creation_leaves_no_partial_folder(service, paths, monkeypatch) -> None:
    from app.project import store as store_module

    def fail_save(self, project, layout, **kwargs):
        from app.project.store import ProjectSaveResult

        return ProjectSaveResult(ok=False, path=layout.project_file, error="disk on fire")

    monkeypatch.setattr(store_module.ProjectStore, "save", fail_save)

    with pytest.raises(ProjectError):
        create(service, "Will Fail")

    assert not (paths.projects_dir / "Will Fail").exists()
    assert service.list_recent() == []


def test_project_names_are_validated_with_reasons() -> None:
    assert validate_project_name("Good Name") == []
    assert validate_project_name("")
    assert validate_project_name("NUL")
    assert validate_project_name("x" * 200)


# --------------------------------------------------------------------------
# Open / save / dirty state
# --------------------------------------------------------------------------

def test_opening_a_project_restores_every_setting(service, paths) -> None:
    created = create(service, "Round Trip", template="finance", quality="very_high")
    folder = service.current_layout.root
    service.close_project()

    opened = service.open_project(folder)

    assert opened.project.name == "Round Trip"
    assert (opened.format.width, opened.format.height, opened.format.fps) == (
        created.format.width,
        created.format.height,
        created.format.fps,
    )
    assert opened.format.quality_preset == created.format.quality_preset
    assert opened.format.crf == created.format.crf
    assert opened.voice.engine == "kokoro"


def test_a_project_can_be_opened_from_its_json_path(service) -> None:
    create(service, "By File")
    project_file = service.current_layout.project_file
    service.close_project()

    assert service.open_project(project_file).project.name == "By File"


def test_opening_a_folder_that_is_not_a_project_is_a_clear_error(service, tmp_path) -> None:
    empty = tmp_path / "not a project"
    empty.mkdir()

    with pytest.raises(ProjectError) as caught:
        service.open_project(empty)

    assert "not a project" in caught.value.friendly().what_happened


def test_dirty_state_tracks_real_changes_only(service) -> None:
    create(service, "Dirty")
    assert service.session.dirty is False

    service.set_script_text("Something new.")
    assert service.session.dirty is True

    service.save()
    assert service.session.dirty is False

    service.set_script_text("Something new.")
    service.undo()
    assert service.session.dirty is False, "undo back to the saved state is not dirty"


def test_saving_reports_the_version_and_the_backup(service) -> None:
    create(service, "Versions")
    service.set_script_text("One.")
    first = service.save()
    service.set_script_text("Two.")
    second = service.save()

    assert first.project_version == 2
    assert second.project_version == 3
    assert second.backup is not None and second.backup.exists()


def test_discard_reloads_the_saved_project(service) -> None:
    create(service, "Discard")
    service.save()
    service.set_script_text("Throw this away.")

    restored = service.discard_changes()

    assert restored.script.source_text == ""
    assert service.session.dirty is False


def test_saving_over_an_external_change_is_refused(service) -> None:
    """Directive section 30: never overwrite a newer file silently."""
    create(service, "Conflict")
    layout = service.current_layout
    service.save()

    outside = json.loads(layout.project_file.read_text(encoding="utf-8"))
    outside["script"]["source_text"] = "Written by another copy of the app."
    layout.project_file.write_text(json.dumps(outside), encoding="utf-8")

    service.set_script_text("My edits here.")
    with pytest.raises(ProjectConflictError) as caught:
        service.save()

    friendly = caught.value.friendly()
    assert "changed outside" in friendly.what_happened
    assert any("Reload" in action for action in friendly.actions)
    assert "another copy" in json.loads(layout.project_file.read_text(encoding="utf-8"))["script"]["source_text"]


def test_an_external_change_can_be_overridden_explicitly(service) -> None:
    create(service, "Force")
    layout = service.current_layout
    service.save()
    layout.project_file.write_text(layout.project_file.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = service.save(force=True)

    assert result.ok


def test_validation_reports_every_problem_before_saving(service) -> None:
    create(service, "Validate me")
    service.edit("Break it", lambda project: setattr(project.format, "fps", 90))

    report = service.validate()

    assert not report.ok
    assert any(issue.code == "FPS" for issue in report.errors)
    assert service.save().ok is False


# --------------------------------------------------------------------------
# Save As / Duplicate
# --------------------------------------------------------------------------

def test_save_as_creates_an_independent_project(service) -> None:
    create(service, "Original")
    service.set_script_text("Shared text.")
    service.save()
    original_folder = service.current_layout.root

    clone = service.save_as("The Copy")

    assert service.current_layout.root != original_folder
    assert clone.project.id != "Original"
    assert (service.current_layout.project_file).exists()
    assert (original_folder / "project.json").exists()

    service.set_script_text("Only in the copy.")
    service.save()
    original = json.loads((original_folder / "project.json").read_text(encoding="utf-8"))
    assert original["script"]["source_text"] == "Shared text.", "the original must not change"


def test_save_as_rejects_an_existing_folder(service) -> None:
    create(service, "First")
    service.save()

    with pytest.raises(ProjectError):
        service.save_as("First")


def test_duplicate_copies_the_assets_by_default(service, tmp_path) -> None:
    create(service, "With Assets")
    layout = service.current_layout
    source = tmp_path / "logo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    report = service.import_asset(source, kind="logo")
    assert report.ok
    service.save()

    loaded_id = service.current.project.id
    duplicate = service.duplicate_project(layout.root)

    duplicate_layout = service.current_layout
    assert duplicate_layout.root != layout.root
    assert (duplicate_layout.assets_dir / "logo.png").exists()
    assert duplicate.assets[0].path == "assets/logo.png"
    assert duplicate.project.id != loaded_id
    assert duplicate.project.project_version == 1


def test_duplicate_can_reference_instead_of_copy(service, tmp_path) -> None:
    create(service, "Referenced")
    layout = service.current_layout
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0" * 32)
    service.import_asset(source, kind="video")
    service.save()

    duplicate = service.duplicate_project(layout.root, name="Referenced Copy", copy_assets=False)

    assert duplicate.assets[0].absolute_path
    assert duplicate.assets[0].path == ""
    assert not (service.current_layout.assets_dir / "clip.mp4").exists()


def test_duplicating_a_broken_project_reports_instead_of_guessing(service, tmp_path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "project.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ProjectError):
        service.duplicate_project(broken)


# --------------------------------------------------------------------------
# Rename / delete / recent
# --------------------------------------------------------------------------

def test_rename_changes_the_name_but_not_the_folder(service) -> None:
    create(service, "Old Name")
    folder_before = service.current_layout.root

    service.rename_project("New Name")

    assert service.current.project.name == "New Name"
    assert service.current_layout.root == folder_before
    assert service.session.dirty is True


def test_renaming_the_folder_keeps_relative_assets_working(service, tmp_path) -> None:
    create(service, "Portable")
    layout = service.current_layout
    source = tmp_path / "bg.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    service.import_asset(source)
    service.save()

    new_folder = service.rename_project_folder("Portable Renamed")

    assert new_folder.name == "Portable Renamed"
    assert (new_folder / "assets" / "bg.png").exists()
    checks = service.verify_assets()
    assert checks[0].exists is True
    assert not layout.root.exists()


def test_remove_from_recent_does_not_delete_files(service) -> None:
    create(service, "Keep My Files")
    folder = service.current_layout.root
    service.close_project()

    assert service.remove_from_recent(folder) is True

    assert service.list_recent() == []
    assert (folder / "project.json").exists(), "the project itself must survive"


def test_delete_requires_confirmation_and_only_works_inside_projects(service, tmp_path) -> None:
    create(service, "Delete Me")
    folder = service.current_layout.root

    with pytest.raises(ProjectError):
        service.delete_project(folder)  # no confirmation

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ProjectError) as caught:
        service.delete_project(outside, confirm=True)
    assert "projects folder" in caught.value.friendly().what_happened

    assert service.delete_project(folder, confirm=True) is True
    assert not folder.exists()
    assert service.list_recent() == []


def test_favourites_and_archives_are_tracked(service) -> None:
    create(service, "Favourite")
    folder = service.current_layout.root
    service.close_project()

    service.set_favorite(folder, True)
    assert service.list_recent()[0].favorite is True

    service.set_archived(folder, True)
    assert service.list_recent() == []
    assert len(service.list_recent(include_archived=True)) == 1


def test_a_missing_project_is_marked_not_hidden(service) -> None:
    create(service, "Will Vanish")
    folder = service.current_layout.root
    service.close_project()
    import shutil

    shutil.rmtree(folder)

    entries = service.list_recent()
    assert entries[0].status == "missing"
    assert entries[0].status_label() == "Folder missing"


# --------------------------------------------------------------------------
# Editing, undo, autosave
# --------------------------------------------------------------------------

def test_scenes_can_be_added_removed_reordered_and_undone(service) -> None:
    create(service, "Scenes")
    first = service.add_scene("title", "Intro")
    second = service.add_scene("text", "Body")

    assert [scene.name for scene in service.current.scenes] == ["Intro", "Body"]

    service.move_scene(second.id, 0)
    assert [scene.name for scene in service.current.scenes] == ["Body", "Intro"]

    assert service.undo() == "Move scene"
    assert [scene.name for scene in service.current.scenes] == ["Intro", "Body"]

    assert service.remove_scene(first.id) is True
    assert [scene.name for scene in service.current.scenes] == ["Body"]
    assert service.undo() == "Delete scene"
    assert len(service.current.scenes) == 2
    assert service.redo() == "Delete scene"
    assert len(service.current.scenes) == 1


def test_undo_on_an_empty_history_does_nothing(service) -> None:
    create(service, "Nothing to undo")
    assert service.undo() is None
    assert service.redo() is None


def test_autosave_only_runs_when_there_is_something_to_save(service, settings) -> None:
    create(service, "Autosave")
    assert service.autosave() is None, "nothing changed yet"

    service.set_script_text("Unsaved.")
    path = service.autosave()
    assert path is not None and path.exists()

    settings.autosave.enabled = False
    assert service.autosave() is None


def test_actions_without_an_open_project_say_so(service) -> None:
    with pytest.raises(ProjectError) as caught:
        service.save()
    assert "No project is open" in caught.value.friendly().what_happened


def test_closing_a_project_can_save_it(service) -> None:
    create(service, "Close and save")
    service.set_script_text("Persisted.")

    service.close_project(save=True)

    assert service.current is None
    folder = service.paths.projects_dir / "Close and save"
    assert json.loads((folder / "project.json").read_text(encoding="utf-8"))["script"]["source_text"] == "Persisted."


def test_the_lock_is_released_when_a_project_is_closed(service) -> None:
    create(service, "Locked")
    layout = service.current_layout
    assert layout.lock_file.exists()

    service.close_project()

    assert not layout.lock_file.exists()


def test_events_are_reported_to_the_listener(paths, settings) -> None:
    seen: list[str] = []
    service = ProjectService(paths, settings, notifier=lambda event, fields: seen.append(event))

    create(service, "Events")
    service.set_script_text("Text.")
    service.save()

    assert "PROJECT_CREATE" in seen
    assert "PROJECT_SAVE" in seen
