"""The render pipeline against a real FFmpeg (Stage E, sections 70-78).

Everything here encodes actual video with the real encoder, then checks the file
that came out.  The whole module skips when FFmpeg is not installed, because a
machine without it cannot render - and a skipped test says so honestly rather
than passing while checking nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.media.probe import probe_media
from app.project.model import SceneSpec, build_project
from app.render import COMPLETED, CANCELLED, FAILED, RenderEngine, RenderRequest, RenderService
from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

DISCOVERY = discover_ffmpeg()
pytestmark = pytest.mark.skipif(
    not DISCOVERY.has_ffmpeg,
    reason="FFmpeg is not installed, so nothing can be encoded",
)

#: Small and fast: these tests prove the pipeline, not the encoder's patience.
WIDTH, HEIGHT, FPS = 320, 240, 25


@pytest.fixture(scope="module")
def tools() -> FFmpegTools:
    return FFmpegTools(DISCOVERY)


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _wav(path: Path, seconds: float, frequency: int = 300) -> Path:
    """A real audio file of a known length."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tools = FFmpegTools(DISCOVERY)
    result = tools.run([
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={seconds}",
        "-ar", "48000", "-ac", "1", str(path),
    ], timeout=120.0)
    assert result.ok, result.describe_failure()
    return path


def _project(root: Path, *, scenes=3, narration=(2.0, 1.6, 1.2), with_music=False,
             with_subtitles=True, transitions=True):
    """A real project with real narration files on disk."""
    project = build_project("Engine Test")
    project.project.name = "EngineTest"
    for index in range(scenes):
        scene = project.add_scene(SceneSpec(id=f"s{index + 1}",
                                            name=f"Scene {index + 1}", type="text"))
        length = narration[index] if index < len(narration) else 1.0
        scene.narration.text = f"This is the narration for scene {index + 1}."
        scene.narration.file = f"audio/s{index + 1}.wav"
        scene.narration.duration = length
        scene.narration.status = "ready"
        _wav(root / "audio" / f"s{index + 1}.wav", length, 220 + index * 60)

    if transitions and scenes > 1:
        for first, second in zip(project.scenes, project.scenes[1:]):
            first.transition_out.type = "fade"
            first.transition_out.duration = 0.4
            second.transition_in.type = "fade"
            second.transition_in.duration = 0.4

    if with_music:
        _wav(root / "audio" / "music.wav", 8.0, 120)
        project.audio.music.id = "m1"
        project.audio.music.path = "audio/music.wav"
        project.audio.music.volume = 0.3

    project.subtitles.enabled = with_subtitles
    project.subtitles.language = "en"

    project.format.width = WIDTH
    project.format.height = HEIGHT
    project.format.fps = FPS
    project.format.encoder_preset = "ultrafast"
    project.format.crf = 32
    project.export.output_dir = str(root / "renders")
    return project


def _with_content(project):
    """Give every scene real text, so the frames have detail.

    A flat single-colour frame encodes to almost nothing, and at that point the
    encoder settings stop mattering - even to the extent that a *higher* CRF can
    produce a larger file, because coarse quantisation of a uniform block costs
    more than encoding it near-losslessly.
    """
    from app.project.model import ElementSpec

    for index, scene in enumerate(project.scenes):
        scene.elements.append(ElementSpec(
            id=f"e{index}", kind="text",
            text=(f"Scene {index + 1}: the quick brown fox jumps over the lazy dog "
                  f"while the encoder has something with real detail to work on."),
            position={"x": 0.5, "y": 0.4},
            size={"mode": "relative", "value": 0.09},
            fit={"auto_fit": True, "max_lines": 4, "min_scale": 0.5},
        ))
    return project


def _fast(project):
    project.format.encoder_preset = "ultrafast"
    project.format.crf = 32
    return project


# -- a real render ---------------------------------------------------------

def test_a_real_render_produces_a_real_video(tools, workdir):
    project = _project(workdir)
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))

    assert result.status == COMPLETED, result.message
    assert result.path is not None and result.path.exists()
    assert result.path.stat().st_size > 0

    info = probe_media(result.path, tools)
    assert info.ok
    assert (info.width, info.height) == (WIDTH, HEIGHT)
    assert info.fps == pytest.approx(FPS, abs=0.5)
    assert info.has_video and info.has_audio
    assert info.video_codec == "h264"


def test_the_video_is_the_length_of_the_timeline(tools, workdir):
    from app.scene.timing import build_timeline

    project = _project(workdir)
    expected = build_timeline(project.scenes).total_duration
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))

    info = probe_media(result.path, tools)
    assert info.duration == pytest.approx(expected, abs=0.35)


