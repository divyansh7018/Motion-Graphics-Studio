"""Backend registry and model manager (Stage F, sections 4, 5, 41, 82).

Opening Image Studio detects what exists and lists it.  It does not load
anything.  Weights are loaded when a generation is requested, one model at a
time, and released when another takes over or the studio closes.

The registry is the only place that knows which adapters exist, so the rest of
the application (and the GUI) just asks it for capabilities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..core.logging_setup import log_event
from ..core.settings import Settings
from .backends import BACKEND_CLASSES
from .capabilities import ImageCapabilities
from .device import DeviceInfo, detect_device
from .provider import ImageModel, ImageProvider, ProviderStatus

__all__ = ["BackendRegistry", "BackendEntry", "ModelManager", "DiscoveryReport"]


class BackendEntry:
    """One adapter plus the reason it is or is not usable."""

    def __init__(self, provider: ImageProvider, *, import_error: str = "") -> None:
        self.provider = provider
        self.import_error = str(import_error or "")
        self._status: Optional[ProviderStatus] = None

    @property
    def id(self) -> str:
        return self.provider.id

    @property
    def label(self) -> str:
        return self.provider.label

    def status(self) -> ProviderStatus:
        """Cached per detection pass; call :meth:`refresh` to look again."""
        if self._status is None:
            if self.import_error:
                self._status = ProviderStatus(
                    available=False, state="not_installed",
                    reason=(f"This backend could not be loaded: "
                            f"{self.import_error}"),
                    instructions=["Reinstall the application, or use another "
                                  "backend."])
            else:
                try:
                    self._status = self.provider.status()
                except Exception as exc:  # noqa: BLE001 - a broken backend is reported
                    self._status = ProviderStatus(
                        available=False, state="not_installed",
                        reason=f"This backend could not be checked: {exc}",
                        instructions=["Use another backend, or reinstall the "
                                      "package it needs."])
        return self._status

    def refresh(self) -> ProviderStatus:
        self._status = None
        return self.status()

    def capabilities(self) -> ImageCapabilities:
        try:
            return self.provider.capabilities()
        except Exception:  # noqa: BLE001 - never let one adapter break the studio
            return ImageCapabilities(notes="This backend could not report its "
                                           "capabilities.")

    def models(self) -> list[ImageModel]:
        try:
            return self.provider.models()
        except Exception:  # noqa: BLE001
            return []

    @property
    def available(self) -> bool:
        return self.status().available


class DiscoveryReport:
    """What a detection pass found, in words a user can read."""

    def __init__(self) -> None:
        self.backends: list[BackendEntry] = []
        self.models: list[ImageModel] = []
        self.device: Optional[DeviceInfo] = None
        self.any_generator = False
        self.notes: list[str] = []

    @property
    def generator_note(self) -> str:
        """The sentence the empty state shows (section 82)."""
        if self.any_generator:
            names = [model.name for model in self.models
                     if model.capabilities.text_to_image]
            if names:
                return (f"{len(names)} local model(s) can draw a prompt: "
                        + ", ".join(names[:4]) + ".")
        return ("No local image-generation model is installed. Importing, "
                "editing, upscaling and organising images all still work; "
                "install a local backend to generate from a prompt.")

    def describe(self) -> str:
        lines = [f"{len(self.backends)} backend(s) checked",
                 f"{len(self.models)} model(s) found",
                 self.generator_note]
        for entry in self.backends:
            state = entry.status()
            lines.append(f"  {entry.label}: {state.state} - {state.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "backends": [{"id": entry.id, "label": entry.label,
                          "state": entry.status().state,
                          "reason": entry.status().reason,
                          "available": entry.available}
                         for entry in self.backends],
            "models": [model.to_dict() for model in self.models],
            "any_generator": bool(self.any_generator),
            "device": self.device.to_dict() if self.device else {},
            "generator_note": self.generator_note,
        }


class ModelManager:
    """Keeps at most one model loaded, and says which one that is."""

    def __init__(self) -> None:
        self._loaded: Optional[tuple[str, str]] = None  # (backend_id, model_id)

    @property
    def loaded(self) -> Optional[tuple[str, str]]:
        return self._loaded

    def mark_loaded(self, backend_id: str, model_id: str) -> None:
        self._loaded = (str(backend_id), str(model_id))

    def mark_unloaded(self) -> None:
        self._loaded = None

    def is_loaded(self, backend_id: str, model_id: str) -> bool:
        return self._loaded == (str(backend_id), str(model_id))

    def describe(self) -> str:
        if self._loaded is None:
            return "No model is loaded."
        return f"Loaded: {self._loaded[1]} ({self._loaded[0]})."


class BackendRegistry:
    """Builds and caches the adapters for this machine."""

    def __init__(self, settings: Optional[Settings] = None, *,
                 data_root: Any = None, device: Optional[DeviceInfo] = None) -> None:
        self.settings = settings
        self.data_root = Path(data_root) if data_root else None
        self.device = device
        self.entries: dict[str, BackendEntry] = {}
        self.model_manager = ModelManager()
        self._report: Optional[DiscoveryReport] = None

    # -- construction ------------------------------------------------------

    def build(self) -> dict[str, BackendEntry]:
        """Construct every adapter.  Cheap: nothing is loaded or probed yet."""
        if self.entries:
            return self.entries
        classes = dict(BACKEND_CLASSES)
        model_paths = self._model_paths()
        values = self._image_settings()

        factories = {
            "standard": lambda: classes["standard"](),
            "command": lambda: classes["command"](values.get("command", "")),
            "http": lambda: classes["http"](values.get("endpoint", "")),
            "comfyui": lambda: classes["comfyui"](
                values.get("comfyui_endpoint", ""),
                values.get("comfyui_workflow", "")),
            "diffusers": lambda: classes["diffusers"](model_paths),
            "onnx": lambda: classes["onnx"](model_paths),
        }
        for backend_id in ("standard", "command", "http", "comfyui",
                           "diffusers", "onnx"):
            factory = factories.get(backend_id)
            if factory is None:
                continue
            try:
                provider = factory()
            except Exception as exc:  # noqa: BLE001 - one bad adapter is not fatal
                # Recorded, never skipped silently: the interface shows which
                # backend failed to load and why.
                self.entries[backend_id] = BackendEntry(
                    _UnavailableProvider(backend_id, str(exc)),
                    import_error=str(exc))
                log_event("IMAGE_BACKEND_BUILD_FAILED",
                          f"'{backend_id}' could not be constructed",
                          backend=backend_id, error=str(exc))
                continue
            self.entries[backend_id] = BackendEntry(provider)
        return self.entries

    def _image_settings(self) -> dict:
        """The image section of the settings, as a plain dict.

        ``Settings.image`` is a dataclass (``ImageSettings``), not a mapping, so
        it is converted here; a stand-in object that *is* a mapping also works,
        which keeps the registry testable without a whole settings file.
        """
        if self.settings is None:
            return {}
        try:
            section = self.settings.get("image", {}) \
                if hasattr(self.settings, "get") else getattr(self.settings, "image", None)
        except Exception:  # noqa: BLE001 - settings are never worth crashing for
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return section
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(section):
                return dict(asdict(section))
        except Exception:  # noqa: BLE001
            pass
        return {key: getattr(section, key) for key in
                ("command", "endpoint", "comfyui_endpoint", "comfyui_workflow")
                if hasattr(section, key)}

    def _model_paths(self) -> list[Path]:
        """Model files found in the folders the scanner knows about.

        Discovery only lists files; it never loads one.
        """
        from .backends.model_backends import ModelFolderScanner

        roots: list[Path] = []
        if self.data_root is not None:
            roots.append(Path(self.data_root))
        try:
            roots.append(Path.home())
        except Exception:  # noqa: BLE001 - a home-less account is not fatal
            pass
        try:
            return list(ModelFolderScanner(roots).files())
        except Exception as exc:  # noqa: BLE001
            log_event("IMAGE_MODEL_SCAN_FAILED", "Model folders could not be read",
                      error=str(exc))
            return []

    # -- discovery ---------------------------------------------------------

    def detect(self, *, refresh: bool = True) -> DiscoveryReport:
        """One detection pass: statuses and models, no loading."""
        report = DiscoveryReport()
        report.device = self.device or detect_device()
        self.build()
        for backend_id in ("standard", "diffusers", "comfyui", "onnx",
                           "command", "http"):
            entry = self.entries.get(backend_id)
            if entry is None:
                continue
            if refresh:
                entry.refresh()
            report.backends.append(entry)
            for model in entry.models():
                report.models.append(model)
        report.any_generator = any(
            model.capabilities.text_to_image for model in report.models)
        self._report = report
        log_event("IMAGE_BACKENDS_DETECTED",
                  f"{len(report.backends)} backends, {len(report.models)} models",
                  generators=report.any_generator,
                  device=report.device.accelerator if report.device else "unknown")
        return report

    @property
    def report(self) -> Optional[DiscoveryReport]:
        return self._report

    # -- access ------------------------------------------------------------

    def get(self, backend_id: str) -> Optional[BackendEntry]:
        self.build()
        return self.entries.get(str(backend_id))

    def available_backends(self) -> list[BackendEntry]:
        self.build()
        return [entry for entry in self.entries.values() if entry.available]

    def find_model(self, backend_id: str, model_id: str) -> Optional[ImageModel]:
        entry = self.get(backend_id)
        if entry is None:
            return None
        for model in entry.models():
            if model.id == model_id or model.name == model_id:
                return model
        return None

    def capabilities_for(self, backend_id: str) -> ImageCapabilities:
        entry = self.get(backend_id)
        return entry.capabilities() if entry else ImageCapabilities(
            notes="Unknown backend.")

    def release_all(self) -> None:
        """Unload anything this registry loaded (studio closing)."""
        for entry in self.entries.values():
            try:
                entry.provider.unload()
            except Exception:  # noqa: BLE001
                continue
        self.model_manager.mark_unloaded()

    def describe(self) -> str:
        report = self._report or self.detect()
        return report.describe()


class _UnavailableProvider(ImageProvider):
    """Placeholder for an adapter that could not even be constructed."""

    id = "unavailable"
    label = "Unavailable backend"
    kind = "unavailable"

    def __init__(self, backend_id: str, error: str) -> None:
        self.id = str(backend_id)
        self.label = str(backend_id).title()
        self._error = str(error)

    def status(self) -> ProviderStatus:
        return ProviderStatus(available=False, state="not_installed",
                              reason=f"This backend could not start: {self._error}")

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities()
