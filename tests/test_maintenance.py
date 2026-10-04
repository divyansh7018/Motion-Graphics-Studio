"""Cache cleanup safety (directive sections 31, 72).

The most important property in this file is negative: cleanup must never delete
anything outside the cache folders - especially not projects, assets or exported
videos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.maintenance import (
    CLEARABLE_TARGETS,
    clear_folder_safely,
    cleanup_stale_temporaries,
    run_cleanup,
    storage_report,
    total_clearable_size,
)
from app.core.paths import CLEARABLE_DIRECTORIES, PROTECTED_DIRECTORIES


def _seed(paths) -> None:
    """Create one file in every folder so deletion can be observed."""
    (paths.previews_dir / "preview.mp4").write_bytes(b"preview")
    (paths.cache_dir / "frames").mkdir()
    (paths.cache_dir / "frames" / "f0001.png").write_bytes(b"frame")
    (paths.temp_dir / "leftover.tmp").write_bytes(b"temp")
    (paths.workspace_dir / "scratch.json").write_text("{}", encoding="utf-8")

    (paths.projects_dir / "My Project").mkdir()
    (paths.projects_dir / "My Project" / "project.json").write_text('{"name": "keep me"}', encoding="utf-8")
    (paths.assets_dir / "logo.png").write_bytes(b"png")
    (paths.output_dir / "Video.mp4").write_bytes(b"final video")
    (paths.themes_dir / "theme.json").write_text("{}", encoding="utf-8")


def test_cleanup_removes_only_cache_contents(paths) -> None:
    _seed(paths)

    report = run_cleanup(paths, ["preview_cache", "render_cache", "temp_files", "workspace"])

    assert report.removed_files >= 3
    assert report.freed_bytes > 0

    # Cleared
    assert list(paths.previews_dir.iterdir()) == []
    assert list(paths.cache_dir.iterdir()) == []
    assert list(paths.temp_dir.iterdir()) == []
    assert list(paths.workspace_dir.iterdir()) == []

    # Untouched - these are the files that matter
    assert (paths.projects_dir / "My Project" / "project.json").read_text(encoding="utf-8") == '{"name": "keep me"}'
    assert (paths.assets_dir / "logo.png").exists()
    assert (paths.output_dir / "Video.mp4").exists()
    assert (paths.themes_dir / "theme.json").exists()
    # The cache folder itself must survive (it is part of the layout).
    assert paths.cache_dir.is_dir()


def test_cleanup_never_touches_folders_outside_the_data_root(tmp_path: Path, paths) -> None:
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    (outside / "valuable.txt").write_text("keep", encoding="utf-8")

    outcome = clear_folder_safely(outside, paths.data_root, "preview_cache")

    assert outcome.removed_files == 0
    assert outcome.skipped
    assert (outside / "valuable.txt").exists(), "cleanup must refuse paths outside the data folder"


def test_cleanup_skips_symbolic_links_pointing_outside(tmp_path: Path, paths) -> None:
    outside = tmp_path / "linked-target"
    outside.mkdir()
    (outside / "important.txt").write_text("keep", encoding="utf-8")

    link = paths.previews_dir / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
        pytest.skip("symbolic links are not available on this system")

    outcome = clear_folder_safely(paths.previews_dir, paths.data_root, "preview_cache")

    assert outcome.skipped
    assert (outside / "important.txt").exists()


def test_cleanup_on_missing_folder_is_harmless(paths) -> None:
    import shutil

    shutil.rmtree(paths.temp_dir)
    report = run_cleanup(paths, ["temp_files"])
    assert report.removed_files == 0
    assert report.errors == []


def test_unknown_target_is_ignored(paths) -> None:
    report = run_cleanup(paths, ["definitely-not-a-target"])
    assert report.outcomes == []


def test_protected_and_clearable_lists_do_not_overlap() -> None:
    assert not set(PROTECTED_DIRECTORIES) & set(CLEARABLE_DIRECTORIES)
    for target, folders in CLEARABLE_TARGETS.items():
        for folder in folders:
            assert folder in CLEARABLE_DIRECTORIES, f"{target} points at a non-clearable folder"


def test_storage_report_lists_every_clearable_folder(paths) -> None:
    rows = storage_report(paths)
    for name in CLEARABLE_DIRECTORIES:
        assert name in rows
    assert "free_space" in rows


def test_total_clearable_size_adds_up(paths) -> None:
    _seed(paths)
    total = total_clearable_size(paths)
    assert total >= len("preview") + len("frame") + len("temp")


def test_stale_temporaries_are_cleaned_only_in_temp_and_cache(paths) -> None:
    import os

    old_temp = paths.temp_dir / "abandoned.tmp"
    old_temp.write_text("x", encoding="utf-8")
    os.utime(old_temp, (1_600_000_000, 1_600_000_000))

    old_project = paths.projects_dir / "abandoned.tmp"
    old_project.write_text("x", encoding="utf-8")
    os.utime(old_project, (1_600_000_000, 1_600_000_000))

    removed = cleanup_stale_temporaries(paths, max_age_minutes=60)

    assert removed == 1
    assert old_project.exists(), "temporary-file cleanup must never run inside the projects folder"


def test_cleanup_report_summary_is_readable(paths) -> None:
    _seed(paths)
    report = run_cleanup(paths, ["preview_cache"])
    assert "freed" in report.summary() or "nothing to remove" in report.summary()
