"""Schema versioning and migration (directive sections 3 and 4).

The rules under test: an old project keeps working, a newer project is refused
without being touched, and nothing is silently dropped on the way through.
"""

from __future__ import annotations

import copy

import pytest

from app.core.errors import ProjectVersionError
from app.core.version import PROJECT_SCHEMA_VERSION
from app.project.migrations import (
    detect_version,
    migrate_project_data,
    registered_migrations,
)
from app.project.model import Project


def draft_v1_project() -> dict:
    """The shape published in docs/PROJECT_FORMAT.md during Stage A."""
    return {
        "schema_version": 1,
        "app_version": "0.1.0",
        "id": "my-project-a1b2c3",
        "name": "Old Project",
        "description": "Written by hand from the Stage A document.",
        "created_at": "2026-01-01T10:00:00Z",
        "modified_at": "2026-01-01T10:42:13Z",
        "random_seed": 20260101,
        "video": {"width": 1080, "height": 1920, "fps": 30, "background": "#101014"},
        "theme": {"id": "clean-dark", "overrides": {"accent": "#4c8dff"}},
        "audio": {
            "narration": {"enabled": True, "voice": "af_heart", "speed": 1.0, "volume": 0.9},
            "music": {"path": "assets/theme.mp3", "volume": 0.18, "ducking": True},
            "sfx": [],
        },
        "subtitles": {"enabled": True, "max_lines": 2, "font_size": 44},
        "scenes": [
            {
                "id": "scene-1",
                "type": "title",
                "start": 0.0,
                "duration": 3.4,
                "script": "Welcome to the studio.",
                "narration": {"file": "audio/scene-1.wav", "duration": 2.6, "voice": "af_heart", "speed": 1.0},
            }
        ],
        "render": {
            "quality": "final",
            "crf": 20,
            "audio_bitrate_kbps": 192,
            "container": "mp4",
            "encoder": "h264_cpu",
        },
    }


def test_an_old_project_is_migrated_into_the_current_sections() -> None:
    result = migrate_project_data(draft_v1_project(), path="old/project.json")
    data = result.data

    assert result.migrated is True
    assert (result.from_version, result.to_version) == (1, PROJECT_SCHEMA_VERSION)
    assert result.steps and "v1 -> v2" in result.steps[0]
    assert data["schema_version"] == PROJECT_SCHEMA_VERSION

    project = Project.from_dict(data)
    assert project.project.name == "Old Project"
    assert project.project.id == "my-project-a1b2c3"
    assert project.project.random_seed == 20260101
    assert project.application_version == "0.1.0"


def test_every_old_field_lands_somewhere_useful() -> None:
    project = Project.from_dict(migrate_project_data(draft_v1_project()).data)

    # video -> format, render -> format + export
    assert (project.format.width, project.format.height) == (1080, 1920)
    assert project.format.fps == 30
    assert project.format.background == "#101014"
    assert project.format.aspect_ratio == "9:16"
    assert project.format.codec == "h264_cpu"
    assert project.format.crf == 20
    assert project.format.container == "mp4"
    assert project.format.quality_preset == "high", "the old 'final' maps onto the new 'high'"
    assert project.export.container == "mp4"
    assert project.export.audio_bitrate_kbps == 192

    # audio.narration -> voice, subtitles -> theme.subtitle_style
    assert project.voice.voice == "af_heart"
    assert project.voice.volume == 0.9
    assert project.voice.engine == "kokoro"
    assert project.theme.subtitle_style.enabled is True
    assert project.theme.subtitle_style.font_size == 44
    assert project.theme.colors["accent"] == "#4c8dff"

    # music keeps its ducking intent
    assert project.audio.music.path == "assets/theme.mp3"
    assert project.audio.ducking_enabled is True

    # scenes survive, and the derived 'start' is kept instead of deleted
    assert project.scenes[0].id == "scene-1"
    assert project.scenes[0].narration.duration == 2.6
    assert project.scenes[0].extra.get("start") == 0.0


def test_a_migrated_project_is_valid_and_saves_without_changes() -> None:
    from app.project.validation import validate_project

    project = Project.from_dict(migrate_project_data(draft_v1_project()).data)
    report = validate_project(project)

    assert report.ok, report.to_text()


def test_an_unknown_quality_name_is_reported_not_guessed_silently() -> None:
    data = draft_v1_project()
    data["render"]["quality"] = "turbo-max"

    result = migrate_project_data(data)

    assert Project.from_dict(result.data).format.quality_preset == "medium"
    assert any("turbo-max" in warning for warning in result.warnings)


def test_a_newer_project_is_refused_and_left_untouched() -> None:
    """Directive section 4: never corrupt a project this build cannot read."""
    future = build_future_project()
    before = copy.deepcopy(future)

    with pytest.raises(ProjectVersionError) as caught:
        migrate_project_data(future, path="newer/project.json")

    assert caught.value.stored == 99
    assert caught.value.supported == PROJECT_SCHEMA_VERSION
    assert future == before, "the input must not be modified"
    friendly = caught.value.friendly()
    assert "schema version 99" in friendly.why
    assert "NOT been changed" in " ".join(friendly.actions)


def test_an_impossibly_old_version_is_refused() -> None:
    with pytest.raises(ProjectVersionError):
        migrate_project_data({"schema_version": 0, "name": "ancient"})


def test_a_missing_version_is_assumed_only_for_real_projects() -> None:
    unversioned = draft_v1_project()
    del unversioned["schema_version"]
    assert detect_version(unversioned) == 1

    with pytest.raises(ProjectVersionError):
        detect_version({"hello": "world"}, path="notes.json")


def test_a_non_numeric_version_is_refused() -> None:
    with pytest.raises(ProjectVersionError):
        detect_version({"schema_version": "two", "scenes": []})


def test_a_current_project_is_not_migrated() -> None:
    current = Project.from_dict(migrate_project_data(draft_v1_project()).data).to_dict()
    snapshot = copy.deepcopy(current)

    result = migrate_project_data(current)

    assert result.migrated is False
    assert result.steps == []
    assert result.data == snapshot


def test_the_migration_registry_is_inspectable() -> None:
    migrations = registered_migrations()
    assert 1 in migrations
    assert all(isinstance(description, str) and description for description in migrations.values())


def build_future_project() -> dict:
    """A project written by a much newer application."""
    data = draft_v1_project()
    data["schema_version"] = 99
    data["hologram_tracks"] = [{"id": "h1"}]
    return data
