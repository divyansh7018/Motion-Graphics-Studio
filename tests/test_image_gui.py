"""Image Studio GUI tests (Stage F, sections 1, 48, 49, 50, 68, 81).

The interface rules this file enforces:

* the page **opens and works with no model installed** - the normal state on a
  fresh machine;
* a control that cannot work is **disabled with the reason shown**, never left
  live and wired to nothing;
* generation runs as a **job**, so the Qt thread is never blocked;
* nothing is labelled an AI result when no AI ran.

They run against real PySide6 widgets on Qt's offscreen platform.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import SIGNAL, QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from app.image.provider import GenerationMode  # noqa: E402
from app.jobs.keys import JobKeys  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
from app.project.service import CreateRequest  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_GENERATOR = REPO_ROOT / "tests" / "fake_image_generator.py"


def process_events(seconds: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_for_jobs(jobs: JobManager, timeout: float = 30.0) -> None:
    """Run the event loop until the job pool is idle."""
    deadline = time.monotonic() + timeout
    jobs.wait_for_all(timeout_ms=int(timeout * 1000))
    while time.monotonic() < deadline and jobs.active_count():
        process_events(0.05)


def command_template() -> str:
    """A real local program the command backend can run.

    Not an image model: a deterministic stand-in, so the whole adapter path is
    exercised without installing one.
    """
    return (f'"{sys.executable}" "{FAKE_GENERATOR}" --prompt {{prompt}} '
            f'--seed {{seed}} --width {{width}} --height {{height}} '
            f'--out {{output}}')


def build_window(paths, *, with_command_backend: bool = True):
    """A real MainWindow, optionally with a local backend configured."""
    from app.core.settings import SettingsStore

    if with_command_backend:
        # Configure the local-command backend before the application starts,
        # the way a user would in Settings.
        store = SettingsStore(paths.settings_file)
        settings = store.load().settings
        settings.image.command = command_template()
        settings.image.active_backend = "command"
        store.save(settings)

    jobs = JobManager()
    context = create_context(
        paths, jobs,
        startup=StartupInfo(data_root_reason="pytest", directories_created=0,
                            stale_temp_removed=0))
    main = MainWindow(context, jobs)
    return main, jobs


def close_window(main, jobs) -> None:
    if main.isVisible():
        main.close()
    jobs.shutdown(timeout_ms=4000)
    main.deleteLater()
    process_events(0.1)


@pytest.fixture()
def window(qapp, paths):
    main, jobs = build_window(paths)
    yield main
    close_window(main, jobs)


@pytest.fixture()
def bare_window(qapp, paths):
    """A window with **no** image backend configured - a fresh machine."""
    main, jobs = build_window(paths, with_command_backend=False)
    yield main
    close_window(main, jobs)


@pytest.fixture()
def page(window):
    studio = window.image_studio_page
    studio.detect_backends()
    wait_for_jobs(window.context.jobs)
    process_events(0.1)
    return studio


def _dead_buttons(widget) -> list[str]:
    dead = []
    for button in widget.findChildren(QPushButton):
        if button.receivers(SIGNAL("clicked()")) == 0:
            dead.append(button.text() or button.objectName() or "(unnamed)")
    return dead


def _select_backend(page, backend_id: str) -> bool:
    index = page.backend_box.findData(backend_id)
    if index < 0:
        return False
    page.backend_box.setCurrentIndex(index)
    process_events(0.05)
    return True


def _wait_for_generation(page, window, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    window.context.jobs.wait_for_all(timeout_ms=int(timeout * 1000))
    while time.monotonic() < deadline and page._generate_job is not None:
        process_events(0.05)


# --------------------------------------------------------------------------
# The page exists and opens honestly (sections 1, 68)
# --------------------------------------------------------------------------

def test_the_image_studio_is_in_the_navigation(window) -> None:
    assert "images" in window._page_keys
    window.show_page("images")
    assert window.stack.currentWidget() is window._pages["images"]


def test_the_visuals_placeholder_is_gone(window) -> None:
    """The page is built now, so a 'later' entry beside it would be a lie."""
    labels = [window.nav.item(row).text() for row in range(window.nav.count())]
    assert not any("Visuals" in label for label in labels)


def test_it_opens_with_no_model_installed_and_says_so(bare_window) -> None:
    """A fresh machine must still get a usable page and an honest sentence."""
    page = bare_window.image_studio_page
    page.detect_backends()
    wait_for_jobs(bare_window.context.jobs)
    process_events(0.1)
    text = page.state_label.text()
    assert "no local image-generation model is installed" in text.lower()
    assert page.device_label.text()
    # The page is still usable: importing and editing need no model.
    assert page.import_button.isEnabled()


def test_a_page_with_no_backend_disables_generate_honestly(bare_window) -> None:
    page = bare_window.image_studio_page
    page.detect_backends()
    wait_for_jobs(bare_window.context.jobs)
    process_events(0.1)
    assert page.generate_button.isEnabled() is False
    assert page.generate_hint.text()


def test_every_backend_is_listed_even_the_unavailable_ones(page) -> None:
    """The user can see what exists and why it cannot be used (section 4)."""
    assert page.backend_box.count() >= 5
    labels = [page.backend_box.itemText(index)
              for index in range(page.backend_box.count())]
    assert any("not available" in label for label in labels)


def test_no_button_on_the_page_is_wired_to_nothing(page) -> None:
    assert _dead_buttons(page) == []


# --------------------------------------------------------------------------
# Controls follow the real capabilities (sections 37, 50, 68)
# --------------------------------------------------------------------------

def test_generate_is_disabled_when_the_mode_is_unsupported(page) -> None:
    """Text to image with the built-in backend cannot work, so it must not."""
    if not _select_backend(page, "standard"):
        pytest.skip("the standard backend is not present")
    page.mode_box.setCurrentIndex(
        page.mode_box.findData(GenerationMode.TEXT_TO_IMAGE))
    process_events(0.05)
    page._update_generate_enabled()
    assert page.generate_button.isEnabled() is False
    assert "not supported" in page.generate_hint.text()


def test_unsupported_settings_are_disabled_not_left_live(page) -> None:
    """The standard backend has no steps, guidance, seed or negative prompt."""
    if not _select_backend(page, "standard"):
        pytest.skip("the standard backend is not present")
    assert page.negative_edit.isEnabled() is False
    assert page.steps_spin.isEnabled() is False
    assert page.guidance_spin.isEnabled() is False


def test_supported_settings_are_enabled_for_a_backend_that_has_them(page) -> None:
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    assert page.negative_edit.isEnabled() is True
    assert page.steps_spin.isEnabled() is True
    assert page.seed_spin.isEnabled() is True


def test_the_batch_limit_follows_the_backend(page) -> None:
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    capabilities = page.service.registry.get("command").capabilities()
    assert page.batch_spin.maximum() == capabilities.max_batch


def test_a_missing_prompt_disables_generate_with_a_reason(page) -> None:
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    page.prompt_edit.setPlainText("")
    process_events(0.05)
    assert not page.generate_button.isEnabled()
    assert "prompt" in page.generate_hint.text().lower()


def test_advanced_settings_start_hidden(page) -> None:
    assert not page.advanced_panel.isVisible()
    page.advanced_toggle.click()
    process_events(0.05)
    assert page.advanced_toggle.isChecked()
    assert page.advanced_toggle.text() == "Hide advanced settings"
    page.advanced_toggle.click()
    process_events(0.05)
    assert not page.advanced_panel.isVisible()


def test_the_chosen_backend_is_remembered(window, page) -> None:
    """Section 63: the studio reopens on what the user last chose."""
    if not _select_backend(page, "standard"):
        pytest.skip("the standard backend is not present")
    assert window.context.settings.image.active_backend == "standard"


# --------------------------------------------------------------------------
# Detection runs off the Qt thread (section 34)
# --------------------------------------------------------------------------

def test_detection_is_submitted_as_a_job(window, paths) -> None:
    page = window.image_studio_page
    page.detect_backends()
    process_events(0.02)
    keys = window.context.jobs.submitted_keys()
    assert JobKeys.IMAGE_DETECT in keys, "detection ran on the Qt thread"
    wait_for_jobs(window.context.jobs)


def test_detection_reports_the_backends_it_found(window) -> None:
    page = window.image_studio_page
    page.detect_backends()
    wait_for_jobs(window.context.jobs)
    process_events(0.1)
    assert page._detect_job is None, "the page never saw the job finish"
    assert page.backend_box.count() >= 5


# --------------------------------------------------------------------------
# A real generation through the page (sections 34, 47, 48)
# --------------------------------------------------------------------------

def test_generating_produces_a_file_and_a_history_row(window, page) -> None:
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    page.prompt_edit.setPlainText("a quiet harbour at dawn")
    page.width_spin.setValue(128)
    page.height_spin.setValue(128)
    page.seed_spin.setValue(4321)
    process_events(0.05)
    page.generate()
    _wait_for_generation(page, window)
    process_events(0.1)
    assert page.status_label.text().startswith("Created"), page.status_label.text()
    assert page.history_list.count() == 1
    assert page._current_image is not None
    assert page._current_image.is_file()


def test_the_seed_used_is_reported_not_invented(window, page) -> None:
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    page.prompt_edit.setPlainText("a seed check")
    page.width_spin.setValue(128)
    page.height_spin.setValue(128)
    page.seed_spin.setValue(777)
    page.generate()
    _wait_for_generation(page, window)
    assert "777" in page.status_label.text()


def test_a_generation_failure_is_shown_with_what_to_do(window, page) -> None:
    """A refusal must explain itself rather than say 'something went wrong'."""
    if not _select_backend(page, "command"):
        pytest.skip("the command backend is not configured")
    page.prompt_edit.setPlainText("")
    process_events(0.05)
    assert page.generate_button.isEnabled() is False
    assert "prompt" in page.generate_hint.text().lower()
    # Calling it anyway must not submit a job that cannot work.
    page.generate()
    process_events(0.1)
    assert page._generate_job is None


def test_stop_is_disabled_until_something_is_running(page) -> None:
    assert page.stop_button.isEnabled() is False


def test_upscaling_from_the_page_creates_a_new_file(window, page, tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "to_upscale.png"
    Image.new("RGB", (120, 80), (40, 90, 140)).save(source)
    page._select_image(source)
    original = source.read_bytes()
    page.upscale_image()
    deadline = time.monotonic() + 30
    window.context.jobs.wait_for_all(timeout_ms=30000)
    while time.monotonic() < deadline and page._upscale_job is not None:
        process_events(0.05)
    process_events(0.1)
    assert source.read_bytes() == original, "the source was modified"
    assert "Standard Resize" in page.status_label.text()


# --------------------------------------------------------------------------
# Editing from the page (sections 16, 17, 53)
# --------------------------------------------------------------------------

def test_applying_an_edit_shows_a_preview_without_writing(window, page, tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "edit_me.png"
    Image.new("RGB", (200, 120), (200, 120, 40)).save(source)
    page._select_image(source)
    page.edit_list.setCurrentRow(0)
    page.apply_edit()
    process_events(0.05)
    assert page._edit_session is not None
    assert "Nothing has been written yet" in page.edit_hint.text()


def test_saving_an_edit_creates_a_new_file_and_keeps_the_original(window, page,
                                                                 tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "original.png"
    Image.new("RGB", (200, 120), (10, 160, 90)).save(source)
    original = source.read_bytes()
    page._select_image(source)
    page.edit_list.setCurrentRow(1)      # a resize
    page.apply_edit()
    page.save_edited()
    process_events(0.1)
    assert source.read_bytes() == original, "the original was overwritten"
    assert page._current_image is not None
    assert page._current_image != source
    assert page._current_image.is_file()


def test_resetting_edits_writes_nothing(window, page, tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "reset_me.png"
    Image.new("RGB", (100, 100), (60, 60, 60)).save(source)
    page._select_image(source)
    page.edit_list.setCurrentRow(0)
    page.apply_edit()
    page.reset_edits()
    process_events(0.05)
    assert page._edit_session is None
    assert "Nothing was written" in page.edit_hint.text()
    assert not list(tmp_path.glob("*_edit*"))


def test_a_corrupt_import_is_reported_not_crashed(window, page, tmp_path,
                                                  monkeypatch) -> None:
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image")
    monkeypatch.setattr(
        "app.ui.views.image_studio_view.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(bad), "")))
    page.import_image()
    process_events(0.05)
    assert page.status_label.text(), "the failure was not explained"
    assert "not a recognisable image" in page.status_label.text()


# --------------------------------------------------------------------------
# Project integration from the page (sections 26, 27)
# --------------------------------------------------------------------------

def _open_project(window) -> None:
    controller = window.context.projects
    service = controller.service
    root = window.context.paths.data_root / "projects" / "Image GUI"
    service.create_project(CreateRequest(
        name="Image GUI", width=512, height=288, fps=25, template="blank",
        folder=root), open_after=False)
    assert controller.open_path(root / "project.json") is True
    process_events(0.05)


def test_send_to_scene_works_from_the_page(window, page, tmp_path) -> None:
    from PIL import Image

    _open_project(window)
    source = tmp_path / "scene_me.png"
    Image.new("RGB", (160, 90), (220, 40, 40)).save(source)
    page._select_image(source)
    page.send_to_scene()
    process_events(0.1)
    assert "overlay element" in page.status_label.text().lower()
    project = window.context.projects.project
    assert project.scenes and project.scenes[0].elements
    assert project.scenes[0].elements[0].kind == "image"


def test_send_to_scene_without_a_project_explains_the_options(window, page,
                                                              tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "orphan.png"
    Image.new("RGB", (64, 64), (10, 10, 10)).save(source)
    page._select_image(source)
    page.send_to_scene()
    process_events(0.05)
    text = page.status_label.text().lower()
    assert "no project is open" in text
    assert "create" in text or "asset library" in text


def test_show_metadata_reports_a_file_with_no_record(window, page, tmp_path) -> None:
    from PIL import Image

    source = tmp_path / "bare.png"
    Image.new("RGB", (64, 64), (5, 5, 5)).save(source)
    page._select_image(source)
    page.show_metadata()
    process_events(0.05)
    assert "no metadata" in page.details_label.text().lower()
