"""Saving, backups, autosave and damaged files (directive sections 9, 11, 12, 24).

Everything here runs against real folders in ``tmp_path`` and real files, so the
guarantees are checked the way they behave on disk - not against mocks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.project.layout import PROJECT_SUBDIRECTORIES, ProjectLayout
from app.project.model import build_project
from app.project.store import (
    ProjectStore,
    backup_project_file,
    fingerprint,
    next_backup_path,
    prune_backups,
)


def make_layout(tmp_path: Path, name: str = "My Project") -> ProjectLayout:
    layout = ProjectLayout(tmp_path / name)
    layout.ensure()
    return layout


def test_a_project_folder_gets_the_documented_layout(tmp_path) -> None:
    layout = make_layout(tmp_path)

    for name in PROJECT_SUBDIRECTORIES:
        assert (layout.root / name).is_dir(), f"missing folder '{name}'"
    assert layout.ensure() == [], "creating twice must not report new folders"


def test_saving_writes_a_readable_versioned_file(tmp_path) -> None:
    layout = make_layout(tmp_path)
    project = build_project("Saved Project")

    result = ProjectStore().save(project, layout, reason="test")

    assert result.ok, result.summary()
    data = json.loads(layout.project_file.read_text(encoding="utf-8"))
    assert data["project"]["name"] == "Saved Project"
    assert data["project"]["project_version"] == 2, "a normal save increments the version"
    assert data["schema_version"] == 2
    assert result.bytes_written == layout.project_file.stat().st_size


def test_the_first_write_of_a_new_project_stays_version_one(tmp_path) -> None:
    layout = make_layout(tmp_path)
    project = build_project("Brand New")

    result = ProjectStore().save(project, layout, backup=False, bump_version=False)

    assert result.ok
    assert json.loads(layout.project_file.read_text(encoding="utf-8"))["project"]["project_version"] == 1


def test_every_save_keeps_the_previous_version(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore(keep_backups=10)
    project = build_project("Backups")

    store.save(project, layout)
    project.script.source_text = "Changed."
    second = store.save(project, layout)

    assert second.backup is not None and second.backup.exists()
    assert second.backup.name.startswith("project_")
    assert json.loads(second.backup.read_text(encoding="utf-8"))["script"]["source_text"] == ""


def test_the_backup_chain_never_grows_forever(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore(keep_backups=3)
    project = build_project("Rotation")

    for index in range(8):
        project.script.source_text = f"Version {index}"
        store.save(project, layout)
        time.sleep(0.01)  # distinct mtimes so pruning is deterministic

    backups = layout.backup_files()
    assert len(backups) == 3
    newest = json.loads(backups[0].read_text(encoding="utf-8"))
    # A backup is taken *before* the write, so the newest one holds the
    # second-to-last version - the last one is the project file itself.
    assert newest["script"]["source_text"] == "Version 6", "the newest backup must be kept"


def test_backup_numbering_follows_the_documented_scheme(tmp_path) -> None:
    backups = tmp_path / "backups"
    first = next_backup_path(backups)
    assert first.name.startswith("project_") and first.name.endswith("_001.json")
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}")
    assert next_backup_path(backups).name.endswith("_002.json")


def test_pruning_keeps_the_newest(tmp_path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(5):
        (backups / f"project_2026-01-0{index + 1}_001.json").write_text("{}")
        time.sleep(0.01)

    removed = prune_backups(backups, keep=2)

    assert len(removed) == 3
    assert len(list(backups.iterdir())) == 2


def test_a_save_is_refused_when_the_project_is_invalid(tmp_path) -> None:
    layout = make_layout(tmp_path)
    project = build_project("Broken")
    project.format.fps = 90  # not a supported rate

    result = ProjectStore().save(project, layout)

    assert not result.ok
    assert "issue" in result.summary()
    assert not layout.project_file.exists(), "an invalid project must not be written"


def test_an_invalid_save_does_not_touch_the_previous_file(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Good")
    store.save(project, layout)
    before = layout.project_file.read_text(encoding="utf-8")

    project.format.width = 1921  # odd width
    result = store.save(project, layout)

    assert not result.ok
    assert layout.project_file.read_text(encoding="utf-8") == before


def test_the_script_is_written_out_exactly_as_typed(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Script")
    project.script.source_text = "Line one\n\nLine  two   with spaces\n"

    store.save(project, layout)

    assert layout.script_file.read_text(encoding="utf-8") == "Line one\n\nLine  two   with spaces\n"


def test_an_empty_script_does_not_leave_a_stray_file(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Empty script")

    store.save(project, layout)
    assert not layout.script_file.exists()

    project.script.source_text = "Now there is text."
    store.save(project, layout)
    assert layout.script_file.exists()

    project.script.source_text = ""
    store.save(project, layout)
    assert not layout.script_file.exists(), "removing the text removes the file"


def test_autosave_never_writes_project_json(tmp_path) -> None:
    """The whole point of a separate recovery file (directive section 11)."""
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Autosave")
    store.save(project, layout)
    saved_copy = layout.project_file.read_text(encoding="utf-8")

    project.script.source_text = "Unsaved work."
    autosave = store.autosave(project, layout)

    assert autosave is not None and autosave.exists()
    assert autosave.parent == layout.autosave_dir
    assert layout.project_file.read_text(encoding="utf-8") == saved_copy
    assert "Unsaved work." in autosave.read_text(encoding="utf-8")


def test_a_successful_save_clears_the_recovery_file(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Clear")
    store.save(project, layout)
    project.script.source_text = "Work."
    store.autosave(project, layout)
    assert layout.autosave_file.exists()

    store.save(project, layout)

    assert not layout.autosave_file.exists()


def test_autosaves_are_rotated(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore(keep_autosaves=3)
    project = build_project("Rotate autosaves")
    store.save(project, layout)

    for index in range(6):
        project.script.source_text = f"Draft {index}"
        store.autosave(project, layout)
        store.snapshot_autosave(layout)
        time.sleep(0.01)

    files = [item for item in layout.autosave_dir.iterdir() if item.is_file()]
    assert len(files) <= 3


def test_a_damaged_project_file_is_kept_and_reported(tmp_path) -> None:
    layout = make_layout(tmp_path)
    layout.project_file.write_text('{"project": {"name": "Cut off', encoding="utf-8")

    result = ProjectStore().load(layout.project_file)

    assert not result.ok
    assert result.error and "JSON" in result.error
    assert result.quarantined is not None and result.quarantined.exists()
    assert result.friendly is not None
    assert "not been" in " ".join(result.friendly.actions) or "backup" in " ".join(result.friendly.actions).lower()


def test_a_missing_project_file_is_reported_not_raised(tmp_path) -> None:
    result = ProjectStore().load(tmp_path / "nothing" / "project.json")

    assert not result.ok
    assert "does not exist" in result.error


def test_loading_an_old_schema_migrates_and_reports_it(tmp_path) -> None:
    from tests.test_project_migrations import draft_v1_project

    layout = make_layout(tmp_path)
    layout.project_file.write_text(json.dumps(draft_v1_project()), encoding="utf-8")

    result = ProjectStore().load(layout.project_file)

    assert result.ok and result.project is not None
    assert result.migrated is True
    assert "migrated from schema 1" in result.summary()
    assert result.project.project.name == "Old Project"


def test_a_newer_project_file_is_refused_without_being_changed(tmp_path) -> None:
    layout = make_layout(tmp_path)
    payload = build_project("Future").to_dict()
    payload["schema_version"] = 99
    layout.project_file.write_text(json.dumps(payload), encoding="utf-8")
    before = layout.project_file.read_text(encoding="utf-8")

    result = ProjectStore().load(layout.project_file)

    assert not result.ok
    assert result.friendly is not None
    assert "schema version 99" in result.friendly.why
    assert layout.project_file.read_text(encoding="utf-8") == before


def test_external_modification_is_detected(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Conflict")
    store.save(project, layout)
    seen = fingerprint(layout.project_file, project.project.project_version)

    assert store.changed_outside(layout.project_file, seen) is False

    time.sleep(0.01)
    layout.project_file.write_text(layout.project_file.read_text(encoding="utf-8") + " ", encoding="utf-8")

    assert store.changed_outside(layout.project_file, seen) is True


def test_a_deleted_project_file_counts_as_an_external_change(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Deleted outside")
    store.save(project, layout)
    seen = fingerprint(layout.project_file)

    layout.project_file.unlink()

    assert store.changed_outside(layout.project_file, seen) is True


def test_a_completed_save_leaves_no_pending_marker(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Marker")

    store.save(project, layout)
    project.script.source_text = "Two."
    store.save(project, layout)

    assert store.pending_backup(layout) is None
    assert store.detect_recovery(layout) is None


def test_a_failed_write_keeps_the_previous_file_and_says_so(tmp_path, monkeypatch) -> None:
    """A locked file on Windows must produce 'not saved', never a traceback."""
    import app.project.store as store_module

    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Locked file")
    store.save(project, layout)
    good = layout.project_file.read_text(encoding="utf-8")

    real_write = store_module.atomic_write_json

    def deny(path, *args, **kwargs):
        if Path(path).name == "project.json":
            raise PermissionError(13, "Sharing violation")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "atomic_write_json", deny)
    project.script.source_text = "Cannot be written."
    result = store.save(project, layout)

    assert result.ok is False
    assert "Sharing violation" in result.error
    assert layout.project_file.read_text(encoding="utf-8") == good
    assert store.pending_backup(layout) is None, "a handled failure clears the marker"


def test_backup_of_a_missing_file_is_a_no_op(tmp_path) -> None:
    assert backup_project_file(tmp_path / "nope.json", tmp_path / "backups") is None
