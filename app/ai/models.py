"""Models: finding them, describing them, loading one at a time.

Three jobs live here (sections 5, 6, 11, 12, 41, 42, 92):

* **discovery** - look in the folders a user configured and in the usual
  application folders, and say what was found without loading anything;
* **requirements** - say whether a model needs a CPU, wants a GPU or cannot run
  without one, using measured device facts and the model's own metadata, never
  a guess dressed up as a fact;
* **loading** - one model at a time, lazily, with the previous one unloaded
  first, so a machine with 16 GB of RAM is not asked to hold two models.

Nothing here loads a model to answer a question.  ``estimate_memory`` is
arithmetic on numbers that exist (file sizes, dtype), and when a model does not
state enough to estimate, the answer is "unknown" rather than a made-up number
(section 86).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.logging_setup import log_event
from .types import DeviceRequirement

__all__ = [
    "WEIGHT_SUFFIXES", "ModelFile", "MemoryEstimate", "ModelStore",
    "LoadedModel", "ModelCache", "human_bytes", "detect_requirement",
];

#: File types that are model weights, not documentation.  ``.json`` is listed
#: because some models ship a single descriptor - never all of them.
WEIGHT_SUFFIXES: tuple[str, ...] = (".safetensors", ".bin", ".ckpt", ".pt",
                                    ".pth", ".onnx", ".gguf", ".ggml")

#: Files that describe a model but are not allowed to make a folder "a model"
#: on their own.
DESCRIPTOR_NAMES: tuple[str, ...] = ("model_index.json", "config.json",
                                     "model.json", "MODEL_CARD.md")

#: Names that mark a folder as video rather than image, used only for sorting.
VIDEO_HINTS: tuple[str, ...] = ("video", "cog", "wan", "ltx", "svd", "i2v",
                                "t2v", "animatediff", "motion")

BYTES_PER_PARAM = {"float32": 4.0, "fp32": 4.0, "f32": 4.0,
                   "float16": 2.0, "fp16": 2.0, "f16": 2.0, "bfloat16": 2.0,
                   "int8": 1.0, "uint8": 1.0, "int4": 0.5, "q4": 0.5,
                   "q8": 1.0}


def _field(model: Any, name: str, default: Any = None) -> Any:
    """One value from a model record, whether it is an object or a mapping.

    The manager carries models as objects (a backend's own model type) and as
    dictionaries (what the model list shows).  Reading them the same way here
    means an estimate or a "what is missing" answer is never wrong just because
    of the shape it arrived in.
    """
    if isinstance(model, dict):
        return model.get(name, default)
    return getattr(model, name, default)


def human_bytes(size: int) -> str:
    """A size a person can read, and the exact number of bytes as a fallback."""
    size = int(size or 0)
    if size <= 0:
        return ""
    for unit, divisor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= divisor:
            value = size / divisor
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{size} bytes"


@dataclass
class ModelFile:
    """One file on disk that could be a model."""

    path: Path
    size_bytes: int = 0
    suffix: str = ""

    def to_dict(self) -> dict:
        return {"path": str(self.path), "size_bytes": int(self.size_bytes),
                "suffix": self.suffix, "size": human_bytes(self.size_bytes)}


@dataclass
class MemoryEstimate:
    """What a model will cost, with the evidence it is based on (section 42)."""

    #: Estimated memory for the weights alone.
    weights_bytes: int = 0
    #: Estimated peak for one generation, by rule of thumb from the weights.
    peak_bytes: int = 0
    #: ``known`` scales (a size was found), ``unknown`` otherwise.
    confidence: str = "unknown"
    basis: str = ""
    dtype: str = "float32"
    #: What the machine has free, if it was measured.
    available_bytes: int = 0

    @property
    def exceeds_available(self) -> bool:
        return bool(self.available_bytes) and \
            int(self.peak_bytes) > int(self.available_bytes)

    @property
    def known(self) -> bool:
        return self.confidence == "known"

    def describe(self) -> str:
        if not self.known:
            return ("Not enough information to estimate the memory this model "
                    "needs. The estimate is unavailable rather than guessed.")
        line = (f"About {human_bytes(self.peak_bytes)} at peak "
                f"({human_bytes(self.weights_bytes)} of weights, "
                f"{self.dtype}).")
        if self.available_bytes:
            line += f" About {human_bytes(self.available_bytes)} is available."
            if self.exceeds_available:
                line += " This may exceed available memory."
        return line

    def to_dict(self) -> dict:
        return {"weights_bytes": int(self.weights_bytes),
                "peak_bytes": int(self.peak_bytes),
                "weights": human_bytes(self.weights_bytes),
                "peak": human_bytes(self.peak_bytes),
                "confidence": self.confidence, "basis": self.basis,
                "dtype": self.dtype,
                "available_bytes": int(self.available_bytes),
                "exceeds_available": bool(self.exceeds_available),
                "description": self.describe()}


@dataclass
class LoadedModel:
    """A model that is currently in memory."""

    model_id: str
    backend: str
    loaded_at: float = field(default_factory=time.monotonic)
    payload: Any = None

    def seconds_loaded(self) -> float:
        return max(0.0, time.monotonic() - self.loaded_at)


class ModelStore:
    """Where models live and what is in them (section 5)."""

    def __init__(self, roots: Optional[Iterable[Any]] = None, *,
                 max_depth: int = 3) -> None:
        self.roots = [Path(item) for item in (roots or self.default_roots())]
        self.max_depth = max(1, int(max_depth))

    @staticmethod
    def default_roots() -> list[Path]:
        """The folders that are looked in when the user has configured none.

        This is the application's own ``models`` folder - the one shown in the
        settings and in the documentation - so a user has somewhere obvious to
        put weights.
        """
        roots: list[Path] = []
        try:
            from ..core.paths import AppPaths, default_source_root, resolve_data_root

            source_root = default_source_root()
            data_root, _reason = resolve_data_root(source_root)
            roots.append(AppPaths(data_root, source_root).models_dir)
        except Exception:  # noqa: BLE001 - a missing folder is normal
            pass
        return roots

    # -- discovery ---------------------------------------------------------

    def weight_files(self, folder: Any, *, recursive: bool = True) -> list[ModelFile]:
        """Every weight file under a folder, with its real size."""
        base = Path(folder)
        if not base.is_dir():
            return []
        found: list[ModelFile] = []
        pattern = "**/*" if recursive else "*"
        try:
            candidates = sorted(base.glob(pattern))
        except OSError:
            return []
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in WEIGHT_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found.append(ModelFile(path=path, size_bytes=int(size),
                                   suffix=path.suffix.lower()))
        return found

    def weights_in(self, folder: Any) -> list[ModelFile]:
        """Weights at the top of a folder, plus one level down.

        A Diffusers folder keeps its weights in sub-folders (``unet``, ``vae``),
        so the search has to go a level down; it does not go deeper, because a
        folder full of other people's models is not one model.
        """
        base = Path(folder)
        direct = self.weight_files(base, recursive=False)
        if direct:
            return direct
        found: list[ModelFile] = []
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir():
                    found.extend(self.weight_files(child, recursive=True))
        except OSError:
            return []
        return found

    def folders(self, *, depth: Optional[int] = None) -> list[Path]:
        """Candidate model folders under every root, shallowest first."""
        limit = self.max_depth if depth is None else max(1, int(depth))
        found: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for current in sorted(root.rglob("*")):
                if not current.is_dir():
                    continue
                relative = current.relative_to(root)
                if len(relative.parts) > limit:
                    continue
                if current.name.startswith("."):
                    continue
                if self.weights_in(current):
                    found.append(current)
        return found

    def discover(self, *, kind: str = "") -> list[dict]:
        """Every model found, described without loading anything."""
        described: list[dict] = []
        for folder in self.folders():
            weights = self.weights_in(folder)
            total = sum(item.size_bytes for item in weights)
            described.append({
                "id": folder.name, "name": folder.name, "path": str(folder),
                "files": [item.to_dict() for item in weights],
                "file_count": len(weights), "size_bytes": total,
                "size": human_bytes(total),
                "kind": kind or self._guess_kind(folder),
                "requirement": str(detect_requirement(folder.name, weights)),
                "notes": self._notes(folder),
            })
        return described

    def discover_video_models(self) -> list:
        """Describe the video-capable models, in the shape the video layer wants."""
        from .video import VideoModel

        models: list[VideoModel] = []
        for entry in self.discover():
            if str(entry.get("kind", "")) != "video":
                continue
            models.append(VideoModel(
                id=str(entry["id"]), name=str(entry["name"]),
                backend="", kind="diffusers", path=str(entry["path"]),
                size_bytes=int(entry.get("size_bytes", 0)),
                requirement=entry.get("requirement", DeviceRequirement.UNKNOWN),
                status="available", notes=str(entry.get("notes", ""))))
        return models

    @staticmethod
    def _guess_kind(folder: Path) -> str:
        """Video, image or unknown, from names that really exist on disk."""
        text = str(folder).lower()
        if any(hint in text for hint in VIDEO_HINTS):
            return "video"
        for name in ("model_index.json", "config.json"):
            descriptor = folder / name
            if not descriptor.is_file():
                continue
            try:
                payload = json.loads(descriptor.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            class_name = str(payload.get("_class_name", "")).lower()
            if "video" in class_name:
                return "video"
            if class_name:
                return "image"
        return "unknown"

    @staticmethod
    def _notes(folder: Path) -> str:
        for name in DESCRIPTOR_NAMES:
            descriptor = folder / name
            if descriptor.is_file():
                return f"Described by {name}."
        return "No descriptor file; the kind is unknown."

    # -- requirements and memory ------------------------------------------

    def estimate_memory(self, model: Any, *, dtype: str = "float32",
                        available_bytes: int = 0) -> MemoryEstimate:
        """What this model will probably cost, from its real file sizes."""
        weights = 0
        declared_path = str(_field(model, "path", "") or "")
        if declared_path:
            path = Path(declared_path)
            if path.is_file():
                try:
                    weights = path.stat().st_size
                except OSError:
                    weights = 0
            elif path.is_dir():
                weights = sum(item.size_bytes for item in self.weights_in(path))
        if not weights:
            # A model described without a path is measured by the sizes it and
            # its own file list report - never by looking around the folder the
            # application happens to be running from.
            weights = int(_field(model, "size_bytes", 0) or 0)
        if not weights:
            for item in list(_field(model, "files", []) or []):
                weights += int(getattr(item, "size_bytes", 0) or 0
                               or (item.get("size_bytes", 0)
                                   if isinstance(item, dict) else 0))
        if not weights:
            return MemoryEstimate(
                confidence="unknown",
                basis=("No weight files were found, so the memory this model "
                       "needs is unknown."),
                dtype=dtype, available_bytes=int(available_bytes))
        factor = BYTES_PER_PARAM.get(str(dtype).lower(), 4.0) / 4.0
        scaled = int(weights * factor)
        # Weights, activations and the working frame buffer.  A video model
        # holds more than an image model; the doubling is stated, not hidden.
        peak = int(scaled * 2.6) + 256 * 1024 * 1024
        return MemoryEstimate(
            weights_bytes=scaled, peak_bytes=peak, confidence="known",
            basis=(f"{human_bytes(weights)} of weights read from disk, scaled "
                   f"for {dtype}, plus working memory."),
            dtype=dtype, available_bytes=int(available_bytes))

    def missing_for(self, model: Any) -> list[str]:
        """What is missing for a model to be usable, in plain words."""
        missing: list[str] = []
        declared = str(_field(model, "path", "") or "")
        path = Path(declared)
        if not declared:
            missing.append("No path is set for this model.")
        elif not path.exists():
            missing.append(f"There is nothing at {path}.")
        elif path.is_dir() and not self.weights_in(path):
            missing.append(f"{path} holds no model weight files.")
        return missing


class ModelCache:
    """One model at a time, loaded when a job needs it (sections 11, 92).

    Reusing a loaded model is a real saving on a big video model, and it is
    only ever done for the same backend *and* the same model id.  A different
    model unloads the current one first, so two large models are never resident
    together on a machine that may not have room.
    """

    def __init__(self, *, keep_loaded: bool = True) -> None:
        self.keep_loaded = bool(keep_loaded)
        self.current: Optional[LoadedModel] = None
        self.events: list[dict] = []

    def ensure(self, backend: Any, model: Any, loader: Any = None) -> LoadedModel:
        """The model this backend needs, loading or reusing it as appropriate."""
        model_id = str(getattr(model, "id", "") or "")
        backend_id = str(getattr(backend, "id", "") or "")
        if self.current is not None and self.current.model_id == model_id \
                and self.current.backend == backend_id:
            self.events.append({"action": "reuse", "model": model_id,
                                "backend": backend_id})
            return self.current
        if self.current is not None:
            self.unload(reason=f"loading {model_id or 'another model'}")
        started = time.monotonic()
        loader = loader or (lambda: backend.load(model))
        payload = loader()
        loaded = LoadedModel(model_id=model_id, backend=backend_id,
                             payload=payload if payload is not None else model)
        self.current = loaded
        self.events.append({"action": "load", "model": model_id,
                            "backend": backend_id,
                            "seconds": round(time.monotonic() - started, 3)})
        log_event("AI_MODEL_LOADED", f"Loaded {model_id or backend_id}",
                  backend=backend_id,
                  seconds=round(time.monotonic() - started, 3))
        return loaded

    def unload(self, *, reason: str = "") -> None:
        current = self.current
        self.current = None
        if current is None:
            return
        self.events.append({"action": "unload", "model": current.model_id,
                            "backend": current.backend, "reason": reason,
                            "seconds_loaded": round(current.seconds_loaded(), 3)})
        log_event("AI_MODEL_UNLOADED",
                  f"Unloaded {current.model_id or current.backend}",
                  reason=reason or "requested")

    def release_for(self, backend_id: str) -> None:
        """Unload when a backend is disabled, so nothing stays resident."""
        if self.current is not None and self.current.backend == backend_id:
            self.unload(reason=f"{backend_id} was disabled")

    def describe(self) -> dict:
        if self.current is None:
            return {"loaded": "", "backend": "", "seconds_loaded": 0.0}
        return {"loaded": self.current.model_id,
                "backend": self.current.backend,
                "seconds_loaded": round(self.current.seconds_loaded(), 3)}


def detect_requirement(name: str, weights: Iterable[ModelFile] = ()) -> DeviceRequirement:
    """Whether a model needs a GPU, from its real name and real files.

    The only evidence available without loading the model is its name and its
    file sizes.  Large video models are marked GPU_RECOMMENDED rather than
    GPU_REQUIRED, because "recommended" is what the evidence supports: the
    application has not run the model, so it cannot promise it will not work.
    """
    text = f"{name} " + " ".join(str(item.path) for item in weights)
    lowered = text.lower()
    if any(hint in lowered for hint in VIDEO_HINTS):
        return DeviceRequirement.GPU_RECOMMENDED
    return DeviceRequirement.UNKNOWN


def format_model_line(model: Any) -> str:
    """A one-line summary used by the CLI and the manager list."""
    parts = [str(getattr(model, "name", "") or getattr(model, "id", "") or "model")]
    size = human_bytes(int(getattr(model, "size_bytes", 0) or 0))
    if size:
        parts.append(size)
    requirement = str(getattr(model, "requirement", "") or "")
    if requirement:
        parts.append(requirement.replace("_", " ").lower())
    return " - ".join(parts)
