"""Crash recovery (directive section 13).

The rules under test: recovery is *offered*, never applied silently; restoring
keeps a backup of what was there; ignoring does not delete anything.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.project.layout import ProjectLayout
from app.project.model import build_project
from app.project.store import ProjectStore


def make_layout(tmp_path: Path) -> ProjectLayout:
    layout = ProjectLayout(tmp_path / "Recovery")
    layout.ensure()
    return layout


def test_no_recovery_is_offered_for_a_clean_project(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Clean")
    store.save(project, layout)

    assert store.detect_recovery(layout) is None


def test_an_autosave_newer_than_the_project_is_offered(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Crashed")
    store.save(project, layout)

    project.script.source_text = "Work that was never saved."
    store.autosave(project, layout)

    candidate = store.detect_recovery(layout)
    assert candidate is not None
    assert candidate.kind == "autosave"
    assert candidate.project_name == "Crashed"
    assert "newer than the saved project" in candidate.detail()
    assert candidate.headline() == "Recovered project available."


def test_an_identical_autosave_is_not_offered(tmp_path) -> None:
    """No nagging when the autosave holds nothing new."""
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Identical")
    store.save(project, layout)
    store.autosave(project, layout)

    assert store.detect_recovery(layout) is None


def test_an_interrupted_save_offers_the_backup_it_made(tmp_path) -> None:
    """A save that dies halfway leaves a marker, not a guess from timestamps."""
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Interrupted save")
    store.save(project, layout)
    project.script.source_text = "Second version."
    store.save(project, layout)
    backup = layout.backup_files()[0]

    # A hard kill between "backup written" and "project written" leaves this.
    store._mark_save_pending(layout, backup)

    candidate = store.detect_recovery(layout)
    assert candidate is not None
    assert candidate.kind == "backup"
    assert candidate.path == backup
    assert "interrupted" in candidate.reason


def test_a_missing_project_file_is_recoverable(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Lost file")
    store.save(project, layout)
    project.script.source_text = "Last work."
    store.autosave(project, layout)
    layout.project_file.unlink()

    candidate = store.detect_recovery(layout)
    assert candidate is not None
    assert candidate.project_file_missing is True


def test_restoring_a_recovery_file_keeps_a_backup_of_the_old_one(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Restore me")
    store.save(project, layout)
    project.script.source_text = "Recovered work."
    store.autosave(project, layout)
    backups_before = len(layout.backup_files())

    candidate = store.detect_recovery(layout)
    result = store.apply_recovery(candidate, layout)

    assert result.ok and result.project is not None
    assert result.source == "recovery"
    assert json.loads(layout.project_file.read_text(encoding="utf-8"))["script"]["source_text"] == "Recovered work."
    assert len(layout.backup_files()) == backups_before + 1, "the old file must be backed up first"
    assert not layout.autosave_file.exists(), "the recovery file is retired after a restore"


def test_restoring_twice_is_not_possible_from_the_same_file(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Once")
    store.save(project, layout)
    project.script.source_text = "Work."
    store.autosave(project, layout)

    candidate = store.detect_recovery(layout)
    assert store.apply_recovery(candidate, layout).ok
    assert store.detect_recovery(layout) is None


def test_ignoring_a_recovery_file_keeps_it_on_disk(tmp_path) -> None:
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Ignored")
    store.save(project, layout)
    project.script.source_text = "Work."
    store.autosave(project, layout)

    candidate = store.detect_recovery(layout)
    assert store.ignore_recovery(candidate) is True

    assert store.detect_recovery(layout) is None
    kept = list(layout.autosave_dir.iterdir())
    assert kept, "ignoring must not delete the recovery data"
    assert kept[0].name.endswith(".ignored.json")


def test_recovery_never_overwrites_good_data_with_bad(tmp_path) -> None:
    """A damaged recovery file must not replace a healthy project."""
    layout = make_layout(tmp_path)
    store = ProjectStore()
    project = build_project("Healthy")
    store.save(project, layout)
    healthy = layout.project_file.read_text(encoding="utf-8")

    layout.autosave_dir.mkdir(parents=True, exist_ok=True)
    layout.autosave_file.write_text('{"project": {"name": "trunc', encoding="utf-8")

    from app.project.store import RecoveryCandidate

    candidate = RecoveryCandidate(kind="autosave", path=layout.autosave_file, modified_at=time.time())
    result = store.apply_recovery(candidate, layout)

    assert not result.ok
    assert layout.project_file.read_text(encoding="utf-8") == healthy
    assert not store.detect_recovery(layout), "a damaged autosave must not keep being offered"
