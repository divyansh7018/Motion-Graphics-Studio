"""Narration generation and lifecycle (directive sections 24-28, 34-43).

This is the layer that turns a script into validated audio and keeps the project
honest about what it holds:

* **One action, one job.**  :func:`generate_narration` performs exactly one
  generation pass.  It is only ever called from a job body, never from an import,
  a watcher or a retry loop.
* **Nothing is claimed that did not happen.**  A track becomes ``ready`` only
  after the WAV is written, re-read and measured.  A file that is missing,
  truncated or silent makes the job fail.
* **Stale is a state, not a guess.**  Content hashes decide whether existing audio
  still matches the script and settings; the interface never silently reuses it.

No Qt in here: the interface drives this through jobs and reads the returned
structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.core.errors import AppError, FriendlyError, JobCancelled, Severity
from app.core.logging_setup import get_logger, log_event
from app.core.events import Event
from app.project.model import (
    NARRATION_FULL_FILENAME,
    NARRATION_SCENE_TEMPLATE,
    NARRATION_SUBDIR,
    NarrationTrack,
    Project,
    new_id,
    utc_now_iso,
)

LOGGER = get_logger("tts.narration")


class NarrationError(AppError):
    """Narration could not be generated, or the result is not usable."""

    default_title = "Narration could not be generated"
    default_severity = Severity.ERROR


@dataclass
class NarrationSettings:
    """The voice settings used for one generation, gathered in one place."""

    voice: str = ""
    language: str = ""
    speed: float = 1.0
    volume: float = 1.0
    sample_rate: int = 24000
    model_version: str = ""
    #: Serialised preprocessing options (part of the cache key).
    preprocessing: dict = field(default_factory=dict)
    #: Options object handed to the preprocessor.
    preprocess_options: Any = None
    section_gap_seconds: float = 0.0

    def describe(self) -> str:
        voice = self.voice or "no voice"
        return f"{voice} · {self.language or 'no language'} · {self.speed:.2f}x · {int(self.volume * 100)}%"


@dataclass
class NarrationOutcome:
    """What a generation pass produced."""

    ok: bool = False
    #: Tracks written by this pass, in project order.
    tracks: list[NarrationTrack] = field(default_factory=list)
    status: str = "not_generated"
    error: Optional[FriendlyError] = None
    #: Preprocessing changes, so the interface can show them (section 20).
    preprocessing_notes: list[str] = field(default_factory=list)
    #: Chunks the engine split the text into.
    chunks: int = 0
    elapsed_seconds: float = 0.0

    @property
    def total_duration(self) -> float:
        return round(sum(track.duration_seconds() for track in self.tracks), 3)

    def summary(self) -> str:
        if not self.ok:
            return self.error.title if self.error else "Narration was not generated."
        count = len(self.tracks)
        return (
            f"{count} narration file{'s' if count != 1 else ''} generated "
            f"({self.total_duration:.2f}s of audio)."
        )


# --------------------------------------------------------------------------
# Planning: which files, with which names
# --------------------------------------------------------------------------

def output_dir_for(project: Project) -> str:
    """The narration folder inside the project, relative and configurable."""
    configured = (project.narration.output_dir or "").strip()
    return configured or NARRATION_SUBDIR


def plan_outputs(project: Project, section_ids: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Deterministic (relative path, section id) pairs for a generation pass.

    Names are predictable - ``narration_full.wav``, ``narration_scene_001.wav`` -
    so a user can find them and a re-run overwrites the same file instead of
    scattering copies (directive section 26).
    """
    base = output_dir_for(project).strip("/")
    mode = project.narration.mode
    if mode == "section_scene":
        ids = [section_id for section_id in section_ids if section_id]
        if not ids:
            ids = [section.id for section in project.script.sections]
        return [
            (f"{base}/{NARRATION_SCENE_TEMPLATE.format(index=index)}", section_id)
            for index, section_id in enumerate(ids, start=1)
        ]
    return [(f"{base}/{NARRATION_FULL_FILENAME}", "")]


def resolve_audio_path(project_dir: Path, relative: str) -> Path:
    """Resolve a project-relative narration path, refusing to escape the folder."""
    root = Path(project_dir)
    candidate = Path(relative)
    if candidate.is_absolute():
        raise NarrationError(
            f"“{relative}” is an absolute path.",
            actions=((
                "Use a project-relative path such as audio/narration/narration_full.wav."
            ),),
            title="The narration path is not inside the project"
        )
    resolved = (root / candidate).resolve()
    if not str(resolved).startswith(str(root.resolve())):
        raise NarrationError(
            f"“{relative}” resolves to {resolved}.",
            actions=("Keep narration inside the project folder.",),
            title="The narration path points outside the project"
        )
    return resolved


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _section_text(project: Project, section_id: str) -> str:
    for section in project.script.sections:
        if section.id == section_id:
            return section.text or ""
    return ""


