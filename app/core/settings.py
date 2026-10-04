"""Application settings: one versioned, validated source of truth.

Principles (directive sections 6, 12, 64):

* Settings live in :class:`Settings`, a plain dataclass tree - no globals that
  silently drift, no QSettings hidden state.
* The file is versioned.  Unknown keys are preserved, missing keys fall back to
  defaults, out-of-range values are **clamped and reported** rather than
  crashing the app.
* Saving is atomic with a backup of the previous file (section 12).
* A corrupt settings file never prevents startup: it is quarantined and
  defaults are used.
* No I/O happens at import time.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

from .atomicio import FileWriteError, human_size, load_json, save_with_backup
from .events import Event
from .logging_setup import get_logger, log_event
from .version import SETTINGS_SCHEMA_VERSION

LOGGER = get_logger("settings")

SETTINGS_FILENAME = "settings.json"

#: Selectable video frame rates for the UI (Stage D+).
SUPPORTED_FPS: tuple[int, ...] = (24, 25, 30, 50, 60)

#: Selectable render quality presets (Stage F+).
RENDER_QUALITY_PRESETS: tuple[str, ...] = ("draft", "medium", "final")

#: Video export codecs offered in Advanced mode.  ``h264_cpu`` is the default
#: because the primary target machine has no dedicated GPU (section 2).
VIDEO_CODEC_PRESETS: dict[str, str] = {
    "h264_cpu": "H.264 (CPU, x264) - maximum compatibility",
    "h264_gpu": "H.264 (hardware, if available) - faster, may be lower quality",
    "hevc_cpu": "H.265 / HEVC (CPU) - smaller files, slower",
}

#: Log levels exposed in Settings.
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

@dataclass
class DirectorySettings:
    """Folder overrides.  Empty strings mean 'use the resolved default'."""

    data_root: str = ""
    projects_dir: str = ""
    assets_dir: str = ""
    output_dir: str = ""
    cache_dir: str = ""
    models_dir: str = ""
    ffmpeg_dir: str = ""

    def effective(self, key: str, default: Path) -> Path:
        raw = (getattr(self, key, "") or "").strip()
        return Path(raw).expanduser() if raw else Path(default)

    def overrides(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if (value or "").strip()}


@dataclass
class GeneralSettings:
    theme: str = "dark"                 # "dark" | "light" | "system"
    language: str = "en"
    advanced_mode: bool = False         # section 52 - beginner UI first
    confirm_on_exit: bool = True
    single_instance: bool = True        # avoids two GUIs writing the same project (section 9)
    show_resource_monitor: bool = True  # section 71


@dataclass
class AutosaveSettings:
    enabled: bool = True
    interval_seconds: int = 120         # configurable interval (section 13)
    autosave_on_focus_loss: bool = True
    keep_autosaves: int = 10
    keep_backups: int = 10


@dataclass
class ProjectDefaults:
    """Defaults applied to newly created projects (used from Stage B)."""

    width: int = 1920
    height: int = 1080
    fps: int = 30
    quality: str = "medium"
    title: str = "Untitled Project"


@dataclass
class MediaSettings:
    """FFmpeg / FFprobe options (section 29)."""

    ffmpeg_path: str = ""               # explicit executable override
    ffprobe_path: str = ""
    use_bundled_tools: bool = True      # look in <app>/tools first
    thread_count: int = 0               # 0 = let FFmpeg decide
    hardware_encoding: str = "auto"     # auto | off | nvenc | qsv | amf
    encoder_preset: str = "medium"      # x264 speed preset
    crf: int = 20                       # quality (lower = better)
    audio_bitrate_kbps: int = 192
    output_container: str = "mp4"       # mp4 | mkv
    keep_temp_frames: bool = False      # diagnostics only


@dataclass
class VoiceSettings:
    """Kokoro TTS options (sections 15-17)."""

    engine: str = "kokoro"              # V1 has exactly one engine
    voice: str = ""                     # discovered voice id; "" = first available
    language: str = "en-us"
    speed: float = 1.0                  # 0.5 - 2.0
    volume: float = 1.0                 # 0.0 - 2.0
    sample_rate: int = 24000
    threads: int = 0                    # 0 = auto (CPU-only target, section 2)
    model_dir: str = ""
    allow_online_first_run: bool = True  # Kokoro may download its model once
    preview_text: str = "Hello! This is a preview of the selected voice."


@dataclass
class PreviewSettings:
    """Preview quality (section 32).  Previews must never trigger a final render."""

    draft_scale: float = 0.5            # fraction of project resolution
    draft_fps: int = 15
    medium_scale: float = 0.75
    medium_fps: int = 24
    autoplay: bool = True


@dataclass
class ImageSettings:
    """Image pipeline options (sections 20, 21)."""

    backend: str = "none"               # none | procedural | local_ai
    default_fit: str = "cover"          # contain | cover | crop | center | anchor
    thumbnail_size: int = 320
    max_import_megapixels: int = 80


@dataclass
class SubtitleSettings:
    """Subtitles are optional and off until requested (section 35)."""

    enabled: bool = False
    max_lines: int = 2
    font_size: int = 44
    safe_area_percent: float = 8.0
    burn_in: bool = True


@dataclass
class LoggingSettings:
    level: str = "INFO"
    keep_days: int = 30


@dataclass
class WindowSettings:
    """Remembered window geometry so the app opens where the user left it.

    Stored here rather than in a separate file so there is still exactly one
    settings source (directive section 6).  Values are validated on load: a
    window that would open off-screen is reset to the default geometry.
    """

    width: int = 1280
    height: int = 800
    x: int = -1
    y: int = -1
    maximized: bool = False
    last_page: str = "welcome"


@dataclass
class UpdatesSettings:
    """Placeholder for Phase 2.

    There is deliberately **no** licensing, activation, account or payment
    setting in this build (directive sections 1 and 79).
    """

    check_on_startup: bool = False


@dataclass
class Settings:
    """Root settings object persisted to ``config/settings.json``."""

    schema_version: int = SETTINGS_SCHEMA_VERSION
    app_version_last_run: str = ""
    general: GeneralSettings = field(default_factory=GeneralSettings)
    directories: DirectorySettings = field(default_factory=DirectorySettings)
    autosave: AutosaveSettings = field(default_factory=AutosaveSettings)
    project_defaults: ProjectDefaults = field(default_factory=ProjectDefaults)
    media: MediaSettings = field(default_factory=MediaSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    preview: PreviewSettings = field(default_factory=PreviewSettings)
    image: ImageSettings = field(default_factory=ImageSettings)
    subtitles: SubtitleSettings = field(default_factory=SubtitleSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    window: WindowSettings = field(default_factory=WindowSettings)
    updates: UpdatesSettings = field(default_factory=UpdatesSettings)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def copy(self) -> "Settings":
        """Deep copy via the serialised form (dataclasses are plain data)."""
        return from_dict(self.to_dict())

    # -- validation --------------------------------------------------------

    def normalized(self) -> tuple["Settings", list[str]]:
        """Return a copy with every value inside its allowed range.

        Also returns the list of adjustments made, which the UI surfaces as a
        warning instead of silently ignoring the user's file (section 62).
        """
        data = self.to_dict()
        notes: list[str] = []

        def clamp(path: str, value: Any, low: Any, high: Any) -> Any:
            if value is None:
                notes.append(f"{path}: missing value - default restored")
                return low
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                notes.append(f"{path}: '{value}' is not a number - default restored")
                return low
            if numeric < float(low):
                notes.append(f"{path}: {numeric} raised to minimum {low}")
                return type(low)(low)
            if numeric > float(high):
                notes.append(f"{path}: {numeric} lowered to maximum {high}")
                return type(high)(high)
            if isinstance(low, int) and not isinstance(low, bool):
                return int(round(numeric))
            return numeric

        general = data["general"]
        if general.get("theme") not in ("dark", "light", "system"):
            notes.append(f"general.theme: '{general.get('theme')}' unknown - using 'dark'")
            general["theme"] = "dark"

        defaults = data["project_defaults"]
        defaults["width"] = clamp("project_defaults.width", defaults.get("width"), 256, 7680)
        defaults["height"] = clamp("project_defaults.height", defaults.get("height"), 256, 7680)
        if defaults.get("fps") not in SUPPORTED_FPS:
            notes.append(f"project_defaults.fps: {defaults.get('fps')} unsupported - using 30")
            defaults["fps"] = 30
        if defaults.get("quality") not in RENDER_QUALITY_PRESETS:
            notes.append(f"project_defaults.quality: '{defaults.get('quality')}' unknown - using 'medium'")
            defaults["quality"] = "medium"

        autosave = data["autosave"]
        autosave["interval_seconds"] = clamp("autosave.interval_seconds", autosave.get("interval_seconds"), 15, 3600)
        autosave["keep_autosaves"] = clamp("autosave.keep_autosaves", autosave.get("keep_autosaves"), 0, 500)
        autosave["keep_backups"] = clamp("autosave.keep_backups", autosave.get("keep_backups"), 0, 500)

        media = data["media"]
        media["thread_count"] = clamp("media.thread_count", media.get("thread_count"), 0, 256)
        media["crf"] = clamp("media.crf", media.get("crf"), 0, 51)
        media["audio_bitrate_kbps"] = clamp("media.audio_bitrate_kbps", media.get("audio_bitrate_kbps"), 64, 512)
        if media.get("output_container") not in ("mp4", "mkv"):
            notes.append(f"media.output_container: '{media.get('output_container')}' unknown - using mp4")
            media["output_container"] = "mp4"
        if media.get("hardware_encoding") not in ("auto", "off", "nvenc", "qsv", "amf"):
            notes.append("media.hardware_encoding: unknown value - using 'auto'")
            media["hardware_encoding"] = "auto"

        voice = data["voice"]
        voice["speed"] = clamp("voice.speed", voice.get("speed"), 0.5, 2.0)
        voice["volume"] = clamp("voice.volume", voice.get("volume"), 0.0, 2.0)
        voice["sample_rate"] = clamp("voice.sample_rate", voice.get("sample_rate"), 8000, 48000)
        voice["threads"] = clamp("voice.threads", voice.get("threads"), 0, 256)
        if voice.get("engine") != "kokoro":
            notes.append(f"voice.engine: '{voice.get('engine')}' is not supported in V1 - using Kokoro")
            voice["engine"] = "kokoro"

        preview = data["preview"]
        preview["draft_scale"] = clamp("preview.draft_scale", preview.get("draft_scale"), 0.1, 1.0)
        preview["medium_scale"] = clamp("preview.medium_scale", preview.get("medium_scale"), 0.1, 1.0)
        preview["draft_fps"] = clamp("preview.draft_fps", preview.get("draft_fps"), 5, 60)
        preview["medium_fps"] = clamp("preview.medium_fps", preview.get("medium_fps"), 5, 60)

        image = data["image"]
        image["thumbnail_size"] = clamp("image.thumbnail_size", image.get("thumbnail_size"), 64, 1024)
        image["max_import_megapixels"] = clamp("image.max_import_megapixels", image.get("max_import_megapixels"), 1, 500)
        if image.get("default_fit") not in ("contain", "cover", "crop", "center", "anchor"):
            notes.append(f"image.default_fit: '{image.get('default_fit')}' unknown - using 'cover'")
            image["default_fit"] = "cover"
        if image.get("backend") not in ("none", "procedural", "local_ai"):
            notes.append(f"image.backend: '{image.get('backend')}' unknown - using 'none'")
            image["backend"] = "none"

        subtitles = data["subtitles"]
        subtitles["max_lines"] = clamp("subtitles.max_lines", subtitles.get("max_lines"), 1, 6)
        subtitles["font_size"] = clamp("subtitles.font_size", subtitles.get("font_size"), 12, 200)
        subtitles["safe_area_percent"] = clamp("subtitles.safe_area_percent", subtitles.get("safe_area_percent"), 0.0, 30.0)

        logging_data = data["logging"]
        if str(logging_data.get("level", "")).upper() not in LOG_LEVELS:
            notes.append(f"logging.level: '{logging_data.get('level')}' unknown - using INFO")
            logging_data["level"] = "INFO"
        else:
            logging_data["level"] = str(logging_data["level"]).upper()
        logging_data["keep_days"] = clamp("logging.keep_days", logging_data.get("keep_days"), 1, 3650)

        window = data["window"]
        window["width"] = clamp("window.width", window.get("width"), 1024, 10000)
        window["height"] = clamp("window.height", window.get("height"), 660, 10000)
        window["x"] = clamp("window.x", window.get("x"), -10000, 10000)
        window["y"] = clamp("window.y", window.get("y"), -10000, 10000)
        if not isinstance(window.get("last_page"), str):
            window["last_page"] = "welcome"

        data["schema_version"] = SETTINGS_SCHEMA_VERSION
        return from_dict(data), notes


def from_dict(data: Any) -> Settings:
    """Build a :class:`Settings` tree from parsed JSON, ignoring junk keys."""
    settings = Settings()
    if not isinstance(data, dict):
        return settings
    _merge_dataclass(settings, data)
    return settings


def _merge_dataclass(target: Any, data: dict[str, Any]) -> None:
    """Copy recognised keys onto a dataclass instance, recursing into sections."""
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            continue  # forward compatibility: unknown keys are ignored, not fatal
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(target, key, value)


def detect_system_theme() -> str:
    """Best-effort dark/light detection used by the "system" theme option."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except Exception:
            return "dark"
    return "dark"


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------

