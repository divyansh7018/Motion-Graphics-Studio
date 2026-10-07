"""One generation history for everything the studio makes (sections 47, 50).

A single list holds images and clips, whatever backend produced them, with the
metadata needed to find them again and to understand them later: backend,
model, operation, prompt, seed, size, duration, project, scene, parents,
references, favourite.

Two things this module is careful about:

* **it never invents an entry.**  An entry exists because a file exists and was
  measured; ``refresh_exists`` marks entries whose file has gone rather than
  deleting the record, because "this was generated and the file moved" is
  something a person wants to know.
* **it is one view, not a second store.**  Entries made by Stage F's image
  history are imported by path, idempotently, so an image generated in Image
  Studio appears here without being copied or duplicated.
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
from .types import AIOperation, OPERATION_KIND, OPERATION_LABELS

__all__ = ["GenerationEntry", "GenerationHistory", "KINDS", "STATES"]

#: What the history can hold.
KINDS: tuple[str, ...] = ("image", "video")

#: The state of the file behind an entry.
STATES: tuple[str, ...] = ("COMPLETED", "MISSING", "FAILED")


def _new_id(prefix: str = "gen") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _digest(path: Path, *, limit: int = 4 * 1024 * 1024) -> str:
    """A short checksum of a file, without reading a whole video into memory."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            remaining = int(limit)
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return ""
    return hasher.hexdigest()[:32]


@dataclass
class GenerationEntry:
    """One thing that was generated."""

    id: str = ""
    kind: str = "image"
    path: str = ""
    operation: str = ""
    backend: str = ""
    backend_name: str = ""
    model: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    seed: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    frames: int = 0
    size_bytes: int = 0
    output_format: str = ""
    status: str = "COMPLETED"
    created: float = field(default_factory=time.time)
    seconds: float = 0.0
    project: str = ""
    scene: str = ""
    collection: str = ""
    parent: str = ""
    references: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    favourite: bool = False
    #: Whether the backend that made it runs a real model.
    is_ai_model: bool = True
    #: The full reproducibility record (section 100).
    settings: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    checksum: str = ""
    error: str = ""

    # -- display -----------------------------------------------------------

    @property
    def name(self) -> str:
        return Path(self.path).name if self.path else "(no file)"

    def label(self) -> str:
        what = OPERATION_LABELS.get(self.operation, self.operation or self.kind)
        bits = [what]
        if self.backend_name or self.backend:
            bits.append(self.backend_name or self.backend)
        if self.model:
            bits.append(self.model)
        return " - ".join(bits)

    def prompt_preview(self, limit: int = 48) -> str:
        """The prompt, on one line, for a history row (section 47)."""
        text = " ".join(str(self.prompt or "").split())
        if not text:
            return ""
        return (text[:limit] + "...") if len(text) > limit + 3 else text

    def dimensions(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return ""

    def detail(self) -> str:
        parts = [item for item in (self.dimensions(),) if item]
        if self.duration:
            parts.append(f"{self.duration:.2f}s")
        if self.fps:
            parts.append(f"{self.fps:g} fps")
        if self.size_bytes:
            parts.append(human_bytes(self.size_bytes))
        if self.seed:
            parts.append(f"seed {self.seed}")
        return ", ".join(parts)

    def created_label(self) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created))
        except (ValueError, OSError):  # pragma: no cover - bad clock
            return ""

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["name"] = self.name
        data["detail"] = self.detail()
        data["created_label"] = self.created_label()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationEntry":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in known}
        return cls(**payload)


