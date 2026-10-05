"""Scene composition: a laid-out scene becomes an image (Stage D).

This is the only module in the engine that touches pixels.  It takes the output
of :func:`app.scene.elements.layout_scene` plus an animation time and produces
a Pillow ``RGBA`` image.  It never starts a thread, never writes a file and
never blocks: callers that need it off the UI thread run it inside a job
(:mod:`app.scene.jobs`).

Rendering is deterministic - the same scene, canvas and time always produce the
same pixels - which is what makes previews trustworthy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image, ImageDraw

from .animation import AnimationSpec, ElementTransform, IDENTITY, evaluate
from .canvas import Canvas, PixelRect
from .charts import ChartStyle, draw_chart
from .elements import LayoutContext, ResolvedElement, SceneLayout
from .palette import Color, Gradient, parse_color
from .shapes import composite_layer, draw_gradient, draw_shape

__all__ = [
    "RenderOptions",
    "render_background",
    "compose_scene",
    "render_scene",
    "compose_transition",
    "save_image",
]


class RenderOptions:
    """Tunables for one render."""

    def __init__(self, *, quality: str = "preview", draw_safe_area: bool = False,
                 draw_bounds: bool = False, background_fallback: str = "#101014") -> None:
        self.quality = quality
        self.draw_safe_area = draw_safe_area
        self.draw_bounds = draw_bounds
        self.background_fallback = background_fallback


def render_background(canvas: Canvas, background: Any, *,
                      fallback: str = "#101014") -> Image.Image:
    """The scene's background: a gradient, a colour, or the project default."""
    image = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 255))
    gradient = Gradient.from_value(background)
    if gradient is not None:
        layer = draw_gradient(PixelRect(0, 0, canvas.width, canvas.height), gradient)
        composite_layer(image, layer, (0, 0))
        return image
    colour = parse_color(background, fallback) if background not in (None, "") else parse_color(fallback)
    return Image.new("RGBA", (canvas.width, canvas.height), colour.tuple)


def compose_scene(layout: SceneLayout, *, background: Any = None, time: Optional[float] = None,
                  scene_duration: float = 0.0, animations: Optional[dict] = None,
                  options: Optional[RenderOptions] = None,
                  ctx: Optional[LayoutContext] = None) -> Image.Image:
    """Draw a laid-out scene at ``time`` seconds into its scene.

    ``animations`` maps element id to :class:`AnimationSpec`; when it is not
    given, each element's own ``spec.animation`` is used.
    """
    opts = options or RenderOptions()
    canvas = layout.canvas
    image = render_background(canvas, background, fallback=opts.background_fallback)
    moment = max(0.0, float(time)) if time is not None else None

    for element in layout.elements:
        if not element.visible:
            continue
        transform = IDENTITY
        if moment is not None:
            spec = (animations or {}).get(element.element_id)
            if spec is None:
                spec = _spec_animation(element)
            transform = evaluate(spec, moment, scene_duration=scene_duration)
        if transform.is_invisible:
            continue
        _draw_element(image, element, transform, ctx, opts)

    if opts.draw_safe_area and ctx is not None:
        _draw_guide(image, ctx.safe_area.rect, canvas, (255, 200, 60, 160))
    if opts.draw_bounds:
        for element in layout.elements:
            if element.visible:
                _draw_box(image, element.rect, (120, 200, 255, 150))
    return image


def render_scene(scene: Any, ctx: LayoutContext, *, time: Optional[float] = None,
                 scene_duration: float = 0.0, background: Any = None,
                 options: Optional[RenderOptions] = None) -> Image.Image:
    """Lay out and draw one scene in a single call."""
    from .elements import layout_scene

    elements = getattr(scene, "elements", []) or []
    layout = layout_scene(elements, ctx)
    if background is None:
        background = getattr(scene, "background", "") or ctx.palette.get("background")
    return compose_scene(layout, background=background, time=time,
                         scene_duration=scene_duration, options=options, ctx=ctx)


def _spec_animation(element: ResolvedElement) -> Optional[AnimationSpec]:
    """The animation for one element, parsed on demand.

    An empty ``animation`` dict is *not* the same as "no animation": it means
    the user has not chosen, so the element gets the default preset for its
    kind.  An explicit ``{"preset": "none"}`` is what turns animation off.
    """
    spec = getattr(element, "spec", None)
    raw = getattr(spec, "animation", None)
    from .animation import parse_animation

    parsed = parse_animation(raw if isinstance(raw, dict) else {}, kind=element.kind)
    return None if parsed.is_empty else parsed


