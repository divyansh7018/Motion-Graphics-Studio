"""Job bodies for the voice pipeline (directive sections 28, 30-32, 51-52).

These are plain functions with the :class:`app.jobs.spec.JobContext` signature,
so the same code runs in the GUI thread pool, from the CLI and in tests.  They
never touch Qt.

Guarantees enforced here:

* **One action, one job.**  Each body does exactly one unit of work and is
  submitted under a key the manager refuses to duplicate.
* **Cancellation is real.**  The token is checked before the model is loaded and
  between chunks, and a cancelled preview deletes its temporary file.
* **A preview is not a generation.**  Preview audio goes to the application's
  preview folder, never into the project, and never creates a narration track.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.core.errors import JobCancelled, to_friendly
from app.core.logging_setup import get_logger, log_event
from app.core.events import Event
from app.jobs.spec import JobContext, JobSpec
from app.jobs.keys import JobKeys

LOGGER = get_logger("tts.jobs")

#: Preview files are tiny and disposable; they never enter a project.
PREVIEW_FILENAME = "voice_preview.wav"


def report_progress(context: "JobContext", fraction: float, message: str) -> None:
    """Report job progress using the Stage A ``ProgressReporter`` API.

    The reporter works in units of ``current`` out of ``total``, so a 0..1
    fraction is expressed against a total of one.  Kept here so every TTS job
    reports the same way and cannot drift from the Stage A convention.
    """
    progress = context.progress
    if getattr(progress.progress, "total", 0) != 1.0:
        progress.start(total=1.0, message=message, unit="narration")
    progress.update(current=min(1.0, max(0.0, float(fraction))), message=message)


def build_engine(paths: Any = None, model_path: Optional[Path] = None) -> Any:
    """Create the single Kokoro engine used by a job.

    Loading is left to the engine itself so the (slow) model read happens inside
    the job, on the worker thread, and can be reported as progress.
    """
    from app.tts.engine import KokoroEngine

    resolved = model_path
    if resolved is None and paths is not None:
        candidate = Path(paths.kokoro_model_dir)
        if candidate.is_dir():
            matches = sorted(
                entry for entry in candidate.rglob("*")
                if entry.is_file() and entry.suffix.lower() in (".onnx", ".pt", ".bin")
            )
            resolved = matches[0] if matches else None
    return KokoroEngine(model_path=resolved)


# --------------------------------------------------------------------------
# Kokoro initialisation (lazy - only when something needs audio)
# --------------------------------------------------------------------------

def kokoro_init_job(context: JobContext) -> dict:
    """Load the model once and report what was found.

    Returns a plain dict so the interface can show real facts instead of a
    guess based on whether a package imported.
    """
    from app.tts.capabilities import probe_kokoro, requirements_summary

    context.raise_if_cancelled()
    paths = context.paths
    model_dir = Path(paths.kokoro_model_dir) if paths is not None else None
    report_progress(context, 0.2, "Checking the Kokoro installation")
    status = probe_kokoro(model_dir=model_dir)
    context.raise_if_cancelled()

    if not status.ready:
        log_event(Event.KOKORO_INIT_FAILED, "Kokoro is not usable yet",
                  logger=LOGGER, problems=len(status.problems))
        # The summary is spread first: it carries its own "ready" key, and
        # spreading it last would silently overwrite the verdict below.
        return {**requirements_summary(status), "ready": False}

    report_progress(context, 0.6, "Loading the model")
    engine = build_engine(paths, status.model.path)
    try:
        engine.load()
        status.verified = True
        log_event(Event.KOKORO_INITIALIZED, "Kokoro model loaded",
                  logger=LOGGER, backend=engine.backend)
    except Exception as error:  # noqa: BLE001 - reported to the user
        log_event(Event.KOKORO_INIT_FAILED, "Kokoro failed to initialise",
                  logger=LOGGER, error=str(error))
        # status.ready is still True here - the probe passed, the load did not -
        # so the explicit verdict must win the merge.
        return {**requirements_summary(status), "ready": False, "error": str(error)}
    finally:
        engine.unload()

    report_progress(context, 1.0, "Kokoro is ready")
    return {**requirements_summary(status), "ready": True}


# --------------------------------------------------------------------------
# Voice catalogue scan (read-only; never generates audio)
# --------------------------------------------------------------------------

def voice_scan_job(context: JobContext) -> dict:
    """Discover voices and languages from the installed model."""
    from app.tts.capabilities import probe_kokoro
    from app.tts.voices import discover_voices

    context.raise_if_cancelled()
    report_progress(context, 0.3, "Scanning the Kokoro voice catalogue")
    paths = context.paths
    # A model dir set in Settings wins, exactly as in the Voice panel; otherwise
    # the app's own models folder is searched.
    configured = context.get("model_dir")
    if not configured and getattr(context.settings, "voice", None) is not None:
        configured = getattr(context.settings.voice, "model_dir", "") or ""
    if configured:
        model_dir = Path(configured)
    else:
        model_dir = Path(paths.kokoro_model_dir) if paths is not None else None
    favourites = context.get("favourites") or ()

    status = probe_kokoro(model_dir=model_dir)
    catalogue = discover_voices(status=status, favourites=favourites)
    log_event(Event.VOICES_SCANNED, "Voice catalogue scanned",
              logger=LOGGER, voices=catalogue.count,
              languages=len(catalogue.languages),
              source=catalogue.language_source)
    report_progress(context, 1.0, catalogue.describe())
    return {
        # Engine facts, so the Voice panel can fill its engine card from this
        # result instead of probing again on the UI thread.
        "installed": status.installed,
        "package_version": status.package_version,
        "runtime": status.runtime.describe,
        "model": status.model.describe(),
        "model_name": status.model.path.name if status.model.path else "",
        "headline": status.headline(),
        "verified": status.verified,
        "voices": [
            {
                "id": voice.id, "language": voice.language, "gender": voice.gender,
                "available": voice.available, "note": voice.note,
                "favorite": voice.favorite, "label": voice.label,
            }
            for voice in catalogue.voices
        ],
        "languages": list(catalogue.languages),
        "language_source": catalogue.language_source,
        "genders": catalogue.genders(),
        "summary": catalogue.describe(),
        "blocker": catalogue.blocker(),
    }


# --------------------------------------------------------------------------
# Voice preview (separate from generation - directive sections 9-10)
# --------------------------------------------------------------------------

def voice_preview_job(context: JobContext) -> dict:
    """Speak a short sample into the preview folder.

    Creates no project data, no narration track and no render job.  The file is
    written to the application's preview directory and replaced on the next
    preview, so previews cannot accumulate inside a project.
    """
    from app.tts.audio import write_wav
    from app.tts.engine import GenerationRequest
    from app.tts.preprocess import preprocess

    context.raise_if_cancelled()
    text = (context.get("text") or "").strip()
    voice = (context.get("voice") or "").strip()
    if not text:
        raise ValueError(
            "There is no preview text. Type a sentence to hear, then press Preview."
        )
    if not voice:
        raise ValueError("Choose a voice before previewing it.")

    paths = context.paths
    preview_dir = Path(paths.previews_dir) if paths is not None else Path.cwd()
    preview_dir.mkdir(parents=True, exist_ok=True)
    target = preview_dir / PREVIEW_FILENAME

    engine = build_engine(paths)
    engine.reset_cancel()
    try:
        log_event(Event.VOICE_PREVIEW_START, "Voice preview started",
                  logger=LOGGER, voice=voice, chars=len(text))
        report_progress(context, 0.2, "Loading the voice model")
        cleaned = preprocess(text, context.get("preprocess_options"))
        context.raise_if_cancelled()
        report_progress(context, 0.5, f"Speaking with {voice}")
        result = engine.synthesize(GenerationRequest(
            text=cleaned.text,
            voice=voice,
            language=context.get("language") or "",
            speed=context.get("speed") or 1.0,
            volume=context.get("volume") or 1.0,
        ))
        context.raise_if_cancelled()
        report_progress(context, 0.85, "Writing the preview file")
        info = write_wav(target, result.samples, result.sample_rate)
        if not info.valid:
            raise RuntimeError(
                "The preview audio could not be written: " + "; ".join(info.problems)
            )
        log_event(Event.VOICE_PREVIEW_COMPLETE, "Voice preview ready",
                  logger=LOGGER, voice=voice, duration=info.duration_seconds)
        report_progress(context, 1.0, "Preview ready")
        return {
            "path": str(target),
            "duration_seconds": round(info.duration_seconds, 3),
            "sample_rate": info.sample_rate,
            "voice": voice,
            "preprocessing_notes": list(cleaned.changes),
        }
    except JobCancelled:
        _remove_quietly(target)
        raise
    except Exception:
        _remove_quietly(target)
        raise
    finally:
        engine.unload()


# --------------------------------------------------------------------------
# Narration generation (one job, one pass)
# --------------------------------------------------------------------------

def narration_job(context: JobContext) -> dict:
    """Generate narration for the open project.

    The project object is handed in through the payload; committing the save is
    the caller's job so a failed generation never writes a half-updated project.
    """
    from app.tts.narration import (
        NarrationSettings,
        generate_narration,
    )

    context.raise_if_cancelled()
    project = context.get("project")
    project_dir = context.get("project_dir")
    if project is None or project_dir is None:
        raise ValueError("No project is open, so there is nothing to narrate.")

    settings = NarrationSettings(
        voice=context.get("voice") or project.voice.voice,
        language=context.get("language") or project.voice.language,
        speed=float(context.get("speed") if context.get("speed") is not None else project.voice.speed),
        volume=float(context.get("volume") if context.get("volume") is not None else project.voice.volume),
        sample_rate=int(project.voice.sample_rate or 24000),
        model_version=context.get("model_version") or "",
        preprocessing=context.get("preprocessing") or dict(project.narration.preprocessing or {}),
        preprocess_options=context.get("preprocess_options"),
        section_gap_seconds=float(project.narration.section_gap_seconds or 0.0),
    )

    def report(message: str, index: int, total: int) -> None:
        report_progress(context, max(0.05, index / max(1, total)), message)

    engine = build_engine(paths=context.paths)
    engine.reset_cancel()
    try:
        outcome = generate_narration(
            project,
            Path(project_dir),
            settings,
            engine=engine,
            section_ids=context.get("section_ids") or (),
            progress=report,
            cancel=context.is_cancelled,
        )
    finally:
        engine.unload()

    if not outcome.ok:
        error = outcome.error or to_friendly(RuntimeError("Narration generation failed."))
        log_event(Event.TTS_FAILED, "Narration generation failed",
                  logger=LOGGER, reason=error.title)
        raise RuntimeError(error.what_happened or error.title) from None

    report_progress(context, 1.0, outcome.summary())
    return {
        "summary": outcome.summary(),
        "files": [track.path for track in outcome.tracks],
        "durations": [track.actual_duration_seconds for track in outcome.tracks],
        "total_duration": outcome.total_duration,
        "preprocessing_notes": list(outcome.preprocessing_notes),
        "chunks": outcome.chunks,
        "elapsed_seconds": round(outcome.elapsed_seconds, 3),
    }


def _remove_quietly(path: Path) -> None:
    """Delete a partial preview file without masking the original error."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------

