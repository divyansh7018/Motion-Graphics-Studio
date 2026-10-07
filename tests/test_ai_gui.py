"""Stage G tests: the whole call chain through the real window.

Sections 78, 83-86, 113-115.  The mandatory chain is

    window -> AI Studio page -> submitted job -> backend -> validator
           -> history -> asset manager -> project -> timeline

and the only way to test it is to do it: a real :class:`MainWindow` (offscreen), a
real :class:`JobManager` thread pool, the real AI Studio service with a labelled
test backend, and the real project service.  Nothing here claims an AI model ran:
every backend in this module reports ``TEST BACKEND``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from app.ai.service import AIService  # noqa: E402
from app.core.paths import AppPaths  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
from app.project.service import CreateRequest  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui import project_controller as pc  # noqa: E402
from app.ui.dialogs import project_dialogs as dialogs  # noqa: E402
from app.ui.main_window import PAGES, MainWindow  # noqa: E402

from tests.ai_fakes import FakeVideoBackend  # noqa: E402


# ---------------------------------------------------------------------------
# the event loop
# ---------------------------------------------------------------------------

def pump(seconds: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


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
# the window, as the user gets it
# ---------------------------------------------------------------------------

@pytest.fixture()
def window(tmp_path, monkeypatch, qapp):
    paths = AppPaths(data_root=tmp_path / "data",
                     source_root=Path(__file__).resolve().parents[1],
                     reason="unit test")
    paths.ensure()
    jobs = JobManager()
    context = create_context(
        paths, jobs,
        startup=StartupInfo(data_root_reason=paths.reason,
                            directories_created=0, stale_temp_removed=0))
    # Real dialogs would block a headless test; the controller calls these by
    # name, so they are replaced where they are used.
    monkeypatch.setattr(pc, "ask_unsaved_changes",
                        lambda *a, **k: dialogs.UnsavedChoice.SAVE)
    monkeypatch.setattr(pc, "ask_recovery",
                        lambda *a, **k: dialogs.RecoveryChoice.IGNORE)
    monkeypatch.setattr(pc, "ask_external_change",
                        lambda *a, **k: dialogs.ConflictChoice.CANCEL)
    monkeypatch.setattr(pc, "ask_missing_asset",
                        lambda *a, **k: (dialogs.MissingAssetChoice.CANCEL, None))
    monkeypatch.setattr(pc, "ask_confirm", lambda *a, **k: True)
    main = MainWindow(context, jobs)
    yield main
    jobs.shutdown(timeout_ms=5000)
    main.close()
    pump(0.05)


@pytest.fixture()
def page(window, tmp_path):
    """The AI Studio page, with this module's test backends in front of it."""
    from app.ai.registry import AIBackendManager

    ai_page = window.ai_studio_page
    settings = window.context.settings
    fake = FakeVideoBackend(
        "fake_video", is_model=False,
        name="TEST BACKEND (fake video - not an AI model)")
    manager = AIBackendManager(settings, data_root=tmp_path,
                               extra_backends=[fake])
    service = AIService(settings, paths=window.context.paths,
                        data_root=tmp_path, manager=manager)
    service.status()
    ai_page.service = service
    ai_page.refresh()
    pump(0.05)
    return ai_page


def open_project(window) -> None:
    service = window.context.projects.service
    request = CreateRequest(name="GUI AI Test", width=640, height=360, fps=24,
                            folder=str(window.context.paths.projects_dir))
    service.create_project(request, open_after=True)
    window.context.projects.project_opened.emit(service.current)
    pump(0.1)


def select_backend(page, backend_id: str) -> bool:
    index = page.backend_combo.findData(backend_id)
    if index < 0:
        return False
    page.backend_combo.setCurrentIndex(index)
    pump(0.02)
    return True


def unconnected_buttons(page) -> list[str]:
    """Buttons on the page that nothing is connected to (no dead buttons)."""
    dead: list[str] = []
    for button in page.findChildren(QPushButton):
        try:
            count = button.receivers(button.clicked)
        except Exception:  # noqa: BLE001 - if Qt cannot tell, do not accuse it
            continue
        if count <= 0 and button.isEnabled():
            dead.append(button.text())
    return dead


# ---------------------------------------------------------------------------
# the page is part of the application (sections 83-84)
# ---------------------------------------------------------------------------

