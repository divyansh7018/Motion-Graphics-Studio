"""Resolution-independent geometry for the scene engine (Stage D).

Nothing in this module knows about a particular video size.  Every position is
a fraction of the frame (``0..1``) and every size is either a fraction of the
frame's **reference dimension** or an explicit pixel value chosen by the user.

The reference dimension is ``min(width, height)``.  Using the shorter side is
what makes one project look right in landscape, portrait and square without
anyone hard-coding ``1080x1920`` anywhere: a title sized ``0.1`` keeps the same
relationship to the *narrow* edge of the frame whichever way round it is.

Conventions used across :mod:`app.scene`:

``normalised``
    Fractions of the frame, ``0..1``, origin top-left.  Resolution independent.
``design``
    Fractions of the reference dimension.  Resolution independent for *sizes*
    (font sizes, paddings, corner radii) so proportions survive a resize.
``pixel``
    Concrete integers for one specific :class:`Canvas`.  Only ever produced at
    layout/render time, never stored in ``project.json``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "MIN_CANVAS_DIMENSION",
    "MAX_CANVAS_DIMENSION",
    "ANCHOR_POINTS",
    "ORIENTATION_LABELS",
    "Canvas",
    "SafeArea",
    "Rect",
    "PixelRect",
    "anchor_point",
    "clamp01",
    "lerp",
    "normalise_size",
]

#: Hard bounds the engine will lay out.  These protect memory, not creativity:
#: a 7680x4320 frame at RGBA is ~127 MB, which is already large for a preview.
#: There is deliberately **no limit on video length** anywhere in Stage D.
MIN_CANVAS_DIMENSION = 64
MAX_CANVAS_DIMENSION = 8192

#: Normalised offset of each named anchor inside a box (directive: 9 anchors).
ANCHOR_POINTS: dict[str, tuple[float, float]] = {
    "top-left": (0.0, 0.0),
    "top": (0.5, 0.0),
    "top-right": (1.0, 0.0),
    "left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0),
    "bottom": (0.5, 1.0),
    "bottom-right": (1.0, 1.0),
}

ORIENTATION_LABELS: dict[str, str] = {
    "landscape": "Landscape",
    "portrait": "Portrait",
    "square": "Square",
}


def clamp01(value: float) -> float:
    """Clamp to the ``0..1`` range; non-numbers become ``0.0``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number):
        return 0.0
    return max(0.0, min(1.0, number))


def lerp(start: float, end: float, fraction: float) -> float:
    """Linear interpolation; ``fraction`` is clamped to ``0..1``."""
    return start + (end - start) * clamp01(fraction)


def anchor_point(anchor: str) -> tuple[float, float]:
    """Normalised offset for an anchor name; unknown names fall back to centre."""
    return ANCHOR_POINTS.get((anchor or "center").strip().lower(), (0.5, 0.5))


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort integer coercion for values that came from a JSON file."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SafeArea:
    """Insets, as fractions of the frame, that platform UI tends to cover.

    Portrait video needs a larger top and bottom inset than landscape because
    phone players stack their controls at the bottom and the notch at the top.
    Everything here is a fraction, so it scales with the frame rather than
    assuming a device.
    """

    top: float = 0.06
    right: float = 0.05
    bottom: float = 0.08
    left: float = 0.05

    @classmethod
    def for_orientation(cls, orientation: str, *, margin: float = 1.0) -> "SafeArea":
        """Sensible defaults for an orientation.  ``margin`` scales all insets."""
        scale = max(0.0, float(margin))
        if orientation == "portrait":
            return cls(top=0.09 * scale, right=0.07 * scale, bottom=0.12 * scale, left=0.07 * scale)
        if orientation == "square":
            return cls(top=0.07 * scale, right=0.07 * scale, bottom=0.10 * scale, left=0.07 * scale)
        return cls(top=0.06 * scale, right=0.05 * scale, bottom=0.08 * scale, left=0.05 * scale)

    @property
    def rect(self) -> "Rect":
        """The safe area as a normalised rectangle."""
        left = clamp01(self.left)
        top = clamp01(self.top)
        width = max(0.0, 1.0 - left - clamp01(self.right))
        height = max(0.0, 1.0 - top - clamp01(self.bottom))
        return Rect(left, top, width, height)

    def clamp(self, rect: "Rect") -> "Rect":
        """Shrink *rect* so it fits inside the safe area, keeping its centre."""
        return self.rect.intersect(rect)

    def to_dict(self) -> dict:
        return {"top": self.top, "right": self.right, "bottom": self.bottom, "left": self.left}


