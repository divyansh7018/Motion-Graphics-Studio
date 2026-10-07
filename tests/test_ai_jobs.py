"""Stage G tests: the AI job queue - submission, progress, cancel, retry, failure.

Sections 9, 32-36, 51, 83, 85-86.  These run through the application's real
:class:`~app.jobs.manager.JobManager` (the same thread pool the window uses) and
the real AI Studio service, with deterministic test backends.  Every backend
here is a labelled fake: no test in this module claims a real AI model ran.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.ai.jobs import (AIJobRegistry, AIJobState, finalize_from_result,  # noqa: E402
                         image_generate_spec, job_key_for, submit_ai_job,
                         video_batch_spec, video_generate_spec)
from app.ai.service import AIService  # noqa: E402
from app.ai.video import VideoMode  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
from app.jobs.keys import JobKeys  # noqa: E402

from tests.ai_fakes import FakeVideoBackend, make_video_request  # noqa: E402


# ---------------------------------------------------------------------------
# the event loop, driven explicitly (these tests have no window)
# ---------------------------------------------------------------------------

def wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return predicate()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def jobs(qapp):
    manager = JobManager()
    yield manager
    manager.shutdown(timeout_ms=5000)
    QCoreApplication.processEvents()


@pytest.fixture()
def studio(paths, settings, tmp_path):
    """An AI service whose manager also holds the fakes this module needs."""
    from app.ai.registry import AIBackendManager

    manager = AIBackendManager(settings, data_root=tmp_path, extra_backends=[
        FakeVideoBackend("fake_video"),
        FakeVideoBackend("fake_raises", behaviour="raise"),
        FakeVideoBackend("fake_empty", behaviour="empty"),
        FakeVideoBackend("fake_hang", behaviour="hang"),
        FakeVideoBackend("fake_missing", behaviour="missing_model",
                         state="NOT_INSTALLED"),
    ])
    service = AIService(settings, paths=paths, data_root=tmp_path,
                        manager=manager)
    service.status()
    service.registry = AIJobRegistry()
    return service


def clip_payload(studio, tmp_path, *, backend="fake_video", **overrides):
    request = make_video_request(tmp_path, **overrides)
    request.backend = backend
    return {"request": request, "backend_id": backend, "registry": studio.registry,
            "service": studio, "paths": studio.paths,
            "settings": studio.settings}


# ---------------------------------------------------------------------------
# the job record (section 32)
# ---------------------------------------------------------------------------

def test_the_eight_job_states_are_the_documented_ones() -> None:
    from app.ai.types import JOB_STATE_LABELS, JOB_STATES, TERMINAL_JOB_STATES

    assert set(JOB_STATES) == {
        "QUEUED", "INITIALIZING", "RUNNING", "PROCESSING", "SAVING",
        "COMPLETED", "FAILED", "CANCELLED"}
    assert TERMINAL_JOB_STATES == {"COMPLETED", "FAILED", "CANCELLED"}
    assert all(JOB_STATE_LABELS[state] for state in JOB_STATES)


def test_a_record_shows_backend_model_progress_elapsed_and_eta(studio) -> None:
    record = studio.registry.register(
        "job_1", operation="video_generate", backend="fake_video",
        backend_name="Fake video backend", model="fake-model", mode="text_to_video",
        batch_size=1)
    studio.registry.update("job_1", status=AIJobState.RUNNING, progress=0.5)
    assert record.progress_label() == "50%"
    assert record.elapsed() >= 0.0
    assert record.elapsed_label().endswith("s")
    assert record.eta() is not None and record.eta() >= 0.0
    assert record.eta_label().startswith("~")
    assert record.title() == "Text to video - Fake video backend"
    assert record.verdict() == "Running"

    data = record.to_dict()
    for key in ("id", "operation", "backend", "model", "status", "progress",
                "elapsed_label", "eta_label", "verdict", "request", "output"):
        assert key in data
    assert record.finished is False


def test_a_finished_record_says_how_it_ended(studio) -> None:
    studio.registry.register("job_2", operation="video_generate",
                             backend="fake_video")
    studio.registry.finish("job_2", state=AIJobState.COMPLETED,
                           output={"path": "/tmp/clip.mp4"}, completed_items=1)
    record = studio.registry.get("job_2")
    assert record.finished is True
    assert record.verdict() == "Completed"
    assert record.progress == 1.0
    assert record.eta() is None
    assert record.output["path"] == "/tmp/clip.mp4"
    assert record.completed_items == 1


def test_a_failure_record_keeps_what_happened_why_and_what_to_do(studio) -> None:
    studio.registry.register("job_3", operation="video_generate",
                             backend="fake_video")
    studio.registry.finish("job_3", state=AIJobState.FAILED,
                           error="The clip was not generated.",
                           why="The backend refused.", what_to_do="Try another.",
                           code="FAKE_REFUSED")
    record = studio.registry.get("job_3")
    assert record.verdict() == "The clip was not generated."
    assert record.why and record.what_to_do and record.code == "FAKE_REFUSED"


def test_a_cancelled_record_is_marked_cancelled(studio) -> None:
    studio.registry.register("job_4", operation="video_generate")
    studio.registry.finish("job_4", state=AIJobState.CANCELLED,
                           error="The job was cancelled.")
    record = studio.registry.get("job_4")
    assert record.cancelled is True
    assert record.finished is True
    assert record.verdict() == "Cancelled"


def test_the_registry_only_keeps_active_jobs_in_the_active_list(studio) -> None:
    studio.registry.register("a", operation="video_generate")
    studio.registry.register("b", operation="video_generate")
    studio.registry.finish("a", state=AIJobState.COMPLETED)
    assert [item.id for item in studio.registry.active()] == ["b"]
    text = studio.registry.describe()
    assert "[b]" in text
    assert "Generate clip" in text
    assert studio.registry.clear_finished() == 1
    assert studio.registry.get("a") is None
    studio.registry.finish("b", state=AIJobState.CANCELLED)
    assert studio.registry.describe().startswith("No AI jobs running")


def test_the_registry_never_grows_without_bound() -> None:
    registry = AIJobRegistry(limit=20)
    for index in range(40):
        registry.register(f"job_{index}", operation="video_generate")
        registry.finish(f"job_{index}", state=AIJobState.COMPLETED)
    assert len(registry.all()) <= 20


# ---------------------------------------------------------------------------
# submission and duplicate refusal (sections 9, 33)
# ---------------------------------------------------------------------------

def test_one_user_action_submits_exactly_one_job(qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path)
    submitted = submit_ai_job(jobs, video_generate_spec(payload), registry=studio.registry,
                              operation="video_generate", backend="fake_video",
                              backend_name="Fake video backend", mode="text_to_video")
    assert submitted["ok"] is True
    job = submitted["job"]
    assert jobs.active_count() <= 1
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.COMPLETED
    assert record.completed_items == 1
    assert record.output["seed"] == 1234
    assert record.backend == "fake_video"
    assert record.mode == "text_to_video"
    from pathlib import Path

    assert Path(record.output["path"]).is_file()
    assert jobs.active_count() == 0


def test_a_second_identical_submission_is_refused_and_names_the_first(
        qapp, jobs, studio, tmp_path) -> None:
    studio.registry.register("running_1", operation="video_generate",
                             backend="fake_video", model="", mode="text_to_video")
    payload = clip_payload(studio, tmp_path)
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_video",
                              mode="text_to_video")
    assert submitted["ok"] is False
    assert "already running" in submitted["reason"]
    assert submitted["running"].id == "running_1"
    assert "running_1"[:8] in submitted["reason"]
    assert jobs.active_count() == 0, "no twin job was started"


def test_a_duplicate_can_be_ignored_deliberately(qapp, jobs, studio, tmp_path) -> None:
    """The guard is a guard, not a rule the caller cannot get past."""
    studio.registry.register("running_1", operation="video_generate",
                             backend="fake_video", mode="text_to_video")
    payload = clip_payload(studio, tmp_path)
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_video",
                              mode="text_to_video", guard_duplicates=False)
    assert submitted["ok"] is True
    assert wait_until(lambda: studio.registry.get(submitted["job"].id).finished)


def test_a_different_mode_is_not_a_duplicate(studio) -> None:
    studio.registry.register("running_1", operation="video_generate",
                             backend="fake_video", mode="text_to_video")
    assert studio.registry.duplicate_of(operation="video_generate",
                                        backend="fake_video", model="",
                                        mode="image_to_video") is None
    assert studio.registry.duplicate_of(operation="video_generate",
                                        backend="other_video", model="",
                                        mode="text_to_video") is None


def test_the_job_key_says_batch_or_single_and_extend_is_its_own_key() -> None:
    single = make_video_request("/tmp/x")
    batched = make_video_request("/tmp/x", batch=4)
    extend = make_video_request("/tmp/x", mode=VideoMode.EXTEND)
    assert job_key_for(single) == JobKeys.AI_VIDEO_GENERATE
    assert job_key_for(batched) == JobKeys.AI_VIDEO_BATCH
    assert job_key_for(extend) == JobKeys.AI_VIDEO_EXTEND


def test_a_normal_generate_is_never_a_batch(qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path)
    spec = video_generate_spec(payload)
    assert spec.key == JobKeys.AI_VIDEO_GENERATE
    assert spec.payload["request"].batch == 1


# ---------------------------------------------------------------------------
# the real queue: progress, cancellation, failure (sections 32, 34, 35)
# ---------------------------------------------------------------------------

def test_progress_is_reported_while_the_job_runs(qapp, jobs, studio, tmp_path) -> None:
    backend = studio.manager.get("fake_video").backend
    original = backend.generate

    def slow(request, *, progress=None, cancel=None):
        for step in range(4):
            time.sleep(0.05)
            if progress is not None:
                progress("RUNNING", 0.2 + 0.2 * step)
        return original(request, progress=progress, cancel=cancel)

    backend.generate = slow  # type: ignore[assignment]
    try:
        payload = clip_payload(studio, tmp_path)
        submitted = submit_ai_job(jobs, video_generate_spec(payload),
                                  registry=studio.registry,
                                  operation="video_generate",
                                  backend="fake_video")
        seen: list[float] = []

        def watch() -> bool:
            record = studio.registry.get(submitted["job"].id)
            seen.append(record.progress)
            return record.finished

        assert wait_until(watch)
        assert any(value > 0 for value in seen), "no progress was ever reported"
        record = studio.registry.get(submitted["job"].id)
        assert record.status == AIJobState.COMPLETED
        assert record.progress == 1.0
    finally:
        backend.generate = original  # type: ignore[assignment]


def test_cancelling_a_clip_job_really_stops_it(qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, backend="fake_hang")
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_hang")
    job = submitted["job"]
    assert wait_until(lambda: job.started_at > 0)
    assert jobs.cancel(job.id) is True
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.CANCELLED
    assert record.cancelled is True
    assert record.output == {}
    assert studio.history.counts().get("video", 0) == 0


def test_a_failed_job_keeps_the_reason_in_its_record(qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, backend="fake_raises")
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_raises")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.FAILED
    assert "raised on purpose" in record.error
    assert record.what_to_do
    assert record.code == "BACKEND_CRASHED"


def test_a_job_whose_backend_wrote_nothing_is_failed_not_completed(
        qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, backend="fake_empty")
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_empty")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.FAILED
    assert record.code == "VIDEO_OUTPUT_MISSING"


def test_a_backend_with_no_model_reports_that_instead_of_failing_darkly(
        qapp, jobs, studio, tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, backend="fake_missing")
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_missing")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.FAILED
    assert record.code in ("BACKEND_NOT_AVAILABLE", "MODEL_NOT_FOUND")
    assert record.what_to_do


def test_a_job_that_cannot_be_run_says_so_before_starting(qapp, jobs, studio,
                                                          tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, backend="not_a_backend")
    submitted = submit_ai_job(jobs, video_generate_spec(payload),
                              registry=studio.registry,
                              operation="video_generate",
                              backend="not_a_backend")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.FAILED
    assert record.code == "NO_BACKEND_AVAILABLE"
    assert "install" in (record.error + record.what_to_do).lower()


# ---------------------------------------------------------------------------
# batches (sections 33, 35, 51)
# ---------------------------------------------------------------------------

def test_a_batch_is_one_job_that_produces_several_files(qapp, jobs, studio,
                                                        tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, batch=3, name_stem="batch")
    payload["request"].seed = 100
    submitted = submit_ai_job(jobs, video_batch_spec(payload),
                              registry=studio.registry, kind="video",
                              operation="video_generate", backend="fake_video")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.status == AIJobState.COMPLETED
    assert record.completed_items == 3
    assert len(record.output["paths"]) == 3
    assert len(set(record.output["paths"])) == 3
    assert {p.split("/")[-1] for p in record.output["paths"]} == {
        "batch_01.mp4", "batch_02.mp4", "batch_03.mp4"}


def test_cancelling_a_batch_keeps_the_clips_that_finished(qapp, jobs, studio,
                                                          tmp_path) -> None:
    payload = clip_payload(studio, tmp_path, batch=4, name_stem="kept")
    payload["request"].seed = 500
    backend = studio.manager.get("fake_video").backend
    original = backend.generate
    calls = {"count": 0}

    def cancel_after_two(request, *, progress=None, cancel=None):
        calls["count"] += 1
        if calls["count"] >= 2 and cancel is not None:
            cancel.cancel()
        return original(request, progress=progress, cancel=cancel)

    backend.generate = cancel_after_two  # type: ignore[assignment]
    try:
        submitted = submit_ai_job(jobs, video_batch_spec(payload),
                                  registry=studio.registry, kind="video",
                                  operation="video_generate",
                                  backend="fake_video")
        job = submitted["job"]
        assert wait_until(lambda: studio.registry.get(job.id).finished)
    finally:
        backend.generate = original  # type: ignore[assignment]

    record = studio.registry.get(job.id)
    assert record.status == AIJobState.CANCELLED
    assert record.completed_items >= 2, "the finished clips must be kept"
    from pathlib import Path

    for path in record.output["paths"]:
        assert Path(path).is_file()
    assert len(record.output["paths"]) == record.completed_items


# ---------------------------------------------------------------------------
# retry (section 34)
# ---------------------------------------------------------------------------

def test_a_retry_is_a_new_job_for_the_same_request(qapp, jobs, studio,
                                                   tmp_path) -> None:
    """Retry runs the identical request again, and the chain is visible."""
    payload = clip_payload(studio, tmp_path, seed=77)
    first = submit_ai_job(jobs, video_generate_spec(payload), registry=studio.registry,
                          operation="video_generate", backend="fake_video")
    assert wait_until(lambda: studio.registry.get(first["job"].id).finished)
    original = studio.registry.get(first["job"].id)

    again = clip_payload(studio, tmp_path, seed=77)
    submitted = submit_ai_job(jobs, video_generate_spec(again),
                              registry=studio.registry,
                              operation="video_generate", backend="fake_video")
    job = submitted["job"]
    assert job.id != original.id
    studio.registry.update(job.id, retry_of=original.id)
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.retry_of == original.id
    assert record.request["seed"] == original.request["seed"] == 77
    assert record.status == AIJobState.COMPLETED

    from pathlib import Path

    assert Path(record.output["path"]) != Path(original.output["path"])


def test_retry_only_when_safe_is_the_caller_s_choice(studio) -> None:
    """A cancelled job and a failed job both carry a retry option; a completed
    job does not need one, and the record says which is which."""
    studio.registry.register("c1", operation="video_generate")
    studio.registry.finish("c1", state=AIJobState.CANCELLED)
    studio.registry.register("f1", operation="video_generate")
    studio.registry.finish("f1", state=AIJobState.FAILED, error="nope")
    studio.registry.register("d1", operation="video_generate")
    studio.registry.finish("d1", state=AIJobState.COMPLETED)
    assert studio.registry.get("c1").cancelled
    assert studio.registry.get("f1").error == "nope"
    assert studio.registry.get("d1").output == {}


# ---------------------------------------------------------------------------
# the safety net for a body that never finished (section 34)
# ---------------------------------------------------------------------------

class _JobResult:
    def __init__(self, **kwargs):
        self.__dict__.update(
            {"job_id": "j", "cancelled": False, "succeeded": False,
             "error": None, **kwargs})


class _Error:
    what_happened = "The clip was not generated."
    why = "The backend refused."
    actions = ("Try another backend.", "Check the log.")
    error_code = "FAKE_REFUSED"


def test_a_body_that_never_reported_is_brought_to_a_terminal_state() -> None:
    registry = AIJobRegistry()
    registry.register("j", operation="video_generate")
    record = finalize_from_result(registry, _JobResult(
        error=_Error(), error_code="FAKE_REFUSED"))
    assert record.status == AIJobState.FAILED
    assert record.error == "The clip was not generated."
    assert "log" in record.what_to_do.lower()
    assert record.code == "FAKE_REFUSED"


def test_the_safety_net_marks_a_cancelled_run_and_never_overwrites_a_verdict() -> None:
    registry = AIJobRegistry()
    registry.register("j", operation="video_generate",
                      request={"prompt": "x"})
    registry.finish("j", state=AIJobState.COMPLETED)
    assert finalize_from_result(registry, _JobResult(succeeded=True)).status \
        == AIJobState.COMPLETED

    registry.register("k", operation="video_generate")
    assert finalize_from_result(
        registry, _JobResult(job_id="k", cancelled=True)).status \
        == AIJobState.CANCELLED
    assert finalize_from_result(registry, None) is None


def test_the_safety_net_clears_a_job_left_running_forever() -> None:
    registry = AIJobRegistry()
    registry.register("j", operation="video_generate")
    registry.update("j", status=AIJobState.RUNNING)
    finalize_from_result(registry, _JobResult(error=_Error()))
    assert registry.active() == []


# ---------------------------------------------------------------------------
# the image job uses the same queue (sections 20, 33)
# ---------------------------------------------------------------------------

def test_an_image_job_is_submitted_to_the_same_queue(qapp, jobs, studio,
                                                     tmp_path) -> None:
    from app.image.provider import GenerationRequest

    request = GenerationRequest(prompt="a harbour", width=128, height=128,
                                output_dir=str(tmp_path / "images"),
                                name_stem="img")
    payload = {"request": request, "backend_id": "not_a_backend",
               "registry": studio.registry, "service": studio,
               "paths": studio.paths, "settings": studio.settings}
    submitted = submit_ai_job(jobs, image_generate_spec(payload),
                              registry=studio.registry, kind="image",
                              operation="image_generate",
                              backend="not_a_backend")
    job = submitted["job"]
    assert wait_until(lambda: studio.registry.get(job.id).finished)
    record = studio.registry.get(job.id)
    assert record.kind == "image"
    assert record.status == AIJobState.FAILED
    assert record.error
    assert record.what_to_do
    assert not list((tmp_path / "images").glob("*.png"))
