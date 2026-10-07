"""Real local video-model adapters: Diffusers and a local Python model.

These are the two honest paths to real AI video generation.  Both are written
so that the *only* thing standing between the user and a real clip is the
weights and the package - not the code in this application:

* :class:`DiffusersVideoBackend` discovers the Diffusers package and the local
  model folders, reports exactly what it found, and refuses to guess a pipeline
  it was not told about.  Given a pipeline and weights it loads them on demand,
  runs one inference, and unloads.
* :class:`LocalPythonVideoBackend` loads a module the user chose, checks that
  it exposes the documented entry point, and calls it in-process with a
  cancel/poll callback.  A module that does not match the contract is reported,
  not worked around.

Neither of them ever reports success without a file.  Neither loads weights
until a job is actually running (sections 11, 92).  When the package or the
weights are absent, the state is NOT INSTALLED and the interface says so.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.logging_setup import log_event
from ...image.saving import unique_path
from ..capabilities import AICapabilities
from ..models import ModelStore, human_bytes
from ..provider import ProviderStatus, SettingField
from ..types import BackendKind, DeviceRequirement, ProviderState
from ..video import (VideoModel, VideoProvider, VideoRequest, VideoResult,
                     VideoState)
from ..video_validation import validate_video_file

__all__ = ["DiffusersVideoBackend", "LocalPythonVideoBackend",
           "PIPELINE_NAMES", "ENTRY_POINT"]

#: Pipelines Stage G looks for, in the order it prefers them.  Each one is a
#: real Diffusers class name; a model folder usually carries its own
#: ``model_index.json`` that names its pipeline, and that wins.
PIPELINE_NAMES: tuple[str, ...] = (
    "CogVideoXPipeline", "WanPipeline", "LTXPipeline",
    "StableVideoDiffusionPipeline", "AnimateDiffPipeline",
    "TextToVideoSDPipeline", "I2VGenXLPipeline",
)

#: The function a local Python video model must provide.
ENTRY_POINT = "generate_video"

MODULE_CONTRACT = (
    f"The module must define {ENTRY_POINT}(request: dict, output_path: str, "
    f"progress=None, cancel=None) -> dict, write the clip to output_path, and "
    f"return {{'ok': True, 'path': '<the file>'}} - or {{'ok': False, "
    f"'error': '<why>'}}.")


class _LocalVideoBase(VideoProvider):
    """Shared plumbing for backends whose work happens on this machine."""

    kind = BackendKind.VIDEO
    licence = ""
    homepage = ""

    def __init__(self, tools: Any = None, *, timeout: float = 3600.0,
                 store: Optional[ModelStore] = None) -> None:
        self.tools = tools
        self.timeout = float(timeout or 3600.0)
        self.store = store or ModelStore()
        self._loaded = ""
        self._pipeline = None
        self._pipeline_key = ""

    def uses_network(self) -> bool:
        """Local models may reach a model hub to fetch weights.  Only if asked."""
        return True

    def load(self, model: Any = None) -> None:
        if self._loaded == str(getattr(model, "id", "")):
            return
        self.unload()
        self._pipeline = self._create_pipeline(model)
        self._pipeline_key = str(getattr(model, "id", "") or "")
        self._loaded = self._pipeline_key

    def unload(self) -> None:
        pipeline = self._pipeline
        self._pipeline = None
        self._loaded = ""
        self._pipeline_key = ""
        if pipeline is None:
            return
        # Release the tensors the model held, so a second model can fit.
        for attribute in ("to", "to_empty"):
            method = getattr(pipeline, attribute, None)
            if attribute == "to" and callable(method):
                try:
                    method("cpu")
                except Exception:  # noqa: BLE001 - optional tidy-up
                    pass
        for name in ("unet", "transformer", "vae", "text_encoder", "scheduler"):
            part = getattr(pipeline, name, None)
            if part is not None and hasattr(part, "cpu"):
                try:
                    part.cpu()
                except Exception:  # noqa: BLE001
                    pass
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass
        self._free_accelerator_cache()

    @staticmethod
    def _free_accelerator_cache() -> None:
        """Ask the GPU to release its cache - only when a GPU is actually there."""
        try:
            from ...image.device import detect_device

            info = detect_device()
            if not (getattr(info, "cuda_available", False)
                    or str(getattr(info, "accelerator", "")) == "cuda"):
                return
            torch = sys.modules.get("torch")
            if torch is None:
                return
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - releasing memory is best-effort
            pass

    def loaded_model(self) -> str:
        return self._loaded

    def _create_pipeline(self, model: Any) -> Any:
        raise NotImplementedError

    def _run_pipeline(self, request: VideoRequest, *, seed: int,
                      cancel: Any, report: Callable[[str, float], None]) -> Any:
        raise NotImplementedError

    def generate(self, request: VideoRequest, *,
                 progress: Optional[Callable[[str, float], None]] = None,
                 cancel: Any = None) -> VideoResult:
        started = self.started()
        report = progress or (lambda _state, _fraction: None)
        issues = [issue for issue in self.validate(request)
                  if issue.severity == "error"]
        if issues:
            first = issues[0]
            return self._failed(request, started, first.code, first.message,
                                "The request cannot be run as it is.",
                                first.what_to_do)
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        model_id = str(request.model or "")
        try:
            selected = self._select_model(model_id)
        except LookupError as exc:
            return self._failed(
                request, started, "MODEL_NOT_FOUND", str(exc),
                "The chosen model is not on this machine.",
                "Install the model, or choose another one in the model list.")
        if selected is None:
            return self._failed(
                request, started, "NO_MODEL_INSTALLED",
                "No model is configured for this backend.",
                "This backend runs a model you install; none is set up.",
                "Choose a model folder in Settings -> AI Studio.")

        seed = int(request.seed or 0) or self._fresh_seed(request)
        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{request.output_format or 'mp4'}"
        target = unique_path(output_dir, request.name_stem or "clip", suffix)

        try:
            report("INITIALIZING", 0.02)
            self.load(selected)
        except NotImplementedError as exc:
            return self._failed(
                request, started, "MODEL_NOT_CONFIGURED", str(exc),
                "Nothing was generated: the backend does not know how to load "
                "this model.",
                "Describe the model in Settings -> AI Studio, or use another "
                "backend.")
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            return self._failed(
                request, started, "MODEL_LOAD_FAILED",
                f"The model could not be loaded: {exc}",
                "The weights or the package the model needs are not usable.",
                "Check the model folder, and that the required package is "
                "installed.")
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)

        report("RUNNING", 0.1)
        try:
            produced = self._run_pipeline(request, seed=seed, cancel=cancel,
                                          report=report)
        except NotImplementedError as exc:
            return self._failed(request, started, "MODEL_RUN_NOT_IMPLEMENTED",
                                str(exc), "Nothing was generated.",
                                "Use a backend that supports this operation.")
        except (OSError, RuntimeError, ValueError) as exc:
            return self._failed(
                request, started, "MODEL_RUN_FAILED",
                f"The model stopped with an error: {exc}",
                "The model itself reported a failure.",
                "Check the model's own log; the same run in its own tool will "
                "show the details.")
        if cancel is not None and cancel.is_cancelled():
            return self._cancelled(request, started)
        if hasattr(produced, "save") and callable(getattr(produced, "save")):
            # A pipeline that returns frames rather than a file: the frames are
            # encoded here with the same encoder the renderer uses, and the
            # result is measured like any other clip.
            try:
                produced = produced.save(target, tools=self.tools, cancel=cancel)
            except (OSError, RuntimeError, ValueError) as exc:
                return self._failed(
                    request, started, "MODEL_SAVE_FAILED",
                    f"The model produced frames that could not be saved: {exc}",
                    "The frames were made but not written as a video.",
                    "Check that FFmpeg is installed and the output folder is "
                    "writable.")
        path = Path(getattr(produced, "path", "") or produced or "")
        if not path or not Path(path).is_file():
            return self._failed(
                request, started, "MODEL_NO_OUTPUT",
                "The model finished without writing a file.",
                "No clip was produced, so this is not a success.",
                "Check the model's output folder and its log.")

        report("SAVING", 0.9)
        if Path(path).resolve() != target.resolve():
            try:
                target.write_bytes(Path(path).read_bytes())
            except OSError as exc:
                return self._failed(
                    request, started, "MODEL_OUTPUT_UNREADABLE",
                    f"The model's clip could not be copied: {exc}",
                    "The file it produced is not readable.",
                    "Check the model's output folder permissions.")
        check = validate_video_file(target, tools=self.tools,
                                    expected_width=request.width,
                                    expected_height=request.height,
                                    expected_fps=request.fps,
                                    expected_duration=request.duration)
        if check.code == "VIDEO_CHECK_NOT_AVAILABLE":
            return self._failed(
                request, started, "CLIP_UNMEASURED",
                "The model wrote a file but FFmpeg is not available to check it.",
                "An unmeasurable file is not reported as a valid clip.",
                "Install FFmpeg, then generate again.")
        if not check.ok:
            _remove_quietly(target)
            return self._failed(request, started, check.code, check.error,
                                check.why, check.what_to_do)

        log_event("AI_CLIP_WRITTEN",
                  f"{self.id} model produced {target.name}",
                  backend=self.id, model=selected.id, seed=seed)
        result = VideoResult(
            ok=True, path=target, state=VideoState.COMPLETED, seed=seed,
            model=selected.id, backend=self.id, mode=request.mode,
            width=check.width, height=check.height, fps=check.fps,
            duration=check.duration, frames=check.frames,
            output_format=request.output_format or "mp4",
            has_audio=check.has_audio, requested=request.to_dict(),
            seconds=self.started() - started, mismatch=list(check.mismatch))
        result.quality = {
            "model": selected.id, "backend": self.id,
            "resolution": f"{result.width}x{result.height}",
            "fps": round(result.fps, 3), "duration": round(result.duration, 3),
            "seed": seed, "size_bytes": check.size_bytes,
            "measured_with": check.measured_with, "status": "measured"}
        result.metadata = dict(getattr(produced, "metadata", {}) or {})
        result.metadata.update({
            "prompt": request.prompt, "negative_prompt": request.negative_prompt,
            "seed": seed, "mode": request.mode, "camera": request.camera,
            "generator": self.id, "is_ai_model": True,
            "model_path": str(getattr(selected, "path", "") or ""),
            **self.reproducibility(),
        })
        return result

    def _select_model(self, model_id: str) -> Optional[VideoModel]:
        available = self.models()
        if not model_id:
            return available[0] if available else None
        for model in available:
            if model.id == model_id:
                return model
        raise LookupError(f"No model called '{model_id}' was found for {self.name}.")

    def reproducibility(self) -> dict:
        """What has to be true for a clip to be reproducible (section 100)."""
        return {"package": self.required_package or "the model you run",
                "loaded": bool(self._loaded)}

    @staticmethod
    def _fresh_seed(request: VideoRequest) -> int:
        import hashlib
        import time as _time

        raw = f"{request.prompt}|{request.mode}|{_time.time()}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") or 1

    def _failed(self, request: VideoRequest, started: float, code: str,
                error: str, why: str, what_to_do: str) -> VideoResult:
        log_event("AI_CLIP_FAILED", error, code=code, backend=self.id)
        return VideoResult(ok=False, state=VideoState.FAILED, mode=request.mode,
                           backend=self.id, model=request.model, error=error,
                           why=why, what_to_do=what_to_do, code=code,
                           seconds=self.started() - started)

    def _cancelled(self, request: VideoRequest, started: float) -> VideoResult:
        return VideoResult(ok=False, state=VideoState.CANCELLED, cancelled=True,
                           mode=request.mode, backend=self.id,
                           model=request.model,
                           error="Clip generation was cancelled.",
                           seconds=self.started() - started)


class DiffusersVideoBackend(_LocalVideoBase):
    """Local video generation through the Diffusers package (section 39)."""

    id = "diffusers_video"
    name = "Diffusers (local video model)"
    transport = "python"
    required_package = "diffusers"
    package_label = "Diffusers"
    device_requirement = DeviceRequirement.GPU_RECOMMENDED
    licence = "Diffusers (Apache-2.0); the model's own licence applies to the weights"
    homepage = "https://github.com/huggingface/diffusers"

    def __init__(self, tools: Any = None, *, model_path: str = "",
                 pipeline: str = "", dtype: str = "auto", device: str = "auto",
                 timeout: float = 3600.0, store: Optional[ModelStore] = None) -> None:
        super().__init__(tools, timeout=timeout, store=store)
        self.model_path = str(model_path or "")
        self.pipeline_name = str(pipeline or "")
        self.dtype_choice = str(dtype or "auto")
        self.device_choice = str(device or "auto")

    # -- description -------------------------------------------------------

    def describe(self) -> str:
        if not self.required_package_present():
            return (f"Runs a local video model through {self.package_label}, "
                    f"which is not installed.")
        if not self.model_path:
            return (f"{self.package_label} is installed. Choose a local video "
                    f"model folder to run.")
        return (f"Loads {self.model_path} on demand with {self.package_label} "
                f"and runs one clip per job. Nothing is loaded until a job "
                f"starts.")

    def location(self) -> str:
        if not self.model_path:
            return f"{self.package_label}: installed, no model chosen"
        return f"{self.package_label}\nModel: {self.model_path}"

    def install_hint(self) -> str:
        return (f"Install {self.package_label} into this application's "
                f"environment, then choose a local video model folder.")

    def required_package_present(self) -> bool:
        return importlib.util.find_spec(self.required_package) is not None

    def settings_schema(self) -> list:
        return [
            SettingField(name="model_path", label="Model folder", kind="path",
                         default=self.model_path,
                         help="A folder of local video-model weights."),
            SettingField(name="pipeline", label="Pipeline class", kind="text",
                         default=self.pipeline_name,
                         help=("Optional. Left empty, the pipeline named in the "
                               "model folder's model_index.json is used.")),
            SettingField(name="device", label="Device", kind="choice",
                         default="auto", choices=["auto", "cpu", "cuda", "mps"],
                         help="'auto' picks the best available device."),
            SettingField(name="dtype", label="Precision", kind="choice",
                         default="auto",
                         choices=["auto", "float32", "float16", "bfloat16"],
                         help="float16 needs a GPU; CPU always uses float32."),
        ]

    # -- discovery ---------------------------------------------------------

    def status(self) -> ProviderStatus:
        if not self.required_package_present():
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=(f"{self.package_label} is not installed, so no local "
                        f"video model can be loaded."),
                instructions=[f"Install {self.package_label} in this "
                              f"application's environment.",
                              "Then choose a model folder in Settings -> AI Studio."],
                device="cpu")
        if not self.model_path:
            return ProviderStatus(
                state=ProviderState.LIMITED,
                reason=(f"{self.package_label} is installed, but no video model "
                        f"folder has been chosen."),
                instructions=["Choose a folder of local video-model weights."],
                version=self._package_version(), device="cpu")
        folder = Path(self.model_path)
        if not folder.exists():
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=f"The model folder was not found: {folder}",
                instructions=["Choose the model folder again - it may have moved."],
                version=self._package_version(), device="cpu")
        weights = self.store.weights_in(folder)
        if not weights:
            return ProviderStatus(
                state=ProviderState.LIMITED,
                reason=(f"{folder} exists but holds no model weights "
                        f"(.safetensors, .bin, .ckpt or .onnx)."),
                instructions=["Choose the folder that contains the model files."],
                version=self._package_version(), device="cpu")
        return ProviderStatus(
            state=ProviderState.AVAILABLE,
            reason=(f"{self.package_label} {self._package_version() or ''} with "
                    f"{len(weights)} weight file(s) in {folder.name}.").strip(),
            version=self._package_version(), device="cpu")

    def _package_version(self) -> str:
        try:
            from importlib.metadata import version

            return version(self.required_package)
        except Exception:  # noqa: BLE001 - a missing version is not a failure
            return ""

    def capabilities(self) -> AICapabilities:
        return AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=False,
            video_extend=False, reference_image=False, negative_prompt=True,
            seed_control=True, steps=True, guidance=True, sampler=True,
            duration=True, fps=True, resolution_control=True, quality=True,
            batch=False, strength=True, offload=True, device_choice=True,
            dtype_choice=True, max_dimension=1920, min_dimension=64,
            dimension_multiple=8, min_duration=0.5, max_duration=30.0,
            max_fps=30,
            notes=("Local Diffusers video pipeline. Long clips are made from "
                   "several short runs, and a CPU run of a large model is slow."))

    def models(self) -> list[VideoModel]:
        if not self.model_path:
            return []
        found = self.store.discover_video_models()
        for model in found:
            if str(model.path) == str(Path(self.model_path)):
                return [model]
        return [VideoModel(
            id=Path(self.model_path).name, name=Path(self.model_path).name,
            backend=self.id, kind="diffusers", path=self.model_path,
            capabilities=self.capabilities(),
            requirement=DeviceRequirement.GPU_RECOMMENDED,
            status="available" if Path(self.model_path).exists() else "missing",
            notes=MODULE_CONTRACT)]

    # -- pipeline ----------------------------------------------------------

    def pipelines_in(self, folder: Path) -> list[str]:
        """Which known pipelines this folder could be loaded as."""
        names: list[str] = []
        index = Path(folder) / "model_index.json"
        if index.is_file():
            try:
                import json

                payload = json.loads(index.read_text(encoding="utf-8"))
                for key in payload:
                    if key.endswith("Pipeline"):
                        names.append(str(key))
            except (OSError, ValueError):
                pass
        if self.pipeline_name:
            names = [self.pipeline_name] + [n for n in names if n != self.pipeline_name]
        if not names:
            names = ["CogVideoXPipeline"]
        return names

    def _create_pipeline(self, model: Any) -> Any:
        if not self.required_package_present():
            raise ImportError(f"{self.package_label} is not installed.")
        folder = Path(getattr(model, "path", "") or self.model_path)
        if not folder.exists():
            raise FileNotFoundError(f"The model folder was not found: {folder}")
        from ._diffusers_support import load_video_pipeline

        return load_video_pipeline(
            folder, pipelines=self.pipelines_in(folder),
            device=self.device_choice, dtype=self.dtype_choice)

    def _run_pipeline(self, request: VideoRequest, *, seed: int,
                      cancel: Any, report: Callable[[str, float], None]) -> Any:
        from ._diffusers_support import run_video_pipeline

        fps = int(request.fps or 8)
        duration = float(request.duration or 2.0)
        return run_video_pipeline(
            self._pipeline, request, seed=seed, fps=fps, duration=duration,
            cancel=cancel, report=report)

    def reproducibility(self) -> dict:
        return {"package": f"diffusers {self._package_version()}".strip(),
                "model_path": self.model_path,
                "device": self.device_choice, "dtype": self.dtype_choice}


class LocalPythonVideoBackend(_LocalVideoBase):
    """A local Python module you point the application at (section 2).

    The module is imported only when a job runs, and only if it matches the
    documented contract.  Nothing is downloaded, and a module that does not
    provide the entry point is reported rather than called hopefully.
    """

    id = "python_video"
    name = "Local Python video model"
    transport = "python"
    required_package = ""
    package_label = "Python"
    device_requirement = DeviceRequirement.UNKNOWN
    licence = "The implementation you install"
    homepage = ""

    def __init__(self, tools: Any = None, *, module_path: str = "",
                 timeout: float = 3600.0,
                 store: Optional[ModelStore] = None) -> None:
        super().__init__(tools, timeout=timeout, store=store)
        self.module_path = str(module_path or "")

    def describe(self) -> str:
        if not self.module_path:
            return ("Runs a local Python video model you choose. No module has "
                    "been chosen yet.")
        return (f"Runs {self.module_path} in this application's process. "
                f"{MODULE_CONTRACT}")

    def location(self) -> str:
        return self.module_path or "(no module chosen)"

    def install_hint(self) -> str:
        return ("Choose a Python module that implements generate_video(). "
                "Nothing is downloaded automatically.")

    def uses_network(self) -> bool:
        return False

    def settings_schema(self) -> list:
        return [
            SettingField(name="module_path", label="Python module", kind="path",
                         default=self.module_path, required=True,
                         help=MODULE_CONTRACT),
            SettingField(name="timeout", label="Timeout (seconds)", kind="float",
                         default=self.timeout),
        ]

    def status(self) -> ProviderStatus:
        if not self.module_path:
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason="No local Python video module has been chosen.",
                instructions=[MODULE_CONTRACT], device="cpu")
        path = Path(self.module_path)
        if not path.is_file():
            return ProviderStatus(
                state=ProviderState.NOT_INSTALLED,
                reason=f"The module was not found: {path}",
                instructions=["Choose the file again - it may have moved."],
                device="cpu")
        spec = self._spec(path)
        if spec is None:
            return ProviderStatus(
                state=ProviderState.NOT_VERIFIED,
                reason=f"The module could not be inspected: {path.name}",
                instructions=["Check the file is readable Python."],
                device="cpu")
        if spec is None or ENTRY_POINT not in (spec or "").split():
            return ProviderStatus(
                state=ProviderState.LIMITED,
                reason=(f"{path.name} does not define {ENTRY_POINT}()."),
                instructions=[MODULE_CONTRACT], device="cpu")
        return ProviderStatus(
            state=ProviderState.AVAILABLE,
            reason=f"{path.name} defines {ENTRY_POINT}().",
            version="", device="cpu")

    def _spec(self, path: Path) -> Optional[str]:
        """Names defined at the top level, read without importing the module."""
        import ast

        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return None
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.append(target.id)
        return " ".join(names)

    def capabilities(self) -> AICapabilities:
        return AICapabilities(
            text_to_video=True, image_to_video=True, video_to_video=True,
            video_extend=True, reference_image=True, negative_prompt=True,
            seed_control=True, duration=True, fps=True, resolution_control=True,
            quality=True, batch=False, strength=True, device_choice=True,
            dtype_choice=True, max_dimension=7680, min_dimension=16,
            dimension_multiple=2, min_duration=0.1, max_duration=1800.0,
            max_fps=120,
            notes=("Whatever the module you install supports. The application "
                   "cannot see inside it, so nothing more is claimed."))

    def models(self) -> list[VideoModel]:
        if not self.module_path or not Path(self.module_path).is_file():
            return []
        return [VideoModel(
            id=Path(self.module_path).stem, name=Path(self.module_path).name,
            backend=self.id, kind="python", path=self.module_path,
            capabilities=self.capabilities(),
            requirement=DeviceRequirement.UNKNOWN, status="available",
            notes=MODULE_CONTRACT)]

    def _create_pipeline(self, model: Any) -> Any:
        path = Path(str(getattr(model, "path", "") or self.module_path))
        if not path.is_file():
            raise FileNotFoundError(f"The module was not found: {path}")
        spec = self._spec(path)
        if spec is None or ENTRY_POINT not in spec.split():
            raise ValueError(
                f"{path.name} does not define {ENTRY_POINT}(), so it cannot be "
                f"run. Nothing was called.")
        name = f"mgs_ai_model_{path.stem}"
        module_spec = importlib.util.spec_from_file_location(name, path)
        if module_spec is None or module_spec.loader is None:
            raise ValueError(f"{path.name} could not be loaded as a module.")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
        entry = getattr(module, ENTRY_POINT, None)
        if not callable(entry):
            raise ValueError(f"{path.name} defines {ENTRY_POINT} but it is not "
                             f"callable.")
        return entry

    def _run_pipeline(self, request: VideoRequest, *, seed: int,
                      cancel: Any, report: Callable[[str, float], None]) -> Any:
        entry = self._pipeline
        output_dir = Path(request.output_dir or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        target = unique_path(output_dir, request.name_stem or "clip",
                             f".{request.output_format or 'mp4'}")
        payload = request.to_dict()
        payload["seed"] = seed

        def check_cancel() -> bool:
            return bool(cancel is not None and cancel.is_cancelled())

        answer = entry(payload, str(target), progress=report,
                       cancel=check_cancel)
        if check_cancel():
            import builtins

            raise builtins.RuntimeError("cancelled")
        if isinstance(answer, dict):
            if answer.get("ok") and answer.get("path"):
                return Path(str(answer["path"]))
            if answer.get("ok"):
                return Path(target)
            raise ValueError(str(answer.get("error") or
                                 "The module reported that it did nothing."))
        if isinstance(answer, (str, Path)) and str(answer):
            return Path(str(answer))
        if Path(target).is_file():
            return target
        raise ValueError("The module returned no path and wrote no file.")

    def reproducibility(self) -> dict:
        return {"package": "the module you install",
                "module": self.module_path}


def model_summary(model: VideoModel) -> str:
    """One line describing a model, for the manager list."""
    size = human_bytes(int(getattr(model, "size_bytes", 0) or 0))
    return f"{model.name} ({model.backend}{', ' + size if size else ''})"


def _remove_quietly(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass
