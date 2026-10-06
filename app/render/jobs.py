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
    "subtitle_build_body",
    "subtitle_build_spec",
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
        srt = folder / f"{stem}.srt"
        vtt = folder / f"{stem}.vtt"
        write_subtitle_file(srt, to_srt(cues))
        write_subtitle_file(vtt, to_vtt(cues))
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
