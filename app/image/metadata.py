"""Image metadata (Stage F, sections 22, 32, 33, 54).

What made this picture, kept with the picture.  Two copies, on purpose:

* a **sidecar JSON** next to the image, which is the authoritative record and
  holds everything, including edit history and variant relationships;
* the generation fields also written **inside** a PNG's text chunks, so an image
  that is copied somewhere on its own still carries its prompt and seed.

Only fields that actually exist are stored.  A backend that has no sampler does
not get an empty ``sampler`` key invented for it, because then a reader could
not tell "not supported" from "not recorded".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event

__all__ = ["ImageMetadata", "EditStep", "read_metadata", "write_metadata",
           "sidecar_path", "embed_png_metadata"]

#: Current sidecar format.  Bumped only when the shape actually changes.
METADATA_VERSION = 1

#: Fields written into the PNG itself.  The rest live in the sidecar.
PNG_KEYS: tuple[str, ...] = (
    "prompt", "negative_prompt", "model", "backend", "seed", "steps",
    "guidance", "sampler", "mode", "source_image",
)


@dataclass
class EditStep:
    """One entry in an image's edit history."""

    at: str = ""
    operation: str = ""
    #: The parameters, exactly as applied.
    params: dict = field(default_factory=dict)
    #: Where this step produced a new file, if it did.
    output: str = ""

    def to_dict(self) -> dict:
        return {"at": self.at, "operation": self.operation,
                "params": dict(self.params or {}), "output": self.output}

    @classmethod
    def from_dict(cls, data: dict) -> "EditStep":
        return cls(at=str(data.get("at", "")),
                   operation=str(data.get("operation", "")),
                   params=dict(data.get("params") or {}),
                   output=str(data.get("output", "")))


