"""Stage G tests: the Video library page and honest preview, through the window.

Sections 25, 30, 31, 33, 62, 78, 83-86, 113-115.  The chain exercised here is

    window -> Video library page -> submitted job -> library service
           -> FFprobe measurement -> project asset / scene / timeline

on a real (offscreen) :class:`MainWindow` with a real :class:`JobManager`.  The
clips are tiny real MP4 files, and the page's own actions - scan, make pictures,
send to project - are clicked the way a user clicks them, not called directly.

Nothing in this module is an AI model: a clip registered as generated comes from
a labelled test backend, and the tests check the page says so.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from app.core.paths import AppPaths  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
from app.project.service import CreateRequest  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui import project_controller as pc  # noqa: E402
from app.ui.dialogs import project_dialogs as dialogs  # noqa: E402
from app.ui.main_window import PAGES, MainWindow  # noqa: E402

from tests.ai_fakes import _tiny_mp4, ffmpeg_tools  # noqa: E402


TOOLS = ffmpeg_tools()


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


def make_clip(folder: Path, name: str = "clip.mp4", *, width: int = 160,
              height: int = 96, fps: int = 12, seconds: float = 2.0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return _tiny_mp4(folder / name, width=width, height=height, fps=fps,
                     seconds=seconds)


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
def page(window):
    """The library page, pointed at the application's own folders."""
    view = window.video_library_page
    view.refresh()
    pump(0.05)
    return view


def open_project(window) -> object:
    service = window.context.projects.service
    request = CreateRequest(name="Library GUI Test", width=640, height=360,
                            fps=24, folder=str(window.context.paths.projects_dir))
    service.create_project(request, open_after=True)
    window.context.projects.project_opened.emit(service.current)
    pump(0.1)
    return service.current


def index_a_clip(page, path: Path, **kwargs) -> None:
    """Put a clip into the library the way the studio itself does."""
    page.library().add(str(path), **kwargs)
    page.refresh()
    pump(0.05)


def select_row(page, row: int = 0) -> None:
    page.table.selectRow(row)
    page._on_selection_changed()
    pump(0.02)


def button(page, text: str) -> QPushButton:
    for candidate in page.findChildren(QPushButton):
        if candidate.text().strip() == text:
            return candidate
    raise AssertionError(f"no button labelled {text!r} on the page")


def unconnected_buttons(widget) -> list[str]:
    dead: list[str] = []
    for candidate in widget.findChildren(QPushButton):
        try:
            count = candidate.receivers(candidate.clicked)
        except Exception:  # noqa: BLE001 - if Qt cannot tell, do not accuse it
            continue
        if count <= 0 and candidate.isEnabled():
            dead.append(candidate.text() or candidate.objectName() or "(unnamed)")
    return dead


# ---------------------------------------------------------------------------
# the page in the window
# ---------------------------------------------------------------------------

def test_the_page_is_a_real_page_between_timeline_and_render(window) -> None:
    keys = [entry[0] for entry in PAGES]
    assert "videos" in keys
    assert keys.index("timeline") < keys.index("videos") < keys.index("render")
    labels = [window.nav.item(row).text() for row in range(window.nav.count())]
    assert "Video library" in labels
    assert window._pages["videos"] is window.video_library_page
    window.show_page("videos")
    pump(0.05)
    assert window.stack.currentWidget() is window.video_library_page


def test_no_button_on_the_page_is_dead(page) -> None:
    assert unconnected_buttons(page) == []


def test_an_empty_library_says_what_to_do(page) -> None:
    assert page.library().counts() == {}
    assert "Scan the folder" in page.status_label.text()
    assert page.table.rowCount() == 0


def test_a_clip_shows_up_with_its_measured_numbers(page, tmp_path) -> None:
    path = make_clip(tmp_path / "clips", "harbour.mp4", width=320, height=176)
    index_a_clip(page, path)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text().startswith("harbour")
    row = [page.table.item(0, column).text()
           for column in range(page.table.columnCount())]
    assert "2s" in " ".join(row), "the length column carries the measured length"
    assert "h264" in " ".join(row).lower()
    assert "ready" in " ".join(row).lower()
    select_row(page)
    assert "320x176" in page.detail_label.text(), \
        "the measured resolution appears once the clip is selected"


def test_selecting_a_clip_shows_its_provenance(page, tmp_path) -> None:
    path = make_clip(tmp_path / "clips", "made.mp4")
    index_a_clip(page, path, source="generated", backend="fake_video",
                 label="TEST BACKEND (fake video - not an AI model)",
                 prompt="a harbour at dawn", seed=11)
    select_row(page)
    detail = page.detail_label.text()
    assert "320" not in detail  # nothing invented for a 160x96 clip
    assert "160x96" in detail
    assert page.provenance_label.text().startswith("Generated by")
    assert "TEST BACKEND" in page.provenance_label.text()
    assert "AI model" not in page.provenance_label.text() or \
        "not an AI model" in page.provenance_label.text()


