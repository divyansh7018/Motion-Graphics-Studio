"""Shape drawing for the scene engine.

Every function takes a concrete :class:`~app.scene.canvas.PixelRect` produced
by the layout pass, so shapes are as resolution independent as the rest of the
system: a card border is a fraction of the reference edge, not "2 pixels
because that looked right at 1080p".

Draw calls go through a scratch RGBA layer whenever opacity or a gradient is
involved, so transparency composites correctly instead of painting flat.
"""

from __future__ import annotations

from typing import Optional

from PIL import Image, ImageDraw

from .canvas import PixelRect
from .palette import Color, Gradient, gradient_colors

__all__ = [
    "draw_shape",
    "draw_gradient",
    "draw_rounded_box",
    "composite_layer",
    "nice_number",
]


def composite_layer(base: Image.Image, layer: Image.Image, position: tuple = (0, 0)) -> None:
    """Alpha-composite *layer* onto *base* in place."""
    if base.mode != "RGBA":
        base.paste(layer, position, layer)
        return
    base.alpha_composite(layer, dest=(int(position[0]), int(position[1])))


def draw_gradient(rect: PixelRect, gradient: Gradient, *, opacity: float = 1.0,
                  radius: int = 0) -> Image.Image:
    """Build an RGBA layer holding a linear gradient, optionally rounded."""
    width = max(1, int(rect.width))
    height = max(1, int(rect.height))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    angle = float(gradient.angle) % 360.0
    vertical = angle < 45 or angle >= 315 or (135 <= angle < 225)
    colours = gradient_colors(gradient, height if vertical else width)
    alpha_scale = max(0.0, min(1.0, opacity))

    if vertical:
        reverse = 135 <= angle < 225
        for index, colour in enumerate(colours):
            y = height - 1 - index if reverse else index
            rgba = (colour[0], colour[1], colour[2], int(colour[3] * alpha_scale))
            draw.line([(0, y), (width, y)], fill=rgba)
    else:
        reverse = 225 <= angle < 315
        for index, colour in enumerate(colours):
            x = width - 1 - index if reverse else index
            rgba = (colour[0], colour[1], colour[2], int(colour[3] * alpha_scale))
            draw.line([(x, 0), (x, height)], fill=rgba)

    if radius > 0:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, width, height], radius=_safe_radius(radius, width, height),
                                               fill=255)
        layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", (width, height), 0), mask))
    return layer


