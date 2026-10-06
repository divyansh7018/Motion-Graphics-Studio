"""The animation system (Stage D).

An animation is a list of **tracks**; a track moves one property from one value
to another between two times, shaped by an easing curve.  Everything is
expressed in *seconds and normalised units*, never in frames or pixels, so the
same animation plays identically at 24, 30 or 60 fps and at any resolution.

Evaluation is a pure function of time: ``evaluate(spec, t)`` returns the
transform for that instant and changes nothing.  That is what lets a preview
scrub backwards and forwards without accumulating drift, and it means there is
no animation state to leak between frames.

Times are never clamped into the project: an animation that runs longer than
its scene simply holds its end value (see ``hold``), and the timeline - not the
animation - decides how long a scene lasts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "EASINGS",
    "ANIMATION_PROPERTIES",
    "ANIMATION_PRESETS",
    "easing",
    "EasingCurve",
    "AnimationTrack",
    "AnimationSpec",
    "ElementTransform",
    "IDENTITY",
    "parse_animation",
    "evaluate",
]

#: Properties a track can animate.  All are resolution independent.
ANIMATION_PROPERTIES: tuple[str, ...] = (
    "opacity",   # 0..1 multiplier
    "x",         # normalised offset, fraction of frame width
    "y",         # normalised offset, fraction of frame height
    "scale",     # multiplier around the element's anchor
    "rotation",  # degrees
)

EasingCurve = Callable[[float], float]


# --------------------------------------------------------------------------
# Easing
# --------------------------------------------------------------------------

def _linear(t: float) -> float:
    return t


def _ease_in(t: float) -> float:
    return t * t


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def _ease_in_out(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1.0 - ((-2 * t + 2) ** 2) / 2.0


def _ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _ease_out_back(t: float) -> float:
    """Slight overshoot - reads as "pop" without looking broken."""
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def _ease_out_elastic(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    c4 = (2 * math.pi) / 3
    return 2.0 ** (-10 * t) * math.sin((t * 10 - 0.75) * c4) + 1.0


def _bounce_out(t: float) -> float:
    n1, d1 = 7.5625, 2.75
    if t < 1 / d1:
        return n1 * t * t
    if t < 2 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    if t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    t -= 2.625 / d1
    return n1 * t * t + 0.984375


def _shake_curve(t: float) -> float:
    """A decaying side-to-side oscillation, fully deterministic.

    Starts at -1 and settles at 0, so an ``x`` track from ``-amplitude`` to ``0``
    reads as a nudge that comes to rest.  No randomness, so a preview and the
    final render are pixel-identical.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    decay = 1.0 - t
    return 1.0 - decay * math.cos(t * math.pi * 4.0)


EASINGS: dict[str, EasingCurve] = {
    "linear": _linear,
    "ease_in": _ease_in,
    "ease_out": _ease_out,
    "ease_in_out": _ease_in_out,
    "ease_out_cubic": _ease_out_cubic,
    "smoothstep": _smoothstep,
    "back": _ease_out_back,
    "elastic": _ease_out_elastic,
    "bounce": _bounce_out,
    "shake": _shake_curve,
}


def easing(name: str) -> EasingCurve:
    """Look an easing curve up by name; unknown names fall back to linear."""
    return EASINGS.get((name or "linear").strip().lower(), _linear)


# --------------------------------------------------------------------------
# Tracks and specs
# --------------------------------------------------------------------------

