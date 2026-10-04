"""Project validation.

Rule (directive section 14): **collect every problem, then report**.  A user
fixing one field at a time is a user who gives up, so the report says
"3 issues found" and lists all of them, each with the field, what is wrong and
how to fix it.

Levels
------
``error``    the project cannot be saved/rendered like this
``warning``  it will work, but not the way you probably meant
``info``     worth knowing, nothing to do

Nothing here touches Qt and nothing writes to disk.  File existence is only
checked when the caller passes ``check_files=True`` (opening a project), so
validation stays fast enough to run on every keystroke-driven dirty check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.version import PROJECT_SCHEMA_VERSION
from .model import ANCHORS, ASSET_KINDS, SCENE_TYPES, TRANSITION_TYPES, Project, utc_now_iso
from .presets import (
    AUDIO_BITRATE_RANGE,
    CONTAINERS,
    CRF_RANGES,
    ENCODER_PRESETS,
    FPS_OPTIONS,
    INVALID_FILENAME_CHARACTERS,
    KEYFRAME_INTERVAL_RANGE,
    MAX_DIMENSION,
    MIN_DIMENSION,
    PIXEL_FORMATS_BY_CODEC,
    QUALITY_KEYS,
    SAMPLE_RATES,
    VIDEO_BITRATE_RANGE,
    VIDEO_CODECS,
    aspect_ratio_label,
    audio_codec_in_container,
    codec_in_container,
    quality_preset,
)

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"

LEVEL_GLYPHS = {LEVEL_ERROR: "[x]", LEVEL_WARNING: "[!]", LEVEL_INFO: "[i]"}


@dataclass(frozen=True)
class ValidationIssue:
    """One problem, with enough detail to act on it."""

    field: str
    level: str
    code: str
    message: str
    fix: str = ""

    def to_line(self) -> str:
        glyph = LEVEL_GLYPHS.get(self.level, "[?]")
        text = f"{glyph} {self.field}: {self.message}"
        if self.fix:
            text += f"  -> {self.fix}"
        return text


@dataclass
class ValidationReport:
    """All problems found in one pass."""

    issues: list[ValidationIssue] = field(default_factory=list)
    project_name: str = ""
    checked_at: str = ""

    # -- views -------------------------------------------------------------

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == LEVEL_WARNING]

    @property
    def infos(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == LEVEL_INFO]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def count(self) -> int:
        return len(self.errors) + len(self.warnings)

    def summary(self) -> str:
        """One line for the status bar / CLI."""
        errors, warnings = len(self.errors), len(self.warnings)
        if not errors and not warnings:
            return "No issues found."
        parts = []
        if errors:
            parts.append(f"{errors} error(s)")
        if warnings:
            parts.append(f"{warnings} warning(s)")
        return f"{errors + warnings} issue(s) found: " + ", ".join(parts) + "."

    def headline(self) -> str:
        if self.ok and not self.warnings:
            return "The project is valid."
        if self.ok:
            return f"The project is valid, with {len(self.warnings)} warning(s)."
        return f"{len(self.errors)} problem(s) must be fixed."

    def to_text(self) -> str:
        lines = [f"Validation: {self.project_name or 'project'}", self.summary()]
        for issue in self.issues:
            lines.append("  " + issue.to_line())
        return "\n".join(lines)

    def issues_for(self, field_prefix: str) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.field.startswith(field_prefix)]

    def as_dicts(self) -> list[dict]:
        return [
            {"field": i.field, "level": i.level, "code": i.code, "message": i.message, "fix": i.fix}
            for i in self.issues
        ]

    # -- building ----------------------------------------------------------

    def add(self, field_name: str, level: str, code: str, message: str, fix: str = "") -> ValidationIssue:
        issue = ValidationIssue(field_name, level, code, message, fix)
        self.issues.append(issue)
        return issue

    def error(self, field_name: str, code: str, message: str, fix: str = "") -> ValidationIssue:
        return self.add(field_name, LEVEL_ERROR, code, message, fix)

    def warning(self, field_name: str, code: str, message: str, fix: str = "") -> ValidationIssue:
        return self.add(field_name, LEVEL_WARNING, code, message, fix)

    def info(self, field_name: str, code: str, message: str, fix: str = "") -> ValidationIssue:
        return self.add(field_name, LEVEL_INFO, code, message, fix)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _in_range(report: ValidationReport, field_name: str, value, low, high, code: str, label: str, unit: str = "") -> None:
    number = _number(value, low - 1)
    suffix = f" {unit}".strip()
    if number < low or number > high:
        report.error(
            field_name,
            code,
            f"{label} is {value}{(' ' + unit) if unit else ''}, which is outside {low}-{high}{suffix}.",
            f"Choose a value between {low} and {high}.",
        )


def _path_escapes(relative: str) -> bool:
    """True when a project-relative path climbs out of the project folder."""
    text = (relative or "").replace("\\", "/").strip()
    if not text:
        return False
    depth = 0
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def _is_absolute_windows_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text or "")) or (text or "").startswith("\\\\")


# --------------------------------------------------------------------------
# Section validators
# --------------------------------------------------------------------------

def _validate_meta(project: Project, report: ValidationReport) -> None:
    meta = project.project

    name = (meta.name or "").strip()
    if not name:
        report.error("project.name", "NAME_EMPTY", "The project has no name.", "Give the project a name.")
    elif len(name) > 120:
        report.warning("project.name", "NAME_LONG", f"The name is {len(name)} characters long.", "Use 120 characters or fewer.")
    if any(character in name for character in INVALID_FILENAME_CHARACTERS):
        report.warning(
            "project.name",
            "NAME_CHARACTERS",
            "The name contains characters Windows does not allow in a folder name.",
            "They will be replaced when the project folder is named.",
        )

    if not meta.id:
        report.error("project.id", "ID_MISSING", "The project has no id.", "Create the project through the app so an id is assigned.")
    elif not _ID_RE.match(meta.id):
        report.error("project.id", "ID_INVALID", f"The id '{meta.id}' is not valid.", "Use letters, digits, '-' and '_' only.")

    from .model import parse_iso

    for field_name, value in (("project.created_at", meta.created_at), ("project.modified_at", meta.modified_at)):
        if value and parse_iso(value) is None:
            report.warning(field_name, "TIMESTAMP", f"'{value}' is not a recognised timestamp.", "Leave it to the app to set.")

    if _int(meta.project_version, 0) < 1:
        report.error("project.project_version", "VERSION", "The saved version number is not valid.", "Set it to 1 or higher.")

    if _int(project.schema_version, 0) != PROJECT_SCHEMA_VERSION:
        report.warning(
            "schema_version",
            "SCHEMA",
            f"The file says schema {project.schema_version}; this build writes {PROJECT_SCHEMA_VERSION}.",
            "Saving will update it.",
        )


def _validate_format(project: Project, report: ValidationReport) -> None:
    fmt = project.format

    width, height = _int(fmt.width), _int(fmt.height)
    _in_range(report, "format.width", width, MIN_DIMENSION, MAX_DIMENSION, "RESOLUTION", "Width", "px")
    _in_range(report, "format.height", height, MIN_DIMENSION, MAX_DIMENSION, "RESOLUTION", "Height", "px")
    if width > 0 and width % 2:
        report.error("format.width", "ODD_WIDTH", f"Width {width} is odd.", "Use an even number - the H.264 encoder requires it.")
    if height > 0 and height % 2:
        report.error("format.height", "ODD_HEIGHT", f"Height {height} is odd.", "Use an even number - the H.264 encoder requires it.")

    fps = _int(fmt.fps)
    if fps not in FPS_OPTIONS:
        report.error(
            "format.fps",
            "FPS",
            f"{fps} fps is not one of the supported rates.",
            f"Choose one of {', '.join(str(value) for value in FPS_OPTIONS)}.",
        )

    if fmt.quality_preset not in QUALITY_KEYS:
        report.error("format.quality_preset", "QUALITY", f"Unknown quality '{fmt.quality_preset}'.", f"Choose one of {', '.join(QUALITY_KEYS)}.")

    if fmt.container not in CONTAINERS:
        report.error("format.container", "CONTAINER", f"Unknown container '{fmt.container}'.", f"Choose one of {', '.join(CONTAINERS)}.")

    if fmt.codec not in VIDEO_CODECS:
        report.error("format.codec", "CODEC", f"Unknown video codec '{fmt.codec}'.", f"Choose one of {', '.join(VIDEO_CODECS)}.")
    elif not codec_in_container(fmt.codec, fmt.container):
        report.error(
            "format.codec",
            "CODEC_CONTAINER",
            f"{fmt.codec} cannot be stored in a .{fmt.container} file.",
            f"For .{fmt.container} use one of: {', '.join(_codecs_for(fmt.container))}.",
        )

    if fmt.encoder_preset not in ENCODER_PRESETS:
        report.error(
            "format.encoder_preset",
            "ENCODER_PRESET",
            f"Unknown encoder preset '{fmt.encoder_preset}'.",
            f"Choose one of {', '.join(ENCODER_PRESETS)}.",
        )

    low, high = CRF_RANGES.get(fmt.codec, (0, 51))
    crf = _int(fmt.crf, -1)
    if crf < low or crf > high:
        report.error("format.crf", "CRF", f"CRF {fmt.crf} is outside {low}-{high} for {fmt.codec}.", f"Use {low}-{high}.")

    bitrate = _int(fmt.bitrate_kbps, 0)
    if bitrate:
        _in_range(report, "format.bitrate_kbps", bitrate, *VIDEO_BITRATE_RANGE, code="BITRATE", label="Video bitrate", unit="kbps")
    elif crf == 0 and not bitrate:
        report.warning(
            "format.crf",
            "NO_QUALITY_MODE",
            "Neither CRF nor a bitrate is set, so quality is undefined.",
            "Set CRF (recommended) or an explicit bitrate.",
        )

    allowed_pixels = PIXEL_FORMATS_BY_CODEC.get(fmt.codec, ())
    if fmt.pixel_format not in allowed_pixels:
        report.error(
            "format.pixel_format",
            "PIXEL_FORMAT",
            f"{fmt.pixel_format} is not supported by {fmt.codec}.",
            f"Choose one of {', '.join(allowed_pixels)}.",
        )

    if not audio_codec_in_container(fmt.audio_codec, fmt.container):
        report.error(
            "format.audio_codec",
            "AUDIO_CODEC",
            f"{fmt.audio_codec} audio cannot be stored in a .{fmt.container} file.",
            f"For .{fmt.container} use one of: {', '.join(_audio_codecs_for(fmt.container))}.",
        )

    _in_range(report, "format.audio_bitrate_kbps", _int(fmt.audio_bitrate_kbps), *AUDIO_BITRATE_RANGE, code="AUDIO_BITRATE", label="Audio bitrate", unit="kbps")

    if _int(fmt.sample_rate) not in SAMPLE_RATES:
        report.error(
            "format.sample_rate",
            "SAMPLE_RATE",
            f"{fmt.sample_rate} Hz is not a standard sample rate.",
            f"Choose one of {', '.join(str(value) for value in SAMPLE_RATES)}.",
        )

    _in_range(report, "format.keyframe_interval", _int(fmt.keyframe_interval), *KEYFRAME_INTERVAL_RANGE, code="KEYFRAME", label="Keyframe interval", unit="s")

    expected_aspect = aspect_ratio_label(width, height) if width > 0 and height > 0 else fmt.aspect_ratio
    if fmt.aspect_ratio and expected_aspect and fmt.aspect_ratio != expected_aspect:
        report.warning(
            "format.aspect_ratio",
            "ASPECT",
            f"{width}x{height} is {expected_aspect}, but the project says {fmt.aspect_ratio}.",
            "The real resolution wins when rendering.",
        )

    _validate_encode_cost(fmt, report)


def _codecs_for(container: str) -> tuple[str, ...]:
    from .presets import CODECS_BY_CONTAINER

    return CODECS_BY_CONTAINER.get(container, ())


def _audio_codecs_for(container: str) -> tuple[str, ...]:
    from .presets import AUDIO_CODECS_BY_CONTAINER

    return AUDIO_CODECS_BY_CONTAINER.get(container, ())


def _validate_encode_cost(fmt, report: ValidationReport) -> None:
    """Warn about combinations that are valid but painfully slow on a CPU.

    These are **warnings**, never errors: the target machine has no GPU, and the
    user is allowed to choose a slow, high-quality encode (directive 27/36-9).
    """
    pixels = _int(fmt.width) * _int(fmt.height)
    fps = _int(fmt.fps)
    preset = fmt.encoder_preset
    slow_presets = {"slow", "slower", "veryslow"}

    if pixels >= 3840 * 2160:
        if preset in slow_presets:
            report.warning(
                "format.quality_preset",
                "SLOW_4K",
                f"4K at {fps} fps with the '{preset}' encoder preset is very slow on CPU only.",
                "Use 'medium' or 'fast' for a first render, or lower the resolution.",
            )
        if fps >= 60:
            report.warning(
                "format.fps",
                "HEAVY_4K60",
                f"4K at {fps} fps is the heaviest combination this app supports.",
                "Expect a long render; a 1080p draft is much faster to review.",
            )
    elif pixels >= 2560 * 1440 and preset == "veryslow":
        report.warning(
            "format.encoder_preset",
            "SLOW_ENCODE",
            f"'veryslow' at {fmt.width}x{fmt.height} is slow on CPU only.",
            "Choose 'slow' or faster unless you need the last percent of quality.",
        )

    if fmt.codec == "vp9_cpu" and pixels >= 1920 * 1080:
        report.warning(
            "format.codec",
            "VP9_SLOW",
            "VP9 encoding is slow without a GPU.",
            "H.264 is much faster and plays everywhere.",
        )

    if fmt.quality_preset == "ultra" and quality_preset("ultra") is not None and fps >= 50 and pixels >= 2560 * 1440:
        report.warning(
            "format.quality_preset",
            "ULTRA_HEAVY",
            "Ultra quality at this resolution and frame rate can take a very long time.",
            "Render a draft first to check timing.",
        )


def _validate_voice(project: Project, report: ValidationReport) -> None:
    voice = project.voice
    if voice.engine != "kokoro":
        report.error(
            "voice.engine",
            "ENGINE",
            f"'{voice.engine}' is not a supported voice engine.",
            "This version supports Kokoro only.",
        )
    _in_range(report, "voice.speed", _number(voice.speed, 1.0), 0.5, 2.0, code="SPEED", label="Speech speed", unit="x")
    _in_range(report, "voice.volume", _number(voice.volume, 1.0), 0.0, 2.0, code="VOLUME", label="Narration volume")
    if voice.language and not re.match(r"^[a-z]{2}(?:-[a-z]{2})?$", voice.language.lower()):
        report.warning("voice.language", "LANGUAGE", f"'{voice.language}' does not look like a language code.", "Use a code such as en-us.")
    if _int(voice.sample_rate) and _int(voice.sample_rate) not in SAMPLE_RATES:
        report.warning(
            "voice.sample_rate",
            "SAMPLE_RATE",
            f"{voice.sample_rate} Hz is unusual for narration.",
            f"Choose one of {', '.join(str(value) for value in SAMPLE_RATES)}.",
        )
    if not voice.voice:
        report.info(
            "voice.voice",
            "VOICE_AUTO",
            "No voice is selected; the first available Kokoro voice will be used.",
            "Pick a voice on the Voice page once the engine is installed.",
        )


def _validate_script(project: Project, report: ValidationReport) -> None:
    script = project.script
    if _number(script.estimated_duration_seconds, 0.0) < 0:
        report.error("script.estimated_duration_seconds", "DURATION", "The estimated duration is negative.", "Set it to 0 or more.")
    wpm = _int(script.words_per_minute, 150)
    if not 60 <= wpm <= 300:
        report.warning("script.words_per_minute", "WPM", f"{wpm} words per minute is unusual.", "Use 60-300 (150 is normal speech).")

    seen: set[str] = set()
    for section in script.sections:
        if not section.id:
            report.warning("script.sections", "SECTION_ID", "A script section has no id.", "The app will assign one on the next save.")
        elif section.id in seen:
            report.error("script.sections", "SECTION_ID_DUPLICATE", f"Two script sections use the id '{section.id}'.", "Give them different ids.")
        else:
            seen.add(section.id)


def _validate_scenes(project: Project, report: ValidationReport) -> None:
    if not project.scenes:
        report.info("scenes", "NO_SCENES", "The project has no scenes yet.", "Add scenes when you are ready to build the video.")
        return

    seen: set[str] = set()
    known_assets = {asset.id for asset in project.assets}
    for index, scene in enumerate(project.scenes):
        label = f"scenes[{index}]"
        if not scene.id:
            report.warning(label, "SCENE_ID", "A scene has no id.", "The app will assign one on the next save.")
        elif scene.id in seen:
            report.error(label, "SCENE_ID_DUPLICATE", f"Two scenes use the id '{scene.id}'.", "Give them different ids.")
        else:
            seen.add(scene.id)

        if scene.type not in SCENE_TYPES:
            report.error(f"{label}.type", "SCENE_TYPE", f"Unknown scene type '{scene.type}'.", f"Choose one of {', '.join(SCENE_TYPES)}.")

        if _number(scene.duration, 0.0) < 0:
            report.error(f"{label}.duration", "SCENE_DURATION", f"Scene {index + 1} has a negative duration.", "Use 0 or more seconds.")

        for transition_name, transition in (("transition_in", scene.transition_in), ("transition_out", scene.transition_out)):
            if transition.type not in TRANSITION_TYPES:
                report.error(f"{label}.{transition_name}", "TRANSITION", f"Unknown transition '{transition.type}'.", f"Choose one of {', '.join(TRANSITION_TYPES)}.")
            if _number(transition.duration, 0.0) < 0 or _number(transition.duration, 0.0) > 5:
                report.error(f"{label}.{transition_name}.duration", "TRANSITION_DURATION", f"Transition length {transition.duration}s is outside 0-5s.", "Use 0-5 seconds.")

        narration = scene.narration
        if _number(narration.duration, 0.0) < 0:
            report.error(f"{label}.narration.duration", "NARRATION_DURATION", "Narration duration is negative.", "Use 0 or more seconds.")
        if narration.file:
            _validate_relative_path(report, f"{label}.narration.file", narration.file)

        element_ids: set[str] = set()
        for element in scene.elements:
            element_label = f"{label}.elements"
            if element.id and element.id in element_ids:
                report.error(element_label, "ELEMENT_ID_DUPLICATE", f"Two elements in scene {index + 1} use '{element.id}'.", "Give them different ids.")
            element_ids.add(element.id)
            if element.anchor and element.anchor not in ANCHORS:
                report.warning(f"{element_label}.anchor", "ANCHOR", f"Unknown anchor '{element.anchor}'.", f"Choose one of {', '.join(ANCHORS)}.")
            position = element.position if isinstance(element.position, dict) else {}
            for axis in ("x", "y"):
                value = _number(position.get(axis), 0.5)
                if value < 0 or value > 1:
                    report.error(
                        f"{element_label}.position.{axis}",
                        "POSITION",
                        f"Position {axis}={value} is outside the frame.",
                        "Positions are fractions of the frame (0..1).",
                    )
            if element.asset_id and element.asset_id not in known_assets:
                report.error(
                    f"{element_label}.asset_id",
                    "ASSET_MISSING_REF",
                    f"Element refers to asset '{element.asset_id}', which is not in the project.",
                    "Add the asset or point the element at another one.",
                )


def _validate_relative_path(report: ValidationReport, field_name: str, value: str) -> None:
    """A project-relative field must stay inside the project and stay relative.

    An absolute path is a *warning* (it works here, but not on another PC);
    ``..`` climbing out of the project is an *error* (it breaks the folder).
    """
    text = (value or "").strip()
    if _is_absolute_windows_path(text) or text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        report.warning(
            field_name,
            "PATH_ABSOLUTE",
            f"'{value}' is an absolute path, so the project will not open on another PC.",
            "Copy the file into the project's assets folder instead.",
        )
    elif _path_escapes(text):
        report.error(
            field_name,
            "PATH_ESCAPES",
            f"'{value}' points outside the project folder.",
            "Use a path inside the project (assets/...) so the project stays portable.",
        )


def _validate_assets(project: Project, report: ValidationReport) -> None:
    seen: set[str] = set()
    used = project.referenced_asset_ids()
    for index, asset in enumerate(project.assets):
        label = f"assets[{index}]"
        if not asset.id:
            report.warning(label, "ASSET_ID", "An asset has no id.", "The app will assign one on the next save.")
        elif asset.id in seen:
            report.error(label, "ASSET_ID_DUPLICATE", f"Two assets use the id '{asset.id}'.", "Give them different ids.")
        else:
            seen.add(asset.id)

        if asset.kind not in ASSET_KINDS:
            report.error(f"{label}.kind", "ASSET_KIND", f"Unknown asset kind '{asset.kind}'.", f"Choose one of {', '.join(ASSET_KINDS)}.")

        if not asset.path and not asset.absolute_path:
            report.error(f"{label}.path", "ASSET_PATH", f"Asset '{asset.label()}' has no path.", "Relink it to a real file.")
        if asset.path:
            _validate_relative_path(report, f"{label}.path", asset.path)
        if asset.absolute_path:
            report.warning(
                f"{label}.absolute_path",
                "ASSET_ABSOLUTE",
                f"Asset '{asset.label()}' points at a fixed location on this PC.",
                "Copy it into the project's assets folder to make the project portable.",
            )
        if asset.missing:
            report.warning(
                f"{label}",
                "ASSET_MISSING",
                f"Asset '{asset.label()}' could not be found at the last check.",
                "Relink or replace it, or ignore it if the project no longer needs it.",
            )
        if asset.id and asset.id not in used:
            report.info(f"{label}", "ASSET_UNUSED", f"Asset '{asset.label()}' is not used by any scene.", "You can delete it to keep the project small.")


def _validate_audio(project: Project, report: ValidationReport) -> None:
    audio = project.audio
    _in_range(report, "audio.narration_volume", _number(audio.narration_volume, 1.0), 0.0, 2.0, code="NARRATION_VOLUME", label="Narration volume")
    _in_range(report, "audio.music.volume", _number(audio.music.volume, 0.18), 0.0, 1.0, code="MUSIC_VOLUME", label="Music volume")
    _in_range(report, "audio.ducking_level", _number(audio.ducking_level, 0.35), 0.0, 1.0, code="DUCKING", label="Ducking level")
    _in_range(report, "audio.target_lufs", _number(audio.target_lufs, -16.0), -40.0, -5.0, code="LUFS", label="Normalisation target", unit="LUFS")
    if audio.music.path:
        _validate_relative_path(report, "audio.music.path", audio.music.path)
    if not audio.narration_enabled and project.script.source_text.strip():
        report.info("audio.narration_enabled", "NARRATION_OFF", "Narration is switched off, but the project has a script.", "Switch narration on if you want the script spoken.")

    seen: set[str] = set()
    for index, effect in enumerate(audio.sfx):
        label = f"audio.sfx[{index}]"
        if effect.id and effect.id in seen:
            report.error(label, "SFX_ID_DUPLICATE", f"Two sound effects use the id '{effect.id}'.", "Give them different ids.")
        seen.add(effect.id)
        if effect.path:
            _validate_relative_path(report, f"{label}.path", effect.path)
        if _number(effect.volume, 0.6) < 0 or _number(effect.volume, 0.6) > 2:
            report.error(f"{label}.volume", "SFX_VOLUME", "Sound effect volume is outside 0-2.", "Use 0-2.")


def _validate_theme(project: Project, report: ValidationReport) -> None:
    theme = project.theme
    for field_name, value in (("theme.background", theme.background), ("theme.accent", theme.accent)):
        if value and not _HEX_RE.match(value):
            report.warning(field_name, "COLOR", f"'{value}' is not a hex colour.", "Use a value like #4c8dff.")
    for name, value in (theme.colors or {}).items():
        if isinstance(value, str) and value and not _HEX_RE.match(value):
            report.warning(f"theme.colors.{name}", "COLOR", f"'{value}' is not a hex colour.", "Use a value like #4c8dff.")

    typography = theme.typography
    _in_range(report, "theme.typography.line_height", _number(typography.line_height, 1.2), 0.8, 3.0, code="LINE_HEIGHT", label="Line height")

    subtitles = theme.subtitle_style
    _in_range(report, "theme.subtitle_style.font_size", _int(subtitles.font_size, 44), 8, 200, code="SUBTITLE_SIZE", label="Subtitle size", unit="px")
    _in_range(report, "theme.subtitle_style.max_lines", _int(subtitles.max_lines, 2), 1, 6, code="SUBTITLE_LINES", label="Subtitle lines")
    _in_range(report, "theme.subtitle_style.safe_area_percent", _number(subtitles.safe_area_percent, 8.0), 0.0, 25.0, code="SAFE_AREA", label="Safe area", unit="%")


def _validate_export(project: Project, report: ValidationReport) -> None:
    export = project.export
    template = (export.filename_template or "").strip()
    if not template:
        report.error("export.filename_template", "TEMPLATE_EMPTY", "No output filename template is set.", "Use something like {name}_{seq}.")
    else:
        bad = [character for character in INVALID_FILENAME_CHARACTERS if character in template]
        if bad:
            report.error(
                "export.filename_template",
                "TEMPLATE_CHARACTERS",
                f"The template contains characters Windows forbids: {' '.join(bad)}",
                "Remove them; {name}, {channel}, {date}, {seq} are filled in automatically.",
            )
        if "{seq}" not in template:
            report.warning(
                "export.filename_template",
                "TEMPLATE_NO_SEQ",
                "The template has no {seq}, so repeated renders want the same filename.",
                "Add {seq} to keep every render (files are never overwritten).",
            )
        from .presets import FILENAME_TEMPLATE_FIELDS

        unknown = set(re.findall(r"{(\w+)}", template)) - set(FILENAME_TEMPLATE_FIELDS)
        if unknown:
            report.warning(
                "export.filename_template",
                "TEMPLATE_FIELDS",
                f"Unknown placeholder(s): {', '.join(sorted(unknown))}.",
                f"Available: {', '.join(FILENAME_TEMPLATE_FIELDS)}.",
            )

    if _int(export.next_sequence_number, 1) < 1:
        report.error("export.next_sequence_number", "SEQUENCE", "The sequence number must start at 1.", "Set it to 1 or higher.")

    if export.overwrite_policy not in ("never", "ask", "overwrite"):
        report.error("export.overwrite_policy", "OVERWRITE_POLICY", f"Unknown overwrite policy '{export.overwrite_policy}'.", "Use 'never' (recommended), 'ask' or 'overwrite'.")
    elif export.overwrite_policy == "overwrite":
        report.warning(
            "export.overwrite_policy",
            "OVERWRITE_DANGEROUS",
            "Existing output files would be replaced without asking.",
            "Use 'never' unless you are sure.",
        )

    directory = (export.output_dir or "").strip()
    if not directory:
        report.error("export.output_dir", "OUTPUT_DIR_EMPTY", "No output folder is set.", "Use 'renders' (inside the project) or an absolute folder.")
    if directory and not export.output_dir_absolute and (
        _path_escapes(directory) or _is_absolute_windows_path(directory)
    ):
        report.error(
            "export.output_dir",
            "OUTPUT_DIR_ESCAPES",
            f"'{directory}' points outside the project folder.",
            "Use a folder inside the project, or tick 'absolute path'.",
        )
    if directory and not export.output_dir_absolute:
        first = directory.replace("\\", "/").strip("/").split("/")[0].lower()
        if first in ("assets", "audio", "generated"):
            report.warning(
                "export.output_dir",
                "OUTPUT_DIR_COLLISION",
                f"Finished videos would be written into the project's '{first}' folder.",
                "Use 'renders' so exports stay separate from source media.",
            )

    if not codec_in_container(export.codec, export.container):
        report.error(
            "export.codec",
            "EXPORT_CODEC",
            f"{export.codec} cannot be stored in a .{export.container} file.",
            "Match the export settings to the project format.",
        )
    if not audio_codec_in_container(export.audio_codec, export.container):
        report.error(
            "export.audio_codec",
            "EXPORT_AUDIO_CODEC",
            f"{export.audio_codec} audio cannot be stored in a .{export.container} file.",
            "Match the export settings to the project format.",
        )
    if export.codec != project.format.codec or export.container != project.format.container:
        report.info(
            "export",
            "EXPORT_DIFFERS",
            "The export settings differ from the project format.",
            "That is fine when intentional; the export settings win when rendering.",
        )


# --------------------------------------------------------------------------
# File checks (only when opening a project)
# --------------------------------------------------------------------------

def _validate_files(project: Project, project_dir: Optional[Path], report: ValidationReport) -> None:
    if project_dir is None:
        return
    project_dir = Path(project_dir)

    for index, asset in enumerate(project.assets):
        target = asset.resolve(project_dir)
        if not target.exists():
            report.warning(
                f"assets[{index}]",
                "FILE_MISSING",
                f"Asset '{asset.label()}' is not at {target}.",
                "Relink it to the file's new location, or replace it.",
            )

    for index, scene in enumerate(project.scenes):
        narration_file = scene.narration.file
        if narration_file and not (project_dir / narration_file).exists():
            report.info(
                f"scenes[{index}].narration.file",
                "NARRATION_NOT_GENERATED",
                f"Narration '{narration_file}' has not been generated yet.",
                "Generate narration before rendering.",
            )

    if project.audio.music.path and not (project_dir / project.audio.music.path).exists():
        report.warning(
            "audio.music.path",
            "MUSIC_MISSING",
            f"Music file '{project.audio.music.path}' is missing.",
            "Relink it or remove the music track.",
        )

    script_file = project_dir / "script.txt"
    if script_file.exists() and project.script.source_text:
        try:
            on_disk = script_file.read_text(encoding="utf-8")
        except OSError:
            on_disk = ""
        if on_disk.strip() and on_disk != project.script.source_text:
            report.info(
                "script.txt",
                "SCRIPT_DIFFERS",
                "script.txt differs from the script stored in project.json.",
                "project.json is authoritative; saving rewrites script.txt.",
            )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def validate_project(
    project: Project,
    *,
    project_dir: Optional[Path] = None,
    check_files: bool = False,
) -> ValidationReport:
    """Validate everything, collecting every issue instead of stopping early."""
    report = ValidationReport(project_name=project.project.name, checked_at=utc_now_iso())
    _validate_meta(project, report)
    _validate_format(project, report)
    _validate_voice(project, report)
    _validate_script(project, report)
    _validate_scenes(project, report)
    _validate_assets(project, report)
    _validate_audio(project, report)
    _validate_theme(project, report)
    _validate_export(project, report)
    if check_files:
        _validate_files(project, project_dir, report)
    return report


def validate_for_render(project: Project, project_dir: Optional[Path] = None) -> ValidationReport:
    """Stricter pass used before rendering (Stage F).

    Missing assets stop a render instead of producing a video with holes in it,
    and a project with no scenes is an error rather than an informational note.
    """
    report = validate_project(project, project_dir=project_dir, check_files=True)
    for issue in list(report.issues):
        if issue.code in ("FILE_MISSING", "MUSIC_MISSING", "ASSET_MISSING") and issue.level == LEVEL_WARNING:
            report.issues.remove(issue)
            report.add(issue.field, LEVEL_ERROR, issue.code, issue.message, issue.fix)
    if not project.scenes:
        for issue in list(report.issues):
            if issue.code == "NO_SCENES":
                report.issues.remove(issue)
        report.error("scenes", "NO_SCENES", "The project has no scenes, so there is nothing to render.", "Add at least one scene.")
    return report


__all__ = [
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "ValidationIssue",
    "ValidationReport",
    "validate_for_render",
    "validate_project",
]
