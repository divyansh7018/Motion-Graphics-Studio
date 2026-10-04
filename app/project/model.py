"""The typed project model - the single source of truth for a project.

Directive sections 2, 3, 6 and 34 drive the design:

* **Typed, not a dict soup.**  Every part of a project is a dataclass with
  real field names, so a typo fails loudly instead of silently reading
  ``None``.  Nothing in the application reads ``project["format"]["w"]``.
* **One file, one truth.**  ``project.json`` holds the whole model; the
  renderer, the CLI, the GUI and the tests all read this same object.
* **Forward compatible.**  Keys this build does not know are kept in each
  section's ``extra`` dict and written back untouched, so opening and saving a
  project with a newer application never destroys data it does not understand.
* **Deterministic.**  Timestamps are UTC ISO-8601, ids are stable strings, and
  any randomness carries an explicit seed that is stored.

No Qt, no I/O and no logging in this module: it is pure data plus the
transformations the service layer needs.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..core.version import APP_VERSION, PROJECT_SCHEMA_VERSION
from .presets import (
    aspect_ratio_label,
    default_audio_codec,
    default_codec,
    default_pixel_format,
)

# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

PROJECT_FILENAME = "project.json"
SCRIPT_FILENAME = "script.txt"

#: Scene types the storyboard understands (Stage D renders them).
SCENE_TYPES: tuple[str, ...] = ("blank", "title", "text", "image", "number", "cta", "video", "graphic")

ASSET_KINDS: tuple[str, ...] = ("image", "video", "audio", "font", "logo", "svg", "other")

TRANSITION_TYPES: tuple[str, ...] = ("none", "fade", "cut", "slide", "zoom")

ANCHORS: tuple[str, ...] = ("top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right")


def utc_now_iso() -> str:
    """Current UTC time as ``2026-10-05T12:34:56Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; ``None`` when it cannot be understood."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    """A short, stable, unique-enough id (``scene-4f2a9c1b``)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _merge(target: Any, data: Any) -> None:
    """Copy known keys onto a dataclass and keep unknown keys in ``extra``.

    This is what makes the file format forward compatible: a project written by
    a newer application keeps the fields this build does not know about.
    """
    if not isinstance(data, dict):
        return
    known = {f.name: f for f in fields(target)}
    extra = getattr(target, "extra", None)
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            if isinstance(extra, dict) and key != "extra":
                extra[key] = value
            continue
        current = getattr(target, key)
        if isinstance(current, _Section) and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(target, key, value)


def _section_dict(section: Any) -> dict:
    """Serialise a section, writing ``extra`` keys back at the top level."""
    data = asdict(section)
    extra = data.pop("extra", {}) or {}
    for key, value in extra.items():
        data.setdefault(key, value)
    return data


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


class _Section:
    """Base class that gives every section ``to_dict`` / ``from_dict``.

    Each section declares its own ``extra`` dict; that is what makes unknown
    keys survive a load/save round trip.
    """

    def to_dict(self) -> dict:
        return _section_dict(self)

    @classmethod
    def from_dict(cls, data: Any):
        instance = cls()
        _merge(instance, data)
        return instance


# --------------------------------------------------------------------------
# Project metadata
# --------------------------------------------------------------------------

@dataclass
class ProjectMeta(_Section):
    """Identity and bookkeeping for one project."""

    id: str = ""
    name: str = "Untitled Project"
    description: str = ""
    channel_id: str = ""
    channel_name: str = ""
    created_at: str = ""
    modified_at: str = ""
    #: Increments on every successful save.  Together with the file hash this is
    #: how the application detects a project changed by another instance.
    project_version: int = 1
    application_version: str = APP_VERSION
    template: str = "blank"
    favorite: bool = False
    archived: bool = False
    random_seed: int = 0
    extra: dict = field(default_factory=dict)

    def touch(self, when: Optional[str] = None) -> None:
        self.modified_at = when or utc_now_iso()

    def summary_line(self) -> str:
        parts = [self.name]
        if self.channel_name:
            parts.append(self.channel_name)
        parts.append(f"v{self.project_version}")
        return " - ".join(parts)


