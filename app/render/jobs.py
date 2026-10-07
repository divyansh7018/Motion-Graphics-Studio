"""Background job bodies for rendering (Stage E, sections 9, 32, 50).

Plain functions with the :class:`app.jobs.spec.JobContext` signature, so the
exact same code runs in the GUI's thread pool, from the CLI and in tests - and
never on the Qt thread.

Cancellation is wired all the way down: the job's cancel token is handed to the
engine, which hands it to FFmpeg, so cancelling kills the encoder rather than
leaving it running in the background (directive section 50).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..audio.service import AudioService
from ..core.logging_setup import get_logger
from ..jobs.keys import JobKeys
from ..jobs.spec import JobContext, JobSpec
from ..scene.timing import build_timeline
from ..subtitles.service import generate_cues, to_srt, to_vtt, write_subtitle_file
from ..tools.ffmpeg import FFmpegCancelToken, discover_ffmpeg
from .engine import COMPLETED
from .qc import QCService
from .service import RenderService

LOGGER = get_logger("render.jobs")

__all__ = [
    "render_body",
    "render_spec",
    "audio_mix_body",
    "audio_mix_spec",
    "audio_validate_body",
    "audio_validate_spec",
    "subtitle_build_body",
    "subtitle_build_spec",
    "subtitle_export_body",
    "subtitle_export_spec",
    "timeline_check_body",
    "timeline_check_spec",
    "capabilities_body",
    "capabilities_spec",
    "render_plan_body",
    "render_plan_spec",
    "qc_body",
    "qc_spec",
    "ffmpeg_cancel_token_for",
]


def _tools(context: JobContext) -> Any:
    """The FFmpeg tools, taken from the job context or discovered on demand."""
    tools = context.get("tools")
    if tools is not None:
        return tools
    from ..tools.ffmpeg import FFmpegTools

    return FFmpegTools(discover_ffmpeg())


def ffmpeg_cancel_token_for(context: JobContext) -> FFmpegCancelToken:
    """Bridge the job's cancel token to the FFmpeg process registry.

    The job system already terminates children it knows about; this makes sure
    the encoder is one of them, so a cancelled render leaves no FFmpeg running.
    """
    token = FFmpegCancelToken()
    job_token = getattr(context, "cancel", None)
    if job_token is not None:
        job_token.on_cancel(lambda reason="": token.cancel())
    return token


def _progress_bridge(context: JobContext):
    """Turn engine progress into job progress."""
    def report(progress: Any) -> None:
        reporter = context.progress
        fraction = max(0.0, min(1.0, float(progress.percent) / 100.0))
        if getattr(reporter.progress, "total", 0) != 1.0:
            reporter.start(total=1.0, message=progress.message, unit="frame")
        reporter.update(current=fraction, message=progress.message)
    return report


# --------------------------------------------------------------------------
# Final render
# --------------------------------------------------------------------------

def render_body(context: JobContext) -> dict:
    """Render the open project to a finished video file."""
    project = context.get("project")
    if project is None:
        raise ValueError("No project was supplied to the render job.")
    project_dir = context.get("project_dir")
    if project_dir is None:
        raise ValueError("The render job was not given a project folder.")

    service = RenderService(
        _tools(context), project_dir=Path(project_dir), paths=context.paths,
        cancel_token=ffmpeg_cancel_token_for(context),
        progress=_progress_bridge(context),
    )
    result = service.render(
        project,
        overrides=context.get("overrides") or None,
        include_audio=bool(context.get("include_audio", True)),
        include_subtitles=bool(context.get("include_subtitles", True)),
        burn_subtitles=context.get("burn_subtitles"),
        two_pass=bool(context.get("two_pass", False)),
        resume=bool(context.get("resume", True)),
        quick_qc=bool(context.get("quick_qc", False)),
        persist_settings=bool(context.get("persist_settings", False)),
    )

    if result.status == COMPLETED:
        return {
            "status": result.status,
            "path": str(result.path) if result.path else "",
            "message": result.message,
            "qc": result.qc.to_dict() if result.qc else None,
            "seconds": round(result.seconds, 2),
            "estimates": result.estimates,
            "details": result.details,
            "warnings": [item.to_dict() if hasattr(item, "to_dict") else str(item)
                         for item in result.warnings],
        }

    # A failure is returned, not raised: the job result carries what happened,
    # why and what to do, so the UI never has to invent a message.
    return {
        "status": result.status,
        "path": "",
        "message": result.message,
        "what_to_do": result.what_to_do,
        "technical": result.technical,
        "errors": [item.to_dict() if hasattr(item, "to_dict") else str(item)
                   for item in result.errors],
        "qc": result.qc.to_dict() if result.qc else None,
    }


def render_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.RENDER_FINAL,
        title="Render video",
        body=render_body,
        description="Rendering the finished video.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Audio preview (the master mix, without rendering any video)
# --------------------------------------------------------------------------

def audio_mix_body(context: JobContext) -> dict:
    """Mix the project's audio to a file the user can listen to."""
    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("The audio job needs a project and a project folder.")

    context.raise_if_cancelled()
    tools = _tools(context)
    service = AudioService(tools, project_dir=Path(project_dir))
    timeline = build_timeline(project.scenes)

    output = context.get("output")
    if output is None:
        paths = context.paths
        base = Path(paths.previews_dir) if paths is not None and hasattr(paths, "previews_dir") \
            else Path(project_dir) / "cache" / "previews"
        base.mkdir(parents=True, exist_ok=True)
        output = base / "audio_preview.wav"

    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Mixing the audio", unit="track")

    result = service.render_master(
        project, timeline, Path(output), cancel_token=ffmpeg_cancel_token_for(context),
        progress=lambda fraction, note: reporter.update(
            current=max(0.0, min(1.0, fraction)), message=note or "Mixing the audio"),
    )
    if not result.ok:
        return {"ok": False, "message": result.message,
                "issues": [issue.to_dict() for issue in result.issues]}
    return {"ok": True, "path": str(result.path), "duration": result.duration,
            "issues": [issue.to_dict() for issue in result.issues]}


