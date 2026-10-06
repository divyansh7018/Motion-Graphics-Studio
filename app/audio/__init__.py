"""The audio subsystem (Stage E).

Importing this package starts nothing: no FFmpeg process, no decoding, no files.
Every function here either computes a plan or is called explicitly by a job, the
CLI or a test (directive section 84).
"""

from __future__ import annotations

from .ducking import (
    DEFAULT_DUCK_AMOUNT,
    DuckingSettings,
    duck_expression,
    duck_level_at,
    merge_windows,
    narration_windows,
)
from .mix import (
    AudioIssue,
    MixInput,
    MixPlan,
    build_mix_command,
    resolve_mix_inputs,
)
from .service import (
    AudioService,
    AudioValidation,
    MasterAudioResult,
    NarrationPlacement,
)

__all__ = [
    "AudioIssue",
    "AudioService",
    "AudioValidation",
    "DEFAULT_DUCK_AMOUNT",
    "DuckingSettings",
    "MasterAudioResult",
    "MixInput",
    "MixPlan",
    "NarrationPlacement",
    "build_mix_command",
    "duck_expression",
    "duck_level_at",
    "merge_windows",
    "narration_windows",
    "resolve_mix_inputs",
]