@dataclass
class AnimationTrack:
    """One property moving between two values.

    The animated property is stored in ``name`` rather than ``property``
    because a dataclass field called ``property`` would shadow the builtin
    decorator for the rest of the class body.  The JSON key stays
    ``"property"``, which is what reads naturally in ``project.json``.
    """

    name: str = "opacity"
    start: float = 0.0
    duration: float = 0.5
    value_from: float = 0.0
    value_to: float = 1.0
    easing: str = "ease_out"
    #: Hold the end value after the track finishes (default) or snap back.
    hold: bool = True

    def value_at(self, time: float) -> float:
        """The property value at ``time`` seconds from the animation start."""
        if self.duration <= 0:
            return self.value_to if time >= self.start else self.value_from
        local = (time - self.start) / self.duration
        if local <= 0.0:
            return self.value_from
        if local >= 1.0:
            return self.value_to if self.hold else self.value_from
        curve = easing(self.easing)
        return self.value_from + (self.value_to - self.value_from) * curve(max(0.0, min(1.0, local)))

    def is_active(self, time: float) -> bool:
        return self.start <= time <= self.start + self.duration

    @property
    def end(self) -> float:
        return self.start + max(0.0, self.duration)

    def to_dict(self) -> dict:
        return {
            "property": self.name,
            "start": self.start,
            "duration": self.duration,
            "from": self.value_from,
            "to": self.value_to,
            "easing": self.easing,
            "hold": self.hold,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["AnimationTrack"]:
        if not isinstance(data, dict):
            return None
        name = str(data.get("property", "opacity")).lower()
        if name not in ANIMATION_PROPERTIES:
            return None
        return cls(
            name=name,
            start=_number(data.get("start", 0.0)),
            duration=max(0.0, _number(data.get("duration", 0.5))),
            value_from=_number(data.get("from", 0.0)),
            value_to=_number(data.get("to", 1.0)),
            easing=str(data.get("easing", "ease_out")),
            hold=bool(data.get("hold", True)),
        )


@dataclass
class AnimationSpec:
    """Enter and exit animations for one element."""

    enter: list = field(default_factory=list)
    exit: list = field(default_factory=list)
    #: Name of the preset this came from, so the UI can show it back.
    preset: str = ""
    #: A value animation ("count_up"/"progress_fill") rather than a transform.
    #: The compositor reads this to animate a number or a bar's fill.
    value_mode: str = ""
    #: Times to repeat the enter (0 = play once).  Deterministic.
    repeat: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.enter and not self.exit and not self.value_mode

    @property
    def enter_duration(self) -> float:
        return max((track.end for track in self.enter), default=0.0)

    @property
    def exit_duration(self) -> float:
        return max((track.end for track in self.exit), default=0.0)

    def to_dict(self) -> dict:
        return {
            "preset": self.preset,
            "enter": [track.to_dict() for track in self.enter],
            "exit": [track.to_dict() for track in self.exit],
        }


@dataclass(frozen=True)
class ElementTransform:
    """The animated state of one element at one instant."""

    opacity: float = 1.0
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0

    @property
    def is_identity(self) -> bool:
        return (abs(self.opacity - 1.0) < 1e-4 and abs(self.x) < 1e-6 and abs(self.y) < 1e-6
                and abs(self.scale - 1.0) < 1e-4 and abs(self.rotation) < 1e-4)

    @property
    def is_invisible(self) -> bool:
        return self.opacity <= 0.001 or self.scale <= 0.001


IDENTITY = ElementTransform()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

def _fade(duration: float) -> list:
    return [AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "ease_out")]


def _fade_up(duration: float, distance: float = 0.06) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "ease_out"),
        AnimationTrack("y", 0.0, duration, distance, 0.0, "ease_out_cubic"),
    ]


def _fade_down(duration: float, distance: float = 0.06) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "ease_out"),
        AnimationTrack("y", 0.0, duration, -distance, 0.0, "ease_out_cubic"),
    ]


def _slide_left(duration: float, distance: float = 0.25) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.5, 0.0, 1.0, "ease_out"),
        AnimationTrack("x", 0.0, duration, distance, 0.0, "ease_out_cubic"),
    ]


def _slide_right(duration: float, distance: float = 0.25) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.5, 0.0, 1.0, "ease_out"),
        AnimationTrack("x", 0.0, duration, -distance, 0.0, "ease_out_cubic"),
    ]


