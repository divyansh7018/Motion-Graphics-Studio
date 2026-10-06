"""Final render pipeline (Stage E).

Importing this package has no side effects: no subprocess is started, no file is
read and nothing is rendered until a service is called (directive section 84).
"""

from __future__ import annotations

from .capabilities import (
    CapabilityIssue,
    EncoderCapabilities,
    detect_capabilities,
    estimate_file_size,
    estimate_render_time,
    rate_control_modes,
    validate_export_settings,
)
from .encode import (
    EncodeResult,
    burn_subtitles,
    concat_and_mux,
    probe_detect,
    stream_encode,
    video_encoder_args,
)
from .engine import (
    AUDIO,
    CANCELLED,
    COMPLETED,
    ENCODING,
    FAILED,
    PREPARING,
    QC,
    QUEUED,
    RENDER_STATES,
    SCENES,
    SUBTITLES,
    VALIDATING,
    RenderEngine,
    RenderProgress,
    RenderRequest,
    RenderResult,
)
from .frames import FrameError, FrameSource
from .output import (
    DEFAULT_TEMPLATE,
    HistoryEntry,
    OutputDecision,
    OutputService,
    render_template,
    sanitize_component,
    sequence_from_name,
)
from .platform import (
    PLATFORM_KEYS,
    PLATFORM_PRESETS,
    PlatformPreset,
    apply_platform,
    platform_from_dict,
    platform_preset,
    platforms_from_project,
)
from .qc import FAIL, PASS, QCIssue, QCReport, QCService, WARNING
from .service import ExportOptions, RenderPlanSummary, RenderService
from .segments import (
    RenderPlanIssue,
    RenderSegment,
    SegmentPlan,
    plan_segments,
    validate_plan,
)

__all__ = [
    "AUDIO", "CANCELLED", "COMPLETED", "DEFAULT_TEMPLATE", "ENCODING", "FAIL",
    "FAILED", "PASS", "PREPARING", "QC", "QUEUED", "RENDER_STATES", "SCENES",
    "SUBTITLES", "VALIDATING", "WARNING",
    "CapabilityIssue", "EncodeResult", "EncoderCapabilities", "ExportOptions",
    "FrameError",
    "FrameSource", "HistoryEntry", "OutputDecision", "OutputService",
    "PLATFORM_KEYS", "PLATFORM_PRESETS", "PlatformPreset",
    "QCIssue", "QCReport", "QCService", "RenderEngine", "RenderPlanIssue",
    "RenderPlanSummary", "RenderProgress", "RenderRequest", "RenderResult",
    "RenderSegment", "RenderService", "SegmentPlan", "apply_platform",
    "burn_subtitles", "concat_and_mux", "detect_capabilities",
    "estimate_file_size", "estimate_render_time", "plan_segments",
    "platform_from_dict", "platform_preset", "platforms_from_project",
    "probe_detect", "rate_control_modes", "render_template", "sanitize_component",
    "sequence_from_name", "stream_encode", "validate_export_settings",
    "validate_plan", "video_encoder_args",
]