# --------------------------------------------------------------------------
# Format / quality
# --------------------------------------------------------------------------

@dataclass
class FormatSpec(_Section):
    """Video format plus the resolved quality settings (directive section 26).

    The project stores **numbers**, never just a preset name, so a project can
    be reproduced exactly even if the preset tables change later.
    """

    width: int = 1920
    height: int = 1080
    aspect_ratio: str = "16:9"
    fps: int = 30
    quality_preset: str = "high"
    codec: str = "h264_cpu"
    #: Constant-quality mode.  Used when ``bitrate_kbps`` is 0.
    crf: int = 20
    #: Explicit bitrate in kbps; 0 means "use CRF".
    bitrate_kbps: int = 0
    encoder_preset: str = "slow"
    pixel_format: str = "yuv420p"
    keyframe_interval: int = 2
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 192
    sample_rate: int = 48000
    container: str = "mp4"
    background: str = "#101014"
    extra: dict = field(default_factory=dict)

    @classmethod
    def create(cls, width: int, height: int, fps: int, quality: dict, background: str = "#101014") -> "FormatSpec":
        spec = cls(
            width=int(width),
            height=int(height),
            fps=int(fps),
            background=background,
        )
        spec.apply_quality(quality)
        spec.aspect_ratio = aspect_ratio_label(spec.width, spec.height)
        return spec

    def apply_quality(self, quality: dict) -> None:
        """Copy resolved quality values (from :func:`presets.resolve_quality`)."""
        for key, value in (quality or {}).items():
            if hasattr(self, key):
                setattr(self, key, value)
        if not self.audio_codec:
            self.audio_codec = default_audio_codec(self.container)
        if not self.codec:
            self.codec = default_codec(self.container)
        if not self.pixel_format:
            self.pixel_format = default_pixel_format(self.codec)

    @property
    def megapixels(self) -> float:
        return round(self.width * self.height / 1_000_000, 2)

    @property
    def frame_count_per_second(self) -> int:
        return max(1, int(self.fps))

    def label(self) -> str:
        return f"{self.width}x{self.height} @ {self.fps} fps ({self.aspect_ratio})"

    def quality_label(self) -> str:
        mode = f"CRF {self.crf}" if not self.bitrate_kbps else f"{self.bitrate_kbps} kbps"
        return f"{self.quality_preset} - {self.codec}, {self.encoder_preset}, {mode}"


# --------------------------------------------------------------------------
# Script
# --------------------------------------------------------------------------

@dataclass
class ScriptSection(_Section):
    """One titled block of the script (scenes are built from these later)."""

    id: str = ""
    title: str = ""
    text: str = ""
    order: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class ScriptSpec(_Section):
    """The written script.  The source text is stored **exactly as typed**.

    ``script.txt`` in the project folder holds the same text for users who want
    to read or edit it outside the application; ``source_text`` here is the
    authoritative copy and is never reformatted (directive section 24).
    """

    source_text: str = ""
    notes: str = ""
    sections: list[ScriptSection] = field(default_factory=list)
    #: Rough estimate only - real timing comes from the narration audio (Stage C).
    estimated_duration_seconds: float = 0.0
    words_per_minute: int = 150
    extra: dict = field(default_factory=dict)

    def word_count(self) -> int:
        return len(re.findall(r"\S+", self.source_text or ""))

    def recompute_estimate(self) -> float:
        """Estimate duration from the word count (never used for real timing)."""
        wpm = self.words_per_minute if self.words_per_minute > 0 else 150
        self.estimated_duration_seconds = round(self.word_count() / wpm * 60.0, 2)
        return self.estimated_duration_seconds

    def rebuild_sections(self) -> list[ScriptSection]:
        """Split the source text into sections on blank lines.

        Called only when the user asks for it (or when a template seeds the
        script), never silently on load - the text itself is never rewritten.
        """
        blocks = [block.strip() for block in re.split(r"\n\s*\n", self.source_text or "") if block.strip()]
        sections: list[ScriptSection] = []
        for index, block in enumerate(blocks):
            title = ""
            body = block
            if "\n" in block:
                first, rest = block.split("\n", 1)
                if len(first) <= 80 and not first.endswith((".", ",", ";")):
                    title, body = first.strip(), rest.strip()
            sections.append(
                ScriptSection(id=new_id("sec"), title=title, text=body, order=index)
            )
        self.sections = sections
        return sections

    def to_dict(self) -> dict:
        data = _section_dict(self)
        data["sections"] = [_section_dict(section) for section in self.sections]
        return data


