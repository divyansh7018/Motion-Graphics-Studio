"""The generation cache: what makes "the same thing again" cheap, and honest.

The cache stores a **fingerprint** of a generation - backend, model, operation
and every setting that changes the pixels - and, when a generation finishes,
remembers where its output went.  It is deliberately a *lookup*, not a
substitution:

* a hit is **reported** to the user ("an identical generation already exists"),
  and only reused when the user has turned reuse on;
* anything that changes the output changes the fingerprint, so a cache hit can
  never serve a different picture or clip than the one asked for (section 50);
* a file that has been deleted, or whose size no longer matches, invalidates
  its entry.

This is what makes the cache testable in both directions: a hit, a miss, and an
invalidation are all explicit operations rather than side effects.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event

__all__ = ["GenerationCache", "CacheEntry", "FINGERPRINT_FIELDS"]

#: The request fields that change what comes out.  A field not in this list
#: (a file name, a folder, a note) must not change the fingerprint.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "mode", "prompt", "negative_prompt", "model", "seed", "duration", "fps",
    "width", "height", "strength", "camera", "camera_note", "camera_amount",
    "steps", "guidance", "sampler", "quality", "batch", "source_image",
    "source_video", "reference_image", "style_reference", "extend_from",
    "operation",
)


@dataclass
class CacheEntry:
    """One remembered generation."""

    fingerprint: str = ""
    path: str = ""
    size_bytes: int = 0
    created: float = field(default_factory=time.time)
    backend: str = ""
    model: str = ""
    operation: str = ""
    seconds: float = 0.0
    hits: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> "CacheEntry":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in known})

    def valid(self) -> bool:
        """A hit is only a hit while the file is really there and unchanged."""
        if not self.path:
            return False
        target = Path(self.path)
        if not target.is_file():
            return False
        if not self.size_bytes:
            return True
        try:
            return target.stat().st_size == int(self.size_bytes)
        except OSError:
            return False


class GenerationCache:
    """A small index of finished generations, keyed by fingerprint."""

    def __init__(self, root: Any = None, *, enabled: bool = False,
                 limit: int = 1000) -> None:
        self.root = Path(root) if root else None
        self.path = (self.root / "ai_cache.json") if self.root else None
        self.enabled = bool(enabled)
        self.limit = max(20, int(limit or 1000))
        self._entries: dict[str, CacheEntry] = {}
        self.loaded = False
        self.hits = 0
        self.misses = 0
        self.invalidated = 0

    # -- storage -----------------------------------------------------------

    def load(self) -> dict[str, CacheEntry]:
        if self.loaded:
            return self._entries
        self.loaded = True
        if self.path is None or not self.path.is_file():
            return self._entries
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log_event("AI_CACHE_UNREADABLE",
                      f"The generation cache could not be read: {exc}",
                      level="WARNING")
            return self._entries
        for item in (data.get("entries") or []):
            try:
                entry = CacheEntry.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._entries[entry.fingerprint] = entry
        return self._entries

    def save(self) -> bool:
        if self.path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = sorted(self._entries.values(),
                             key=lambda item: item.created, reverse=True)
            self._entries = {entry.fingerprint: entry
                             for entry in entries[:self.limit]}
            save_with_backup(self.path, {
                "schema_version": 1,
                "entries": [entry.to_dict()
                            for entry in self._entries.values()]})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("AI_CACHE_SAVE_FAILED",
                      f"The generation cache could not be saved: {exc}",
                      level="WARNING")
            return False

    # -- keys --------------------------------------------------------------

    @staticmethod
    def fingerprint(*, backend: str, model: str, operation: str,
                    request: Any) -> str:
        """A stable key for one generation.

        Only the fields that change the output are included, and they are
        sorted, so the same request made twice gives the same key and a changed
        seed, size or prompt gives a different one.
        """
        data = request if isinstance(request, dict) else \
            getattr(request, "to_dict", lambda: {})()
        payload = {key: _canonical(data.get(key)) for key in FINGERPRINT_FIELDS}
        payload.update({"backend": str(backend or ""), "model": str(model or ""),
                        "operation": str(operation or "")})
        text = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    # -- use ---------------------------------------------------------------

    def lookup(self, fingerprint: str) -> Optional[CacheEntry]:
        """The entry for this fingerprint, if it is still valid."""
        entries = self.load()
        entry = entries.get(str(fingerprint))
        if entry is None:
            self.misses += 1
            return None
        if not entry.valid():
            # The file behind it has gone or changed: the entry is dropped
            # rather than served (section 50).
            entries.pop(str(fingerprint), None)
            self.invalidated += 1
            self.save()
            self.misses += 1
            log_event("AI_CACHE_INVALIDATED",
                      "A cached generation no longer matches its file",
                      fingerprint=str(fingerprint)[:12])
            return None
        entry.hits += 1
        self.hits += 1
        return entry

    def remember(self, fingerprint: str, result: Any) -> Optional[CacheEntry]:
        """Record where a finished generation went."""
        path = Path(str(getattr(result, "path", "") or ""))
        if not path.is_file():
            return None
        entry = CacheEntry(
            fingerprint=str(fingerprint), path=str(path),
            size_bytes=path.stat().st_size,
            backend=str(getattr(result, "backend", "") or ""),
            model=str(getattr(result, "model", "") or ""),
            operation=str(getattr(result, "mode", "") or ""),
            seconds=float(getattr(result, "seconds", 0.0) or 0.0))
        self.load()[str(fingerprint)] = entry
        self.save()
        return entry

    def forget(self, fingerprint: str) -> bool:
        entries = self.load()
        if str(fingerprint) not in entries:
            return False
        entries.pop(str(fingerprint), None)
        self.save()
        return True

    def clear(self) -> int:
        count = len(self.load())
        self._entries = {}
        self.save()
        return count

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        entries = self.load()
        total_bytes = 0
        for entry in entries.values():
            total_bytes += int(entry.size_bytes or 0)
        return {"enabled": bool(self.enabled), "entries": len(entries),
                "hits": self.hits, "misses": self.misses,
                "invalidated": self.invalidated,
                "size_bytes": total_bytes}

    def describe(self) -> str:
        if not self.enabled:
            return ("Reuse of identical generations is off. Every Generate "
                    "makes a new file.")
        stats = self.stats()
        return (f"{stats['entries']} remembered generation(s); "
                f"{stats['hits']} reuse(s), {stats['misses']} miss(es), "
                f"{stats['invalidated']} invalidated.")


def _canonical(value: Any) -> Any:
    """A value that fingerprints the same however it was typed."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, float):
        return round(float(value), 4)
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    return value
