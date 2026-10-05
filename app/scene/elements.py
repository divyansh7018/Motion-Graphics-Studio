"""The visual element model and the responsive layout pass (Stage D).

A scene is a list of :class:`app.project.model.ElementSpec` objects.  Those
store *intent* in normalised units; this module turns intent into concrete
pixels for one :class:`~app.scene.canvas.Canvas`.  Nothing is written back to
the project - layout is a pure function of (element, canvas, theme), so the
same project previews identically at 720p, 1080p and 4K.

Sizing rules (documented because they are the whole point of the system):

``fraction``
    A fraction of the frame's width (horizontal) or height (vertical).
``design``
    A fraction of the frame's **reference dimension** (``min(width, height)``).
    This is the responsive unit: a title at ``0.1`` keeps its relationship to
    the narrow edge whether the video is landscape, portrait or square.
``pixels``
    An explicit pixel size.  Honoured exactly, which is why it is opt-in.
``fill``
    Grow to the safe area on that axis.
``auto``
    Driven by the content (text block height, image aspect ratio).

For **text** elements the legacy Stage B form ``{"mode": "relative",
"value": 0.12}`` is read as a *font size* in design units, not a box width -
that is what the value meant when Stage B stored it, and reading it any other
way would silently resize every existing project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..project.model import ElementSpec
from .canvas import Canvas, PixelRect, Rect, SafeArea, anchor_point, normalise_size
from .palette import Color, Gradient, parse_color
from .text import FitOptions, fit_text

__all__ = [
    "ELEMENT_KINDS",
    "SHAPE_KINDS",
    "IMAGE_FIT_MODES",
    "CHART_KINDS",
    "TEXT_ALIGNMENTS",
    "DEFAULT_TEXT_SIZE",
    "ElementIssue",
    "ResolvedElement",
    "LayoutContext",
    "layout_element",
    "layout_scene",
    "SceneLayout",
]

#: Kinds of visual element the engine can draw.
ELEMENT_KINDS: tuple[str, ...] = ("text", "image", "shape", "card", "number", "chart", "divider")

#: Shapes a ``shape`` element can draw.
SHAPE_KINDS: tuple[str, ...] = (
    "rect", "rounded", "ellipse", "circle", "line", "triangle", "pill", "badge", "arrow", "frame",
)

#: How an image is placed inside its box.
IMAGE_FIT_MODES: tuple[str, ...] = ("cover", "contain", "fill", "original")

#: Chart types the engine can draw.
CHART_KINDS: tuple[str, ...] = ("bar", "column", "line", "area", "pie", "donut", "sparkline")

TEXT_ALIGNMENTS: tuple[str, ...] = ("left", "center", "right")

#: Default text size when an element does not say: 12% of the reference edge.
DEFAULT_TEXT_SIZE = 0.12


def build_asset_paths(project: Any, project_dir: Optional[Path]) -> dict:
    """Absolute path per asset id, from the project's own asset list.

    Kept here (not in :mod:`app.scene.storyboard`) so validation can use it
    without a circular import.
    """
    paths: dict = {}
    if project is None or project_dir is None:
        return paths
    for asset in getattr(project, "assets", []) or []:
        try:
            resolved = asset.resolve(Path(project_dir))
        except Exception:
            continue
        if asset.id:
            paths[asset.id] = resolved
    return paths


@dataclass
class ElementIssue:
    """Something worth telling the user about, found while laying out."""

    element_id: str
    code: str
    message: str
    what_to_do: str = ""
    severity: str = "warning"  # "warning" | "error" | "info"

    def to_dict(self) -> dict:
        return {
            "element_id": self.element_id,
            "code": self.code,
            "message": self.message,
            "what_to_do": self.what_to_do,
            "severity": self.severity,
        }


@dataclass
class LayoutContext:
    """Everything the layout pass needs.  Built once per preview or render."""

    canvas: Canvas
    safe_area: SafeArea = field(default_factory=SafeArea)
    #: Theme colours by role, resolved from the project theme.
    palette: dict = field(default_factory=dict)
    #: Callable ``(family, size, bold, italic) -> (font, match, note)``.
    font_loader: Any = None
    font_family: str = ""
    #: Directory the project's assets live in (for image elements).
    asset_root: Optional[Path] = None
    #: Resolved absolute path per asset id, built from the project's asset list.
    #: This is the trustworthy lookup; ``asset_root`` is only a fallback.
    asset_paths: dict = field(default_factory=dict)
    #: Multiplier applied to design units.  Used to render thumbnails cheaply.
    scale: float = 1.0

    @property
    def reference(self) -> float:
        return self.canvas.reference_dimension * max(0.0001, self.scale)

    def color(self, value: Any, role: str = "") -> Color:
        """A colour from an explicit value, falling back to a theme role."""
        if value not in (None, "", "auto", "theme"):
            return parse_color(value, self.palette.get(role))
        theme_value = self.palette.get(role)
        if theme_value is None:
            return parse_color(value if value not in (None, "auto", "theme") else "#ffffff")
        return parse_color(theme_value)

    def font(self, size: float, *, bold: bool = False, italic: bool = False):
        """Load a font, degrading to the bundled one if none is installed."""
        pixels = max(1, int(round(size)))
        if self.font_loader is None:
            from PIL import ImageFont

            try:
                return ImageFont.load_default(size=pixels), None, ""
            except TypeError:
                return ImageFont.load_default(), None, ""
        return self.font_loader(self.font_family, pixels, bold, italic)


@dataclass
class ResolvedElement:
    """One element laid out for one canvas.  Pure output - nothing is saved."""

    element_id: str = ""
    kind: str = "text"
    #: Final pixel box, clipped to the frame.
    rect: PixelRect = field(default_factory=PixelRect)
    #: The same box in normalised units (handy for validation messages).
    norm_rect: Rect = field(default_factory=Rect)
    # -- shared style ----------------------------------------------------
    opacity: float = 1.0
    color: Color = field(default_factory=lambda: Color(255, 255, 255, 255))
    background: Optional[Color] = None
    gradient: Optional[Gradient] = None
    radius: int = 0
    border_width: int = 0
    border_color: Optional[Color] = None
    padding: tuple = (0, 0, 0, 0)  # left, top, right, bottom in pixels
    align: str = "left"
    # -- text ------------------------------------------------------------
    font_size: float = 0.0
    lines: list = field(default_factory=list)
    text_metrics: Any = None
    bold: bool = False
    italic: bool = False
    line_spacing: float = 1.2
    overflow: bool = False
    overflow_reason: str = ""
    #: Characters the resolved font could not draw (best effort).
    unsupported_characters: list = field(default_factory=list)
    #: Note when a substitute font was used.
    font_note: str = ""
    # -- image -----------------------------------------------------------
    image_path: Optional[Path] = None
    image_fit: str = "cover"
    image_size: tuple = (0, 0)
    image_box: Optional[PixelRect] = None
    # -- shape -----------------------------------------------------------
    shape: str = "rect"
    rotation: float = 0.0
    # -- number ----------------------------------------------------------
    number_text: str = ""
    unit_text: str = ""
    label_text: str = ""
    #: Secondary caption under a number, laid out separately and smaller.
    label_lines: list = field(default_factory=list)
    label_font_size: float = 0.0
    # -- chart -----------------------------------------------------------
    chart_kind: str = "bar"
    chart_series: list = field(default_factory=list)
    chart_labels: list = field(default_factory=list)
    chart_colors: list = field(default_factory=list)
    # -- card ------------------------------------------------------------
    children: list = field(default_factory=list)
    # -- bookkeeping -----------------------------------------------------
    z: int = 0
    visible: bool = True
    issues: list = field(default_factory=list)
    #: The spec this came from, so animation can read the element's tracks.
    spec: Any = None

    @property
    def has_content(self) -> bool:
        if self.kind == "image":
            return self.image_path is not None
        if self.kind == "chart":
            return bool(self.chart_series)
        return bool(self.lines or self.number_text or self.shape != "none")

    def to_dict(self) -> dict:
        """A JSON-safe snapshot (used by validation reports and the CLI)."""
        return {
            "element_id": self.element_id,
            "kind": self.kind,
            "rect": self.rect.to_dict(),
            "font_size": round(self.font_size, 2),
            "lines": len(self.lines),
            "overflow": self.overflow,
            "overflow_reason": self.overflow_reason,
            "opacity": round(self.opacity, 3),
            "visible": self.visible,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class SceneLayout:
    """A whole scene laid out for one canvas."""

    canvas: Canvas = field(default_factory=lambda: Canvas(1, 1))
    elements: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def visible_elements(self) -> list:
        return [element for element in self.elements if element.visible]

    def element(self, element_id: str) -> Optional[ResolvedElement]:
        for element in self.elements:
            if element.element_id == element_id:
                return element
        return None

    def to_dict(self) -> dict:
        return {
            "canvas": {"width": self.canvas.width, "height": self.canvas.height, "fps": self.canvas.fps},
            "elements": [element.to_dict() for element in self.elements],
            "issues": [issue.to_dict() for issue in self.issues],
        }


# --------------------------------------------------------------------------
# Size resolution
# --------------------------------------------------------------------------

def _resolve_axis(axis: dict, *, frame_extent: float, reference: float,
                  fill_extent: float) -> tuple[float, bool]:
    """Resolve one axis to pixels.  Returns ``(pixels, is_auto)``."""
    mode = str(axis.get("mode", "auto")).lower()
    try:
        value = float(axis.get("value", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0

    if mode == "fraction":
        return max(0.0, value * frame_extent), False
    if mode == "design":
        return max(0.0, value * reference), False
    if mode == "relative":  # legacy alias for design units
        return max(0.0, value * reference), False
    if mode == "pixels":
        return max(0.0, value), False
    if mode == "fill":
        return max(0.0, fill_extent), False
    return 0.0, True


def _design_font_size(spec: ElementSpec, ctx: LayoutContext) -> float:
    """The requested text size in pixels, before fitting shrinks it."""
    size = spec.size if isinstance(spec.size, dict) else {}
    mode = str(size.get("mode", "") or "").strip().lower()
    value = size.get("value", None)

    if mode == "pixels":
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return ctx.reference * DEFAULT_TEXT_SIZE

    # "width" given explicitly as a design/pixel size is used for the box, so
    # the font size then comes from the (legacy) top-level value.
    try:
        fraction = float(value) if value is not None else DEFAULT_TEXT_SIZE
    except (TypeError, ValueError):
        fraction = DEFAULT_TEXT_SIZE
    if fraction <= 0:
        fraction = DEFAULT_TEXT_SIZE
    return ctx.reference * fraction


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

def layout_element(spec: ElementSpec, ctx: LayoutContext, *, z: int = 0) -> ResolvedElement:
    """Lay one element out.  Never raises: problems become issues."""
    kind = str(getattr(spec, "kind", "text") or "text").strip().lower()
    if kind not in ELEMENT_KINDS:
        resolved = _base_element(spec, ctx, z=z)
        resolved.kind = "text"
        resolved.issues.append(ElementIssue(
            resolved.element_id, "ELEMENT_KIND",
            f"Unknown element kind '{kind}'.",
            f"Choose one of {', '.join(ELEMENT_KINDS)}.",
            severity="error",
        ))
        return resolved

    builders = {
        "text": _layout_text,
        "image": _layout_image,
        "shape": _layout_shape,
        "card": _layout_card,
        "number": _layout_number,
        "chart": _layout_chart,
        "divider": _layout_divider,
    }
    try:
        return builders[kind](spec, ctx, z)
    except Exception as exc:  # never let one element break a preview
        resolved = _base_element(spec, ctx, z=z)
        resolved.kind = kind
        resolved.issues.append(ElementIssue(
            resolved.element_id, "ELEMENT_LAYOUT",
            f"This {kind} element could not be laid out: {exc.__class__.__name__}: {exc}.",
            "Check the element's settings. The rest of the scene still renders.",
            severity="error",
        ))
        return resolved


def _base_element(spec: ElementSpec, ctx: LayoutContext, *, z: int = 0) -> ResolvedElement:
    """Shared style resolution for every kind."""
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    fit = spec.fit if isinstance(spec.fit, dict) else {}
    canvas = ctx.canvas
    safe = ctx.safe_area.rect

    element = ResolvedElement(element_id=getattr(spec, "id", "") or "", z=z, spec=spec)
    element.kind = str(getattr(spec, "kind", "text") or "text")

    element.color = ctx.color(spec.color, role="text")
    background = extra.get("background")
    if background not in (None, ""):
        gradient = Gradient.from_value(background)
        if gradient is not None:
            element.gradient = gradient
        else:
            element.background = ctx.color(background, role="surface")

    try:
        element.opacity = max(0.0, min(1.0, float(extra.get("opacity", 1.0))))
    except (TypeError, ValueError):
        element.opacity = 1.0

    element.radius = int(round(_design(extra.get("radius", 0.0), ctx)))
    element.border_width = int(round(_design(extra.get("border_width", 0.0), ctx)))
    if element.border_width:
        element.border_color = ctx.color(extra.get("border_color"), role="accent")

    element.padding = (
        int(round(_design(extra.get("padding_left", extra.get("padding", 0.0)), ctx))),
        int(round(_design(extra.get("padding_top", extra.get("padding", 0.0)), ctx))),
        int(round(_design(extra.get("padding_right", extra.get("padding", 0.0)), ctx))),
        int(round(_design(extra.get("padding_bottom", extra.get("padding", 0.0)), ctx))),
    )
    element.align = str(extra.get("align", "left")).lower()
    if element.align not in TEXT_ALIGNMENTS:
        element.align = "left"
    try:
        element.line_spacing = max(0.6, min(3.0, float(fit.get("line_spacing", 1.2))))
    except (TypeError, ValueError):
        element.line_spacing = 1.2
    element.bold = bool(extra.get("bold", False))
    element.italic = bool(extra.get("italic", False))
    try:
        element.rotation = float(extra.get("rotation", 0.0) or 0.0)
    except (TypeError, ValueError):
        element.rotation = 0.0

    element.visible = bool(extra.get("visible", True))

    # The box: width/height resolved from the size dict, then anchored.
    sizes = normalise_size(spec.size)
    position = spec.position if isinstance(spec.position, dict) else {"x": 0.5, "y": 0.5}
    try:
        px = float(position.get("x", 0.5))
        py = float(position.get("y", 0.5))
    except (TypeError, ValueError):
        px, py = 0.5, 0.5

    width, width_auto = _resolve_axis(
        sizes["width"], frame_extent=canvas.width, reference=ctx.reference,
        fill_extent=safe.width * canvas.width,
    )
    height, height_auto = _resolve_axis(
        sizes["height"], frame_extent=canvas.height, reference=ctx.reference,
        fill_extent=safe.height * canvas.height,
    )
    element._box_hint = (px, py, width, height, width_auto, height_auto)  # type: ignore[attr-defined]
    return element


def _design(value: Any, ctx: LayoutContext) -> float:
    """A design-space value (fraction of the reference edge) in pixels."""
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return 0.0
    return fraction * ctx.reference


def _place(element: ResolvedElement, ctx: LayoutContext, width: float, height: float) -> None:
    """Anchor a box of ``width``x``height`` at the element's normalised point."""
    px, py = element._box_hint[0], element._box_hint[1]  # type: ignore[attr-defined]
    anchor = str(getattr(element.spec, "anchor", "center") or "center")
    ax, ay = anchor_point(anchor)
    canvas = ctx.canvas

    left = px * canvas.width - ax * width
    top = py * canvas.height - ay * height
    norm = Rect(left / canvas.width if canvas.width else 0.0,
                top / canvas.height if canvas.height else 0.0,
                width / canvas.width if canvas.width else 0.0,
                height / canvas.height if canvas.height else 0.0)
    element.norm_rect = norm
    element.rect = ctx.canvas.rect_pixels(norm)