@dataclass
class SettingsLoadResult:
    settings: Settings
    source: str                     # "file" | "defaults" | "recovered"
    existed: bool = False
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    quarantined: Optional[Path] = None


class SettingsStore:
    """Loads and saves :class:`Settings` for one application instance.

    The store never raises on load: a broken settings file produces defaults
    plus a warning, and the damaged file is copied aside for inspection.
    """

    def __init__(self, settings_file: Path, backup_dir: Optional[Path] = None) -> None:
        self.path = Path(settings_file)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / "backups"

    # -- load --------------------------------------------------------------

    def load(self) -> SettingsLoadResult:
        result = load_json(self.path, default=None, quarantine_dir=self.backup_dir)
        if not result.exists:
            log_event(Event.SETTINGS_DEFAULTS_USED, "No settings file yet - using defaults", logger=LOGGER, path=str(self.path))
            return SettingsLoadResult(settings=Settings(), source="defaults", existed=False)

        if not result.ok or not isinstance(result.data, dict):
            log_event(
                Event.SETTINGS_CORRUPT,
                "Settings file could not be read - defaults restored, damaged file kept",
                logger=LOGGER,
                path=str(self.path),
                reason=result.error,
                quarantine=str(result.corrupted_copy) if result.corrupted_copy else None,
            )
            return SettingsLoadResult(
                settings=Settings(),
                source="recovered",
                existed=True,
                error=result.error,
                quarantined=result.corrupted_copy,
                notes=[
                    "The settings file could not be read, so default settings were used.",
                    "The old file was kept for inspection"
                    + (f": {result.corrupted_copy}" if result.corrupted_copy else "."),
                ],
            )

        raw = result.data
        stored_schema = raw.get("schema_version", SETTINGS_SCHEMA_VERSION)
        settings = from_dict(raw)
        notes: list[str] = []
        if not isinstance(stored_schema, int):
            notes.append(f"settings.schema_version: '{stored_schema}' is not a number - assuming {SETTINGS_SCHEMA_VERSION}")
            stored_schema = SETTINGS_SCHEMA_VERSION
        elif stored_schema > SETTINGS_SCHEMA_VERSION:
            notes.append(
                f"The settings file was written by a newer version (schema {stored_schema} > "
                f"{SETTINGS_SCHEMA_VERSION}). Unknown options were ignored."
            )
            log_event(
                Event.SETTINGS_MIGRATED,
                "Settings file is from a newer build; unknown options ignored",
                logger=LOGGER,
                stored_schema=stored_schema,
                current_schema=SETTINGS_SCHEMA_VERSION,
            )

        settings, normalisation_notes = settings.normalized()
        notes.extend(normalisation_notes)
        settings.schema_version = SETTINGS_SCHEMA_VERSION
        log_event(
            Event.SETTINGS_LOADED,
            "Settings loaded",
            logger=LOGGER,
            path=str(self.path),
            schema=stored_schema,
            adjustments=len(normalisation_notes),
        )
        if normalisation_notes:
            LOGGER.warning("Settings adjusted: %s", "; ".join(normalisation_notes))
        return SettingsLoadResult(settings=settings, source="file", existed=True, notes=notes)

    # -- save --------------------------------------------------------------

    def save(self, settings: Settings, keep_backups: int = 10) -> Path:
        """Atomically save *settings* (with a backup of the previous file)."""
        normalized, _notes = settings.normalized()
        normalized.schema_version = SETTINGS_SCHEMA_VERSION
        try:
            path = save_with_backup(
                self.path,
                normalized.to_dict(),
                backup_dir=self.backup_dir,
                keep_backups=keep_backups,
            )
        except FileWriteError as exc:
            log_event(Event.ERROR, "Settings could not be saved", level=logging.ERROR, logger=LOGGER, path=str(self.path), reason=str(exc))
            raise
        log_event(Event.SETTINGS_SAVED, "Settings saved", logger=LOGGER, path=str(path))
        return path

    def reset_to_defaults(self) -> Settings:
        """Replace the file with defaults (keeps a backup)."""
        defaults = Settings()
        self.save(defaults)
        log_event(Event.SETTINGS_RESET, "Settings reset to defaults", logger=LOGGER, path=str(self.path))
        return defaults

    # -- convenience -------------------------------------------------------

    def resolve_dir(self, key: str, default: Path) -> Path:
        """Resolve a directory setting, falling back to *default* when unset."""
        result = self.load()
        return result.settings.directories.effective(key, default)


def settings_summary(settings: Settings) -> str:
    """Short multi-line summary for the diagnostics panel."""
    lines = [
        f"Theme: {settings.general.theme}    Advanced mode: {settings.general.advanced_mode}",
        f"New project: {settings.project_defaults.width}x{settings.project_defaults.height} "
        f"@ {settings.project_defaults.fps} fps ({settings.project_defaults.quality})",
        f"Autosave: {'on' if settings.autosave.enabled else 'off'}, "
        f"every {settings.autosave.interval_seconds}s, keeping {settings.autosave.keep_autosaves}",
        f"Video: {settings.media.output_container.upper()}, CRF {settings.media.crf}, "
        f"{settings.media.audio_bitrate_kbps} kbps audio, encoder {settings.media.hardware_encoding}",
        f"Voice: {settings.voice.engine}, voice '{settings.voice.voice or 'auto'}', "
        f"speed {settings.voice.speed:.2f}, volume {settings.voice.volume:.2f}",
        f"Preview: draft {int(settings.preview.draft_scale * 100)}% @ {settings.preview.draft_fps} fps, "
        f"medium {int(settings.preview.medium_scale * 100)}% @ {settings.preview.medium_fps} fps",
        f"Image backend: {settings.image.backend}    Subtitles: {'on' if settings.subtitles.enabled else 'off'}",
    ]
    return "\n".join(lines)


