"""Background job bodies for the scene engine (Stage D).

These are plain functions with the :class:`app.jobs.spec.JobContext` signature,
so the exact same code runs in the GUI thread pool, from the CLI and in tests -
and never on the Qt thread (directive: heavy preview/render off the UI thread).

Both jobs write only into the application's preview folder, never into the
project, so previewing cannot create, modify or clobber any project file.
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging_setup import get_logger
from app.jobs.keys import JobKeys
from app.jobs.spec import JobContext, JobSpec

from .canvas import Canvas
from .compose import render_scene, save_image
from .storyboard import build_context, preview_frame_path, render_thumbnails

LOGGER = get_logger("scene.jobs")


def _previews_dir(context: JobContext) -> Path:
    """Where previews are written.  The GUI and CLI both supply a folder."""
    explicit = context.get("previews_dir")
    if explicit:
        return Path(explicit)
    paths = context.paths
    if paths is not None and hasattr(paths, "previews_dir"):
        return Path(paths.previews_dir)
    raise ValueError("The scene job was not given a preview folder.")


def _project_and_dir(context: JobContext):
    return context.get("project"), context.get("project_dir")


def report_progress(context: JobContext, fraction: float, message: str) -> None:
    progress = context.progress
    if getattr(progress.progress, "total", 0) != 1.0:
        progress.start(total=1.0, message=message, unit="scene")
    progress.update(current=min(1.0, max(0.0, float(fraction))), message=message)


# --------------------------------------------------------------------------
# Storyboard (thumbnails for every scene)
# --------------------------------------------------------------------------

def storyboard_body(context: JobContext) -> dict:
    """Render a thumbnail for each scene into the preview folder."""
    project, project_dir = _project_and_dir(context)
    if project is None:
        raise ValueError("No project was supplied to the storyboard job.")

    context.raise_if_cancelled()
    long_edge = int(context.get("long_edge", 480))
    canvas = context.get("canvas")
    ctx = build_context(project, canvas=canvas, project_dir=project_dir)

    report_progress(context, 0.0, "Starting storyboard")
    rows = render_thumbnails(
        project,
        _previews_dir(context) / "storyboard",
        ctx=ctx,
        long_edge=long_edge,
        progress=lambda fraction, message: report_progress(context, fraction, message),
        is_cancelled=context.is_cancelled,
    )
    report_progress(context, 1.0, "Storyboard complete")
    LOGGER.info("Storyboard rendered %d scene(s)", len(rows))
    return {
        "rows": [row.to_dict() for row in rows],
        "count": len(rows),
    }


def storyboard_spec(context_payload: dict, *, long_edge: int = 480) -> JobSpec:
    return JobSpec(
        key=JobKeys.STORYBOARD_RENDER,
        title="Render storyboard",
        body=storyboard_body,
        description="Rendering a thumbnail for every scene.",
        allow_parallel=False,
        payload=dict(context_payload, long_edge=long_edge),
    )


# --------------------------------------------------------------------------
# Scene preview (one frame, full resolution)
# --------------------------------------------------------------------------

def scene_preview_body(context: JobContext) -> dict:
    """Render one frame of one scene at full resolution into the preview folder."""
    project, project_dir = _project_and_dir(context)
    scene_id = context.get("scene_id", "")
    if project is None:
        raise ValueError("No project was supplied to the preview job.")

    scene = next((s for s in (getattr(project, "scenes", []) or []) if s.id == scene_id), None)
    if scene is None:
        raise ValueError(f"No scene '{scene_id}' in the project.")

    context.raise_if_cancelled()
    canvas = context.get("canvas") or Canvas.from_project(project)
    ctx = build_context(project, canvas=canvas, project_dir=project_dir)

    time_value = float(context.get("time", 1.5))
    narration = getattr(scene, "narration", None)
    duration = max(float(getattr(narration, "duration", 0.0) or 0.0),
                   float(getattr(scene, "duration", 3.0) or 3.0), 1.0)

    report_progress(context, 0.2, "Laying out scene")
    image = render_scene(scene, ctx, time=time_value, scene_duration=duration)
    context.raise_if_cancelled()
    report_progress(context, 0.8, "Writing preview")

    destination = preview_frame_path(_previews_dir(context), getattr(project, "name", ""), scene_id, time_value)
    save_image(image, destination, overwrite=True)

    report_progress(context, 1.0, "Preview ready")
    return {
        "path": str(destination),
        "scene_id": scene_id,
        "time": time_value,
        "width": canvas.width,
        "height": canvas.height,
    }


def scene_preview_spec(context_payload: dict, *, scene_id: str, time: float = 1.5) -> JobSpec:
    return JobSpec(
        key=JobKeys.SCENE_PREVIEW,
        title="Render scene preview",
        body=scene_preview_body,
        description="Rendering one frame of the selected scene.",
        allow_parallel=False,
        payload=dict(context_payload, scene_id=scene_id, time=time),
    )
