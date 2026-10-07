"""The render service - one entry point for the GUI, the CLI and the tests.

Nothing here talks to Qt.  A widget calls a method and gets a plain result back;
the same method is what ``motion-studio render`` calls, which is how a render
started from a button and one started from a terminal are guaranteed to be the
same render (directive sections 12, 68).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..core.logging_setup import log_event
from ..scene.timing import build_timeline
from .capabilities import (
    EncoderCapabilities,
    detect_capabilities,
    estimate_file_size,
    estimate_render_time,
)
from .engine import RenderEngine, RenderProgress, RenderRequest, RenderResult
from .output import OutputDecision, OutputService, export_settings
from .platform import PlatformPreset, apply_platform, platform_preset, platforms_from_project
from .segments import plan_segments

__all__ = ["RenderService", "RenderPlanSummary", "ExportOptions"]


@dataclass
class ExportOptions:
    """The choices the export dialog offers, all read from the real machine."""

    caps: EncoderCapabilities = field(default_factory=EncoderCapabilities)
    platforms: list = field(default_factory=list)
    #: Container -> the codecs actually available for it.
    codecs_by_container: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    def codecs_for(self, container: str) -> list[str]:
        return list(self.codecs_by_container.get(str(container), []))

    def to_dict(self) -> dict:
        return {"capabilities": self.caps.to_dict(),
                "platforms": [preset.to_dict() for preset in self.platforms],
                "codecs_by_container": self.codecs_by_container,
                "problems": [str(item) for item in self.problems]}


@dataclass
class RenderPlanSummary:
    """What a render will do, worked out before it starts.

    This is what the UI shows while the button is still enabled, so the user can
    see the length, the file name and the estimates before committing to a long
    encode (directive sections 30, 31).
    """

    duration: float = 0.0
    frames: int = 0
    fps: int = 30
    segments: int = 0
    transitions: int = 0
    resolution: str = ""
    quality: str = ""
    output: Optional[OutputDecision] = None
    size_estimate: dict = field(default_factory=dict)
    time_estimate: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    #: True when nothing blocks the render.
    ready: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.errors)

    def describe(self) -> str:
        parts = [f"{self.duration:.2f}s", f"{self.frames} frames @ {self.fps} fps",
                 self.resolution, self.quality]
        if self.output is not None:
            parts.append(self.output.filename)
        if self.size_estimate:
            parts.append(self.size_estimate.get("label", ""))
        return " | ".join(part for part in parts if part)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3), "frames": self.frames,
            "fps": self.fps, "segments": self.segments,
            "transitions": self.transitions, "resolution": self.resolution,
            "quality": self.quality,
            "output": self.output.to_dict() if self.output else None,
            "size_estimate": self.size_estimate,
            "time_estimate": self.time_estimate,
            "errors": [_as_dict(item) for item in self.errors],
            "warnings": [_as_dict(item) for item in self.warnings],
            "ready": self.ready,
        }


class RenderService:
    """Everything the outside world needs in order to render."""

    def __init__(self, tools: Any, *, project_dir: Path, paths: Any = None,
                 cancel_token: Any = None,
                 progress: Optional[Callable[[RenderProgress], None]] = None,
                 caps: Optional[EncoderCapabilities] = None) -> None:
        self.tools = tools
        self.project_dir = Path(project_dir)
        self.paths = paths
        self.cancel_token = cancel_token
        self._progress = progress
        self._caps = caps
        self.output = OutputService(self.project_dir)

    # -- engine ----------------------------------------------------------

    def engine(self, *, cancel_token: Any = None,
               progress: Optional[Callable[[RenderProgress], None]] = None) -> RenderEngine:
        """A fresh engine.  One engine per render, so state cannot leak."""
        return RenderEngine(
            self.tools, project_dir=self.project_dir, paths=self.paths,
            cancel_token=cancel_token or self.cancel_token,
            progress=progress or self._progress, caps=self.capabilities(),
        )

    # -- capabilities ----------------------------------------------------

    def capabilities(self, *, refresh: bool = False) -> EncoderCapabilities:
        if self._caps is None or refresh:
            self._caps = detect_capabilities(self.tools)
            log_event("RENDER_CAPABILITIES", f"FFmpeg reports {len(self._caps.encoders)} video encoder(s)",
                      version=self._caps.ffmpeg_version,
                      h264=self._caps.supports_codec("h264_cpu"))
        return self._caps

    def export_options(self, project: Any) -> ExportOptions:
        """What the export dialog may offer, given this machine and project."""
        caps = self.capabilities()
        options = ExportOptions(caps=caps, platforms=platforms_from_project(project))
        containers = ("mp4", "mkv", "mov", "webm")
        for container in containers:
            available = caps.available_codecs(container)
            options.codecs_by_container[container] = available
            if not available:
                options.problems.append(
                    f"No supported video encoder is available for .{container} files in "
                    f"this FFmpeg installation."
                )
        if not caps.detected:
            options.problems.append(
                "FFmpeg was not found, so the available encoders could not be checked. "
                "Install FFmpeg before rendering."
            )
        return options

    # -- planning --------------------------------------------------------

    def plan(self, project: Any, *, overrides: Optional[dict] = None,
             two_pass: bool = False,
             include_audio: bool = True) -> RenderPlanSummary:
        """Work out what a render would do, without rendering anything."""
        settings = project.format
        if overrides:
            for key, value in dict(overrides).items():
                if hasattr(settings, key):
                    setattr(settings, key, value)

        timeline = build_timeline(project.scenes)
        fps = max(1, int(settings.fps))
        segment_plan = plan_segments(timeline, fps=fps)
        engine = self.engine()
        errors, warnings = engine.validate(project, duration=timeline.total_duration,
                                           two_pass=two_pass,
                                           include_audio=include_audio)
        for issue in segment_plan.issues:
            (errors if issue.severity == "error" else warnings).append(issue)

        decision = self.output.decide(
            export_settings(project), project_name=_project_name(project),
            quality=str(settings.quality_preset),
            resolution=f"{settings.width}x{settings.height}")

        summary = RenderPlanSummary(
            duration=float(timeline.total_duration),
            frames=segment_plan.total_frames, fps=fps,
            segments=len(segment_plan.segments),
            transitions=sum(1 for segment in segment_plan.segments if segment.is_transition),
            resolution=f"{settings.width}x{settings.height}",
            quality=str(settings.quality_preset),
            output=decision, errors=errors, warnings=warnings,
            size_estimate=estimate_file_size(settings, timeline.total_duration),
            time_estimate=estimate_render_time(
                frames=segment_plan.total_frames, width=settings.width,
                height=settings.height, fps=fps,
                encoder_preset=settings.encoder_preset),
        )
        summary.ready = not errors
        return summary

    def validate(self, project: Any, *, two_pass: bool = False,
                 include_audio: bool = True) -> tuple[list, list]:
        """Blocking problems and warnings, without planning a whole render."""
        timeline = build_timeline(project.scenes)
        return self.engine().validate(project, duration=timeline.total_duration,
                                      two_pass=two_pass,
                                      include_audio=include_audio)

    # -- rendering -------------------------------------------------------

    def render(self, project: Any, *, overrides: Optional[dict] = None,
               include_audio: bool = True, include_subtitles: bool = True,
               burn_subtitles: Optional[bool] = None, two_pass: bool = False,
               resume: bool = True, quick_qc: bool = False,
               persist_settings: bool = False,
               progress: Optional[Callable[[RenderProgress], None]] = None,
               cancel_token: Any = None) -> RenderResult:
        """Render the project.  Returns a result; never raises for a known failure."""
        request = RenderRequest(
            project=project, overrides=dict(overrides or {}),
            include_audio=include_audio, include_subtitles=include_subtitles,
            burn_subtitles=burn_subtitles, two_pass=two_pass, resume=resume,
            quick_qc=quick_qc, persist_settings=persist_settings,
        )
        engine = self.engine(cancel_token=cancel_token, progress=progress)
        return engine.render(request)

    # -- platform presets ------------------------------------------------

    def platforms(self, project: Any) -> list[PlatformPreset]:
        return platforms_from_project(project)

    def apply_platform(self, project: Any, key: str) -> dict:
        """Apply a platform preset.  Returns what changed."""
        preset = platform_preset(key)
        if preset is None:
            raise ValueError(f"There is no export preset called '{key}'.")
        return apply_platform(project, preset)

    def update_platform(self, project: Any, key: str, **changes: Any) -> None:
        """Store the user's edit to a preset inside the project file."""
        presets = self.platforms(project)
        for preset in presets:
            if preset.key == key:
                for name, value in dict(changes).items():
                    if hasattr(preset, name):
                        setattr(preset, name, value)
                preset.edited = True
                break
        export = getattr(project, "export", None)
        if export is not None:
            export.extra["platform_presets"] = [preset.to_dict() for preset in presets]

    # -- history ---------------------------------------------------------

    def history(self) -> list:
        return self.output.history()


def _project_name(project: Any) -> str:
    meta = getattr(project, "project", None)
    return str(getattr(meta, "name", "") or "").strip() or "Video"


def _as_dict(issue: Any) -> dict:
    if hasattr(issue, "to_dict"):
        try:
            return issue.to_dict()
        except Exception:  # noqa: BLE001 - a report must never crash
            pass
    return {"code": str(getattr(issue, "code", "")),
            "message": str(getattr(issue, "message", "")),
            "what_to_do": str(getattr(issue, "what_to_do", "")),
            "severity": str(getattr(issue, "severity", "warning"))}