def _script_from_dict(data: Any) -> ScriptSpec:
    spec = ScriptSpec()
    _merge(spec, data)
    spec.sections = [ScriptSection.from_dict(item) for item in _as_list(getattr(spec, "sections", []))]
    return spec


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------

@dataclass
class VoiceSpec(_Section):
    """Narration settings.  Kokoro is the only engine in V1 (directive 15)."""

    engine: str = "kokoro"
    language: str = "en-us"
    gender: str = ""
    #: Empty means "pick the first available voice when narration is generated".
    voice: str = ""
    speed: float = 1.0
    volume: float = 1.0
    sample_rate: int = 24000
    extra: dict = field(default_factory=dict)

    def label(self) -> str:
        voice = self.voice or "first available"
        return f"{voice} ({self.language}) @ {self.speed:.2f}x"


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

@dataclass
class TypographySpec(_Section):
    heading_font: str = "DejaVu Sans"
    body_font: str = "DejaVu Sans"
    heading_scale: float = 1.0
    body_scale: float = 1.0
    line_height: float = 1.2
    extra: dict = field(default_factory=dict)


@dataclass
class SubtitleStyle(_Section):
    enabled: bool = False
    font: str = "DejaVu Sans"
    font_size: int = 44
    color: str = "#ffffff"
    outline_color: str = "#000000"
    outline_width: float = 2.0
    max_lines: int = 2
    safe_area_percent: float = 8.0
    extra: dict = field(default_factory=dict)


@dataclass
class ThemeSpec(_Section):
    """Visual identity: colours, typography and subtitle styling."""

    id: str = "clean-dark"
    background: str = "#101014"
    accent: str = "#4c8dff"
    colors: dict = field(default_factory=dict)
    typography: TypographySpec = field(default_factory=TypographySpec)
    subtitle_style: SubtitleStyle = field(default_factory=SubtitleStyle)
    extra: dict = field(default_factory=dict)

    def color(self, name: str, default: str = "#ffffff") -> str:
        value = self.colors.get(name)
        return value if isinstance(value, str) and value else default

    def to_dict(self) -> dict:
        data = _section_dict(self)
        data["typography"] = _section_dict(self.typography)
        data["subtitle_style"] = _section_dict(self.subtitle_style)
        return data


def _theme_from_dict(data: Any) -> ThemeSpec:
    theme = ThemeSpec()
    _merge(theme, data)
    if isinstance(data, dict):
        theme.typography = TypographySpec.from_dict(data.get("typography"))
        theme.subtitle_style = SubtitleStyle.from_dict(data.get("subtitle_style"))
    return theme


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

@dataclass
class MusicTrack(_Section):
    #: Project-relative path (``assets/music.mp3``) preferred, never absolute.
    path: str = ""
    volume: float = 0.18
    loop: bool = True
    fade_in: float = 1.0
    fade_out: float = 2.0
    extra: dict = field(default_factory=dict)


@dataclass
class SoundEffect(_Section):
    id: str = ""
    path: str = ""
    volume: float = 0.6
    at_seconds: float = 0.0
    duration: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class AudioSpec(_Section):
    """Narration, music and effects mixing intent (mixed in Stage E)."""

    narration_enabled: bool = True
    narration_volume: float = 1.0
    music: MusicTrack = field(default_factory=MusicTrack)
    sfx: list[SoundEffect] = field(default_factory=list)
    ducking_enabled: bool = True
    #: Music level while narration is speaking (fraction of ``music.volume``).
    ducking_level: float = 0.35
    normalize_enabled: bool = True
    target_lufs: float = -16.0
    sample_rate: int = 48000
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = _section_dict(self)
        data["music"] = _section_dict(self.music)
        data["sfx"] = [_section_dict(effect) for effect in self.sfx]
        return data


def _audio_from_dict(data: Any) -> AudioSpec:
    audio = AudioSpec()
    _merge(audio, data)
    if isinstance(data, dict):
        audio.music = MusicTrack.from_dict(data.get("music"))
        audio.sfx = [SoundEffect.from_dict(item) for item in _as_list(data.get("sfx"))]
    return audio


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------

@dataclass
class TransitionSpec(_Section):
    type: str = "none"
    duration: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class NarrationSpec(_Section):
    """Narration for one scene.  ``duration`` is **measured**, never guessed."""

    file: str = ""
    duration: float = 0.0
    voice: str = ""
    speed: float = 1.0
    text: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ElementSpec(_Section):
    """A visual element inside a scene.

    Positions are **normalised 0..1**, never pixels, so the same project lays
    out correctly at any resolution (directive section 23).  Stage D renders
    these; Stage B only stores them faithfully.
    """

    id: str = ""
    kind: str = "text"
    text: str = ""
    asset_id: str = ""
    anchor: str = "center"
    position: dict = field(default_factory=lambda: {"x": 0.5, "y": 0.5})
    size: dict = field(default_factory=lambda: {"mode": "relative", "value": 0.12})
    fit: dict = field(default_factory=lambda: {"auto_fit": True, "max_lines": 3, "min_scale": 0.6})
    color: str = "#ffffff"
    animation: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class SceneSpec(_Section):
    """One scene.  Stage B stores scenes; Stage D draws them."""

    id: str = ""
    name: str = ""
    type: str = "blank"
    script: str = ""
    notes: str = ""
    #: Manual duration in seconds.  When narration exists, its measured
    #: duration is authoritative (directive section 27).
    duration: float = 3.0
    background: str = ""
    transition_in: TransitionSpec = field(default_factory=TransitionSpec)
    transition_out: TransitionSpec = field(default_factory=TransitionSpec)
    narration: NarrationSpec = field(default_factory=NarrationSpec)
    elements: list[ElementSpec] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def effective_duration(self) -> float:
        """Narration length when it exists, otherwise the manual duration."""
        measured = _as_float(self.narration.duration, 0.0)
        if measured > 0:
            return measured
        return max(0.0, _as_float(self.duration, 0.0))

    def to_dict(self) -> dict:
        data = _section_dict(self)
        data["transition_in"] = _section_dict(self.transition_in)
        data["transition_out"] = _section_dict(self.transition_out)
        data["narration"] = _section_dict(self.narration)
        data["elements"] = [_section_dict(element) for element in self.elements]
        return data


def _scene_from_dict(data: Any) -> SceneSpec:
    scene = SceneSpec()
    _merge(scene, data)
    if isinstance(data, dict):
        scene.transition_in = TransitionSpec.from_dict(data.get("transition_in"))
        scene.transition_out = TransitionSpec.from_dict(data.get("transition_out"))
        scene.narration = NarrationSpec.from_dict(data.get("narration"))
        scene.elements = [ElementSpec.from_dict(item) for item in _as_list(data.get("elements"))]
    return scene


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------

@dataclass
class AssetSpec(_Section):
    """A file the project uses.

    ``path`` is project-relative whenever the file lives inside the project
    folder (``assets/logo.png``); ``absolute_path`` is only filled in for files
    that must stay where they are (a font installed on this PC, a network
    share).  Absolute paths are flagged so portability problems are visible
    (directive sections 22 and 23).
    """

    id: str = ""
    name: str = ""
    kind: str = "image"
    path: str = ""
    absolute_path: str = ""
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    imported_at: str = ""
    checksum: str = ""
    notes: str = ""
    #: Set when the file could not be found at the last check.
    missing: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_portable(self) -> bool:
        return bool(self.path) and not self.absolute_path

    def resolve(self, project_dir: Path) -> Path:
        """Absolute path of this asset for the given project folder."""
        if self.absolute_path:
            return Path(self.absolute_path)
        return Path(project_dir) / (self.path or self.name or "")

    def label(self) -> str:
        return self.name or self.path or self.id or "asset"


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

