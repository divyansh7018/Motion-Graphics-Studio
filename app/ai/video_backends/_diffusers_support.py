"""Loading and running a Diffusers video pipeline, kept in one place.

This module is the only code in the application that touches Diffusers for
video.  It imports lazily - nothing happens at application start - and every
failure is turned into a sentence a person can act on.

Two rules are enforced here rather than trusted:

* the pipeline class is taken from the model folder's own metadata or from what
  the user chose; the code never guesses a class and hopes;
* a run that produced no frames is an error, never an empty success.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any, Callable

from .local_model import PIPELINE_NAMES  # noqa: F401 - kept importable from here

__all__ = ["load_video_pipeline", "run_video_pipeline", "export_frames",
           "pick_device"]


def pick_device(choice: str, devices: Any = None) -> tuple[str, str]:
    """The device and dtype to use, from the choice and what is really there.

    ``auto`` prefers CUDA, then Apple's MPS, then CPU.  float16 is only offered
    on a GPU: on CPU it is both unsupported and slower, so choosing it would be
    a way to make the application slower without saying so.
    """
    if devices is None:
        from ...image.device import detect_device

        devices = detect_device()
    preferred = str(choice or "auto").lower()
    if preferred not in ("auto", "cpu", "cuda", "mps"):
        preferred = "auto"
    if preferred == "cuda" and not _has_cuda(devices):
        raise RuntimeError(
            "CUDA was chosen, but no CUDA device was detected. Nothing was "
            "loaded. Choose 'auto' or 'cpu'.")
    if preferred == "auto":
        if _has_cuda(devices):
            return "cuda", "float16"
        if _has_mps(devices):
            return "mps", "float32"
        return "cpu", "float32"
    if preferred == "cuda":
        return "cuda", "float16"
    if preferred == "mps":
        return "mps", "float32"
    return "cpu", "float32"


def _has_cuda(devices: Any) -> bool:
    """Whether the device report really saw a usable CUDA device.

    The lightweight probe and the torch probe both count; nothing is imported
    here just to answer the question (section 41).
    """
    return bool(getattr(devices, "cuda_available", False)) or \
        str(getattr(devices, "accelerator", "")) in ("cuda", "directml")


def _has_mps(devices: Any) -> bool:
    return str(getattr(devices, "accelerator", "")) == "mps"


def _torch_tensor_to_frames(tensor: Any, fps: int) -> tuple[list[Any], int, int]:
    """Turn a pipeline's video tensor into RGB frames."""
    import numpy as np

    array = tensor
    if hasattr(array, "detach"):
        array = array.detach().cpu().numpy()
    array = np.asarray(array)
    while array.ndim > 4:
        array = array[0]
    if array.ndim == 4:
        # (frames, channels, height, width) or (channels, frames, height, width)
        if array.shape[1] in (1, 3, 4) and array.shape[1] < array.shape[0]:
            frames = array
        else:
            frames = np.transpose(array, (1, 0, 2, 3))
    elif array.ndim == 3:
        frames = array[None, ...]
    else:
        raise ValueError(f"The pipeline returned an array of shape {array.shape}, "
                         f"which is not a video.")
    if frames.shape[1] == 1:
        frames = np.repeat(frames, 3, axis=1)
    if frames.shape[1] > 3:
        frames = frames[:, :3]
    if frames.dtype != np.uint8:
        if frames.max() <= 1.0 + 1e-6:
            frames = frames * 255.0
        frames = np.clip(frames, 0, 255).astype(np.uint8)
    height, width = int(frames.shape[2]), int(frames.shape[3])
    return [np.ascontiguousarray(item.transpose(1, 2, 0)) for item in frames], \
        width, height


def load_video_pipeline(folder: Path, *, pipelines: list[str], device: str,
                        dtype: str) -> Any:
    """Load a video pipeline from a local folder, or explain why not."""
    import diffusers  # type: ignore
    import torch  # type: ignore

    device_name, resolved_dtype = pick_device(device)
    if dtype and dtype != "auto":
        resolved_dtype = str(dtype)
    if device_name == "cpu" and resolved_dtype in ("float16", "bfloat16"):
        raise RuntimeError(
            f"{resolved_dtype} is not usable on the CPU, so it was not used. "
            f"Choose float32, or run on a GPU.")

    torch_dtype = {"float32": torch.float32, "float16": torch.float16,
                   "bfloat16": torch.bfloat16}.get(resolved_dtype, torch.float32)
    errors: list[str] = []
    for name in pipelines:
        cls = getattr(diffusers, name, None)
        if cls is None:
            errors.append(f"{name} is not in the installed Diffusers version")
            continue
        try:
            pipeline = cls.from_pretrained(str(folder), torch_dtype=torch_dtype)
        except Exception as exc:  # noqa: BLE001 - a load failure is data
            errors.append(f"{name}: {exc}")
            continue
        pipeline = pipeline.to(device_name)
        # Offloading is only offered where the pipeline has it and only when a
        # GPU is in use; it trades speed for memory and that is the user's call.
        if device_name == "cuda" and hasattr(pipeline, "enable_model_cpu_offload"):
            try:
                pipeline.enable_model_cpu_offload()
            except Exception:  # noqa: BLE001 - optional optimisation
                pass
        return pipeline
    raise RuntimeError("No pipeline could be loaded from "
                       f"{folder}: " + "; ".join(errors[:3]))


