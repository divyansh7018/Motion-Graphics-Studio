"""The Video Library (directive sections 14, 15, 29, 30, 42).

What this is
------------
A folder of finished clips plus an index and a thumbnail cache.  The index is a
*cache*: it can be deleted at any time and rebuilt by scanning, and every number
in it was measured from the file with FFprobe - never guessed from the name.

Three kinds of clip live here, and they stay distinct (section 30):

* ``generated``  - what an AI backend produced;
* ``render``     - what the renderer produced (a copy the user chose to keep);
* ``imported``   - a clip the user brought in from somewhere else.

Listing a folder of videos never opens one: a page of entries is served from the
index, metadata is measured once and cached against the file's size and
modification time, and a thumbnail is a separate, cancellable job.

The library never deletes a file unless it is explicitly asked to, and never
writes over one: an entry that is added twice is the same entry, and a thumbnail
for a replaced file is regenerated rather than shown stale.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event
from .models import human_bytes

__all__ = [
    "VideoEntry", "VideoLibrary", "LibraryPage", "LibraryQuery",
    "MediaProbeCache", "VideoThumbnailCache", "VIDEO_SUFFIXES",
    "ENTRY_READY", "ENTRY_MISSING", "ENTRY_UNREADABLE", "probe_state",
    "_binary_path",
    "LIBRARY_SORTS", "LIBRARY_SOURCES", "SOURCE_LABELS",
    "probe_video_file",
]

INDEX_FILE = "videos.json"
PROBE_CACHE_FILE = "media_cache.json"

#: What counts as a video in the library folder.
VIDEO_SUFFIXES: tuple[str, ...] = (
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv")

#: ``generated`` (an AI backend), ``render`` (the renderer), ``imported`` (you).
LIBRARY_SOURCES: tuple[str, ...] = ("generated", "render", "imported")

SOURCE_LABELS: dict[str, str] = {
    "generated": "Generated",
    "render": "Rendered",
    "imported": "Imported",
}

#: Sort orders the interface offers; the value is what the user reads.
LIBRARY_SORTS: dict[str, str] = {
    "recent": "Newest first",
    "oldest": "Oldest first",
    "name": "Name",
    "duration": "Longest first",
    "size": "Largest first",
    "resolution": "Highest resolution",
}

#: An entry's own state, so the list can show a file that has gone.
ENTRY_READY = "READY"
ENTRY_MISSING = "MISSING"
ENTRY_UNREADABLE = "UNREADABLE"


def _new_id() -> str:
    return f"vid_{uuid.uuid4().hex[:10]}"


def _fingerprint(path: Path) -> str:
    """Size and modification time: enough to know a file has been replaced."""
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def probe_video_file(path: Any, tools: Any = None) -> dict:
    """Measure one file with FFprobe through the shared validator.

    Returns ``{}`` when the file cannot be measured, so a caller always knows
    the difference between "measured" and "not measured" (section 15).  Use
    :func:`probe_state` when the *reason* matters.
    """
    return probe_state(path, tools)[1]


def probe_state(path: Any, tools: Any = None) -> tuple[str, dict]:
    """Measure a file and say how it went.

    Returns ``(state, metadata)`` where the state is one of:

    ``ok``                 - measured; the metadata is real;
    ``check_not_available``- it exists, but it cannot be measured here (no
                             FFprobe): the clip is fine, the *check* is missing;
    ``unreadable``         - it exists and the checker refused it, so it is not
                             a usable video file.

    The difference matters: a machine without FFmpeg must not label every clip in
    the library as broken (section 15).
    """
    from .video_validation import (VIDEO_CHECK_NOT_AVAILABLE,
                                   validate_video_file)

    target = Path(str(path))
    try:
        check = validate_video_file(target, tools=tools)
    except Exception as exc:  # noqa: BLE001 - measuring never raises to a caller
        log_event("VIDEO_LIBRARY_PROBE_FAILED",
                  f"{target.name} could not be measured: {exc}", level="WARNING")
        return "check_not_available", {}
    if getattr(check, "code", "") == VIDEO_CHECK_NOT_AVAILABLE:
        return "check_not_available", {}
    if not getattr(check, "measured_with", ""):
        return "unreadable", {}
    return "ok", {
        "duration": float(check.duration or 0.0),
        "width": int(check.width or 0), "height": int(check.height or 0),
        "fps": float(check.fps or 0.0), "frames": int(check.frames or 0),
        "codec": str(check.video_codec or ""),
        "audio_codec": str(check.audio_codec or ""),
        "has_audio": bool(check.has_audio),
        "size_bytes": int(check.size_bytes or 0),
        "measured_with": str(check.measured_with or ""),
        "measured_at": time.time(),
    }


# ---------------------------------------------------------------------------
# measured metadata, cached against the file
# ---------------------------------------------------------------------------

class MediaProbeCache:
    """Measured clip metadata, kept while the file has not changed.

    FFprobe is fast, but listing a folder and measuring every file in it is not,
    so each measurement is stored with the file's size and modification time.
    Replacing the file invalidates its entry, which is the only correct
    behaviour - showing an old duration for a new file would be a lie
    (section 15).
    """

    def __init__(self, path: Any = None, *, limit: int = 4000) -> None:
        self.path = Path(path) if path else None
        self.limit = max(50, int(limit or 4000))
        self._records: dict[str, dict] = {}
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> dict[str, dict]:
        if self.loaded:
            return self._records
        self.loaded = True
        if self.path is None or not self.path.is_file():
            return self._records
        try:
            from ..core.atomicio import load_json

            result = load_json(self.path, default={},
                               quarantine_dir=self.path.parent / "corrupt")
            data = result.data if getattr(result, "data", None) is not None else {}
        except Exception as exc:  # noqa: BLE001 - a cache is never fatal
            log_event("VIDEO_PROBE_CACHE_UNREADABLE",
                      f"The clip metadata cache could not be read: {exc}",
                      level="WARNING")
            return self._records
        for key, record in (data.get("files") or {}).items():
            if isinstance(record, dict) and record.get("fingerprint"):
                self._records[str(key)] = record
        return self._records

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.path, {"schema_version": 1,
                                         "files": self._records})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("VIDEO_PROBE_CACHE_SAVE_FAILED",
                      f"The clip metadata cache could not be saved: {exc}",
                      level="WARNING")
            return False

    # -- reading -----------------------------------------------------------

    def cached(self, path: Any) -> Optional[dict]:
        """What was measured before, or None when this file has changed."""
        target = Path(str(path))
        record = self.load().get(str(target))
        if not record:
            return None
        if record.get("fingerprint") != _fingerprint(target):
            return None
        return dict(record.get("metadata") or {})

    def measure(self, path: Any, tools: Any = None, *, force: bool = False) -> dict:
        """Measured metadata for one file, from the cache when it still fits."""
        target = Path(str(path))
        if not force:
            known = self.cached(target)
            if known:
                return dict(known)
        state, metadata = probe_state(target, tools)
        if metadata:
            self.load()
            self._records[str(target)] = {"fingerprint": _fingerprint(target),
                                          "metadata": dict(metadata),
                                          "path": str(target)}
            self._trim()
            self.save()
        return metadata

    def invalidate(self, path: Any) -> bool:
        self.load()
        return self._records.pop(str(Path(str(path))), None) is not None

    def clear(self) -> int:
        count = len(self.load())
        self._records = {}
        self.loaded = True
        self.save()
        return count

    def _trim(self) -> None:
        if len(self._records) <= self.limit:
            return
        ordered = sorted(self._records.items(),
                         key=lambda item: float(
                             (item[1].get("metadata") or {}).get("measured_at", 0.0)),
                         reverse=True)
        self._records = dict(ordered[:self.limit])


# ---------------------------------------------------------------------------
# thumbnails
# ---------------------------------------------------------------------------

class VideoThumbnailCache:
    """One-frame previews, made with FFmpeg and cached on disk.

    Keyed by the file's size and modification time, so replacing a clip
    invalidates its picture instead of showing the old one.
    """

    def __init__(self, cache_dir: Any, *, height: int = 180) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.height = max(48, int(height or 180))

    def key_for(self, video_path: Any) -> str:
        target = Path(str(video_path))
        digest = hashlib.sha256(
            f"{target}|{_fingerprint(target)}".encode("utf-8")).hexdigest()[:24]
        return digest

    def path_for(self, video_path: Any) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{self.key_for(video_path)}.jpg"

    def get(self, video_path: Any, tools: Any = None, *, at: float = 0.1,
            cancel: Any = None, regenerate: bool = False) -> Optional[Path]:
        """The cached picture, or None when one could not be made."""
        target = self.path_for(video_path)
        if target is None:
            return None
        if target.is_file() and not regenerate:
            return target
        return self.create(video_path, tools, at=at, cancel=cancel)

    def create(self, video_path: Any, tools: Any = None, *, at: float = 0.1,
               cancel: Any = None) -> Optional[Path]:
        """Extract one frame.  Slow, so callers run this in a job (section 41)."""
        target = self.path_for(video_path)
        source = Path(str(video_path))
        if target is None or not source.is_file():
            return None
        binary = _ffmpeg_binary(tools)
        if not binary:
            log_event("VIDEO_THUMBNAIL_NO_FFMPEG",
                      "A thumbnail could not be made: FFmpeg was not found",
                      level="WARNING", path=str(source))
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part.jpg")
        from ..core.process import run_process

        outcome = run_process(
            [binary, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{max(0.0, float(at)):.3f}", "-i", str(source),
             "-frames:v", "1",
             "-vf", f"scale=-2:{self.height}", str(temporary)],
            timeout=120.0, cancel=cancel)
        if not outcome.ok or not temporary.is_file():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - cleanup is best effort
                pass
            log_event("VIDEO_THUMBNAIL_FAILED",
                      f"A thumbnail could not be made for {source.name}",
                      level="WARNING", error=outcome.error or outcome.output[-200:])
            return None
        try:
            temporary.replace(target)
        except OSError:  # pragma: no cover - target locked
            return None
        return target

    def clear(self) -> int:
        removed = 0
        if self.cache_dir is None or not self.cache_dir.is_dir():
            return 0
        for path in self.cache_dir.rglob("*.jpg"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


def _binary_path(value: Any) -> str:
    """The executable path inside whatever a caller handed in.

    Three shapes reach here: an :class:`FFmpegTools` wrapper (its ``ffmpeg`` is
    already a :class:`Path`), an :class:`FFmpegDiscovery` (its ``ffmpeg`` is a
    :class:`ToolInfo`), and a plain path or string.  Getting this wrong passes
    the *repr* of a tool object to the operating system, which fails every time
    with "the program could not be started" - so it is one helper, tested.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (str, Path)):
        return str(value)
    inner = getattr(value, "path", None)
    if inner:
        return str(inner)
    return str(value)


def _ffmpeg_binary(tools: Any = None) -> str:
    """The FFmpeg executable to use, discovered the same way everything else is."""
    if tools is not None:
        found = _binary_path(getattr(tools, "ffmpeg", ""))
        if found:
            return found
        # An FFmpegTools-style object that also carries the discovery result.
        found = _binary_path(getattr(getattr(tools, "discovery", None),
                                     "ffmpeg", None))
        if found:
            return found
    try:
        from ..tools.ffmpeg import discover_ffmpeg

        return _binary_path(getattr(discover_ffmpeg(), "ffmpeg", ""))
    except Exception:  # noqa: BLE001 - no FFmpeg is a normal machine state
        return ""


# ---------------------------------------------------------------------------
# one entry
# ---------------------------------------------------------------------------

@dataclass
class VideoEntry:
    """One clip in the library, with what was measured and what was recorded."""

    id: str = ""
    path: str = ""
    name: str = ""
    #: ``generated`` | ``render`` | ``imported`` (section 30).
    source: str = "imported"
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frames: int = 0
    codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    size_bytes: int = 0
    container: str = ""
    bitrate: int = 0
    created: float = field(default_factory=time.time)
    added: float = field(default_factory=time.time)
    # -- provenance (sections 21, 22, 100) --------------------------------
    provider: str = ""
    backend: str = ""
    model: str = ""
    prompt: str = ""
    seed: int = 0
    parameters: dict = field(default_factory=dict)
    parent: str = ""
    project: str = ""
    scene: str = ""
    #: True only when something recorded that a **real** AI model made this
    #: clip.  Imports, renders and test backends must never claim it.
    is_ai_model: bool = False
    label: str = ""
    # -- library metadata --------------------------------------------------
    collection: str = ""
    tags: list = field(default_factory=list)
    favourite: bool = False
    thumbnail: str = ""
    checksum: str = ""
    status: str = ENTRY_READY
    measured_with: str = ""
    notes: str = ""

    # -- display -----------------------------------------------------------

    @property
    def filename(self) -> str:
        return Path(self.path).name if self.path else "(gone)"

    def display_name(self) -> str:
        return self.name or Path(self.path).stem if self.path else self.name

    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    def duration_label(self) -> str:
        if not self.duration:
            return ""
        minutes, seconds = divmod(int(round(self.duration)), 60)
        return f"{minutes}:{seconds:02d}" if minutes else f"{seconds}s"

    def size_label(self) -> str:
        return human_bytes(self.size_bytes) if self.size_bytes else ""

    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def measurements(self) -> str:
        """One line of measured facts, for the list and the CLI."""
        parts = [part for part in (self.resolution(), self.duration_label(),
                                   f"{self.fps:g} fps" if self.fps else "",
                                   self.codec.upper() if self.codec else "",
                                   self.size_label()) if part]
        return ", ".join(parts)

    def provenance(self) -> str:
        """Who made it, in words a user can check (section 22)."""
        if self.source == "generated":
            who = self.label or self.backend or self.provider
            if not who:
                # Say so rather than inventing a maker (section 22).
                return "Generated (the backend was not recorded)"
            model = f" ({self.model})" if self.model else ""
            return f"Generated by {who}{model}"
        if self.source == "render":
            return "Rendered from the project"
        return "Imported from a file"

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data.update({"filename": self.filename, "resolution": self.resolution(),
                     "duration_label": self.duration_label(),
                     "measurements": self.measurements(),
                     "source_label": self.source_label(),
                     "provenance": self.provenance()})
        return data

    def update_from_probe(self, metadata: dict) -> None:
        """Fill in the measured numbers from one probe result."""
        for key in ("duration", "width", "height", "fps", "frames", "codec",
                    "audio_codec", "has_audio", "size_bytes", "measured_with"):
            if key in metadata and metadata[key] not in (None, ""):
                setattr(self, key, metadata[key])
        measured_at = metadata.get("measured_at")
        if measured_at and not self.created:
            self.created = float(measured_at)

    def to_metadata(self) -> dict:
        """The provenance block a project asset carries (sections 22, 26)."""
        return {
            "library_id": self.id, "file": self.filename,
            "source": self.source, "generated": self.source == "generated",
            "provider": self.provider, "backend": self.backend,
            "model": self.model, "prompt": self.prompt, "seed": self.seed,
            "is_ai_model": self.is_ai_model, "label": self.label,
            "duration": self.duration, "fps": self.fps,
            "width": self.width, "height": self.height,
            "video_codec": self.codec, "has_audio": self.has_audio,
            "size_bytes": self.size_bytes, "collection": self.collection,
            "tags": list(self.tags or []), "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VideoEntry":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in known})