@dataclass
class ExportSpec(_Section):
    """Where finished videos go and how they are named.

    Files are **never** silently overwritten: the sequence number keeps
    increasing and the output manager refuses to replace an existing file.
    """

    #: Relative to the project folder by default (``renders``).
    output_dir: str = "renders"
    output_dir_absolute: bool = False
    filename_template: str = "{name}_{seq}"
    next_sequence_number: int = 1
    container: str = "mp4"
    codec: str = "h264_cpu"
    crf: int = 20
    bitrate_kbps: int = 0
    encoder_preset: str = "slow"
    pixel_format: str = "yuv420p"
    keyframe_interval: int = 2
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 192
    sample_rate: int = 48000
    #: ``never`` (default), ``ask`` or ``overwrite``.  ``never`` is the safe one.
    overwrite_policy: str = "never"
    extra: dict = field(default_factory=dict)

    def sync_from_format(self, spec: FormatSpec) -> None:
        """Mirror the resolved format so the export settings stay truthful."""
        self.container = spec.container
        self.codec = spec.codec
        self.crf = spec.crf
        self.bitrate_kbps = spec.bitrate_kbps
        self.encoder_preset = spec.encoder_preset
        self.pixel_format = spec.pixel_format
        self.keyframe_interval = spec.keyframe_interval
        self.audio_codec = spec.audio_codec or default_audio_codec(spec.container)
        self.audio_bitrate_kbps = spec.audio_bitrate_kbps
        self.sample_rate = spec.sample_rate


# --------------------------------------------------------------------------
# The project itself
# --------------------------------------------------------------------------