# --------------------------------------------------------------------------
# Element drawing
# --------------------------------------------------------------------------

def _draw_element(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                  ctx: Optional[LayoutContext], opts: RenderOptions) -> None:
    """Draw one element, applying its animated transform."""
    if transform.is_identity:
        _paint_element(image, element, transform, ctx)
        return

    # Animate on its own layer so scaling and rotation stay clean.
    margin = _transform_margin(element.rect, transform)
    layer_rect = PixelRect(element.rect.x - margin, element.rect.y - margin,
                           element.rect.width + margin * 2, element.rect.height + margin * 2)
    if layer_rect.width <= 0 or layer_rect.height <= 0:
        return

    layer = Image.new("RGBA", (layer_rect.width, layer_rect.height), (0, 0, 0, 0))
    shifted = ResolvedElement(
        element_id=element.element_id, kind=element.kind,
        rect=PixelRect(element.rect.x - layer_rect.x, element.rect.y - layer_rect.y,
                       element.rect.width, element.rect.height),
    )
    _copy_paintable(element, shifted)

    _paint_element(layer, shifted, ElementTransform(opacity=1.0), ctx)

    if transform.opacity < 1.0:
        _scale_alpha(layer, transform.opacity)

    scaled = layer
    if abs(transform.scale - 1.0) > 1e-4:
        new_size = (max(1, int(layer.width * transform.scale)),
                    max(1, int(layer.height * transform.scale)))
        scaled = layer.resize(new_size, Image.BILINEAR)
    if abs(transform.rotation) > 1e-4:
        scaled = scaled.rotate(transform.rotation, resample=Image.BILINEAR, expand=True)

    offset_x = int(round(transform.x * image.width))
    offset_y = int(round(transform.y * image.height))
    paste_x = layer_rect.x + (layer.width - scaled.width) // 2 + offset_x
    paste_y = layer_rect.y + (layer.height - scaled.height) // 2 + offset_y
    image.alpha_composite(scaled, dest=(paste_x, paste_y))


def _transform_margin(rect: PixelRect, transform: ElementTransform) -> int:
    """Enough room that a scaled or rotated element is not clipped."""
    span = max(rect.width, rect.height)
    growth = max(0.0, transform.scale - 1.0) * span
    rotation_room = span * 0.42 if abs(transform.rotation) > 1e-4 else 0.0
    return int(max(1, growth / 2 + rotation_room))


_PAINTABLE_FIELDS = (
    "norm_rect", "color", "background", "gradient", "radius", "border_width", "border_color",
    "padding", "align", "font_size", "lines", "bold", "italic", "line_spacing",
    "image_path", "image_fit", "image_size", "image_box", "shape",
    "number_text", "unit_text", "label_text", "label_lines", "label_font_size",
    "chart_kind", "chart_series", "chart_labels", "chart_colors", "children", "opacity",
)


def _copy_paintable(source: ResolvedElement, target: ResolvedElement) -> None:
    for name in _PAINTABLE_FIELDS:
        setattr(target, name, getattr(source, name))


def _paint_element(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                   ctx: Optional[LayoutContext]) -> None:
    kind = element.kind
    if kind in ("shape", "divider"):
        _paint_shape(image, element, transform)
    elif kind == "image":
        _paint_image(image, element, transform)
    elif kind == "card":
        _paint_card(image, element, transform, ctx)
    elif kind == "chart":
        _paint_chart(image, element, transform, ctx)
    elif kind == "number":
        _paint_number(image, element, transform, ctx)
    else:
        _paint_text(image, element, transform, ctx)


def _effective_opacity(element: ResolvedElement, transform: ElementTransform) -> float:
    return max(0.0, min(1.0, element.opacity * transform.opacity))


def _paint_shape(image: Image.Image, element: ResolvedElement, transform: ElementTransform) -> None:
    draw_shape(
        image, element.rect, element.shape,
        fill=element.background, gradient=element.gradient,
        radius=element.radius, border_width=element.border_width,
        border_color=element.border_color, opacity=_effective_opacity(element, transform),
    )


