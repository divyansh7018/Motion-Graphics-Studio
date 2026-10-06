"""Transitions foundation (Stage D).

A transition blends two already-rendered frames, so it knows nothing about
scenes, elements or timing - that separation is what makes it reusable by the
preview, the storyboard and (later) the renderer.

Everything here is CPU-only and works on a single frame pair.  There is no
hidden state, no thread and no temporary file: ``blend(a, b, fraction, kind)``
returns an image and touches nothing else.

``none`` and ``cut`` are not the same thing.  ``none`` means "this scene has no
transition of its own"; ``cut`` means "deliberately hard-cut here", which stops
an adjacent cross-fade from bleeding into it.
"""

from __future__ import annotations

from typing import Optional

from PIL import Image

from .canvas import Canvas

__all__ = [
    "ENGINE_TRANSITIONS",
    "TRANSITION_LABELS",
    "is_known_transition",
    "transition_duration",
    "blend",
    "crossfade",
]

#: Transitions the engine can draw.  The project model's ``TRANSITION_TYPES``
#: is a subset of these - the model lists what may be *stored*, this lists what
#: may be *drawn*.
ENGINE_TRANSITIONS: tuple[str, ...] = (
    "none", "cut", "fade", "slide", "zoom", "wipe", "push", "dip", "dip white",
)

TRANSITION_LABELS: dict[str, str] = {
    "none": "None",
    "cut": "Hard cut",
    "fade": "Cross-fade",
    "slide": "Slide in",
    "zoom": "Zoom in",
    "wipe": "Wipe",
    "push": "Push",
    "dip": "Dip to black",
    "dip white": "Dip to white",
}


def is_known_transition(kind: str) -> bool:
    return (kind or "none").strip().lower() in ENGINE_TRANSITIONS


def transition_duration(kind: str, requested: float, *, clamp: float = 2.0) -> float:
    """The length a transition will actually run for.

    A cross-fade of 0 seconds is a hard cut, so ``none``/``cut`` always resolve
    to 0 regardless of what the project says; the others are clamped so a
    hand-edited project cannot ask for a ten second fade.
    """
    name = (kind or "none").strip().lower()
    if name in ("none", "cut"):
        return 0.0
    try:
        value = float(requested)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return 0.0
    return max(0.05, min(float(clamp), value))


def blend(first: Image.Image, second: Image.Image, fraction: float, kind: str = "fade",
          canvas: Optional[Canvas] = None) -> Image.Image:
    """Blend *first* into *second* at ``fraction`` (0 = all first, 1 = all second).

    Unknown kinds degrade to a cross-fade rather than raising, because a
    preview that refuses to draw is worse than one that draws something close.
    """
    a = _as_rgba(first)
    b = _as_rgba(second)
    if b.size != a.size:
        b = b.resize(a.size)

    t = max(0.0, min(1.0, float(fraction)))
    name = (kind or "fade").strip().lower()

    if name == "none" or name == "cut":
        return a.copy() if t < 0.5 else b.copy()
    if name == "fade":
        return crossfade(a, b, t)
    if name == "slide":
        return _slide(a, b, t)
    if name == "push":
        return _push(a, b, t)
    if name == "zoom":
        return _zoom(a, b, t)
    if name == "wipe":
        return _wipe(a, b, t)
    if name == "dip":
        return _dip(a, b, t)
    if name in ("dip white", "dip_white", "fade to white", "fade white"):
        return _dip_white(a, b, t)
    return crossfade(a, b, t)


def crossfade(first: Image.Image, second: Image.Image, fraction: float) -> Image.Image:
    """A straight alpha blend of two frames."""
    return Image.blend(first, second, max(0.0, min(1.0, float(fraction))))


def _as_rgba(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGBA" else image.convert("RGBA")


def _slide(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """The incoming frame slides in from the right over the outgoing one."""
    width, height = first.size
    offset = int(round(width * (1.0 - t)))
    frame = first.copy()
    if offset < width:
        visible = second.crop((0, 0, width - offset, height))
        frame.paste(visible, (offset, 0), visible)
    return frame


def _push(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """Both frames move: the old one leaves as the new one arrives."""
    width, height = first.size
    offset = int(round(width * t))
    frame = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    if offset < width:
        frame.paste(first.crop((offset, 0, width, height)), (0, 0))
    if offset > 0:
        frame.paste(second.crop((0, 0, offset, height)), (width - offset, 0))
    return frame


def _zoom(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """The incoming frame grows from 90% while fading in."""
    width, height = first.size
    scale = 0.9 + 0.1 * t
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    grown = second.resize(new_size, Image.BILINEAR)
    left = (width - new_size[0]) // 2
    top = (height - new_size[1]) // 2
    frame = first.copy()
    frame.paste(grown, (left, top), grown)
    return crossfade(first, frame, t)


def _wipe(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """A vertical edge sweeps across revealing the incoming frame."""
    width, height = first.size
    revealed = max(0, min(width, int(round(width * t))))
    frame = first.copy()
    if revealed > 0:
        frame.paste(second.crop((0, 0, revealed, height)), (0, 0))
    return frame


def _dip(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """Fade to black halfway, then up from black - hides a hard scene change."""
    black = Image.new("RGBA", first.size, (0, 0, 0, 255))
    if t < 0.5:
        return crossfade(first, black, t / 0.5)
    return crossfade(black, second, (t - 0.5) / 0.5)


def _dip_white(first: Image.Image, second: Image.Image, t: float) -> Image.Image:
    """Fade to white halfway, then up from white (directive section 32).

    The bright counterpart to ``dip``; useful for clean, airy scene changes.
    """
    white = Image.new("RGBA", first.size, (255, 255, 255, 255))
    if t < 0.5:
        return crossfade(first, white, t / 0.5)
    return crossfade(white, second, (t - 0.5) / 0.5)
