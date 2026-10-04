"""Settings: defaults, round trips, validation and recovery (directive sections 12, 13, 64)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.settings import (
    LOG_LEVELS,
    SETTINGS_SCHEMA_VERSION,
    SUPPORTED_FPS,
    Settings,
    SettingsStore,
    detect_system_theme,
    from_dict,
)


def test_defaults_are_valid(settings: Settings) -> None:
    normalized, notes = settings.normalized()
    assert notes == []
    assert normalized.schema_version == SETTINGS_SCHEMA_VERSION
    assert normalized.project_defaults.fps in SUPPORTED_FPS
    assert normalized.voice.engine == "kokoro"
    assert normalized.logging.level in LOG_LEVELS


def test_round_trip_preserves_values(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json", tmp_path / "backups")
    original = Settings()
    original.general.theme = "light"
    original.general.advanced_mode = True
    original.voice.speed = 1.35
    original.voice.voice = "af_heart"
    original.media.output_container = "mkv"
    original.project_defaults.width = 1080
    original.project_defaults.height = 1920
    original.project_defaults.fps = 60
    original.window.width = 1500
    original.window.last_page = "diagnostics"

    store.save(original)
    result = store.load()

    assert result.source == "file"
    assert result.settings.general.theme == "light"
    assert result.settings.general.advanced_mode is True
    assert result.settings.voice.speed == pytest.approx(1.35)
    assert result.settings.voice.voice == "af_heart"
    assert result.settings.media.output_container == "mkv"
    assert result.settings.project_defaults.width == 1080
    assert result.settings.project_defaults.fps == 60
    assert result.settings.window.width == 1500
    assert result.settings.window.last_page == "diagnostics"


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "nothing-here.json")
    result = store.load()
    assert result.source == "defaults"
    assert result.existed is False
    assert result.settings.general.theme == "dark"


def test_damaged_file_is_recovered_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    backup_dir = tmp_path / "backups"
    path.write_text("{ this is not json at all", encoding="utf-8")

    result = SettingsStore(path, backup_dir).load()

    assert result.source == "recovered"
    assert result.existed is True
    assert result.error
    assert result.quarantined is not None and result.quarantined.exists()
    # The damaged file is kept, not silently deleted.
    assert result.quarantined.read_text(encoding="utf-8").startswith("{ this is not json")
    # Defaults are usable immediately.
    assert result.settings.general.theme == "dark"


def test_empty_file_is_treated_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("   \n", encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.source == "recovered"
    assert "empty" in (result.error or "")


def test_out_of_range_values_are_clamped_with_a_note(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "voice": {"speed": 99, "volume": -4},
                "media": {"crf": 900, "audio_bitrate_kbps": 1},
                "autosave": {"interval_seconds": 1},
                "project_defaults": {"fps": 999, "quality": "insane"},
            }
        ),
        encoding="utf-8",
    )

    result = SettingsStore(path).load()

    assert result.settings.voice.speed == pytest.approx(2.0)     # clamped to max
    assert result.settings.voice.volume == pytest.approx(0.0)    # clamped to min
    assert result.settings.media.crf == 51
    assert result.settings.media.audio_bitrate_kbps == 64
    assert result.settings.autosave.interval_seconds == 15
    assert result.settings.project_defaults.fps == 30
    assert result.settings.project_defaults.quality == "medium"
    assert len(result.notes) >= 6


def test_unknown_keys_are_ignored_not_fatal() -> None:
    settings = from_dict({"general": {"theme": "dark", "future_option": True}, "unknown_section": {"a": 1}})
    assert settings.general.theme == "dark"


def test_newer_schema_is_reported_but_loaded(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema_version": SETTINGS_SCHEMA_VERSION + 5, "general": {"theme": "light"}}), encoding="utf-8")

    result = SettingsStore(path).load()

    assert result.settings.general.theme == "light"
    assert any("newer version" in note for note in result.notes)


def test_wrong_types_fall_back_to_defaults() -> None:
    settings = from_dict({"project_defaults": {"width": "wide", "fps": "fast"}})
    normalized, notes = settings.normalized()
    assert normalized.project_defaults.width == 256  # clamped default
    assert normalized.project_defaults.fps == 30
    assert notes


def test_saving_keeps_a_backup_of_the_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    backup_dir = tmp_path / "backups"
    store = SettingsStore(path, backup_dir)

    first = Settings()
    first.general.theme = "light"
    store.save(first)

    second = Settings()
    second.general.theme = "dark"
    store.save(second)

    backups = list(backup_dir.glob("*.json*"))
    assert backups, "a backup of the previous settings file must exist"

    # And the newest file wins.
    assert store.load().settings.general.theme == "dark"


def test_reset_to_defaults_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path, tmp_path / "backups")
    custom = Settings()
    custom.general.theme = "light"
    store.save(custom)

    defaults = store.reset_to_defaults()

    assert defaults.general.theme == "dark"
    assert store.load().settings.general.theme == "dark"


def test_settings_survive_a_round_trip_through_dict(settings: Settings) -> None:
    settings.voice.speed = 1.1
    clone = settings.copy()
    assert clone.voice.speed == pytest.approx(1.1)
    clone.voice.speed = 1.9
    assert settings.voice.speed == pytest.approx(1.1), "copy() must deep copy"


def test_detect_system_theme_returns_a_valid_name() -> None:
    assert detect_system_theme() in {"dark", "light"}


def test_window_geometry_is_validated() -> None:
    settings = from_dict({"window": {"width": 10, "height": 10, "x": -999999, "y": -999999, "last_page": 5}})
    normalized, notes = settings.normalized()
    assert normalized.window.width >= 1024
    assert normalized.window.height >= 660
    assert normalized.window.last_page == "welcome"
    assert notes


def test_settings_file_is_written_atomically_no_temp_left(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(Settings())

    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []
    assert path.exists()