# -- text ------------------------------------------------------------------

def _legacy_size_form(spec: ElementSpec) -> bool:
    """True when ``size`` is the Stage B single-value form.

    ``{"mode": "relative", "value": 0.12}`` predates Stage D.  For a text
    element that value is a **font size**, so it must not also be read as the
    box width - doing that would squeeze every existing project's text into a
    12% wide column.  Box axes then default to ``auto``.
    """
    size = spec.size if isinstance(spec.size, dict) else {}
    if "width" in size or "height" in size:
        return False
    return str(size.get("mode", "") or "").strip().lower() in ("relative", "fraction", "pixels")


def _layout_text(spec: ElementSpec, ctx: LayoutContext, z: int, *,
                 text: Optional[str] = None, height_fraction: float = 1.0,
                 max_lines_override: Optional[int] = None,
                 min_scale_override: Optional[float] = None) -> ResolvedElement:
    element = _base_element(spec, ctx, z=z)
    element.kind = "text"
    fit = spec.fit if isinstance(spec.fit, dict) else {}
    canvas = ctx.canvas
    safe = ctx.safe_area.rect

    requested = _design_font_size(spec, ctx)
    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]

    # The legacy single-value form describes the font, not the box.
    if _legacy_size_form(spec):
        width_auto = True
        height_auto = True

    pad_left, pad_top, pad_right, pad_bottom = element.padding
    if width_auto:
        # No explicit width: use the safe area so long text wraps sensibly.
        width = safe.width * canvas.width
    if height_auto:
        # Generous height; fitting will shrink the text to fit the width first.
        height = max(requested * 1.6, safe.height * canvas.height * 0.5)
    height = height * max(0.05, min(1.0, height_fraction))

    inner_width = max(1.0, width - pad_left - pad_right)
    inner_height = max(1.0, height - pad_top - pad_bottom)

    try:
        min_scale = max(0.05, min(1.0, float(fit.get("min_scale", 0.4))))
    except (TypeError, ValueError):
        min_scale = 0.4
    if min_scale_override is not None:
        min_scale = max(0.01, min(1.0, min_scale_override))
    try:
        max_lines = max(0, int(fit.get("max_lines", 0) or 0))
    except (TypeError, ValueError):
        max_lines = 0
    if max_lines_override is not None:
        max_lines = max_lines_override

    options = FitOptions(
        max_size=requested,
        min_size=max(4.0, requested * min_scale),
        max_lines=max_lines,
        line_spacing=element.line_spacing,
        allow_grow=bool(fit.get("allow_grow", False)),
    )

    content = (getattr(spec, "text", "") or "") if text is None else text
    fonts_seen: dict = {}
    fitted = fit_text(lambda size: ctx.font(size, bold=element.bold, italic=element.italic)[0],
                      content, inner_width, inner_height, options)

    font, match, note = ctx.font(fitted.font_size, bold=element.bold, italic=element.italic)
    element.font_size = fitted.font_size
    element.lines = fitted.lines
    element.overflow = fitted.overflow
    element.overflow_reason = fitted.reason
    element.font_note = note
    element.text_metrics = fitted

    from .text import missing_glyphs

    element.unsupported_characters = missing_glyphs(font, content, cache=fonts_seen)

    if fitted.overflow:
        element.issues.append(ElementIssue(
            element.element_id, "TEXT_OVERFLOW",
            f"The text does not fit: {fitted.reason}",
            "Shorten the text, make the element larger, or lower its minimum text size.",
        ))
    if element.unsupported_characters:
        preview = "".join(element.unsupported_characters[:8])
        family = match.describe() if match else "the bundled font"
        element.issues.append(ElementIssue(
            element.element_id, "FONT_MISSING_GLYPHS",
            f"{family} cannot draw these characters: {preview}",
            "Install a font that covers this script, or choose a different font in Settings.",
        ))
    if note:
        element.issues.append(ElementIssue(
            element.element_id, "FONT_SUBSTITUTED", note,
            "Install the font or pick one that is available on this PC.", severity="info",
        ))

    # Shrink the box to the text so backgrounds hug the content.
    used_height = fitted.height + pad_top + pad_bottom if height_auto else height
    used_width = max(fitted.width + pad_left + pad_right, 1.0) if width_auto else width
    _place(element, ctx, used_width, used_height)
    return element