def generate_narration(project: Project,
                       project_dir: Path,
                       settings: NarrationSettings,
                       *,
                       engine: Any = None,
                       section_ids: Iterable[str] = (),
                       progress: Optional[Callable[[str, int, int], None]] = None,
                       cancel: Optional[Callable[[], bool]] = None,
                       overwrite: bool = True) -> NarrationOutcome:
    """Generate narration for *project* and record the result in the model.

    Performs exactly one generation pass.  The caller is responsible for running
    this on a worker thread and for committing the project afterwards.
    """
    from app.tts.audio import validate_wav, write_wav
    from app.tts.cache import settings_hash, source_hash
    from app.tts.preprocess import preprocess

    outcome = NarrationOutcome()
    root = Path(project_dir)

    # -- 1. validate the request up front --------------------------------
    problem = validate_settings(settings)
    if problem:
        outcome.error = problem.friendly()
        outcome.status = "failed"
        project.narration.status = "failed"
        project.narration.last_error = problem.title
        return outcome

    if engine is None:
        from app.tts.engine import KokoroEngine

        engine = KokoroEngine()

    plans = plan_outputs(project, section_ids)
    if not plans:
        outcome.error = NarrationError(
            "The script has no text in the selected mode.",
            actions=((
                "Write or import a script, or switch the narration mode back to "
                "“Full script”."
            ),),
            title="There is nothing to generate"
        ).friendly()
        outcome.status = "failed"
        return outcome

    settings_digest = settings_hash(
        voice=settings.voice, language=settings.language, speed=settings.speed,
        volume=settings.volume, model_version=settings.model_version,
        preprocessing=settings.preprocessing, sample_rate=settings.sample_rate,
    )

    log_event(Event.TTS_START,
              "Narration generation started",
              logger=LOGGER,
              mode=project.narration.mode,
              files=len(plans),
              voice=settings.voice,
              language=settings.language)

    new_tracks: list[NarrationTrack] = []
    for index, (relative, section_id) in enumerate(plans, start=1):
        if cancel and cancel():
            project.narration.status = "cancelled"
            raise JobCancelled(
                f"Narration generation cancelled before part {index} of {len(plans)}."
            )

        text = project.script.source_text if not section_id else _section_text(project, section_id)
        if not (text or "").strip():
            outcome.error = NarrationError(
            (
                    "The full script has no text." if not section_id
                    else f"Section “{section_id}” has no text."
                ),
            actions=("Add text to the script, then generate again.",),
            title="That part of the script is empty"
        ).friendly()
            outcome.status = "failed"
            return outcome

        cleaned = preprocess(text, settings.preprocess_options)
        for note in cleaned.changes:
            if note not in outcome.preprocessing_notes:
                outcome.preprocessing_notes.append(note)

        if progress:
            progress(f"Generating {Path(relative).name}", index, len(plans))

        from app.tts.engine import GenerationRequest

        try:
            result = engine.synthesize(
                GenerationRequest(
                    text=cleaned.text,
                    voice=settings.voice,
                    language=settings.language,
                    speed=settings.speed,
                    volume=settings.volume,
                ),
                progress=None,
            )
        except JobCancelled:
            # Cancellation is a normal outcome, not a failure: it must reach the
            # job runner untouched so the job is marked CANCELLED (section 31).
            project.narration.status = "cancelled"
            raise
        except AppError as error:
            outcome.error = error.friendly()
            outcome.status = "failed"
            project.narration.status = "failed"
            project.narration.last_error = error.what_happened
            log_event(Event.TTS_FAILED, "Narration generation failed",
                      logger=LOGGER, reason=error.title)
            return outcome
        except Exception as error:  # noqa: BLE001 - converted, never swallowed
            # The user gets a friendly message; the log and the expandable
            # technical detail keep the real exception type and text, so an
            # unexpected failure is still diagnosable (section 48).
            detail = f"{type(error).__name__}: {error}".strip(": ")
            friendly = NarrationError(
                "Kokoro could not produce audio for this text.",
                why=str(error) or "The engine failed without giving a reason.",
                actions=(
                    "Run System Check to confirm Kokoro, its model and a voice "
                    "are all available.",
                    "Try a different voice, then generate again.",
                ),
                technical=detail,
                title="Narration generation failed",
            )
            outcome.error = friendly.friendly()
            outcome.status = "failed"
            project.narration.status = "failed"
            project.narration.last_error = str(error)
            log_event(Event.TTS_FAILED, "Narration generation failed",
                      logger=LOGGER, reason=detail)
            return outcome
        outcome.chunks += result.chunks
        outcome.elapsed_seconds += result.elapsed_seconds

        target = resolve_audio_path(root, relative)

        def _fail(title: str, what: str, actions: tuple[str, ...]) -> NarrationOutcome:
            """Record a failure on both the outcome and the project model."""
            error = NarrationError(what, actions=actions, title=title)
            outcome.error = error.friendly()
            outcome.status = "failed"
            project.narration.status = "failed"
            project.narration.last_error = what
            log_event(Event.TTS_FAILED, "Narration generation failed",
                      logger=LOGGER, reason=title)
            return outcome

        # -- 2. write the audio, then validate what is really on disk -----
        try:
            info = write_wav(target, result.samples, result.sample_rate,
                             overwrite=overwrite)
        except JobCancelled:
            project.narration.status = "cancelled"
            raise
        except AppError as error:
            return _fail(error.title, error.what_happened, error.actions)
        except OSError as error:
            return _fail(
                "The narration file could not be written",
                f"{target.name}: {error}",
                (
                    "Check the project folder is writable and the disk is not full.",
                    "Try generating again.",
                ),
            )

        if not info.valid:
            log_event(Event.AUDIO_VALIDATION_FAILED,
                      "Generated narration failed validation",
                      logger=LOGGER, path=str(target),
                      problems="; ".join(info.problems))
            return _fail(
                "The generated narration file is not valid",
                f"{target.name}: " + "; ".join(info.problems),
                (
                    "The audio was written but cannot be played back safely.",
                    "Try generating again; if it keeps failing, run System Check.",
                ),
            )

        # Re-read the file independently so the metadata comes from disk, not
        # from the in-memory buffer that was just written.
        measured = validate_wav(target)
        if not measured.valid:
            return _fail(
                "The narration file could not be verified",
                f"{target.name}: " + "; ".join(measured.problems),
                ("Delete the file and generate again.",),
            )

        estimated = _estimate_seconds(cleaned.text)
        track = NarrationTrack(
            id=new_id("nar"),
            kind="full" if not section_id else "section",
            section_id=section_id,
            path=relative,
            status="ready",
            source_hash=source_hash(text),
            settings_hash=settings_digest,
            estimated_duration_seconds=estimated,
            actual_duration_seconds=round(measured.duration_seconds, 3),
            sample_rate=measured.sample_rate,
            channels=measured.channels,
            size_bytes=measured.size_bytes,
            voice=settings.voice,
            language=settings.language,
            speed=float(settings.speed),
            volume=float(settings.volume),
            engine="kokoro",
            model_version=settings.model_version,
            generated_at=utc_now_iso(),
        )
        new_tracks.append(track)
        log_event(Event.TTS_COMPLETE,
                  "Narration file written and verified",
                  logger=LOGGER,
                  path=str(target),
                  duration=track.actual_duration_seconds,
                  voice=settings.voice,
                  chunks=result.chunks)

    _merge_tracks(project, new_tracks)
    # Record what actually produced the audio.  Every one of these values is
    # part of the settings hash stored on each track, so if the project did not
    # carry them the hash could never be reproduced and each track would be
    # reported stale on the next refresh (directive sections 26, 39 and 43).
    project.narration.preprocessing = dict(settings.preprocessing or {})
    project.voice.engine = "kokoro"
    project.voice.voice = settings.voice
    project.voice.language = settings.language
    project.voice.speed = float(settings.speed)
    project.voice.volume = float(settings.volume)
    project.voice.sample_rate = int(settings.sample_rate)
    project.narration.status = project.narration.recompute_status()
    project.narration.last_generated_at = utc_now_iso()
    project.narration.last_error = ""
    outcome.ok = True
    outcome.tracks = new_tracks
    outcome.status = project.narration.status
    return outcome