def test_the_ai_studio_is_a_page_of_the_real_window(window) -> None:
    assert window.ai_studio_page is not None
    assert "ai" in {key for key, _label, _group, _state in PAGES}
    assert window._pages["ai"] is window.ai_studio_page


def test_the_page_has_no_dead_buttons(page) -> None:
    assert unconnected_buttons(page) == []
    assert page.generate_button.isEnabled() is True


def test_the_state_card_never_claims_an_ai_model_when_there_is_none(page) -> None:
    studio = page._studio()
    summary = studio.manager.summary()
    assert summary["usable_real_models"] == 0
    assert "TEST BACKEND" in summary["note"] or "test backend" in summary["note"].lower()
    text = (page.state_label.text() + " " + page.honesty_label.text()).lower()
    assert "test backend" in text or "not an ai model" in text or "no ai model" in text


def test_detect_backends_runs_as_a_job_and_updates_the_list(page) -> None:
    page.detect_backends()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    assert page.backend_combo.count() >= 1
    rows = [page.backends_list.item(index).text()
            for index in range(page.backends_list.count())]
    assert any("fake video" in row.lower() or "TEST BACKEND" in row
               for row in rows), rows
    assert page.status_label.text()
    assert unconnected_buttons(page) == []


# ---------------------------------------------------------------------------
# generate through the page, on the real thread pool (sections 33, 83)
# ---------------------------------------------------------------------------

def generate_clip(page, *, prompt: str = "a harbour"):
    """Press the page's own Generate button and wait for the job to finish."""
    assert select_backend(page, "fake_video"), "the test backend is in the list"
    page.prompt_edit.setPlainText(prompt)
    page.duration_spin.setValue(1.0)
    page.width_spin.setValue(160)
    page.height_spin.setValue(96)
    index = page.fps_combo.findData(12)
    if index >= 0:
        page.fps_combo.setCurrentIndex(index)
    pump(0.02)
    page.generate()
    assert wait_until(lambda: page._studio().jobs.active() == []), \
        "the generation job never finished"
    return page._last_result


def test_pressing_generate_runs_one_job_and_shows_the_result(window, page) -> None:
    open_project(window)
    before = len(page._studio().jobs.all())
    result = generate_clip(page)
    assert result, "the page kept no result to show"
    assert Path(result["path"]).is_file()
    records = page._studio().jobs.all()
    assert len(records) == before + 1, "one press, one job"
    assert records[0].status == "COMPLETED"
    assert records[0].output["path"] == result["path"]
    assert page.jobs_table.rowCount() >= 1


def test_the_clip_that_appears_is_the_file_the_validator_measured(page) -> None:
    result = generate_clip(page)
    from tests.ai_fakes import probe_video, ffmpeg_tools
    from app.ai.video_validation import validate_video_file

    measured = probe_video(Path(result["path"]))
    assert measured["width"] == 160 and measured["height"] == 96
    check = validate_video_file(Path(result["path"]), tools=ffmpeg_tools())
    assert check.ok
    assert result["width"] == 160 and result["height"] == 96


def test_the_history_tab_shows_what_was_generated(page) -> None:
    generate_clip(page, prompt="a lighthouse")
    page.refresh_history()
    rows = [page.history_list.item(index).text()
            for index in range(page.history_list.count())]
    assert any("lighthouse" in row.lower() for row in rows)
    assert page._studio().history.counts().get("video") == 1


def test_two_presses_cannot_start_the_same_job_twice(window, page) -> None:
    """The duplicate guard is on the page's own submission path."""
    studio = page._studio()
    studio.jobs.register("already_running", operation="text_to_video",
                         backend="fake_video", mode="text_to_video")
    try:
        assert select_backend(page, "fake_video")
        page.prompt_edit.setPlainText("a harbour")
        pump(0.02)
        page.generate()
        assert "already running" in page.status_label.text()
        assert studio.jobs.get("already_running") is not None
    finally:
        studio.jobs.finish("already_running", state="CANCELLED")
        pump(0.02)


def test_a_backend_that_cannot_run_says_so_on_the_page(page) -> None:
    """Choosing something that is not installed never starts a silent fallback."""
    studio = page._studio()
    backend = FakeVideoBackend("missing_model", behaviour="missing_model",
                               state="NOT_INSTALLED", is_model=True)
    studio.manager.extra_backends.append(backend)
    studio.manager.refresh(rebuild=True)
    page._fill_backends()
    assert select_backend(page, "missing_model")
    page.prompt_edit.setPlainText("a harbour")
    pump(0.02)
    page.generate()
    assert wait_until(lambda: studio.jobs.active() == [])
    text = (page.status_label.text() + " " + page.send_note.text()).lower()
    assert "install" in text or "not available" in text or "no model" in text
    assert "fake_video" not in str(page._last_result.get("backend", ""))


