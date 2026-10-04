"""Cache and temporary-file maintenance (directive sections 31, 72).

Rules that protect user data:

* Only folders listed in :data:`CLEARABLE_TARGETS` can ever be cleared.
* Every delete is guarded by :func:`is_within`, so a path that resolves outside
  its cache folder (for example a symlink pointing at a project) is skipped.
* Project folders, assets, themes, models and exported videos are never touched
  - they are not in the list at all, which is the strongest guarantee we can
  make in code.
* Deletion is counted and reported, so the UI can say exactly what happened
  instead of "cache cleared".
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import atomicio
from .atomicio import human_size
from .events import Event
from .logging_setup import get_logger, log_event
from .paths import CLEARABLE_DIRECTORIES, is_within

LOGGER = get_logger("maintenance")

#: Named cleanup targets exposed in the UI.  Each maps to one or more folders
#: inside the data root; nothing outside this mapping is ever deleted.
CLEARABLE_TARGETS: dict[str, tuple[str, ...]] = {
    "preview_cache": ("previews",),
    "render_cache": ("cache",),
    "temp_files": ("temp",),
    "workspace": ("workspace",),
}

TARGET_LABELS: dict[str, str] = {
    "preview_cache": "Preview cache",
    "render_cache": "Render cache",
    "temp_files": "Temporary files",
    "workspace": "Working files",
}


@dataclass
class CleanupOutcome:
    """What a cleanup run actually removed."""

    target: str
    removed_files: int = 0
    removed_folders: int = 0
    freed_bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return TARGET_LABELS.get(self.target, self.target)

    def summary(self) -> str:
        if self.removed_files == 0 and self.removed_folders == 0 and not self.errors:
            return f"{self.label}: nothing to remove"
        text = f"{self.label}: removed {self.removed_files} file(s), freed {human_size(self.freed_bytes)}"
        if self.errors:
            text += f", {len(self.errors)} item(s) could not be removed"
        return text


@dataclass
class CleanupReport:
    """All outcomes of one cleanup run."""

    outcomes: list[CleanupOutcome] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def freed_bytes(self) -> int:
        return sum(outcome.freed_bytes for outcome in self.outcomes)

    @property
    def removed_files(self) -> int:
        return sum(outcome.removed_files for outcome in self.outcomes)

    @property
    def errors(self) -> list[str]:
        errors: list[str] = []
        for outcome in self.outcomes:
            errors.extend(outcome.errors)
        return errors

    def summary(self) -> str:
        if self.removed_files == 0 and not self.errors:
            return "Nothing needed cleaning - caches are already empty."
        text = f"Removed {self.removed_files} file(s) and freed {human_size(self.freed_bytes)}."
        if self.errors:
            text += f" {len(self.errors)} item(s) could not be removed (they may be in use)."
        return text


def clear_folder_safely(directory: Path, allowed_root: Path, target: str) -> CleanupOutcome:
    """Delete the *contents* of *directory* (never the folder itself).

    *directory* must be inside *allowed_root*; anything else is refused and
    recorded as skipped.
    """
    outcome = CleanupOutcome(target=target)
    directory = Path(directory)
    allowed_root = Path(allowed_root)

    if not is_within(directory, allowed_root):
        outcome.skipped.append(f"{directory} is outside the application data folder")
        log_event(
            Event.WARNING,
            "Cleanup refused: path is outside the data folder",
            logger=LOGGER,
            path=str(directory),
            root=str(allowed_root),
        )
        return outcome

    if not directory.exists():
        return outcome

    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        # Defensive: never follow a link that points outside the cache folder.
        if entry.is_symlink() or not is_within(entry, allowed_root):
            outcome.skipped.append(str(entry))
            continue
        try:
            if entry.is_dir():
                size = atomicio.directory_size_bytes(entry)
                shutil.rmtree(entry, ignore_errors=False)
                outcome.removed_folders += 1
                outcome.freed_bytes += size
            else:
                size = entry.stat().st_size
                entry.unlink()
                outcome.removed_files += 1
                outcome.freed_bytes += size
        except OSError as exc:
            outcome.errors.append(f"{entry.name}: {exc}")

    return outcome


def run_cleanup(
    paths,
    targets: Sequence[str],
    progress=None,
) -> CleanupReport:
    """Clear the requested targets.  Unknown target names are ignored."""
    started = time.time()
    report = CleanupReport()

    valid_targets = [name for name in targets if name in CLEARABLE_TARGETS]
    if progress is not None:
        progress.start(total=len(valid_targets), message="Cleaning up...", unit="folders")

    for index, name in enumerate(valid_targets, start=1):
        if progress is not None:
            progress.update(current=index - 1, message=TARGET_LABELS.get(name, name))
        merged = CleanupOutcome(target=name)
        for folder_name in CLEARABLE_TARGETS[name]:
            directory = Path(paths.data_root) / folder_name
            outcome = clear_folder_safely(directory, Path(paths.data_root), name)
            merged.removed_files += outcome.removed_files
            merged.removed_folders += outcome.removed_folders
            merged.freed_bytes += outcome.freed_bytes
            merged.skipped.extend(outcome.skipped)
            merged.errors.extend(outcome.errors)
        report.outcomes.append(merged)

    report.duration_seconds = time.time() - started
    if progress is not None:
        progress.finish(report.summary())

    log_event(
        Event.CACHE_CLEARED,
        report.summary(),
        logger=LOGGER,
        targets=",".join(valid_targets),
        files=report.removed_files,
        freed_bytes=report.freed_bytes,
        errors=len(report.errors),
    )
    return report


def storage_report(paths) -> dict[str, object]:
    """Size of each cache/temp folder, for the Maintenance page."""
    from . import env as env_module

    rows: dict[str, object] = {}
    for name in CLEARABLE_DIRECTORIES:
        directory = Path(paths.data_root) / name
        size_bytes = atomicio.directory_size_bytes(directory)
        rows[name] = {
            "path": str(directory),
            "size_bytes": size_bytes,
            "size": human_size(size_bytes),
        }
    free_bytes = env_module.disk_free_bytes(Path(paths.data_root))
    rows["free_space"] = {"bytes": free_bytes, "text": human_size(free_bytes)}
    return rows


def cleanup_stale_temporaries(paths, max_age_minutes: int = 720) -> int:
    """Remove abandoned ``*.tmp`` files from the app's own temp folders.

    Called once at startup - it only ever looks inside ``temp/`` and ``cache/``.
    """
    removed = 0
    for name in ("temp", "cache"):
        directory = Path(paths.data_root) / name
        removed += atomicio.cleanup_stale_temp_files(directory, max_age_minutes=max_age_minutes)
    if removed:
        log_event(Event.TEMP_CLEANED, f"Removed {removed} stale temporary file(s)", logger=LOGGER, files=removed)
    return removed


def total_clearable_size(paths) -> int:
    return sum(atomicio.directory_size_bytes(Path(paths.data_root) / name) for name in CLEARABLE_DIRECTORIES)
