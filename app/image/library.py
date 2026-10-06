"""Thumbnails and the image library (Stage F, sections 55, 56, 57, 58, 59, 25).

Two things live here because they share one index:

* **thumbnails** - small cached previews, generated on demand and never by
  loading a grid of full-resolution images into memory;
* **the library** - what is in the image folder, with collections, tags,
  search, duplicate detection and paging.

Both are built to stay responsive with thousands of files: the index reads file
metadata lazily and in pages, thumbnails come from a cache, and nothing scans
the whole folder on the Qt thread.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event

__all__ = [
    "ImageEntry",
    "ImageLibrary",
    "ThumbnailCache",
    "LibraryQuery",
    "COLLECTION_PRESETS",
    "IMAGE_SUFFIXES",
]

IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"})

#: Starting points, not a fixed taxonomy.  Collections are user-defined and
#: these are only offered as suggestions, so no single niche is baked in.
COLLECTION_PRESETS: tuple[str, ...] = (
    "Backgrounds", "Characters", "Objects", "Logos", "Thumbnails",
    "Storytelling", "Charts", "Custom",
)

INDEX_FILE = "library_index.json"
INDEX_VERSION = 1


@dataclass
class ImageEntry:
    """One image in the library."""

    path: str = ""
    name: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    size_bytes: int = 0
    modified: str = ""
    checksum: str = ""
    collection: str = ""
    tags: list = field(default_factory=list)
    prompt: str = ""
    model: str = ""
    seed: Optional[int] = None
    project: str = ""
    #: Set when the file has gone since it was indexed.
    missing: bool = False

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    def matches(self, query: "LibraryQuery") -> bool:
        if query.collection and self.collection != query.collection:
            return False
        if query.project and self.project != query.project:
            return False
        if query.tags:
            wanted = {str(tag).lower() for tag in query.tags}
            if not wanted.issubset({str(tag).lower() for tag in self.tags}):
                return False
        if query.min_width and self.width < query.min_width:
            return False
        if query.min_height and self.height < query.min_height:
            return False
        text = str(query.text or "").strip().lower()
        if text:
            haystack = " ".join([
                self.name, self.prompt, self.model, self.collection,
                " ".join(str(tag) for tag in self.tags), self.project,
            ]).lower()
            if text not in haystack:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "width": self.width,
            "height": self.height, "format": self.format,
            "size_bytes": self.size_bytes, "modified": self.modified,
            "checksum": self.checksum, "collection": self.collection,
            "tags": list(self.tags or []), "prompt": self.prompt,
            "model": self.model, "seed": self.seed, "project": self.project,
            "missing": bool(self.missing),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImageEntry":
        payload = {key: value for key, value in dict(data or {}).items()
                   if key in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**payload)


@dataclass
class LibraryQuery:
    """A search over the library."""

    text: str = ""
    collection: str = ""
    project: str = ""
    tags: list = field(default_factory=list)
    min_width: int = 0
    min_height: int = 0
    #: Page controls: a folder with thousands of images is never all loaded.
    limit: int = 200
    offset: int = 0

    def to_dict(self) -> dict:
        return {"text": self.text, "collection": self.collection,
                "project": self.project, "tags": list(self.tags or []),
                "min_width": self.min_width, "min_height": self.min_height,
                "limit": self.limit, "offset": self.offset}


@dataclass
class LibraryPage:
    """One page of results, with the total so the UI can show "1-200 of 4123"."""

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


class ThumbnailCache:
    """Small previews, cached on disk outside the library folder.

    The cache key includes the source size and modification time, so replacing
    an image invalidates its thumbnail rather than showing the old one.
    """

    def __init__(self, cache_dir: Any, size: int = 256) -> None:
        self.cache_dir = Path(cache_dir)
        self.size = max(32, int(size or 256))

    # -- keys --------------------------------------------------------------

    def key_for(self, image_path: Any, *, size: int = 0) -> str:
        path = Path(image_path)
        size = size or self.size
        try:
            stat = path.stat()
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            stamp = "missing"
        digest = hashlib.sha256(
            f"{path.resolve()}|{size}|{stamp}".encode("utf-8")).hexdigest()[:24]
        return digest

    def path_for(self, image_path: Any, *, size: int = 0) -> Path:
        size = size or self.size
        return self.cache_dir / f"{size}" / f"{self.key_for(image_path, size=size)}.png"

    # -- generation --------------------------------------------------------

    def get(self, image_path: Any, *, size: int = 0,
            regenerate: bool = False) -> Optional[Path]:
        """The cached thumbnail, generating it if it is not there yet."""
        target = self.path_for(image_path, size=size)
        if target.is_file() and not regenerate:
            return target
        return self.create(image_path, size=size)

    def create(self, image_path: Any, *, size: int = 0) -> Optional[Path]:
        """Build one thumbnail.  Returns ``None`` if the source is unreadable."""
        from PIL import Image

        source = Path(image_path)
        size = size or self.size
        if not source.is_file():
            return None
        target = self.path_for(source, size=size)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as image:
                image.load()
                thumbnail = image.copy()
            thumbnail.thumbnail((size, size), Image.LANCZOS)
            # A thumbnail of a transparent image on a transparent background is
            # invisible in a light UI, so composite onto a neutral square.
            if thumbnail.mode in ("RGBA", "LA", "P"):
                from PIL import Image as _Image

                background = _Image.new("RGB", thumbnail.size, (24, 26, 32))
                background.paste(thumbnail.convert("RGBA"),
                                 (0, 0), thumbnail.convert("RGBA").getchannel("A"))
                thumbnail = background
            temporary = target.with_suffix(".part")
            thumbnail.save(temporary, format="PNG", optimize=True)
            os.replace(str(temporary), str(target))
        except Exception as exc:  # noqa: BLE001 - a bad file has no thumbnail
            log_event("IMAGE_THUMBNAIL_FAILED", "A thumbnail could not be made",
                      path=str(source), error=str(exc))
            return None
        return target

    def clear(self) -> int:
        """Delete every cached thumbnail.  Returns how many were removed."""
        removed = 0
        if not self.cache_dir.is_dir():
            return 0
        for path in self.cache_dir.rglob("*.png"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


class ImageLibrary:
    """The image folder, indexed.

    The index is a cache: it can be deleted at any time and is rebuilt by
    scanning.  Metadata is read lazily - a page of 200 entries does not open
    4,000 files.
    """

    def __init__(self, root: Any, *, index_name: str = INDEX_FILE) -> None:
        self.root = Path(root)
        self.index_path = self.root / index_name
        self._entries: Optional[dict[str, ImageEntry]] = None
        self._collections: dict[str, str] = {}

    # -- scanning ----------------------------------------------------------

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def image_paths(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(path for path in self.root.rglob("*")
                      if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
                      and not path.name.startswith("."))

    def scan(self, *, checksums: bool = False,
             progress: Any = None) -> int:
        """Walk the folder and index every image.  Returns how many were found.

        An image that was indexed before and has since gone is **kept** in the
        index, marked ``missing``, rather than being silently dropped: a user
        who moves a file should be told their image has gone, not find it
        quietly absent from the library (directive section 21).

        ``checksums=True`` hashes each file for duplicate detection; it is off
        by default because hashing thousands of images is slow, and duplicates
        are usually found by size and dimensions first.
        """
        self.ensure_root()
        stored = self._stored_meta()
        entries: dict[str, ImageEntry] = {}
        paths = self.image_paths()
        for index, path in enumerate(paths):
            entry = self._index_one(path, stored.get(str(path)),
                                    checksums=checksums)
            entries[str(path)] = entry
            if progress is not None and paths:
                progress((index + 1) / len(paths))

        vanished = 0
        for key, previous in stored.items():
            if key in entries:
                continue
            entry = ImageEntry.from_dict(previous)
            entry.missing = True
            entries[key] = entry
            vanished += 1

        self._entries = entries
        self._write_index()
        if vanished:
            log_event("IMAGE_LIBRARY_MISSING",
                      f"{vanished} indexed image(s) are no longer on disk",
                      root=str(self.root), count=vanished)
        log_event("IMAGE_LIBRARY_INDEXED",
                  f"{len(entries) - vanished} image(s) indexed",
                  root=str(self.root))
        return len(entries) - vanished

    def _index_one(self, path: Path, previous: Optional[dict],
                   *, checksums: bool = False) -> ImageEntry:
        entry = ImageEntry(path=str(path), name=path.stem)
        try:
            stat = path.stat()
        except OSError:
            entry.missing = True
            return entry
        entry.size_bytes = int(stat.st_size)
        entry.modified = datetime.fromtimestamp(stat.st_mtime).isoformat(
            timespec="seconds")

        # Reuse what the index already knew when the file has not changed.
        unchanged = bool(previous) and not previous.get("missing") \
            and int(previous.get("size_bytes") or 0) == entry.size_bytes \
            and str(previous.get("modified") or "") == entry.modified
        if unchanged:
            cached = ImageEntry.from_dict(previous)
            cached.modified = entry.modified
            cached.size_bytes = entry.size_bytes
            return cached

        from .metadata import read_metadata
        from .validation import validate_image_file

        check = validate_image_file(path)
        if check.ok:
            entry.width, entry.height = check.width, check.height
            entry.format = check.format
        metadata = read_metadata(path)
        if metadata is not None:
            entry.prompt = metadata.prompt
            entry.model = metadata.model
            entry.seed = metadata.seed
            entry.collection = metadata.collection
            entry.tags = list(metadata.tags or [])
            entry.project = metadata.project
        if checksums:
            entry.checksum = file_checksum(path)
        return entry

    # -- queries -----------------------------------------------------------

    def entries(self) -> list[ImageEntry]:
        if self._entries is None:
            self._load_index()
        return list((self._entries or {}).values())

    def query(self, query: Optional[LibraryQuery] = None) -> LibraryPage:
        """One page of matching entries.  Never returns the whole library."""
        request = query or LibraryQuery()
        matched = [entry for entry in self.entries() if entry.matches(request)]
        matched.sort(key=lambda item: item.modified or "", reverse=True)
        total = len(matched)
        start = max(0, int(request.offset or 0))
        limit = max(1, int(request.limit or 200))
        return LibraryPage(entries=matched[start:start + limit], total=total,
                           offset=start, limit=limit)

    def collections(self) -> dict[str, int]:
        """Collection name -> how many images are in it."""
        counts: dict[str, int] = {}
        for entry in self.entries():
            name = entry.collection or "Uncategorised"
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def all_tags(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            for tag in entry.tags or []:
                key = str(tag)
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def entry_for(self, path: Any) -> Optional[ImageEntry]:
        key = str(Path(path))
        for entry in self.entries():
            if entry.path == key:
                return entry
        return None

    # -- duplicates --------------------------------------------------------

    def duplicates(self, *, use_checksum: bool = False) -> list[list[ImageEntry]]:
        """Groups of images that look like the same picture.

        Nothing is ever deleted: this only reports, and the user decides.
        """
        buckets: dict[tuple, list[ImageEntry]] = {}
        for entry in self.entries():
            if entry.missing or entry.size_bytes <= 0:
                continue
            if use_checksum:
                if not entry.checksum:
                    entry.checksum = file_checksum(Path(entry.path))
                key: tuple = ("sha", entry.checksum)
            else:
                # Size plus dimensions catches the obvious cases (the same file
                # imported twice) without hashing every image on disk.
                key = ("sd", entry.size_bytes, entry.width, entry.height)
            buckets.setdefault(key, []).append(entry)
        return [group for group in buckets.values() if len(group) > 1]

    # -- mutations ---------------------------------------------------------

    def set_tags(self, path: Any, tags: Iterable[str]) -> bool:
        """Replace an image's tags, in the index and in its sidecar."""
        entry = self.entry_for(path)
        if entry is None:
            return False
        cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
        entry.tags = cleaned
        self._persist_entry(entry)
        return True

    def add_tag(self, path: Any, tag: str) -> bool:
        entry = self.entry_for(path)
        if entry is None or not str(tag).strip():
            return False
        tags = list(entry.tags or [])
        if str(tag).strip() not in tags:
            tags.append(str(tag).strip())
        return self.set_tags(path, tags)

    def set_collection(self, path: Any, collection: str) -> bool:
        entry = self.entry_for(path)
        if entry is None:
            return False
        entry.collection = str(collection or "").strip()
        self._persist_entry(entry)
        return True

    def remove(self, path: Any, *, delete_file: bool = False) -> bool:
        """Drop an image from the library.  The file is only deleted if asked."""
        key = str(Path(path))
        if self._entries is None:
            self._load_index()
        entry = (self._entries or {}).pop(key, None)
        if entry is None:
            return False
        if delete_file:
            try:
                Path(key).unlink(missing_ok=True)
            except OSError:
                pass
        self._write_index()
        return True

    def add_entry(self, path: Any, *, checksums: bool = False) -> ImageEntry:
        """Index one file, without rescanning the whole folder."""
        if self._entries is None:
            self._load_index()
        entry = self._index_one(Path(path), None, checksums=checksums)
        if self._entries is None:
            self._entries = {}
        self._entries[entry.path] = entry
        self._write_index()
        return entry

    # -- index storage -----------------------------------------------------

    def _stored_meta(self) -> dict[str, dict]:
        if not self.index_path.is_file():
            return {}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if int(payload.get("version") or 0) != INDEX_VERSION:
            return {}
        return {str(item.get("path")): dict(item)
                for item in payload.get("entries") or []}

    def _load_index(self) -> None:
        stored = self._stored_meta()
        self._entries = {key: ImageEntry.from_dict(value)
                         for key, value in stored.items()}

    def save(self) -> None:
        """Persist the index now.

        ``add_entry`` already writes as it goes; this exists so a caller that
        mutates several entries (a batch, or a service closing down) can flush
        once rather than after every change.
        """
        self._write_index()

    def save_entry(self, path: Any) -> bool:
        """Persist one entry, including its tags and collection sidecar."""
        entry = self.entry_for(path)
        if entry is None:
            return False
        self._persist_entry(entry)
        return True

    def _write_index(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"version": INDEX_VERSION,
                       "updated": datetime.now().isoformat(timespec="seconds"),
                       "root": str(self.root),
                       "entries": [entry.to_dict()
                                   for entry in (self._entries or {}).values()]}
            temporary = self.index_path.with_suffix(".part")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(str(temporary), str(self.index_path))
        except OSError as exc:
            log_event("IMAGE_LIBRARY_INDEX_FAILED", "The index could not be saved",
                      root=str(self.root), error=str(exc))

    def _persist_entry(self, entry: ImageEntry) -> None:
        """Write tags/collection into the image's sidecar and the index."""
        from .metadata import read_metadata, write_metadata, ImageMetadata

        target = Path(entry.path)
        if target.is_file():
            metadata = read_metadata(target) or ImageMetadata()
            metadata.tags = list(entry.tags or [])
            metadata.collection = str(entry.collection or "")
            if not metadata.asset_id:
                metadata.asset_id = entry.name
            write_metadata(target, metadata)
        self._write_index()


def file_checksum(path: Any, *, algorithm: str = "sha256",
                  chunk: int = 1 << 20) -> str:
    """Hash a file in chunks, so a large image never sits in memory at once."""
    digest = hashlib.new(algorithm)
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()