def _safe_radius(radius: int, width: int, height: int) -> int:
    """Pillow rejects a radius larger than half the smaller side."""
    return max(0, min(int(radius), min(width, height) // 2))


def draw_rounded_box(image: Image.Image, rect: PixelRect, *, fill: Optional[Color] = None,
                     radius: int = 0, border_width: int = 0,
                     border_color: Optional[Color] = None, opacity: float = 1.0) -> None:
    """A rectangle (or rounded rectangle) with optional border."""
    if rect.is_empty:
        return
    scale = max(0.0, min(1.0, opacity))
    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = [0, 0, rect.width - 1, rect.height - 1]
    safe_radius = _safe_radius(radius, rect.width, rect.height)

    if fill is not None and not fill.is_transparent:
        rgba = (fill.r, fill.g, fill.b, int(fill.a * scale))
        if safe_radius:
            draw.rounded_rectangle(box, radius=safe_radius, fill=rgba)
        else:
            draw.rectangle(box, fill=rgba)

    if border_width > 0 and border_color is not None:
        width = max(1, min(int(border_width), min(rect.width, rect.height) // 2 or 1))
        inset = width // 2
        stroke_box = [inset, inset, rect.width - 1 - inset, rect.height - 1 - inset]
        rgba = (border_color.r, border_color.g, border_color.b, int(border_color.a * scale))
        if safe_radius:
            draw.rounded_rectangle(stroke_box, radius=max(0, safe_radius - inset), outline=rgba, width=width)
        else:
            draw.rectangle(stroke_box, outline=rgba, width=width)

    composite_layer(image, layer, (rect.x, rect.y))


def draw_shape(image: Image.Image, rect: PixelRect, shape: str, *,
               fill: Optional[Color] = None, gradient: Optional[Gradient] = None,
               radius: int = 0, border_width: int = 0,
               border_color: Optional[Color] = None, opacity: float = 1.0) -> None:
    """Draw one shape into *image*.  Unknown shapes fall back to a rectangle."""
    if rect.is_empty:
        return
    kind = (shape or "rect").lower()
    if kind in ("rounded", "pill", "badge"):
        if kind == "pill":
            radius = min(rect.width, rect.height) // 2
        elif kind == "badge" and rect.width == rect.height:
            kind = "circle"
        else:
            radius = radius or min(rect.width, rect.height) // 4

    if kind in ("rect", "rounded", "pill"):
        _draw_filled_rect(image, rect, fill, gradient, radius, border_width, border_color, opacity)
    elif kind in ("ellipse", "circle", "badge"):
        _draw_ellipse(image, rect, fill, gradient, border_width, border_color, opacity)
    elif kind == "line":
        _draw_line(image, rect, fill or border_color, opacity)
    elif kind == "triangle":
        _draw_polygon(image, [(rect.x + rect.width // 2, rect.y),
                              (rect.x, rect.y1), (rect.x1, rect.y1)],
                      fill, border_width, border_color, opacity)
    elif kind == "arrow":
        _draw_arrow(image, rect, fill, opacity)
    elif kind == "frame":
        draw_rounded_box(image, rect, fill=None, radius=radius, border_width=max(1, border_width),
                         border_color=border_color or fill, opacity=opacity)
    else:
        _draw_filled_rect(image, rect, fill, gradient, 0, border_width, border_color, opacity)


def _draw_filled_rect(image, rect, fill, gradient, radius, border_width, border_color, opacity) -> None:
    if gradient is not None:
        layer = draw_gradient(rect, gradient, opacity=opacity, radius=radius)
        composite_layer(image, layer, (rect.x, rect.y))
        if border_width and border_color is not None:
            draw_rounded_box(image, rect, fill=None, radius=radius,
                             border_width=border_width, border_color=border_color, opacity=opacity)
        return
    draw_rounded_box(image, rect, fill=fill, radius=radius, border_width=border_width,
                     border_color=border_color, opacity=opacity)


def _draw_ellipse(image, rect, fill, gradient, border_width, border_color, opacity) -> None:
    scale = max(0.0, min(1.0, opacity))
    layer = Image.new("RGBA", (max(1, rect.width), max(1, rect.height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = [0, 0, rect.width - 1, rect.height - 1]

    if gradient is not None:
        painted = draw_gradient(PixelRect(0, 0, rect.width, rect.height), gradient, opacity=1.0)
        mask = Image.new("L", (rect.width, rect.height), 0)
        ImageDraw.Draw(mask).ellipse(box, fill=255)
        layer.paste(painted, (0, 0), mask)
    elif fill is not None and not fill.is_transparent:
        draw.ellipse(box, fill=(fill.r, fill.g, fill.b, int(fill.a * scale)))

    if border_width > 0 and border_color is not None:
        width = max(1, min(int(border_width), min(rect.width, rect.height) // 2 or 1))
        inset = width // 2
        draw.ellipse([inset, inset, rect.width - 1 - inset, rect.height - 1 - inset],
                     outline=(border_color.r, border_color.g, border_color.b, int(border_color.a * scale)),
                     width=width)
    composite_layer(image, layer, (rect.x, rect.y))


def _draw_line(image, rect, colour, opacity) -> None:
    if colour is None:
        return
    scale = max(0.0, min(1.0, opacity))
    layer = Image.new("RGBA", (max(1, rect.width), max(1, rect.height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # A line fills its box: horizontal when the box is wider than tall.
    if rect.width >= rect.height:
        y = rect.height // 2
        draw.line([(0, y), (rect.width, y)],
                  fill=(colour.r, colour.g, colour.b, int(colour.a * scale)),
                  width=max(1, rect.height))
    else:
        x = rect.width // 2
        draw.line([(x, 0), (x, rect.height)],
                  fill=(colour.r, colour.g, colour.b, int(colour.a * scale)),
                  width=max(1, rect.width))
    composite_layer(image, layer, (rect.x, rect.y))


def _draw_polygon(image, points, fill, border_width, border_color, opacity) -> None:
    if fill is None and border_color is None:
        return
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    left, top = min(xs), min(ys)
    width = max(1, max(xs) - left + 1)
    height = max(1, max(ys) - top + 1)
    scale = max(0.0, min(1.0, opacity))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shifted = [(x - left, y - top) for x, y in points]
    if fill is not None:
        draw.polygon(shifted, fill=(fill.r, fill.g, fill.b, int(fill.a * scale)))
    if border_width and border_color is not None:
        draw.line(shifted + [shifted[0]],
                  fill=(border_color.r, border_color.g, border_color.b, int(border_color.a * scale)),
                  width=max(1, int(border_width)), joint="curve")
    composite_layer(image, layer, (int(left), int(top)))


def _draw_arrow(image, rect, fill, opacity) -> None:
    """A right-pointing arrow that fills its box."""
    if fill is None:
        return
    width, height = rect.width, rect.height
    head = max(width // 4, 1)
    shaft = max(height // 3, 1)
    mid_y = height // 2
    points = [
        (0, mid_y - shaft),
        (width - head, mid_y - shaft),
        (width - head, 0),
        (width, mid_y),
        (width - head, height),
        (width - head, mid_y + shaft),
        (0, mid_y + shaft),
    ]
    _draw_polygon(image, [(rect.x + x, rect.y + y) for x, y in points], fill, 0, None, opacity)


def nice_number(value: float, *, round_up: bool = True) -> float:
    """Round an axis maximum to a readable value (1, 2, 2.5, 5 x 10^n).

    Chart axes labelled "43.7" look broken next to bars that clearly reach "44".
    This is the standard axis-rounding trick and keeps labels honest - it rounds
    the *axis*, never the data.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number <= 0:
        return 0.0
    import math

    exponent = math.floor(math.log10(number))
    fraction = number / (10 ** exponent)
    if round_up:
        for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
            if fraction <= candidate + 1e-9:
                return candidate * (10 ** exponent)
        return 10.0 * (10 ** exponent)
    if fraction >= 5.0:
        return 5.0 * (10 ** exponent)
    if fraction >= 2.0:
        return 2.0 * (10 ** exponent)
    return 10 ** exponent