def _zoom_in(duration: float) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.6, 0.0, 1.0, "ease_out"),
        AnimationTrack("scale", 0.0, duration, 0.86, 1.0, "ease_out_back"),
    ]


def _pop(duration: float) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.4, 0.0, 1.0, "linear"),
        AnimationTrack("scale", 0.0, duration, 0.5, 1.0, "back"),
    ]


def _typewriter(duration: float) -> list:
    """A soft reveal for body text (true per-character typing is Stage I)."""
    return [
        AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "linear"),
        AnimationTrack("x", 0.0, duration, -0.02, 0.0, "ease_out"),
    ]


def _grow_bar(duration: float) -> list:
    """For charts and numbers: scale up from the baseline."""
    return [
        AnimationTrack("opacity", 0.0, duration * 0.3, 0.0, 1.0, "linear"),
        AnimationTrack("scale", 0.0, duration, 0.2, 1.0, "ease_out_cubic"),
    ]


def _slide_up(duration: float, distance: float = 0.25) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.5, 0.0, 1.0, "ease_out"),
        AnimationTrack("y", 0.0, duration, distance, 0.0, "ease_out_cubic"),
    ]


def _slide_down(duration: float, distance: float = 0.25) -> list:
    return [
        AnimationTrack("opacity", 0.0, duration * 0.5, 0.0, 1.0, "ease_out"),
        AnimationTrack("y", 0.0, duration, -distance, 0.0, "ease_out_cubic"),
    ]


def _scale_out(duration: float) -> list:
    """Grow slightly past full size as it fades in - a gentle "breathe"."""
    return [
        AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "ease_out"),
        AnimationTrack("scale", 0.0, duration, 1.08, 1.0, "ease_out_cubic"),
    ]


def _fade_out(duration: float) -> list:
    """An enter that resolves to fully visible; used reversed for exits."""
    return [AnimationTrack("opacity", 0.0, duration, 1.0, 0.0, "ease_in")]


def _shake(duration: float, intensity: float = 0.03) -> list:
    """A short side-to-side nudge for emphasis.  Deterministic, no randomness."""
    amplitude = max(0.0, float(intensity))
    return [
        AnimationTrack("opacity", 0.0, duration * 0.2, 0.0, 1.0, "ease_out"),
        AnimationTrack("x", 0.0, duration, -amplitude, 0.0, "shake"),
    ]


#: Named animations the UI offers.  Adding one here is all it takes to expose
#: it - nothing else in the engine knows these names.
ANIMATION_PRESETS: dict[str, dict] = {
    "none": {"label": "No animation", "build": lambda d: []},
    "fade": {"label": "Fade in", "build": _fade},
    "fade up": {"label": "Fade up", "build": _fade_up},
    "fade down": {"label": "Fade down", "build": _fade_down},
    "fade out": {"label": "Fade out", "build": _fade_out},
    "slide from right": {"label": "Slide in from the right", "build": _slide_left},
    "slide from left": {"label": "Slide in from the left", "build": _slide_right},
    "slide up": {"label": "Slide up", "build": _slide_up},
    "slide down": {"label": "Slide down", "build": _slide_down},
    "zoom": {"label": "Zoom in", "build": _zoom_in},
    "scale out": {"label": "Scale out", "build": _scale_out},
    "pop": {"label": "Pop", "build": _pop},
    "reveal": {"label": "Reveal text", "build": _typewriter},
    "grow": {"label": "Grow from baseline", "build": _grow_bar},
    "shake": {"label": "Shake", "build": _shake},
    # Value animations: no transform track, the compositor animates a value.
    "count up": {"label": "Count up", "build": lambda d: []},
    "progress fill": {"label": "Fill progress", "build": lambda d: []},
}

#: Presets whose effect is a *value* over time rather than a transform track.
#: The compositor reads these to count a number up or fill a progress bar
#: (directive section 31).  They still parse as normal presets so the UI can
#: list them.
VALUE_PRESETS: dict[str, str] = {
    "count up": "count_up",
    "progress fill": "progress_fill",
}