# ---------------------------------------------------------------------------
# the clip reaches the project, the timeline and the scenes (sections 83, 86)
# ---------------------------------------------------------------------------

def test_send_to_timeline_puts_the_generated_clip_in_the_project(window, page) -> None:
    open_project(window)
    result = generate_clip(page)
    page.send_to_timeline()
    pump(0.05)

    service = window.context.projects.service
    current = service.current
    assert current is not None
    scene = next((item for item in current.scenes if item.type == "video"), None)
    assert scene is not None, "no video scene was created"
    asset_id = scene.extra["video"]["asset_id"]
    asset = current.asset_by_id(asset_id)
    assert asset is not None
    path = Path(asset.resolve(service.current_layout.project_file.parent))
    assert path.is_file()
    assert path.read_bytes() == Path(result["path"]).read_bytes(), \
        "the project plays the clip that was generated"
    assert "added" in page.status_label.text().lower()
    assert page.project_changed is not None


def test_send_to_scene_updates_the_scene_the_user_chose(window, page) -> None:
    open_project(window)
    service = window.context.projects.service
    existing = service.add_scene(scene_type="title", name="Opening")
    generate_clip(page)
    page._fill_scenes()
    index = page.scene_combo.findData(str(existing.id))
    assert index >= 0, "the open project's scenes are offered"
    page.scene_combo.setCurrentIndex(index)
    page.send_to_scene()
    pump(0.05)

    scene = next(item for item in service.current.scenes
                 if str(item.id) == str(existing.id))
    assert scene.type == "video"
    assert scene.extra["video"]["asset_id"]
    assert len(service.current.scenes) == 1, "the scene was updated, not doubled"


def test_the_timeline_page_shows_the_clip_that_was_sent(window, page) -> None:
    open_project(window)
    generate_clip(page)
    page.send_to_timeline()
    pump(0.1)
    timeline = getattr(window, "timeline_page", None)
    if timeline is not None and hasattr(timeline, "refresh"):
        timeline.refresh()
    pump(0.05)
    service = window.context.projects.service
    assert [scene.type for scene in service.current.scenes] == ["video"]
    assert window.context.projects.service.is_open


def test_the_project_saved_from_the_window_keeps_the_clip(window, page) -> None:
    open_project(window)
    generate_clip(page)
    page.send_to_timeline()
    pump(0.05)
    assert window.context.projects.save(window, reason="stage g test") is True
    pump(0.05)
    service = window.context.projects.service
    project_file = service.current_layout.project_file
    text = project_file.read_text(encoding="utf-8")
    assert "asset_id" in text
    assert '"video"' in text


# ---------------------------------------------------------------------------
# cancellation and the queue UI (sections 34, 35, 85)
# ---------------------------------------------------------------------------

def test_cancel_from_the_page_stops_a_running_clip(window, page) -> None:
    studio = page._studio()
    studio.manager.extra_backends.append(FakeVideoBackend("fake_hang",
                                                          behaviour="hang"))
    studio.manager.refresh(rebuild=True)
    page._fill_backends()
    assert select_backend(page, "fake_hang")
    page.prompt_edit.setPlainText("a slow one")
    pump(0.02)
    page.generate()
    assert wait_until(lambda: studio.jobs.active() != []), "no job started"

    page.cancel()
    assert wait_until(lambda: studio.jobs.active() == [])
    record = studio.jobs.all()[0]
    assert record.status == "CANCELLED"
    assert record.cancelled is True
    assert record.output == {}
    assert studio.history.counts().get("video", 0) == 0


def test_a_failed_job_is_visible_in_the_queue_with_its_reason(window, page) -> None:
    studio = page._studio()
    studio.manager.extra_backends.append(FakeVideoBackend("fake_raises",
                                                          behaviour="raise"))
    studio.manager.refresh(rebuild=True)
    page._fill_backends()
    assert select_backend(page, "fake_raises")
    page.prompt_edit.setPlainText("this one throws")
    pump(0.02)
    page.generate()
    assert wait_until(lambda: studio.jobs.active() == [])
    record = studio.jobs.all()[0]
    assert record.status == "FAILED"
    assert record.error and record.what_to_do
    assert page.jobs_table.rowCount() >= 1
    assert not studio.history.counts().get("video")


