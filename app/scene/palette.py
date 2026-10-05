"""Colours for the scene engine.

Handles ``#rgb``, ``#rrggbb``, ``#rrggbbaa``, ``rgb()``/``rgba()`` CSS strings,
named colours and Pillow colour tuples.  Every parse returns a *result object*
that says whether it succeeded, because a scene must render with a visible
fallback and a clear message rather than crash on a typo in ``project.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

__all__ = [
    "Color",
    "Gradient",
    "NAMED_COLORS",
    "parse_color",
    "mix",
    "with_alpha",
    "luminance",
    "readable_on",
    "gradient_colors",
]

RGBA = tuple[int, int, int, int]

#: A small set of names users actually type.  Anything unknown is reported.
NAMED_COLORS: dict[str, RGBA] = {
    "white": (255, 255, 255, 255),
    "black": (0, 0, 0, 255),
    "red": (224, 64, 64, 255),
    "green": (64, 176, 96, 255),
    "blue": (64, 128, 224, 255),
    "yellow": (240, 200, 64, 255),
    "orange": (240, 148, 64, 255),
    "purple": (160, 96, 224, 255),
    "pink": (240, 128, 176, 255),
    "grey": (160, 160, 160, 255),
    "gray": (160, 160, 160, 255),
    "transparent": (0, 0, 0, 0),
    "none": (0, 0, 0, 0),
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3,8})$")
_FUNC_RE = re.compile(r"^(rgba?)\s*\(([^)]*)\)$", re.IGNORECASE)


@dataclass(frozen=True)
class Color:
    """An RGBA colour plus how it was obtained."""

    r: int = 0
    g: int = 0
    b: int = 0
    a: int = 255
    #: Set when the input was not understood and a fallback was used.
    invalid: bool = False
    source: str = ""

    @property
    def tuple(self) -> RGBA:
        return self.r, self.g, self.b, self.a

    @property
    def rgb(self) -> tuple[int, int, int]:
        return self.r, self.g, self.b

    @property
    def alpha(self) -> float:
        return self.a / 255.0

    @property
    def is_transparent(self) -> bool:
        return self.a == 0

    def hex(self) -> str:
        if self.a >= 255:
            return f"#{self.r:02x}{self.g:02x}{self.b:02x}"
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}{self.a:02x}"

    def describe(self) -> str:
        return self.hex() if not self.invalid else f"{self.hex()} (fallback for '{self.source}')"


def _channel(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number <= 1.0 and isinstance(value, float):
        number *= 255.0
    return max(0, min(255, int(round(number))))


def parse_color(value: Any, fallback: Union[RGBA, "Color", str, None] = None) -> Color:
    """Parse a colour from any of the supported spellings.

    Returns a :class:`Color`; when the input cannot be understood the result has
    ``invalid=True`` and carries the fallback, so callers can both render and
    explain.
    """
    raw = value
    if isinstance(raw, Color):
        return raw
    if isinstance(raw, (tuple, list)) and len(raw) in (3, 4):
        channels = [_channel(c) for c in raw[:3]]
        alpha = _channel(raw[3]) if len(raw) == 4 else 255
        return Color(*channels, alpha)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return Color((raw >> 16) & 255, (raw >> 8) & 255, raw & 255, 255)

    text = str(raw or "").strip()
    if not text:
        return _fallback_color(fallback, text)

    lowered = text.lower()
    if lowered in NAMED_COLORS:
        return Color(*NAMED_COLORS[lowered])

    match = _FUNC_RE.match(text)
    if match:
        parts = [p.strip() for p in match.group(2).replace("/", " ").split(",") if p.strip()]
        parts = [p for p in parts if p]
        if len(parts) >= 3:
            alpha = _channel(parts[3]) if len(parts) >= 4 else 255
            return Color(_channel(parts[0]), _channel(parts[1]), _channel(parts[2]), alpha)

    hex_match = _HEX_RE.match(text)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) == 3:
            return Color(*(int(c * 2, 16) for c in digits), 255)
        if len(digits) == 4:
            return Color(*(int(c * 2, 16) for c in digits[:3]), int(digits[3] * 2, 16))
        if len(digits) == 6:
            return Color(int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), 255)
        if len(digits) == 8:
            return Color(int(digits[0:2], 16), int(digits[2:4], 16),
                         int(digits[4:6], 16), int(digits[6:8], 16))

    return _fallback_color(fallback, text)


def _fallback_color(fallback: Any, source: str) -> Color:
    if fallback is None:
        return Color(255, 0, 255, 255, invalid=True, source=source)  # obvious magenta
    resolved = fallback if isinstance(fallback, Color) else parse_color(fallback)
    return Color(resolved.r, resolved.g, resolved.b, resolved.a, invalid=True, source=source)


def with_alpha(color: Color, alpha: float) -> Color:
    """The same colour at a different opacity (``alpha`` 0..1)."""
    return Color(color.r, color.g, color.b, max(0, min(255, int(round(clamp01(alpha) * 255)))))


def clamp01(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def mix(a: Color, b: Color, fraction: float) -> Color:
    """Blend two colours; ``fraction`` is clamped to ``0..1``."""
    t = clamp01(fraction)
    return Color(
        int(round(a.r + (b.r - a.r) * t)),
        int(round(a.g + (b.g - a.g) * t)),
        int(round(a.b + (b.b - a.b) * t)),
        int(round(a.a + (b.a - a.a) * t)),
    )


def luminance(color: Color) -> float:
    """Perceived brightness, ``0`` (black) to ``1`` (white)."""
    def linear(channel: int) -> float:
        scaled = channel / 255.0
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(color.r) + 0.7152 * linear(color.g) + 0.0722 * linear(color.b)


def readable_on(background: Color, *, light: RGBA = (255, 255, 255, 255),
                dark: RGBA = (16, 16, 20, 255)) -> Color:
    """Black-ish or white-ish text, whichever is legible on *background*."""
    return Color(*light) if luminance(background) < 0.4 else Color(*dark)


@dataclass(frozen=True)
class Gradient:
    """A two-or-more-stop gradient, used for scene and card backgrounds."""

    stops: tuple[RGBA, ...] = ((16, 16, 20, 255), (32, 32, 44, 255))
    #: Angle in degrees; 0 points down, 90 points right.
    angle: float = 90.0

    @classmethod
    def from_value(cls, value: Any, fallback: Optional[Color] = None) -> Optional["Gradient"]:
        """Parse ``["#111", "#333"]`` or ``{"stops": [...], "angle": 45}``."""
        if value is None or isinstance(value, (str, int)):
            return None
        stops_raw: Sequence[Any]
        angle = 90.0
        if isinstance(value, dict):
            stops_raw = value.get("stops") or []
            angle = float(value.get("angle", 90.0) or 0.0)
        elif isinstance(value, (list, tuple)):
            stops_raw = value
        else:
            return None
        stops = tuple(parse_color(stop, fallback).tuple for stop in stops_raw if stop)
        if len(stops) < 2:
            return None
        return cls(stops=stops, angle=angle)

    def color_at(self, fraction: float) -> Color:
        """The gradient colour at ``fraction`` (0..1) along its axis."""
        t = clamp01(fraction)
        if len(self.stops) == 1:
            return Color(*self.stops[0])
        scaled = t * (len(self.stops) - 1)
        index = min(len(self.stops) - 2, int(scaled))
        local = scaled - index
        return mix(Color(*self.stops[index]), Color(*self.stops[index + 1]), local)


def gradient_colors(gradient: Gradient, length: int) -> list[RGBA]:
    """Sample a gradient into ``length`` colours (used to build a fill strip)."""
    count = max(1, int(length))
    if count == 1:
        return [gradient.color_at(0.0).tuple]
    return [gradient.color_at(i / (count - 1)).tuple for i in range(count)]