def _merge_tracks(project: Project, new_tracks: list[NarrationTrack]) -> None:
    """Replace tracks for the same section, keep the others untouched."""
    replaced = {track.section_id for track in new_tracks}
    kept = [track for track in project.narration.tracks if track.section_id not in replaced]
    project.narration.tracks = kept + new_tracks


def _estimate_seconds(text: str, words_per_minute: int = 0) -> float:
    """Labelled estimate only - the measured WAV duration is authoritative."""
    from app.script.stats import DEFAULT_WORDS_PER_MINUTE, count_words, estimate_seconds

    wpm = words_per_minute if words_per_minute and words_per_minute > 0 else DEFAULT_WORDS_PER_MINUTE
    return round(estimate_seconds(count_words(text), wpm), 2)


def validate_settings(settings: NarrationSettings) -> Optional[NarrationError]:
    """Check a generation request before any model is touched (section 42)."""
    if not (settings.voice or "").strip():
        return NarrationError(
            "A voice must be chosen before audio can be generated.",
            actions=((
                "Open the Voice panel, pick a language and a voice, preview it, "
                "then generate."
            ),),
            title="No narration voice is selected"
        )
    try:
        speed = float(settings.speed)
    except (TypeError, ValueError):
        speed = 0.0
    if not 0.5 <= speed <= 2.0:
        return NarrationError(
            f"Speed was {settings.speed}; Kokoro supports 0.5x to 2.0x.",
            actions=("Set the speed between 0.75x and 1.50x for natural speech.",),
            title="The narration speed is outside the supported range"
        )
    try:
        volume = float(settings.volume)
    except (TypeError, ValueError):
        volume = -1.0
    if not 0.0 <= volume <= 1.25:
        return NarrationError(
            f"Volume was {settings.volume}; 0% to 125% is supported.",
            actions=("Set the volume between 0% and 125%.",),
            title="The narration volume is outside the supported range"
        )
    return None


