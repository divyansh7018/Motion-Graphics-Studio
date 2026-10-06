"""The audio service (Stage E, directive sections 3, 4, 7, 9, 11, 68).

One place that knows how to turn a project's audio intent into a real master
audio file - and how to explain, before anything expensive happens, why it
cannot.  The GUI, the CLI and the render engine all call this; none of them
contain mixing logic of their own.

Rules it enforces:

* Narration is **read, never regenerated**.  A render never re-runs Kokoro
  behind the user's back (directive section 4).
* A narration track marked stale or missing stops the render with a clear
  message, rather than quietly muxing old audio against a new script.
* Measured narration duration is authoritative; estimates are never used to
  place audio when a real file exists.
* Files that cannot be found are reported, never replaced with silence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..media.probe import MediaInfo, probe_media
from .ducking import DuckingSettings, duck_level_at, narration_windows
from .mix import AudioIssue, MixPlan, build_mix_command, resolve_mix_inputs

__all__ = ["AudioService", "AudioValidation", "NarrationPlacement", "MasterAudioResult"]


@dataclass
class NarrationPlacement:
    """One narration file and where it sits on the timeline."""

    track: Any = None
    path: Optional[Path] = None
    start: float = 0.0
    duration: float = 0.0
    scene_id: str = ""
    scene_name: str = ""
    status: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class AudioValidation:
    """What the audio setup looks like before a render is allowed to start."""

    issues: list = field(default_factory=list)
    placements: list = field(default_factory=list)
    #: Total length the audio is expected to occupy.
    expected_duration: float = 0.0

    @property
    def errors(self) -> list:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list:
        return [issue for issue in self.issues if issue.severity != "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "narration_segments": len(self.placements),
            "expected_duration": round(self.expected_duration, 3),
        }


@dataclass
class MasterAudioResult:
    """The outcome of building the master audio."""

    ok: bool = False
    path: Optional[Path] = None
    info: Optional[MediaInfo] = None
    plan: Optional[MixPlan] = None
    issues: list = field(default_factory=list)
    #: Why nothing was produced, in plain language.
    message: str = ""

    @property
    def duration(self) -> float:
        return float(self.info.duration) if self.info else 0.0


class AudioService:
    """Validation, mixing and preview for a project's audio."""

    def __init__(self, tools: Any, *, project_dir: Optional[Path] = None):
        self.tools = tools
        self.project_dir = Path(project_dir) if project_dir else None

    # -- file resolution ---------------------------------------------------

    def asset_paths(self, project: Any) -> dict:
        """The project's asset id -> absolute path map (built once per call)."""
        from ..scene.elements import build_asset_paths

        return build_asset_paths(project, self.project_dir)

    def resolve_file(self, project: Any, reference: str) -> Optional[Path]:
        """Resolve a stored reference to a real file, or ``None``.

        Accepts an asset id, a project-relative path or an absolute path - in
        that order - and never invents a location.
        """
        if not reference:
            return None
        raw = str(reference).strip()
        if not raw:
            return None
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate if candidate.is_file() else None
        if self.project_dir is not None:
            joined = self.project_dir / raw
            if joined.is_file():
                return joined
        assets = self.asset_paths(project)
        resolved = assets.get(raw)
        if resolved is not None and Path(resolved).is_file():
            return Path(resolved)
        return None

    def mix_resolver(self, project: Any):
        """Resolve whatever :func:`resolve_mix_inputs` hands back.

        Narration reaches the mixer as an already-resolved path (it was measured
        a moment earlier, so re-resolving it would only risk a different answer),
        while music and effects arrive as tracks carrying a stored reference.
        One resolver has to handle both.
        """
        def resolver(item: Any) -> Optional[Path]:
            if item is None:
                return None
            if isinstance(item, Path):
                return item if item.is_file() else None
            reference = getattr(item, "reference", "") or getattr(item, "path", "") or ""
            if isinstance(item, (str, os.PathLike)):
                reference = str(item)
            return self.resolve_file(project, str(reference))
        return resolver

    # -- narration ---------------------------------------------------------

    def narration_placements(self, project: Any, timeline: Any) -> list:
        """Where every scene's narration file sits on the timeline.

        Only scenes that are actually on the timeline contribute, and only when
        a narration file is named - a scene without voice contributes silence,
        which is the truth rather than an invented track.
        """
        placements: list[NarrationPlacement] = []
        scenes = {scene.id: scene for scene in getattr(project, "scenes", []) or []}
        for timing in getattr(timeline, "timings", []) or []:
            scene = scenes.get(timing.scene_id)
            if scene is None:
                continue
            narration = getattr(scene, "narration", None)
            reference = str(getattr(narration, "file", "") or "")
            if not reference:
                continue
            path = self.resolve_file(project, reference)
            duration = float(getattr(narration, "duration", 0.0) or 0.0)
            if duration <= 0 and path is not None:
                # The project did not record a duration; measure the real file
                # rather than guessing (directive section 4).
                measured = probe_media(path, self.tools)
                duration = measured.duration if measured.ok else 0.0
            placements.append(NarrationPlacement(
                track=narration, path=path, start=float(timing.start),
                duration=duration, scene_id=timing.scene_id,
                scene_name=timing.name,
                status=str(getattr(narration, "status", "") or ""),
            ))
        return placements

    # -- validation --------------------------------------------------------

    def validate(self, project: Any, timeline: Any) -> AudioValidation:
        """Check the audio setup without rendering anything.

        This is what stops a render before it wastes time: a stale narration, a
        missing music file or an effect anchored to a deleted scene are all
        reported here, with what to do about each.
        """
        audio = getattr(project, "audio", None)
        result = AudioValidation()
        placements = self.narration_placements(project, timeline)
        result.placements = placements

        # Staleness is a hard stop: rendering an old voice against a new script
        # produces a video that is silently wrong (directive section 4).
        plan = getattr(project, "narration", None)
        for track in getattr(plan, "tracks", []) or []:
            status = str(getattr(track, "status", "") or "")
            if status == "stale":
                result.issues.append(AudioIssue(
                    "NARRATION_STALE",
                    "Narration needs regeneration: the script changed after this audio was made.",
                    "Open the Narration page and generate the narration again, then render.",
                    severity="error",
                ))
            elif status == "missing":
                result.issues.append(AudioIssue(
                    "NARRATION_MISSING",
                    f"A narration file is missing "
                    f"({getattr(track, 'path', '') or 'no file recorded'}).",
                    "Generate the narration again, or clear the track.",
                    severity="error",
                ))
            elif status == "failed":
                result.issues.append(AudioIssue(
                    "NARRATION_FAILED",
                    f"Narration generation failed: {getattr(track, 'message', '') or 'no reason recorded'}.",
                    "Fix the reported problem and generate the narration again.",
                    severity="error",
                ))

        if getattr(audio, "narration_enabled", True):
            wanted = [scene for scene in getattr(project, "scenes", []) or []
                      if getattr(scene, "enabled", True) and str(getattr(scene.narration, "text", "") or "").strip()]
            missing = [placement for placement in placements if placement.path is None]
            for placement in missing:
                result.issues.append(AudioIssue(
                    "NARRATION_FILE_MISSING",
                    f"The narration for '{placement.scene_name or placement.scene_id}' has no readable file.",
                    "Generate the narration, or remove it from that scene.",
                    severity="error",
                ))
            if wanted and not placements:
                result.issues.append(AudioIssue(
                    "NARRATION_NOT_GENERATED",
                    "The script has narration text but no narration audio has been generated yet.",
                    "Open the Narration page and generate the narration before rendering.",
                    severity="error",
                ))

        # Music and effects: a reference to a file that is not there is fatal.
        scene_starts = {timing.scene_id: float(timing.start)
                        for timing in getattr(timeline, "timings", []) or []}
        project_duration = float(getattr(timeline, "total_duration", 0.0) or 0.0)
        result.expected_duration = project_duration

        _inputs, issues = resolve_mix_inputs(
            audio,
            narration=[placement.path for placement in placements],
            narration_starts=[placement.start for placement in placements],
            resolve_file=self.mix_resolver(project),
            ducking=self.ducking(project, timeline),
            scene_starts=scene_starts,
            project_duration=project_duration,
        )
        result.issues.extend(issues)
        return result

    # -- ducking -----------------------------------------------------------

    def ducking(self, project: Any, timeline: Any) -> Optional[DuckingSettings]:
        """The ducking settings, with the real narration windows attached."""
        audio = getattr(project, "audio", None)
        if audio is None or not getattr(audio, "ducking_enabled", False):
            return None
        windows = narration_windows(timeline)
        if not windows:
            return None
        # ``ducking_level`` in the model is the *remaining* music level, while the
        # envelope wants the amount *removed* - convert once, here.
        remaining = max(0.0, min(1.0, float(getattr(audio, "ducking_level", 0.35) or 0.35)))
        settings = DuckingSettings(
            enabled=True, amount=1.0 - remaining,
            attack=float(getattr(audio, "ducking_attack", 0.25) or 0.25),
            release=float(getattr(audio, "ducking_release", 0.75) or 0.75),
        ).clamped()
        settings.windows = windows  # type: ignore[attr-defined]
        return settings

    def duck_level_at(self, project: Any, timeline: Any, moment: float) -> float:
        """The music gain at ``moment`` - used by the UI and by tests."""
        settings = self.ducking(project, timeline)
        if settings is None:
            return 1.0
        return duck_level_at(moment, getattr(settings, "windows", []), settings)

    # -- mixing ------------------------------------------------------------

    def build_plan(self, project: Any, timeline: Any) -> tuple[MixPlan, list]:
        """Build (but do not run) the master-audio mix plan."""
        audio = getattr(project, "audio", None)
        placements = self.narration_placements(project, timeline)
        scene_starts = {timing.scene_id: float(timing.start)
                        for timing in getattr(timeline, "timings", []) or []}
        project_duration = float(getattr(timeline, "total_duration", 0.0) or 0.0)

        inputs, issues = resolve_mix_inputs(
            audio,
            narration=[placement.path for placement in placements],
            narration_starts=[placement.start for placement in placements],
            resolve_file=self.mix_resolver(project),
            ducking=self.ducking(project, timeline),
            scene_starts=scene_starts,
            project_duration=project_duration,
        )
        plan = build_mix_command(
            inputs, "",
            duration=project_duration,
            sample_rate=int(getattr(audio, "sample_rate", 48000) or 48000),
            channels=int(getattr(audio, "channels", 2) or 2),
            master_volume=float(getattr(audio, "master_volume", 1.0) or 1.0),
            normalize=bool(getattr(audio, "normalize_enabled", False)),
            target_lufs=float(getattr(audio, "target_lufs", -16.0) or -16.0),
        )
        return plan, issues

    def render_master(self, project: Any, timeline: Any, output: Any, *,
                      progress: Optional[Callable[[float, str], None]] = None,
                      cancel_token: Any = None, timeout: float = 1800.0) -> MasterAudioResult:
        """Mix the project's audio into one master file.

        Returns a result rather than raising, so a caller can show what happened
        and what to do.  An empty mix is not an error: a video with no audio is
        legitimate, and the caller decides whether that is acceptable.
        """
        output_path = Path(output)
        plan, issues = self.build_plan(project, timeline)
        if not plan.has_audio:
            return MasterAudioResult(
                ok=False, path=None, plan=plan, issues=issues,
                message="There is no audio to mix: no narration, music or effects are configured.",
            )
        if progress:
            progress(0.1, "Mixing audio")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        plan.output_args[-1] = str(output_path)

        argv: list[str] = ["-y", "-hide_banner", "-loglevel", "error"]
        for item in plan.inputs:
            if item.stream_loop:
                argv.extend(["-stream_loop", str(item.stream_loop)])
            argv.extend(["-i", str(item.path)])
        argv.extend(["-filter_complex", plan.filter_complex, *plan.output_args])

        result = self.tools.run(argv, timeout=timeout, cancel_token=cancel_token)
        if progress:
            progress(0.8, "Checking the mixed audio")
        if not result.ok:
            issues.append(AudioIssue(
                "AUDIO_MIX_FAILED",
                "FFmpeg could not mix the audio.",
                "Check that every audio file is readable, then try again.",
                severity="error",
            ))
            return MasterAudioResult(ok=False, path=output_path, plan=plan, issues=issues,
                                     message=result.describe_failure())

        info = probe_media(output_path, self.tools)
        if not info.ok:
            issues.append(AudioIssue(
                "AUDIO_MASTER_INVALID",
                "The mixed audio could not be read back.",
                "Try again; if it keeps failing, check the audio files for corruption.",
                severity="error",
            ))
            return MasterAudioResult(ok=False, path=output_path, info=info, plan=plan,
                                     issues=issues, message=info.error)
        if progress:
            progress(1.0, "Audio ready")
        return MasterAudioResult(ok=True, path=output_path, info=info, plan=plan, issues=issues)

    # -- preview -----------------------------------------------------------

    def preview_track(self, project: Any, reference: str, output: Any, *,
                      seconds: float = 10.0, start: float = 0.0,
                      volume: float = 1.0, timeout: float = 120.0) -> Optional[Path]:
        """Render a short preview of one audio file (directive section 11).

        Deliberately small and quick: it decodes one file, never the project, and
        never starts a video render.
        """
        path = self.resolve_file(project, reference)
        if path is None:
            return None
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        seconds = max(0.5, float(seconds))
        argv = [
            "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{seconds:.3f}",
            "-i", str(path),
            "-af", f"volume={max(0.0, float(volume)):.4f}",
            "-c:a", "pcm_s16le", str(output_path),
        ]
        result = self.tools.run(argv, timeout=timeout)
        return output_path if result.ok and output_path.is_file() else None