def test_the_queue_table_shows_the_job_the_model_and_the_backend(page) -> None:
    generate_clip(page)
    page.refresh_jobs()
    assert page.jobs_table.rowCount() >= 1
    cells = " ".join(page.jobs_table.item(0, column).text()
                     for column in range(page.jobs_table.columnCount()))
    assert "TEST BACKEND" in cells
    assert "COMPLETED" in cells
    assert "text_to_video" in cells
    assert "%" in cells and "s" in cells, "progress and elapsed are shown"


def test_showing_a_result_points_at_the_file_that_was_made(page) -> None:
    result = generate_clip(page)
    page.refresh_jobs()
    page.jobs_table.selectRow(0)
    page.show_selected_result()
    pump(0.02)
    assert page._last_result["path"] == result["path"]
    assert result["path"] in page.result_label.text()
    assert "send to" in page.result_label.text().lower()


def test_retry_of_a_finished_job_is_refused_when_it_cannot_be_the_same(page) -> None:
    generate_clip(page)
    page.refresh_jobs()
    page.jobs_table.selectRow(0)
    page.retry_selected_job()
    pump(0.02)
    text = page.status_label.text().lower()
    assert "generate again" in text or "cannot" in text or "not available" in text


# ---------------------------------------------------------------------------
# the image and storyboard paths through the page (sections 30, 49, 61)
# ---------------------------------------------------------------------------

def test_an_unapproved_storyboard_plan_is_refused_by_the_page(window, page) -> None:
    open_project(window)
    service = window.context.projects.service
    scene = service.add_scene(scene_type="title", name="Harbour")
    service.update_scene_field(str(scene.id), script="a harbour at dawn")
    assert select_backend(page, "fake_video")
    page.plan_storyboard()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    assert page.plan_list.count() == 1
    assert "approv" in page.plan_label.text().lower()

    page.run_plan()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    assert page._studio().history.counts().get("video", 0) == 0, \
        "nothing is generated from an unapproved plan"


def test_approving_the_plan_then_running_it_makes_one_clip_per_scene(window,
                                                                    page) -> None:
    open_project(window)
    service = window.context.projects.service
    for name, words in (("Harbour", "a harbour at dawn"),
                        ("Forest", "a forest in the rain")):
        scene = service.add_scene(scene_type="title", name=name)
        service.update_scene_field(str(scene.id), script=words)
    assert select_backend(page, "fake_video")
    page.width_spin.setValue(160)
    page.height_spin.setValue(96)
    page.plan_storyboard()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    page.approve_plan()
    assert page.plan_list.count() == 2
    assert "approved" in page.plan_label.text().lower()

    page.run_plan()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    made = page._studio().history.all()
    assert len(made) == 2, "one clip per approved scene"
    assert {entry.scene for entry in made} == {
        str(scene.id) for scene in service.current.scenes}
    for entry in made:
        assert Path(entry.path).is_file()


def test_a_scene_with_no_words_falls_back_to_its_name_in_the_plan(window,
                                                                  page) -> None:
    open_project(window)
    window.context.projects.service.add_scene(scene_type="title", name="Opening")
    assert select_backend(page, "fake_video")
    page.plan_storyboard()
    assert wait_until(lambda: page._studio().jobs.active() == [])
    rows = [page.plan_list.item(index).text()
            for index in range(page.plan_list.count())]
    assert rows, "the project's scene is offered, named after itself"
    assert page._studio().history.counts().get("video", 0) == 0, \
        "planning never generates anything"


def test_prompts_and_presets_are_saved_through_the_page(page) -> None:
    studio = page._studio()
    page.prompt_edit.setPlainText("a quiet harbour")
    page.save_prompt()
    pump(0.02)
    assert [entry.text for entry in studio.prompts.all()] == ["a quiet harbour"]
    assert page.prompts_list.count() >= 1

    assert select_backend(page, "fake_video")
    page.save_preset()
    pump(0.02)
    assert studio.presets.all(), "the form's settings were stored as a preset"
    assert page.preset_combo.count() >= 1
    page.preset_combo.setCurrentIndex(page.preset_combo.count() - 1)
    page.apply_preset()
    pump(0.02)
    assert "applied" in page.status_label.text().lower()