def run_video_pipeline(pipeline: Any, request: Any, *, seed: int, fps: int,
                       duration: float,
                       cancel: Any,
                       report: Callable[[str, float], None]) -> Any:
    """One generation, with the parameters this pipeline actually accepts."""
    import torch  # type: ignore

    frames_wanted = max(1, int(round(float(duration) * max(1, fps))))
    parameters = {
        "prompt": str(getattr(request, "prompt", "") or ""),
        "negative_prompt": str(getattr(request, "negative_prompt", "") or "") or None,
        "num_frames": frames_wanted,
        "num_inference_steps": int(getattr(request, "steps", 0) or 0) or None,
        "guidance_scale": float(getattr(request, "guidance", 0.0) or 0.0) or None,
        "width": int(getattr(request, "width", 0) or 0) or None,
        "height": int(getattr(request, "height", 0) or 0) or None,
        "generator": torch.Generator(device="cpu").manual_seed(int(seed)),
    }
    parameters = {key: value for key, value in parameters.items()
                  if value is not None}
    image = str(getattr(request, "source_image", "") or "")
    if image and Path(image).is_file():
        from PIL import Image

        with Image.open(image) as handle:
            handle.load()
            parameters["image"] = handle.convert("RGB").copy()
    strength = float(getattr(request, "strength", 0.0) or 0.0)
    if strength:
        parameters["strength"] = strength

    accepted = _accepted_parameters(pipeline, parameters)
    dropped = [key for key in parameters if key not in accepted]
    if dropped:
        report("RUNNING", 0.15)
    started = time.monotonic()
    try:
        output = pipeline(**accepted)
    except torch.cuda.OutOfMemoryError as exc:  # type: ignore[attr-defined]
        raise RuntimeError(
            "The GPU ran out of memory while generating. Nothing was produced. "
            "Try a smaller resolution or a shorter clip, or unload other "
            f"models. ({exc})") from exc
    seconds = max(0.0, time.monotonic() - started)
    if cancel is not None and cancel.is_cancelled():
        raise RuntimeError("cancelled")
    frames = getattr(output, "frames", None)
    if frames is None:
        frames = getattr(output, "videos", None)
    if frames is None:
        raise RuntimeError("The pipeline finished without returning any frames.")
    return _FrameBundle(frames, fps=fps, seconds=seconds, dropped=dropped)


def _accepted_parameters(pipeline: Any, parameters: dict) -> dict:
    """Drop parameters this pipeline's signature does not take.

    Diffusers pipelines differ; passing an argument a pipeline does not take is
    an immediate crash, so the signature is consulted.  What was dropped is
    reported with the result, never silently forgotten.
    """
    try:
        signature = inspect.signature(pipeline.__call__)
    except (TypeError, ValueError):
        return dict(parameters)
    names = set(signature.parameters)
    if any(item.kind == inspect.Parameter.VAR_KEYWORD
           for item in signature.parameters.values()):
        return dict(parameters)
    return {key: value for key, value in parameters.items() if key in names}


def export_frames(bundle: Any, target: Path, *, fps: int, tools: Any,
                  cancel: Any = None) -> Path:
    """Encode a pipeline's frames to a real file with the shared encoder."""
    from ...render.encode import stream_encode
    from types import SimpleNamespace

    frames, width, height = _torch_tensor_to_frames(bundle.frames, fps)
    settings = SimpleNamespace(
        codec="h264_cpu", encoder_preset="medium", crf=20, bitrate_kbps=0,
        pixel_format="yuv420p", keyframe_interval=0, fps=int(fps),
        container="mp4", audio_codec="aac", audio_bitrate_kbps=192,
        sample_rate=48000)
    encoded = stream_encode(tools=tools, frames=frames, output=target,
                            width=width, height=height, fps=int(fps),
                            settings=settings, cancel_token=cancel,
                            timeout=1800.0)
    if not encoded.ok:
        raise RuntimeError(encoded.error or
                           f"The frames could not be encoded (FFmpeg exited "
                           f"{encoded.returncode}).")
    return target


class _FrameBundle:
    """A pipeline's answer, with the details needed to save it."""

    def __init__(self, frames: Any, *, fps: int, seconds: float,
                 dropped: list) -> None:
        self.frames = frames
        self.fps = int(fps)
        self.seconds = float(seconds)
        self.dropped_parameters = list(dropped)
        self.metadata = {
            "dropped_parameters": list(dropped),
            "inference_seconds": round(float(seconds), 3),
        }

    def save(self, target: Path, *, tools: Any, cancel: Any = None) -> Path:
        return export_frames(self, target, fps=self.fps, tools=tools,
                             cancel=cancel)
