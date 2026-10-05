"""Chart drawing for the scene engine.

Charts are drawn from the pixel box the layout pass produced, so the same data
renders correctly in a full-width landscape panel or a narrow portrait card.
Font sizes, bar gaps and line widths are all derived from the box, never
hard-coded.

The data is never altered.  Axis *rounding* applies only to the axis maximum
(:func:`app.scene.shapes.nice_number`) so gridlines read as round numbers; the
bars themselves are plotted at their true values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from PIL import Image, ImageDraw

from .canvas import PixelRect
from .palette import Color, parse_color
from .shapes import composite_layer, nice_number

__all__ = ["ChartStyle", "draw_chart", "chart_value_range", "DEFAULT_CHART_COLORS"]

#: A categorical palette with enough contrast for colour-blind viewers.
DEFAULT_CHART_COLORS: tuple[tuple[int, int, int, int], ...] = (
    (79, 140, 255, 255),
    (96, 200, 140, 255),
    (240, 176, 80, 255),
    (224, 96, 128, 255),
    (160, 128, 224, 255),
    (80, 192, 208, 255),
    (208, 144, 96, 255),
    (144, 160, 176, 255),
)

#: Below this many pixels per category, labels would overlap and are dropped.
MIN_LABEL_SLOT = 26


@dataclass
class ChartStyle:
    """Look-and-feel for one chart.  All sizes are fractions of the box."""

    colors: list = field(default_factory=lambda: list(DEFAULT_CHART_COLORS))
    grid_color: Color = field(default_factory=lambda: Color(255, 255, 255, 40))
    axis_color: Color = field(default_factory=lambda: Color(255, 255, 255, 120))
    label_color: Color = field(default_factory=lambda: Color(230, 230, 235, 255))
    value_color: Color = field(default_factory=lambda: Color(255, 255, 255, 255))
    background: Optional[Color] = None
    show_values: bool = True
    show_labels: bool = True
    show_grid: bool = True
    #: Fraction of each slot left as a gap between bars.
    bar_gap: float = 0.30
    #: Line thickness as a fraction of the smaller box dimension.
    line_width: float = 0.012
    #: Fraction of the box height reserved for category labels.
    label_band: float = 0.18
    grid_lines: int = 4
    opacity: float = 1.0

    @classmethod
    def from_element(cls, extra: dict, palette: Optional[dict] = None) -> "ChartStyle":
        """Build a style from an element's ``extra`` dict."""
        extra = extra or {}
        palette = palette or {}
        style = cls()
        raw_colors = extra.get("colors")
        if isinstance(raw_colors, (list, tuple)) and raw_colors:
            style.colors = [parse_color(c, DEFAULT_CHART_COLORS[0]).tuple for c in raw_colors]
        for role, attribute in (("grid_color", "grid_color"), ("axis_color", "axis_color"),
                                ("label_color", "label_color"), ("value_color", "value_color"),
                                ("background", "background")):
            value = extra.get(role, palette.get(attribute))
            if value not in (None, ""):
                setattr(style, attribute, parse_color(value))
        for flag in ("show_values", "show_labels", "show_grid"):
            if flag in extra:
                setattr(style, flag, bool(extra[flag]))
        for name, default, low, high in (
            ("bar_gap", 0.30, 0.0, 0.9),
            ("line_width", 0.012, 0.001, 0.2),
            ("label_band", 0.18, 0.0, 0.6),
            ("opacity", 1.0, 0.0, 1.0),
        ):
            if name in extra:
                try:
                    setattr(style, name, max(low, min(high, float(extra[name]))))
                except (TypeError, ValueError):
                    setattr(style, name, default)
        try:
            style.grid_lines = max(0, min(12, int(extra.get("grid_lines", style.grid_lines))))
        except (TypeError, ValueError):
            pass
        return style

    def color_for(self, index: int) -> tuple:
        if not self.colors:
            return DEFAULT_CHART_COLORS[0]
        return self.colors[index % len(self.colors)]


