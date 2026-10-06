"""Image Studio service (Stage F).

The single facade the CLI, the GUI and the tests talk to.  It owns the backend
registry, the library, the history and the device facts, and it is the only place
that turns a user action into a job.

Nothing here loads a model at import time, and nothing here runs on the Qt
thread: every generation goes through a :class:`JobSpec` (sections 34, 41).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from ..core.errors import AppError
from ..core.logging_setup import log_event
from ..core.settings import Settings
from .capabilities import ImageCapabilities
from .device import DeviceInfo, detect_device, recommend_size
from .editor import ImageEditSession
from .history import HistoryEntry, ImageHistory, PromptLibrary
from .library import ImageEntry, ImageLibrary, LibraryQuery, LibraryPage, ThumbnailCache
from .metadata import ImageMetadata, read_metadata, write_metadata
from .provider import (GenerationMode, GenerationRequest, GenerationResult,
                       ImageModel)
from .registry import BackendEntry, BackendRegistry, DiscoveryReport
from .saving import SaveReport, convert_image, unique_path
from .upscale import UpscaleResult, upscale
from .validation import (ImageCheck, ImageIssue, validate_image_file,
                         validate_request)
from .variants import Origin, VersionGraph, origin_for_mode

__all__ = ["ImageStudioError", "ImageService", "StudioStatus", "ImageCheck"]


class ImageStudioError(AppError):
    """An Image Studio problem the user can act on."""


class StudioStatus:
    """What the studio knows about this machine, for the UI's state bar."""

    def __init__(self, report: DiscoveryReport, device: DeviceInfo) -> None:
        self.report = report
        self.device = device

    @property
    def any_backend(self) -> bool:
        return any(entry.available for entry in self.report.backends)

    @property
    def any_generator(self) -> bool:
        return bool(self.report.any_generator)

    @property
    def generator_note(self) -> str:
        return self.report.generator_note

    @property
    def model_count(self) -> int:
        return len(self.report.models)

    def describe(self) -> str:
        return self.report.describe()

    def to_dict(self) -> dict:
        data = self.report.to_dict()
        data["cpu_only"] = self.device.cpu_only
        return data