def audio_mix_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.AUDIO_MIX,
        title="Mix audio preview",
        body=audio_mix_body,
        description="Mixing the project's audio tracks.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

def subtitle_build_body(context: JobContext) -> dict:
    """Generate captions from the narration and write the side-car files."""
    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("The subtitle job needs a project and a project folder.")

    context.raise_if_cancelled()
    timeline = build_timeline(project.scenes)
    cues, issues = generate_cues(project, timeline)
    project.subtitles.cues = cues

    output_dir = context.get("output_dir")
    written: list[str] = []
    if output_dir is not None and cues:
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = context.get("stem") or "subtitles"
        # The helper never replaces a caption file that already holds different
        # text, so the paths it returns are the ones that were really written
        # (directive sections 12, 36).
        srt = write_subtitle_file(folder / f"{stem}.srt", to_srt(cues))
        vtt = write_subtitle_file(folder / f"{stem}.vtt", to_vtt(cues))
        written = [str(srt), str(vtt)]

    return {
        "count": len(cues),
        "cues": [cue.to_dict() for cue in cues],
        "files": written,
        "issues": [issue.to_dict() for issue in issues],
    }


def subtitle_build_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.SUBTITLE_BUILD,
        title="Build subtitles",
        body=subtitle_build_body,
        description="Generating captions from the narration.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Quality check on an existing file
# --------------------------------------------------------------------------

def qc_body(context: JobContext) -> dict:
    """Run the quality checks on a finished file."""
    path = context.get("path")
    if not path:
        raise ValueError("The quality check needs the path of a finished video.")

    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Checking the video", unit="check")
    reporter.update(current=0.2, message="Reading the file")

    service = QCService(_tools(context), deep_checks=bool(context.get("deep", True)))
    report = service.check(
        Path(path),
        expected_duration=float(context.get("expected_duration", 0.0) or 0.0),
        expected_width=int(context.get("expected_width", 0) or 0),
        expected_height=int(context.get("expected_height", 0) or 0),
        expected_fps=float(context.get("expected_fps", 0.0) or 0.0),
        expect_audio=bool(context.get("expect_audio", True)),
    )
    reporter.update(current=1.0, message=report.summary())
    return report.to_dict()


def qc_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.QC_RUN,
        title="Quality check",
        body=qc_body,
        description="Checking the finished video.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


# --------------------------------------------------------------------------
# Timeline and audio checks (Stage E services, off the Qt thread)
# --------------------------------------------------------------------------

def timeline_check_body(context: JobContext) -> dict:
    """Build and validate the timeline - the same service the CLI uses."""
    from ..scene.service import TimelineService

    project = context.get("project")
    if project is None:
        raise ValueError("The timeline check needs an open project.")
    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Building the timeline", unit="scene")
    service = TimelineService(project_dir=context.get("project_dir") or None,
                              tools=_tools(context))
    report = service.check(project)
    reporter.update(current=1.0, message=report.summary())
    return report.to_dict()