@dataclass
class LibraryQuery:
    """A search over the library (section 14)."""

    text: str = ""
    source: str = ""
    collection: str = ""
    tag: str = ""
    project: str = ""
    favourites_only: bool = False
    missing_only: bool = False
    min_duration: float = 0.0
    max_duration: float = 0.0
    sort: str = "recent"
    #: Page controls: a folder of thousands of clips is never all loaded.
    limit: int = 200
    offset: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class LibraryPage:
    """One page of results, with the total so the UI can say "1-200 of 4123"."""

    entries: list = field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 200

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.entries) < self.total

    def to_dict(self) -> dict:
        return {"entries": [entry.to_dict() for entry in self.entries],
                "total": self.total, "offset": self.offset, "limit": self.limit,
                "has_more": self.has_more}


# ---------------------------------------------------------------------------
# the library
# ---------------------------------------------------------------------------

class VideoLibrary:
    """The clip folder, indexed and searched without opening any file."""

    def __init__(self, root: Any = None, *, index_name: str = INDEX_FILE,
                 tools: Any = None, cache: Any = None) -> None:
        self.root = Path(root) if root else None
        self.index_path = (self.root / index_name) if self.root else None
        self.tools = tools
        self.probes = cache or MediaProbeCache(
            (self.root / PROBE_CACHE_FILE) if self.root else None)
        self.thumbnails = VideoThumbnailCache(
            (self.root / "thumbnails") if self.root else None)
        self._entries: dict[str, VideoEntry] = {}
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> dict[str, VideoEntry]:
        """Read the index once, then answer from memory.

        Every writer calls this first: appending to an index that has not been
        read yet would save a file containing only the new entry, and the next
        read would then show each clip twice.
        """
        if self.loaded:
            return self._entries
        self.loaded = True
        if self.index_path is None or not self.index_path.is_file():
            return self._entries
        try:
            from ..core.atomicio import load_json

            result = load_json(self.index_path, default={},
                               quarantine_dir=self.index_path.parent / "corrupt")
            data = result.data if getattr(result, "data", None) is not None else {}
        except Exception as exc:  # noqa: BLE001 - a bad index is not fatal
            log_event("VIDEO_LIBRARY_UNREADABLE",
                      f"The video library index could not be read: {exc}",
                      level="WARNING")
            return self._entries
        for item in (data.get("videos") or []):
            try:
                entry = VideoEntry.from_dict(item)
            except (TypeError, ValueError):
                continue
            if entry.id:
                self._entries[entry.id] = entry
        return self._entries

    def save(self) -> bool:
        if self.index_path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.load()
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.index_path, {
                "schema_version": 1,
                "videos": [entry.to_dict() for entry in self._sorted("recent")]})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("VIDEO_LIBRARY_SAVE_FAILED",
                      f"The video library could not be saved: {exc}",
                      level="WARNING")
            return False

    # -- scanning ----------------------------------------------------------

    def ensure_root(self) -> Optional[Path]:
        if self.root is None:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def video_paths(self) -> list[Path]:
        if self.root is None or not self.root.is_dir():
            return []
        found: list[Path] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if "thumbnails" in path.parts or "corrupt" in path.parts:
                continue
            found.append(path)
        return sorted(found)

    def scan(self, *, folder: Any = None, measure: bool = True,
             progress: Any = None, cancel: Any = None) -> int:
        """Index every clip in the folder (or in *folder*), measuring each once.

        A clip that has gone is **kept** and marked, rather than being silently
        dropped: a user who moves a file should be told it has gone, not find it
        quietly missing (sections 14, 21).
        """
        self.load()
        targets = ([Path(str(folder))] if folder else [])
        paths = []
        if targets:
            for base in targets:
                if base.is_file():
                    paths.append(base)
                elif base.is_dir():
                    paths.extend(sorted(path for path in base.rglob("*")
                                        if path.is_file() and path.suffix.lower()
                                        in VIDEO_SUFFIXES))
        else:
            self.ensure_root()
            paths = self.video_paths()

        found = 0
        for index, path in enumerate(paths):
            if cancel is not None and cancel.is_cancelled():
                break
            self._index_path(path, measure=measure)
            found += 1
            if progress is not None:
                progress(index + 1, len(paths), path.name)
        self.refresh_exists()
        self.save()
        log_event("VIDEO_LIBRARY_SCANNED",
                  f"{found} clip(s) were indexed", folder=str(folder or self.root))
        return found

    def _index_path(self, path: Path, *, measure: bool = True) -> VideoEntry:
        """One file into the index: the same file is the same entry."""
        existing = self.for_path(path)
        if existing is not None:
            if measure:
                metadata = self.probes.measure(path, self.tools)
                if metadata:
                    existing.update_from_probe(metadata)
                elif path.is_file():
                    state, _ = probe_state(path, self.tools)
                    existing.status = (ENTRY_READY
                                       if state == "check_not_available"
                                       else ENTRY_UNREADABLE)
            self._refresh_status(existing, exists=path.is_file())
            return existing
        entry = VideoEntry(id=_new_id(), path=str(path), name=path.stem,
                           source=self._guess_source(path),
                           container=path.suffix.lower().lstrip("."))
        if path.is_file():
            try:
                entry.created = path.stat().st_mtime
            except OSError:  # pragma: no cover - unreadable stat
                pass
        if measure:
            metadata = self.probes.measure(path, self.tools)
            if metadata:
                entry.update_from_probe(metadata)
            elif not path.is_file():
                entry.status = ENTRY_MISSING
            else:
                state, _ = probe_state(path, self.tools)
                entry.status = (ENTRY_READY if state == "check_not_available"
                                else ENTRY_UNREADABLE)
                # Even a file that is not a video has a size on disk.
                entry.size_bytes = _size_of(path)
        self._entries[entry.id] = entry
        return entry

    @staticmethod
    def _guess_source(path: Path) -> str:
        """What a clip found by scanning most likely is.

        Only a name that says so is taken as a guess - a clip generated by this
        application is registered as ``generated`` with its real provenance when
        it is made, not guessed from where it sits.
        """
        lowered = path.name.lower()
        if "render" in lowered or "final" in lowered:
            return "render"
        return "imported"

    # -- reading -----------------------------------------------------------

    def all(self) -> list[VideoEntry]:
        return self._sorted("recent")

    def find(self, entry_id: str) -> Optional[VideoEntry]:
        return self.load().get(str(entry_id))

    def for_path(self, path: Any) -> Optional[VideoEntry]:
        wanted = str(Path(str(path)))
        for entry in self.load().values():
            if entry.path == wanted:
                return entry
        return None

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self.load().values():
            counts["total"] = counts.get("total", 0) + 1
            counts[entry.source] = counts.get(entry.source, 0) + 1
            if entry.status != ENTRY_READY:
                counts["missing"] = counts.get("missing", 0) + 1
            if entry.favourite:
                counts["favourite"] = counts.get("favourite", 0) + 1
        return counts

    def describe(self) -> str:
        counts = self.counts()
        if not counts:
            return ("The video library is empty. Generate a clip, or scan a "
                    "folder that has one.")
        parts = [f"{counts.get('total', 0)} clip(s)"]
        for source in LIBRARY_SOURCES:
            if counts.get(source):
                parts.append(f"{counts[source]} {SOURCE_LABELS[source].lower()}")
        if counts.get("missing"):
            parts.append(f"{counts['missing']} missing")
        return ", ".join(parts) + "."

    def query(self, query: Optional[LibraryQuery] = None, *,
              limit: int = 0, offset: int = 0) -> LibraryPage:
        """Search, filter, sort and page (section 14)."""
        filters = query or LibraryQuery()
        text = str(filters.text or "").strip().lower()
        tag = str(filters.tag or "").strip().lower()
        found: list[VideoEntry] = []
        for entry in self.load().values():
            if filters.source and entry.source != filters.source:
                continue
            if filters.collection and entry.collection != filters.collection:
                continue
            if filters.project and entry.project != filters.project:
                continue
            if filters.favourites_only and not entry.favourite:
                continue
            if filters.missing_only and entry.status == ENTRY_READY:
                continue
            if filters.min_duration and entry.duration < float(filters.min_duration):
                continue
            if filters.max_duration and entry.duration > float(filters.max_duration):
                continue
            if tag and tag not in [str(item).lower() for item in entry.tags or []]:
                continue
            if text:
                haystack = " ".join([
                    entry.name, entry.filename, entry.prompt, entry.model,
                    entry.backend, entry.provider, entry.collection,
                    " ".join(str(item) for item in entry.tags or [])]).lower()
                if text not in haystack:
                    continue
            found.append(entry)
        ordered = self._sorted(str(filters.sort or "recent"), entries=found)
        page_limit = int(limit or filters.limit or 200)
        page_offset = int(offset or filters.offset or 0)
        window = ordered[page_offset:page_offset + page_limit]
        return LibraryPage(entries=window, total=len(ordered),
                           offset=page_offset, limit=page_limit)

    def _sorted(self, order: str, *,
                entries: Optional[Iterable[VideoEntry]] = None) -> list[VideoEntry]:
        items = list(entries if entries is not None else self.load().values())
        keys = {
            "recent": lambda item: (item.added or item.created, item.name),
            "oldest": lambda item: (item.added or item.created, item.name),
            "name": lambda item: (item.name.lower(), -item.added),
            "duration": lambda item: (-float(item.duration or 0.0), item.name.lower()),
            "size": lambda item: (-int(item.size_bytes or 0), item.name.lower()),
            "resolution": lambda item: (-(int(item.width or 0) * int(item.height or 0)),
                                        item.name.lower()),
        }
        key = keys.get(str(order), keys["recent"])
        return sorted(items, key=key,
                      reverse=str(order) == "recent")

    def collections(self) -> list[str]:
        return sorted({entry.collection for entry in self.load().values()
                       if entry.collection})

    def tags(self) -> list[str]:
        found: set[str] = set()
        for entry in self.load().values():
            found.update(str(tag) for tag in (entry.tags or []) if str(tag).strip())
        return sorted(found)

    def missing(self) -> list[VideoEntry]:
        return [entry for entry in self.all() if entry.status != ENTRY_READY]

    def total_size(self) -> int:
        return sum(int(entry.size_bytes or 0) for entry in self.load().values())

    # -- writing -----------------------------------------------------------

    def add(self, path: Any, *, source: str = "", name: str = "",
            metadata: Optional[dict] = None, measure: bool = True,
            **fields: Any) -> Optional[VideoEntry]:
        """Register a clip.  The same file is never registered twice.

        Adding clips does not copy or move anything: the library indexes the
        file where it is, so a generated clip stays in one place.
        """
        target = Path(str(path))
        if not target.is_file():
            return None
        self.load()
        entry = self.for_path(target)
        if entry is None:
            entry = VideoEntry(
                id=_new_id(), path=str(target), name=str(name or target.stem),
                source=str(source or self._guess_source(target)),
                container=target.suffix.lower().lstrip("."),
                created=_mtime(target), added=time.time(),
                checksum=_digest(target))
            self._entries[entry.id] = entry
        if name:
            entry.name = str(name)
        if source:
            entry.source = str(source)
        if measure:
            measured = self.probes.measure(target, self.tools)
            if measured:
                entry.update_from_probe(measured)
            else:
                state, _ = probe_state(target, self.tools)
                entry.status = (ENTRY_READY if state == "check_not_available"
                                else ENTRY_UNREADABLE)
                entry.size_bytes = _size_of(target)
        else:
            # The caller asked for no measuring, so existence is all we know.
            entry.status = ENTRY_READY
            entry.measured_with = ""
        payload = dict(metadata or {})
        payload.update(fields)
        for key, value in payload.items():
            if hasattr(entry, key) and value not in (None, ""):
                setattr(entry, key, value)
        if not measure:
            entry.status = ENTRY_READY
        self.save()
        log_event("VIDEO_LIBRARY_ADDED", f"{target.name} was indexed",
                  source=entry.source, id=entry.id)
        return entry

    def update(self, entry_id: str, **fields: Any) -> Optional[VideoEntry]:
        self.load()
        entry = self.find(entry_id)
        if entry is None:
            return None
        for key, value in fields.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        self.save()
        return entry

    def rename(self, entry_id: str, name: str) -> bool:
        """Rename the library entry.  The file on disk is not touched."""
        cleaned = str(name or "").strip()
        if not cleaned:
            return False
        entry = self.update(entry_id, name=cleaned)
        return entry is not None

    def set_favourite(self, entry_id: str, favourite: bool = True) -> bool:
        return self.update(entry_id, favourite=bool(favourite)) is not None

    def set_tags(self, entry_id: str, tags: Any) -> bool:
        cleaned = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        return self.update(entry_id, tags=cleaned) is not None

    def add_tag(self, entry_id: str, tag: str) -> bool:
        entry = self.find(entry_id)
        if entry is None:
            return False
        wanted = str(tag or "").strip()
        if not wanted:
            return False
        if wanted not in (entry.tags or []):
            entry.tags = list(entry.tags or []) + [wanted]
            self.save()
        return True

    def set_collection(self, entry_id: str, collection: str) -> bool:
        return self.update(entry_id, collection=str(collection or "").strip()) is not None

    def remove(self, entry_id: str, *, delete_file: bool = False) -> bool:
        """Forget a clip.  The file is deleted only when explicitly asked."""
        self.load()
        entry = self.find(entry_id)
        if entry is None:
            return False
        self._entries.pop(entry.id, None)
        self.save()
        if delete_file and entry.path:
            try:
                Path(entry.path).unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - a locked file
                log_event("VIDEO_LIBRARY_DELETE_FAILED",
                          f"{entry.path} could not be deleted: {exc}",
                          level="WARNING")
                return False
        return True

    @staticmethod
    def _refresh_status(entry: VideoEntry, *, exists: bool) -> bool:
        """Move an entry on the "is the file there?" axis only.

        Coming and going is the only thing this knows about: a clip that is back
        is usable again, but a clip we already failed to read stays marked until
        it is checked again.  Returns ``True`` when something changed.
        """
        if not exists:
            if entry.status != ENTRY_MISSING:
                entry.status = ENTRY_MISSING
                return True
            return False
        if entry.status == ENTRY_MISSING:
            entry.status = ENTRY_READY
            return True
        return False

    def refresh_exists(self) -> int:
        """Mark the entries whose file has gone (and unmark the ones that are back)."""
        changed = 0
        for entry in self.load().values():
            if self._refresh_status(entry, exists=Path(entry.path).is_file()):
                changed += 1
        return changed

    def recheck(self, entry_id: str) -> Optional[VideoEntry]:
        """Measure one clip again, because the file may have been replaced."""
        entry = self.find(entry_id)
        if entry is None:
            return None
        self.probes.invalidate(entry.path)
        metadata = self.probes.measure(entry.path, self.tools, force=True)
        if metadata:
            entry.update_from_probe(metadata)
            entry.status = ENTRY_READY
            entry.checksum = _digest(Path(entry.path))
        else:
            if not Path(entry.path).is_file():
                entry.status = ENTRY_MISSING
            else:
                state, _ = probe_state(entry.path, self.tools)
                entry.status = (ENTRY_READY if state == "check_not_available"
                                else ENTRY_UNREADABLE)
        self.save()
        return entry

    def thumbnail_for(self, entry_id: str, *, regenerate: bool = False,
                      cancel: Any = None) -> Optional[Path]:
        """The cached picture for one clip.  Never runs on the GUI thread."""
        entry = self.find(entry_id)
        if entry is None:
            return None
        target = self.thumbnails.get(entry.path, self.tools, cancel=cancel,
                                     regenerate=regenerate)
        if target is not None and str(target) != entry.thumbnail:
            entry.thumbnail = str(target)
            self.save()
        return target

    def clear_thumbnails(self) -> int:
        for entry in self.load().values():
            entry.thumbnail = ""
        self.save()
        return self.thumbnails.clear()

    def register_generated(self, result: Any, request: Any) -> Optional[VideoEntry]:
        """Index a clip an AI backend just produced, with its provenance.

        This is what makes a generated clip an ordinary library asset that can be
        sent to a project: the numbers are measured, and the prompt, seed,
        backend and model travel with it (sections 22, 25).
        """
        path = getattr(result, "path", "")
        if not path or not Path(str(path)).is_file():
            return None
        metadata = dict(getattr(result, "metadata", {}) or {})
        parameters = dict(getattr(result, "requested", {}) or {})
        return self.add(
            path, source="generated",
            name=Path(str(path)).stem,
            provider=str(metadata.get("provider", "") or ""),
            backend=str(getattr(result, "backend", "") or request.backend or ""),
            model=str(getattr(result, "model", "") or request.model or ""),
            prompt=str(getattr(request, "prompt", "") or ""),
            seed=int(getattr(result, "seed", 0) or 0),
            parameters=parameters,
            project=str(getattr(request, "project", "") or ""),
            scene=str(getattr(request, "scene_id", "") or ""),
            is_ai_model=metadata.get("is_ai_model", None) is True,
            label=str(metadata.get("label", "") or getattr(
                result, "backend_name", "") or ""))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - unreadable stat
        return time.time()


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - unreadable stat
        return 0


def _digest(path: Path, *, limit: int = 8 * 1024 * 1024) -> str:
    """A cheap content digest: the first few megabytes, not the whole file."""
    try:
        handle = path.open("rb")
    except OSError:  # pragma: no cover - unreadable file
        return ""
    try:
        return hashlib.sha256(handle.read(limit)).hexdigest()
    except OSError:  # pragma: no cover
        return ""
    finally:
        handle.close()