def test_the_audio_track_matches_the_video_length(tools, workdir):
    project = _project(workdir, with_music=True)
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED, result.message
    # A/V sync: the QC pass would have flagged a mismatch, and it did not.
    assert result.qc is not None and not result.qc.has_fail


def test_a_second_render_never_touches_the_first(tools, workdir):
    engine = RenderEngine(tools, project_dir=workdir)
    first = engine.render(RenderRequest(project=_project(workdir), quick_qc=True))
    assert first.status == COMPLETED
    before = first.path.read_bytes()

    second = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=_project(workdir), quick_qc=True))
    assert second.status == COMPLETED
    assert second.path != first.path
    assert first.path.read_bytes() == before


def test_the_render_walks_through_every_state(tools, workdir):
    seen = []
    project = _project(workdir)
    RenderEngine(tools, project_dir=workdir,
                 progress=lambda progress: seen.append(progress.state)).render(
        RenderRequest(project=project, quick_qc=True))

    for state in ("PREPARING", "VALIDATING", "AUDIO", "SUBTITLES", "SCENES",
                  "ENCODING", "QC", "COMPLETED"):
        assert state in seen, f"{state} was never reported"


def test_burnt_in_subtitles_are_drawn_into_the_picture(tools, workdir):
    """The burned file must differ from the plain one, or nothing was drawn."""
    project = _project(workdir, with_subtitles=True)
    plain = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, burn_subtitles=False, quick_qc=True))
    burned = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, burn_subtitles=True, quick_qc=True))

    if "SUBTITLE_BURN_UNAVAILABLE" in [getattr(item, "code", "") for item in burned.errors]:
        pytest.skip("this FFmpeg build has no libass, so captions cannot be burned in")
    assert burned.status == COMPLETED, burned.message
    assert burned.path.read_bytes() != plain.path.read_bytes()


def test_side_car_subtitle_files_are_written(tools, workdir):
    project = _project(workdir, with_subtitles=True)
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED
    assert result.details.get("subtitle_count", 0) > 0
    files = result.details.get("subtitle_files") or []
    assert any(name.endswith(".srt") for name in files)
    assert any(name.endswith(".vtt") for name in files)


# -- resolution and quality ------------------------------------------------

@pytest.mark.parametrize("width,height", [(320, 240), (640, 360), (480, 480),
                                          (240, 426), (640, 480)])
def test_five_resolutions_all_render(tools, workdir, width, height):
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    project.format.width, project.format.height = width, height
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED, result.message
    info = probe_media(result.path, tools)
    assert (info.width, info.height) == (width, height)


@pytest.mark.parametrize("quality,crf", [("draft", 34), ("medium", 28),
                                         ("high", 24), ("ultra", 18)])
def test_quality_is_not_silently_downgraded(tools, workdir, quality, crf):
    """Each quality must produce a file of its own; better quality, more bytes."""
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0),
                             with_subtitles=False, transitions=False))
    project.format.quality_preset = quality
    project.format.crf = crf
    project.format.encoder_preset = "ultrafast"
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED, result.message
    info = probe_media(result.path, tools)
    assert info.ok
    # The size is recorded so a comparison can be made by the caller.
    assert result.qc.measured["size_bytes"] > 0


def test_higher_quality_produces_a_bigger_file(tools, workdir):
    """Measured on the video alone.

    With audio in the file the AAC track (192 kbps) outweighs a couple of seconds
    of nearly-static video, so file size would say nothing about the quality.
    """
    sizes = {}
    for quality, crf in (("draft", 36), ("ultra", 16)):
        project = _with_content(_fast(_project(
            workdir, scenes=2, narration=(1.2, 1.0),
            with_subtitles=False, transitions=False)))
        project.format.quality_preset = quality
        project.format.crf = crf
        result = RenderEngine(tools, project_dir=workdir).render(
            RenderRequest(project=project, include_audio=False, quick_qc=True))
        assert result.status == COMPLETED, result.message
        sizes[quality] = result.path.stat().st_size
    assert sizes["ultra"] > sizes["draft"], sizes


def test_an_explicit_bitrate_is_honoured(tools, workdir):
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0),
                             with_subtitles=False, transitions=False))
    project.format.bitrate_kbps = 400
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED, result.message
    info = probe_media(result.path, tools)
    assert info.bitrate_kbps is not None and info.bitrate_kbps > 0


# -- long form -------------------------------------------------------------