def _paint_image(image: Image.Image, element: ResolvedElement, transform: ElementTransform) -> None:
    path = element.image_path
    if path is None or not Path(path).is_file():
        _paint_placeholder(image, element.rect, "Image not found")
        return
    try:
        with Image.open(path) as source:
            picture = source.convert("RGBA")
    except Exception as exc:  # a corrupt file must not break the preview
        _paint_placeholder(image, element.rect, f"Cannot open image ({exc.__class__.__name__})")
        return

    box = element.image_box or element.rect
    if box.width <= 0 or box.height <= 0:
        return
    try:
        resized = picture.resize((max(1, box.width), max(1, box.height)), Image.BILINEAR)
    except (ValueError, OSError):
        return

    if element.image_fit == "cover" and (box.x < element.rect.x or box.y < element.rect.y
                                         or box.width > element.rect.width
                                         or box.height > element.rect.height):
        # Clip the overflow to the element's own box.
        canvas = Image.new("RGBA", (max(1, element.rect.width), max(1, element.rect.height)), (0, 0, 0, 0))
        canvas.paste(resized, (box.x - element.rect.x, box.y - element.rect.y), resized)
        resized = canvas
        box = element.rect

    if _effective_opacity(element, transform) < 1.0:
        _scale_alpha(resized, _effective_opacity(element, transform))
    image.alpha_composite(resized, dest=(box.x, box.y))