def chart_value_range(values: Sequence[float]) -> tuple[float, float]:
    """The axis range for a dataset, including negatives.

    Returns ``(minimum, maximum)`` rounded to readable values.  A flat or empty
    dataset still produces a usable axis instead of dividing by zero.
    """
    numbers = [float(v) for v in values if _is_number(v)]
    if not numbers:
        return 0.0, 1.0
    low = min(numbers)
    high = max(numbers)
    if low == high:
        if high == 0:
            return 0.0, 1.0
        return 0.0, nice_number(high * 1.1)
    if low >= 0:
        return 0.0, nice_number(high * 1.05)
    if high <= 0:
        return -nice_number(-low * 1.05), 0.0
    return -nice_number(-low * 1.05), nice_number(high * 1.05)


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def draw_chart(image: Image.Image, rect: PixelRect, kind: str, values: Sequence[float],
               labels: Sequence[str], style: Optional[ChartStyle] = None,
               font_factory: Optional[Callable[[float], Any]] = None) -> None:
    """Draw a chart into *image*.  Unknown kinds fall back to a bar chart."""
    if rect.is_empty or not values:
        return
    style = style or ChartStyle()
    kind = (kind or "bar").lower()

    if style.background is not None:
        layer = Image.new("RGBA", (rect.width, rect.height),
                          (style.background.r, style.background.g, style.background.b,
                           int(style.background.a * style.opacity)))
        composite_layer(image, layer, (rect.x, rect.y))

    if kind in ("pie", "donut"):
        _draw_pie(image, rect, kind, values, labels, style, font_factory)
        return
    if kind in ("line", "area"):
        _draw_line_chart(image, rect, kind, values, labels, style, font_factory)
        return
    if kind == "sparkline":
        _draw_sparkline(image, rect, values, style)
        return
    if kind == "column":
        _draw_columns(image, rect, values, labels, style, font_factory)
        return
    _draw_bars(image, rect, values, labels, style, font_factory)


# --------------------------------------------------------------------------
# Bars and columns
# --------------------------------------------------------------------------

def _plot_area(rect: PixelRect, style: ChartStyle, *, label_band: bool) -> PixelRect:
    """The area the data is drawn in, after reserving room for labels."""
    band = int(rect.height * style.label_band) if label_band else 0
    top_pad = max(1, int(rect.height * 0.08)) if style.show_values else 0
    return PixelRect(rect.x, rect.y + top_pad, rect.width, max(1, rect.height - band - top_pad))


def _y_for(value: float, low: float, high: float, area: PixelRect) -> float:
    """Map a data value to a y coordinate inside *area* (top = high)."""
    span = (high - low) or 1.0
    fraction = (high - value) / span
    return area.y + fraction * area.height


def _draw_grid(area: PixelRect, low: float, high: float, style: ChartStyle, draw) -> None:
    """Horizontal gridlines at evenly spaced values.

    Only the lines are drawn: the value each line represents is already shown
    on the bars and columns themselves, so a second set of numbers would be
    noise.  (``low``/``high`` are kept in the signature so a future axis-label
    pass has the range it needs.)
    """
    if not style.show_grid or style.grid_lines <= 0:
        return
    _ = (low, high)
    for index in range(style.grid_lines + 1):
        y = int(area.y + (index / style.grid_lines) * area.height)
        draw.line([(area.x, y), (area.x1, y)],
                  fill=(style.grid_color.r, style.grid_color.g, style.grid_color.b,
                        int(style.grid_color.a * style.opacity)),
                  width=1)


def _format_tick(value: float) -> str:
    if value == int(value):
        return f"{int(value)}"
    return f"{value:.1f}"


def _draw_bars(image, rect, values, labels, style, font_factory) -> None:
    """Horizontal bars - the readable choice in a narrow portrait frame."""
    area = _plot_area(rect, style, label_band=False)
    low, high = chart_value_range(values)
    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    local = area.offset(-rect.x, -rect.y)

    _draw_grid(local, low, high, style, draw)

    slot = area.height / len(values)
    thickness = max(1.0, slot * (1.0 - style.bar_gap))
    zero_x = local.x + ((0 - low) / ((high - low) or 1.0)) * local.width
    label_font = font_factory(max(6, slot * 0.55)) if font_factory else None

    for index, value in enumerate(values):
        if not _is_number(value):
            continue
        top = local.y + index * slot + (slot - thickness) / 2
        span = (high - low) or 1.0
        value_x = local.x + ((float(value) - low) / span) * local.width
        left, right = sorted((zero_x, value_x))
        draw.rectangle([left, top, max(right, left + 1), top + thickness],
                       fill=_with_alpha(style.color_for(index), style.opacity))
        if label_font is not None:
            label = labels[index] if index < len(labels) else ""
            if label:
                draw.text((local.x + 2, top + thickness / 2 - _half_height(label_font)), label,
                          font=label_font,
                          fill=(style.label_color.r, style.label_color.g, style.label_color.b,
                                int(style.label_color.a * style.opacity)))
        if style.show_values and font_factory is not None:
            text = _format_tick(float(value))
            value_font = font_factory(max(6, slot * 0.45))
            draw.text((min(right + 4, local.x1 - _text_width(value_font, text) - 2),
                       top + thickness / 2 - _half_height(value_font)),
                      text, font=value_font,
                      fill=(style.value_color.r, style.value_color.g, style.value_color.b,
                            int(style.value_color.a * style.opacity)))

    composite_layer(image, layer, (rect.x, rect.y))