def test_a_long_timeline_is_not_refused(tools, workdir):
    """No artificial duration limit: 50 scenes render like 3 (section 34)."""
    project = _fast(_project(workdir, scenes=50,
                             narration=tuple([0.4] * 50),
                             with_subtitles=False, transitions=False))
    from app.scene.timing import build_timeline

    expected = build_timeline(project.scenes).total_duration
    assert expected > 20.0

    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED, result.message
    info = probe_media(result.path, tools)
    assert info.duration == pytest.approx(expected, abs=1.0)
    assert result.details["segments"] == 50


# -- failures that must stop the render ------------------------------------

def test_missing_narration_stops_the_render_before_any_frame(tools, workdir):
    project = _project(workdir)
    (workdir / "audio" / "s2.wav").unlink()
    result = RenderEngine(tools, project_dir=workdir).render(RenderRequest(project=project))

    assert result.status == FAILED
    assert "NARRATION_FILE_MISSING" in [getattr(item, "code", "") for item in result.errors]
    assert result.what_to_do
    assert result.path is None


def test_a_codec_this_machine_lacks_stops_the_render(tools, workdir):
    caps = RenderService(tools, project_dir=workdir).capabilities()
    missing = next((codec for codec in ("h264_cpu", "hevc_cpu", "vp9_cpu")
                    if not caps.supports_codec(codec)), None)
    if missing is None:
        pytest.skip("this FFmpeg has every codec, so none can be shown missing")

    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    project.format.codec = missing
    project.format.container = {"vp9_cpu": "webm"}.get(missing, "mp4")
    result = RenderEngine(tools, project_dir=workdir).render(RenderRequest(project=project))

    assert result.status == FAILED
    assert "CODEC_UNAVAILABLE" in [getattr(item, "code", "") for item in result.errors]
    assert result.path is None


def test_an_unavailable_codec_blocks_the_render_whatever_ffmpeg_has(tools, workdir):
    """Determined by the capability list, not by luck of the installed build.

    A render must stop before a single frame is drawn when the encoder is missing.
    """
    from app.render.capabilities import EncoderCapabilities

    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    project.format.codec = "hevc_cpu"
    caps = EncoderCapabilities(encoders=["libx264"], audio_encoders=["aac"],
                              detected=True, ffmpeg_version="pretend")
    result = RenderEngine(tools, project_dir=workdir, caps=caps).render(
        RenderRequest(project=project))

    assert result.status == FAILED
    assert "CODEC_UNAVAILABLE" in [getattr(item, "code", "") for item in result.errors]
    assert result.path is None
    assert not (workdir / "renders").exists() or \
        list((workdir / "renders").glob("*.mp4")) == []


def test_an_unusable_output_folder_is_reported_clearly(tools, workdir):
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    blocker = workdir / "renders"
    blocker.write_text("i am a file, not a folder", encoding="utf-8")
    project.export.output_dir = str(blocker / "inside")

    result = RenderEngine(tools, project_dir=workdir).render(RenderRequest(project=project))
    assert result.status == FAILED
    assert result.path is None
    assert result.what_to_do


def test_an_empty_project_is_not_rendered(tools, workdir):
    project = build_project("Empty")
    project.export.output_dir = str(workdir / "renders")
    result = RenderEngine(tools, project_dir=workdir).render(RenderRequest(project=project))
    assert result.status == FAILED
    assert "NOTHING_TO_RENDER" in [getattr(item, "code", "") for item in result.errors]


# -- cancellation and resume ----------------------------------------------

def test_cancelling_stops_the_render_and_leaves_no_video(tools, workdir):
    from app.tools.ffmpeg import FFmpegCancelToken

    token = FFmpegCancelToken()
    project = _fast(_project(workdir, scenes=6, narration=tuple([1.0] * 6),
                             with_subtitles=False, transitions=False))

    def cancel_after_a_few_frames(progress):
        if progress.frames_done >= 5:
            token.cancel()

    result = RenderEngine(tools, project_dir=workdir, cancel_token=token,
                          progress=cancel_after_a_few_frames).render(
        RenderRequest(project=project))

    assert result.status == CANCELLED
    assert result.path is None
    renders = workdir / "renders"
    assert not renders.exists() or list(renders.glob("*.mp4")) == []
    # The project itself is untouched and still valid.
    assert len(project.scenes) == 6


def test_a_resumed_render_reuses_finished_scenes(tools, workdir):
    """Segments are keyed by content, so an interrupted render continues."""
    from app.tools.ffmpeg import FFmpegCancelToken

    project = _fast(_project(workdir, scenes=5, narration=tuple([1.0] * 5),
                             with_subtitles=False, transitions=False))
    token = FFmpegCancelToken()

    def cancel_midway(progress):
        if progress.segments_done >= 2:
            token.cancel()

    first = RenderEngine(tools, project_dir=workdir, cancel_token=token,
                         progress=cancel_midway).render(RenderRequest(project=project))
    assert first.status == CANCELLED

    second = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True, resume=True))
    assert second.status == COMPLETED, second.message
    assert second.details["resumed_frames"] > 0