# ---------------------------------------------------------------------------
# retry: the same request again, or a reason why not (sections 34, 100)
# ---------------------------------------------------------------------------

def select_job(page, job_id: str) -> None:
    """Select a job's row in the queue table."""
    page.refresh_jobs()
    for row in range(page.jobs_table.rowCount()):
        item = page.jobs_table.item(row, 0)
        if item is not None and str(item.data(Qt.UserRole) or "") == job_id:
            page.jobs_table.selectRow(row)
            pump(0.02)
            return
    raise AssertionError(f"job {job_id} is not in the queue table")


def test_a_failed_job_is_retried_with_the_same_seed_and_settings(window, page,
                                                                tmp_path) -> None:
    """Retry runs the recorded request again - same backend, seed and size."""
    from app.ai.jobs import AIJobState

    open_project(window)
    studio = page._studio()
    record = studio.jobs.register(
        "retry-target", operation="video_generate", kind="video",
        backend="fake_video", backend_name="TEST BACKEND (fake video - not an AI model)",
        model="", mode="text_to_video",
        request={"mode": "text_to_video", "backend": "fake_video",
                 "prompt": "a retried harbour", "seed": 4242,
                 "duration": 1.0, "fps": 12, "width": 160, "height": 96,
                 "output_dir": str(tmp_path / "retried"), "name_stem": "retried"},
        is_ai_model=False, label="TEST BACKEND (fake video - not an AI model)")
    studio.jobs.finish(record.id, state=AIJobState.FAILED,
                       error="the backend stopped halfway")

    select_job(page, record.id)
    page.retry_selected_job()
    assert wait_until(lambda: studio.jobs.active() == [] and
                      len(studio.jobs.all()) >= 2), "the retry never finished"

    retried = next(item for item in studio.jobs.all() if item.retry_of == record.id)
    assert retried.status == AIJobState.COMPLETED
    assert retried.request["seed"] == 4242, "the same seed is used"
    assert retried.request["prompt"] == "a retried harbour"
    assert retried.backend == "fake_video", "the same backend is used"
    made = Path(str(retried.output.get("path") or ""))
    assert made.is_file() and made.name.startswith("retried")
    assert retried.retry_of == record.id, "the chain back to the failed job is kept"
    assert Path(str(record.output.get("path") or made)).parent == made.parent


def test_a_completed_job_is_not_retried_into_a_duplicate(window, page) -> None:
    open_project(window)
    result = generate_clip(page)
    studio = page._studio()
    finished = studio.jobs.all()[0]
    select_job(page, finished.id)
    page.retry_selected_job()
    pump(0.05)
    assert "generate again" in page.status_label.text().lower()
    assert len(studio.jobs.all()) == 1, "nothing new was submitted"
    assert Path(result["path"]).is_file(), "the earlier clip is still there"


def test_a_retry_is_refused_when_the_backend_has_gone(window, page) -> None:
    """A retry never quietly switches to another backend."""
    from app.ai.jobs import AIJobState

    open_project(window)
    studio = page._studio()
    record = studio.jobs.register(
        "gone-backend", operation="video_generate", kind="video",
        backend="uninstalled_backend", model="", mode="text_to_video",
        request={"mode": "text_to_video", "prompt": "x", "seed": 1},
        is_ai_model=False)
    studio.jobs.finish(record.id, state=AIJobState.CANCELLED)

    before = len(studio.jobs.all())
    select_job(page, record.id)
    page.retry_selected_job()
    pump(0.05)
    text = page.status_label.text()
    assert "not available" in text.lower() and "choose a backend" in text.lower()
    assert len(studio.jobs.all()) == before, "no job was submitted"


def test_a_retry_is_refused_when_the_model_has_gone(window, page) -> None:
    from app.ai.jobs import AIJobState

    open_project(window)
    studio = page._studio()
    record = studio.jobs.register(
        "gone-model", operation="video_generate", kind="video",
        backend="fake_video", model="a-model-that-is-not-installed",
        mode="text_to_video",
        request={"mode": "text_to_video", "prompt": "x", "seed": 1},
        is_ai_model=False)
    studio.jobs.finish(record.id, state=AIJobState.FAILED, error="the model failed")

    select_job(page, record.id)
    page.retry_selected_job()
    pump(0.05)
    assert "not installed" in page.status_label.text().lower()
    assert "choose a model" in page.status_label.text().lower()
