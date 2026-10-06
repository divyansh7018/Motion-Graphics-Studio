"""The audio mix pipeline (Stage E, directive sections 3, 5, 6, 7, 39).

One FFmpeg invocation turns the project's audio intent into a single master WAV
that the video is muxed against.  Everything is explicit: each track's trim,
placement, fades, gain, ducking and mute become filter arguments, so the mix can
be inspected, logged and reproduced.

Signal flow::

    narration  ─┐
    music beds ─┼─ per-track trim / fades / gain / ducking ─→ amix ─→ master
    sfx        ─┘                                    gain ─→ normalise ─→ WAV

The master is padded and trimmed to exactly the timeline duration, so the audio
and video lengths cannot drift apart (directive sections 39, 40, 41).

Nothing here starts a process: :func:`build_mix_command` only returns the
argument list, so a caller (job, CLI or test) decides when to run it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = [
    "MixInput",
    "MixPlan",
    "AudioIssue",
    "build_mix_command",
    "resolve_mix_inputs",
]


@dataclass
class AudioIssue:
    """Something wrong with the audio setup, reported before any render."""

    code: str
    message: str
    what_to_do: str = ""
    severity: str = "warning"   # "warning" | "error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "what_to_do": self.what_to_do, "severity": self.severity}


@dataclass
class MixInput:
    """One source going into the mix, fully resolved to a file on disk."""

    path: Path = field(default_factory=Path)
    #: Human label for logs and the UI ("narration", "music", "sfx whoosh").
    label: str = ""
    #: Kind, so the mix can treat narration differently from music.
    kind: str = ""
    #: ``-stream_loop`` value: 0 = once, N = N extra passes, -1 = loop forever.
    stream_loop: int = 0
    #: Seconds to skip at the start of the file.
    trim_in: float = 0.0
    #: How long this track should occupy on the timeline (0 = to its own end).
    length: float = 0.0
    #: Where on the project timeline it starts.
    start: float = 0.0
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    #: A ``volume`` expression (``eval=frame``) used instead of a flat gain,
    #: which is how ducking is applied.
    volume_expression: str = ""

    @property
    def is_infinite(self) -> bool:
        return self.stream_loop < 0


@dataclass
class MixPlan:
    """Everything needed to run the mix, plus why it looks the way it does."""

    inputs: list = field(default_factory=list)
    filter_complex: str = ""
    output_args: list = field(default_factory=list)
    duration: float = 0.0
    sample_rate: int = 48000
    channels: int = 2
    issues: list = field(default_factory=list)
    #: True when there is anything at all to mix.
    @property
    def has_audio(self) -> bool:
        return bool(self.inputs)

    def describe(self) -> str:
        if not self.inputs:
            return "no audio tracks"
        kinds = {}
        for item in self.inputs:
            kinds[item.kind or item.label] = kinds.get(item.kind or item.label, 0) + 1
        return ", ".join(f"{count} x {kind}" for kind, count in sorted(kinds.items()))


# --------------------------------------------------------------------------
# Resolving the project's audio into mix inputs
# --------------------------------------------------------------------------

def resolve_mix_inputs(audio: Any, *, narration: Sequence[Any], narration_starts: Sequence[float],
                       resolve_file, ducking=None, scene_starts: Optional[dict] = None,
                       project_duration: float = 0.0) -> tuple[list, list]:
    """Turn the project's audio settings into concrete :class:`MixInput`s.

    ``narration`` and ``narration_starts`` are parallel sequences: the narration
    files and where each one begins on the timeline.  ``resolve_file`` maps a
    track's stored reference (a project-relative path or an asset id) to a real
    path, so this function never guesses where a file lives.

    Returns ``(inputs, issues)``.  A track whose file is missing becomes an issue
    and is left out - it is never silently replaced by silence.
    """
    inputs: list[MixInput] = []
    issues: list[AudioIssue] = []
    scene_starts = scene_starts or {}

    # -- narration ---------------------------------------------------------
    if getattr(audio, "narration_enabled", True):
        narration_volume = max(0.0, float(getattr(audio, "narration_volume", 1.0) or 0.0))
        for index, track in enumerate(narration or []):
            path = resolve_file(track)
            if path is None:
                issues.append(AudioIssue(
                    "NARRATION_FILE_MISSING",
                    f"Narration {index + 1} has no readable file.",
                    "Generate the narration, or remove the track.",
                    severity="error",
                ))
                continue
            inputs.append(MixInput(
                path=path, label=f"narration {index + 1}", kind="narration",
                start=max(0.0, float(narration_starts[index] if index < len(narration_starts) else 0.0)),
                volume=narration_volume,
            ))
    else:
        issues.append(AudioIssue(
            "NARRATION_DISABLED",
            "Narration is switched off, so the video will have no voice.",
            "Turn narration back on if this video needs a voiceover.",
        ))

    # -- music beds --------------------------------------------------------
    duck_amount = float(getattr(audio, "ducking_level", 0.35) or 0.35)
    for bed in getattr(audio, "all_music", lambda: [])() or []:
        if not getattr(bed, "enabled", True):
            continue
        if getattr(bed, "mute", False):
            continue
        path = resolve_file(bed)
        if path is None:
            issues.append(AudioIssue(
                "MUSIC_FILE_MISSING",
                f"The music file '{getattr(bed, 'reference', '')}' was not found.",
                "Import the music on the Assets page, or clear this music track.",
                severity="error",
            ))
            continue
        start = max(0.0, float(getattr(bed, "start", 0.0) or 0.0))
        end = float(getattr(bed, "end", 0.0) or 0.0)
        length = max(0.0, end - start) if end > start else max(0.0, project_duration - start)
        inputs.append(MixInput(
            path=path, label=f"music {getattr(bed, 'id', '') or Path(str(path)).name}",
            kind="music",
            stream_loop=-1 if getattr(bed, "loop", False) else 0,
            trim_in=max(0.0, float(getattr(bed, "trim_in", 0.0) or 0.0)),
            length=length, start=start,
            volume=max(0.0, float(getattr(bed, "volume", 1.0) or 0.0)),
            fade_in=max(0.0, float(getattr(bed, "fade_in", 0.0) or 0.0)),
            fade_out=max(0.0, float(getattr(bed, "fade_out", 0.0) or 0.0)),
            volume_expression=_ducked_volume(ducking, duck_amount, float(getattr(bed, "volume", 1.0) or 0.0)),
        ))

    # -- sound effects -----------------------------------------------------
    for effect in getattr(audio, "sfx", []) or []:
        if not getattr(effect, "enabled", True) or getattr(effect, "mute", False):
            continue
        path = resolve_file(effect)
        if path is None:
            issues.append(AudioIssue(
                "SFX_FILE_MISSING",
                f"The sound effect '{getattr(effect, 'reference', '')}' was not found.",
                "Import the effect, or remove it from the project.",
                severity="error",
            ))
            continue
        start = _effect_start(effect, scene_starts)
        if start is None:
            issues.append(AudioIssue(
                "SFX_ANCHOR_UNKNOWN",
                f"The sound effect '{getattr(effect, 'reference', '')}' points at a scene that is not in this project.",
                "Choose another scene, or anchor the effect to the project timeline.",
                severity="error",
            ))
            continue
        repeat = max(0, int(getattr(effect, "repeat", 0) or 0))
        inputs.append(MixInput(
            path=path, label=f"sfx {getattr(effect, 'id', '') or Path(str(path)).name}",
            kind="sfx",
            stream_loop=repeat,
            trim_in=max(0.0, float(getattr(effect, "trim_in", 0.0) or 0.0)),
            length=max(0.0, float(getattr(effect, "duration", 0.0) or 0.0)),
            start=start,
            volume=max(0.0, float(getattr(effect, "volume", 1.0) or 0.0)),
            fade_in=max(0.0, float(getattr(effect, "fade_in", 0.0) or 0.0)),
            fade_out=max(0.0, float(getattr(effect, "fade_out", 0.0) or 0.0)),
        ))

    return inputs, issues


def _ducked_volume(ducking, duck_amount: float, bed_volume: float) -> str:
    """The ``volume`` expression for a music bed, ducked if ducking is on."""
    if ducking is None or not getattr(ducking, "enabled", False):
        return ""
    from .ducking import DuckingSettings, duck_expression

    settings = DuckingSettings(
        enabled=True,
        amount=max(0.0, min(1.0, duck_amount)),
        attack=float(getattr(ducking, "attack", 0.25) or 0.25),
        release=float(getattr(ducking, "release", 0.75) or 0.75),
    )
    windows = list(getattr(ducking, "windows", []) or [])
    if not windows:
        return ""
    return duck_expression(windows, settings.clamped(), base=bed_volume)


def _effect_start(effect: Any, scene_starts: dict) -> Optional[float]:
    """Resolve where a sound effect begins, honouring its anchor."""
    anchor = str(getattr(effect, "anchor", "project") or "project")
    base = 0.0
    if anchor == "scene":
        scene_id = str(getattr(effect, "scene_id", "") or "")
        if scene_id not in scene_starts:
            return None
        base = float(scene_starts[scene_id])
    elif anchor == "narration":
        scene_id = str(getattr(effect, "scene_id", "") or "")
        if scene_id not in scene_starts:
            return None
        base = float(scene_starts[scene_id])
    at = float(getattr(effect, "at_seconds", 0.0) or 0.0)
    offset = float(getattr(effect, "offset", 0.0) or 0.0)
    return max(0.0, base + at + offset)


# --------------------------------------------------------------------------
# The FFmpeg command
# --------------------------------------------------------------------------

def build_mix_command(inputs: Sequence[MixInput], output: Any, *, duration: float,
                      sample_rate: int = 48000, channels: int = 2,
                      master_volume: float = 1.0, normalize: bool = False,
                      target_lufs: float = -16.0) -> MixPlan:
    """Build the FFmpeg argument list that produces the master audio.

    The plan is returned, not executed.  ``duration`` is the timeline length: the
    mix is padded and then trimmed to exactly that, so audio and video agree.
    """
    plan = MixPlan(duration=max(0.0, float(duration)), sample_rate=int(sample_rate),
                   channels=max(1, int(channels)))
    usable = [item for item in inputs if item.path and Path(item.path).is_file()]
    plan.inputs = list(usable)
    if not usable:
        return plan

    chains = []
    labels = []
    for index, item in enumerate(usable):
        chain = _track_chain(index, item, plan.duration)
        chains.append(chain)
        labels.append(f"[a{index}]")

    mix = (f"{''.join(labels)}amix=inputs={len(usable)}:duration=longest:"
           f"normalize=0[mixed]")
    chains.append(mix)

    master_filters: list[str] = []
    master = max(0.0, float(master_volume))
    if abs(master - 1.0) > 1e-6:
        master_filters.append(f"volume={_num(master)}")
    if normalize:
        # Single-pass loudnorm: it measures and adjusts in one go, and its true
        # peak target keeps the result from clipping.
        master_filters.append(f"loudnorm=I={_num(target_lufs)}:TP=-1.5:LRA=11")
    # Pad then trim, so the master is exactly the timeline length - no short
    # audio (video would keep playing in silence) and no overrun.
    master_filters.append("apad")
    if plan.duration > 0:
        master_filters.append(f"atrim=end={_num(plan.duration)}")
    master_filters.append("asetpts=PTS-STARTPTS")
    master_filters.append(f"aresample={plan.sample_rate}")
    master_filters.append(f"aformat=channel_layouts={'stereo' if plan.channels == 2 else 'mono'}")
    chains.append("[mixed]" + ",".join(master_filters) + "[master]")

    plan.filter_complex = ";".join(chains)
    # ``-t`` is the hard stop.  The chain above ends in ``apad`` so it can supply
    # silence for as long as it is asked, and a looping music bed makes the mixed
    # stream effectively endless - without this the encoder would keep writing
    # until the disk filled.  The master is never longer than the timeline.
    plan.output_args = ["-map", "[master]", "-c:a", "pcm_s16le",
                        "-ar", str(plan.sample_rate), "-ac", str(plan.channels)]
    if plan.duration > 0:
        plan.output_args += ["-t", _num(plan.duration)]
    plan.output_args.append(str(output))
    return plan


def _track_chain(index: int, item: MixInput, mix_duration: float = 0.0) -> str:
    """One input's filter chain: trim, fades, gain, then placement.

    ``mix_duration`` lets a fade-out land at the end of the video even when the
    track's own length is unknown (a looping bed, or a bed that runs to the end).
    Without it the fade the user asked for would simply not happen.
    """
    steps = []
    end = (item.trim_in + item.length) if item.length > 0 else 0.0
    if item.trim_in > 0 or end > 0:
        if end > item.trim_in:
            steps.append(f"atrim=start={_num(item.trim_in)}:end={_num(end)}")
        elif item.trim_in > 0:
            steps.append(f"atrim=start={_num(item.trim_in)}")
        steps.append("asetpts=PTS-STARTPTS")

    if item.fade_in > 0:
        steps.append(f"afade=t=in:st=0:d={_num(item.fade_in)}")
    if item.fade_out > 0:
        fade_end = item.length
        if fade_end <= item.fade_out and mix_duration > 0:
            fade_end = max(0.0, float(mix_duration) - item.start)
        if fade_end > item.fade_out:
            steps.append(
                f"afade=t=out:st={_num(fade_end - item.fade_out)}:d={_num(item.fade_out)}")

    if item.volume_expression:
        steps.append(f"volume=volume='{item.volume_expression}':eval=frame")
    elif abs(item.volume - 1.0) > 1e-6:
        steps.append(f"volume={_num(item.volume)}")

    if item.start > 0:
        steps.append(f"adelay={int(round(item.start * 1000))}:all=1")

    if not steps:
        steps.append("anull")
    return f"[{index}:a]{','.join(steps)}[a{index}]"


def _num(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"
