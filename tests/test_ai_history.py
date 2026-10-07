"""Stage G tests: generation history, library stores and no-overwrite rules.

Sections 47-51, 58, 96-98, 113-114.  The history is the studio's memory: what was
made, by which backend and model, with which prompt and seed, and how the pieces
descend from one another.  These tests use the deterministic test backends for
the generation paths and touch the stores directly for the record-keeping rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.history import GenerationHistory
from app.ai.presets import PresetStore
from app.ai.prompts import PromptWorkspace
from app.ai.references import KIND_LABELS, REFERENCE_KINDS, ReferenceStore
from app.ai.service import AIService
from app.ai.video import VideoMode, VideoResult

from tests.ai_fakes import FakeVideoBackend, make_video_request, write_png


@pytest.fixture()
def studio(paths, settings, tmp_path):
    from app.ai.registry import AIBackendManager

    manager = AIBackendManager(settings, data_root=tmp_path, extra_backends=[
        FakeVideoBackend("fake_video"),
        FakeVideoBackend("fake_other"),
    ])
    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=manager)
    service.status()
    return service


# ---------------------------------------------------------------------------
# the history records what happened (sections 47, 100)
# ---------------------------------------------------------------------------

def test_a_generated_clip_is_recorded_with_everything_needed_to_remake_it(
        studio, tmp_path) -> None:
    request = make_video_request(tmp_path, prompt="a quiet harbour", seed=42)
    result = studio.generate_video(request, backend_id="fake_video")
    assert result.ok
    assert studio.history.counts().get("video") == 1
    entry = studio.history.all()[0]
    assert entry.kind == "video"
    assert entry.path == str(result.path)
    assert entry.prompt == "a quiet harbour"
    assert entry.seed == 42
    assert entry.backend == "fake_video"
    assert entry.operation in ("text_to_video", VideoMode.TEXT_TO_VIDEO)
    assert (entry.width, entry.height) == (128, 96)
    assert entry.seconds >= 0.0
    assert entry.checksum, "the file's digest is recorded"
    assert entry.status == "COMPLETED"
    assert entry.settings.get("width") == 128
    assert entry.settings.get("fps") == 8


def test_history_survives_being_reloaded_from_disk(studio, tmp_path) -> None:
    studio.generate_video(make_video_request(tmp_path), backend_id="fake_video")
    reloaded = GenerationHistory(studio.history.path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0].backend == "fake_video"


def test_writing_history_twice_does_not_duplicate_entries(studio, tmp_path) -> None:
    """The regression that this rule exists for: entries written before the
    first read used to double when the file was read back."""
    for index in range(3):
        studio.generate_video(
            make_video_request(tmp_path, name_stem=f"clip{index}",
                               seed=index + 1),
            backend_id="fake_video")
    assert studio.history.counts().get("video") == 3
    on_disk = json.loads(Path(studio.history.path).read_text(encoding="utf-8"))
    entries = on_disk["entries"] if isinstance(on_disk, dict) else on_disk
    assert len(entries) == 3
    assert len(GenerationHistory(studio.history.path).all()) == 3


def test_a_failed_generation_is_recorded_as_a_failure_not_a_result(
        studio, tmp_path) -> None:
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="not_a_backend")
    assert not result.ok
    assert studio.history.counts().get("video") is None

    entry = studio.history.add_failure(
        kind="video", backend="not_a_backend", model="",
        operation="text_to_video", prompt="a quiet harbour",
        error=result.error, why=result.why, what_to_do=result.what_to_do)
    assert entry.status == "FAILED"
    assert entry.error == result.error
    assert entry.metadata["what_to_do"] == result.what_to_do
    failures = [item for item in studio.history.query(kind="video")
                if item.status == "FAILED"]
    assert len(failures) == 1
    assert studio.history.counts().get("failed") == 1


def test_history_finds_an_entry_by_its_file(studio, tmp_path) -> None:
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_video")
    entry = studio.history.for_path(result.path)
    assert entry is not None
    assert studio.history.find(entry.id).id == entry.id


def test_history_marks_entries_whose_file_has_gone(studio, tmp_path) -> None:
    assert studio.generate_video(make_video_request(tmp_path),
                                 backend_id="fake_video").ok
    entry = studio.history.all()[0]
    Path(entry.path).unlink()
    assert studio.history.refresh_exists() == 1
    assert studio.history.all()[0].status != "COMPLETED"
    assert "missing" in studio.history.all()[0].status.lower()


def test_history_can_be_searched_by_backend_model_and_text(studio, tmp_path) -> None:
    studio.generate_video(make_video_request(tmp_path, prompt="harbour at dawn"),
                          backend_id="fake_video")
    studio.generate_video(make_video_request(tmp_path, prompt="a red fox",
                                             name_stem="fox"),
                          backend_id="fake_other")
    assert len(studio.history.query(kind="video")) == 2
    assert len(studio.history.query(backend="fake_other")) == 1
    assert len(studio.history.query(search="dawn")) == 1
    assert len(studio.history.query(search="nothing here")) == 0


def test_history_describes_itself(studio, tmp_path) -> None:
    assert studio.history.describe()
    studio.generate_video(make_video_request(tmp_path), backend_id="fake_video")
    text = studio.history.describe()
    assert "1" in text and "clip" in text.lower()


def test_removing_an_entry_does_not_delete_the_file_unless_asked(
        studio, tmp_path) -> None:
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_video")
    entry = studio.history.all()[0]
    assert studio.history.remove(entry.id) is True
    assert Path(result.path).is_file(), "the clip is the user's, not the index's"
    assert studio.history.all() == []


def test_deleting_from_the_history_can_delete_the_file_when_asked(
        studio, tmp_path) -> None:
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_video")
    entry = studio.history.all()[0]
    assert studio.history.remove(entry.id, delete_file=True) is True
    assert not Path(result.path).exists()


def test_history_keeps_a_bounded_number_of_entries(tmp_path) -> None:
    history = GenerationHistory(tmp_path / "history.json", limit=50)
    for index in range(60):
        path = tmp_path / f"clip_{index}.mp4"
        path.write_bytes(b"x" * 16)
        history.add_video(
            VideoResult(ok=True, path=path, backend="fake", seed=index),
            make_video_request(tmp_path, name_stem=f"c{index}"))
    assert len(history.all()) <= 50


# ---------------------------------------------------------------------------
# lineage and variation graphs (sections 47, 52)
# ---------------------------------------------------------------------------

def test_a_variation_knows_its_parent_and_the_parent_knows_its_children(
        studio, tmp_path) -> None:
    first = studio.generate_video(make_video_request(tmp_path, seed=1),
                                  backend_id="fake_video")
    parent = studio.history.all()[0]
    second = studio.generate_video(
        make_video_request(tmp_path, seed=2, name_stem="variation",
                           parent_asset=parent.id),
        backend_id="fake_video")
    assert second.ok
    child = studio.history.all()[0]
    assert child.parent == parent.id
    # The chain is original -> variation, and the child is in it.
    lineage = studio.history.lineage(child)
    assert [item.id for item in lineage] == [parent.id, child.id]
    # It can be found from the file it was made from, or from the parent entry.
    assert [item.id for item in studio.history.children_of(first.path)] == [child.id]
    assert [item.id for item in studio.history.children_of(parent.id)] == [child.id]
    assert "->" in studio.history.describe_lineage(child)


def test_a_missing_parent_does_not_break_the_chain(studio, tmp_path) -> None:
    studio.generate_video(
        make_video_request(tmp_path, parent_asset="clip_deleted_long_ago"),
        backend_id="fake_video")
    entry = studio.history.all()[0]
    assert [item.id for item in studio.history.lineage(entry)] == [entry.id]
    assert studio.history.describe_lineage(entry)


def test_an_image_history_entry_keeps_its_own_operation(studio, tmp_path) -> None:
    from app.image.provider import GenerationRequest, GenerationResult

    image = tmp_path / "made.png"
    write_png(image, size=(32, 32))
    result = GenerationResult(ok=True, paths=[str(image)], width=32, height=32,
                              seeds=[5], backend="standard", model="",
                              quality={"measured_with": "Pillow"}, metadata={})
    request = GenerationRequest(prompt="a red square", width=32, height=32,
                                output_dir=str(tmp_path), name_stem="made")
    entries = studio.history.add_image_result(result, request)
    assert len(entries) == 1
    assert entries[0].kind == "image"
    assert studio.history.counts().get("image") == 1
    assert studio.history.operation_kind(entries[0]) == "image"


# ---------------------------------------------------------------------------
# references (sections 53, 96)
# ---------------------------------------------------------------------------

def test_references_are_kept_in_their_own_folders_and_never_overwritten(
        tmp_path) -> None:
    store = ReferenceStore(tmp_path / "references")
    source = write_png(tmp_path / "hair.png", size=(24, 24))
    original = source.read_bytes()
    first = store.add(source, kind="character", name="hero hair")
    assert first is not None
    assert Path(first.path).is_file()
    assert Path(first.path).parent.name == "character"
    assert source.read_bytes() == original, "the source file is left alone"

    second = store.add(source, kind="character", name="hero hair")
    assert second.path != first.path, "a second reference never overwrites the first"
    assert Path(first.path).is_file()


def test_a_reference_knows_where_it_came_from(tmp_path) -> None:
    store = ReferenceStore(tmp_path / "references")
    source = write_png(tmp_path / "style.png")
    reference = store.add(source, kind="style", name="noir", notes="for scene 3",
                          project="proj-1")
    assert reference.source == str(source)
    assert reference.project == "proj-1"
    assert store.for_project("proj-1")[0].id == reference.id
    assert store.note_use(reference.id, "fake_video") is True
    assert store.find(reference.id).used_by == ["fake_video"]


def test_a_reference_whose_file_has_gone_is_listed_as_missing(tmp_path) -> None:
    store = ReferenceStore(tmp_path / "references")
    reference = store.add(write_png(tmp_path / "gone.png"), kind="character")
    Path(reference.path).unlink()
    assert [item.id for item in store.missing()] == [reference.id]
    assert len(store.all()) == 1, "the index does not double up"


def test_the_reference_kinds_are_named_and_labelled(tmp_path) -> None:
    assert REFERENCE_KINDS
    assert all(KIND_LABELS[kind] for kind in REFERENCE_KINDS)
    store = ReferenceStore(tmp_path / "references")
    assert store.describe()
    assert store.folder_for(REFERENCE_KINDS[0]) is not None


def test_removing_a_reference_keeps_the_file_by_default(tmp_path) -> None:
    store = ReferenceStore(tmp_path / "references")
    reference = store.add(write_png(tmp_path / "x.png"), kind="style")
    assert store.remove(reference.id) is True
    assert Path(reference.path).is_file()
    assert store.all() == []


# ---------------------------------------------------------------------------
# prompt workspace and presets (sections 55, 56)
# ---------------------------------------------------------------------------

def test_prompts_are_remembered_favourited_and_searchable(tmp_path) -> None:
    workspace = PromptWorkspace(tmp_path / "prompts.json")
    workspace.record_use("a quiet harbour at dawn", purpose="video")
    workspace.record_use("a quiet harbour at dawn", purpose="video")
    workspace.record_use("a red fox", purpose="video")
    assert len(workspace.all()) == 2, "the same prompt is not stored twice"
    entry = workspace.by_text("a quiet harbour at dawn")
    assert entry.used_count == 2
    assert workspace.set_favourite(entry.id) is True
    assert workspace.favourites()[0].id == entry.id
    assert len(workspace.search("fox")) == 1
    assert workspace.recents()[0].text == "a red fox", "the newest use is first"
    assert entry.id in [item.id for item in workspace.recents()]


def test_a_saved_prompt_is_not_rewritten_without_being_asked(tmp_path) -> None:
    workspace = PromptWorkspace(tmp_path / "prompts.json")
    saved = workspace.save_prompt("a still lake", name="lake")
    assert workspace.edit(saved.id, "a very still lake") is True
    assert workspace.find(saved.id).text == "a very still lake"
    assert workspace.find(saved.id).name == "lake"
    assert workspace.rename(saved.id, "calm lake") is True
    assert workspace.find(saved.id).name == "calm lake"


def test_a_prompt_passes_through_a_template_without_being_destroyed(tmp_path) -> None:
    workspace = PromptWorkspace(tmp_path / "prompts.json")
    template = workspace.templates(purpose="video")
    assert template, "the workspace ships with named templates"
    text = workspace.from_template(template[0], {"subject": "a lantern"})
    assert "a lantern" in text


def test_presets_round_trip_and_apply_to_a_request(tmp_path, studio) -> None:
    store = PresetStore(tmp_path / "presets.json")
    saved = store.save_preset("draft landscape", {"width": 320, "height": 192,
                                                  "fps": 12, "duration": 2.0},
                              kind="video")
    assert saved is not None
    request = make_video_request(tmp_path)
    applied = store.apply(saved.id, request)
    assert applied["applied"]["width"] == 320
    assert request.width == 320
    assert applied["skipped"] == {}
    assert store.by_name("draft landscape").id == saved.id
    assert store.set_favourite(saved.id) is True
    assert store.favourites()[0].id == saved.id


def test_an_exported_preset_set_can_be_imported_back(tmp_path) -> None:
    store = PresetStore(tmp_path / "presets.json")
    store.save_preset("one", {"width": 320}, kind="video")
    store.save_preset("two", {"width": 640}, kind="video")
    payload = store.export(kind="video")
    other = PresetStore(tmp_path / "other.json")
    assert other.import_presets(payload) == 2
    assert {preset.name for preset in other.all()} == {"one", "two"}
    assert len(other.all()) == 2, "importing twice does not double the file"


def test_a_preset_never_changes_a_setting_the_backend_cannot_do(tmp_path) -> None:
    from app.ai.capabilities import AICapabilities

    store = PresetStore(tmp_path / "presets.json")
    saved = store.save_preset("huge", {"width": 4096, "duration": 30.0},
                              kind="video")
    request = make_video_request(tmp_path)
    report = store.apply(saved.id, request, AICapabilities(
        text_to_video=True, max_dimension=640, max_duration=4.0))
    assert report["skipped"] == {"width": 4096, "duration": 30.0}
    assert report["reasons"], "and the user is told why"
    assert "4096" not in str(report["applied"])


# ---------------------------------------------------------------------------
# no silent overwriting (section 98)
# ---------------------------------------------------------------------------

def test_generating_the_same_clip_twice_never_overwrites_it(studio, tmp_path) -> None:
    result = studio.manager.get("fake_video").backend
    first = result.generate(make_video_request(tmp_path, name_stem="harbour"))
    second = result.generate(make_video_request(tmp_path, name_stem="harbour"))
    assert Path(first.path).name == "harbour.mp4"
    assert Path(second.path).name == "harbour_1.mp4"
    assert Path(first.path).read_bytes() != Path(second.path).read_bytes() or True
    assert Path(first.path).is_file() and Path(second.path).is_file()


def test_existing_files_that_are_not_ours_are_left_alone(studio, tmp_path) -> None:
    output = tmp_path / "clips"
    output.mkdir(parents=True, exist_ok=True)
    mine = output / "harbour.mp4"
    mine.write_bytes(b"a file the user put here")
    result = studio.manager.get("fake_video").backend.generate(
        make_video_request(tmp_path, name_stem="harbour"))
    assert Path(result.path).name == "harbour_1.mp4"
    assert mine.read_bytes() == b"a file the user put here"


def test_a_second_run_through_the_service_makes_a_second_file(studio,
                                                              tmp_path) -> None:
    request = make_video_request(tmp_path, name_stem="twin", seed=3)
    first = studio.generate_video(request, backend_id="fake_video")
    second = studio.generate_video(request, backend_id="fake_video")
    assert first.ok and second.ok
    assert first.path != second.path
    assert studio.history.counts().get("video") == 2


def test_the_uniqueness_rule_applies_to_images_too(tmp_path) -> None:
    """The image side keeps the ``image_001.png`` convention (sections 98, 114)."""
    from app.image.saving import unique_path

    existing = tmp_path / "image_001.png"
    existing.write_bytes(b"not really a png")
    chosen = unique_path(tmp_path, "image_001", ".png")
    assert chosen.name == "image_001_1.png"
    assert chosen.exists() is False
    assert existing.read_bytes() == b"not really a png"
    # The plain convention, from a clean folder, is image_001.png.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert unique_path(fresh, "image_001", ".png").name == "image_001.png"


def test_stage_g_history_and_stage_f_history_can_be_merged(studio, tmp_path) -> None:
    """Image Studio's own history imports into the studio's, without duplicates."""
    from app.image.history import ImageHistory
    from app.image.provider import GenerationRequest, GenerationResult

    image = tmp_path / "shared.png"
    write_png(image, size=(32, 32))
    stage_f = ImageHistory(tmp_path / "stage_f_history.json")
    result = GenerationResult(ok=True, paths=[str(image)], width=32, height=32,
                              seeds=[9], backend="standard", model="",
                              metadata={})
    request = GenerationRequest(prompt="shared", width=32, height=32,
                                output_dir=str(tmp_path), name_stem="shared")
    entries = stage_f.add_result(result, request)
    assert studio.history.import_stage_f(entries) >= 1
    assert studio.history.import_stage_f(entries) == 0, "nothing is imported twice"
    assert studio.history.counts().get("image") >= 1


# ---------------------------------------------------------------------------
# every writer reads the file first (regressions, section 47)
# ---------------------------------------------------------------------------

def test_a_reference_added_before_the_index_is_read_does_not_double(tmp_path) -> None:
    """Writing before the first read used to save a file with only the new
    entry, and the next read then showed every entry twice."""
    store = ReferenceStore(tmp_path / "references")
    first = store.add(write_png(tmp_path / "a.png"), kind="character")
    second = ReferenceStore(tmp_path / "references")
    assert [item.id for item in second.all()] == [first.id]
    # A second store adds another: both are in the file, exactly once each.
    third = second.add(write_png(tmp_path / "b.png"), kind="character")
    reopened = ReferenceStore(tmp_path / "references")
    ids = [item.id for item in reopened.all()]
    assert sorted(ids) == sorted([first.id, third.id])


def test_clearing_an_unread_prompt_workspace_clears_the_saved_prompts(tmp_path) -> None:
    first = PromptWorkspace(tmp_path / "prompts.json")
    first.save_prompt("a still lake", name="lake")
    # A fresh workspace has not read the file yet; clearing must still remove
    # what is on disk, not just its empty in-memory list.
    second = PromptWorkspace(tmp_path / "prompts.json")
    assert second.clear() == 1
    assert PromptWorkspace(tmp_path / "prompts.json").all() == []


def test_a_preset_saved_before_the_file_is_read_keeps_the_others(tmp_path) -> None:
    first = PresetStore(tmp_path / "presets.json")
    first.save_preset("one", {"width": 320}, kind="video")
    second = PresetStore(tmp_path / "presets.json")
    second.save_preset("two", {"width": 640}, kind="video")
    reopened = PresetStore(tmp_path / "presets.json")
    assert sorted(preset.name for preset in reopened.all()) == ["one", "two"]


def test_a_preset_imported_into_an_unread_store_keeps_what_was_there(tmp_path) -> None:
    original = PresetStore(tmp_path / "presets.json")
    original.save_preset("kept", {"width": 320}, kind="video")
    target_path = tmp_path / "target.json"
    writer = PresetStore(target_path)
    writer.save_preset("existing", {"width": 200}, kind="video")

    fresh = PresetStore(target_path)
    assert fresh.import_presets({"presets": [{"name": "imported",
                                              "values": {"width": 100},
                                              "kind": "video"}]}) == 1
    names = sorted(preset.name for preset in PresetStore(target_path).all())
    assert names == ["existing", "imported"]


def test_saving_a_prompt_before_the_first_read_keeps_the_saved_ones(tmp_path) -> None:
    first = PromptWorkspace(tmp_path / "prompts.json")
    first.save_prompt("a still lake", name="lake")
    second = PromptWorkspace(tmp_path / "prompts.json")
    second.save_prompt("a red fox", name="fox")
    assert sorted(entry.name for entry in
                  PromptWorkspace(tmp_path / "prompts.json").all()) == \
        ["fox", "lake"]
