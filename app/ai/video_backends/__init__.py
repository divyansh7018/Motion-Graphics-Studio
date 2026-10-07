"""Video backend adapters (Stage G, sections 2, 3, 21, 79).

Seven adapters, all behind the same :class:`~app.ai.video.VideoProvider`
contract, so nothing in the studio is wired to one particular model:

``standard``
    **TEST BACKEND (fixture - not an AI model).** Writes a real MP4 with
    FFmpeg from a deterministic picture, and moves a source image or clip when
    asked.  Its output is real; its intelligence is not, and it says so
    everywhere.
``command``
    A local program the user configured, with placeholders.
``http``
    A local HTTP video endpoint (loopback only).
``comfyui``
    A local ComfyUI server, driven by the user's own workflow.
``diffusers_video``
    A local Diffusers video model folder.
``python_video``
    A local Python module implementing ``generate_video``.

Importing this package imports no optional dependency and loads no model: each
adapter decides what it needs inside its own ``status()``, and the Diffusers
package is imported only when a pipeline is actually loaded.

The order is the order the interface lists them in: the test backend first,
because it always works and is clearly labelled, then the ones that need
something installed.
"""

from __future__ import annotations

from typing import Any, Callable

from .command import CommandVideoBackend
from .comfyui import ComfyUIVideoBackend
from .frames import (CAMERA_MOVES_SUPPORTED, CameraMove, image_frames,
                     synthetic_frames, transform_frames, video_frames)
from .http import HttpVideoBackend
from .local_model import DiffusersVideoBackend, LocalPythonVideoBackend
from .standard import TEST_BACKEND_LABEL, TEST_BACKEND_NOTE, TEST_MODEL_ID, \
    StandardVideoBackend

__all__ = [
    "VIDEO_BACKEND_CLASSES",
    "VIDEO_BACKEND_ORDER",
    "VIDEO_BACKEND_LABELS",
    "CommandVideoBackend",
    "ComfyUIVideoBackend",
    "HttpVideoBackend",
    "DiffusersVideoBackend",
    "LocalPythonVideoBackend",
    "StandardVideoBackend",
    "TEST_BACKEND_LABEL",
    "TEST_BACKEND_NOTE",
    "TEST_MODEL_ID",
    "CAMERA_MOVES_SUPPORTED",
    "CameraMove",
    "image_frames",
    "synthetic_frames",
    "transform_frames",
    "video_frames",
]

#: Adapter id -> class.
VIDEO_BACKEND_CLASSES: dict[str, Callable[..., Any]] = {
    "standard_video": StandardVideoBackend,
    "command_video": CommandVideoBackend,
    "http_video": HttpVideoBackend,
    "comfyui_video": ComfyUIVideoBackend,
    "diffusers_video": DiffusersVideoBackend,
    "python_video": LocalPythonVideoBackend,
}

VIDEO_BACKEND_ORDER: tuple[str, ...] = (
    "standard_video", "command_video", "http_video", "comfyui_video",
    "diffusers_video", "python_video",
)

#: Plain names for the interfaces that are not the studio (reports, CLI lists).
VIDEO_BACKEND_LABELS: dict[str, str] = {
    "standard_video": TEST_BACKEND_LABEL,
    "command_video": "Local video command",
    "http_video": "Local HTTP video endpoint",
    "comfyui_video": "ComfyUI (local video workflow)",
    "diffusers_video": "Diffusers (local video model)",
    "python_video": "Local Python video model",
}