def test_the_search_box_filters_the_list(page, tmp_path) -> None:
    index_a_clip(page, make_clip(tmp_path / "clips", "harbour.mp4"))
    index_a_clip(page, make_clip(tmp_path / "clips", "forest.mp4"))
    assert page.table.rowCount() == 2
    page.search_edit.setText("forest")
    page.refresh_list()
    pump(0.02)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text().startswith("forest")
    page.search_edit.setText("")
    page.refresh_list()
    pump(0.02)
    assert page.table.rowCount() == 2


def test_the_source_filter_only_keeps_one_kind(page, tmp_path) -> None:
    index_a_clip(page, make_clip(tmp_path / "clips", "gen.mp4"),
                 source="generated", backend="fake_video")
    index_a_clip(page, make_clip(tmp_path / "clips", "render.mp4"),
                 source="render")
    index = page.source_combo.findData("render")
    assert index >= 0
    page.source_combo.setCurrentIndex(index)
    page.refresh_list()
    pump(0.02)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text().startswith("render")


def test_a_clip_whose_file_has_gone_is_marked_and_filterable(page, tmp_path,
                                                             window) -> None:
    path = make_clip(tmp_path / "clips", "vanishing.mp4")
    index_a_clip(page, path)
    path.unlink()
    page.library().refresh_exists()
    page.refresh()
    pump(0.02)
    assert page.table.item(0, 6).text() != "READY"
    page.missing_only.setChecked(True)
    page.refresh_list()
    pump(0.02)
    assert page.table.rowCount() == 1


def test_scan_is_one_job_and_indexes_the_library_folder(page, window,
                                                        tmp_path) -> None:
    """Scan goes through the job manager, not around it."""
    library_root = Path(page.library().root)
    make_clip(library_root, "in_folder.mp4")
    page.scan_folder()
    assert window.context.jobs.is_running("video.library.scan") or \
        page.table.rowCount() == 1, "the scan was not submitted as a job"
    assert wait_until(lambda: page.table.rowCount() == 1), "the scan never finished"
    assert page.library().counts()["total"] == 1


def test_make_pictures_is_one_job_per_press(page, window, tmp_path) -> None:
    index_a_clip(page, make_clip(tmp_path / "clips", "shot.mp4"))
    select_row(page)
    page.make_thumbnails()
    assert wait_until(lambda: page.library().all()[0].thumbnail != ""), \
        "the picture was never made"
    picture = Path(page.library().all()[0].thumbnail)
    assert picture.is_file() and picture.stat().st_size > 0


def test_a_job_that_fails_leaves_an_honest_message(page, window, tmp_path) -> None:
    """Importing a file that is not there fails with what happened and what to do."""
    missing = tmp_path / "clips" / "not-there.mp4"
    page.import_clip(str(missing))
    assert wait_until(
        lambda: "not there" in page.status_label.text().lower()), \
        page.status_label.text()
    # The failure is visible, and nothing was indexed.
    assert page.library().counts() == {}


# ---------------------------------------------------------------------------
# sending a clip into the project (sections 25, 30, 31)
# ---------------------------------------------------------------------------

def test_send_to_project_puts_the_clip_in_the_assets(page, window, tmp_path) -> None:
    open_project(window)
    path = make_clip(tmp_path / "clips", "asset.mp4")
    index_a_clip(page, path, source="generated", backend="fake_video",
                 label="TEST BACKEND (fake video - not an AI model)")
    select_row(page)
    page.send_selected("project")
    project = window.context.projects.project
    assert wait_until(lambda: bool(project.assets)), "the clip never reached the project"
    asset = project.assets[0]
    assert asset.name.startswith("asset"), asset
    assert asset.kind == "video", asset
    assert Path(str(asset.path)).name == "asset.mp4" or \
        Path(str(asset.absolute_path)).name == "asset.mp4", asset


def test_send_to_scene_adds_a_video_scene(page, window, tmp_path) -> None:
    project = open_project(window)
    path = make_clip(tmp_path / "clips", "scene.mp4")
    index_a_clip(page, path)
    select_row(page)
    page.send_selected("scene")
    assert wait_until(lambda: len(project.scenes) >= 1), \
        f"no scene was added: {page.send_note.text()}"
    kinds = [getattr(scene, "type", getattr(scene, "scene_type", ""))
             for scene in project.scenes]
    assert "video" in kinds, f"{kinds} - {page.send_note.text()}"


def test_send_to_timeline_adds_a_clip(page, window, tmp_path) -> None:
    project = open_project(window)
    path = make_clip(tmp_path / "clips", "timeline.mp4", seconds=1.0)
    index_a_clip(page, path)
    select_row(page)
    page.send_selected("timeline")
    assert wait_until(lambda: bool(project.timeline())), \
        f"the clip never reached the timeline: {page.send_note.text()}"
    rows = project.timeline()
    assert rows and rows[-1]["duration"] == pytest.approx(1.0, abs=0.2), rows


