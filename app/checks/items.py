"""Built-in readiness checks.

Importing this module registers every check in the check registry (the
decorators run at import time, which is a normal Python pattern and has no
side effect other than filling a dict - no I/O, no subprocesses, no Qt).

Display order is defined by :data:`DISPLAY_ORDER` so the System Check page
always shows the same, predictable list.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from ..core import atomicio, env
from ..core.errors import MissingDependencyError, to_friendly
from ..core.packages import probe_many, probe_package
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..tools import ffmpeg as ffmpeg_tools
from ..tools import kokoro as kokoro_tools
from .status import CheckContext, CheckResult, Status, register_check

LOGGER = get_logger("checks")

#: Packages the application imports, with the reason each one is needed.
REQUIRED_PACKAGES: tuple[tuple[str, str, bool], ...] = (
    ("PySide6", "The user interface (required).", True),
    ("PIL", "Reading and validating images (required).", True),
    ("numpy", "Audio and image maths for previews and rendering (required).", True),
    ("soundfile", "Writing narration audio to disk (required from Stage C).", False),
)
# Third element: ``True`` = required now, ``False`` = required by a later stage.

#: Display order of the System Check page.
DISPLAY_ORDER: tuple[str, ...] = (
    "python.version",
    "filesystem.data_root",
    "filesystem.directories",
    "storage.output",
    "media.ffmpeg",
    "media.ffprobe",
    "media.ffmpeg_quick_test",
    "packages.core",
    "voice.kokoro",
    "voice.voices",
    "hardware.summary",
    "options.monitoring",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _format_path(path: object) -> str:
    return str(path)


# --------------------------------------------------------------------------
# Python / runtime
# --------------------------------------------------------------------------

@register_check("python.version", "Python runtime", "Always", "Python version and architecture.")
def check_python(context: CheckContext) -> CheckResult:
    from ..core.version import MIN_PYTHON

    version = sys.version_info
    version_text = f"{version.major}.{version.minor}.{version.micro}"
    is_64 = sys.maxsize > 2**32

    details = [
        f"Version: {version_text}",
        f"Architecture: {'64-bit' if is_64 else '32-bit'}",
        f"Executable: {sys.executable}",
    ]

    if (version.major, version.minor) < MIN_PYTHON:
        return CheckResult(
            check_id="python.version",
            title="Python runtime",
            status=Status.BLOCKED,
            summary=f"Python {version_text} is too old.",
            what_happened=f"This application needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, "
            f"but it is running on Python {version_text}.",
            why="Newer language and library features are required.",
            actions=(
                f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer from python.org.",
                "Delete and recreate the virtual environment with the new Python, then reinstall requirements.",
            ),
            details=details,
            required_for="Always",
        )
    if not is_64:
        return CheckResult(
            check_id="python.version",
            title="Python runtime",
            status=Status.BLOCKED,
            summary="32-bit Python cannot render HD video.",
            what_happened="This application is running on 32-bit Python.",
            why="A 32-bit process can only address about 2 GB of memory, which is not enough for HD video frames.",
            actions=("Install 64-bit Python from python.org and recreate the environment.",),
            details=details,
            required_for="Always",
        )
    return CheckResult(
        check_id="python.version",
        title="Python runtime",
        status=Status.READY,
        summary=f"Python {version_text} ({'64-bit' if is_64 else '32-bit'})",
        details=details,
        required_for="Always",
    )


# --------------------------------------------------------------------------
# Folders and storage
# --------------------------------------------------------------------------

@register_check("filesystem.data_root", "Application folders", "Always", "Where projects, assets and output are stored.")
def check_data_root(context: CheckContext) -> CheckResult:
    paths = context.paths
    data_root = Path(paths.data_root)
    from ..core.paths import directory_is_writable

    details = [
        f"Data folder: {data_root}",
        f"Chosen because: {paths.reason}",
        f"Source folder: {paths.source_root}",
    ]
    exists = data_root.is_dir()
    is_writable = directory_is_writable(data_root) if exists else False

    if not is_writable:
        from ..core.settings import disk_free_bytes

        return CheckResult(
            check_id="filesystem.data_root",
            title="Application folders",
            status=Status.BLOCKED,
            summary="The data folder cannot be written to.",
            what_happened=f"The folder below could not be created or written to:\n{data_root}",
            why=(
                "The location is read-only, belongs to another user, or is blocked by Windows "
                "(this commonly happens when the app is installed inside Program Files)."
            ),
            actions=(
                "Open Settings → Folders and choose a different data folder (for example in Documents).",
                "Or right-click the application shortcut and choose 'Run as administrator' once.",
            ),
            details=details + [f"Free space: {atomicio.human_size(disk_free_bytes(data_root))}"],
            technical=f"exists={exists} writable={is_writable}",
            required_for="Always",
        )

    return CheckResult(
        check_id="filesystem.data_root",
        title="Application folders",
        status=Status.READY,
        summary="Writable",
        details=details,
        required_for="Always",
    )


@register_check("filesystem.directories", "Project and asset folders", "Always", "Every layout folder exists and is writable.")
def check_directories(context: CheckContext) -> CheckResult:
    statuses = context.paths.directory_statuses()
    problems = [status for status in statuses if not (status.exists and status.writable)]
    details = [
        f"{status.name + '/':<12} {'ok' if status.exists and status.writable else 'PROBLEM'}  {status.path}"
        for status in statuses
    ]
    if problems:
        names = ", ".join(status.name for status in problems)
        return CheckResult(
            check_id="filesystem.directories",
            title="Project and asset folders",
            status=Status.BLOCKED,
            summary=f"{len(problems)} folder(s) are not usable: {names}",
            what_happened=f"These folders are missing or read-only: {names}",
            why="The data folder is not writable, or another program is holding one of the folders open.",
            actions=(
                "Press 'Re-check system' to try creating them again.",
                "If it keeps failing, choose a different data folder in Settings → Folders.",
            ),
            details=details,
            required_for="Always",
        )
    return CheckResult(
        check_id="filesystem.directories",
        title="Project and asset folders",
        status=Status.READY,
        summary=f"{len(statuses)} folders ready",
        details=details,
        required_for="Always",
    )


@register_check("storage.output", "Free disk space", "Rendering", "Enough room to render and export videos.")
def check_storage(context: CheckContext) -> CheckResult:
    paths = context.paths
    output_dir = Path(paths.output_dir)
    free = env.disk_free_bytes(output_dir if output_dir.exists() else paths.data_root)
    total = env.disk_total_bytes(output_dir if output_dir.exists() else paths.data_root)
    details = [
        f"Export folder: {output_dir}",
        f"Free space: {atomicio.human_size(free)} of {atomicio.human_size(total)}",
        "Guideline: about 1 GB free per minute of 1080p30 video.",
    ]

    if free and free < env.LOW_DISK_BLOCK_BYTES:
        return CheckResult(
            check_id="storage.output",
            title="Free disk space",
            status=Status.BLOCKED,
            summary=f"Only {atomicio.human_size(free)} free.",
            what_happened=f"The drive holding the export folder has only {atomicio.human_size(free)} free.",
            why=f"Rendering a video needs at least {atomicio.human_size(env.LOW_DISK_BLOCK_BYTES)} of working space.",
            actions=(
                "Free up space on this drive.",
                "Or move the data folder to a larger drive in Settings → Folders.",
                "Clear cached files in Settings → Maintenance.",
            ),
            details=details,
            technical=f"free_bytes={free}",
            required_for="Rendering",
        )
    if free and free < env.LOW_DISK_WARNING_BYTES:
        return CheckResult(
            check_id="storage.output",
            title="Free disk space",
            status=Status.WARNING,
            summary=f"{atomicio.human_size(free)} free - enough for short videos only.",
            what_happened=f"Only {atomicio.human_size(free)} of free space is available.",
            why="Long HD renders can need several gigabytes of temporary space.",
            actions=(
                "Consider freeing space before rendering a long video.",
                "Clear cached files in Settings → Maintenance.",
            ),
            details=details,
            required_for="Rendering",
        )
    return CheckResult(
        check_id="storage.output",
        title="Free disk space",
        status=Status.READY,
        summary=f"{atomicio.human_size(free)} free",
        details=details,
        required_for="Rendering",
    )


# --------------------------------------------------------------------------
# Media tools
# --------------------------------------------------------------------------

def _discovery(context: CheckContext) -> ffmpeg_tools.FFmpegDiscovery:
    """Reuse the discovery from the context, or perform one now."""
    if isinstance(context.ffmpeg_discovery, ffmpeg_tools.FFmpegDiscovery):
        return context.ffmpeg_discovery
    media = context.settings.media
    discovery = ffmpeg_tools.discover_ffmpeg(
        source_root=Path(context.paths.source_root),
        ffmpeg_dir_setting=media.ffmpeg_path if Path(media.ffmpeg_path or "").is_dir() else "",
        ffmpeg_exe_setting=media.ffmpeg_path if Path(media.ffmpeg_path or "").is_file() else "",
        ffprobe_exe_setting=media.ffprobe_path,
    )
    context.ffmpeg_discovery = discovery
    return discovery


@register_check("media.ffmpeg", "FFmpeg", "Rendering", "Creates the final video file.")
def check_ffmpeg(context: CheckContext) -> CheckResult:
    discovery = _discovery(context)
    details = [
        f"Searched: {len(discovery.searched)} location(s)",
        f"Executable: {discovery.ffmpeg.path if discovery.ffmpeg else 'not found'}",
    ]
    if discovery.ffmpeg and discovery.ffmpeg.version:
        details.append(f"Version: {discovery.ffmpeg.version}")
    if discovery.errors:
        details.extend(discovery.errors[:3])

    if discovery.has_ffmpeg:
        return CheckResult(
            check_id="media.ffmpeg",
            title="FFmpeg",
            status=Status.READY,
            summary=f"Found ({discovery.ffmpeg.version or 'version unknown'})",
            details=details,
            required_for="Rendering",
        )
    friendly = to_friendly(
        ffmpeg_tools.FFmpegNotFoundError(
            program="FFmpeg",
            searched=discovery.searched[:6],
            technical="\n".join(discovery.errors) or None,
        )
    )
    return CheckResult(
        check_id="media.ffmpeg",
        title="FFmpeg",
        status=Status.MISSING,
        summary="Not found",
        what_happened=friendly.what_happened,
        why=friendly.why,
        actions=friendly.actions,
        details=details,
        technical=friendly.technical or "",
        required_for="Rendering",
    )


@register_check("media.ffprobe", "FFprobe", "Rendering", "Measures and verifies finished videos.")
def check_ffprobe(context: CheckContext) -> CheckResult:
    discovery = _discovery(context)
    details = [f"Executable: {discovery.ffprobe.path if discovery.ffprobe else 'not found'}"]
    if discovery.ffprobe and discovery.ffprobe.version:
        details.append(f"Version: {discovery.ffprobe.version}")

    if discovery.has_ffprobe:
        return CheckResult(
            check_id="media.ffprobe",
            title="FFprobe",
            status=Status.READY,
            summary=f"Found ({discovery.ffprobe.version or 'version unknown'})",
            details=details,
            required_for="Rendering",
        )
    friendly = to_friendly(
        ffmpeg_tools.FFmpegNotFoundError(
            program="FFprobe",
            searched=discovery.searched[:6],
            technical="\n".join(discovery.errors) or None,
        )
    )
    return CheckResult(
        check_id="media.ffprobe",
        title="FFprobe",
        status=Status.MISSING,
        summary="Not found",
        what_happened=friendly.what_happened,
        why=friendly.why,
        actions=friendly.actions,
        details=details,
        technical=friendly.technical or "",
        required_for="Rendering",
    )


@register_check("media.ffmpeg_quick_test", "FFmpeg self-test", "Rendering", "Encodes one second of video to prove the tool chain works.")
def check_ffmpeg_selftest(context: CheckContext) -> CheckResult:
    """Optional, on-demand check (only runs in a deep check to save time)."""
    discovery = _discovery(context)
    if not discovery.is_complete:
        return CheckResult(
            check_id="media.ffmpeg_quick_test",
            title="FFmpeg self-test",
            status=Status.UNKNOWN,
            summary="Skipped - FFmpeg is not available",
            skipped=True,
            required_for="Rendering",
        )
    if not context.deep:
        return CheckResult(
            check_id="media.ffmpeg_quick_test",
            title="FFmpeg self-test",
            status=Status.UNKNOWN,
            summary="Skipped (runs on 'Re-check system')",
            skipped=True,
            required_for="Rendering",
        )

    tools = ffmpeg_tools.FFmpegTools(discovery)
    temp_dir = Path(context.paths.temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / "ffmpeg_selftest.mp4"
    try:
        if target.exists():
            target.unlink()
        result = tools.run(
            [
                "-hide_banner",
                "-loglevel", "error",
                "-f", "lavfi",
                "-i", "color=c=black:s=320x240:d=1:r=25",
                "-c:v", "mpeg4",
                "-y",
                str(target),
            ],
            timeout=45,
        )
    except Exception as exc:  # pragma: no cover - defensive
        friendly = to_friendly(exc, "The FFmpeg self-test could not run.")
        return CheckResult(
            check_id="media.ffmpeg_quick_test",
            title="FFmpeg self-test",
            status=Status.WARNING,
            summary="Could not run",
            what_happened=friendly.what_happened,
            why=friendly.why,
            actions=friendly.actions,
            technical=friendly.technical or "",
            required_for="Rendering",
        )

    size = target.stat().st_size if target.exists() else 0
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass

    if result.ok and size > 0:
        return CheckResult(
            check_id="media.ffmpeg_quick_test",
            title="FFmpeg self-test",
            status=Status.READY,
            summary=f"Encoded a 1 second test clip ({atomicio.human_size(size)})",
            details=[ffmpeg_tools.format_command(result.argv)],
            required_for="Rendering",
        )
    return CheckResult(
        check_id="media.ffmpeg_quick_test",
        title="FFmpeg self-test",
        status=Status.WARNING,
        summary="FFmpeg could not encode the test clip",
        what_happened="FFmpeg was found, but it failed to encode a short test video.",
        why="The executable is incomplete or blocked (some antivirus tools block freshly installed binaries).",
        actions=(
            "Try again after allowing the FFmpeg folder in your antivirus.",
            "Download FFmpeg again and place ffmpeg.exe and ffprobe.exe in the app's 'tools' folder.",
        ),
        technical=result.describe_failure(),
        required_for="Rendering",
    )


# --------------------------------------------------------------------------
# Python packages
# --------------------------------------------------------------------------

@register_check("packages.core", "Required Python packages", "Always", "GUI, imaging and audio libraries.")
def check_packages(context: CheckContext) -> CheckResult:
    statuses, missing_required, missing_later = probe_many(list(REQUIRED_PACKAGES))

    details: list[str] = []
    for status in statuses:
        if status.installed:
            details.append(status.line())
            log_event(Event.PACKAGE_DETECTED, f"{status.name} found", logger=LOGGER, package=status.name, version=status.version or "-")
        else:
            details.append(f"{status.name} - MISSING ({status.description})")
            log_event(Event.PACKAGE_MISSING, f"{status.name} is not installed", logger=LOGGER, package=status.name)

    if missing_required:
        names = ", ".join(name for name, _purpose in missing_required)
        friendly = MissingDependencyError(
            component="Required Python packages",
            why=f"These packages are required but were not found: {names}",
            actions=(
                "Install the requirements:  pip install -r requirements.txt",
                "Make sure you are using the same Python as the application.",
                "Then press 'Re-check system'.",
            ),
            technical=f"missing={names} executable={sys.executable}",
        ).friendly()
        return CheckResult(
            check_id="packages.core",
            title="Required Python packages",
            status=Status.MISSING,
            summary=f"Missing: {names}",
            what_happened=friendly.what_happened,
            why=friendly.why,
            actions=friendly.actions,
            details=details,
            technical=friendly.technical or "",
            required_for="Always",
        )

    if missing_later:
        names = ", ".join(name for name, _purpose in missing_later)
        return CheckResult(
            check_id="packages.core",
            title="Required Python packages",
            status=Status.WARNING,
            summary=f"Optional for now: {names}",
            what_happened=f"These packages are not installed yet: {names}",
            why="They are needed by the voice and rendering stages, which are not enabled in this build yet.",
            actions=("Install them with:  pip install -r requirements.txt",),
            details=details,
            required_for="Always",
        )

    return CheckResult(
        check_id="packages.core",
        title="Required Python packages",
        status=Status.READY,
        summary=f"{len(REQUIRED_PACKAGES)} packages ready",
        details=details,
        required_for="Always",
    )


# --------------------------------------------------------------------------
# Voice engine
# --------------------------------------------------------------------------

def _deep_kokoro_status(context: CheckContext):
    """The deep capability probe, cached on the context for the whole run.

    Section 22: this verifies that the engine can really be initialised, not just
    that a package imports.  It is cached so the three voice checks share one
    probe instead of scanning the model folder three times.
    """
    from app.tts.capabilities import probe_kokoro

    cached = getattr(context, "tts_status", None)
    if cached is not None:
        return cached
    settings = context.settings
    configured = getattr(settings.voice, "model_dir", "") or ""
    model_dir = Path(configured) if configured else Path(context.paths.kokoro_model_dir)
    status = probe_kokoro(model_dir=model_dir, deep_init_check=context.deep)
    context.tts_status = status
    return status


@register_check("voice.kokoro", "Kokoro voice engine", "Voice", "Local text-to-speech (Kokoro-82M).")
def check_kokoro(context: CheckContext) -> CheckResult:
    """Report the real state of the local Kokoro installation.

    Every component is reported separately - package, runtime, model weights,
    phonemiser - because "Kokoro is installed" is not the same statement as
    "Kokoro can speak", and the user needs to know which part is missing.
    """
    from app.tts.capabilities import requirements_summary

    status = _deep_kokoro_status(context)
    details = [
        f"package: {'installed' if status.installed else 'not installed'}"
        + (f" ({status.package_version})" if status.package_version else ""),
        f"runtime: {status.runtime.describe}",
        f"model: {status.model.describe()}",
        f"voices on disk: {len(status.voices)}",
        f"languages: {len(status.languages)} (from {status.language_source})",
        f"phonemiser: {', '.join(status.phonemizer) if status.phonemizer else 'none found'}",
    ]
    if status.verified:
        details.append("initialisation: verified by loading the model")
    elif status.import_error:
        details.append(f"import error: {status.import_error}")

    if status.ready:
        return CheckResult(
            check_id="voice.kokoro",
            title="Kokoro voice engine",
            status=Status.READY if status.verified else Status.WARNING,
            summary=status.headline(),
            why="" if status.verified else (
                "Everything needed is present, but the model has not been loaded "
                "in this run. Press 'Re-check system' to load it and verify."
            ),
            details=details,
            technical=str(requirements_summary(status)),
            required_for="Voice",
        )

    return CheckResult(
        check_id="voice.kokoro",
        title="Kokoro voice engine",
        status=Status.OPTIONAL if not status.installed else Status.WARNING,
        summary=status.headline(),
        what_happened="; ".join(status.problems) or "Kokoro is not ready.",
        why=(
            "Narration is optional: a project can still be written, organised and "
            "saved without a voice. Everything else in the application works."
        ),
        actions=tuple(status.instructions) or kokoro_tools.install_instructions(),
        details=details,
        technical=status.verification_error or status.import_error,
        required_for="Voice",
    )


@register_check("voice.voices", "Installed voices", "Voice", "Voice catalogue discovered from the installed Kokoro model.")
def check_voices(context: CheckContext) -> CheckResult:
    """Report the voices discovered on disk - never a hard-coded list."""
    from app.tts.voices import discover_voices

    status = _deep_kokoro_status(context)
    catalogue = discover_voices(status=status)

    if catalogue.count == 0:
        return CheckResult(
            check_id="voice.voices",
            title="Installed voices",
            status=Status.OPTIONAL,
            summary=catalogue.reason or "No voices were discovered.",
            why="Voice names always come from the installed model - none are hard-coded.",
            actions=(
                "Install Kokoro and place the model and voice files in the models folder.",
                "Then press 'Re-check system'.",
            ),
            required_for="Voice",
        )

    blocker = catalogue.blocker()
    details = [catalogue.describe()]
    for voice in catalogue.voices[:12]:
        details.append(f"{voice.id} · {voice.language or '?'} · {voice.gender}")
    if catalogue.count > 12:
        details.append(f"… and {catalogue.count - 12} more")

    return CheckResult(
        check_id="voice.voices",
        title="Installed voices",
        status=Status.READY if not blocker else Status.WARNING,
        summary=catalogue.describe(),
        what_happened=blocker,
        why="" if not blocker else (
            "The voice files were found, but the engine cannot use them yet."
        ),
        actions=() if not blocker else tuple(status.instructions),
        details=details,
        required_for="Voice",
    )


@register_check("voice.languages", "Narration languages", "Voice", "Languages the installed pipeline actually supports.")
def check_languages(context: CheckContext) -> CheckResult:
    """Report the language catalogue, and where it came from (section 33).

    The list is read from the installed engine or derived from the discovered
    voice identifiers; it is never a fixed table in this file.
    """
    from app.tts.voices import language_label

    status = _deep_kokoro_status(context)

    if not status.languages:
        return CheckResult(
            check_id="voice.languages",
            title="Narration languages",
            status=Status.OPTIONAL,
            summary="Available once the Kokoro model is installed",
            why="Only languages the installed pipeline supports are ever offered.",
            actions=("Install Kokoro and its model, then press 'Re-check system'.",),
            required_for="Voice",
        )

    labels = ", ".join(f"{language_label(code)} ({code})" for code in status.languages)
    return CheckResult(
        check_id="voice.languages",
        title="Narration languages",
        status=Status.READY,
        summary=f"{len(status.languages)} languages (detected from {status.language_source})",
        details=[labels],
        required_for="Voice",
    )


@register_check("voice.selftest", "Narration self-test", "Voice", "Generates one short sentence to prove the pipeline works.")
def check_tts_selftest(context: CheckContext) -> CheckResult:
    """Run a real, tiny generation (directive section 50).

    Only on a manual re-check: generating audio takes seconds and must not slow
    down the automatic startup check.  The audio is written to the application's
    preview folder and deleted afterwards, so nothing is left behind.
    """
    from app.tts.capabilities import SELF_TEST_TEXT
    from app.tts.audio import write_wav
    from app.tts.engine import GenerationRequest, KokoroEngine

    if not context.deep:
        return CheckResult(
            check_id="voice.selftest",
            title="Narration self-test",
            status=Status.UNKNOWN,
            summary="Runs when you press 'Re-check system'",
            why="The self-test really generates audio, so it is not run on startup.",
            skipped=True,
            required_for="Voice",
        )

    status = _deep_kokoro_status(context)
    if not status.ready:
        return CheckResult(
            check_id="voice.selftest",
            title="Narration self-test",
            status=Status.OPTIONAL,
            summary="Skipped: Kokoro is not ready",
            what_happened=status.headline(),
            why="The self-test speaks a real sentence, which needs a working engine.",
            actions=tuple(status.instructions),
            required_for="Voice",
        )

    if not status.voices:
        return CheckResult(
            check_id="voice.selftest",
            title="Narration self-test",
            status=Status.WARNING,
            summary="No voice available to speak with",
            actions=("Place the Kokoro voice files next to the model weights.",),
            required_for="Voice",
        )

    target = Path(context.paths.previews_dir) / "system_check_selftest.wav"
    engine = KokoroEngine(model_path=status.model.path, runtime=status.runtime.name or None)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = engine.synthesize(GenerationRequest(text=SELF_TEST_TEXT, voice=status.voices[0]))
        info = write_wav(target, result.samples, result.sample_rate)
    except Exception as error:  # noqa: BLE001 - the whole point is to report it
        return CheckResult(
            check_id="voice.selftest",
            title="Narration self-test",
            status=Status.WARNING,
            summary="The self-test could not generate audio",
            what_happened=str(error),
            why="The engine loaded but produced no usable audio.",
            actions=(
                "Try a different voice from the Voice page.",
                "Check the model files are complete, then re-check.",
            ),
            technical=f"{type(error).__name__}: {error}",
            required_for="Voice",
        )
    finally:
        engine.unload()

    if not info.valid:
        return CheckResult(
            check_id="voice.selftest",
            title="Narration self-test",
            status=Status.WARNING,
            summary="The generated test audio is not valid",
            what_happened="; ".join(info.problems),
            actions=("Run the self-test again; if it repeats, reinstall the Kokoro model.",),
            required_for="Voice",
        )

    summary = (
        f"Spoke “{SELF_TEST_TEXT}” as {info.duration_label()} of "
        f"{info.sample_rate} Hz audio with voice {status.voices[0]}."
    )
    try:
        target.unlink()
    except OSError:
        pass
    return CheckResult(
        check_id="voice.selftest",
        title="Narration self-test",
        status=Status.READY,
        summary=summary,
        details=["The test file was deleted after validation."],
        required_for="Voice",
    )


# --------------------------------------------------------------------------
# Hardware summary (informational, never blocks)
# --------------------------------------------------------------------------

@register_check("hardware.summary", "Rendering mode", "Rendering", "Video is produced on the CPU; a GPU is optional.")
def check_hardware(context: CheckContext) -> CheckResult:
    info = context.environment
    if info is None:
        return CheckResult(
            check_id="hardware.summary",
            title="Rendering mode",
            status=Status.UNKNOWN,
            summary="Hardware information is not available",
            required_for="Rendering",
        )

    details = info.summary_lines()
    gpu_text = ", ".join(info.gpu_names) if info.gpu_names else "none detected"
    details.append(f"Detected graphics: {gpu_text}")

    low_ram = info.ram_total_bytes is not None and info.ram_total_bytes < env.RECOMMENDED_RAM_BYTES
    if low_ram:
        return CheckResult(
            check_id="hardware.summary",
            title="Rendering mode",
            status=Status.WARNING,
            summary="CPU rendering, less than 16 GB RAM",
            what_happened="This computer has less memory than recommended for HD video rendering.",
            why="Rendering uses memory for frames and audio mixing; a small amount of RAM makes long videos slow.",
            actions=(
                "Render at 720p or use fewer effects for long videos.",
                "Close other memory-heavy programs while rendering.",
            ),
            details=details,
            required_for="Rendering",
        )
    return CheckResult(
        check_id="hardware.summary",
        title="Rendering mode",
        status=Status.READY,
        summary="CPU rendering (no GPU required)",
        details=details,
        required_for="Rendering",
    )


@register_check("options.monitoring", "Resource monitoring", "Optional", "CPU and RAM display in the status bar.")
def check_monitoring(context: CheckContext) -> CheckResult:
    status = probe_package("psutil")
    installed, version = status.installed, status.version
    if installed:
        return CheckResult(
            check_id="options.monitoring",
            title="Resource monitoring",
            status=Status.READY,
            summary=f"psutil {version}".strip(),
            required_for="Optional",
        )
    return CheckResult(
        check_id="options.monitoring",
        title="Resource monitoring",
        status=Status.OPTIONAL,
        summary="Not installed - CPU/RAM display is limited",
        what_happened="The optional 'psutil' package is not installed.",
        why="It is only used to show live CPU and memory usage; everything else works without it.",
        actions=("Install it with:  pip install psutil",),
        required_for="Optional",
    )


def build_context(
    paths: object,
    settings: object,
    deep: bool = False,
    environment: Optional[object] = None,
    ffmpeg_discovery: Optional[object] = None,
    kokoro_status: Optional[object] = None,
) -> CheckContext:
    """Create a check context, probing what is missing.

    Kept here (next to the checks) so callers - GUI, CLI, tests - build the
    context the same way every time.
    """
    if environment is None:
        environment = env.probe_environment()

    if ffmpeg_discovery is None:
        media = settings.media
        ffmpeg_path = (media.ffmpeg_path or "").strip()
        ffprobe_path = (media.ffprobe_path or "").strip()
        ffmpeg_discovery = ffmpeg_tools.discover_ffmpeg(
            source_root=Path(paths.source_root) if media.use_bundled_tools else None,
            ffmpeg_dir_setting=ffmpeg_path if Path(ffmpeg_path).is_dir() else "",
            ffmpeg_exe_setting=ffmpeg_path if Path(ffmpeg_path).is_file() else "",
            ffprobe_exe_setting=ffprobe_path,
            extra_dirs=(Path(paths.tools_dir), Path(paths.data_root)),
        )

    if kokoro_status is None:
        model_dir = Path(settings.voice.model_dir) if settings.voice.model_dir else Path(paths.kokoro_model_dir)
        kokoro_status = kokoro_tools.probe_kokoro(model_dir=model_dir, deep_import_check=False)

    return CheckContext(
        paths=paths,
        settings=settings,
        ffmpeg_discovery=ffmpeg_discovery,
        kokoro_status=kokoro_status,
        environment=environment,
        deep=deep,
    )


def quick_readiness_summary(report) -> str:
    """One-line status for the status bar."""
    counts = report.summary_counts()
    ready = counts.get(Status.READY.value, 0)
    missing = counts.get(Status.MISSING.value, 0) + counts.get(Status.BLOCKED.value, 0)
    optional = counts.get(Status.OPTIONAL.value, 0) + counts.get(Status.WARNING.value, 0)
    parts = [f"{ready} ready"]
    if missing:
        parts.append(f"{missing} missing")
    if optional:
        parts.append(f"{optional} optional")
    return "System: " + ", ".join(parts)


def clear_old_self_test_files(paths) -> int:
    """Remove leftover self-test files from the temp folder (safety net)."""
    removed = 0
    temp_dir = Path(paths.temp_dir)
    for name in ("ffmpeg_selftest.mp4", "ffmpeg_selftest.mkv"):
        candidate = temp_dir / name
        try:
            if candidate.exists():
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed


__all__ = [
    "DISPLAY_ORDER",
    "REQUIRED_PACKAGES",
    "build_context",
    "check_ffmpeg_selftest",
    "clear_old_self_test_files",
    "quick_readiness_summary",
]