#: A generic preset name plus a ``direction`` resolves to a specific variant, so
#: ``{"preset": "slide", "direction": "up"}`` becomes "slide up".
_DIRECTIONAL: dict[str, dict[str, str]] = {
    "slide": {"up": "slide up", "down": "slide down", "left": "slide from right", "right": "slide from left"},
    "fade": {"up": "fade up", "down": "fade down", "out": "fade out"},
}

#: Sensible default per element kind, used when a scene template is created.
DEFAULT_PRESET_BY_KIND: dict[str, str] = {
    "text": "fade up",
    "image": "fade",
    "shape": "fade",
    "card": "fade up",
    "number": "pop",
    "chart": "grow",
    "divider": "fade",
    "group": "fade up",
    "progress": "fade",
}


def value_progress(spec: Optional[AnimationSpec], time: float) -> float:
    """Eased 0..1 progress for a value animation (count up / progress fill).

    Uses the first enter track's clock and easing, so a count-up eases exactly
    like the transform animations do.  Returns 1.0 once finished, and 1.0 for a
    spec with no value mode (nothing to animate - show the final value).
    """
    if spec is None or not spec.value_mode or not spec.enter:
        return 1.0
    track = spec.enter[0]
    start, end = track.start, track.end
    if end <= start:
        return 1.0
    t = max(0.0, min(1.0, (time - start) / (end - start)))
    return easing(track.easing)(t)


def preset_names() -> list[str]:
    """Preset keys, with "none" first (that is the order the UI wants)."""
    names = list(ANIMATION_PRESETS)
    names.remove("none")
    return ["none", *sorted(names)]


# --------------------------------------------------------------------------
# Parsing and evaluation
# --------------------------------------------------------------------------

def parse_animation(data: Any, *, kind: str = "", default_duration: float = 0.6) -> AnimationSpec:
    """Build an :class:`AnimationSpec` from an element's ``animation`` dict.

    Accepts three forms:

    ``{}``
        Nothing.  Falls back to the default preset for the element kind, which
        is what makes a new scene feel finished rather than static.
    ``{"preset": "pop", "duration": 0.8, "delay": 0.2}``
        A named preset with timing.
    ``{"enter": [{"property": "opacity", "from": 0, "to": 1, ...}]}``
        Explicit tracks, for advanced users.
    """
    spec = AnimationSpec()
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return spec

    duration = max(0.0, _number(data.get("duration", default_duration), default_duration))
    delay = max(0.0, _number(data.get("delay", 0.0)))
    preset_name = str(data.get("preset", "") or "").strip().lower()
    spec.repeat = max(0, int(_number(data.get("repeat", 0.0), 0.0)))

    # Stagger: a delay that grows with an element's index, so a list of items
    # cascades in.  ``index`` is supplied by the caller (the compositor knows the
    # element's position); ``stagger`` is the per-item gap in seconds.
    stagger = _number(data.get("stagger", 0.0))
    index = int(_number(data.get("index", 0.0), 0.0))
    if stagger and index:
        delay += stagger * index

    # ``direction`` picks the matching variant of a directional preset, so a
    # template can say ``{"preset": "slide", "direction": "up"}``.
    direction = str(data.get("direction", "") or "").strip().lower()
    if direction and preset_name in _DIRECTIONAL:
        variant = _DIRECTIONAL[preset_name].get(direction)
        if variant:
            preset_name = variant

    intensity = _number(data.get("intensity", 0.0), 0.0)

    # Value presets (count up / progress fill) drive a number, not a transform.
    if preset_name in VALUE_PRESETS:
        spec.preset = preset_name
        spec.value_mode = VALUE_PRESETS[preset_name]
        # A value animation still needs a clock, so give it one opacity track.
        spec.enter = _shift([AnimationTrack("opacity", 0.0, duration, 0.0, 1.0, "ease_out")], delay)
        return spec

    raw_enter = data.get("enter")
    if isinstance(raw_enter, Sequence) and not isinstance(raw_enter, (str, bytes)):
        spec.enter = [track for track in (AnimationTrack.from_dict(item) for item in raw_enter) if track]
    elif preset_name:
        builder = ANIMATION_PRESETS.get(preset_name, {}).get("build")
        if builder is not None:
            spec.preset = preset_name
            tracks = builder(duration, intensity) if preset_name == "shake" and intensity else builder(duration)
            spec.enter = _shift(tracks, delay)
    elif data.get("enabled", True) and kind:
        builder = ANIMATION_PRESETS.get(DEFAULT_PRESET_BY_KIND.get(kind, "fade"), {}).get("build")
        if builder is not None:
            spec.preset = DEFAULT_PRESET_BY_KIND.get(kind, "fade")
            spec.enter = _shift(builder(duration), delay)

    raw_exit = data.get("exit")
    if isinstance(raw_exit, Sequence) and not isinstance(raw_exit, (str, bytes)):
        spec.exit = [track for track in (AnimationTrack.from_dict(item) for item in raw_exit) if track]
    elif isinstance(raw_exit, str) and raw_exit.strip().lower() in ANIMATION_PRESETS:
        builder = ANIMATION_PRESETS[raw_exit.strip().lower()].get("build")
        if builder is not None:
            spec.exit = _reverse(builder(duration))
    return spec