class GenerationHistory:
    """The studio's own history file, with the filters the interface needs."""

    def __init__(self, path: Any = None, *, limit: int = 2000) -> None:
        self.path = Path(path) if path else None
        self.limit = max(50, int(limit or 2000))
        self._entries: list[GenerationEntry] = []
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> list[GenerationEntry]:
        """Read the file once, then answer from memory.

        Every writer calls this first: appending to a list that has not been
        read yet would save a file with only the new entries in it - and then
        the next read would show each entry twice (section 47).
        """
        if self.loaded:
            return self._entries
        self.loaded = True
        if self.path is None or not self.path.is_file():
            return self._entries
        try:
            from ..core.atomicio import load_json

            result = load_json(self.path, default={},
                               quarantine_dir=self.path.parent / "corrupt")
            data = (result.data if getattr(result, "data", None) is not None
                    else {}) or {}
        except Exception as exc:  # noqa: BLE001 - a bad history is not fatal
            log_event("AI_HISTORY_UNREADABLE",
                      f"The AI history could not be read: {exc}", level="WARNING")
            return self._entries
        for item in (data.get("entries") or []):
            try:
                self._entries.append(GenerationEntry.from_dict(item))
            except (TypeError, ValueError):
                continue
        return self._entries

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            payload = {"schema_version": 1,
                       "entries": [entry.to_dict() for entry in self._entries]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.path, payload)
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("AI_HISTORY_SAVE_FAILED",
                      f"The AI history could not be saved: {exc}", level="WARNING")
            return False

    # -- reading -----------------------------------------------------------

    def all(self) -> list[GenerationEntry]:
        return self.load()

    def recent(self, limit: int = 30) -> list[GenerationEntry]:
        return sorted(self.all(), key=lambda entry: entry.created, reverse=True)[:limit]

    def find(self, entry_id: str) -> Optional[GenerationEntry]:
        for entry in self.all():
            if entry.id == entry_id:
                return entry
        return None

    def for_path(self, path: Any) -> Optional[GenerationEntry]:
        target = str(Path(path))
        for entry in self.all():
            if entry.path and str(entry.path) == target:
                return entry
        return None

    def query(self, *, kind: str = "", backend: str = "", model: str = "",
              project: str = "", favourites_only: bool = False,
              since: float = 0.0, until: float = 0.0, search: str = "",
              scene: str = "", limit: int = 0) -> list[GenerationEntry]:
        """Filter by type, backend, model, project, date and favourite (section 50)."""
        text = str(search or "").strip().lower()
        found: list[GenerationEntry] = []
        for entry in sorted(self.all(), key=lambda item: item.created, reverse=True):
            if kind and entry.kind != kind:
                continue
            if backend and entry.backend != backend:
                continue
            if model and entry.model != model:
                continue
            if project and entry.project != project:
                continue
            if scene and entry.scene != scene:
                continue
            if favourites_only and not entry.favourite:
                continue
            if since and entry.created < float(since):
                continue
            if until and entry.created > float(until):
                continue
            if text and text not in (entry.prompt + " " + entry.name + " " +
                                     entry.tags.__str__()).lower():
                continue
            found.append(entry)
        return found[:int(limit)] if limit else found

    def counts(self) -> dict:
        """How many of each kind, plus how many of them failed.

        A failure is counted under its own name as well as under its kind: the
        history should never make a failed attempt look like something that was
        produced (sections 47, 100).
        """
        counts: dict[str, int] = {}
        failed = 0
        for entry in self.all():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
            if str(entry.status).upper() == "FAILED":
                failed += 1
        if failed:
            counts["failed"] = failed
        return counts

    def describe(self) -> str:
        counts = self.counts()
        if not counts:
            return "No generations yet."
        text = (f"{counts.get('image', 0)} image(s), {counts.get('video', 0)} "
                f"clip(s) in the history.")
        if counts.get("failed"):
            text += f" {counts['failed']} failed."
        return text

    # -- writing -----------------------------------------------------------

    def add_video(self, result: Any, request: Any) -> Optional[GenerationEntry]:
        """Record a finished clip, with everything needed to remake it."""
        self.load()
        path = Path(str(getattr(result, "path", "") or ""))
        if not path.is_file():
            return None
        entry = GenerationEntry(
            id=_new_id("clip"), kind="video", path=str(path),
            operation=str(getattr(request, "mode", "") or ""),
            backend=str(getattr(result, "backend", "") or ""),
            backend_name=str(getattr(result, "backend_name", "") or ""),
            model=str(getattr(result, "model", "") or ""),
            prompt=str(getattr(request, "prompt", "") or ""),
            negative_prompt=str(getattr(request, "negative_prompt", "") or ""),
            seed=int(getattr(result, "seed", 0) or 0),
            width=int(getattr(result, "width", 0) or 0),
            height=int(getattr(result, "height", 0) or 0),
            duration=float(getattr(result, "duration", 0.0) or 0.0),
            fps=float(getattr(result, "fps", 0.0) or 0.0),
            frames=int(getattr(result, "frames", 0) or 0),
            size_bytes=path.stat().st_size,
            output_format=str(getattr(result, "output_format", "") or ""),
            status="COMPLETED",
            seconds=float(getattr(result, "seconds", 0.0) or 0.0),
            project=str(getattr(request, "project", "") or ""),
            scene=str(getattr(request, "scene_id", "") or ""),
            collection=str(getattr(request, "collection", "") or ""),
            parent=str(getattr(request, "parent_asset", "") or ""),
            references=[item for item in (
                str(getattr(request, "reference_image", "") or ""),
                str(getattr(request, "style_reference", "") or ""),
                str(getattr(request, "source_image", "") or ""),
                str(getattr(request, "source_video", "") or ""),
                str(getattr(request, "extend_from", "") or "")) if item],
            tags=list(getattr(request, "tags", []) or []),
            is_ai_model=bool((getattr(result, "metadata", {}) or {}).get(
                "is_ai_model", True) is not False),
            settings=dict(getattr(result, "requested", {}) or {}),
            quality=dict(getattr(result, "quality", {}) or {}),
            metadata=dict(getattr(result, "metadata", {}) or {}),
            checksum=_digest(path))
        self._entries.insert(0, entry)
        self._trim()
        self.save()
        log_event("AI_HISTORY_ADDED", f"Recorded {entry.name}",
                  kind="video", backend=entry.backend, model=entry.model)
        return entry

    def add_image_result(self, result: Any, request: Any) -> list[GenerationEntry]:
        """Record the images a Stage F generation produced."""
        self.load()
        added: list[GenerationEntry] = []
        paths = list(getattr(result, "paths", []) or [])
        seeds = list(getattr(result, "seeds", []) or [])
        for index, raw in enumerate(paths):
            path = Path(str(raw))
            if not path.is_file():
                continue
            entry = GenerationEntry(
                id=_new_id("img"), kind="image", path=str(path),
                operation=str(getattr(request, "mode", "") or
                              AIOperation.TEXT_TO_IMAGE),
                backend=str(getattr(result, "backend", "") or ""),
                model=str(getattr(result, "model", "") or ""),
                prompt=str(getattr(request, "prompt", "") or ""),
                negative_prompt=str(getattr(request, "negative_prompt", "") or ""),
                seed=int(seeds[index]) if index < len(seeds) else
                int(getattr(request, "seed", 0) or 0),
                width=int(getattr(result, "width", 0) or 0),
                height=int(getattr(result, "height", 0) or 0),
                size_bytes=path.stat().st_size,
                output_format=str(getattr(result, "output_format", "") or ""),
                status="COMPLETED",
                seconds=float(getattr(result, "seconds", 0.0) or 0.0),
                project=str(getattr(request, "project", "") or ""),
                scene=str(getattr(request, "scene_id", "") or ""),
                parent=str(getattr(getattr(request, "variant_of", None), "path", "") or ""),
                references=[str(item) for item in (
                    getattr(request, "reference_image", ""),
                    getattr(request, "style_reference", "")) if item],
                is_ai_model=True,
                settings=dict(getattr(result, "metadata", {}) or {}),
                quality=dict(getattr(result, "quality", {}) or {}),
                metadata=dict(getattr(result, "metadata", {}) or {}),
                checksum=_digest(path))
            self._entries.insert(0, entry)
            added.append(entry)
        if added:
            self._trim()
            self.save()
        return added

    def add_failure(self, *, kind: str, backend: str, model: str, operation: str,
                    prompt: str, error: str, why: str = "",
                    what_to_do: str = "") -> GenerationEntry:
        """Record a failure, because "it failed and I do not know why" is worse."""
        self.load()
        entry = GenerationEntry(
            id=_new_id("fail"), kind=kind, path="", operation=operation,
            backend=backend, model=model, prompt=prompt, status="FAILED",
            error=error, metadata={"why": why, "what_to_do": what_to_do})
        self._entries.insert(0, entry)
        self._trim()
        self.save()
        return entry

    def set_favourite(self, entry_id: str, favourite: bool = True) -> bool:
        entry = self.find(entry_id)
        if entry is None:
            return False
        entry.favourite = bool(favourite)
        self.save()
        return True

    def update_thumbnail(self, entry_id: str, thumbnail: Any) -> bool:
        entry = self.find(entry_id)
        if entry is None:
            return False
        entry.metadata["thumbnail"] = str(thumbnail)
        self.save()
        return True

    def tag(self, entry_id: str, tags: Iterable[str]) -> bool:
        entry = self.find(entry_id)
        if entry is None:
            return False
        existing = list(entry.tags or [])
        for tag in tags:
            text = str(tag or "").strip()
            if text and text not in existing:
                existing.append(text)
        entry.tags = existing
        self.save()
        return True

    def remove(self, entry_id: str, *, delete_file: bool = False) -> bool:
        """Forget an entry, and only delete the file when explicitly asked."""
        entry = self.find(entry_id)
        if entry is None:
            return False
        self._entries = [item for item in self._entries if item.id != entry_id]
        self.save()
        if delete_file and entry.path:
            try:
                Path(entry.path).unlink(missing_ok=True)
            except OSError as exc:
                log_event("AI_HISTORY_DELETE_FAILED",
                          f"{entry.path} could not be deleted: {exc}",
                          level="WARNING")
                return False
        return True

    def refresh_exists(self) -> int:
        """Mark entries whose file has gone.  Returns how many are missing."""
        changed = 0
        for entry in self.all():
            if not entry.path:
                continue
            exists = Path(entry.path).is_file()
            wanted = "COMPLETED" if exists else "MISSING"
            if entry.status != wanted:
                entry.status = wanted
                changed += 1
        if changed:
            self.save()
        return changed

    def import_stage_f(self, entries: Iterable[Any]) -> int:
        """Bring Stage F's image history into this one, once each.

        Idempotent by path: an image that is already recorded is not added
        again, so pressing the button twice cannot duplicate the library.
        """
        added = 0
        known = {entry.path for entry in self.all() if entry.path}
        for item in entries:
            path = str(getattr(item, "path", "") or "")
            if not path or path in known or not Path(path).is_file():
                continue
            entry = GenerationEntry(
                id=_new_id("img"), kind="image", path=path,
                operation=str(getattr(item, "mode", "") or
                              AIOperation.TEXT_TO_IMAGE),
                backend=str(getattr(item, "backend", "") or ""),
                model=str(getattr(item, "model", "") or ""),
                prompt=str(getattr(item, "prompt", "") or ""),
                negative_prompt=str(getattr(item, "negative_prompt", "") or ""),
                seed=int(getattr(item, "seed", 0) or 0),
                width=int(getattr(item, "width", 0) or 0),
                height=int(getattr(item, "height", 0) or 0),
                size_bytes=Path(path).stat().st_size,
                status=str(getattr(item, "status", "COMPLETED") or "COMPLETED"),
                created=float(getattr(item, "created", 0.0) or time.time()),
                seconds=float(getattr(item, "seconds", 0.0) or 0.0),
                favourite=bool(getattr(item, "favourite", False)),
                metadata={"imported_from": "image_history"})
            self._entries.append(entry)
            known.add(path)
            added += 1
        if added:
            self._trim()
            self.save()
            log_event("AI_HISTORY_IMPORTED",
                      f"{added} image history entry(ies) were brought in", count=added)
        return added

    def clear(self) -> int:
        self.load()
        count = len(self._entries)
        self._entries = []
        self.save()
        return count

    def _trim(self) -> None:
        if len(self._entries) > self.limit:
            self._entries = sorted(self._entries, key=lambda item: item.created,
                                   reverse=True)[:self.limit]

    # -- version graph -----------------------------------------------------

    def _parent_of(self, entry: GenerationEntry) -> Optional[GenerationEntry]:
        """The entry an entry came from.

        A parent is recorded as whatever the caller had to hand - the file the
        variation was made from, or the id of the asset it was made for - so both
        are looked up rather than assuming one of them (section 52).
        """
        wanted = str(entry.parent or "")
        if not wanted:
            return None
        return self.for_path(wanted) or self.find(wanted)

    def lineage(self, entry: GenerationEntry) -> list[GenerationEntry]:
        """The chain this entry came from: original -> variation -> edit (section 52)."""
        chain = [entry]
        seen = {entry.id}
        current = entry
        guard = 0
        while guard < 32:
            guard += 1
            parent = self._parent_of(current)
            if parent is None or parent.id in seen:
                break
            chain.append(parent)
            seen.add(parent.id)
            current = parent
        return list(reversed(chain))

    def children_of(self, path: Any) -> list[GenerationEntry]:
        """Every entry made from a given file, or for a given entry id."""
        target = str(Path(path)) if path else ""
        if not target:
            return []
        known = self.for_path(target)
        wanted = {target}
        if known is not None:
            wanted.add(known.id)
        return [entry for entry in self.all()
                if entry.parent and str(entry.parent) in wanted]

    def describe_lineage(self, entry: GenerationEntry) -> str:
        chain = self.lineage(entry)
        return " -> ".join(item.name for item in chain)

    def operation_kind(self, entry: GenerationEntry) -> str:
        return OPERATION_KIND.get(str(entry.operation), entry.kind)
