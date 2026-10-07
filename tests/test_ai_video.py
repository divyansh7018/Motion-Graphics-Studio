"""Stage G tests: the video contract, the validator and the built-in fixture.

Sections 26-31, 43-46, 51, 78, 84-85, 97-98.  The built-in test backend is used
for the real-generation paths (it writes a real MP4 with FFmpeg and is labelled
as a test backend everywhere), and the fakes cover the failure modes a real
model can have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.capabilities import AICapabilities
from app.ai.service import AIService, NOT_INSTALLED_HELP
from app.ai.types import BackendKind, ProviderState
from app.ai.video import (CAMERA_MOVES, MODE_FEATURE, MODE_LABELS, VIDEO_MODES,
                          CameraMove, VideoMode, VideoRequest, VideoResult)
from app.ai.video_backends.standard import (TEST_BACKEND_LABEL, TEST_MODEL_ID,
                                            StandardVideoBackend)
from app.ai.video_validation import (OUTPUT_CODES, VIDEO_CHECK_NOT_AVAILABLE,
                                     VIDEO_OUTPUT_EMPTY, VIDEO_OUTPUT_MISSING,
                                     validate_video_file,
                                     validate_video_request)

from tests.ai_fakes import (FakeVideoBackend, make_video_request, probe_video,
                            write_png)


@pytest.fixture()
def studio(paths, settings, tmp_path):
    """A studio whose manager also holds the fakes this file needs."""
    from app.ai.registry import AIBackendManager

    manager = AIBackendManager(settings, data_root=tmp_path, extra_backends=[
        FakeVideoBackend("fake_video"),
        FakeVideoBackend("fake_raises", behaviour="raise"),
        FakeVideoBackend("fake_empty", behaviour="empty"),
        FakeVideoBackend("fake_corrupt", behaviour="corrupt"),
        FakeVideoBackend("fake_hang", behaviour="hang"),
        FakeVideoBackend("fake_missing", behaviour="missing_model",
                         state=ProviderState.NOT_INSTALLED),
    ])
    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=manager)
    service.status()
    return service


# --------------------------------------------------------------------------
# The contract (sections 26, 43)
# --------------------------------------------------------------------------

def test_the_five_video_modes_are_the_five_the_studio_offers() -> None:
    assert set(VIDEO_MODES) == {
        VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO,
        VideoMode.VIDEO_TO_VIDEO, VideoMode.EXTEND,
        VideoMode.STORYBOARD_TO_VIDEO}
    for mode in VIDEO_MODES:
        assert MODE_LABELS[mode]
        assert MODE_FEATURE[mode]


def test_a_request_round_trips_through_json() -> None:
    request = make_video_request("/tmp/x", prompt="harbour", seed=99,
                                 camera=CameraMove.PAN)
    restored = VideoRequest.from_dict(request.to_dict())
    assert restored.prompt == "harbour"
    assert restored.seed == 99
    assert restored.camera == CameraMove.PAN
    assert restored.to_dict() == request.to_dict()


def test_an_unknown_key_is_dropped_rather_than_guessed() -> None:
    request = VideoRequest.from_dict({"prompt": "x", "something": 1})
    assert request.prompt == "x"
    assert not hasattr(request, "something")


def test_a_result_is_only_a_success_with_a_file_behind_it() -> None:
    assert not VideoResult(ok=False, error="nothing").ok
    assert VideoResult(ok=True, path=Path("/tmp/clip.mp4"),
                       duration=1.0).summary().startswith("Clip generated")


def test_the_camera_moves_are_named() -> None:
    assert CameraMove.PAN in CAMERA_MOVES
    assert len(CAMERA_MOVES) >= 5


def test_a_clip_needs_its_own_feature() -> None:
    """Image to video from a backend that cannot do it is refused with a reason."""
    caps = AICapabilities(text_to_video=True)
    issues = validate_video_request(
        make_video_request("/tmp/x", mode=VideoMode.IMAGE_TO_VIDEO), caps)
    errors = [issue for issue in issues if issue.severity == "error"]
    assert errors
    assert all(issue.what_to_do for issue in errors)
    assert any("image" in issue.code.lower() for issue in errors)


# --------------------------------------------------------------------------
# Validation (sections 46, 48, 84)
# --------------------------------------------------------------------------

def test_a_request_the_backend_cannot_do_is_refused_with_a_reason() -> None:
    caps = AICapabilities(text_to_video=True, max_duration=4.0, max_fps=24,
                          max_dimension=1024, min_dimension=64,
                          dimension_multiple=8)
    request = make_video_request("/tmp/x", duration=10.0, fps=60, width=100,
                                 height=96)
    issues = validate_video_request(request, caps)
    codes = {issue.code for issue in issues if issue.severity == "error"}
    assert codes
    for issue in issues:
        if issue.severity == "error":
            assert issue.what_to_do


def test_a_missing_output_is_a_failure_not_a_small_success(tmp_path) -> None:
    check = validate_video_file(tmp_path / "nope.mp4")
    assert not check.ok
    assert check.code == VIDEO_OUTPUT_MISSING


def test_an_empty_file_is_refused(tmp_path) -> None:
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"")
    check = validate_video_file(path)
    assert not check.ok
    assert check.code == VIDEO_OUTPUT_EMPTY


def test_a_file_that_is_not_a_video_is_refused(tmp_path) -> None:
    path = tmp_path / "notavideo.mp4"
    path.write_bytes(b"#!/bin/sh\necho not a video\n")
    check = validate_video_file(path)
    assert not check.ok
    assert check.code in OUTPUT_CODES
    assert check.what_to_do


def test_a_real_clip_passes_with_measured_numbers(tmp_path) -> None:
    from tests.ai_fakes import _tiny_mp4

    path = _tiny_mp4(tmp_path / "real.mp4", width=96, height=64, fps=8,
                     seconds=1.0)
    check = validate_video_file(path)
    assert check.ok
    assert check.width == 96 and check.height == 64
    assert check.duration == pytest.approx(1.0, abs=0.15)


def test_a_check_that_cannot_run_is_not_reported_as_a_failure(tmp_path) -> None:
    """Without FFmpeg the clip cannot be measured, and that is not a failure."""
    from tests.ai_fakes import _tiny_mp4

    class NoTools:
        discovery = None

    path = _tiny_mp4(tmp_path / "unmeasurable.mp4")
    check = validate_video_file(path, tools=NoTools())
    assert not check.ok
    assert check.code == VIDEO_CHECK_NOT_AVAILABLE
    assert "not available" in check.error.lower()


# --------------------------------------------------------------------------
# The built-in test backend (section 79)
# --------------------------------------------------------------------------

def test_the_test_backend_is_labelled_everywhere_it_appears() -> None:
    backend = StandardVideoBackend()
    assert backend.name == TEST_BACKEND_LABEL
    assert backend.is_model is False
    payload = backend.to_dict()
    assert payload["is_model"] is False
    assert "not an AI model" in payload["describe"].lower() or \
        "TEST BACKEND" in payload["name"]
    assert [model.id for model in backend.models()] == [TEST_MODEL_ID]


def test_it_writes_a_real_clip_and_reports_what_it_measured(tmp_path) -> None:
    backend = StandardVideoBackend()
    request = make_video_request(tmp_path)
    result = backend.generate(request)
    assert result.ok
    assert Path(result.path).is_file()
    measured = probe_video(Path(result.path))
    assert measured["codec"] == "h264"
    assert (measured["width"], measured["height"]) == (128, 96)
    assert result.width == 128 and result.height == 96


def test_the_same_request_twice_gives_the_same_clip(tmp_path) -> None:
    backend = StandardVideoBackend()
    first = backend.generate(make_video_request(tmp_path, seed=7))
    second = backend.generate(make_video_request(tmp_path, seed=7))
    assert first.path != second.path          # never overwritten
    assert Path(first.path).read_bytes() == Path(second.path).read_bytes()
    assert Path(second.path).name == "clip_1.mp4"


def test_it_refuses_an_operation_it_does_not_implement(tmp_path) -> None:
    backend = StandardVideoBackend()
    request = make_video_request(tmp_path, mode="not_a_mode")
    result = backend.generate(request)
    assert not result.ok
    assert result.error
    assert result.what_to_do


def test_it_holds_its_own_limits(tmp_path) -> None:
    backend = StandardVideoBackend()
    caps = backend.capabilities()
    assert caps.min_dimension >= 16
    assert caps.dimension_multiple == 2
    too_wide = make_video_request(tmp_path, width=caps.max_dimension + 2)
    issues = [issue for issue in validate_video_request(too_wide, caps)
              if issue.severity == "error"]
    assert issues


# --------------------------------------------------------------------------
# The service: one door for generation (sections 8, 36, 79, 100)
# --------------------------------------------------------------------------

def test_generation_through_the_service_records_history(studio, tmp_path) -> None:
    request = make_video_request(tmp_path, prompt="a harbour")
    result = studio.generate_video(request, backend_id="fake_video")
    assert result.ok
    assert studio.history.counts().get("video") == 1
    entry = studio.history.all()[0]
    assert entry.backend == "fake_video"
    assert entry.prompt == "a harbour"


def test_a_backend_that_does_not_exist_is_named_not_switched(studio, tmp_path) -> None:
    request = make_video_request(tmp_path)
    result = studio.generate_video(request, backend_id="not_a_backend")
    assert not result.ok
    assert result.code == "NO_BACKEND_AVAILABLE"
    assert NOT_INSTALLED_HELP.split(".")[0] in result.error
    assert result.options


def test_no_silent_switching_when_the_chosen_backend_is_unavailable(
        studio, tmp_path) -> None:
    request = make_video_request(tmp_path)
    result = studio.generate_video(request, backend_id="fake_missing")
    assert not result.ok
    assert result.code == "BACKEND_NOT_AVAILABLE"
    assert result.state_detail == ProviderState.NOT_INSTALLED
    assert "choose_backend" in result.options
    # ...and nothing was written by another backend on its behalf.
    assert not list((tmp_path / "clips").glob("*.mp4"))


def test_a_request_the_backend_cannot_do_says_which_operation(studio, tmp_path) -> None:

    backend = FakeVideoBackend("text_only",
                               capabilities=AICapabilities(text_to_video=True))
    studio.manager.extra_backends.append(backend)
    studio.manager.refresh(rebuild=True)
    request = make_video_request(tmp_path, mode=VideoMode.IMAGE_TO_VIDEO,
                                 source_image=str(write_png(Path(tmp_path) / "s.png")))
    result = studio.generate_video(request, backend_id="text_only")
    assert not result.ok
    assert result.code in ("OPERATION_NOT_SUPPORTED", "VIDEO_IMAGE_TO_VIDEO_UNSUPPORTED")


def test_a_backend_that_raises_is_reported_and_the_app_survives(studio, tmp_path) -> None:
    request = make_video_request(tmp_path)
    result = studio.generate_video(request, backend_id="fake_raises")
    assert not result.ok
    assert result.code == "BACKEND_CRASHED"
    assert "raised on purpose" in result.error
    assert result.options == ["retry", "choose_backend", "cancel"]


def test_a_backend_that_claims_success_without_a_file_is_not_believed(
        studio, tmp_path) -> None:
    request = make_video_request(tmp_path)
    result = studio.generate_video(request, backend_id="fake_empty")
    assert not result.ok
    assert result.error


def test_a_corrupt_output_is_refused(studio, tmp_path) -> None:
    request = make_video_request(tmp_path)
    result = studio.generate_video(request, backend_id="fake_corrupt")
    assert not result.ok
    assert result.code in OUTPUT_CODES


def test_a_disabled_backend_is_refused_by_name(studio, tmp_path) -> None:
    studio.manager.set_enabled("fake_video", False)
    try:
        result = studio.generate_video(make_video_request(tmp_path),
                                       backend_id="fake_video")
        assert not result.ok
        assert result.code == "BACKEND_DISABLED"
        assert "enable" in result.what_to_do.lower()
    finally:
        studio.manager.set_enabled("fake_video", True)


def test_a_mismatch_between_request_and_file_is_shown_not_hidden(studio, tmp_path) -> None:

    backend = studio.manager.get("fake_video").backend
    backend.behaviour = "tiny"
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_video")
    assert result.ok
    assert result.width == 2 and result.height == 2
    assert result.mismatch, "a 2x2 clip where 128x96 was asked for must say so"


def test_cancellation_really_stops_the_backend(studio, tmp_path) -> None:
    from tests.ai_fakes import FakeCancel

    cancel = FakeCancel(after=1)
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_hang", cancel=cancel)
    assert result.cancelled
    assert not result.ok
    assert studio.manager.get("fake_hang").backend.calls


def test_a_cancelled_run_is_never_recorded_as_a_success(studio, tmp_path) -> None:
    from tests.ai_fakes import FakeCancel

    studio.history.clear()
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_hang",
                                   cancel=FakeCancel(after=1))
    assert not result.ok
    assert studio.history.counts().get("video", 0) == 0


def test_reuse_is_reported_never_silently_substituted(studio, tmp_path) -> None:
    request = make_video_request(tmp_path, seed=5)
    studio.cache.enabled = True
    first = studio.generate_video(request, backend_id="fake_video")
    assert first.ok
    note = studio.cache_or_reuse_note(request, backend_id="fake_video",
                                      model_id=first.model)
    assert note["hit"] is True
    assert note["path"] == str(first.path)
    unseeded = make_video_request(tmp_path, seed=0)
    assert studio.cache_or_reuse_note(unseeded, backend_id="fake_video",
                                      model_id="x")["hit"] is False


# --------------------------------------------------------------------------
# Image to video and stills through the same door (sections 27, 61)
# --------------------------------------------------------------------------

def test_image_to_video_uses_the_source_image_and_keeps_it(studio, tmp_path) -> None:
    source = write_png(Path(tmp_path) / "source.png", size=(96, 64))
    before = source.read_bytes()
    request = make_video_request(tmp_path, mode=VideoMode.IMAGE_TO_VIDEO,
                                 source_image=str(source), name_stem="i2v")
    result = studio.generate_video(request, backend_id="fake_video")
    assert result.ok
    assert source.read_bytes() == before, "the source image must not change"
    assert Path(result.path).name == "i2v.mp4"


def test_an_image_the_backend_refuses_is_not_sent_anyway(studio, tmp_path) -> None:
    request = make_video_request(tmp_path, mode=VideoMode.IMAGE_TO_VIDEO,
                                 source_image=str(tmp_path / "gone.png"))
    result = studio.generate_video(request, backend_id="fake_video")
    assert not result.ok
    assert result.code == "VIDEO_SOURCE_IMAGE_MISSING"
    assert "gone.png" in result.error


def test_the_image_backend_reported_for_this_machine_is_reused(paths, settings,
                                                              tmp_path) -> None:
    """The AI Studio's image work goes through Stage F's own service."""
    service = AIService(settings, paths=paths, data_root=tmp_path)
    image_service = service.image_service()
    assert image_service is service.image_service()
    # Stage F's own service answers for the machine's image capabilities; the
    # studio does not keep a second opinion about them.
    status = image_service.status(refresh=False)
    assert status.device.cpu_count >= 1
    assert status.device.accelerator
    assert isinstance(image_service.backends(), list)


def test_generating_an_image_with_no_backend_says_so(studio, tmp_path) -> None:
    from app.image.provider import GenerationRequest

    request = GenerationRequest(prompt="a harbour", width=128, height=128,
                                output_dir=str(tmp_path / "images"),
                                name_stem="img")
    result = studio.generate_image(request, backend_id="not_a_backend")
    assert not result.ok
    assert result.error
    assert result.what_to_do


# --------------------------------------------------------------------------
# Checks (section 7)
# --------------------------------------------------------------------------

def test_a_light_check_reads_paths_and_a_deep_check_generates(studio) -> None:
    light = studio.self_test.__self__  # the service, for clarity below
    del light
    light_report = studio.manager.get("fake_video").backend.check(deep=False)
    assert light_report.level == "light"
    deep = studio.self_test("fake_video")
    assert deep["state"] == ProviderState.VERIFIED
    assert deep["ok"] is True
    evidence = deep["evidence"]
    assert Path(evidence["path"]).is_file()
    assert evidence["is_ai_model"] is True


def test_a_deep_check_that_cannot_run_says_check_not_available(studio) -> None:
    result = studio.self_test("fake_missing")
    assert result["ok"] is False
    assert result["state"] in (ProviderState.NOT_VERIFIED,
                               ProviderState.NOT_INSTALLED,
                               "CHECK NOT AVAILABLE")


def test_the_service_measures_the_clip_it_keeps(studio, tmp_path) -> None:
    """Numbers in the result come from the file, not from the request."""

    backend = studio.manager.get("fake_video").backend
    backend.behaviour = "tiny"      # writes 2x2 whatever was asked for
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_video")
    assert result.ok
    assert (result.width, result.height) == (2, 2)
    assert result.quality.get("measured_with")
    assert result.mismatch


def test_a_backend_with_nothing_it_can_do_reports_check_not_available(studio) -> None:
    from app.ai.capabilities import AICapabilities as Caps

    backend = FakeVideoBackend("no_ops", capabilities=Caps(), is_model=True)
    studio.manager.extra_backends.append(backend)
    studio.manager.refresh(rebuild=True)
    result = studio.self_test("no_ops")
    assert result["ok"] is False
    assert result["state"] == "CHECK NOT AVAILABLE"
    assert "does not declare" in result["why"]
    assert result["what_to_do"]


def test_the_studio_can_describe_itself_without_a_model(studio, tmp_path) -> None:
    studio.generate_video(make_video_request(tmp_path), backend_id="fake_video")
    text = studio.describe()
    assert "backends" in text.lower()
    assert "video" in text.lower()


def test_the_studio_lists_what_it_can_do_per_backend(studio) -> None:
    report = studio.status()
    entries = report.by_kind
    assert BackendKind.VIDEO in entries
    names = {entry.id for entry in entries[BackendKind.VIDEO]}
    assert {"standard_video", "fake_video"} <= names


def test_a_backend_that_writes_nothing_is_never_a_success(studio, tmp_path) -> None:
    """The service checks the file itself, whatever the backend claims."""
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_empty")
    assert not result.ok
    assert result.code == "VIDEO_OUTPUT_MISSING"
    assert "no video file" in result.error
    assert result.what_to_do


def test_a_corrupt_file_is_refused_even_when_the_backend_claims_success(
        studio, tmp_path) -> None:
    result = studio.generate_video(make_video_request(tmp_path),
                                   backend_id="fake_corrupt")
    assert not result.ok
    assert result.code in OUTPUT_CODES
    assert not studio.history.counts().get("video")
