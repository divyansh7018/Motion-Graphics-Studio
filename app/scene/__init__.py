"""The Stage D scene engine.

Public entry points, so callers never reach into submodules for the common
operations:

``build_context``
    Make a :class:`LayoutContext` from a project (canvas, theme, fonts).
``layout_scene``
    Turn a scene's elements into concrete pixels for one canvas.
``render_scene``
    Draw a laid-out scene to a Pillow image.
``validate_scene`` / ``validate_project_scenes``
    Report problems with wording a beginner can act on.

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
from .text import (
    FittedText,
    FitOptions,
    FontMatch,
    FontResolver,
    TextMetrics,
    fit_text,
    measure_text,
    missing_glyphs,
    wrap_text,
)

__all__ = [
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
    "TextMetrics",
    "anchor_point",
    "fit_text",
    "format_number",
    "layout_element",
    "layout_scene",
    "measure_text",
    "missing_glyphs",
    "mix",
    "normalise_size",
    "parse_color",
    "readable_on",
    "wrap_text",
    "with_alpha",
]
