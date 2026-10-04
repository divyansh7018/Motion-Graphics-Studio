"""Atomic, crash-safe file I/O helpers.

Directive sections 12 and 36 require that a failure while writing never
destroys valid data:

* writes go to a temporary file **in the same directory** (so ``os.replace`` is
  atomic on the same volume),
* data is flushed and ``fsync``-ed before the replace,
* the previous version of a file is copied into a backup folder before it is
  replaced,
* corrupt JSON is detected and preserved (never silently dropped).

This module performs no I/O at import time and is safe to import from workers.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class FileWriteError(Exception):
    """Raised when a file could not be written safely."""


class JsonFileError(Exception):
    """Raised when a JSON file exists but cannot be parsed."""


# --------------------------------------------------------------------------
# Atomic writes
# --------------------------------------------------------------------------

def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write *data* to *path* atomically.

    The temporary file is created in the destination directory so the final
    ``os.replace`` is a same-volume rename (atomic on Windows and POSIX).
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FileWriteError(f"Could not create folder '{path.parent}': {exc}") from exc

    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            fd = -1  # ownership transferred to the file object
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
        return path
    except OSError as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name:
            _silent_unlink(tmp_name)
        raise FileWriteError(_explain_os_error(path, exc)) from exc


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """Atomic text write (UTF-8 by default, newline preserved as given)."""
    return atomic_write_bytes(Path(path), text.encode(encoding))


def atomic_write_json(path: Path, payload: Any, indent: int = 2) -> Path:
    """Atomic JSON write with a trailing newline and stable key order."""
    text = json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False)
    if not text.endswith("\n"):
        text += "\n"
    return atomic_write_text(path, text)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync so a rename survives a power loss.

    Not supported on Windows; failures are ignored on purpose.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _silent_unlink(path: Path | str) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _explain_os_error(path: Path, exc: OSError) -> str:
    """Translate common OS errors into an actionable sentence."""
    if exc.errno == errno.ENOSPC:
        return (
            f"Not enough free disk space to save '{path.name}'. "
            "Free some space (or move the data folder to another drive) and try again."
        )
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"Windows refused access to '{path}'. The folder may be read-only or "
            "protected (for example inside Program Files). Choose a different data folder "
            "in Settings, or run the app as administrator once."
        )
    return f"Could not write '{path}': {exc}"


# --------------------------------------------------------------------------
# JSON reads
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JsonLoadResult:
    """Outcome of a tolerant JSON load."""

    data: Optional[Any]
    exists: bool
    ok: bool
    error: Optional[str] = None
    corrupted_copy: Optional[Path] = None


def load_json(path: Path, default: Any = None, quarantine_dir: Optional[Path] = None) -> JsonLoadResult:
    """Load JSON tolerantly.

    * missing file  -> ``ok=False, exists=False`` (caller decides defaults)
    * corrupt file  -> ``ok=False, exists=True`` and, when *quarantine_dir* is
      given, the corrupt file is copied there so no data is lost.
    """
    path = Path(path)
    if not path.exists():
        return JsonLoadResult(data=default, exists=False, ok=False, error="file does not exist")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return JsonLoadResult(data=default, exists=True, ok=False, error=f"could not read file: {exc}")
    if not raw.strip():
        return JsonLoadResult(data=default, exists=True, ok=False, error="file is empty")
    try:
        return JsonLoadResult(data=json.loads(raw), exists=True, ok=True)
    except json.JSONDecodeError as exc:
        corrupted = None
        if quarantine_dir is not None:
            corrupted = _quarantine(path, Path(quarantine_dir))
        return JsonLoadResult(
            data=default,
            exists=True,
            ok=False,
            error=f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            corrupted_copy=corrupted,
        )


def read_json_or_raise(path: Path) -> Any:
    """Strict JSON read used by tests and importers."""
    result = load_json(path)
    if not result.ok:
        raise JsonFileError(f"{path}: {result.error}")
    return result.data


def _quarantine(path: Path, quarantine_dir: Path) -> Optional[Path]:
    """Copy a damaged file aside so a later repair is possible."""
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = unique_backup_path(quarantine_dir, f"{path.name}.corrupt-{stamp}")
        shutil.copy2(path, target)
        return target
    except OSError:
        return None


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def timestamp_slug(when: Optional[datetime] = None) -> str:
    when = datetime.now(timezone.utc) if when is None else when
    return when.strftime("%Y%m%d-%H%M%S")


def unique_backup_path(directory: Path, stem: str) -> Path:
    candidate = Path(directory) / stem
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = Path(directory) / f"{stem}_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def backup_file(path: Path, backup_dir: Optional[Path] = None, keep: int = 10) -> Optional[Path]:
    """Copy *path* to a timestamped backup and prune old backups.

    Returns the backup path, or ``None`` when the file does not exist or the
    backup could not be taken (a backup failure must never block a save).
    """
    path = Path(path)
    if not path.exists():
        return None
    directory = Path(backup_dir) if backup_dir is not None else path.parent / "backups"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = unique_backup_path(directory, f"{path.stem}.{timestamp_slug()}{path.suffix}")
        shutil.copy2(path, target)
        prune_backups(directory, path.stem, keep=keep)
        return target
    except OSError:
        return None


def prune_backups(directory: Path, stem: str, keep: int = 10) -> list[Path]:
    """Delete the oldest backups for *stem*, keeping the newest *keep* ones."""
    if keep <= 0:
        return []
    directory = Path(directory)
    try:
        candidates = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.name.startswith(stem)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    removed: list[Path] = []
    for old in candidates[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            continue
    return removed


def save_with_backup(
    path: Path,
    data: Any,
    backup_dir: Optional[Path] = None,
    keep_backups: int = 10,
    as_json: bool = True,
) -> Path:
    """Back up the current file (when present) and atomically write new data."""
    path = Path(path)
    if path.exists():
        backup_file(path, backup_dir=backup_dir, keep=keep_backups)
    if as_json:
        return atomic_write_json(path, data)
    if isinstance(data, bytes):
        return atomic_write_bytes(path, data)
    return atomic_write_text(path, str(data))


# --------------------------------------------------------------------------
# Temporary file hygiene
# --------------------------------------------------------------------------

def cleanup_stale_temp_files(directory: Path, max_age_minutes: int = 720, pattern: str = "*.tmp") -> int:
    """Delete abandoned ``*.tmp`` files older than *max_age_minutes*.

    Only used for the application's own temp/cache folders - never for user
    project folders.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_minutes * 60
    removed = 0
    try:
        entries = list(directory.glob(pattern))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def directory_size_bytes(directory: Path) -> int:
    """Total size of the files inside *directory* (non-recursive errors ignored)."""
    total = 0
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    try:
        for root, _dirs, files in os.walk(directory):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def human_size(num_bytes: int) -> str:
    """Format a byte count for the UI (``1.4 GB``)."""
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
