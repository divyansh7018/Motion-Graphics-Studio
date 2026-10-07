"""Reference images, stored apart and never overwritten (sections 15, 16, 44).

A reference is not a generation: it is an input the user chose, and it is kept
in its own folder by kind, with a copy made on import so the original file is
untouched.  Nothing here ever writes over an existing file - a second import of
the same picture gets its own copy - and nothing is sent anywhere.

Consistency (sections 16, 17) is *only* claimed where the backend's own
metadata says it supports it.  This module therefore records which backends
have been told about a reference; the studio asks the backend before offering
the feature, and a backend that does not support references is never offered
them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event

__all__ = ["ReferenceKind", "Reference", "ReferenceStore", "REFERENCE_KINDS",
           "KIND_LABELS"]

#: What a reference can be.  The names match the directive's list.
REFERENCE_KINDS: tuple[str, ...] = ("character", "style", "object",
                                    "composition", "pose", "environment")

KIND_LABELS: dict[str, str] = {
    "character": "Character",
    "style": "Style",
    "object": "Object",
    "composition": "Composition",
    "pose": "Pose",
    "environment": "Environment",
}


class ReferenceKind:
    CHARACTER = "character"
    STYLE = "style"
    OBJECT = "object"
    COMPOSITION = "composition"
    POSE = "pose"
    ENVIRONMENT = "environment"


@dataclass
class Reference:
    """One reference image the user imported."""

    id: str = ""
    kind: str = "character"
    name: str = ""
    path: str = ""          # the stored copy
    source: str = ""        # where it was copied from
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    checksum: str = ""
    created: float = field(default_factory=time.time)
    project: str = ""
    notes: str = ""
    tags: list = field(default_factory=list)
    #: Which backends have been told about this reference (never assumed).
    used_by: list = field(default_factory=list)

    def label(self) -> str:
        return self.name or Path(self.path).name

    def detail(self) -> str:
        bits = [KIND_LABELS.get(self.kind, self.kind)]
        if self.width and self.height:
            bits.append(f"{self.width}x{self.height}")
        return ", ".join(bits)

    def to_dict(self) -> dict:
        data = dict(self.__dict__)
        data["label"] = self.label()
        data["detail"] = self.detail()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Reference":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in dict(data or {}).items()
                      if key in known})


class ReferenceStore:
    """The reference folder plus its index."""

    def __init__(self, root: Any = None, *, index_name: str = "references.json") -> None:
        self.root = Path(root) / "references" if root else None
        self.index_path = (self.root / index_name) if self.root else None
        self._references: list[Reference] = []
        self.loaded = False

    # -- storage -----------------------------------------------------------

    def load(self) -> list[Reference]:
        if self.loaded:
            return self._references
        self.loaded = True
        if self.index_path is None or not self.index_path.is_file():
            return self._references
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log_event("AI_REFERENCES_UNREADABLE",
                      f"The reference index could not be read: {exc}",
                      level="WARNING")
            return self._references
        for item in (data.get("references") or []):
            try:
                self._references.append(Reference.from_dict(item))
            except (TypeError, ValueError):
                continue
        return self._references

    def save(self) -> bool:
        if self.index_path is None:
            return False
        try:
            from ..core.atomicio import save_with_backup

            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            save_with_backup(self.index_path, {
                "schema_version": 1,
                "references": [item.to_dict() for item in self._references]})
            return True
        except Exception as exc:  # noqa: BLE001
            log_event("AI_REFERENCES_SAVE_FAILED",
                      f"The reference index could not be saved: {exc}",
                      level="WARNING")
            return False

    # -- reading -----------------------------------------------------------

    def all(self) -> list[Reference]:
        return self.load()

    def folder_for(self, kind: str) -> Optional[Path]:
        if self.root is None:
            return None
        chosen = str(kind or ReferenceKind.CHARACTER)
        if chosen not in REFERENCE_KINDS:
            chosen = ReferenceKind.CHARACTER
        return self.root / chosen

    def of_kind(self, kind: str) -> list[Reference]:
        return [item for item in self.all() if item.kind == str(kind)]

    def find(self, reference_id: str) -> Optional[Reference]:
        for item in self.all():
            if item.id == reference_id:
                return item
        return None

    def for_project(self, project: str) -> list[Reference]:
        wanted = str(project or "")
        return [item for item in self.all() if item.project == wanted]

    def missing(self) -> list[Reference]:
        return [item for item in self.all()
                if item.path and not Path(item.path).is_file()]

    # -- writing -----------------------------------------------------------

    def add(self, source: Any, *, kind: str = "character", name: str = "",
            project: str = "", notes: str = "", tags: Any = (),
            copy: bool = True) -> Reference:
        """Import a reference image, always as a new file.

        ``copy`` may be turned off when the source already lives inside the
        reference folder, but the stored name is still made unique, so an
        existing file is never overwritten (section 44).
        """
        self.load()
        origin = Path(source)
        if not origin.is_file():
            raise FileNotFoundError(
                f"The reference image was not found: {origin}")
        chosen = str(kind or ReferenceKind.CHARACTER)
        if chosen not in REFERENCE_KINDS:
            raise ValueError(
                f"'{chosen}' is not a kind of reference. Choose one of: "
                + ", ".join(REFERENCE_KINDS) + ".")
        folder = self.folder_for(chosen)
        if folder is None:
            raise RuntimeError("No reference folder is configured, so nothing "
                               "was imported.")
        folder.mkdir(parents=True, exist_ok=True)
        stem = _safe(Path(name).stem if name else origin.stem)
        target = _unique(folder, stem, origin.suffix.lower() or ".png")
        if copy:
            shutil.copy2(origin, target)
        else:
            target = origin
        width, height = _image_size(target)
        reference = Reference(
            id=f"ref_{uuid.uuid4().hex[:10]}", kind=chosen,
            name=str(name or origin.stem), path=str(target), source=str(origin),
            size_bytes=target.stat().st_size if target.is_file() else 0,
            width=width, height=height, checksum=_digest(target),
            project=str(project or ""), notes=str(notes or ""),
            tags=[str(item) for item in (tags or []) if str(item).strip()])
        self._references.insert(0, reference)
        self.save()
        log_event("AI_REFERENCE_ADDED",
                  f"Reference '{reference.label()}' was imported",
                  kind=chosen, path=str(target))
        return reference

    def note_use(self, reference_id: str, backend_id: str) -> bool:
        """Record that a backend was told about this reference."""
        item = self.find(reference_id)
        if item is None:
            return False
        if str(backend_id) and str(backend_id) not in item.used_by:
            item.used_by.append(str(backend_id))
            self.save()
        return True

    def update(self, reference_id: str, *, name: str = "", notes: str = "",
               tags: Any = None) -> bool:
        item = self.find(reference_id)
        if item is None:
            return False
        if name:
            item.name = str(name)
        if notes:
            item.notes = str(notes)
        if tags is not None:
            item.tags = [str(tag) for tag in tags if str(tag).strip()]
        self.save()
        return True

    def remove(self, reference_id: str, *, delete_file: bool = False) -> bool:
        """Forget a reference; the copy is deleted only when explicitly asked."""
        item = self.find(reference_id)
        if item is None:
            return False
        self._references = [entry for entry in self._references
                            if entry.id != reference_id]
        self.save()
        if delete_file and item.path:
            try:
                Path(item.path).unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - a locked file
                log_event("AI_REFERENCE_DELETE_FAILED",
                          f"{item.path} could not be deleted: {exc}",
                          level="WARNING")
                return False
        return True

    def describe(self) -> str:
        if not self.all():
            return "No reference images yet."
        counts: dict[str, int] = {}
        for item in self.all():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return "References: " + ", ".join(
            f"{counts[kind]} {KIND_LABELS.get(kind, kind).lower()}"
            for kind in REFERENCE_KINDS if kind in counts)


def _safe(stem: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_ " else "_"
                      for char in str(stem or "reference")).strip()
    return cleaned[:60] or "reference"


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    index = 0
    while candidate.exists():
        index += 1
        candidate = folder / f"{stem}_{index}{suffix}"
    return candidate


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as handle:
            return int(handle.width), int(handle.height)
    except Exception:  # noqa: BLE001 - a size is a bonus, not a requirement
        return 0, 0


def _digest(path: Path, *, limit: int = 2 * 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            remaining = int(limit)
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return ""
    return hasher.hexdigest()[:32]
