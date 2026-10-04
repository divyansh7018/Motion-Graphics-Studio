"""Project thumbnails without rendering (directive sections 17, 37).

A thumbnail is a small drawing of the project's *intent*: background colour,
accent, project name and the first scene's text.  It is generated with Pillow in
a fraction of a second, so a browser with hundreds of projects stays
responsive - and it never needs FFmpeg, a scene render or a source video.

Thumbnails are cache: they live in the application's cache folder from Stage A
(``cache/thumbnails``), never inside the project, and can be deleted at any
time.  Loading source images is deliberately avoided - the whole point is that a
big media library must not slow the dashboard down.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..core.atomicio import atomic_write_bytes
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from .layout import ProjectLayout
from .model import Project

LOGGER = get_logger("project.thumbnails")

THUMBNAIL_SIZE = (320, 180)
CACHE_SUBFOLDER = "thumbnails"


def thumbnail_cache_dir(paths) -> Path:
    """Where thumbnails are cached (regenerable, outside the project)."""
    return Path(paths.cache_dir) / CACHE_SUBFOLDER


def thumbnail_path(paths, project_id: str) -> Path:
    safe = "".join(ch for ch in str(project_id) if ch.isalnum() or ch in "-_") or "project"
    return thumbnail_cache_dir(paths) / f"{safe}.png"


def is_fresh(path: Path, newer_than: float) -> bool:
    """True when a cached thumbnail is newer than the project file."""
    try:
        return path.exists() and path.stat().st_mtime >= newer_than
    except OSError:
        return False


def _parse_color(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) not in (6, 8):
        return fallback
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return fallback


def generate_thumbnail(
    project: Project,
    target: Path,
    size: tuple[int, int] = THUMBNAIL_SIZE,
    layout: Optional[ProjectLayout] = None,
) -> Optional[Path]:
    """Draw and cache a thumbnail.  Returns the path, or ``None`` if impossible.

    Failing here is never fatal: a project without a thumbnail simply shows its
    initials, and the reason is logged once.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - Pillow is a core requirement
        log_event(
            Event.WARNING,
            "Thumbnails need Pillow, which is not installed",
            level=logging.WARNING,
            logger=LOGGER,
        )
        return None

    width, height = size
    background = _parse_color(project.theme.background or project.format.background, (16, 16, 20))
    accent = _parse_color(project.theme.accent, (76, 141, 255))
    text_color = _parse_color(project.theme.color("text", "#ffffff"), (240, 240, 240))

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    # A frame that matches the project's own aspect ratio, so a 9:16 project
    # looks like a 9:16 project in the browser.
    project_w = max(1, int(project.format.width or 16))
    project_h = max(1, int(project.format.height or 9))
    scale = min((width - 24) / project_w, (height - 24) / project_h)
    frame_w = max(8, int(project_w * scale))
    frame_h = max(8, int(project_h * scale))
    left = (width - frame_w) // 2
    top = (height - frame_h) // 2
    draw.rectangle([left, top, left + frame_w, top + frame_h], outline=accent, width=2)

    headline = (project.scenes[0].script if project.scenes else "") or project.project.name or "Untitled"
    _draw_wrapped(draw, headline.strip(), (left + 10, top + 10), frame_w - 20, text_color)

    footer = f"{project.format.width}x{project.format.height} - {len(project.scenes)} scene(s)"
    draw.text((8, height - 18), footer, fill=accent)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        atomic_write_bytes(Path(target), buffer.getvalue())
    except (OSError, ValueError) as exc:
        log_event(
            Event.WARNING,
            "A project thumbnail could not be written",
            level=logging.WARNING,
            logger=LOGGER,
            path=str(target),
            reason=str(exc),
        )
        return None
    return Path(target)


def _draw_wrapped(draw, text: str, origin: tuple[int, int], max_width: int, color) -> None:
    """Draw *text* wrapped to *max_width*, using Pillow's default bitmap font."""
    if not text:
        return
    x, y = origin
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate) <= max_width or not line:
            line = candidate
        else:
            draw.text((x, y), line, fill=color)
            y += 14
            line = word
        if y > origin[1] + 60:
            break
    if line:
        draw.text((x, y), line, fill=color)


def refresh_thumbnail(paths, project: Project, layout: Optional[ProjectLayout] = None) -> Optional[Path]:
    """Regenerate a project's cached thumbnail."""
    target = thumbnail_path(paths, project.project.id or project.project.name)
    return generate_thumbnail(project, target, layout=layout)


def clear_thumbnail_cache(paths) -> int:
    """Delete cached thumbnails.  Returns how many were removed."""
    directory = thumbnail_cache_dir(paths)
    if not directory.is_dir():
        return 0
    removed = 0
    for item in directory.iterdir():
        if item.is_file() and item.suffix == ".png":
            try:
                item.unlink()
                removed += 1
            except OSError:
                continue
    if removed:
        log_event(Event.CACHE_CLEARED, "Project thumbnails cleared", logger=LOGGER, count=removed)
    return removed


__all__ = [
    "CACHE_SUBFOLDER",
    "THUMBNAIL_SIZE",
    "clear_thumbnail_cache",
    "generate_thumbnail",
    "is_fresh",
    "refresh_thumbnail",
    "thumbnail_cache_dir",
    "thumbnail_path",
]