# -- image -----------------------------------------------------------------

def _layout_image(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    element = _base_element(spec, ctx, z=z)
    element.kind = "image"
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    element.image_fit = str(extra.get("fit", "cover")).lower()
    if element.image_fit not in IMAGE_FIT_MODES:
        element.image_fit = "cover"

    asset_id = getattr(spec, "asset_id", "") or ""
    asset_root = ctx.asset_root
    path: Optional[Path] = None

    if asset_id and ctx.asset_paths:
        resolved = ctx.asset_paths.get(asset_id)
        if resolved is not None and Path(resolved).is_file():
            path = Path(resolved)

    if path is None and asset_id and asset_root is not None:
        # Assets live under the project's assets folder; the spec stores the id
        # and, optionally, an explicit relative path in extra.
        relative = str(extra.get("path", "") or "")
        candidate = (asset_root / relative) if relative else None
        if candidate is not None and candidate.is_file():
            path = candidate
        else:
            for suffix in (".png", ".jpg", ".jpeg", ".webp"):
                guess = asset_root / f"{asset_id}{suffix}"
                if guess.is_file():
                    path = guess
                    break

    if path is None:
        element.issues.append(ElementIssue(
            element.element_id, "IMAGE_MISSING",
            "No image file is available for this element." if not asset_id
            else f"The image asset '{asset_id}' was not found in the project.",
            "Add the image on the Assets page, or point this element at an image that exists.",
            severity="error",
        ))

    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]
    if width_auto:
        width = ctx.safe_area.rect.width * ctx.canvas.width
    if height_auto:
        # Keep the image's own proportions when only a width was given.
        ratio = _image_ratio(path)
        height = width / ratio if ratio else width * 9.0 / 16.0

    _place(element, ctx, width, height)
    element.image_path = path
    element.image_size = _image_size(path)
    element.image_box = _fit_image_box(element.image_size, element.rect, element.image_fit)
    return element


