"""The Stage D scene engine.

Public entry points, so callers never reach into submodules for the common
operations:

``build_context``
    Make a :class:`LayoutContext` from a project (canvas, theme, fonts, assets).
``layout_scene``
    Turn a scene's elements into concrete pixels for one canvas.
``render_scene``
    Draw a laid-out scene to a Pillow image.
``validate_scene`` / ``validate_project_scenes``
    Report problems with wording a beginner can act on.
``build_timeline``
    Work out when each scene runs (narration wins).
``TimelineService``
    The Stage E entry point: build that one timeline and validate it, so the
    preview, audio, subtitles and final render cannot disagree.

Everything here is CPU-only, uses ``pathlib`` and has no import-time side
effects: importing this package does not scan fonts, touch the disk or start a
thread.
"""

from __future__ import annotations

from .canvas import (
    ANCHOR_POINTS,
    MAX_CANVAS_DIMENSION,
    MIN_CANVAS_DIMENSION,
    Canvas,
    PixelRect,
    Rect,
    SafeArea,
    anchor_point,
    normalise_size,
)
from .compose import compose_scene, render_background, render_scene, save_image
from .service import (
    ERROR as TIMELINE_ERROR,
    WARNING as TIMELINE_WARNING,
    TimelineIssue,
    TimelineReport,
    TimelineService,
    describe_timeline,
)
from .elements import (
    CHART_KINDS,
    ELEMENT_KINDS,
    IMAGE_FIT_MODES,
    SHAPE_KINDS,
    ElementIssue,
    LayoutContext,
    ResolvedElement,
    SceneLayout,
    format_number,
    layout_element,
    layout_scene,
)
from .palette import Color, Gradient, mix, parse_color, readable_on, with_alpha
from .storyboard import build_context, build_rows, render_thumbnail, render_thumbnails
from .templates import (
    create_scene_from_template,
    default_templates_registered,
    template_keys,
    template_summaries,
)
from .text import (
    FittedText,
    FitOptions,
    FontMatch,
    FontResolver,
    TextMetrics,
    default_resolver,
    fit_text,
    measure_text,
    missing_glyphs,
    wrap_text,
)
from .timing import Timeline, build_timeline, format_duration
from .validate import SceneValidation, validate_project_scenes, validate_scene

__all__ = [
    "TIMELINE_ERROR",
    "TIMELINE_WARNING",
    "TimelineIssue",
    "TimelineReport",
    "TimelineService",
    "describe_timeline",
    "ANCHOR_POINTS",
    "CHART_KINDS",
    "ELEMENT_KINDS",
    "IMAGE_FIT_MODES",
    "MAX_CANVAS_DIMENSION",
    "MIN_CANVAS_DIMENSION",
    "SHAPE_KINDS",
    "Canvas",
    "Color",
    "ElementIssue",
    "FittedText",
    "FitOptions",
    "FontMatch",
    "FontResolver",
    "Gradient",
    "LayoutContext",
    "PixelRect",
    "Rect",
    "ResolvedElement",
    "SafeArea",
    "SceneLayout",
    "SceneValidation",
    "TextMetrics",
    "Timeline",
    "anchor_point",
    "build_context",
    "build_rows",
    "build_timeline",
    "compose_scene",
    "create_scene_from_template",
    "default_resolver",
    "default_templates_registered",
    "fit_text",
    "format_duration",
    "format_number",
    "layout_element",
    "layout_scene",
    "measure_text",
    "missing_glyphs",
    "mix",
    "normalise_size",
    "parse_color",
    "readable_on",
    "render_background",
    "render_scene",
    "render_thumbnail",
    "render_thumbnails",
    "save_image",
    "template_keys",
    "template_summaries",
    "validate_project_scenes",
    "validate_scene",
    "with_alpha",
    "wrap_text",
]
