"""The project model: serialisation, copies, timeline and script handling.

Directive sections 2, 3 and 6: a typed model, one versioned file, nothing
destroyed on a round trip, and no fake content in a blank project.
"""

from __future__ import annotations

from app.project.model import (
    AssetSpec,
    ElementSpec,
    Project,
    SceneSpec,
    build_project,
    new_id,
    parse_iso,
    utc_now_iso,
)
from app.project.presets import resolve_quality
from app.core.version import APP_VERSION, PROJECT_SCHEMA_VERSION


def test_a_new_project_has_the_documented_sections() -> None:
    project = build_project("My Video")
    data = project.to_dict()

    for key in ("schema_version", "application_version", "project", "format", "script", "voice", "theme", "audio", "scenes", "assets", "export"):
        assert key in data, f"project.json must contain '{key}'"
    assert data["schema_version"] == PROJECT_SCHEMA_VERSION
    assert data["application_version"] == APP_VERSION


def test_a_blank_project_contains_no_fake_content() -> None:
    """Directive section 41: no fake scenes, narration or media."""
    project = build_project("My Video")

    assert project.scenes == []
    assert project.assets == []
    assert project.script.source_text == ""
    assert project.audio.music.path == ""
    assert project.audio.sfx == []
    assert project.voice.voice == ""


def test_serialisation_round_trip_is_lossless() -> None:
    project = build_project("Round Trip", channel_name="Chan", quality=resolve_quality("ultra"))
    project.script.source_text = "Line one.\n\nLine two with  a double space."
    project.add_scene(SceneSpec(id="scene-1", name="Intro", type="title", script="Hello"))
    project.add_asset(AssetSpec(id="asset-1", name="logo.png", kind="logo", path="assets/logo.png"))

    data = project.to_dict()
    restored = Project.from_dict(data)

    assert restored.to_dict() == data
    assert restored.project.name == "Round Trip"
    assert restored.format.quality_preset == "ultra"
    assert restored.format.crf == 15
    assert restored.scenes[0].name == "Intro"
    assert restored.assets[0].path == "assets/logo.png"


def test_unknown_keys_survive_a_round_trip() -> None:
    """A project written by a newer build must not lose its extra data."""
    data = build_project("Future").to_dict()
    data["brand_kit"] = {"logo": "assets/kit.png", "version": 3}
    data["format"]["experimental_flag"] = True
    data["scenes"] = [{"id": "s1", "type": "text", "duration": 2.0, "future_element": {"x": 1}}]

    project = Project.from_dict(data)
    again = project.to_dict()

    assert again["brand_kit"] == {"logo": "assets/kit.png", "version": 3}
    assert again["format"]["experimental_flag"] is True
    assert again["scenes"][0]["future_element"] == {"x": 1}


def test_junk_input_never_crashes_the_model() -> None:
    for junk in (None, 42, "text", [], {"format": "not a dict", "scenes": "nope", "assets": {"a": 1}}):
        project = Project.from_dict(junk)
        assert project.to_dict()["schema_version"] == PROJECT_SCHEMA_VERSION


def test_clone_creates_an_independent_project() -> None:
    original = build_project("Original")
    original.add_scene(SceneSpec(id="scene-1", name="Intro", type="title"))

    clone = original.clone_as("Duplicate")

    assert clone.project.id != original.project.id
    assert clone.project.name == "Duplicate"
    assert clone.project.project_version == 1
    assert clone.project.created_at != ""

    # Editing the clone must never touch the original.
    clone.scenes[0].name = "Changed"
    clone.format.width = 720
    assert original.scenes[0].name == "Intro"
    assert original.format.width == 1920


def test_quality_is_stored_as_numbers_not_just_a_name() -> None:
    """Directive section 26: 'High' alone is not enough to reproduce a render."""
    project = build_project("Quality", quality=resolve_quality("high"))
    fmt = project.format

    assert fmt.quality_preset == "high"
    assert fmt.crf == 20
    assert fmt.encoder_preset == "slow"
    assert fmt.pixel_format == "yuv420p"
    assert fmt.audio_codec == "aac"
    assert fmt.audio_bitrate_kbps == 192
    assert fmt.sample_rate == 48000
    assert project.export.crf == fmt.crf, "export mirrors the resolved format"