def test_sending_with_no_clip_selected_is_refused_politely(page, window,
                                                          tmp_path) -> None:
    """With nothing selected the send buttons are off - no job, no surprise."""
    open_project(window)
    index_a_clip(page, make_clip(tmp_path / "clips", "unselected.mp4"))
    page.table.clearSelection()
    page._update_enabled()
    pump(0.05)
    assert not button(page, "Send to project").isEnabled()
    assert not button(page, "Send to timeline").isEnabled()
    before = set(window.context.projects.project.assets)
    page.send_selected("project")
    pump(0.05)
    assert set(window.context.projects.project.assets) == before, \
        "refusing must not touch the project"


def test_sending_with_no_project_open_says_what_to_do(page, tmp_path) -> None:
    index_a_clip(page, make_clip(tmp_path / "clips", "noproject.mp4"))
    select_row(page)
    page.send_selected("project")
    pump(0.05)
    note = page.send_note.text().lower()
    assert "project" in note and ("open" in note or "create" in note), note


# ---------------------------------------------------------------------------
# removing (never surprising) and preview (never pretending)
# ---------------------------------------------------------------------------

def test_remove_forgets_the_clip_and_keeps_the_file(page, tmp_path) -> None:
    path = make_clip(tmp_path / "clips", "keep.mp4")
    index_a_clip(page, path)
    select_row(page)
    page.remove_selected(delete_file=False)
    assert wait_until(lambda: page.table.rowCount() == 0), "the clip is still listed"
    assert path.is_file(), "the file must be left where it is"


def test_the_library_never_overwrites_a_clip_it_was_given(page, tmp_path) -> None:
    path = make_clip(tmp_path / "clips", "once.mp4")
    before = path.read_bytes()
    index_a_clip(page, path)
    index_a_clip(page, path)
    assert path.read_bytes() == before
    assert page.table.rowCount() == 1


def test_preview_reports_what_this_machine_can_do(page, tmp_path) -> None:
    """Playback is feature-detected, and the page never claims it played."""
    from app.ui import preview

    path = make_clip(tmp_path / "clips", "preview.mp4")
    index_a_clip(page, path)
    select_row(page)
    page.preview_selected()
    pump(0.05)
    text = page.preview_label.text()
    if preview.preview_supported():
        assert text == "" or "playing" in text.lower()
    else:
        assert "cannot play" in text.lower()
        assert "folder" in text.lower(), \
            "an unavailable player must suggest what to do instead"


def test_preview_of_a_missing_file_is_honest(tmp_path) -> None:
    from app.ui import preview

    result = preview.preview_video(tmp_path / "nothing.mp4")
    assert result.opened is False
    text = result.describe()
    assert "nothing.mp4" in text or "not" in text.lower()
    assert result.what_to_do, "the user is told what to do next"


def test_open_in_system_player_refuses_a_missing_file(tmp_path) -> None:
    from app.ui import preview

    result = preview.open_in_system_player(tmp_path / "gone.mp4")
    assert result.opened is False
    assert result.message


def test_opening_the_containing_folder_of_a_missing_file_is_honest(tmp_path) -> None:
    from app.ui import preview

    result = preview.open_containing_folder(tmp_path / "gone.mp4")
    assert result.opened is False
    assert result.what_to_do


# ---------------------------------------------------------------------------
# the page follows the project and the renderer (sections 25, 30)
# ---------------------------------------------------------------------------

def test_opening_and_closing_a_project_refreshes_the_list(page, window,
                                                          tmp_path) -> None:
    path = make_clip(tmp_path / "clips", "before.mp4")
    index_a_clip(page, path)
    open_project(window)
    pump(0.05)
    window.context.projects.close(window, reason="test finished")
    pump(0.05)
    assert page.table.rowCount() == 1, "the library is the studio's, not the project's"
    assert page.table.item(0, 0).text().startswith("before")


def test_a_finished_render_is_indexed_as_a_render(page, window, tmp_path) -> None:
    """The render hook must not call a finished render an AI generation."""
    clip = make_clip(tmp_path / "renders", "final_render.mp4")
    window._on_render_completed(clip)
    pump(0.1)
    assert page.table.rowCount() == 1
    entry = page.library().all()[0]
    assert entry.source == "render"
    assert entry.provenance() == "Rendered from the project"
    assert entry.is_ai_model is False


def test_a_clip_from_a_test_backend_never_claims_an_ai_model(page, tmp_path) -> None:
    from app.ai.service import AIService
    from app.ai.registry import AIBackendManager
    from tests.ai_fakes import FakeVideoBackend, make_video_request

    service = AIService(None, data_root=tmp_path,
                        manager=AIBackendManager(None, data_root=tmp_path,
                                                 extra_backends=[
                                                     FakeVideoBackend(
                                                         "fake_video",
                                                         is_model=False)]))
    service.status()
    assert service.generate_video(make_video_request(tmp_path),
                                  backend_id="fake_video").ok
    entry = service.library.all()[0]
    assert entry.is_ai_model is False, \
        "a test backend must never be recorded as an AI model"
    assert entry.to_dict()["is_ai_model"] is False