@dataclass
class Project:
    """A complete project: metadata, format, script, voice, theme, audio,
    scenes, assets and export settings.

    ``Project.from_dict`` / ``Project.to_dict`` are the **only** serialisation
    entry points (directive section 6).
    """

    schema_version: int = PROJECT_SCHEMA_VERSION
    application_version: str = APP_VERSION
    project: ProjectMeta = field(default_factory=ProjectMeta)
    format: FormatSpec = field(default_factory=FormatSpec)
    script: ScriptSpec = field(default_factory=ScriptSpec)
    voice: VoiceSpec = field(default_factory=VoiceSpec)
    theme: ThemeSpec = field(default_factory=ThemeSpec)
    audio: AudioSpec = field(default_factory=AudioSpec)
    scenes: list[SceneSpec] = field(default_factory=list)
    assets: list[AssetSpec] = field(default_factory=list)
    export: ExportSpec = field(default_factory=ExportSpec)
    #: Unknown top-level keys from a newer file, written back unchanged.
    extra: dict = field(default_factory=dict)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        data: dict[str, Any] = {
            "schema_version": int(self.schema_version),
            "application_version": str(self.application_version),
            "project": self.project.to_dict(),
            "format": self.format.to_dict(),
            "script": self.script.to_dict(),
            "voice": self.voice.to_dict(),
            "theme": self.theme.to_dict(),
            "audio": self.audio.to_dict(),
            "scenes": [scene.to_dict() for scene in self.scenes],
            "assets": [asset.to_dict() for asset in self.assets],
            "export": self.export.to_dict(),
        }
        for key, value in (self.extra or {}).items():
            data.setdefault(key, value)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Project":
        """Build a project from parsed JSON.  Junk keys are preserved, not fatal."""
        project = cls()
        if not isinstance(data, dict):
            return project

        known = {"schema_version", "application_version", "project", "format", "script", "voice", "theme", "audio", "scenes", "assets", "export"}
        project.schema_version = _as_int(data.get("schema_version"), PROJECT_SCHEMA_VERSION)
        project.application_version = _as_str(data.get("application_version"), APP_VERSION)
        project.project = ProjectMeta.from_dict(data.get("project"))
        project.format = FormatSpec.from_dict(data.get("format"))
        project.script = _script_from_dict(data.get("script"))
        project.voice = VoiceSpec.from_dict(data.get("voice"))
        project.theme = _theme_from_dict(data.get("theme"))
        project.audio = _audio_from_dict(data.get("audio"))
        project.scenes = [_scene_from_dict(item) for item in _as_list(data.get("scenes"))]
        project.assets = [AssetSpec.from_dict(item) for item in _as_list(data.get("assets"))]
        project.export = ExportSpec.from_dict(data.get("export"))
        project.extra = {key: value for key, value in data.items() if key not in known}
        return project

    # -- copies ------------------------------------------------------------

    def copy(self) -> "Project":
        """A deep copy (used for undo snapshots and previews)."""
        return Project.from_dict(self.to_dict())

    def clone_as(self, name: str, new_project_id: str = "", channel_name: str = "") -> "Project":
        """An **independent** copy: new id, new timestamps, version reset.

        Used by Save As and Duplicate so two projects can never end up pointing
        at the same writable ``project.json`` (directive section 10).
        """
        clone = self.copy()
        clone.project.id = new_project_id or new_id("project")
        clone.project.name = name or f"{self.project.name} (copy)"
        clone.project.created_at = utc_now_iso()
        clone.project.modified_at = clone.project.created_at
        clone.project.project_version = 1
        clone.project.application_version = APP_VERSION
        if channel_name:
            clone.project.channel_name = channel_name
        clone.export.next_sequence_number = 1
        clone.touch()
        return clone

    # -- timestamps / versions --------------------------------------------

    def touch(self) -> None:
        now = utc_now_iso()
        self.project.modified_at = now
        if not self.project.created_at:
            self.project.created_at = now

    def bump_version(self) -> int:
        self.project.project_version = _as_int(self.project.project_version, 0) + 1
        return self.project.project_version

    # -- scenes ------------------------------------------------------------

    def scene_by_id(self, scene_id: str) -> Optional[SceneSpec]:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        return None

    def add_scene(self, scene: Optional[SceneSpec] = None, index: Optional[int] = None) -> SceneSpec:
        scene = scene or SceneSpec(id=new_id("scene"), name=f"Scene {len(self.scenes) + 1}")
        if not scene.id:
            scene.id = new_id("scene")
        if index is None:
            self.scenes.append(scene)
        else:
            self.scenes.insert(max(0, min(index, len(self.scenes))), scene)
        self.touch()
        return scene

    def remove_scene(self, scene_id: str) -> Optional[SceneSpec]:
        scene = self.scene_by_id(scene_id)
        if scene is not None:
            self.scenes.remove(scene)
            self.touch()
        return scene

    def move_scene(self, scene_id: str, new_index: int) -> bool:
        scene = self.scene_by_id(scene_id)
        if scene is None:
            return False
        self.scenes.remove(scene)
        self.scenes.insert(max(0, min(new_index, len(self.scenes))), scene)
        self.touch()
        return True

    def timeline(self) -> list[dict]:
        """Compute start times from the effective durations (sequential).

        Narration durations win over manual ones, which is what makes the final
        video match the real audio (directive section 27).
        """
        cursor = 0.0
        rows: list[dict] = []
        for index, scene in enumerate(self.scenes):
            duration = max(0.0, scene.effective_duration)
            rows.append(
                {
                    "index": index,
                    "id": scene.id,
                    "name": scene.name or scene.type,
                    "start": round(cursor, 3),
                    "duration": round(duration, 3),
                    "end": round(cursor + duration, 3),
                    "timed_by": "narration" if _as_float(scene.narration.duration, 0.0) > 0 else "manual",
                }
            )
            cursor += duration
        return rows

    def total_duration_seconds(self) -> float:
        rows = self.timeline()
        return round(rows[-1]["end"], 3) if rows else 0.0

    def estimated_duration_seconds(self) -> float:
        """Best available duration guess: timeline when there are scenes."""
        total = self.total_duration_seconds()
        if total > 0:
            return total
        return _as_float(self.script.estimated_duration_seconds, 0.0)

    # -- assets ------------------------------------------------------------

    def asset_by_id(self, asset_id: str) -> Optional[AssetSpec]:
        for asset in self.assets:
            if asset.id == asset_id:
                return asset
        return None

    def add_asset(self, asset: AssetSpec) -> AssetSpec:
        if not asset.id:
            asset.id = new_id("asset")
        if not asset.imported_at:
            asset.imported_at = utc_now_iso()
        self.assets.append(asset)
        self.touch()
        return asset

    def remove_asset(self, asset_id: str) -> Optional[AssetSpec]:
        asset = self.asset_by_id(asset_id)
        if asset is not None:
            self.assets.remove(asset)
            self.touch()
        return asset

    def assets_of_kind(self, *kinds: str) -> list[AssetSpec]:
        wanted = set(kinds)
        return [asset for asset in self.assets if asset.kind in wanted]

    def asset_references(self, asset_id: str) -> list[str]:
        """Where an asset is used, so a relink can warn about them."""
        uses: list[str] = []
        if self.audio.music.path and self.asset_by_id(asset_id) is not None:
            music_asset = self.asset_by_id(asset_id)
            if music_asset is not None and self.audio.music.path in (music_asset.path, music_asset.name):
                uses.append("background music")
        for effect in self.audio.sfx:
            asset = self.asset_by_id(asset_id)
            if asset is not None and effect.path in (asset.path, asset.name):
                uses.append(f"sound effect {effect.id}")
        for scene in self.scenes:
            for element in scene.elements:
                if element.asset_id == asset_id:
                    uses.append(f"scene '{scene.name or scene.id}'")
        return uses

    # -- integrity ---------------------------------------------------------

    def referenced_asset_ids(self) -> set[str]:
        ids: set[str] = set()
        for scene in self.scenes:
            for element in scene.elements:
                if element.asset_id:
                    ids.add(element.asset_id)
        return ids

    def unreferenced_assets(self) -> list[AssetSpec]:
        used = self.referenced_asset_ids()
        return [asset for asset in self.assets if asset.id not in used]

    def ensure_ids(self) -> int:
        """Give every scene, element, section and effect an id.  Returns count added."""
        added = 0
        if not self.project.id:
            self.project.id = new_id("project")
            added += 1
        for scene in self.scenes:
            if not scene.id:
                scene.id = new_id("scene")
                added += 1
            for element in scene.elements:
                if not element.id:
                    element.id = new_id("el")
                    added += 1
        for section in self.script.sections:
            if not section.id:
                section.id = new_id("sec")
                added += 1
        for effect in self.audio.sfx:
            if not effect.id:
                effect.id = new_id("sfx")
                added += 1
        for asset in self.assets:
            if not asset.id:
                asset.id = new_id("asset")
                added += 1
        return added

    def content_hash(self) -> str:
        """Stable hash of the model, ignoring timestamps and the save counter.

        Used to decide whether an autosave really holds different content.
        """
        import hashlib
        import json

        data = self.to_dict()
        meta = data.get("project", {})
        for key in ("modified_at", "project_version"):
            meta.pop(key, None)
        text = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # -- human readable ----------------------------------------------------

    def summary_lines(self) -> list[str]:
        duration = self.estimated_duration_seconds()
        return [
            f"Name        : {self.project.name}",
            f"Id          : {self.project.id or '(not set)'}",
            f"Channel     : {self.project.channel_name or '(none)'}",
            f"Format      : {self.format.label()}",
            f"Quality     : {self.format.quality_label()}",
            f"Voice       : {self.voice.label()}",
            f"Scenes      : {len(self.scenes)}",
            f"Assets      : {len(self.assets)}",
            f"Duration    : {duration:.1f}s (estimated)" if duration else "Duration    : not set",
            f"Created     : {self.project.created_at or '-'}",
            f"Modified    : {self.project.modified_at or '-'}",
            f"Saved       : version {self.project.project_version}",
            f"Schema      : {self.schema_version} (app {self.application_version})",
        ]

    def to_text(self) -> str:
        return "\n".join(self.summary_lines())