def _image_size(path: Optional[Path]) -> tuple:
    if path is None or not path.is_file():
        return (0, 0)
    try:
        from PIL import Image

        with Image.open(path) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return (0, 0)


def _image_ratio(path: Optional[Path]) -> float:
    width, height = _image_size(path)
    return width / height if width and height else 0.0


def _fit_image_box(image_size: tuple, box: PixelRect, mode: str) -> Optional[PixelRect]:
    """Where inside *box* the image pixels should go."""
    if box.is_empty:
        return None
    width, height = image_size
    if not width or not height:
        return box
    image_aspect = width / height
    box_aspect = box.width / box.height if box.height else 0.0

    if mode == "fill":
        return box
    if mode == "original":
        return PixelRect(box.x + (box.width - width) // 2, box.y + (box.height - height) // 2,
                         width, height)
    if mode == "contain":
        if image_aspect > box_aspect:
            new_width = box.width
            new_height = int(round(box.width / image_aspect))
        else:
            new_height = box.height
            new_width = int(round(box.height * image_aspect))
        return PixelRect(box.x + (box.width - new_width) // 2, box.y + (box.height - new_height) // 2,
                         new_width, new_height)
    # cover (default): fill the box, cropping the overflow.
    if image_aspect > box_aspect:
        new_height = box.height
        new_width = int(round(box.height * image_aspect))
    else:
        new_width = box.width
        new_height = int(round(box.width / image_aspect))
    return PixelRect(box.x - (new_width - box.width) // 2, box.y - (new_height - box.height) // 2,
                     new_width, new_height)