def _paint_placeholder(image: Image.Image, rect: PixelRect, label: str) -> None:
    """A visible, labelled stand-in - never a silently blank area."""
    if rect.is_empty:
        return
    layer = Image.new("RGBA", (rect.width, rect.height), (60, 60, 68, 200))
    draw = ImageDraw.Draw(layer)
    draw.rectangle([0, 0, rect.width - 1, rect.height - 1], outline=(180, 180, 190, 255), width=2)
    draw.line([(0, 0), (rect.width, rect.height)], fill=(140, 140, 150, 200), width=1)
    draw.line([(rect.width, 0), (0, rect.height)], fill=(140, 140, 150, 200), width=1)
    try:
        from PIL import ImageFont

        font = ImageFont.load_default(size=max(9, min(28, rect.height // 8)))
        box = font.getbbox(label)
        draw.text(((rect.width - (box[2] - box[0])) // 2, (rect.height - (box[3] - box[1])) // 2),
                  label, font=font, fill=(230, 230, 235, 255))
    except Exception:
        pass
    composite_layer(image, layer, (rect.x, rect.y))


def _paint_card(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                ctx: Optional[LayoutContext]) -> None:
    draw_shape(
        image, element.rect, "rounded",
        fill=element.background, gradient=element.gradient,
        radius=element.radius or min(element.rect.width, element.rect.height) // 12,
        border_width=element.border_width, border_color=element.border_color,
        opacity=_effective_opacity(element, transform),
    )
    for child in element.children:
        _paint_element(image, child, transform, ctx)


def _paint_chart(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                 ctx: Optional[LayoutContext]) -> None:
    style = ChartStyle()
    if element.chart_colors:
        style.colors = list(element.chart_colors)
    style.opacity = _effective_opacity(element, transform)
    extra = getattr(getattr(element, "spec", None), "extra", None)
    if isinstance(extra, dict):
        style = ChartStyle.from_element(extra)
        style.opacity = _effective_opacity(element, transform)
        if element.chart_colors:
            style.colors = list(element.chart_colors) + list(style.colors)

    font_factory = None
    if ctx is not None:
        font_factory = lambda size: ctx.font(size)[0]  # noqa: E731 - small local closure

    draw_chart(image, element.rect, element.chart_kind, element.chart_series,
               element.chart_labels, style, font_factory)


def _paint_number(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                  ctx: Optional[LayoutContext]) -> None:
    _paint_box_background(image, element, transform)
    _paint_lines(image, element, element.lines, element.font_size, element.color,
                 element.align, transform, ctx, element.bold, element.italic, element.line_spacing)
    if element.label_lines and element.label_font_size > 0:
        label_colour = Color(element.color.r, element.color.g, element.color.b,
                             int(element.color.a * 0.78))
        offset = element.rect.height - int(element.label_font_size * 1.5 * len(element.label_lines))
        _paint_lines(image, element, element.label_lines, element.label_font_size, label_colour,
                     element.align, transform, ctx, False, False, 1.15, y_offset=max(0, offset))


def _paint_text(image: Image.Image, element: ResolvedElement, transform: ElementTransform,
                ctx: Optional[LayoutContext]) -> None:
    _paint_box_background(image, element, transform)
    _paint_lines(image, element, element.lines, element.font_size, element.color,
                 element.align, transform, ctx, element.bold, element.italic, element.line_spacing)


def _paint_box_background(image: Image.Image, element: ResolvedElement,
                          transform: ElementTransform) -> None:
    if element.background is None and element.gradient is None:
        return
    draw_shape(image, element.rect, "rounded" if element.radius else "rect",
               fill=element.background, gradient=element.gradient, radius=element.radius,
               border_width=element.border_width, border_color=element.border_color,
               opacity=_effective_opacity(element, transform))


def _paint_lines(image: Image.Image, element: ResolvedElement, lines: Sequence[str],
                 font_size: float, colour: Color, align: str, transform: ElementTransform,
                 ctx: Optional[LayoutContext], bold: bool, italic: bool, line_spacing: float,
                 y_offset: int = 0) -> None:
    """Draw already-fitted lines.  Fitting happened during layout, not here."""
    if not lines or font_size <= 0 or ctx is None:
        return
    font, _match, _note = ctx.font(font_size, bold=bold, italic=italic)
    pad_left, pad_top, pad_right, _pad_bottom = element.padding
    inner_width = max(1, element.rect.width - pad_left - pad_right)

    try:
        ascent, descent = font.getmetrics()
    except (OSError, ValueError, AttributeError):
        ascent, descent = int(font_size * 0.8), int(font_size * 0.2)
    line_height = max(1.0, (ascent + descent) * max(0.6, line_spacing))

    layer = Image.new("RGBA", (max(1, element.rect.width), max(1, element.rect.height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    opacity = _effective_opacity(element, transform)
    fill = (colour.r, colour.g, colour.b, int(colour.a * opacity))

    y = pad_top + y_offset
    for line in lines:
        if not line:
            y += line_height
            continue
        try:
            box = font.getbbox(line)
            width = box[2] - box[0]
        except (OSError, ValueError):
            width = 0
        if align == "center":
            x = pad_left + (inner_width - width) / 2
        elif align == "right":
            x = pad_left + (inner_width - width)
        else:
            x = pad_left
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height

    composite_layer(image, layer, (element.rect.x, element.rect.y))


def _scale_alpha(image: Image.Image, factor: float) -> None:
    """Multiply an image's alpha channel by ``factor``, in place."""
    if factor >= 1.0:
        return
    alpha = image.getchannel("A").point(lambda value: int(value * max(0.0, min(1.0, factor))))
    image.putalpha(alpha)


# --------------------------------------------------------------------------
# Guides and transitions
# --------------------------------------------------------------------------

def _draw_guide(image: Image.Image, rect, canvas: Canvas, colour) -> None:
    box = canvas.rect_pixels(rect)
    _draw_box(image, box, colour)


def _draw_box(image: Image.Image, rect: PixelRect, colour) -> None:
    if rect.is_empty:
        return
    draw = ImageDraw.Draw(image)
    draw.rectangle([rect.x, rect.y, rect.x1 - 1, rect.y1 - 1], outline=colour, width=1)


def compose_transition(first: Image.Image, second: Image.Image, fraction: float,
                       kind: str = "fade") -> Image.Image:
    """Blend two rendered frames.  Thin wrapper kept for symmetry."""
    from .transitions import blend

    return blend(first, second, fraction, kind)


def save_image(image: Image.Image, path: Path, *, overwrite: bool = False) -> Path:
    """Write an image to disk, refusing to clobber unless told to.

    Writes to a temporary file and renames, so a crash mid-write cannot leave a
    half-written preview that later looks like a valid image.
    """
    import os

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"{destination.name} already exists.")

    temporary = destination.with_name(destination.name + ".part")
    try:
        suffix = destination.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            image.convert("RGB").save(temporary, format="JPEG", quality=92)
        elif suffix == ".webp":
            image.save(temporary, format="WEBP", quality=92)
        else:
            image.save(temporary, format="PNG", optimize=False)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return destination
