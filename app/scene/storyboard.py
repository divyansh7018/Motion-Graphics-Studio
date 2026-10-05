"""Storyboard assembly (Stage D).

Two layers:

* **Data** - :func:`build_rows` produces the rows a storyboard shows (name,
  index, timing, narration state, issue counts) without drawing anything.  The
  UI uses it to render its list immediately.
* **Pixels** - :func:`render_thumbnail` and :func:`render_thumbnails` draw one
  or many scene thumbnails.  The plural form is what the storyboard job runs off
  the UI thread; it writes PNGs and reports progress, and can be cancelled
  between scenes.

Nothing here is cached in the project - thumbnails are disposable previews and
are rebuilt whenever the scene changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

from .canvas import Canvas
from .compose import render_scene
from .elements import LayoutContext, build_asset_paths
from .text import default_resolver
from .timing import Timeline, build_timeline
from .validate import validate_scene

__all__ = [
    "StoryboardRow",
    "build_asset_paths",
    "build_context",
    "build_rows",
    "render_thumbnail",
    "render_thumbnails",
    "preview_frame_path",
]


def build_context(project: Any, *, canvas: Optional[Canvas] = None,
                  project_dir: Optional[Path] = None,
                  font_family: str = "") -> LayoutContext:
    """A layout context for one project, ready to hand to the renderer."""
    canvas = canvas or Canvas.from_project(project)
    palette: dict = {}
    theme = getattr(project, "theme", None)
    colors = getattr(theme, "colors", None)
    if isinstance(colors, dict):
        palette.update(colors)
    background = getattr(getattr(project, "format", None), "background", "")
    if background:
        palette.setdefault("background", background)
    accent = getattr(getattr(project, "theme", None), "accent", "")
    if accent:
        palette.setdefault("accent", accent)

    resolver = default_resolver()

    def loader(family, size, bold, italic):
        return resolver.load(family or font_family, size=size, bold=bold, italic=italic)

    return LayoutContext(
        canvas=canvas,
        safe_area=canvas.safe_area(),
        palette=palette,
        font_loader=loader,
        font_family=font_family,
        asset_paths=build_asset_paths(project, project_dir),
    )


@dataclass
class StoryboardRow:
    """One storyboard entry: enough for the list, without any pixels."""

    scene_id: str = ""
    index: int = 0
    name: str = ""
    type: str = "blank"
    start: float = 0.0
    duration: float = 0.0
    duration_source: str = "default"
    narration_duration: float = 0.0
    element_count: int = 0
    transition_out: str = "none"
    thumbnail_path: str = ""
    issues: int = 0
    errors: int = 0

    @property
    def end(self) -> float:
        return self.start + self.duration

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "index": self.index,
            "name": self.name,
            "type": self.type,
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "duration_source": self.duration_source,
            "element_count": self.element_count,
            "transition_out": self.transition_out,
            "thumbnail_path": self.thumbnail_path,
            "issues": self.issues,
            "errors": self.errors,
        }


def build_rows(project: Any, *, timeline: Optional[Timeline] = None,
               ctx: Optional[LayoutContext] = None,
               validate: bool = False) -> tuple[list, Timeline]:
    """The storyboard's rows plus the timeline they were built from."""
    scenes = getattr(project, "scenes", []) or []
    timeline = timeline or build_timeline(scenes)
    rows: list = []
    for timing in timeline.timings:
        scene = next((s for s in scenes if s.id == timing.scene_id), None)
        row = StoryboardRow(
            scene_id=timing.scene_id,
            index=timing.index,
            name=timing.name or (scene.name if scene else "") or f"Scene {timing.index + 1}",
            type=getattr(scene, "type", "blank") if scene else "blank",
            start=timing.start,
            duration=timing.duration,
            duration_source=timing.source,
            narration_duration=timing.narration_duration,
            element_count=len(getattr(scene, "elements", []) or []) if scene else 0,
            transition_out=timing.transition_out,
        )
        if validate and scene is not None:
            context = ctx or build_context(project)
            result = validate_scene(scene, canvas=context.canvas, ctx=context, project=project)
            row.issues = len(result.warnings)
            row.errors = len(result.errors)
        rows.append(row)
    return rows, timeline


def render_thumbnail(scene: Any, ctx: LayoutContext, *, long_edge: int = 480,
                     time: Optional[float] = None) -> Image.Image:
    """One scene's frame at thumbnail size, at a settled animation time."""
    canvas = ctx.canvas.thumbnail(long_edge)
    small_ctx = LayoutContext(
        canvas=canvas,
        safe_area=canvas.safe_area(),
        palette=dict(ctx.palette),
        font_loader=ctx.font_loader,
        font_family=ctx.font_family,
        asset_paths=dict(ctx.asset_paths),
    )
    moment = time if time is not None else _settled_time(scene)
    return render_scene(scene, small_ctx, time=moment, scene_duration=_scene_duration(scene))


def _settled_time(scene: Any) -> float:
    """A time by which enter animations are done, so thumbnails show content."""
    return 1.5


def _scene_duration(scene: Any) -> float:
    narration = getattr(scene, "narration", None)
    measured = float(getattr(narration, "duration", 0.0) or 0.0)
    manual = float(getattr(scene, "duration", 3.0) or 3.0)
    return max(measured, manual, 1.0)


def render_thumbnails(project: Any, out_dir: Path, *, ctx: Optional[LayoutContext] = None,
                      long_edge: int = 480, progress: Optional[Callable[[float, str], None]] = None,
                      is_cancelled: Optional[Callable[[], bool]] = None) -> list:
    """Render every scene's thumbnail to ``out_dir``; returns the row list.

    Runs on a worker thread.  Checks ``is_cancelled`` between scenes and never
    leaves a half-written file: each thumbnail is written atomically.
    """
    from .compose import save_image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    context = ctx or build_context(project)
    rows, timeline = build_rows(project, timeline=None, ctx=context)

    produced: list = []
    total = max(1, len(rows))
    for done, row in enumerate(rows):
        if is_cancelled is not None and is_cancelled():
            break
        scene = next((s for s in (getattr(project, "scenes", []) or [])
                      if s.id == row.scene_id), None)
        if scene is None:
            continue
        image = render_thumbnail(scene, context, long_edge=long_edge)
        path = out_dir / f"scene-{done:03d}.png"
        save_image(image, path, overwrite=True)
        row.thumbnail_path = str(path)
        produced.append(row)
        if progress is not None:
            progress((done + 1) / total, f"Scene {done + 1} of {total}")

    return produced


def preview_frame_path(previews_dir: Path, project_name: str, scene_id: str,
                       time: float) -> Path:
    """A stable, deterministic filename for one preview frame."""
    safe_name = "".join(ch for ch in (project_name or "project") if ch.isalnum() or ch in "-_") or "project"
    return Path(previews_dir) / f"{safe_name}-{scene_id}-{int(round(time * 1000))}.png"