# --------------------------------------------------------------------------
# Construction helpers used by the service and the wizard
# --------------------------------------------------------------------------

def build_project(
    name: str,
    *,
    project_id: str = "",
    description: str = "",
    channel_id: str = "",
    channel_name: str = "",
    template_key: str = "blank",
    width: int = 1920,
    height: int = 1080,
    fps: int = 30,
    quality: Optional[dict] = None,
    background: str = "#101014",
    accent: str = "#4c8dff",
    heading_font: str = "DejaVu Sans",
    body_font: str = "DejaVu Sans",
    theme_id: str = "clean-dark",
    subtitles_enabled: bool = False,
    subtitle_font_size: int = 44,
    filename_template: str = "{name}_{seq}",
    language: str = "en-us",
    gender: str = "",
    voice: str = "",
    script_note: str = "",
    random_seed: Optional[int] = None,
) -> Project:
    """Create a new in-memory project from resolved settings.

    Deliberately **no** fake scenes, narration or media (directive section 41):
    a blank project is blank.  The one thing that is always present is the
    format/quality/voice configuration the user chose in the wizard.
    """
    from .presets import resolve_quality

    now = utc_now_iso()
    if quality:
        # The caller (wizard, template, CLI) already resolved the numbers.
        resolved_quality = dict(quality)
        resolved_quality.setdefault("quality_preset", "custom")
    else:
        resolved_quality = resolve_quality("high")
    project = Project()
    project.project = ProjectMeta(
        id=project_id or new_id("project"),
        name=name,
        description=description,
        channel_id=channel_id,
        channel_name=channel_name,
        created_at=now,
        modified_at=now,
        project_version=1,
        application_version=APP_VERSION,
        template=template_key,
        random_seed=int(random_seed if random_seed is not None else uuid.uuid4().int % 1_000_000_000),
    )
    project.format = FormatSpec.create(width, height, fps, resolved_quality, background=background)
    project.theme = ThemeSpec(
        id=theme_id,
        background=background,
        accent=accent,
        colors={"background": background, "accent": accent, "text": "#ffffff"},
        typography=TypographySpec(heading_font=heading_font, body_font=body_font),
        subtitle_style=SubtitleStyle(enabled=bool(subtitles_enabled), font_size=int(subtitle_font_size)),
    )
    project.voice = VoiceSpec(engine="kokoro", language=language, gender=gender, voice=voice)
    project.script = ScriptSpec(notes=script_note)
    project.export = ExportSpec(filename_template=filename_template or "{name}_{seq}")
    project.export.sync_from_format(project.format)
    return project


def project_sections() -> Iterable[str]:
    """Top-level section names of ``project.json`` (documentation + tests)."""
    return ("project", "format", "script", "voice", "theme", "audio", "scenes", "assets", "export")


__all__ = [
    "ANCHORS",
    "ASSET_KINDS",
    "AssetSpec",
    "AudioSpec",
    "ElementSpec",
    "ExportSpec",
    "FormatSpec",
    "MusicTrack",
    "NarrationSpec",
    "PROJECT_FILENAME",
    "Project",
    "ProjectMeta",
    "SCENE_TYPES",
    "SCRIPT_FILENAME",
    "SceneSpec",
    "ScriptSection",
    "ScriptSpec",
    "SoundEffect",
    "SubtitleStyle",
    "TRANSITION_TYPES",
    "ThemeSpec",
    "TransitionSpec",
    "TypographySpec",
    "VoiceSpec",
    "build_project",
    "new_id",
    "parse_iso",
    "project_sections",
    "utc_now_iso",
]