@dataclass
class ImageMetadata:
    """Everything known about one image.

    Every field has a falsy default so an absent value stays absent in the
    serialised form - see :meth:`to_dict`.
    """

    version: int = METADATA_VERSION
    # -- generation --------------------------------------------------------
    prompt: str = ""
    negative_prompt: str = ""
    model: str = ""
    backend: str = ""
    seed: Optional[int] = None
    steps: Optional[int] = None
    guidance: Optional[float] = None
    sampler: str = ""
    mode: str = ""
    source_image: str = ""
    mask_image: str = ""
    reference_image: str = ""
    #: The file this record was built from, when it came out of a generation.
    source_file: str = ""
    #: The exact settings a regenerate would replay.
    settings: dict = field(default_factory=dict)
    # -- the file ----------------------------------------------------------
    width: int = 0
    height: int = 0
    format: str = ""
    size_bytes: int = 0
    created_at: str = ""
    generation_seconds: float = 0.0
    # -- library -----------------------------------------------------------
    name: str = ""
    asset_id: str = ""
    project: str = ""
    collection: str = ""
    tags: list = field(default_factory=list)
    # -- relationships -----------------------------------------------------
    #: "original" | "variation" | "edit" | "upscale" | "import"
    origin: str = ""
    parent: str = ""
    children: list = field(default_factory=list)
    #: Human-readable trace: source -> variation -> edit -> upscale.
    lineage: list = field(default_factory=list)
    edits: list = field(default_factory=list)
    #: Anything a backend recorded that this module does not model.
    extra: dict = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_generation(cls, request: Any, result: Any, *, index: int = 0,
                        path: Any = None) -> "ImageMetadata":
        """Build the record for one image a backend just produced."""
        now = datetime.now().isoformat(timespec="seconds")
        seeds = list(getattr(result, "seeds", []) or [])
        paths = list(getattr(result, "paths", []) or [])
        seed = seeds[index] if index < len(seeds) else (seeds[0] if seeds else None)
        source = str(paths[index]) if index < len(paths) else \
            (str(paths[0]) if paths else "")
        record = cls(
            prompt=str(getattr(request, "prompt", "") or ""),
            negative_prompt=str(getattr(request, "negative_prompt", "") or ""),
            model=str(getattr(result, "model", "") or getattr(request, "model", "") or ""),
            backend=str(getattr(result, "backend", "") or ""),
            seed=int(seed) if seed is not None else None,
            steps=int(getattr(request, "steps", 0) or 0) or None,
            guidance=float(getattr(request, "guidance", 0.0) or 0.0) or None,
            sampler=str(getattr(request, "sampler", "") or ""),
            mode=str(getattr(request, "mode", "") or ""),
            source_image=str(getattr(request, "source_image", "") or ""),
            mask_image=str(getattr(request, "mask_image", "") or ""),
            reference_image=str(getattr(request, "reference_image", "") or ""),
            settings=dict(getattr(request, "to_dict", lambda: {})() or {}),
            width=int(getattr(result, "width", 0) or 0),
            height=int(getattr(result, "height", 0) or 0),
            format=str(getattr(result, "output_format", "") or ""),
            created_at=now,
            generation_seconds=float(getattr(result, "seconds", 0.0) or 0.0),
            project=str(getattr(request, "project", "") or ""),
            collection=str(getattr(request, "collection", "") or ""),
            tags=list(getattr(request, "tags", []) or []),
            origin="variation" if str(getattr(request, "mode", "")) == "variation"
            else "original",
            parent=str(getattr(request, "parent_asset", "") or ""),
            source_file=source,
        )
        if path is not None:
            record.size_bytes = Path(path).stat().st_size if Path(path).exists() else 0
        return record

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        """Only what exists.  Absent optional values are left out entirely."""
        data: dict[str, Any] = {"version": int(self.version)}
        for key in ("prompt", "negative_prompt", "model", "backend", "sampler",
                    "mode", "source_image", "mask_image", "reference_image",
                    "name", "asset_id", "project", "collection", "format",
                    "origin", "parent", "created_at"):
            value = getattr(self, key)
            if value:
                data[key] = value
        for key in ("seed", "steps", "width", "height", "size_bytes"):
            value = getattr(self, key)
            if value is not None and value != 0:
                data[key] = value
        if self.guidance:
            data["guidance"] = float(self.guidance)
        if self.generation_seconds:
            data["generation_seconds"] = round(float(self.generation_seconds), 3)
        for key in ("tags", "children", "lineage"):
            value = list(getattr(self, key) or [])
            if value:
                data[key] = value
        if self.edits:
            data["edits"] = [step.to_dict() if isinstance(step, EditStep)
                             else dict(step) for step in self.edits]
        if self.settings:
            data["settings"] = dict(self.settings)
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ImageMetadata":
        data = dict(data or {})
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload: dict[str, Any] = {key: value for key, value in data.items()
                                   if key in known}
        extra = {key: value for key, value in data.items() if key not in known}
        if extra:
            payload["extra"] = dict(payload.get("extra") or {}, **extra)
        payload["edits"] = [EditStep.from_dict(step) if isinstance(step, dict)
                            else step for step in payload.get("edits") or []]
        return cls(**payload)

    def png_text(self) -> dict[str, str]:
        """The subset that fits in a PNG text chunk."""
        text: dict[str, str] = {}
        for key in PNG_KEYS:
            value = getattr(self, key, "")
            if value not in (None, "", 0):
                text[f"mgs_{key}"] = str(value)
        return text

    def add_edit(self, operation: str, params: Optional[dict] = None,
                 output: Any = None) -> EditStep:
        step = EditStep(at=datetime.now().isoformat(timespec="seconds"),
                        operation=str(operation), params=dict(params or {}),
                        output=str(output or ""))
        self.edits.append(step)
        return step

    def describe(self) -> str:
        lines: list[str] = []
        if self.prompt:
            lines.append(f"Prompt: {self.prompt}")
        if self.negative_prompt:
            lines.append(f"Negative: {self.negative_prompt}")
        if self.model:
            lines.append(f"Model: {self.model}"
                         + (f" ({self.backend})" if self.backend else ""))
        if self.seed is not None:
            lines.append(f"Seed: {self.seed}")
        if self.steps is not None:
            lines.append(f"Steps: {self.steps}")
        if self.guidance:
            lines.append(f"Guidance: {self.guidance}")
        if self.sampler:
            lines.append(f"Sampler: {self.sampler}")
        if self.mode:
            lines.append(f"Mode: {self.mode}")
        if self.width and self.height:
            lines.append(f"Resolution: {self.width}x{self.height}")
        if self.format:
            lines.append(f"Format: {self.format}")
        if self.created_at:
            lines.append(f"Created: {self.created_at}")
        if self.lineage:
            lines.append("Lineage: " + " -> ".join(str(x) for x in self.lineage))
        if self.edits:
            lines.append(f"Edits: {len(self.edits)}")
        if self.tags:
            lines.append("Tags: " + ", ".join(str(t) for t in self.tags))
        if self.collection:
            lines.append(f"Collection: {self.collection}")
        if not lines:
            return "No metadata recorded for this image."
        return "\n".join(lines)