# -- shape -----------------------------------------------------------------

def _layout_shape(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    element = _base_element(spec, ctx, z=z)
    element.kind = "shape"
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    element.shape = str(extra.get("shape", "rect")).lower()
    if element.shape not in SHAPE_KINDS:
        element.issues.append(ElementIssue(
            element.element_id, "SHAPE_KIND",
            f"Unknown shape '{element.shape}'.",
            f"Choose one of {', '.join(SHAPE_KINDS)}.",
        ))
        element.shape = "rect"
    if element.background is None and element.gradient is None:
        element.background = ctx.color(extra.get("fill"), role="accent")

    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]
    reference = ctx.reference
    if width_auto:
        width = reference * 0.4
    if height_auto:
        height = width if element.shape in ("circle", "ellipse", "badge") else reference * 0.02
    _place(element, ctx, width, height)
    return element


def _layout_divider(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    """A thin rule.  Responsive by construction: its length follows the frame."""
    element = _base_element(spec, ctx, z=z)
    element.kind = "divider"
    element.shape = "line"
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    if element.background is None:
        element.background = ctx.color(extra.get("fill"), role="accent")
    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]
    if width_auto:
        width = ctx.safe_area.rect.width * ctx.canvas.width * 0.6
    if height_auto:
        height = max(1.0, ctx.reference * float(extra.get("thickness", 0.004) or 0.004))
    _place(element, ctx, width, height)
    return element