# --------------------------------------------------------------------------
# Status maintenance (run on open, and after any edit)
# --------------------------------------------------------------------------

def refresh_statuses(project: Project,
                     project_dir: Path,
                     settings: Optional[NarrationSettings] = None) -> list[str]:
    """Re-check every track against the files on disk and the current settings.

    Returns plain-language notes about what changed, so the interface can tell the
    user instead of showing a status that is quietly wrong.  Never deletes a file
    and never generates anything.
    """
    from app.tts.audio import validate_wav
    from app.tts.cache import compare

    root = Path(project_dir)
    notes: list[str] = []
    if not project.narration.tracks:
        project.narration.status = "not_generated"
        return notes

    for track in project.narration.tracks:
        if track.status in ("generating",):
            # A previous run did not finish; the file cannot be trusted.
            track.status = "failed"
            track.message = "The previous generation did not finish."
            notes.append(f"{track.filename or 'A narration file'} was left incomplete.")
            continue

        if not track.path:
            continue
        try:
            target = resolve_audio_path(root, track.path)
        except NarrationError as error:
            track.status = "missing"
            track.message = error.title
            notes.append(error.title)
            continue

        if not target.exists():
            if track.status != "missing":
                notes.append("Generated narration file is missing.")
            track.status = "missing"
            track.message = (
                f"{target.name} is no longer in the project folder. "
                "Regenerate it, or relink it to another file."
            )
            continue

        info = validate_wav(target)
        if not info.valid:
            track.status = "failed"
            track.message = "; ".join(info.problems)
            notes.append(f"{target.name} is not a valid narration file.")
            continue

        # The file is fine; is it still the right one?
        if settings is not None and track.kind == "full":
            report = compare(
                text=project.script.source_text,
                stored_source=track.source_hash,
                voice=settings.voice, language=settings.language,
                speed=settings.speed, volume=settings.volume,
                model_version=settings.model_version,
                preprocessing=settings.preprocessing,
                sample_rate=settings.sample_rate,
                stored_settings=track.settings_hash,
            )
            if report.stale and track.status == "ready":
                track.status = "stale"
                track.message = report.describe()
                notes.append("The narration no longer matches the script or voice settings.")
            elif not report.stale and track.status == "stale":
                track.status = "ready"
                track.message = ""
        # Keep the measured facts fresh even when the status did not change.
        track.actual_duration_seconds = round(info.duration_seconds, 3)
        track.sample_rate = info.sample_rate
        track.channels = info.channels
        track.size_bytes = info.size_bytes

    project.narration.recompute_status()
    return notes


def status_explanation(project: Project) -> str:
    """One sentence describing the narration state for the interface."""
    status = project.narration.status
    tracks = project.narration.tracks
    if not tracks:
        return "No narration has been generated yet."
    ready = [t for t in tracks if t.status == "ready"]
    if status == "ready" and ready:
        total = project.narration.total_duration_seconds()
        return f"{len(ready)} narration file(s) ready, {total:.2f}s of audio in total."

    # One canonical sentence per state, so the wording is predictable wherever it
    # is shown, followed by the file-specific detail when there is one.
    labels = {
        "generating": "Narration is being generated.",
        "stale": "The narration is out of date: the script or voice settings changed.",
        "failed": "The last narration generation failed.",
        "cancelled": "The last narration generation was cancelled.",
        "missing": "Generated narration file is missing.",
        "not_generated": "No narration has been generated yet.",
    }
    headline = labels.get(status, "Narration state is unknown.")
    for track in tracks:
        if track.status == status and track.message:
            return f"{headline} {track.message}"
    return headline


__all__ = [
    "NarrationError",
    "NarrationOutcome",
    "NarrationSettings",
    "generate_narration",
    "output_dir_for",
    "plan_outputs",
    "refresh_statuses",
    "resolve_audio_path",
    "status_explanation",
    "validate_settings",
]