def sidecar_path(image_path: Any) -> Path:
    """Where an image's metadata lives: ``name.png`` -> ``name.png.json``."""
    return Path(image_path).with_suffix(Path(image_path).suffix + ".json")


def write_metadata(image_path: Any, metadata: ImageMetadata) -> Optional[Path]:
    """Write the sidecar, atomically, and embed what fits into a PNG."""
    target = sidecar_path(image_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_text(json.dumps(metadata.to_dict(), indent=2,
                                        ensure_ascii=False), encoding="utf-8")
        import os

        os.replace(str(temporary), str(target))
    except OSError as exc:
        log_event("IMAGE_METADATA_FAILED", "Metadata could not be saved",
                  path=str(target), error=str(exc))
        return None

    if str(image_path).lower().endswith(".png"):
        try:
            embed_png_metadata(image_path, metadata)
        except Exception as exc:  # noqa: BLE001 - the sidecar is enough
            log_event("IMAGE_METADATA_EMBED_SKIPPED", "PNG text could not be "
                                                      "embedded",
                      path=str(image_path), error=str(exc))
    return target


def embed_png_metadata(image_path: Any, metadata: ImageMetadata) -> bool:
    """Add the generation fields to a PNG's own text chunks."""
    from PIL import Image, PngImagePlugin

    path = Path(image_path)
    text = metadata.png_text()
    if not text:
        return False
    png_info = PngImagePlugin.PngInfo()
    with Image.open(path) as image:
        image.load()
        for key, value in (image.info or {}).items():
            if isinstance(value, str) and key.startswith("mgs_"):
                png_info.add_text(key, value)
        for key, value in text.items():
            png_info.add_text(key, value)
        # Rewrite in place via a temporary file: never a half-written PNG.
        from .saving import atomic_write_bytes
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", pnginfo=png_info, optimize=True)
        atomic_write_bytes(path, buffer.getvalue())
    return True


def read_metadata(image_path: Any) -> Optional[ImageMetadata]:
    """The sidecar if there is one, else whatever the file itself carries."""
    target = sidecar_path(image_path)
    if target.is_file():
        try:
            return ImageMetadata.from_dict(
                json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log_event("IMAGE_METADATA_UNREADABLE", "Metadata could not be read",
                      path=str(target), error=str(exc))
    return read_embedded_metadata(image_path)


def read_embedded_metadata(image_path: Any) -> Optional[ImageMetadata]:
    """Read the generation fields back out of a PNG's text chunks."""
    path = Path(image_path)
    if not path.is_file() or path.suffix.lower() != ".png":
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            info = dict(image.info or {})
    except Exception:  # noqa: BLE001 - an unreadable PNG has no metadata
        return None
    payload: dict[str, Any] = {}
    for key, value in info.items():
        if not str(key).startswith("mgs_"):
            continue
        name = str(key)[4:]
        if name in ("seed", "steps", "width", "height", "size_bytes"):
            try:
                payload[name] = int(value)
                continue
            except (TypeError, ValueError):
                pass
        if name == "guidance":
            try:
                payload[name] = float(value)
                continue
            except (TypeError, ValueError):
                pass
        payload[name] = str(value)
    if not payload:
        return None
    payload.setdefault("origin", "original")
    return ImageMetadata.from_dict(payload)