def init_spec(settings: Any = None, paths: Any = None) -> JobSpec:
    return JobSpec(
        key=JobKeys.KOKORO_INIT,
        title="Loading the Kokoro voice engine",
        body=kokoro_init_job,
        description="Checks the installation and loads the model once.",
        settings=settings,
        paths=paths,
    )


def scan_spec(settings: Any = None, paths: Any = None,
              favourites: Optional[list] = None) -> JobSpec:
    return JobSpec(
        key=JobKeys.VOICE_SCAN,
        title="Scanning available voices",
        body=voice_scan_job,
        description="Reads the installed voice catalogue.",
        allow_parallel=False,
        payload={"favourites": list(favourites or [])},
        settings=settings,
        paths=paths,
    )


def preview_spec(text: str,
                 voice: str,
                 *,
                 language: str = "",
                 speed: float = 1.0,
                 volume: float = 1.0,
                 preprocess_options: Any = None,
                 settings: Any = None,
                 paths: Any = None) -> JobSpec:
    return JobSpec(
        key=JobKeys.VOICE_PREVIEW,
        title=f"Previewing voice {voice or '(none)'}",
        body=voice_preview_job,
        description="Speaks the preview text; creates no project data.",
        payload={
            "text": text, "voice": voice, "language": language,
            "speed": speed, "volume": volume,
            "preprocess_options": preprocess_options,
        },
        settings=settings,
        paths=paths,
    )


def narration_spec(project: Any,
                   project_dir: Path,
                   *,
                   voice: str = "",
                   language: str = "",
                   speed: Optional[float] = None,
                   volume: Optional[float] = None,
                   model_version: str = "",
                   preprocessing: Optional[dict] = None,
                   preprocess_options: Any = None,
                   section_ids: Optional[list] = None,
                   settings: Any = None,
                   paths: Any = None) -> JobSpec:
    return JobSpec(
        key=JobKeys.TTS_NARRATION,
        title="Generating narration",
        body=narration_job,
        description="Runs Kokoro once and validates the audio it writes.",
        payload={
            "project": project,
            "project_dir": Path(project_dir),
            "voice": voice, "language": language,
            "speed": speed, "volume": volume,
            "model_version": model_version,
            "preprocessing": preprocessing,
            "preprocess_options": preprocess_options,
            "section_ids": list(section_ids or []),
        },
        settings=settings,
        paths=paths,
    )


__all__ = [
    "PREVIEW_FILENAME",
    "build_engine",
    "init_spec",
    "kokoro_init_job",
    "narration_job",
    "narration_spec",
    "preview_spec",
    "scan_spec",
    "voice_preview_job",
    "voice_scan_job",
]