def test_timeline_uses_measured_narration_over_manual_duration() -> None:
    """Directive section 27: real audio length is the authority for timing."""
    project = build_project("Timing")
    _first = project.add_scene(SceneSpec(id="s1", name="One", duration=5.0))
    second = project.add_scene(SceneSpec(id="s2", name="Two", duration=5.0))
    second.narration.duration = 2.5
    second.narration.file = "audio/s2.wav"

    rows = project.timeline()

    assert rows[0] == {"index": 0, "id": "s1", "name": "One", "start": 0.0, "duration": 5.0, "end": 5.0, "timed_by": "manual"}
    assert rows[1]["start"] == 5.0
    assert rows[1]["duration"] == 2.5
    assert rows[1]["timed_by"] == "narration"
    assert project.total_duration_seconds() == 7.5


def test_scene_ordering_helpers_keep_the_list_consistent() -> None:
    project = build_project("Scenes")
    project.add_scene(SceneSpec(id="a"))
    project.add_scene(SceneSpec(id="b"))
    project.add_scene(SceneSpec(id="c"))

    assert project.move_scene("c", 0)
    assert [scene.id for scene in project.scenes] == ["c", "a", "b"]
    assert project.remove_scene("a") is not None
    assert [scene.id for scene in project.scenes] == ["c", "b"]
    assert project.scene_by_id("missing") is None
    assert project.move_scene("missing", 0) is False


def test_assets_resolve_relative_to_the_project_folder(tmp_path) -> None:
    project = build_project("Assets")
    portable = project.add_asset(AssetSpec(name="bg.png", kind="image", path="assets/bg.png"))
    external = project.add_asset(AssetSpec(name="font.ttf", kind="font", absolute_path="C:/Windows/Fonts/arial.ttf"))

    assert portable.resolve(tmp_path) == tmp_path / "assets" / "bg.png"
    assert portable.is_portable is True
    assert external.is_portable is False
    assert project.assets_of_kind("image", "logo")[0].id == portable.id


def test_asset_references_are_reported_before_a_relink() -> None:
    project = build_project("Refs")
    asset = project.add_asset(AssetSpec(id="asset-1", name="bg.png", kind="image", path="assets/bg.png"))
    scene = project.add_scene(SceneSpec(id="s1", name="Intro"))
    scene.elements.append(ElementSpec(id="el-1", kind="image", asset_id="asset-1"))

    assert project.asset_references("asset-1") == ["scene 'Intro'"]
    assert project.unreferenced_assets() == []

    project.remove_scene("s1")
    assert [item.id for item in project.unreferenced_assets()] == [asset.id]


def test_ensure_ids_fills_in_missing_identifiers() -> None:
    project = build_project("Ids")
    project.project.id = ""
    # Appended directly: add_scene() assigns an id itself, this checks ensure_ids().
    project.scenes.append(SceneSpec(id="", name="No id"))
    project.scenes[0].elements.append(ElementSpec(kind="text", text="Hello"))

    added = project.ensure_ids()

    assert added == 3
    assert project.project.id.startswith("project-")
    assert project.scenes[0].id.startswith("scene-")
    assert project.scenes[0].elements[0].id.startswith("el-")


def test_content_hash_ignores_timestamps_but_not_content() -> None:
    project = build_project("Hash")
    first = project.content_hash()

    project.touch()
    project.bump_version()
    assert project.content_hash() == first, "timestamps must not change the content hash"

    project.script.source_text = "Now there is a script."
    assert project.content_hash() != first


def test_the_script_text_is_never_rewritten() -> None:
    """Directive section 24: the source text stays exactly as typed."""
    project = build_project("Script")
    original = "First block,   with   spacing.\n\nSecond block.\n\n\nThird."
    project.script.source_text = original

    sections = project.script.rebuild_sections()

    assert project.script.source_text == original
    assert [section.text for section in sections] == [
        "First block,   with   spacing.",
        "Second block.",
        "Third.",
    ]
    assert all(section.id.startswith("sec-") for section in sections)


def test_duration_estimate_uses_the_stored_reading_speed() -> None:
    project = build_project("Estimate")
    project.script.source_text = "one two three four five"
    project.script.words_per_minute = 150

    assert project.script.recompute_estimate() == 2.0
    assert project.estimated_duration_seconds() == 2.0


def test_timestamps_are_utc_and_parseable() -> None:
    stamp = utc_now_iso()
    assert stamp.endswith("Z")
    parsed = parse_iso(stamp)
    assert parsed is not None and parsed.utcoffset().total_seconds() == 0
    assert parse_iso("not a date") is None
    assert parse_iso("") is None


def test_new_ids_are_unique() -> None:
    ids = {new_id("scene") for _ in range(200)}
    assert len(ids) == 200
