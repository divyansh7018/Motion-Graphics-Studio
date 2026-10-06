"""Music ducking (Stage E, directive section 8).

When narration is speaking, the music bed drops by a configurable amount and
comes back smoothly afterwards.  The drop is driven by the *narration windows*
taken from the real timeline - the same measured narration durations the scene
timing uses - so the ducking lands exactly where the voice is, and the result is
deterministic: the same project ducks identically in preview and in the final
render.

Two implementations of the same envelope live here on purpose:

* :func:`duck_level_at` evaluates it in Python, so tests can assert the shape
  (down during speech, back up after) without decoding audio; and
* :func:`duck_expression` emits the equivalent FFmpeg ``volume`` expression, so
  the actual mix uses the same maths.

The envelope is a trapezoid per window: ``attack`` seconds down, held for the
speech, ``release`` seconds back up.  Windows are merged first, so overlapping
speech can never double-duck the music.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

__all__ = [
    "DuckingSettings",
    "narration_windows",
    "merge_windows",
    "duck_level_at",
    "duck_expression",
    "DEFAULT_DUCK_AMOUNT",
]

#: A gentle default: the music is clearly present but never fights the voice.
DEFAULT_DUCK_AMOUNT = 0.65


@dataclass
class DuckingSettings:
    """How much the music drops, and how quickly it moves."""

    enabled: bool = True
    #: Fraction of the music level removed while narration speaks (0..1).
    amount: float = DEFAULT_DUCK_AMOUNT
    attack: float = 0.25
    release: float = 0.75

    def clamped(self) -> "DuckingSettings":
        return DuckingSettings(
            enabled=bool(self.enabled),
            amount=max(0.0, min(1.0, float(self.amount))),
            # A zero ramp would click; keep a tiny minimum instead.
            attack=max(0.01, float(self.attack)),
            release=max(0.01, float(self.release)),
        )


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def narration_windows(timeline: Any, *, padding: float = 0.0) -> list[tuple[float, float]]:
    """The ``(start, end)`` spans during which narration is actually speaking.

    Built from the timeline's real timings, and only for scenes whose length came
    from measured narration - a scene timed by hand has no voice to duck under.
    """
    windows: list[tuple[float, float]] = []
    for timing in getattr(timeline, "timings", []) or []:
        if getattr(timing, "source", "") != "narration":
            continue
        narration_duration = float(getattr(timing, "narration_duration", 0.0) or 0.0)
        if narration_duration <= 0:
            continue
        start = float(timing.start) + float(getattr(timing, "narration_offset", 0.0) or 0.0)
        windows.append((max(0.0, start - padding), start + narration_duration + padding))
    return merge_windows(windows)


def merge_windows(windows: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    """Merge touching/overlapping windows so no moment is counted twice."""
    cleaned = sorted(
        ((max(0.0, float(start)), max(0.0, float(end))) for start, end in windows),
        key=lambda item: item[0],
    )
    merged: list[tuple[float, float]] = []
    for start, end in cleaned:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# --------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------

def _window_factor(moment: float, start: float, end: float, attack: float, release: float) -> float:
    """How far into the duck one window has pulled the music, 0..1."""
    ramp_in = (moment - (start - attack)) / attack if attack > 0 else 1.0
    ramp_out = ((end + release) - moment) / release if release > 0 else 1.0
    return max(0.0, min(1.0, ramp_in)) * max(0.0, min(1.0, ramp_out))


def duck_level_at(moment: float, windows: Sequence[Sequence[float]],
                  settings: DuckingSettings) -> float:
    """The music gain multiplier at ``moment`` seconds (1.0 = untouched).

    Windows are disjoint after :func:`merge_windows`, so the factors multiply
    without ever ducking twice for the same speech.
    """
    if not settings.enabled or settings.amount <= 0 or not windows:
        return 1.0
    options = settings.clamped()
    factor = 1.0
    for start, end in windows:
        factor *= 1.0 - _window_factor(float(moment), float(start), float(end),
                                       options.attack, options.release)
    return max(0.0, 1.0 - options.amount * (1.0 - factor))


def duck_expression(windows: Sequence[Sequence[float]], settings: DuckingSettings,
                    *, base: float = 1.0) -> str:
    """The equivalent FFmpeg ``volume`` expression for ``eval=frame``.

    Returns a constant when there is nothing to duck, so the filter stays simple
    and cheap for a project with no narration.
    """
    if not settings.enabled or settings.amount <= 0 or not windows:
        return f"{_fmt(base)}"
    options = settings.clamped()
    amount = _fmt(options.amount)
    attack = _fmt(options.attack)
    release = _fmt(options.release)

    factors = []
    for start, end in windows:
        # Trapezoid: ramp up over the attack, hold, ramp down over the release.
        factors.append(
            f"(1-clip((t-({_fmt(float(start) - options.attack)}))/{attack},0,1)"
            f"*clip((({_fmt(float(end) + options.release)})-t)/{release},0,1))"
        )
    product = "*".join(factors)
    return f"{_fmt(base)}*(1-{amount}*(1-{product}))"


def _fmt(value: float) -> str:
    """A compact, locale-independent number for an FFmpeg expression."""
    return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"
