"""The AI Studio (Stage G).

A local-first, CPU-first, honest generation layer.  Everything here follows
three rules that are worth stating once, at the top:

1. **No fake AI.**  A backend that is not installed says NOT INSTALLED; a check
   that could not run says CHECK NOT AVAILABLE; only a real generation earns
   VERIFIED.  The built-in clip and image fixtures are labelled TEST BACKEND
   and are never reported as an AI model.
2. **One job, one result.**  A user action submits exactly one job; a batch is
   an explicit action; a cancelled job stops the process, the request or the
   model behind it and keeps what it had already written.
3. **Nothing is silent.**  A backend, model, seed, size or prompt that ends up
   different from the request is shown, not corrected.

The layers, from the bottom up:

``types`` / ``capabilities`` / ``provider`` / ``video``
    the vocabulary every backend answers in;
``models`` / ``health`` / ``registry``
    finding models, checking backends, and choosing one
    (:class:`~app.ai.registry.AIBackendManager`);
``backend`` / ``video_backends``
    the adapters, all behind one contract;
``jobs`` / ``service``
    the orchestration the GUI and the CLI both use;
``history`` / ``prompts`` / ``presets`` / ``references`` / ``cache``
    what the studio remembers;
``integration`` / ``validation``
    getting a result into a project, and checking it on the way.

Importing this package loads no model and imports no optional dependency:
the heavy imports (Diffusers, torch, onnxruntime) happen inside the backend
that needs them, at the moment a job runs.
"""

from __future__ import annotations

__all__ = [
    "AIBackendManager",
    "AIService",
    "AIJob",
    "AIJobRegistry",
    "VideoRequest",
    "VideoResult",
    "VideoProvider",
    "VideoModel",
    "VideoMode",
    "ProviderState",
    "DeviceRequirement",
    "AIJobState",
    "AIOperation",
    "BackendKind",
    "VerificationLevel",
    "AICapabilities",
    "HumanReadable", "describe_capabilities",
]

from .capabilities import AICapabilities
from .jobs import AIJob, AIJobRegistry
from .provider import describe_capabilities  # noqa: F401 - re-exported
from .registry import AIBackendManager
from .service import AIService
from .types import (AIOperation, AIJobState, BackendKind, DeviceRequirement,
                    ProviderState, VerificationLevel)
from .video import (VideoMode, VideoModel, VideoProvider, VideoRequest,
                    VideoResult)

#: A convenience alias used by the interfaces when showing capabilities.
HumanReadable = describe_capabilities
