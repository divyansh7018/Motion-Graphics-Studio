"""Project schema migrations.

Why this exists (directive section 4): a project file outlives the build that
wrote it.  When the format changes, an old project must keep working, and a
project written by a **newer** build must be refused with a clear message rather
than being partially read and then silently truncated on the next save.

How it works
------------
* Each step is a pure function ``dict -> dict`` registered with
  :func:`register_migration` under the version it converts **from**.
* :func:`migrate_project_data` walks the chain until the current version is
  reached, recording every step so the change can be logged and reported.
* Nothing is written to disk here.  The caller decides when to save, and a
  migrated project is only saved by an explicit user action or autosave.

Adding a future migration
-------------------------
::

    @register_migration(2, "Split audio into tracks")
    def _v2_to_v3(data: dict) -> dict:
        ...
        data["schema_version"] = 3
        return data

Bump ``PROJECT_SCHEMA_VERSION`` in ``app/core/version.py`` in the same commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..core.errors import ProjectVersionError
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.version import MIN_SUPPORTED_PROJECT_SCHEMA, PROJECT_SCHEMA_VERSION

LOGGER = get_logger("project.migrations")

#: A step can report problems by storing them under this key; the runner pops it
#: before the result is handed back, so it never reaches the project file.
WARNINGS_KEY = "__migration_warnings__"

MigrationStep = Callable[[dict], dict]

#: from_version -> (step function, description)
_REGISTRY: dict[int, tuple[MigrationStep, str]] = {}


def register_migration(from_version: int, description: str = "") -> Callable[[MigrationStep], MigrationStep]:
    """Register a migration step for projects at *from_version*."""

    def decorator(func: MigrationStep) -> MigrationStep:
        if from_version in _REGISTRY:  # pragma: no cover - developer mistake
            raise ValueError(f"A migration from schema {from_version} is already registered.")
        _REGISTRY[from_version] = (func, description or func.__doc__ or f"v{from_version} -> v{from_version + 1}")
        return func

    return decorator


def registered_migrations() -> dict[int, str]:
    """Which migrations exist (used by diagnostics and tests)."""
    return {version: description for version, (_step, description) in sorted(_REGISTRY.items())}


@dataclass
class MigrationResult:
    """What happened while bringing a project file up to date."""

    data: dict
    from_version: int
    to_version: int
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> bool:
        return self.from_version != self.to_version

    def summary(self) -> str:
        if not self.migrated:
            return f"Project schema {self.to_version} (current) - no migration needed."
        chain = ", ".join(self.steps)
        return f"Migrated project schema {self.from_version} -> {self.to_version}: {chain}"


# --------------------------------------------------------------------------
# Version 1 -> 2
# --------------------------------------------------------------------------

#: The Stage A documentation published a draft shape.  Anyone who wrote a
#: project file by hand from that document gets migrated instead of broken.
_QUALITY_RENAMES = {"draft": "draft", "medium": "medium", "final": "high"}


@register_migration(1, "Stage A draft shape -> the implemented v2 sections")
def _v1_to_v2(data: dict) -> dict:
    """Move the flat draft keys into the v2 section layout.

    Draft (v1)                        Implemented (v2)
    ------------------------------    ----------------------------------
    ``app_version``                   ``application_version``
    ``id``/``name``/``created_at``/   ``project`` section
      ``modified_at``/``random_seed``
    ``video``                         ``format`` (+ resolved quality)
    ``render``                        ``format`` + ``export``
    ``subtitles``                     ``theme.subtitle_style``
    ``theme.overrides``               ``theme.colors``
    ``audio.narration``               ``voice`` + ``audio.narration_volume``
    """
    warnings: list[str] = []
    out: dict = {"schema_version": 2}

    out["application_version"] = data.get("app_version") or data.get("application_version") or ""

    # -- project meta ------------------------------------------------------
    meta: dict = {}
    for key in ("id", "name", "description", "channel_id", "channel_name", "created_at", "modified_at"):
        if key in data:
            meta[key] = data[key]
    meta.setdefault("project_version", int(data.get("project_version") or 1))
    meta.setdefault("application_version", out["application_version"])
    if "random_seed" in data:
        meta["random_seed"] = data["random_seed"]
    out["project"] = meta

    # -- format + quality --------------------------------------------------
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    render = data.get("render") if isinstance(data.get("render"), dict) else {}
    fmt: dict = {
        "width": video.get("width", 1920),
        "height": video.get("height", 1080),
        "fps": video.get("fps", 30),
        "background": video.get("background", "#101014"),
    }
    quality_name = str(render.get("quality", "medium"))
    fmt["quality_preset"] = _QUALITY_RENAMES.get(quality_name, "medium")
    if quality_name not in _QUALITY_RENAMES:
        warnings.append(f"Unknown quality '{quality_name}' in the old file - using 'medium'.")
    fmt["codec"] = render.get("encoder") or render.get("codec") or "h264_cpu"
    fmt["crf"] = int(render.get("crf", 20) or 20)
    fmt["container"] = render.get("container", "mp4")
    fmt["audio_bitrate_kbps"] = int(render.get("audio_bitrate_kbps", 192) or 192)
    fmt["encoder_preset"] = render.get("encoder_preset", "medium")
    fmt["bitrate_kbps"] = int(render.get("bitrate_kbps", 0) or 0)
    fmt["sample_rate"] = int(render.get("sample_rate", 48000) or 48000)
    fmt["audio_codec"] = render.get("audio_codec") or ("opus" if fmt["container"] == "webm" else "aac")
    from .presets import aspect_ratio_label

    fmt["aspect_ratio"] = aspect_ratio_label(int(fmt["width"]), int(fmt["height"]))
    out["format"] = fmt

    # -- theme -------------------------------------------------------------
    theme_in = data.get("theme") if isinstance(data.get("theme"), dict) else {}
    theme: dict = {
        "id": theme_in.get("id", "clean-dark"),
        "background": fmt["background"],
        "colors": dict(theme_in.get("overrides") or {}),
    }
    if theme_in.get("colors"):
        theme["colors"].update(dict(theme_in["colors"]))
    subtitles_in = data.get("subtitles") if isinstance(data.get("subtitles"), dict) else {}
    if subtitles_in:
        theme["subtitle_style"] = {
            "enabled": bool(subtitles_in.get("enabled", False)),
            "font_size": int(subtitles_in.get("font_size", 44) or 44),
            "max_lines": int(subtitles_in.get("max_lines", 2) or 2),
        }
    if theme_in.get("fonts"):
        theme["typography"] = dict(theme_in["fonts"])
    out["theme"] = theme

    # -- voice + audio -----------------------------------------------------
    audio_in = data.get("audio") if isinstance(data.get("audio"), dict) else {}
    narration_in = audio_in.get("narration") if isinstance(audio_in.get("narration"), dict) else {}
    out["voice"] = {
        "engine": narration_in.get("engine", "kokoro"),
        "voice": narration_in.get("voice", ""),
        "speed": float(narration_in.get("speed", 1.0) or 1.0),
        "volume": float(narration_in.get("volume", 1.0) or 1.0),
        "language": narration_in.get("language", "en-us"),
    }
    music_in = audio_in.get("music") if isinstance(audio_in.get("music"), dict) else {}
    out["audio"] = {
        "narration_enabled": bool(narration_in.get("enabled", True)),
        "narration_volume": float(narration_in.get("volume", 1.0) or 1.0),
        "music": {
            "path": music_in.get("path", ""),
            "volume": float(music_in.get("volume", 0.18) or 0.18),
            "loop": bool(music_in.get("loop", True)),
        },
        "sfx": list(audio_in.get("sfx") or []),
        "ducking_enabled": bool(music_in.get("ducking", True)),
    }

    # -- script ------------------------------------------------------------
    script_in = data.get("script") if isinstance(data.get("script"), dict) else {}
    out["script"] = {
        "source_text": script_in.get("source_text", data.get("script_text", "")),
        "notes": script_in.get("notes", ""),
        "sections": list(script_in.get("sections") or []),
    }

    # -- scenes ------------------------------------------------------------
    scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    migrated_scenes: list[dict] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            warnings.append("A scene entry was not an object and was skipped.")
            continue
        entry = dict(scene)
        entry.setdefault("type", "blank")
        entry.setdefault("name", entry.get("type", "scene"))
        entry.setdefault("duration", 3.0)
        # ``start`` is derived from durations in v2; keep the stored value in
        # the scene's unknown keys so nothing is lost.
        migrated_scenes.append(entry)
    out["scenes"] = migrated_scenes

    # -- assets ------------------------------------------------------------
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    out["assets"] = [asset for asset in assets if isinstance(asset, dict)]
    if len(out["assets"]) != len(assets):
        warnings.append("One or more asset entries were not objects and were skipped.")

    # -- export ------------------------------------------------------------
    out["export"] = {
        "output_dir": "renders",
        "filename_template": data.get("filename_template", "{name}_{seq}"),
        "next_sequence_number": int(data.get("next_sequence_number", 1) or 1),
        "container": fmt["container"],
        "codec": fmt["codec"],
        "crf": fmt["crf"],
        "bitrate_kbps": fmt["bitrate_kbps"],
        "encoder_preset": fmt["encoder_preset"],
        "pixel_format": render.get("pixel_format", "yuv420p"),
        "audio_codec": fmt["audio_codec"],
        "audio_bitrate_kbps": fmt["audio_bitrate_kbps"],
        "sample_rate": fmt["sample_rate"],
        "overwrite_policy": "never",
    }

    # -- anything else the old file carried --------------------------------
    handled = {
        "schema_version", "app_version", "application_version", "id", "name", "description",
        "channel_id", "channel_name", "created_at", "modified_at", "project_version",
        "random_seed", "video", "render", "theme", "subtitles", "audio", "script",
        "script_text", "scenes", "assets", "filename_template", "next_sequence_number",
    }
    for key, value in data.items():
        if key not in handled and key not in out:
            out[key] = value

    if warnings:
        # Carried out of the step; migrate_project_data removes it again.
        out[WARNINGS_KEY] = warnings
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def detect_version(data: object, path: str = "project.json") -> int:
    """Read ``schema_version`` from a parsed project file.

    A missing version is treated as the oldest supported one **only** when the
    file looks like a project; anything else is refused so a random JSON file
    is never turned into an empty project.
    """
    if not isinstance(data, dict):
        raise ProjectVersionError(
            path=path,
            stored=0,
            supported=PROJECT_SCHEMA_VERSION,
            minimum=MIN_SUPPORTED_PROJECT_SCHEMA,
        )
    raw = data.get("schema_version")
    if raw is None:
        looks_like_project = any(key in data for key in ("scenes", "video", "name", "format", "script"))
        if looks_like_project:
            log_event(
                Event.PROJECT_MIGRATION,
                "Project file has no schema_version - assuming the oldest supported version",
                level=logging.WARNING,
                logger=LOGGER,
                path=path,
            )
            return MIN_SUPPORTED_PROJECT_SCHEMA
        raise ProjectVersionError(
            path=path,
            stored=0,
            supported=PROJECT_SCHEMA_VERSION,
            minimum=MIN_SUPPORTED_PROJECT_SCHEMA,
        )
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ProjectVersionError(
            path=path,
            stored=0,
            supported=PROJECT_SCHEMA_VERSION,
            minimum=MIN_SUPPORTED_PROJECT_SCHEMA,
        ) from None


def migrate_project_data(
    data: dict,
    path: str = "project.json",
    target: int = PROJECT_SCHEMA_VERSION,
) -> MigrationResult:
    """Bring *data* up to *target*, refusing anything this build cannot handle.

    Raises :class:`ProjectVersionError` for a newer (or impossibly old) file.
    The input dict is never modified in place.
    """
    version = detect_version(data, path=path)

    if version > target:
        raise ProjectVersionError(path=path, stored=version, supported=target, minimum=MIN_SUPPORTED_PROJECT_SCHEMA)
    if version < MIN_SUPPORTED_PROJECT_SCHEMA:
        raise ProjectVersionError(path=path, stored=version, supported=target, minimum=MIN_SUPPORTED_PROJECT_SCHEMA)

    original = version
    current = dict(data)
    steps: list[str] = []
    warnings: list[str] = []

    while version < target:
        entry = _REGISTRY.get(version)
        if entry is None:  # pragma: no cover - a gap would be a developer error
            raise ProjectVersionError(
                path=path,
                stored=version,
                supported=target,
                minimum=MIN_SUPPORTED_PROJECT_SCHEMA,
            )
        step, description = entry
        current = dict(step(dict(current)))
        current["schema_version"] = version + 1
        warnings.extend(current.pop(WARNINGS_KEY, []) or [])
        steps.append(f"v{version} -> v{version + 1} ({description})")
        version += 1

    current.pop(WARNINGS_KEY, None)
    result = MigrationResult(
        data=current,
        from_version=original,
        to_version=version,
        steps=steps,
        warnings=warnings,
    )
    if result.migrated:
        log_event(
            Event.PROJECT_MIGRATION,
            result.summary(),
            logger=LOGGER,
            path=path,
            from_version=original,
            to_version=version,
            steps=len(steps),
        )
    return result


__all__ = [
    "MigrationResult",
    "MigrationStep",
    "detect_version",
    "migrate_project_data",
    "register_migration",
    "registered_migrations",
]