def _draw_columns(image, rect, values, labels, style, font_factory) -> None:
    """Vertical columns with category labels underneath."""
    area = _plot_area(rect, style, label_band=True)
    low, high = chart_value_range(values)
    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    local = area.offset(-rect.x, -rect.y)

    _draw_grid(local, low, high, style, draw)

    slot = local.width / len(values)
    thickness = max(1.0, slot * (1.0 - style.bar_gap))
    span = (high - low) or 1.0
    zero_y = local.y + ((high - 0) / span) * local.height
    label_slot = slot
    show_labels = style.show_labels and label_slot >= MIN_LABEL_SLOT
    label_font = font_factory(max(6, min(label_slot * 0.8, local.height * 0.16))) if (font_factory and show_labels) else None
    value_font = font_factory(max(6, min(thickness * 0.6, local.height * 0.14))) if (font_factory and style.show_values) else None

    for index, value in enumerate(values):
        if not _is_number(value):
            continue
        left = local.x + index * slot + (slot - thickness) / 2
        value_y = local.y + ((high - float(value)) / span) * local.height
        top, bottom = sorted((zero_y, value_y))
        draw.rectangle([left, top, left + thickness, max(bottom, top + 1)],
                       fill=_with_alpha(style.color_for(index), style.opacity))

        if value_font is not None:
            text = _format_tick(float(value))
            width = _text_width(value_font, text)
            draw.text((left + thickness / 2 - width / 2, max(0, top - _line_height(value_font) - 1)),
                      text, font=value_font,
                      fill=(style.value_color.r, style.value_color.g, style.value_color.b,
                            int(style.value_color.a * style.opacity)))

        if label_font is not None and index < len(labels) and labels[index]:
            text = _clip(label_font, labels[index], label_slot - 2)
            width = _text_width(label_font, text)
            draw.text((left + thickness / 2 - width / 2, local.y1 + 2), text, font=label_font,
                      fill=(style.label_color.r, style.label_color.g, style.label_color.b,
                            int(style.label_color.a * style.opacity)))

    composite_layer(image, layer, (rect.x, rect.y))


# --------------------------------------------------------------------------
# Line, area, sparkline
# --------------------------------------------------------------------------

def _draw_line_chart(image, rect, kind, values, labels, style, font_factory) -> None:
    area = _plot_area(rect, style, label_band=bool(labels) and style.show_labels)
    low, high = chart_value_range(values)
    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    local = area.offset(-rect.x, -rect.y)
    _draw_grid(local, low, high, style, draw)

    span = (high - low) or 1.0
    step = local.width / max(1, len(values) - 1) if len(values) > 1 else 0.0
    points = []
    for index, value in enumerate(values):
        if not _is_number(value):
            continue
        x = local.x + (index * step if step else local.width / 2)
        y = local.y + ((high - float(value)) / span) * local.height
        points.append((x, y))

    if len(points) >= 2:
        colour = _with_alpha(style.color_for(0), style.opacity)
        width = max(1, int(min(local.width, local.height) * style.line_width))
        if kind == "area":
            baseline = local.y + ((high - max(0.0, low)) / span) * local.height
            polygon = points + [(points[-1][0], baseline), (points[0][0], baseline)]
            fill_colour = tuple(list(colour[:3]) + [int(colour[3] * 0.28)])
            draw.polygon(polygon, fill=fill_colour)
        draw.line(points, fill=colour, width=width, joint="curve")
        marker = max(2, width)
        for x, y in points:
            draw.ellipse([x - marker, y - marker, x + marker, y + marker], fill=colour)

    if font_factory is not None and labels and style.show_labels and local.width / max(1, len(labels)) >= MIN_LABEL_SLOT:
        font = font_factory(max(6, local.height * 0.14))
        for index, label in enumerate(labels):
            if index >= len(points) or not label:
                continue
            x = points[index][0]
            text = _clip(font, label, local.width / max(1, len(labels)) - 2)
            draw.text((x - _text_width(font, text) / 2, local.y1 + 2), text, font=font,
                      fill=(style.label_color.r, style.label_color.g, style.label_color.b,
                            int(style.label_color.a * style.opacity)))

    composite_layer(image, layer, (rect.x, rect.y))


