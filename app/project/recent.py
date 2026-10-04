"""Recent projects and channel profiles.

Both live in ``config/`` and are written with the Stage A atomic writer, so
there is no second storage mechanism (directive section 38).  A damaged file is
tolerated: the list comes back empty rather than the application refusing to
start.

``recent_projects.json`` is a *list of pointers*, never a copy of a project.
"Remove from recent" deletes the entry only - project files are never touched
by it (directive section 15).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from ..core.atomicio import FileWriteError, atomic_write_json, load_json
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event
from ..core.paths import safe_filename
from .presets import BUILT_IN_CHANNEL, ChannelProfile

LOGGER = get_logger("project.registry")

RECENT_FILENAME = "recent_projects.json"
CHANNELS_FILENAME = "channels.json"

#: How many entries the recent list keeps (bounded, oldest dropped).
DEFAULT_MAX_RECENT = 25

#: Status labels shown in the dashboard and browser.
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_DAMAGED = "damaged"
STATUS_MIGRATION = "needs migration"
STATUS_FAVOURITE = "favourite"


# --------------------------------------------------------------------------
# Recent projects
# --------------------------------------------------------------------------

@dataclass
class RecentProject:
    """One dashboard row."""

    path: str
    name: str = ""
    channel_name: str = ""
    opened_at: str = ""
    modified_at: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    aspect_ratio: str = ""
    quality: str = ""
    scenes: int = 0
    duration_seconds: float = 0.0
    project_version: int = 0
    schema_version: int = 0
    thumbnail: str = ""
    favorite: bool = False
    archived: bool = False
    status: str = STATUS_OK
    notes: str = ""

    # -- derived -----------------------------------------------------------

    @property
    def folder(self) -> Path:
        return Path(self.path)

    @property
    def project_file(self) -> Path:
        return self.folder / "project.json"

    def resolution_label(self) -> str:
        if not self.width or not self.height:
            return "-"
        return f"{self.width}x{self.height}"

    def status_label(self) -> str:
        return {
            STATUS_OK: "Ready",
            STATUS_MISSING: "Folder missing",
            STATUS_DAMAGED: "Needs repair",
            STATUS_MIGRATION: "Needs update",
            STATUS_FAVOURITE: "Favourite",
        }.get(self.status, self.status)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> Optional["RecentProject"]:
        if not isinstance(data, dict) or not data.get("path"):
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def describe(self) -> str:
        parts = [self.name or self.folder.name]
        if self.channel_name:
            parts.append(self.channel_name)
        parts.append(self.resolution_label())
        if self.modified_at:
            parts.append(self.modified_at)
        return " - ".join(parts)


class RecentProjectsStore:
    """The bounded recent-projects list."""

    def __init__(self, path: Path, max_entries: int = DEFAULT_MAX_RECENT) -> None:
        self.path = Path(path)
        self.max_entries = max(5, int(max_entries))

    # -- I/O ---------------------------------------------------------------

    def load(self) -> list[RecentProject]:
        result = load_json(self.path, default=None)
        if not result.ok or not isinstance(result.data, list):
            if result.exists:
                log_event(
                    Event.WARNING,
                    "The recent projects list could not be read - starting an empty list",
                    level=logging.WARNING,
                    logger=LOGGER,
                    path=str(self.path),
                    reason=result.error,
                )
            return []
        entries = [entry for entry in (RecentProject.from_dict(item) for item in result.data) if entry is not None]
        return entries

    def save(self, entries: list[RecentProject]) -> bool:
        payload = [entry.to_dict() for entry in entries[: self.max_entries]]
        try:
            atomic_write_json(self.path, payload)
        except FileWriteError as exc:
            log_event(
                Event.WARNING,
                "The recent projects list could not be saved",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(self.path),
                reason=str(exc),
            )
            return False
        return True

    # -- operations --------------------------------------------------------

    def add(self, entry: RecentProject) -> list[RecentProject]:
        """Put *entry* at the top of the list, replacing any older copy."""
        entries = [item for item in self.load() if Path(item.path) != Path(entry.path)]
        entries.insert(0, entry)
        entries = entries[: self.max_entries]
        self.save(entries)
        return entries

    def update(self, path: Path, **changes) -> Optional[RecentProject]:
        """Update fields of an existing entry (keeps its position)."""
        entries = self.load()
        target: Optional[RecentProject] = None
        for item in entries:
            if Path(item.path) == Path(path):
                for key, value in changes.items():
                    if hasattr(item, key):
                        setattr(item, key, value)
                target = item
                break
        if target is not None:
            self.save(entries)
        return target

    def remove(self, path: Path) -> bool:
        """Forget a project.  **Never** deletes project files (section 15)."""
        entries = self.load()
        kept = [item for item in entries if Path(item.path) != Path(path)]
        if len(kept) == len(entries):
            return False
        self.save(kept)
        log_event(Event.USER_ACTION, "Project removed from the recent list", logger=LOGGER, path=str(path))
        return True

    def set_favorite(self, path: Path, value: bool) -> bool:
        return self.update(path, favorite=bool(value)) is not None

    def set_archived(self, path: Path, value: bool) -> bool:
        return self.update(path, archived=bool(value)) is not None

    def find(self, path: Path) -> Optional[RecentProject]:
        for item in self.load():
            if Path(item.path) == Path(path):
                return item
        return None

    def prune_missing(self) -> list[RecentProject]:
        """Drop entries whose folder no longer exists."""
        entries = self.load()
        kept = [item for item in entries if item.project_file.exists()]
        if len(kept) != len(entries):
            self.save(kept)
        return kept

    def refresh_status(self) -> list[RecentProject]:
        """Mark entries whose folder disappeared or whose file is damaged."""
        from ..core.version import PROJECT_SCHEMA_VERSION

        entries = self.load()
        changed = False
        for item in entries:
            previous = item.status
            if not item.project_file.exists():
                item.status = STATUS_MISSING
            else:
                probe = load_json(item.project_file, default=None)
                if not probe.ok:
                    item.status = STATUS_DAMAGED
                elif isinstance(probe.data, dict) and int(probe.data.get("schema_version") or 0) < PROJECT_SCHEMA_VERSION:
                    item.status = STATUS_MIGRATION
                else:
                    item.status = STATUS_FAVOURITE if item.favorite else STATUS_OK
            changed = changed or item.status != previous
        if changed:
            self.save(entries)
        return entries


def entry_from_project(project, path: Path, thumbnail: str = "") -> RecentProject:
    """Build a dashboard row from a loaded project."""
    from ..core.version import PROJECT_SCHEMA_VERSION

    folder = Path(path).resolve().parent
    return RecentProject(
        path=str(folder),
        name=project.project.name or folder.name,
        channel_name=project.project.channel_name,
        opened_at=project.project.modified_at,
        modified_at=project.project.modified_at,
        width=int(project.format.width or 0),
        height=int(project.format.height or 0),
        fps=int(project.format.fps or 0),
        aspect_ratio=project.format.aspect_ratio,
        quality=project.format.quality_preset,
        scenes=len(project.scenes),
        duration_seconds=float(project.estimated_duration_seconds() or 0.0),
        project_version=int(project.project.project_version or 0),
        schema_version=int(project.schema_version or PROJECT_SCHEMA_VERSION),
        thumbnail=thumbnail,
        favorite=bool(project.project.favorite),
        archived=bool(project.project.archived),
        status=STATUS_FAVOURITE if project.project.favorite else STATUS_OK,
    )


# --------------------------------------------------------------------------
# Channel profiles
# --------------------------------------------------------------------------

class ChannelStore:
    """Creator/channel profiles: reusable defaults, stored apart from projects.

    A project keeps the channel **id** plus its own resolved settings, so
    editing or deleting a profile never changes a finished project (section 6).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[ChannelProfile]:
        result = load_json(self.path, default=None)
        profiles: list[ChannelProfile] = []
        if result.ok and isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    profiles.append(ChannelProfile.from_dict(item))
        if not any(profile.id == BUILT_IN_CHANNEL.id for profile in profiles):
            profiles.insert(0, ChannelProfile(**asdict(BUILT_IN_CHANNEL)))
        return profiles

    def save(self, profiles: list[ChannelProfile]) -> bool:
        payload = [profile.to_dict() for profile in profiles if profile.id != BUILT_IN_CHANNEL.id]
        try:
            atomic_write_json(self.path, payload)
        except FileWriteError as exc:
            log_event(
                Event.WARNING,
                "Channel profiles could not be saved",
                level=logging.WARNING,
                logger=LOGGER,
                path=str(self.path),
                reason=str(exc),
            )
            return False
        return True

    def get(self, channel_id: str) -> Optional[ChannelProfile]:
        if not channel_id or channel_id == BUILT_IN_CHANNEL.id:
            return ChannelProfile(**asdict(BUILT_IN_CHANNEL))
        for profile in self.load():
            if profile.id == channel_id:
                return profile
        return None

    def upsert(self, profile: ChannelProfile) -> list[ChannelProfile]:
        profiles = [item for item in self.load() if item.id not in (profile.id, BUILT_IN_CHANNEL.id)]
        if not profile.id:
            profile.id = safe_filename(profile.name or "channel", fallback="channel", max_length=40).lower()
        profiles.append(profile)
        self.save(profiles)
        return self.load()

    def delete(self, channel_id: str) -> bool:
        if channel_id == BUILT_IN_CHANNEL.id:
            return False
        profiles = [item for item in self.load() if item.id != channel_id]
        return self.save(profiles)

    def defaults_for(self, channel_id: str) -> dict:
        """The settings a new project starts from for this channel."""
        profile = self.get(channel_id) or BUILT_IN_CHANNEL
        return {
            "channel_id": profile.id,
            "channel_name": profile.name,
            "aspect": profile.aspect,
            "width": profile.width,
            "height": profile.height,
            "fps": profile.fps,
            "quality": profile.quality,
            "background": profile.background,
            "accent": profile.accent,
            "heading_font": profile.heading_font,
            "body_font": profile.body_font,
            "theme_id": profile.theme_id,
            "voice": profile.voice,
            "language": profile.language,
            "subtitles_enabled": profile.subtitles_enabled,
            "subtitle_font_size": profile.subtitle_font_size,
            "filename_template": profile.filename_template,
        }


__all__ = [
    "CHANNELS_FILENAME",
    "ChannelStore",
    "DEFAULT_MAX_RECENT",
    "RECENT_FILENAME",
    "RecentProject",
    "RecentProjectsStore",
    "STATUS_DAMAGED",
    "STATUS_FAVOURITE",
    "STATUS_MIGRATION",
    "STATUS_MISSING",
    "STATUS_OK",
    "entry_from_project",
]