def _shift(tracks: list, delay: float) -> list:
    if delay <= 0:
        return tracks
    for track in tracks:
        track.start += delay
    return tracks


def _reverse(tracks: list) -> list:
    """Turn an enter animation into its exit (values swapped, time kept)."""
    for track in tracks:
        track.value_from, track.value_to = track.value_to, track.value_from
    return tracks


def evaluate(spec: Optional[AnimationSpec], time: float, *, scene_duration: float = 0.0) -> ElementTransform:
    """The transform for one element at ``time`` seconds into its scene.

    ``scene_duration`` lets exit animations be anchored to the end of the
    scene, which is what a viewer expects: the exit plays as the scene leaves,
    not at some fixed offset from its start.
    """
    if spec is None or spec.is_empty:
        return IDENTITY
    moment = max(0.0, _number(time))

    opacity, dx, dy, scale, rotation = 1.0, 0.0, 0.0, 1.0, 0.0
    for track in spec.enter:
        value = track.value_at(moment)
        opacity, dx, dy, scale, rotation = _apply(opacity, dx, dy, scale, rotation, track.name, value)

    if spec.exit and scene_duration > 0:
        # Exit runs backwards from the end of the scene.
        exit_time = moment - max(0.0, scene_duration - spec.exit_duration)
        if exit_time >= 0:
            for track in spec.exit:
                value = track.value_at(exit_time)
                opacity, dx, dy, scale, rotation = _apply(
                    opacity, dx, dy, scale, rotation, track.name, value)

    return ElementTransform(
        opacity=max(0.0, min(1.0, opacity)),
        x=dx,
        y=dy,
        scale=max(0.0, min(8.0, scale)),
        rotation=rotation % 360.0,
    )


def _apply(opacity: float, dx: float, dy: float, scale: float, rotation: float,
           name: str, value: float):
    """Fold one track's value into the running transform."""
    if name == "opacity":
        # Multiplying keeps "fade the group" and "fade this" composable.
        return opacity * value, dx, dy, scale, rotation
    if name == "x":
        return opacity, dx + value, dy, scale, rotation
    if name == "y":
        return opacity, dx, dy + value, scale, rotation
    if name == "scale":
        return opacity, dx, dy, scale * value, rotation
    if name == "rotation":
        return opacity, dx, dy, scale, rotation + value
    return opacity, dx, dy, scale, rotation