class ImageService:
    """Everything Image Studio does, in one place."""

    def __init__(self, settings: Optional[Settings] = None, *,
                 data_root: Any = None,
                 library_root: Optional[Any] = None,
                 thumbnail_dir: Optional[Any] = None,
                 state_dir: Optional[Any] = None,
                 registry: Optional[BackendRegistry] = None) -> None:
        self.settings = settings
        self.data_root = Path(data_root) if data_root else None
        base = Path(data_root) if data_root else None
        self.library_root = Path(library_root) if library_root else \
            (base / "images" if base else None)
        self.library: Optional[ImageLibrary] = None
        if self.library_root is not None:
            self.library = ImageLibrary(self.library_root)
        thumbnails = thumbnail_dir or (base / "cache" / "image_thumbs"
                                       if base else None)
        self.thumbnails = ThumbnailCache(thumbnails) if thumbnails else None
        self.registry = registry or BackendRegistry(settings, data_root=data_root)
        self._device: Optional[DeviceInfo] = None
        self._status: Optional[StudioStatus] = None
        # History and prompt history are small pieces of application state, so
        # they live beside the settings rather than in the library folder.
        state = Path(state_dir) if state_dir else base
        self.history = ImageHistory(state / "image_history.json" if state else None)
        self.prompts = PromptLibrary(state / "image_prompts.json" if state else None)

    @classmethod
    def for_paths(cls, paths: Any, settings: Optional[Settings] = None, *,
                  registry: Optional[BackendRegistry] = None) -> "ImageService":
        """Build a service from :class:`AppPaths`.

        One construction point, so the GUI, the CLI and the scripts all put the
        library, the thumbnails and the history in the same places.
        """
        return cls(
            settings,
            data_root=paths.data_root,
            library_root=paths.images_dir,
            thumbnail_dir=paths.image_thumbnails_dir,
            state_dir=paths.config_dir,
            registry=registry,
        )

    # -- state -------------------------------------------------------------

    @property
    def device(self) -> DeviceInfo:
        if self._device is None:
            self._device = detect_device()
        return self._device

    def status(self, *, refresh: bool = True) -> StudioStatus:
        """Detect backends and models.  Never loads one."""
        report = self.registry.detect(refresh=refresh)
        self._status = StudioStatus(report, self.device)
        return self._status

    def capabilities_for(self, backend_id: str) -> ImageCapabilities:
        return self.registry.capabilities_for(backend_id)

    def backends(self) -> list[BackendEntry]:
        self.registry.build()
        return list(self.registry.entries.values())

    def models(self) -> list[ImageModel]:
        report = self.registry.report or self.registry.detect()
        return list(report.models)

    def recommend_size(self, width: int, height: int) -> tuple[int, int, str]:
        """A CPU-friendly size, offered to the user and never applied silently."""
        return recommend_size(self.device, width, height)

    # -- generation --------------------------------------------------------

    def validate(self, request: GenerationRequest, *,
                 backend_id: str = "") -> list[ImageIssue]:
        """Ask the backend that would run this request what it thinks.

        The adapter's own ``validate`` is used when there is one, so a backend
        that can explain itself better than the generic rules is heard - the
        built-in backend, for example, says "no model is installed" rather than
        the less useful "unsupported mode".
        """
        chosen = backend_id or request.backend
        entry = self.registry.get(chosen)
        if entry is not None:
            model = self.registry.find_model(chosen, request.model)
            try:
                return list(entry.provider.validate(request, model))
            except TypeError:
                # An adapter with a different signature falls back to the rules.
                pass
            except Exception as exc:  # noqa: BLE001 - validation never crashes
                log_event("IMAGE_VALIDATE_FAILED",
                          f"'{chosen}' could not validate the request",
                          backend=chosen, error=str(exc))
        caps = self.capabilities_for(chosen)
        model = self.registry.find_model(chosen, request.model)
        return validate_request(request, caps, model=model)

    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        """Run one generation and record it.  Called from a job, never the GUI."""
        backend_id = str(request.backend or "")
        entry = self.registry.get(backend_id)
        if entry is None:
            result = GenerationResult(
                ok=False, error=f"There is no backend called '{backend_id}'.",
                why="It is not one of the adapters this build ships.",
                what_to_do="Pick one of the backends listed in Image Studio.",
                code="UNKNOWN_BACKEND", backend=backend_id)
            self.history.add_failure(request, result)
            return result
        if not entry.available:
            state = entry.status()
            result = GenerationResult(
                ok=False, error=f"'{entry.label}' cannot be used: {state.reason}",
                why=state.reason,
                what_to_do=" ".join(state.instructions) or "Choose another backend.",
                code="BACKEND_UNAVAILABLE", backend=backend_id)
            # Never switch backends behind the user's back (section 36).
            self.history.add_failure(request, result)
            return result

        issues = [issue for issue in self.validate(request, backend_id=backend_id)
                  if issue.severity == "error"]
        if issues:
            result = GenerationResult(
                ok=False, error=issues[0].message,
                why="The request does not meet the backend's requirements.",
                what_to_do=issues[0].what_to_do, code=issues[0].code,
                backend=backend_id)
            self.history.add_failure(request, result)
            return result

        result = entry.provider.generate(request, progress=progress, cancel=cancel)
        if result.ok:
            self.prompts.record_use(request.prompt, negative=request.negative_prompt)
            self.history.add_result(result, request)
            self._register_outputs(request, result)
            self.registry.model_manager.mark_loaded(backend_id, request.model)
        elif result.cancelled:
            self.history.add_failure(request, result)
        else:
            self.history.add_failure(request, result)
        return result

    def _register_outputs(self, request: GenerationRequest,
                          result: GenerationResult) -> None:
        """Add generated files to the library so they are immediately usable."""
        if self.library is None:
            return
        for index, raw in enumerate(result.paths):
            path = Path(raw)
            # Write the record *before* indexing, so the library reads the real
            # prompt, seed and lineage rather than an empty file.
            metadata = ImageMetadata.from_generation(request, result, index=index,
                                                     path=path)
            metadata.asset_id = path.stem
            metadata.origin = origin_for_mode(request.mode)
            if not metadata.parent and request.parent_asset:
                metadata.parent = str(request.parent_asset)
            metadata.lineage = self._lineage_for(metadata)
            write_metadata(path, metadata)
            entry = self.library.add_entry(path)
            # Only fields that really exist are stored: the seed actually used,
            # the prompt actually sent, and where the user filed it.
            entry.seed = result.seeds[index] if index < len(result.seeds) else None
            entry.prompt = request.prompt
            entry.model = result.model
            entry.collection = request.collection
            entry.project = request.project
            entry.tags = list(request.tags or [])
        self.library.save()

    def _lineage_for(self, metadata: ImageMetadata) -> list:
        """The chain of origins from the original to this image.

        Built from the parent's own record rather than assumed, so a variation of
        a variation reads ``original -> variation -> variation`` and not
        ``original -> variation``.
        """
        chain = [metadata.origin or Origin.ORIGINAL]
        parent = str(metadata.parent or "")
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            try:
                parent_path = Path(parent)
            except (TypeError, ValueError):
                break
            if not parent_path.is_file():
                break
            record = read_metadata(parent_path)
            if record is None or not record.origin:
                break
            chain.append(record.origin)
            parent = str(record.parent or "")
        chain.reverse()
        return chain

    def graph_for(self, paths: Any) -> VersionGraph:
        """The version graph for a set of images."""
        graph = VersionGraph(paths)
        graph.relink()
        return graph

    def retry(self, previous: HistoryEntry, *,
              progress: Optional[Callable[[str, float], None]] = None,
              cancel: Any = None) -> GenerationResult:
        """Regenerate from a history row, keeping its seed (section 23)."""
        if not previous.request:
            raise ImageStudioError("That history entry has no saved settings.")
        request = GenerationRequest.from_dict(previous.request)
        return self.generate(request, progress=progress, cancel=cancel)

    # -- editing -----------------------------------------------------------

    def open_editor(self, path: Any) -> ImageEditSession:
        target = Path(path)
        check = validate_image_file(target, required=True)
        if not check.ok:
            raise ImageStudioError(
                check.error,
                actions=[check.what_to_do] if check.what_to_do else None)
        return ImageEditSession(target, metadata=read_metadata(target))

    def save_edit(self, session: ImageEditSession, *, target: Any,
                  requested_format: str = "png",
                  overwrite: bool = False) -> SaveReport:
        """Save an edit as a new file unless the caller confirmed an overwrite."""
        report = session.save_as(target, requested_format=requested_format,
                                 overwrite=overwrite)
        if report.ok and self.library is not None:
            # The edited file is indexed too, so it shows up in the library next
            # to the image it came from.  The original is left alone.
            self.library.add_entry(Path(report.path))
            self.library.save()
        return report

    def upscale_image(self, source: Any, *, scale: float = 2.0,
                      method: str = "standard",
                      output_dir: Optional[Any] = None,
                      allow_fallback: bool = False) -> UpscaleResult:
        """Upscale into a **new** file, never over the original."""
        source_path = Path(source)
        folder = Path(output_dir) if output_dir else source_path.parent
        target = unique_path(folder, f"{source_path.stem}_upscaled",
                             source_path.suffix or ".png")
        result = upscale(source_path, target, scale=scale, method=method,
                         allow_fallback=allow_fallback)
        if result.ok and result.path is not None and self.library is not None:
            entry = self.library.add_entry(Path(result.path))
            entry.prompt = ""
        if result.ok and self.library is not None:
            self.library.save()
        return result

    def variant_of(self, source: Any, *, collection: str = "") -> GenerationRequest:
        """A request that makes a variation of an image, leaving it untouched."""
        source_path = Path(source)
        from PIL import Image

        with Image.open(source_path) as handle:
            width, height = handle.size
        return GenerationRequest(
            mode=GenerationMode.IMG2IMG, prompt="", source_image=str(source_path),
            width=width, height=height, backend="standard", model="standard",
            name_stem=f"{source_path.stem}_variation", strength=0.35,
            output_format="png", collection=collection,
            parent_asset=str(source_path))

    # -- library -----------------------------------------------------------

    def query(self, query: Optional[LibraryQuery] = None) -> LibraryPage:
        if self.library is None:
            return LibraryPage(entries=[], total=0, page=1, pages=0)
        return self.library.query(query or LibraryQuery())

    def thumbnail(self, path: Any) -> Optional[Path]:
        if self.thumbnails is None:
            return None
        return self.thumbnails.get(Path(path))

    def remove_from_library(self, path: Any, *, delete_file: bool = False) -> bool:
        if self.library is None:
            return False
        removed = self.library.remove(Path(path), delete_file=delete_file)
        if removed:
            self.library.save()
        return removed

    def duplicates(self) -> list[list[ImageEntry]]:
        """Groups of images that look identical.  Never auto-deleted."""
        if self.library is None:
            return []
        return self.library.duplicates()

    # -- files -------------------------------------------------------------

    def import_image(self, source: Any, *, destination: Any,
                     requested_format: str = "",
                     overwrite: bool = False) -> SaveReport:
        """Copy an image into a project or the library, validating it first."""
        source_path = Path(source)
        check = validate_image_file(source_path, required=True,
                                    label="The file you dropped")
        if not check.ok:
            return SaveReport(ok=False, error=check.error,
                              what_to_do=check.what_to_do)
        target = Path(destination)
        if not requested_format:
            requested_format = source_path.suffix.lower().lstrip(".") or "png"
        report = convert_image(source_path, target,
                               requested_format=requested_format,
                               overwrite=overwrite)
        if report.ok and report.path is not None and self.library is not None:
            try:
                inside = report.path.resolve().is_relative_to(
                    self.library.root.resolve())
            except (OSError, ValueError):
                inside = False
            if inside:
                self.library.add_entry(Path(report.path))
                self.library.save()
        return report

    def verify(self, path: Any) -> "ImageCheck":
        """Post-save verification with the real values (section 46)."""
        return validate_image_file(Path(path), required=True)

    def metadata_for(self, path: Any) -> Optional[ImageMetadata]:
        return read_metadata(Path(path))

    def write_metadata(self, path: Any, metadata: ImageMetadata) -> None:
        write_metadata(Path(path), metadata)

    def unique_output(self, folder: Any, stem: str, suffix: str = ".png") -> Path:
        """A name that does not overwrite anything (section 44)."""
        return unique_path(Path(folder), stem, suffix)

    # -- lifetime ----------------------------------------------------------

    def close(self) -> None:
        """Release anything loaded.  Safe to call more than once."""
        self.registry.release_all()
        if self.library is not None:
            self.library.save()
        self.history.save()
        self.prompts.save()
