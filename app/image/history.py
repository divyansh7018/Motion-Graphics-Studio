"""Generation history and prompt history (Stage F, sections 9, 23, 24, 63).

Every generation is recorded with the seed and the settings that produced it, so
a user can come back tomorrow, see what they asked for, and get the same picture
again.  The seed is the part that matters: without it, "regenerate" is just
"roll the dice again".

Prompts are kept separately, because reusing a prompt is a different act from
reusing an image - and a prompt is never edited except by an explicit save,
rename or delete from the user.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event

__all__ = ["HistoryEntry", "PromptEntry", "ImageHistory", "PromptLibrary",
           "MAX_ENTRIES", "MAX_PROMPTS"]

#: Bounds so the files cannot grow without limit on a busy machine.
MAX_ENTRIES = 500
MAX_PROMPTS = 200


@dataclass
class HistoryEntry:
    """One generation: one image, or one item of a batch."""

    id: str = ""
    at: str = ""
    path: str = ""
    thumbnail: str = ""
    status: str = ""            # COMPLETED | FAILED | CANCELLED
    prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    backend: str = ""
    mode: str = ""
    seed: Optional[int] = None
    width: int = 0
    height: int = 0
    output_format: str = ""
    seconds: float = 0.0
    project: str = ""
    collection: str = ""
    #: The exact request, so "regenerate" replays it faithfully.
    request: dict = field(default_factory=dict)
    #: Which batch this belongs to, and where in it.
    batch_id: str = ""
    batch_index: int = 0
    batch_total: int = 1
    error: str = ""
    #: "original" | "variation" | "edit" | "upscale"
    origin: str = "original"
    parent: str = ""
    exists: bool = True

    @property
    def prompt_preview(self) -> str:
        text = str(self.prompt or "").strip().replace("\n", " ")
        return text if len(text) <= 90 else text[:87] + "..."

    def label(self) -> str:
        name = Path(self.path).name if self.path else "(no file)"
        seed = f"seed {self.seed}" if self.seed is not None else "no seed"
        return f"{name} · {self.width}x{self.height} · {seed}"

    def to_dict(self) -> dict:
        data = {
            "id": self.id, "at": self.at, "path": self.path,
            "thumbnail": self.thumbnail, "status": self.status,
            "prompt": self.prompt, "negative_prompt": self.negative_prompt,
            "model": self.model, "backend": self.backend, "mode": self.mode,
            "width": self.width, "height": self.height,
            "output_format": self.output_format,
            "seconds": round(float(self.seconds or 0.0), 3),
            "project": self.project, "collection": self.collection,
            "request": dict(self.request or {}), "batch_id": self.batch_id,
            "batch_index": self.batch_index, "batch_total": self.batch_total,
            "error": self.error, "origin": self.origin, "parent": self.parent,
            "exists": bool(self.exists),
        }
        if self.seed is not None:
            data["seed"] = int(self.seed)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**payload)


@dataclass
class PromptEntry:
    """A saved or recently used prompt."""

    id: str = ""
    text: str = ""
    negative: str = ""
    name: str = ""
    favourite: bool = False
    used: int = 0
    last_used: str = ""
    created_at: str = ""
    settings: dict = field(default_factory=dict)

    def label(self) -> str:
        text = str(self.text or "").strip().replace("\n", " ")
        preview = text if len(text) <= 60 else text[:57] + "..."
        return f"{self.name or preview}" + (" *" if self.favourite else "")

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "negative": self.negative,
                "name": self.name, "favourite": bool(self.favourite),
                "used": int(self.used or 0), "last_used": self.last_used,
                "created_at": self.created_at,
                "settings": dict(self.settings or {})}

    @classmethod
    def from_dict(cls, data: dict) -> "PromptEntry":
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**payload)


class _JsonStore:
    """A small JSON file, written atomically."""

    def __init__(self, path: Any) -> None:
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log_event("IMAGE_HISTORY_UNREADABLE", "History could not be read",
                      path=str(self.path), error=str(exc))
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, payload: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".part")
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
            os.replace(str(temporary), str(self.path))
            return True
        except OSError as exc:
            log_event("IMAGE_HISTORY_FAILED", "History could not be saved",
                      path=str(self.path), error=str(exc))
            return False


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class ImageHistory:
    """The generation history for one data root."""

    def __init__(self, path: Any) -> None:
        self.store = _JsonStore(path)

    # -- reading -----------------------------------------------------------

    def all(self) -> list[HistoryEntry]:
        payload = self.store.read()
        return [HistoryEntry.from_dict(item)
                for item in payload.get("entries") or []]

    def recent(self, limit: int = 24) -> list[HistoryEntry]:
        return self.all()[:max(1, int(limit or 1))]

    def find(self, entry_id: str) -> Optional[HistoryEntry]:
        for entry in self.all():
            if entry.id == str(entry_id):
                return entry
        return None

    def for_project(self, project: str) -> list[HistoryEntry]:
        return [entry for entry in self.all() if entry.project == str(project)]

    def same_seed(self, seed: int) -> list[HistoryEntry]:
        """Every generation that used one seed - the basis of a variation."""
        return [entry for entry in self.all()
                if entry.seed is not None and int(entry.seed) == int(seed)]

    # -- writing -----------------------------------------------------------

    def add_result(self, result: Any, request: Any, *, batch_id: str = "",
                   limit: int = MAX_ENTRIES) -> list[HistoryEntry]:
        """Record every image a generation produced, one entry each."""
        entries = self.all()
        added: list[HistoryEntry] = []
        paths = [str(item) for item in (getattr(result, "paths", []) or [])]
        seeds = list(getattr(result, "seeds", []) or [])
        total = max(1, len(paths))
        stamp = datetime.now().isoformat(timespec="seconds")
        for index, path in enumerate(paths):
            seed = seeds[index] if index < len(seeds) else \
                (seeds[0] if seeds else None)
            entry = HistoryEntry(
                id=_new_id("img"), at=stamp, path=path,
                status=str(getattr(result, "state", "") or "COMPLETED"),
                prompt=str(getattr(request, "prompt", "") or ""),
                negative_prompt=str(getattr(request, "negative_prompt", "") or ""),
                model=str(getattr(result, "model", "") or ""),
                backend=str(getattr(result, "backend", "") or ""),
                mode=str(getattr(request, "mode", "") or ""),
                seed=int(seed) if seed is not None else None,
                width=int(getattr(result, "width", 0) or 0),
                height=int(getattr(result, "height", 0) or 0),
                output_format=str(getattr(result, "output_format", "") or ""),
                seconds=float(getattr(result, "seconds", 0.0) or 0.0),
                project=str(getattr(request, "project", "") or ""),
                collection=str(getattr(request, "collection", "") or ""),
                request=dict(getattr(request, "to_dict", lambda: {})() or {}),
                batch_id=str(batch_id or ""),
                batch_index=index + 1, batch_total=total,
                origin="variation"
                if str(getattr(request, "mode", "")) == "variation" else "original",
                parent=str(getattr(request, "parent_asset", "") or ""),
                exists=Path(path).is_file(),
            )
            entries.insert(index, entry)
            added.append(entry)
        self.store.write({"entries": [item.to_dict() for item in entries[:limit]]})
        return added

    def add_failure(self, request: Any, result: Any, *,
                    limit: int = MAX_ENTRIES) -> HistoryEntry:
        """A failed generation is recorded too, so the user can see why."""
        stamp = datetime.now().isoformat(timespec="seconds")
        entry = HistoryEntry(
            id=_new_id("img_fail"), at=stamp,
            status=str(getattr(result, "state", "") or "FAILED"),
            prompt=str(getattr(request, "prompt", "") or ""),
            negative_prompt=str(getattr(request, "negative_prompt", "") or ""),
            model=str(getattr(request, "model", "") or ""),
            backend=str(getattr(result, "backend", "") or ""),
            mode=str(getattr(request, "mode", "") or ""),
            seed=int(getattr(request, "seed") or 0) or None,
            width=int(getattr(request, "width", 0) or 0),
            height=int(getattr(request, "height", 0) or 0),
            project=str(getattr(request, "project", "") or ""),
            request=dict(getattr(request, "to_dict", lambda: {})() or {}),
            error=str(getattr(result, "error", "") or ""),
            exists=False,
        )
        entries = self.all()
        entries.insert(0, entry)
        self.store.write({"entries": [item.to_dict() for item in entries[:limit]]})
        return entry

    def update_thumbnail(self, entry_id: str, thumbnail: Any) -> bool:
        entries = self.all()
        changed = False
        for entry in entries:
            if entry.id == str(entry_id):
                entry.thumbnail = str(thumbnail or "")
                changed = True
        if changed:
            self.store.write({"entries": [item.to_dict() for item in entries]})
        return changed

    def refresh_exists(self) -> int:
        """Re-check which files are still on disk.  Returns how many are gone."""
        entries = self.all()
        missing = 0
        for entry in entries:
            present = bool(entry.path) and Path(entry.path).is_file()
            if not present:
                missing += 1
            entry.exists = present
        self.store.write({"entries": [item.to_dict() for item in entries]})
        return missing

    def remove(self, entry_id: str, *, delete_file: bool = False) -> bool:
        entries = self.all()
        kept = [entry for entry in entries if entry.id != str(entry_id)]
        if len(kept) == len(entries):
            return False
        if delete_file:
            for entry in entries:
                if entry.id == str(entry_id) and entry.path:
                    try:
                        Path(entry.path).unlink(missing_ok=True)
                    except OSError:
                        pass
        self.store.write({"entries": [item.to_dict() for item in kept]})
        return True

    def clear(self) -> int:
        count = len(self.all())
        self.store.write({"entries": []})
        return count


class PromptLibrary:
    """Recent prompts and favourites.

    Favourites keep their place at the top of the list; everything else is
    ordered by when it was last used.
    """

    def __init__(self, path: Any) -> None:
        self.store = _JsonStore(path)

    def all(self) -> list[PromptEntry]:
        payload = self.store.read()
        return [PromptEntry.from_dict(item)
                for item in payload.get("prompts") or []]

    def recent(self, limit: int = 20) -> list[PromptEntry]:
        entries = self.all()
        entries.sort(key=lambda item: item.last_used or item.created_at or "",
                     reverse=True)
        favourites = [item for item in entries if item.favourite]
        others = [item for item in entries if not item.favourite]
        return (favourites + others)[:max(1, int(limit or 1))]

    def favourites(self) -> list[PromptEntry]:
        return [item for item in self.all() if item.favourite]

    def find(self, prompt_id: str) -> Optional[PromptEntry]:
        for entry in self.all():
            if entry.id == str(prompt_id):
                return entry
        return None

    def by_text(self, text: str) -> Optional[PromptEntry]:
        needle = str(text or "").strip()
        for entry in self.all():
            if entry.text.strip() == needle:
                return entry
        return None

    def record_use(self, text: str, *, negative: str = "",
                   settings: Optional[dict] = None,
                   limit: int = MAX_PROMPTS) -> Optional[PromptEntry]:
        """Note that a prompt was used.  Never edits the prompt itself."""
        if not str(text or "").strip():
            return None
        entries = self.all()
        stamp = datetime.now().isoformat(timespec="seconds")
        existing = None
        for entry in entries:
            if entry.text.strip() == str(text).strip():
                existing = entry
                break
        if existing is None:
            existing = PromptEntry(id=_new_id("prompt"), text=str(text or ""),
                                   negative=str(negative or ""),
                                   created_at=stamp,
                                   settings=dict(settings or {}))
            entries.insert(0, existing)
        existing.used = int(existing.used or 0) + 1
        existing.last_used = stamp
        if settings:
            existing.settings = dict(settings)
        self.store.write({"prompts": [item.to_dict() for item in entries[:limit]]})
        return existing

    def save(self, text: str, *, name: str = "", negative: str = "",
             settings: Optional[dict] = None) -> Optional[PromptEntry]:
        """Explicitly save a prompt as a favourite.  Nothing is inferred."""
        if not str(text or "").strip():
            return None
        entries = self.all()
        entry = None
        for item in entries:
            if item.text.strip() == str(text).strip():
                entry = item
                break
        if entry is None:
            entry = PromptEntry(
                id=_new_id("prompt"), text=str(text or ""),
                created_at=datetime.now().isoformat(timespec="seconds"))
            entries.insert(0, entry)
        # Mutate the object that is *in* ``entries``: a separately looked-up
        # entry is a different object, and the change would be thrown away when
        # this list is written back to disk.
        entry.name = str(name or entry.name or "")
        entry.negative = str(negative or entry.negative or "")
        entry.favourite = True
        if settings:
            entry.settings = dict(settings)
        self.store.write({"prompts": [item.to_dict() for item in entries]})
        return entry

    def rename(self, prompt_id: str, name: str) -> bool:
        return self._update(prompt_id, name=str(name or ""))

    def edit(self, prompt_id: str, text: str, *, negative: str = "") -> bool:
        """Change a saved prompt.  Only reachable from an explicit edit."""
        return self._update(prompt_id, text=str(text or ""),
                            negative=str(negative or ""))

    def set_favourite(self, prompt_id: str, favourite: bool = True) -> bool:
        return self._update(prompt_id, favourite=bool(favourite))

    def _update(self, prompt_id: str, **fields: Any) -> bool:
        """Change one entry, in place, in the list that gets written.

        Looking an entry up and then writing a *separate* read back to disk
        silently discards the change.  That was a real bug: favouriting, renaming
        and editing a prompt all appeared to work and saved nothing.
        """
        entries = self.all()
        for entry in entries:
            if entry.id != str(prompt_id):
                continue
            for key, value in fields.items():
                if hasattr(entry, key):
                    setattr(entry, key, value)
            self.store.write({"prompts": [item.to_dict() for item in entries]})
            return True
        return False

    def remove(self, prompt_id: str) -> bool:
        entries = self.all()
        kept = [item for item in entries if item.id != str(prompt_id)]
        if len(kept) == len(entries):
            return False
        self.store.write({"prompts": [item.to_dict() for item in kept]})
        return True

    def clear(self) -> int:
        count = len(self.all())
        self.store.write({"prompts": []})
        return count

    def _write_all(self) -> bool:
        return self.store.write(
            {"prompts": [item.to_dict() for item in self.all()]})
