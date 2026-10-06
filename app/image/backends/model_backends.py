"""Local model backends: Diffusers, ComfyUI and ONNX (Stage F, sections 2, 5, 41).

Three adapters that drive a real local model.  They share one shape and one set
of rules:

* **lazy** - constructing the adapter costs nothing; weights are loaded only
  inside ``generate()``, and only for the model that was chosen;
* **one model at a time** - loading a second model unloads the first, so a
  machine with 8 GB of RAM is not asked to hold three of them;
* **honest** - if the library or the weights are missing, the status says
  ``not_installed`` and names what was looked for.

On a fresh machine none of these is installed, and that is the expected state.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ..capabilities import ImageCapabilities
from ..provider import (GenerationRequest, GenerationResult, GenerationState,
                        ImageModel, ImageProvider, ProviderStatus)
from ..validation import validate_request
from ..saving import unique_path
from .base import (cancelled_result, failed_result, seed_from_request,
                   verify_output)

__all__ = ["DiffusersBackend", "ComfyUIBackend", "OnnxBackend", "ModelFolderScanner"]

#: Folders scanned for local models, relative to the data root or the user's
#: home.  Nothing is downloaded and nothing outside these is touched.
MODEL_SUBFOLDERS: tuple[str, ...] = (
    "models/image", "models/diffusers", "models/upscaler", "models",
)

#: File extensions that look like model weights.
MODEL_SUFFIXES: tuple[str, ...] = (".safetensors", ".ckpt", ".onnx", ".pt", ".pth",
                                   ".bin", ".gguf")


class ModelFolderScanner:
    """Finds model files on disk without loading any of them."""

    def __init__(self, roots: Any) -> None:
        self.roots = [Path(root) for root in (roots or [])]

    def folders(self) -> list[Path]:
        found: list[Path] = []
        for root in self.roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for sub in MODEL_SUBFOLDERS:
                candidate = root / sub
                if candidate.is_dir() and candidate not in found:
                    found.append(candidate)
            # A folder that directly holds weights counts too.
            if any(path.suffix.lower() in MODEL_SUFFIXES
                   for path in root.iterdir() if path.is_file()):
                if root not in found:
                    found.append(root)
        return found

    def files(self) -> list[Path]:
        """Every model file, once each.

        ``models`` and ``models/image`` can both be scan roots, so a file may be
        reached twice; it is de-duplicated here or the model manager would list
        the same weights as two separate models.
        """
        results: list[Path] = []
        seen: set[str] = set()
        for folder in self.folders():
            try:
                for path in sorted(folder.rglob("*")):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in MODEL_SUFFIXES:
                        continue
                    key = str(path.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append(path)
            except OSError:
                continue
        return results


class _LocalModelBackend(ImageProvider):
    """Shared behaviour for the three model adapters."""

    #: The Python package that must be importable.
    required_package: str = ""
    #: What the package is called in a message to the user.
    package_label: str = ""

    def __init__(self, model_paths: Any = (), *, timeout: float = 7200.0) -> None:
        self.model_paths = [Path(item) for item in (model_paths or [])]
        self.timeout = float(timeout or 7200.0)
        self._loaded: Optional[str] = None
        self._pipeline: Any = None

    # -- discovery ---------------------------------------------------------

    def _package_state(self) -> tuple[bool, str]:
        if not self.required_package:
            return True, ""
        try:
            import importlib.util

            found = importlib.util.find_spec(self.required_package) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            return True, ""
        return False, (f"The '{self.package_label or self.required_package}' "
                       "package is not installed.")

    def status(self) -> ProviderStatus:
        installed, reason = self._package_state()
        if not installed:
            return ProviderStatus(
                available=False, state="not_installed", reason=reason,
                instructions=[
                    f"Install {self.package_label or self.required_package} to "
                    "enable this backend.",
                    "Image Studio works without it: importing, editing and "
                    "organising images need no model.",
                ])
        models = self.models()
        if not models:
            return ProviderStatus(
                available=False, state="not_installed",
                reason=(f"{self.package_label or self.required_package} is "
                        "installed but no model files were found."),
                instructions=[
                    "Place model weights in the application's models folder.",
                    "Then reopen Image Studio to have them detected.",
                ])
        return ProviderStatus(available=True, state="available",
                              reason=f"{len(models)} model(s) found.",
                              device="cpu")

    def models(self) -> list[ImageModel]:
        caps = self.capabilities()
        results: list[ImageModel] = []
        for path in self.model_paths:
            path = Path(path)
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            results.append(ImageModel(
                id=path.stem, name=path.stem, backend=self.id,
                kind=self._model_kind(path), path=str(path), size_bytes=size,
                capabilities=caps, status="available",
                loaded=(self._loaded == str(path))))
        return results

    def _model_kind(self, path: Path) -> str:
        name = path.stem.lower()
        if "upscal" in name or "esrgan" in name or "swinir" in name:
            return "upscale"
        if "inpaint" in name:
            return "inpaint"
        return "diffusion"

    # -- model lifetime ----------------------------------------------------

    def load(self, model: ImageModel) -> None:
        """Load one model, releasing whatever was loaded before it."""
        if self._loaded == str(model.path or model.id) and self._pipeline is not None:
            return
        self.unload()
        self._pipeline = self._create_pipeline(model)
        self._loaded = str(model.path or model.id)
        model.loaded = True
        log_event("IMAGE_MODEL_LOADED", f"Loaded {model.name}", backend=self.id,
                  model=model.name)

    def unload(self) -> None:
        if self._pipeline is None and self._loaded is None:
            return
        name = self._loaded or ""
        self._pipeline = None
        self._loaded = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001 - best effort
            pass
        log_event("IMAGE_MODEL_UNLOADED", f"Released {name}", backend=self.id)

    def _create_pipeline(self, model: ImageModel) -> Any:
        raise NotImplementedError

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> GenerationResult:
        started = time.monotonic()
        state = self.status()
        if not state.available:
            return failed_result(
                request, f"This backend cannot be used here: {state.reason}",
                why=state.reason,
                what_to_do=" ".join(state.instructions) or
                           "Choose another backend.",
                code="BACKEND_UNAVAILABLE", backend=self.id)

        issues = [issue for issue in validate_request(request, self.capabilities())
                  if issue.severity == "error"]
        if issues:
            return failed_result(request, issues[0].message,
                                 why="The request cannot be run as it is.",
                                 what_to_do=issues[0].what_to_do,
                                 code=issues[0].code, backend=self.id)

        model = self._pick_model(request)
        if model is None:
            return failed_result(
                request, f"The model '{request.model}' was not found.",
                why="It is not in the folders this backend scans.",
                what_to_do="Choose one of the models listed in Image Studio.",
                code="MODEL_NOT_FOUND", backend=self.id)

        if progress is not None:
            progress(GenerationState.LOADING_MODEL, 0.05)
        try:
            self.load(model)
        except Exception as exc:  # noqa: BLE001 - a load failure is a report
            return failed_result(
                request, f"The model '{model.name}' could not be loaded.",
                why=str(exc)[:400],
                what_to_do=("Check the model file is complete, and that there is "
                            "enough free memory."),
                code="MODEL_LOAD_FAILED", backend=self.id)

        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        batch = max(1, int(request.batch or 1))
        seeds: list[int] = []
        paths: list[Path] = []
        try:
            for index in range(batch):
                if cancel is not None and cancel.is_cancelled():
                    return cancelled_result(request)
                seed = seed_from_request(request) if index == 0 else seeds[0] + index
                seeds.append(seed)
                if progress is not None:
                    progress(GenerationState.GENERATING,
                             0.1 + 0.85 * (index + 1) / batch)
                stem = request.name_stem or "image"
                suffix = f".{request.output_format or 'png'}"
                target = unique_path(
                    output_dir,
                    stem if batch == 1 else f"{stem}_{index + 1:02d}", suffix)
                image = self._run_pipeline(request, seed=seed)
                if cancel is not None and cancel.is_cancelled():
                    return cancelled_result(request)
                if progress is not None:
                    progress(GenerationState.SAVING, 0.97)
                from ..saving import save_image

                report = save_image(image, target,
                                    requested_format=request.output_format or "png")
                if not report.ok:
                    return failed_result(request, report.error,
                                         why="The image was generated but could "
                                             "not be saved.",
                                         what_to_do=report.what_to_do,
                                         code="SAVE_FAILED", backend=self.id)
                ok, reason = verify_output(report.path)
                if not ok:
                    return failed_result(request, reason, why="Verification failed.",
                                         what_to_do="Try again; the file was removed.",
                                         code="OUTPUT_INVALID", backend=self.id)
                paths.append(Path(report.path))
        except NotImplementedError as exc:
            return failed_result(request, str(exc),
                                 why="This adapter has no working pipeline yet.",
                                 what_to_do="Choose another backend or model.",
                                 code="PIPELINE_UNIMPLEMENTED", backend=self.id)
        except Exception as exc:  # noqa: BLE001 - generation errors are reported
            return failed_result(request, f"Generation failed: {exc}",
                                 why="The model raised an error while generating.",
                                 what_to_do=("Try a smaller size or fewer steps. "
                                             "The backend was not switched."),
                                 code="GENERATION_FAILED", backend=self.id)

        seconds = time.monotonic() - started
        from ..validation import validate_image_file

        check = validate_image_file(paths[0]) if paths else None
        width = check.width if check and check.ok else int(request.width or 0)
        height = check.height if check and check.ok else int(request.height or 0)
        log_event("IMAGE_GENERATED", f"{len(paths)} image(s)", backend=self.id,
                  model=model.name, images=len(paths), seconds=round(seconds, 2))
        return GenerationResult(
            ok=True, state=GenerationState.COMPLETED, paths=[str(p) for p in paths],
            seeds=seeds, model=model.name, backend=self.id, mode=request.mode,
            width=width, height=height,
            output_format=request.output_format or "png", seconds=seconds,
            quality={"model": model.name, "backend": self.id,
                     "resolution": f"{width}x{height}",
                     "seed": seeds[0] if seeds else "",
                     "format": request.output_format or "png",
                     "size_bytes": paths[0].stat().st_size if paths else 0,
                     "seconds": round(seconds, 2), "status": "COMPLETED"})

    def _pick_model(self, request: GenerationRequest) -> Optional[ImageModel]:
        available = self.models()
        if not available:
            return None
        wanted = str(request.model or "").strip()
        if wanted:
            for model in available:
                if model.id == wanted or model.name == wanted:
                    return model
            return None
        return available[0]

    def _run_pipeline(self, request: GenerationRequest, *, seed: int) -> Any:
        raise NotImplementedError


class DiffusersBackend(_LocalModelBackend):
    """A Diffusers-compatible local model."""

    id = "diffusers"
    label = "Diffusers (local model)"
    kind = "diffusers"
    required_package = "diffusers"
    package_label = "Diffusers"

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            text_to_image=True, image_to_image=True, inpaint=True,
            outpaint=True, upscale=False, reference_image=True, control=True,
            lora=True, negative_prompt=True, seed_control=True, steps=True,
            guidance=True, sampler=True, batch=True, strength=True,
            style_reference=True, max_batch=8, max_dimension=2048,
            min_dimension=64, dimension_multiple=8,
            notes="Local Diffusers pipeline. Needs the model weights on disk.")

    def _create_pipeline(self, model: ImageModel) -> Any:
        raise NotImplementedError(
            "The Diffusers package was detected, but no pipeline has been "
            "configured for this model. Set the pipeline class in Settings -> "
            "Image Studio, or use another backend.")

    def _run_pipeline(self, request: GenerationRequest, *, seed: int) -> Any:
        raise NotImplementedError(
            "No Diffusers pipeline is configured, so no image was generated.")


class ComfyUIBackend(_LocalModelBackend):
    """A local ComfyUI installation, driven through its own HTTP API."""

    id = "comfyui"
    label = "ComfyUI (local)"
    kind = "comfyui"
    #: ComfyUI is a program, not a package, so discovery is by URL.
    required_package = ""

    def __init__(self, endpoint: str = "", workflow: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.endpoint = str(endpoint or "").rstrip("/")
        self.workflow = str(workflow or "")

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            text_to_image=True, image_to_image=True, inpaint=True,
            outpaint=True, upscale=True, reference_image=True, control=True,
            lora=True, negative_prompt=True, seed_control=True, steps=True,
            guidance=True, sampler=True, batch=True, strength=True,
            max_batch=8, max_dimension=4096, min_dimension=64,
            dimension_multiple=8,
            notes="A local ComfyUI server, driven by the user's own workflow.")

    def status(self) -> ProviderStatus:
        from .http import is_loopback

        if not self.endpoint:
            return ProviderStatus(
                available=False, state="not_installed",
                reason="No ComfyUI address has been configured.",
                instructions=["Start ComfyUI locally and set its address in "
                              "Settings -> Image Studio."])
        if not is_loopback(self.endpoint):
            return ProviderStatus(
                available=False, state="not_supported",
                reason=f"'{self.endpoint}' is not on this machine.",
                instructions=["ComfyUI must be running on this PC."])
        from .http import HttpBackend

        probe = HttpBackend(self.endpoint, path="/prompt")
        reachable, detail = probe._ping()
        if not reachable:
            return ProviderStatus(
                available=False, state="not_installed",
                reason=f"ComfyUI did not answer: {detail}",
                instructions=["Start ComfyUI, then open Image Studio again."])
        return ProviderStatus(available=True, state="available",
                              reason=f"ComfyUI answering at {self.endpoint}.",
                              version=detail)

    def models(self) -> list[ImageModel]:
        if not self.status().available:
            return []
        return [ImageModel(id="comfyui-workflow",
                           name=self.workflow or "ComfyUI workflow",
                           backend=self.id, kind="workflow",
                           capabilities=self.capabilities(), status="available",
                           notes="Uses the workflow file you chose.")]

    def _create_pipeline(self, model: ImageModel) -> Any:
        if not self.workflow:
            raise NotImplementedError(
                "No ComfyUI workflow has been chosen, so there is nothing to run.")
        return {"workflow": self.workflow}

    def _run_pipeline(self, request: GenerationRequest, *, seed: int) -> Any:
        raise NotImplementedError(
            "Submitting a ComfyUI workflow is not wired up yet, so no image was "
            "generated. Nothing was faked.")


class OnnxBackend(_LocalModelBackend):
    """An ONNX image model run through onnxruntime."""

    id = "onnx"
    label = "ONNX Runtime (local model)"
    kind = "onnx"
    required_package = "onnxruntime"
    package_label = "ONNX Runtime"

    def capabilities(self) -> ImageCapabilities:
        return ImageCapabilities(
            text_to_image=True, image_to_image=False, inpaint=False,
            outpaint=False, upscale=True, reference_image=False,
            negative_prompt=False, seed_control=True, steps=False,
            guidance=False, sampler=False, batch=True, max_batch=4,
            max_dimension=2048, min_dimension=64, dimension_multiple=8,
            notes="An ONNX pipeline. Capabilities depend on the model file.")

    def models(self) -> list[ImageModel]:
        return [model for model in super().models()
                if str(model.path).lower().endswith(".onnx")]

    def _create_pipeline(self, model: ImageModel) -> Any:
        import onnxruntime  # type: ignore

        return onnxruntime.InferenceSession(str(model.path),
                                            providers=["CPUExecutionProvider"])

    def _run_pipeline(self, request: GenerationRequest, *, seed: int) -> Any:
        raise NotImplementedError(
            "This ONNX model's input and output layout has not been described, so "
            "it cannot be run safely. Nothing was generated.")