# -- number ----------------------------------------------------------------

def _layout_number(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    """A big statistic: value, unit and label, each sized from real metrics.

    The value plus unit is fitted as one block (a statistic should stay on one
    line); the label gets its own smaller fit underneath.  Both contribute to
    the element's height, so the box is never sized from nothing.
    """
    extra = spec.extra if isinstance(spec.extra, dict) else {}

    raw_value = extra.get("value", None)
    if raw_value is None:
        raw_value = getattr(spec, "text", "")
    value_text = format_number(raw_value, extra)
    unit_text = str(extra.get("unit", "") or "")
    label_text = str(extra.get("label", "") or "")

    primary = f"{value_text} {unit_text}".strip()
    label_share = 0.30 if label_text.strip() else 1.0
    element = _layout_text(
        spec, ctx, z,
        text=primary,
        height_fraction=1.0 - label_share if label_text.strip() else 1.0,
        max_lines_override=1,
    )
    element.kind = "number"

    if element.overflow:
        # A statistic split over two lines is worse than a smaller statistic.
        # Keep shrinking past the user's minimum, but say that we did.
        relaxed = _layout_text(
            spec, ctx, z,
            text=primary,
            height_fraction=1.0 - label_share if label_text.strip() else 1.0,
            max_lines_override=1,
            min_scale_override=0.05,
        )
        if not relaxed.overflow:
            relaxed.issues.append(ElementIssue(
                relaxed.element_id, "NUMBER_SHRUNK",
                f"This number had to be reduced to {relaxed.font_size:.0f}px to stay on one line.",
                "Use fewer digits or decimals, shorten the unit, or make the element wider.",
                severity="info",
            ))
            element = relaxed
            element.kind = "number"
    element.number_text = value_text
    element.unit_text = unit_text
    element.label_text = label_text

    if label_text.strip():
        label_spec = ElementSpec(
            id=f"{element.element_id}:label",
            kind="text",
            text=label_text,
            anchor=getattr(spec, "anchor", "center"),
            position=spec.position if isinstance(spec.position, dict) else {"x": 0.5, "y": 0.5},
            size=spec.size,
            fit=dict(spec.fit if isinstance(spec.fit, dict) else {}, max_lines=2),
            color=spec.color,
            extra=dict(extra, align=element.align, label=""),
        )
        # The caption is a fixed fraction of the statistic's size.
        try:
            caption_scale = max(0.15, min(0.8, float(extra.get("label_scale", 0.32))))
        except (TypeError, ValueError):
            caption_scale = 0.32
        label_spec.size = {"mode": "relative", "value": (_design_font_size(spec, ctx) / ctx.reference) * caption_scale}
        label = _layout_text(label_spec, ctx, 0, height_fraction=1.0)
        element.label_lines = label.lines
        element.label_font_size = label.font_size
        if label.overflow:
            element.issues.append(ElementIssue(
                element.element_id, "LABEL_OVERFLOW",
                f"The label does not fit: {label.reason}",
                "Shorten the label or make the element larger.",
            ))

    if not element.number_text.strip() and not element.lines:
        element.issues.append(ElementIssue(
            element.element_id, "NUMBER_EMPTY",
            "This number element has no value.",
            "Set a value, or remove the element.",
        ))
    return element


def format_number(value: Any, options: Optional[dict] = None) -> str:
    """Format a number for display without changing its meaning.

    Thousands separators and decimal places are applied; the digits themselves
    are never rewritten.  Non-numeric input is returned as text, because a
    number element holding "N/A" should still say "N/A".
    """
    opts = options or {}
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        number = float(text.replace(",", ""))
    except (TypeError, ValueError):
        return text

    try:
        decimals = int(opts.get("decimals", 2 if number % 1 else 0))
    except (TypeError, ValueError):
        decimals = 0
    decimals = max(0, min(10, decimals))
    separators = bool(opts.get("thousands", True))
    prefix = str(opts.get("prefix", "") or "")
    suffix = str(opts.get("suffix", "") or "")

    formatted = f"{number:,.{decimals}f}" if separators else f"{number:.{decimals}f}"
    return f"{prefix}{formatted}{suffix}"


# -- card ------------------------------------------------------------------

def _layout_card(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    """A container: a styled box with its own nested elements.

    Children are laid out in the card's *own* coordinate space (0..1 of the
    card), which is what makes a card reusable at any size - the same card
    definition works full screen or as a small inset.
    """
    element = _base_element(spec, ctx, z=z)
    element.kind = "card"
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    if element.background is None and element.gradient is None:
        element.background = ctx.color(extra.get("fill"), role="surface")
    if element.border_width == 0 and extra.get("border"):
        element.border_width = max(1, int(round(_design(0.004, ctx))))
        element.border_color = ctx.color(extra.get("border_color"), role="accent")

    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]
    safe = ctx.safe_area.rect
    if width_auto:
        width = safe.width * ctx.canvas.width
    if height_auto:
        height = safe.height * ctx.canvas.height * 0.5
    _place(element, ctx, width, height)

    children_raw = extra.get("children")
    if isinstance(children_raw, Sequence) and not isinstance(children_raw, (str, bytes)):
        inner_canvas = Canvas(max(1, element.rect.width), max(1, element.rect.height), ctx.canvas.fps)
        inner_ctx = LayoutContext(
            canvas=inner_canvas,
            safe_area=SafeArea(0.0, 0.0, 0.0, 0.0),
            palette=dict(ctx.palette),
            font_loader=ctx.font_loader,
            font_family=ctx.font_family,
            asset_root=ctx.asset_root,
            scale=1.0,
        )
        for index, child in enumerate(children_raw):
            child_spec = child if isinstance(child, ElementSpec) else ElementSpec.from_dict(child)
            laid_out = layout_element(child_spec, inner_ctx, z=index)
            # Translate the child from card space into scene space.
            laid_out.rect = PixelRect(
                element.rect.x + laid_out.rect.x,
                element.rect.y + laid_out.rect.y,
                laid_out.rect.width,
                laid_out.rect.height,
            )
            element.children.append(laid_out)
            element.issues.extend(laid_out.issues)
    return element


# -- chart -----------------------------------------------------------------

def _layout_chart(spec: ElementSpec, ctx: LayoutContext, z: int) -> ResolvedElement:
    element = _base_element(spec, ctx, z=z)
    element.kind = "chart"
    extra = spec.extra if isinstance(spec.extra, dict) else {}
    element.chart_kind = str(extra.get("chart", "bar")).lower()
    if element.chart_kind not in CHART_KINDS:
        element.issues.append(ElementIssue(
            element.element_id, "CHART_KIND",
            f"Unknown chart type '{element.chart_kind}'.",
            f"Choose one of {', '.join(CHART_KINDS)}.",
        ))
        element.chart_kind = "bar"

    values, labels = _chart_series(extra)
    element.chart_series = values
    element.chart_labels = labels
    if not values:
        element.issues.append(ElementIssue(
            element.element_id, "CHART_EMPTY",
            "This chart has no data.",
            "Add values, or remove the chart.",
            severity="error",
        ))

    _, _, width, height, width_auto, height_auto = element._box_hint  # type: ignore[attr-defined]
    safe = ctx.safe_area.rect
    if width_auto:
        width = safe.width * ctx.canvas.width * 0.8
    if height_auto:
        height = safe.height * ctx.canvas.height * 0.4
    _place(element, ctx, width, height)

    accent = ctx.color(None, role="accent")
    element.chart_colors = [accent.tuple]
    return element


def _chart_series(extra: dict) -> tuple:
    """Accept ``values``/``data`` as a list of numbers or of label/value pairs."""
    raw = extra.get("values", extra.get("data", extra.get("series")))
    if raw is None:
        return [], []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = list(raw)
    else:
        return [], []

    values: list = []
    labels: list = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            label = str(item.get("label", item.get("name", f"#{index + 1}")))
            candidate = item.get("value", item.get("y", 0))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            label = str(item[0])
            candidate = item[1]
        else:
            label = f"#{index + 1}"
            candidate = item
        try:
            values.append(float(candidate))
        except (TypeError, ValueError):
            values.append(0.0)
        labels.append(label)
    return values, labels


# --------------------------------------------------------------------------
# Scene
# --------------------------------------------------------------------------

def layout_scene(elements: Sequence[ElementSpec], ctx: LayoutContext) -> SceneLayout:
    """Lay out every element of a scene.  Later elements draw on top."""
    layout = SceneLayout(canvas=ctx.canvas)
    for index, spec in enumerate(elements):
        resolved = layout_element(spec, ctx, z=index)
        layout.elements.append(resolved)
        layout.issues.extend(resolved.issues)

    _report_offscreen(layout)
    _report_overlaps(layout)
    return layout


def _report_offscreen(layout: SceneLayout) -> None:
    """An element placed entirely outside the frame is almost always a mistake."""
    for element in layout.elements:
        if not element.visible:
            continue
        norm = element.norm_rect
        outside = (norm.x1 <= 0.0 or norm.y1 <= 0.0 or norm.x >= 1.0 or norm.y >= 1.0)
        if outside:
            element.issues.append(ElementIssue(
                element.element_id, "ELEMENT_OFFSCREEN",
                f"This {element.kind} element sits outside the frame "
                f"(x={norm.x:.2f}, y={norm.y:.2f}).",
                "Move it back inside the frame, or delete it if it is not needed.",
            ))
            layout.issues.append(element.issues[-1])


def _report_overlaps(layout: SceneLayout) -> None:
    """Warn when two *text* elements overlap - usually an accidental collision.

    Deliberately limited to text: shapes, cards and dividers are meant to sit
    behind text, so flagging those would be noise.
    """
    text_elements = [e for e in layout.elements if e.visible and e.kind in ("text", "number") and not e.rect.is_empty]
    for i, first in enumerate(text_elements):
        for second in text_elements[i + 1:]:
            overlap = first.rect.width and second.rect.width
            if not overlap:
                continue
            ix0 = max(first.rect.x, second.rect.x)
            iy0 = max(first.rect.y, second.rect.y)
            ix1 = min(first.rect.x1, second.rect.x1)
            iy1 = min(first.rect.y1, second.rect.y1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            area = (ix1 - ix0) * (iy1 - iy0)
            smaller = min(first.rect.width * first.rect.height, second.rect.width * second.rect.height)
            if smaller and area / smaller > 0.34:
                issue = ElementIssue(
                    first.element_id, "TEXT_OVERLAP",
                    "This text overlaps another text element by more than a third.",
                    "Move one of them, or reduce the text size.",
                )
                first.issues.append(issue)
                layout.issues.append(issue)