def _draw_sparkline(image, rect, values, style) -> None:
    """A trend line with no axes, for small cards."""
    numbers = [float(v) for v in values if _is_number(v)]
    if len(numbers) < 2:
        return
    low, high = min(numbers), max(numbers)
    span = (high - low) or 1.0
    step = rect.width / (len(numbers) - 1)
    points = [(rect.x + i * step, rect.y + (1 - (v - low) / span) * rect.height)
              for i, v in enumerate(numbers)]
    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shifted = [(x - rect.x, y - rect.y) for x, y in points]
    width = max(1, int(min(rect.width, rect.height) * style.line_width))
    draw.line(shifted, fill=_with_alpha(style.color_for(0), style.opacity), width=width, joint="curve")
    composite_layer(image, layer, (rect.x, rect.y))


# --------------------------------------------------------------------------
# Pie and donut
# --------------------------------------------------------------------------

def _draw_pie(image, rect, kind, values, labels, style, font_factory) -> None:
    numbers = [float(v) if _is_number(v) else 0.0 for v in values]
    total = sum(n for n in numbers if n > 0)
    if total <= 0:
        return
    side = min(rect.width, rect.height)
    # Coordinates are local to the scratch layer, which is pasted at rect's origin.
    left = (rect.width - side) // 2
    top = (rect.height - side) // 2
    box = [left, top, left + side, top + side]

    layer = Image.new("RGBA", (rect.width, rect.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    start = -90.0
    positive = [v for v in numbers if v > 0]
    for index, value in enumerate(numbers):
        if value <= 0:
            continue
        sweep = value / total * 360.0
        fill = _with_alpha(style.color_for(index), style.opacity)
        if len(positive) == 1:
            # A single slice is a whole circle; pieslice(360 deg) is unreliable.
            draw.ellipse(box, fill=fill)
        else:
            draw.pieslice(box, start=start, end=start + sweep, fill=fill)
        start += sweep

    if kind == "donut":
        hole = side * 0.55
        centre_x = left + side / 2
        centre_y = top + side / 2
        draw.ellipse([centre_x - hole / 2, centre_y - hole / 2,
                      centre_x + hole / 2, centre_y + hole / 2], fill=(0, 0, 0, 0))

    if font_factory is not None and style.show_labels and labels:
        font = font_factory(max(6, side * 0.07))
        legend_y = 2
        for index, value in enumerate(numbers):
            if value <= 0 or index >= len(labels) or not labels[index]:
                continue
            if legend_y + _line_height(font) > rect.height:
                break
            swatch = max(4, int(side * 0.04))
            draw.rectangle([2, legend_y, 2 + swatch, legend_y + swatch],
                           fill=_with_alpha(style.color_for(index), style.opacity))
            text = _clip(font, f"{labels[index]} ({value / total * 100:.0f}%)", rect.width - swatch - 8)
            draw.text((6 + swatch, legend_y), text, font=font,
                      fill=(style.label_color.r, style.label_color.g, style.label_color.b,
                            int(style.label_color.a * style.opacity)))
            legend_y += swatch + max(2, int(side * 0.02))

    composite_layer(image, layer, (rect.x, rect.y))


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def _with_alpha(colour: tuple, opacity: float) -> tuple:
    scale = max(0.0, min(1.0, opacity))
    r, g, b = colour[0], colour[1], colour[2]
    a = colour[3] if len(colour) > 3 else 255
    return (r, g, b, int(a * scale))


def _text_width(font: Any, text: str) -> int:
    try:
        box = font.getbbox(text)
    except (OSError, ValueError):
        return 0
    return box[2] - box[0]


def _line_height(font: Any) -> int:
    try:
        ascent, descent = font.getmetrics()
    except (OSError, ValueError, AttributeError):
        return 12
    return max(1, int(ascent + descent))


def _half_height(font: Any) -> int:
    return _line_height(font) // 2


def _clip(font: Any, text: str, max_width: float) -> str:
    """Trim a label with an ellipsis so it cannot overrun its slot."""
    if max_width <= 0 or _text_width(font, text) <= max_width:
        return text
    trimmed = text
    while trimmed and _text_width(font, trimmed + "\u2026") > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "\u2026") if trimmed else ""
