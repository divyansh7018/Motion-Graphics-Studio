"""Local text-to-speech: Kokoro 82M only (directive sections 2-4).

Kokoro is the single narration engine in V1.  There is no fallback to any other
engine and no network path: if Kokoro is missing or broken, narration reports
that honestly and the rest of the application keeps working.

Importing this package does no work.  Detection, model loading, voice scanning
and generation all happen inside background jobs, on worker threads, only when
the user asks for them.
"""

from .audio import (
    WavInfo,
    duration_seconds,
    format_seconds,
    read_wav,
    validate_wav,
    write_wav,
)
from .cache import (
    StalenessReport,
    cache_key,
    compare,
    settings_hash,
    source_hash,
)
from .capabilities import (
    ENGINE_ID,
    ENGINE_LABEL,
    KokoroStatus,
    probe_kokoro,
    probe_model,
    probe_runtime,
    requirements_summary,
)
from .engine import (
    GenerationRequest,
    GenerationResult,
    KokoroEngine,
    clamp_speed,
    clamp_volume,
)
from .narration import (
    NarrationError,
    NarrationOutcome,
    NarrationSettings,
    generate_narration,
    plan_outputs,
    refresh_statuses,
    status_explanation,
    validate_settings,
)
from .preprocess import (
    PreprocessOptions,
    PreprocessResult,
    chunk_text,
    preprocess,
)
from .voices import (
    VoiceCatalogue,
    VoiceInfo,
    discover_voices,
    filter_voices,
    language_label,
    validate_voice_choice,
)

__all__ = [
    "ENGINE_ID",
    "ENGINE_LABEL",
    "GenerationRequest",
    "GenerationResult",
    "KokoroEngine",
    "KokoroStatus",
    "NarrationError",
    "NarrationOutcome",
    "NarrationSettings",
    "PreprocessOptions",
    "PreprocessResult",
    "StalenessReport",
    "VoiceCatalogue",
    "VoiceInfo",
    "WavInfo",
    "cache_key",
    "chunk_text",
    "clamp_speed",
    "clamp_volume",
    "compare",
    "discover_voices",
    "duration_seconds",
    "filter_voices",
    "format_seconds",
    "generate_narration",
    "language_label",
    "plan_outputs",
    "preprocess",
    "probe_kokoro",
    "probe_model",
    "probe_runtime",
    "read_wav",
    "refresh_statuses",
    "requirements_summary",
    "settings_hash",
    "source_hash",
    "status_explanation",
    "validate_settings",
    "validate_voice_choice",
    "validate_wav",
    "write_wav",
]
