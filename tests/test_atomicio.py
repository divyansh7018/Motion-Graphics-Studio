"""Atomic writes, backups and quarantine (directive sections 12, 36)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.atomicio import (
    FileWriteError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    backup_file,
    cleanup_stale_temp_files,
    directory_size_bytes,
    human_size,
    load_json,
    prune_backups,
    save_with_backup,
)


def test_atomic_write_creates_file_without_temp_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.glob(".*tmp")) == []


def test_atomic_write_replaces_previous_content(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_creates_parent_folders(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"


def test_atomic_write_reports_unwritable_location(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "blocked" / "file.txt"

    def exploding_mkdir(self, *args, **kwargs):  # noqa: ANN001
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "mkdir", exploding_mkdir)
    with pytest.raises(FileWriteError) as excinfo:
        atomic_write_text(target, "x")
    assert "Access is denied" in str(excinfo.value) or "refused access" in str(excinfo.value).lower()


def test_save_with_backup_keeps_previous_version(tmp_path: Path) -> None:
    target = tmp_path / "project.json"
    backups = tmp_path / "backups"
    save_with_backup(target, {"version": 1}, backup_dir=backups)
    save_with_backup(target, {"version": 2}, backup_dir=backups)

    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 2
    kept = list(backups.glob("project.*.json"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text(encoding="utf-8"))["version"] == 1


def test_prune_backups_keeps_newest(tmp_path: Path) -> None:
    for index in range(5):
        path = tmp_path / f"file.2024010{index}-120000.json"
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (1_700_000_000 + index * 100, 1_700_000_000 + index * 100))

    removed = prune_backups(tmp_path, "file", keep=2)

    assert len(removed) == 3
    remaining = sorted(path.name for path in tmp_path.glob("file.*"))
    assert remaining == ["file.20240103-120000.json", "file.20240104-120000.json"]


def test_backup_file_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert backup_file(tmp_path / "nope.json") is None


def test_load_json_reports_missing_file(tmp_path: Path) -> None:
    result = load_json(tmp_path / "nope.json")
    assert result.ok is False
    assert result.exists is False


def test_load_json_quarantines_corrupt_file(tmp_path: Path) -> None:
    target = tmp_path / "broken.json"
    target.write_text("{ not json", encoding="utf-8")
    quarantine = tmp_path / "damaged"

    result = load_json(target, quarantine_dir=quarantine)

    assert result.ok is False
    assert "invalid JSON" in (result.error or "")
    assert result.corrupted_copy is not None and result.corrupted_copy.exists()
    # The original is left untouched.
    assert target.read_text(encoding="utf-8") == "{ not json"


def test_cleanup_stale_temp_files_only_removes_old_files(tmp_path: Path) -> None:
    old = tmp_path / "old.tmp"
    new = tmp_path / "new.tmp"
    keep = tmp_path / "important.json"
    for path in (old, new, keep):
        path.write_text("x", encoding="utf-8")
    os.utime(old, (1_600_000_000, 1_600_000_000))

    removed = cleanup_stale_temp_files(tmp_path, max_age_minutes=60)

    assert removed == 1
    assert not old.exists()
    assert new.exists(), "recent temporary files may belong to a running task"
    assert keep.exists(), "only *.tmp files may be removed"


def test_human_size_formatting() -> None:
    assert human_size(0) == "0 B"
    assert human_size(999) == "999 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024 * 1024 * 3) == "3.0 MB"
    assert human_size(-5) == "0 B"


def test_directory_size_counts_nested_files(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"12345")
    (tmp_path / "sub" / "b.txt").write_bytes(b"123")
    assert directory_size_bytes(tmp_path) == 8