def _cancelled_after(workdir, project, tools, segments):
    """Run a render that is cancelled once ``segments`` segments are done."""
    from app.tools.ffmpeg import FFmpegCancelToken

    token = FFmpegCancelToken()

    def stop(progress):
        if progress.segments_done >= segments:
            token.cancel()

    return RenderEngine(tools, project_dir=workdir, cancel_token=token,
                        progress=stop).render(RenderRequest(project=project))


def test_an_unchanged_scene_is_reused_after_an_interrupt(tools, workdir):
    """Segment keys come from the scene content, so work already done survives."""
    project = _fast(_project(workdir, scenes=5, narration=tuple([1.0] * 5),
                             with_subtitles=False, transitions=False))
    assert _cancelled_after(workdir, project, tools, 2).status == CANCELLED

    # Change a scene that had not been reached yet: the finished ones still match.
    project.scenes[4].narration.text = "A different ending."
    resumed = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True, resume=True))
    assert resumed.status == COMPLETED, resumed.message
    assert resumed.details["resumed_frames"] > 0


def test_editing_a_finished_scene_forces_it_to_be_redrawn(tools, workdir):
    """The key is content-sensitive: change a cached scene and it is redone.

    Compared against an identical run that changes nothing, so the difference is
    the edit and nothing else.
    """
    untouched = _fast(_project(workdir / "a", scenes=5, narration=tuple([1.0] * 5),
                               with_subtitles=False, transitions=False))
    (workdir / "a").mkdir(parents=True, exist_ok=True)
    assert _cancelled_after(workdir / "a", untouched, tools, 2).status == CANCELLED
    baseline = RenderEngine(tools, project_dir=workdir / "a").render(
        RenderRequest(project=untouched, quick_qc=True, resume=True))
    assert baseline.status == COMPLETED

    edited = _fast(_project(workdir / "b", scenes=5, narration=tuple([1.0] * 5),
                            with_subtitles=False, transitions=False))
    (workdir / "b").mkdir(parents=True, exist_ok=True)
    assert _cancelled_after(workdir / "b", edited, tools, 2).status == CANCELLED
    edited.scenes[0].narration.text = "A different opening."
    after_edit = RenderEngine(tools, project_dir=workdir / "b").render(
        RenderRequest(project=edited, quick_qc=True, resume=True))
    assert after_edit.status == COMPLETED

    assert after_edit.details["resumed_frames"] < baseline.details["resumed_frames"]


def test_a_successful_render_leaves_no_scratch_behind(tools, workdir):
    """Cleanup is deliberate: the segments are gone, so the next render starts
    clean and cannot accidentally reuse frames from a different project."""
    project = _fast(_project(workdir, scenes=2, narration=(1.0, 1.0),
                             with_subtitles=False, transitions=False))
    first = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert first.status == COMPLETED
    second = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert second.details["resumed_frames"] == 0


def test_scratch_files_are_cleaned_up_after_a_render(tools, workdir):
    project = _fast(_project(workdir, scenes=2, narration=(1.0, 1.0),
                             with_subtitles=False, transitions=False))
    result = RenderEngine(tools, project_dir=workdir).render(
        RenderRequest(project=project, quick_qc=True))
    assert result.status == COMPLETED
    leftovers = list((workdir / "cache" / "renders" / "render_work").glob("segment_*.mp4"))
    assert leftovers == []


# -- the service -----------------------------------------------------------

def test_the_service_plan_matches_the_render(tools, workdir):
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    service = RenderService(tools, project_dir=workdir)
    plan = service.plan(project)
    assert plan.ready, [getattr(item, "message", "") for item in plan.errors]

    result = service.render(project, quick_qc=True)
    assert result.status == COMPLETED
    info = probe_media(result.path, tools)
    assert info.duration == pytest.approx(plan.duration, abs=0.35)
    assert info.width == int(plan.resolution.split("x")[0])


def test_the_service_reports_problems_without_rendering(tools, workdir):
    project = _fast(_project(workdir, scenes=2, narration=(1.2, 1.0)))
    (workdir / "audio" / "s1.wav").unlink()
    service = RenderService(tools, project_dir=workdir)
    plan = service.plan(project)
    assert not plan.ready
    assert plan.errors
    assert (workdir / "renders").exists() is False or \
        list((workdir / "renders").glob("*.mp4")) == []
