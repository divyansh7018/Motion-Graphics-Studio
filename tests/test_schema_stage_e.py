"""Stage E added model fields without bumping the project schema - prove that is safe.

The directive asks for a schema bump only when a change requires one, and for a
migration when the schema does change.  Stage E's additions are additive with
defaults, so ``PROJECT_SCHEMA_VERSION`` stays at 3.  These tests are the
evidence for that decision rather than an assertion of it: an old file must
load with the new defaults filled in, a new file must round-trip, and a field
this build does not know about must survive instead of being discarded.
"""

from __future__ import annotations

import pytest

from app.core.version import PROJECT_SCHEMA_VERSION
from app.project.model import Project

#: A schema-3 project as it looked before Stage E added its fields.
LEGACY = {
    "schema_version": 3,
    "project": {"id": "p1", "name": "Legacy Project",
                "created_at": "2026-01-01T00:00:00Z"},
    "format": {"width": 1920, "height": 1080, "fps": 30, "quality_preset": "high",
               "codec": "h264_cpu", "crf": 20, "container": "mp4"},
    "script": {"source_text": "Hello."},
    "voice": {"voice": "", "language": "en-us"},
    "theme": {"id": "clean-dark"},
    "audio": {"narration_enabled": True, "narration_volume": 1.0},
    "subtitles": {"enabled": False},
    "scenes": [{"id": "s1", "name": "Scene 1", "duration": 3.0}],
    "assets": [],
    "export": {},
}


def test_the_schema_was_not_bumped_for_stage_e() -> None:
    """A bump with no migration behind it would be worse than no bump."""
    assert PROJECT_SCHEMA_VERSION == 3
    assert LEGACY["schema_version"] == PROJECT_SCHEMA_VERSION


def test_a_pre_stage_e_project_loads_with_the_new_defaults() -> None:
    loaded = Project.from_dict(dict(LEGACY))

    assert loaded.project.name == "Legacy Project"
    # Music fades, ducking and the output folder are Stage E additions.
    assert loaded.audio.music.fade_in == pytest.approx(1.0)
    assert loaded.audio.music.fade_out == pytest.approx(2.0)
    assert loaded.audio.ducking_enabled is True
    assert loaded.audio.ducking_level == pytest.approx(0.35)
    assert loaded.audio.sfx == []
    assert loaded.subtitles.cues == []
    assert loaded.export.output_dir == "renders"


def test_a_saved_project_round_trips_exactly() -> None:
    loaded = Project.from_dict(dict(LEGACY))
    once = loaded.to_dict()
    twice = Project.from_dict(once).to_dict()

    assert twice == once


def test_a_field_this_build_does_not_know_is_kept() -> None:
    """Forward compatibility: a newer file must not lose data here."""
    data = dict(LEGACY)
    data["audio"] = dict(LEGACY["audio"], some_future_knob=42)

    loaded = Project.from_dict(data)
    assert loaded.audio.extra.get("some_future_knob") == 42
    assert Project.from_dict(loaded.to_dict()).audio.extra.get("some_future_knob") == 42


def test_stage_e_settings_survive_a_round_trip() -> None:
    """The fields the Stage E pages write must persist, not just sit in memory."""
    loaded = Project.from_dict(dict(LEGACY))
    loaded.audio.master_volume = 0.8
    loaded.audio.ducking_attack = 0.4
    loaded.audio.music.path = "assets/music.wav"
    loaded.audio.music.loop = False
    loaded.subtitles.enabled = True
    loaded.subtitles.margin_percent = 9.0
    loaded.subtitles.burn_in = True
    loaded.export.output_dir = "my_exports"
    loaded.export.filename_template = "{name}_take{seq}"

    reopened = Project.from_dict(loaded.to_dict())

    assert reopened.audio.master_volume == pytest.approx(0.8)
    assert reopened.audio.ducking_attack == pytest.approx(0.4)
    assert reopened.audio.music.path == "assets/music.wav"
    assert reopened.audio.music.loop is False
    assert reopened.subtitles.enabled is True
    assert reopened.subtitles.margin_percent == pytest.approx(9.0)
    assert reopened.subtitles.burn_in is True
    assert reopened.export.output_dir == "my_exports"
    assert reopened.export.filename_template == "{name}_take{seq}"