@dataclass(frozen=True)
class Canvas:
    """The frame a scene is laid out into.  Always derived from the project."""

    width: int
    height: int
    fps: int = 30
    #: Reference dimension is recomputed, never stored, so it cannot drift.

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", max(1, int(self.width)))
        object.__setattr__(self, "height", max(1, int(self.height)))
        object.__setattr__(self, "fps", max(1, int(self.fps)))

    # -- construction ----------------------------------------------------

    @classmethod
    def from_format(cls, format_spec: Any, *, fps: Optional[int] = None) -> "Canvas":
        """Build a canvas from a :class:`app.project.model.FormatSpec`.

        A corrupt or hand-edited ``project.json`` can hold anything in these
        fields, so every value is coerced rather than trusted; ``Canvas`` then
        clamps the result to something drawable.
        """
        width = _as_int(getattr(format_spec, "width", 0))
        height = _as_int(getattr(format_spec, "height", 0))
        rate = fps if fps is not None else getattr(format_spec, "fps", 30)
        return cls(width=width, height=height, fps=_as_int(rate) or 30)

    @classmethod
    def from_project(cls, project: Any) -> "Canvas":
        return cls.from_format(getattr(project, "format", None))

    # -- properties ------------------------------------------------------

    @property
    def reference_dimension(self) -> int:
        """The shorter edge.  The unit all *sizes* are measured in."""
        return min(self.width, self.height)

    @property
    def long_dimension(self) -> int:
        return max(self.width, self.height)

    @property
    def aspect(self) -> float:
        """Width divided by height (``1.777`` for 16:9)."""
        return self.width / self.height

    @property
    def orientation(self) -> str:
        if self.width > self.height:
            return "landscape"
        if self.height > self.width:
            return "portrait"
        return "square"

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def megapixels(self) -> float:
        return round(self.pixels / 1_000_000, 3)

    @property
    def frame_seconds(self) -> float:
        """Length of one frame in seconds."""
        return 1.0 / self.fps

    @property
    def is_extreme(self) -> bool:
        """True when the frame is unusually large (worth warning the user)."""
        return self.width > MAX_CANVAS_DIMENSION or self.height > MAX_CANVAS_DIMENSION

    def safe_area(self, *, margin: float = 1.0) -> SafeArea:
        return SafeArea.for_orientation(self.orientation, margin=margin)

    # -- unit conversion -------------------------------------------------

    def scale(self, fraction: float) -> float:
        """A design-space fraction of the reference dimension, in pixels."""
        try:
            value = float(fraction)
        except (TypeError, ValueError):
            return 0.0
        return value * self.reference_dimension

    def px(self, fx: float, fy: float) -> tuple[int, int]:
        """Normalised coordinates to integer pixels."""
        return int(round(clamp01(fx) * self.width)), int(round(clamp01(fy) * self.height))

    def rect_pixels(self, rect: "Rect") -> "PixelRect":
        """A normalised rectangle to integer pixels, clipped to the frame."""
        left = clamp01(rect.x) * self.width
        top = clamp01(rect.y) * self.height
        right = clamp01(rect.x + rect.width) * self.width
        bottom = clamp01(rect.y + rect.height) * self.height
        x0 = int(math.floor(min(left, right)))
        y0 = int(math.floor(min(top, bottom)))
        x1 = int(math.ceil(max(left, right)))
        y1 = int(math.ceil(max(top, bottom)))
        x0 = max(0, min(self.width, x0))
        y0 = max(0, min(self.height, y0))
        x1 = max(0, min(self.width, x1))
        y1 = max(0, min(self.height, y1))
        return PixelRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def scaled(self, *, width: Optional[int] = None, height: Optional[int] = None,
               fps: Optional[int] = None) -> "Canvas":
        """A copy with some dimensions replaced (used for preview thumbnails)."""
        return Canvas(
            width=int(width) if width else self.width,
            height=int(height) if height else self.height,
            fps=int(fps) if fps else self.fps,
        )

    def thumbnail(self, target_long_edge: int) -> "Canvas":
        """The same aspect ratio fitted inside ``target_long_edge``."""
        target = max(16, int(target_long_edge))
        long_edge = self.long_dimension
        ratio = target / long_edge if long_edge else 1.0
        return Canvas(
            width=max(2, int(round(self.width * ratio))),
            height=max(2, int(round(self.height * ratio))),
            fps=self.fps,
        )

    def aspect_label(self) -> str:
        """``16:9`` style label, reduced exactly when possible."""
        divisor = math.gcd(self.width, self.height) or 1
        return f"{self.width // divisor}:{self.height // divisor}"

    def describe(self) -> str:
        return f"{self.width}x{self.height} @ {self.fps} fps ({self.aspect_label()}, {ORIENTATION_LABELS[self.orientation]})"


