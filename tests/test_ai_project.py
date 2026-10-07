"""Stage G tests: clips become project assets - no AI island.

Sections 28-30, 44, 47, 52, 56, 61, 100-101.  A generated clip has to behave like
any other piece of media: it is copied into the project as an asset, it can be
placed on the timeline, it can play in a scene, it survives a save/load, and its
provenance is still there afterwards.  Generated with the labelled test backend
and the real :class:`ProjectService`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.integration import (PLACEMENTS, SendReport, clip_metadata,
                                describe_send, make_video_scene, send_to_project,
                                send_to_scene, send_to_timeline)
from app.ai.service import AIService
from app.core.settings import Settings
from app.project.service import CreateRequest, ProjectService

from tests.ai_fakes import (FakeVideoBackend, ffmpeg_tools,
                            make_video_request, probe_video)


@pytest.fixture()
def studio(paths, settings, tmp_path):
    from app.ai.registry import AIBackendManager

    manager = AIBackendManager(settings, data_root=tmp_path, extra_backends=[
        FakeVideoBackend("fake_video"),
    ])
    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=manager)
    service.status()
    return service


@pytest.fixture()
def project(paths, tmp_path):
    service = ProjectService(paths, Settings())
    request = CreateRequest(name="AI Studio Test", width=640, height=360,
                            fps=24, folder=str(tmp_path / "projects"))
    project = service.create_project(request, open_after=True)
    assert service.is_open
    return service, project


def asset_copy(service, asset) -> Path:
    """Where an asset's file really is, through the project's own layout."""
    resolved = asset.resolve(service.current_layout.project_file.parent)
    return Path(resolved)


def clip(studio, tmp_path, **overrides):
    request = make_video_request(tmp_path, width=160, height=96, fps=12,
                                 duration=1.0, **overrides)
    result = studio.generate_video(request, backend_id="fake_video")
    assert result.ok, result.error
    return result


# ---------------------------------------------------------------------------
# artifacts and metadata (sections 44, 100)
# ---------------------------------------------------------------------------

def test_clip_metadata_describes_the_file_it_came_from(tmp_path, studio) -> None:
    result = clip(studio, tmp_path, prompt="a harbour at dawn", seed=21)
    meta = clip_metadata(result.path, extra={"prompt": "a harbour at dawn",
                                             "seed": 21})
    from tests.ai_fakes import ffmpeg_tools

    meta = clip_metadata(result.path, extra={"prompt": "a harbour at dawn",
                                             "seed": 21},
                         tools=ffmpeg_tools())
    assert meta["measured"] is True
    assert meta["duration"] == pytest.approx(1.0, abs=0.15)
    assert meta["fps"] == pytest.approx(12.0, abs=1.0)
    assert meta["width"] == 160 and meta["height"] == 96
    assert meta["video_codec"] == "h264"
    assert meta["prompt"] == "a harbour at dawn"
    assert meta["seed"] == 21
    assert meta["size_bytes"] > 0


def test_clip_metadata_says_so_when_it_cannot_measure_the_file(tmp_path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    meta = clip_metadata(broken, extra={"prompt": "x"})
    assert meta["prompt"] == "x", "the caller's own notes are kept"
    assert meta["measured"] is False
    assert "could not be read" in meta["note"]
    assert meta["size_bytes"] == len(b"not a video")


# ---------------------------------------------------------------------------
# sending a clip into a project (sections 47, 61)
# ---------------------------------------------------------------------------

def test_a_clip_becomes_an_ordinary_project_asset(studio, project, tmp_path) -> None:
    service, current = project
    result = clip(studio, tmp_path, prompt="a harbour")
    report = send_to_project(service, result.path, name="harbour")
    assert report.ok is True
    assert report.asset_id
    asset = next(item for item in service.current.assets
                 if str(item.id) == report.asset_id)
    assert asset.kind == "video"
    assert asset_copy(service, asset).is_file()
    assert "added" in report.message


def test_sending_a_clip_with_no_project_open_says_so(studio, paths, tmp_path) -> None:
    service = ProjectService(paths, Settings())
    result = clip(studio, tmp_path)
    report = send_to_project(service, result.path, name="harbour")
    assert report.ok is False
    assert "no project is open" in report.message.lower()
    assert report.what_to_do


def test_sending_a_file_that_is_not_there_says_so(project, tmp_path) -> None:
    service, _ = project
    report = send_to_project(service, tmp_path / "gone.mp4", name="gone")
    assert report.ok is False
    assert "not there" in report.message
    assert report.what_to_do


def test_the_asset_carries_the_generation_metadata(studio, project, tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path, prompt="a harbour", seed=4)
    metadata = clip_metadata(result.path, extra={"prompt": "a harbour", "seed": 4,
                                                 "backend": "fake_video"})
    report = send_to_project(service, result.path, name="harbour",
                             metadata=metadata)
    assert report.ok
    asset = next(item for item in service.current.assets
                 if str(item.id) == report.asset_id)
    stored = getattr(asset, "metadata", None) or getattr(asset, "extra", {}) or {}
    assert stored.get("prompt") == "a harbour"
    assert stored.get("seed") == 4


def test_sending_the_same_clip_twice_keeps_both_files(studio, project,
                                                      tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path, name_stem="harbour")
    first = send_to_project(service, result.path, name="harbour")
    second = send_to_project(service, result.path, name="harbour")
    assert first.ok and second.ok
    paths = {asset_copy(service, asset) for asset in service.current.assets}
    assert len(paths) == 2, "the second import never overwrote the first"
    for path in paths:
        assert path.is_file()


# ---------------------------------------------------------------------------
# scenes and the timeline (sections 28-30)
# ---------------------------------------------------------------------------

def test_send_to_scene_makes_a_scene_that_plays_the_clip(studio, project,
                                                          tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path)
    metadata = clip_metadata(result.path, tools=ffmpeg_tools())
    report = send_to_scene(service, result.path, name="Clip scene",
                           metadata=metadata, duration=metadata["duration"],
                           fps=metadata["fps"])
    assert report.ok is True, report.message
    scene = next(item for item in service.current.scenes
                 if str(item.id) == report.scene_id)
    assert scene.type == "video"
    assert scene.extra["video"]["asset_id"] == report.asset_id
    assert scene.extra["video"]["fit"] == "cover"
    assert scene.effective_duration == pytest.approx(1.0, abs=0.2)
    assert "position 1" in report.message


def test_send_to_scene_updates_an_existing_scene(studio, project, tmp_path) -> None:
    service, current = project
    first = clip(studio, tmp_path, name_stem="one")
    second = clip(studio, tmp_path, name_stem="two", seed=9)
    scene = service.add_scene(scene_type="title", name="Existing")
    report = send_to_scene(service, first.path, scene_id=str(scene.id),
                           metadata=clip_metadata(first.path))
    assert report.ok
    again = send_to_scene(service, second.path, scene_id=str(scene.id),
                          metadata=clip_metadata(second.path))
    assert again.ok
    target = next(item for item in service.current.scenes
                  if str(item.id) == str(scene.id))
    assert target.extra["video"]["asset_id"] == again.asset_id != report.asset_id
    assert len(service.current.scenes) == 1, "no second scene was created"


def test_send_to_scene_with_an_unknown_scene_is_refused(studio, project,
                                                        tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path)
    report = send_to_scene(service, result.path, scene_id="scene_not_here",
                           metadata=clip_metadata(result.path))
    assert report.ok is False
    assert "was not found" in report.message
    assert report.asset_id, "the file still became an asset, and it says so"


def test_send_to_timeline_places_the_clip_where_it_was_asked(studio, project,
                                                              tmp_path) -> None:
    service, _ = project
    service.add_scene(scene_type="title", name="First")
    service.add_scene(scene_type="title", name="Second")
    result = clip(studio, tmp_path)
    at_start = send_to_timeline(service, result.path, name="Middle",
                                placement="start",
                                metadata=clip_metadata(result.path))
    assert at_start.ok
    assert at_start.position == 0
    assert "position 1" in at_start.message
    order = [scene.name for scene in service.current.scenes]
    assert order[0] == "Middle"

    at_end = send_to_timeline(service, result.path, name="Last", placement="end",
                              metadata=clip_metadata(result.path))
    assert at_end.ok
    assert [scene.name for scene in service.current.scenes][-1] == "Last"

    before = send_to_timeline(service, result.path, name="BeforeSecond",
                              placement="before", relative_to=str(
                                  service.current.scenes[-1].id),
                              metadata=clip_metadata(result.path))
    assert before.ok
    names = [scene.name for scene in service.current.scenes]
    assert names.index("BeforeSecond") == names.index("Last") - 1


def test_an_unknown_placement_is_refused_with_the_list_of_real_ones(studio,
                                                                    project,
                                                                    tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path)
    report = send_to_timeline(service, result.path, placement="sideways",
                              metadata=clip_metadata(result.path))
    assert report.ok is False
    assert "sideways" in report.message
    assert len(PLACEMENTS) >= 3
    for label in ("At the end", "At the start", "After a scene", "Before a scene"):
        assert label in report.what_to_do


def test_a_clip_placed_in_a_scene_really_plays_in_the_render(studio, project,
                                                             tmp_path) -> None:
    """The final check that there is no AI island: the render uses the clip."""
    service, _ = project
    result = clip(studio, tmp_path, prompt="a harbour", seed=3)
    metadata = clip_metadata(result.path)
    report = send_to_timeline(service, result.path, name="Harbour",
                              metadata=metadata, duration=metadata["duration"],
                              fps=metadata["fps"])
    assert report.ok

    from app.render.service import RenderService
    from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

    render = RenderService(FFmpegTools(discover_ffmpeg()),
                           project_dir=service.current_layout.project_file.parent,
                           paths=service.paths)
    report_render = render.render(service.current, include_audio=False,
                                  include_subtitles=False,
                                  overrides={"width": 640, "height": 360,
                                             "fps": 24, "quality": "draft"})
    assert report_render.status == "COMPLETED", report_render.message
    output = Path(report_render.path)
    assert output.is_file()
    measured = probe_video(output)
    assert measured["width"] == 640 and measured["height"] == 360
    assert measured["duration"] > 0.5


def test_make_video_scene_builds_a_scene_the_model_accepts(tmp_path) -> None:
    from tests.ai_fakes import _tiny_mp4

    path = _tiny_mp4(tmp_path / "clip.mp4", width=64, height=48, fps=8,
                     seconds=1.0)
    spec = make_video_scene(clip_metadata(path, tools=ffmpeg_tools()))
    assert spec.type == "video"
    assert spec.extra["video"]["fit"] in ("cover", "contain", "stretch")
    assert spec.duration == pytest.approx(1.0, abs=0.2)
    assert spec.enabled is True
    # A scene built from a file we could not measure still has a usable shape.
    empty = make_video_scene({})
    assert empty.extra["video"]["fit"] == "cover"
    assert empty.duration == 0.0


def test_a_clip_with_no_readable_length_still_goes_in_and_says_so(studio,
                                                                  project,
                                                                  tmp_path) -> None:
    service, _ = project
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not really a video")
    report = send_to_scene(service, broken, name="Broken",
                           metadata=clip_metadata(broken))
    assert report.ok is True, "the file is still added; the length is the issue"
    assert report.notes, "the user is told the length could not be read"


def test_describe_send_line_tells_the_user_what_happened(project, tmp_path) -> None:
    service, _ = project
    report = SendReport(ok=True, action="send_to_timeline",
                        message="harbour.mp4 was added to the timeline.",
                        path=str(tmp_path / "harbour.mp4"), position=2)
    text = describe_send(report)
    assert "harbour.mp4" in text
    assert "timeline" in text.lower()
    payload = report.to_dict()
    assert payload["ok"] is True and payload["action"] == "send_to_timeline"
    assert "what_to_do" in payload


# ---------------------------------------------------------------------------
# provenance in the project file (sections 52, 56, 100)
# ---------------------------------------------------------------------------

def test_the_project_file_remembers_where_a_clip_came_from(studio, project,
                                                           tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path, prompt="a harbour at dawn", seed=77)
    metadata = clip_metadata(result.path, extra={
        "prompt": "a harbour at dawn", "seed": 77, "backend": "fake_video",
        "model": "fake-model", "operation": "text_to_video",
        "is_ai_model": True, "label": "TEST BACKEND (fixture - not an AI model)"})
    report = send_to_timeline(service, result.path, name="Harbour",
                              metadata=metadata, duration=metadata["duration"],
                              fps=metadata["fps"])
    assert report.ok
    assert service.save().ok
    raw = json.loads(service.current_layout.project_file.read_text(
        encoding="utf-8"))
    text = json.dumps(raw)
    assert "a harbour at dawn" in text
    assert "fake_video" in text
    assert "TEST BACKEND" in text, \
        "the project must not describe a test backend as an AI model"


def test_a_saved_project_reopens_with_the_clip_still_playing(studio, project,
                                                             tmp_path) -> None:
    service, current = project
    result = clip(studio, tmp_path, name_stem="kept")
    metadata = clip_metadata(result.path)
    report = send_to_timeline(service, result.path, name="Kept",
                              metadata=metadata, duration=metadata["duration"],
                              fps=metadata["fps"])
    assert report.ok
    assert service.save().ok
    project_path = service.current_layout.project_file

    other = ProjectService(service.paths, Settings())
    service.close_project()
    reopened = other.open_project(project_path)
    assert other.is_open
    scene = next(item for item in reopened.scenes if item.name == "Kept")
    assert scene.type == "video"
    asset_id = scene.extra["video"]["asset_id"]
    asset = next(item for item in reopened.assets if str(item.id) == asset_id)
    assert asset_copy(other, asset).is_file()
    assert probe_video(asset_copy(other, asset))["width"] == 160


def test_a_missing_clip_is_reported_by_validation_not_ignored(studio, project,
                                                              tmp_path) -> None:
    service, _ = project
    result = clip(studio, tmp_path, name_stem="vanishing")
    metadata = clip_metadata(result.path)
    report = send_to_timeline(service, result.path, name="Vanishing",
                              metadata=metadata, duration=metadata["duration"],
                              fps=metadata["fps"])
    assert report.ok
    asset = next(item for item in service.current.assets
                 if str(item.id) == report.asset_id)
    asset_copy(service, asset).unlink()
    report = service.validate(check_files=True)
    text = " ".join(str(getattr(item, "message", item))
                    for item in list(report.errors) + list(report.warnings))
    assert report.errors or report.warnings, "a vanished clip is not silence"
    assert "missing" in text.lower() or "not found" in text.lower()


# ---------------------------------------------------------------------------
# storyboard plans and scenes (sections 30, 49)
# ---------------------------------------------------------------------------

def test_a_storyboard_plan_uses_each_scene_s_own_words(studio, project,
                                                       tmp_path) -> None:
    service, current = project
    first = service.add_scene(scene_type="title", name="Harbour")
    service.update_scene_field(str(first.id), script="a harbour at dawn")
    second = service.add_scene(scene_type="title", name="Forest")
    service.update_scene_field(str(second.id), script="a forest in the rain")
    plan = studio.storyboard_plan(service.current, backend_id="fake_video",
                                  width=160, height=96, fps=12,
                                  project_dir=service.current_layout.project_file.parent)
    assert [item["prompt"] for item in plan] == ["a harbour at dawn",
                                                 "a forest in the rain"]
    assert [item["scene_id"] for item in plan] == \
        [str(scene.id) for scene in service.current.scenes]
    assert all(item["approved"] is False for item in plan)
    assert len({item["seed"] for item in plan}) == 2, "each scene gets its own seed"


def test_a_storyboard_plan_falls_back_to_the_scene_name(studio, project) -> None:
    """A scene with no script still has a name, and the plan says so."""
    service, _ = project
    named = service.add_scene(scene_type="title", name="Harbour")
    plan = studio.storyboard_plan(service.current, backend_id="fake_video")
    assert len(plan) == 1
    assert plan[0]["prompt"] == "Harbour"
    assert plan[0]["scene_id"] == str(named.id)


def test_an_unapproved_plan_cannot_be_generated(studio, project, tmp_path) -> None:
    from app.ai.jobs import video_storyboard_body
    from app.jobs.spec import JobContext

    service, _ = project
    scene = service.add_scene(scene_type="title", name="Harbour")
    service.update_scene_field(str(scene.id), script="a harbour")
    plan = studio.storyboard_plan(service.current, backend_id="fake_video",
                                  project_dir=service.current_layout.project_file.parent)
    payload = {"plan": plan, "service": studio, "backend_id": "fake_video",
               "paths": studio.paths, "settings": studio.settings}

    class _Never:
        def is_cancelled(self) -> bool:
            return False

        def raise_if_cancelled(self) -> None:
            return None

    class _Progress:
        total = 0.0

        def start(self, **kwargs) -> None:
            pass

        def update(self, **kwargs) -> None:
            pass

    context = JobContext(job_id="plan_job", key="ai.video.batch", cancel=_Never(),
                         progress=_Progress(), settings=studio.settings,
                         paths=studio.paths, payload=payload)
    outcome = video_storyboard_body(context)
    assert outcome["ok"] is False
    assert "approv" in outcome["message"].lower()
    assert outcome["what_to_do"]


def test_the_plan_carries_the_scene_id_into_the_result(studio, project,
                                                        tmp_path) -> None:
    """Every clip made from a plan knows which scene it belongs to (section 30)."""
    service, _ = project
    scene = service.add_scene(scene_type="title", name="Harbour")
    service.update_scene_field(str(scene.id), script="a harbour")
    plan = studio.storyboard_plan(service.current, backend_id="fake_video",
                                  width=160, height=96, fps=12,
                                  project_dir=service.current_layout.project_file.parent)
    studio.approve_plan(plan)
    requests = studio.requests_from_plan(plan)
    assert len(requests) == 1
    request = requests[0]
    assert request.scene_id == plan[0]["scene_id"]
    assert request.prompt == "a harbour"
    assert request.seed == plan[0]["seed"]
    assert request.width == 160 and request.height == 96
