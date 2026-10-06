"""Image backend adapters (Stage F, section 2).

Six adapters, all behind the same :class:`~app.image.provider.ImageProvider`
contract, so no part of the application is wired to one particular model:

``standard``
    Built in.  No model, no prompt understanding: resizes, variations,
    outpainting and masks.  Always available.
``command``
    A local program the user configured.
``http``
    A local HTTP endpoint on this machine (loopback only).
``comfyui``
    A local ComfyUI server.
``diffusers``
    A local Diffusers-compatible model on disk.
``onnx``
    A local ONNX model run through onnxruntime.

Importing this package loads no model and imports no optional dependency: each
adapter decides for itself what it needs, inside its own ``status()``.

Construction happens in :class:`~app.image.registry.BackendRegistry`, which is
the only place that builds adapters - it records the reason when one cannot be
built, rather than skipping it quietly.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import cancelled_result, failed_result, run_process, verify_output
from .command import CommandBackend
from .http import HttpBackend
from .model_backends import ComfyUIBackend, DiffusersBackend, OnnxBackend
from .standard import StandardBackend

__all__ = [
    "BACKEND_CLASSES",
    "BACKEND_ORDER",
    "CommandBackend",
    "HttpBackend",
    "StandardBackend",
    "DiffusersBackend",
    "ComfyUIBackend",
    "OnnxBackend",
    "run_process",
    "verify_output",
    "cancelled_result",
    "failed_result",
]

#: Adapter id -> class.  The order is the order the interface lists them in:
#: the one that always works first, then the ones that need something installed.
BACKEND_CLASSES: dict[str, Callable[..., Any]] = {
    "standard": StandardBackend,
    "command": CommandBackend,
    "http": HttpBackend,
    "comfyui": ComfyUIBackend,
    "diffusers": DiffusersBackend,
    "onnx": OnnxBackend,
}

BACKEND_ORDER: tuple[str, ...] = (
    "standard", "command", "http", "comfyui", "diffusers", "onnx",
)