@dataclass(frozen=True)
class Rect:
    """A normalised rectangle: origin plus size, both fractions of the frame."""

    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    # -- constructors ----------------------------------------------------

    @classmethod
    def full(cls) -> "Rect":
        return cls(0.0, 0.0, 1.0, 1.0)

    @classmethod
    def from_center(cls, cx: float, cy: float, width: float, height: float) -> "Rect":
        return cls(cx - width / 2.0, cy - height / 2.0, width, height)

    @classmethod
    def from_edges(cls, x0: float, y0: float, x1: float, y1: float) -> "Rect":
        return cls(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    # -- geometry --------------------------------------------------------

    @property
    def x1(self) -> float:
        return self.x + self.width

    @property
    def y1(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0.0 or self.height <= 0.0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def point(self, anchor: str) -> tuple[float, float]:
        """The normalised coordinates of one of the nine anchors."""
        ax, ay = anchor_point(anchor)
        return self.x + ax * self.width, self.y + ay * self.height

    def inset(self, left: float, top: float, right: float, bottom: float) -> "Rect":
        """Shrink (positive) or grow (negative) each edge, as fractions."""
        return Rect(
            self.x + left,
            self.y + top,
            max(0.0, self.width - left - right),
            max(0.0, self.height - top - bottom),
        )._clamp_inside()

    def offset(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.width, self.height)

    def intersect(self, other: "Rect") -> "Rect":
        """The overlap of two rectangles (empty when they do not touch)."""
        x0 = max(self.x, other.x)
        y0 = max(self.y, other.y)
        x1 = min(self.x1, other.x1)
        y1 = min(self.y1, other.y1)
        if x1 <= x0 or y1 <= y0:
            return Rect(x0, y0, 0.0, 0.0)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def overlaps(self, other: "Rect") -> bool:
        return not self.intersect(other).is_empty

    def contains_point(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x1 and self.y <= y <= self.y1

    def union(self, other: "Rect") -> "Rect":
        if other.is_empty:
            return self
        if self.is_empty:
            return other
        x0 = min(self.x, other.x)
        y0 = min(self.y, other.y)
        return Rect.from_edges(x0, y0, max(self.x1, other.x1), max(self.y1, other.y1))

    def _clamp_inside(self) -> "Rect":
        """Keep the rectangle inside the frame (used after insets)."""
        x = clamp01(self.x)
        y = clamp01(self.y)
        return Rect(x, y, max(0.0, min(self.width, 1.0 - x)), max(0.0, min(self.height, 1.0 - y)))

    def rounded(self, digits: int = 5) -> "Rect":
        return Rect(round(self.x, digits), round(self.y, digits),
                    round(self.width, digits), round(self.height, digits))

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class PixelRect:
    """A rectangle in concrete pixels for one canvas.  Produced, never stored."""

    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def x1(self) -> int:
        return self.x + self.width

    @property
    def y1(self) -> int:
        return self.y + self.height

    @property
    def box(self) -> tuple[int, int, int, int]:
        """Pillow's ``(left, top, right, bottom)`` form."""
        return self.x, self.y, self.x + self.width, self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def inset(self, left: int, top: int, right: int, bottom: int) -> "PixelRect":
        return PixelRect(
            self.x + left,
            self.y + top,
            max(0, self.width - left - right),
            max(0, self.height - top - bottom),
        )

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def normalise_size(size: Any) -> dict:
    """Coerce an element ``size`` dict into a predictable shape.

    Stage B stores ``size`` as a free-form dict (``{"mode": ..., "value": ...}``)
    so this accepts the old form, the explicit ``width``/``height`` form and
    plain numbers, and always returns both axes.  Unknown keys are preserved by
    the caller in ``extra``; this function only reads.
    """
    data: dict = dict(size) if isinstance(size, dict) else {}
    mode = str(data.get("mode", "") or "").strip().lower()
    value = data.get("value", None)

    def axis(raw: Any) -> dict:
        if isinstance(raw, dict):
            return {
                "mode": str(raw.get("mode", "fraction") or "fraction").strip().lower(),
                "value": raw.get("value", 0.5),
            }
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return {"mode": "fraction", "value": float(raw)}
        return {"mode": "auto", "value": 0.0}

    if "width" in data or "height" in data:
        return {"width": axis(data.get("width")), "height": axis(data.get("height"))}

    # Legacy / simple form: one value applied to both axes.
    if mode in ("fraction", "relative"):
        try:
            fraction = float(value)
        except (TypeError, ValueError):
            fraction = 0.5
        return {
            "width": {"mode": "fraction", "value": fraction},
            "height": {"mode": "auto", "value": 0.0},
        }
    if mode == "pixels":
        try:
            pixels = float(value)
        except (TypeError, ValueError):
            pixels = 0.0
        return {
            "width": {"mode": "pixels", "value": pixels},
            "height": {"mode": "auto", "value": 0.0},
        }
    if mode == "fill":
        return {"width": {"mode": "fill", "value": 0.0}, "height": {"mode": "fill", "value": 0.0}}
    return {"width": {"mode": "auto", "value": 0.0}, "height": {"mode": "auto", "value": 0.0}}