def timeline_check_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.TIMELINE_CHECK,
        title="Check timeline",
        body=timeline_check_body,
        description="Checking the scene timings.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def audio_validate_body(context: JobContext) -> dict:
    """Check every audio track really exists and fits the timeline."""
    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("The audio check needs a project and a project folder.")

    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Checking the audio tracks", unit="track")

    service = AudioService(_tools(context), project_dir=Path(project_dir))
    timeline = build_timeline(project.scenes)
    validation = service.validate(project, timeline)
    reporter.update(current=1.0, message="Audio checked")
    return {
        "ok": validation.ok,
        "errors": [issue.to_dict() for issue in validation.errors],
        "warnings": [issue.to_dict() for issue in validation.warnings],
        "placements": [
            {"scene_id": item.scene_id, "scene_name": item.scene_name,
             "path": str(item.path), "start": round(item.start, 3),
             "duration": round(item.duration, 3), "end": round(item.end, 3)}
            for item in validation.placements
        ],
    }


def audio_validate_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.AUDIO_VALIDATE,
        title="Check audio",
        body=audio_validate_body,
        description="Checking the audio tracks.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def subtitle_export_body(context: JobContext) -> dict:
    """Write the caption side-car files next to the project."""
    from ..subtitles.service import SubtitleService

    project = context.get("project")
    output_dir = context.get("output_dir")
    if project is None or output_dir is None:
        raise ValueError("The subtitle export needs a project and an output folder.")

    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Writing the caption files", unit="file")

    service = SubtitleService(project_dir=context.get("project_dir") or None)
    written = service.export(project, Path(output_dir),
                             stem=context.get("stem") or "subtitles",
                             formats=tuple(context.get("formats") or ("srt", "vtt")))
    issues = service.validate(project)
    reporter.update(current=1.0, message=f"Wrote {len(written)} caption file(s)")
    return {"files": written, "count": len(service.cues(project)),
            "issues": [issue.to_dict() for issue in issues]}


def subtitle_export_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.SUBTITLE_EXPORT,
        title="Export subtitles",
        body=subtitle_export_body,
        description="Writing the caption files.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def capabilities_body(context: JobContext) -> dict:
    """Detect what the local FFmpeg can actually encode.

    Run as a job because it starts a subprocess: the Render page must never wait
    for FFmpeg on the Qt thread (directive section 32).
    """
    from .service import RenderService

    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("The capability check needs a project and a project folder.")

    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Checking FFmpeg", unit="codec")
    service = RenderService(_tools(context), project_dir=Path(project_dir),
                            paths=context.paths)
    options = service.export_options(project)
    reporter.update(current=1.0, message="FFmpeg checked")
    # A plain dict, like every other job result, so the UI never has to know
    # which module the object came from.
    return options.to_dict()


def capabilities_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.RENDER_CAPABILITIES,
        title="Check FFmpeg",
        body=capabilities_body,
        description="Checking which encoders FFmpeg has.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )


def render_plan_body(context: JobContext) -> dict:
    """Work out what a render would do, without drawing a frame.

    The UI shows this while the Render button is still enabled, so the length,
    the file name and the estimates are real before the user commits to a long
    encode (directive sections 30-31).  It runs as a job because planning reads
    the encoder list from FFmpeg.
    """
    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("The render plan needs a project and a project folder.")

    context.raise_if_cancelled()
    reporter = context.progress
    if getattr(reporter.progress, "total", 0) != 1.0:
        reporter.start(total=1.0, message="Working out the render plan", unit="step")

    service = RenderService(_tools(context), project_dir=Path(project_dir),
                            paths=context.paths)
    plan = service.plan(project, overrides=context.get("overrides") or None,
                        two_pass=bool(context.get("two_pass", False)),
                        include_audio=bool(context.get("include_audio", True)))
    reporter.update(current=1.0, message="Plan ready")
    return {
        "ready": plan.ready,
        "duration": round(plan.duration, 3),
        "frames": plan.frames,
        "fps": plan.fps,
        "segments": plan.segments,
        "transitions": plan.transitions,
        "resolution": plan.resolution,
        "quality": plan.quality,
        # OutputDecision.to_dict() already has the right fields; inventing a
        # second shape here would drift from the one the CLI prints.
        "output": plan.output.to_dict() if plan.output is not None else {},
        "size_estimate": plan.size_estimate,
        "time_estimate": plan.time_estimate,
        "errors": [issue.to_dict() if hasattr(issue, "to_dict") else str(issue)
                   for issue in plan.errors],
        "warnings": [issue.to_dict() if hasattr(issue, "to_dict") else str(issue)
                     for issue in plan.warnings],
    }


def render_plan_spec(context_payload: dict, **options: Any) -> JobSpec:
    return JobSpec(
        key=JobKeys.RENDER_PLAN,
        title="Plan the render",
        body=render_plan_body,
        description="Working out what the render will do.",
        allow_parallel=False,
        payload=dict(context_payload, **options),
    )