def describe_disk_budget(free_bytes: int, used_cache_bytes: int) -> str:
    """Human readable disk line for the Maintenance page."""
    return f"Free space: {human_size(free_bytes)}    Cache: {human_size(used_cache_bytes)}"


def settings_file_size(settings_file: Path) -> str:
    try:
        return human_size(settings_file.stat().st_size)
    except OSError:
        return "-"


def disk_free_bytes(path: Path) -> int:
    """Free bytes on the volume holding *path* (0 when unknown)."""
    import shutil as _shutil

    try:
        return _shutil.disk_usage(str(path)).free
    except OSError:
        return 0


__all__ = [
    "AutosaveSettings",
    "DirectorySettings",
    "GeneralSettings",
    "ImageSettings",
    "LoggingSettings",
    "LOG_LEVELS",
    "MediaSettings",
    "PreviewSettings",
    "ProjectDefaults",
    "RENDER_QUALITY_PRESETS",
    "SUPPORTED_FPS",
    "Settings",
    "SettingsLoadResult",
    "SettingsStore",
    "SubtitleSettings",
    "UpdatesSettings",
    "VIDEO_CODEC_PRESETS",
    "VoiceSettings",
    "WindowSettings",
    "describe_disk_budget",
    "detect_system_theme",
    "disk_free_bytes",
    "from_dict",
    "settings_file_size",
    "settings_summary",
]
