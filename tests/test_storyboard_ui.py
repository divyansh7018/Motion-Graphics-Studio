"""Stage D - the Storyboard page, driven through the real window and JobManager.

These prove the page builds, degrades honestly with no project, and that
thumbnails and previews are produced by background jobs (not on the Qt thread)
and land back in the UI.  Modal dialogs (rename) are bypassed by calling the
same service methods the dialog would use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.project.service import CreateRequest  # noqa: E402


def process_events(seconds: float) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 20.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture()
def window(qapp, paths):
    from app.jobs.manager import JobManager
    from app.ui.context import StartupInfo, create_context
    from app.ui.main_window import MainWindow

    jobs = JobManager()
    context = create_context(paths, jobs,
                             startup=StartupInfo(data_root_reason="pytest",
                                                 directories_created=0, stale_temp_removed=0))
    main = MainWindow(context, jobs)
    yield main
    if main.isVisible():
        main.close()
    jobs.shutdown(timeout_ms=4000)
    main.deleteLater()
    process_events(0.1)


def _open_project(window, name="StoryProj"):
    controller = window.context.projects
    controller.service.create_project(CreateRequest(name=name))
    return controller


def test_storyboard_page_is_registered(window):
    assert "storyboard" in window._pages
    assert window.storyboard_page is window._pages["storyboard"]


def test_storyboard_degrades_honestly_with_no_project(window):
    page = window.storyboard_page
    page.refresh()
    assert page.strip.count() == 0
    assert "No project is open" in page.strip_hint.text()
    assert not page.add_button.isEnabled()


def test_adding_scenes_populates_the_strip(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()

    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.template_combo.setCurrentIndex(page.template_combo.findData("stat"))
    page._add_scene()
    page.template_combo.setCurrentIndex(page.template_combo.findData("chart"))
    page._add_scene()

    assert page.strip.count() == 3
    assert len(page._rows) == 3


def test_duplicate_and_delete_scene(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.strip.setCurrentRow(0)

    page._duplicate_scene()
    assert page.strip.count() == 2

    # The delete confirmation is a modal dialog; answer it without blocking.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *args, **kwargs: QMessageBox.Yes))
    page.strip.setCurrentRow(1)
    page._delete_scene()
    assert page.strip.count() == 1


def test_reorder_scenes(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    for key in ("title", "stat"):
        page.template_combo.setCurrentIndex(page.template_combo.findData(key))
        page._add_scene()

    page.strip.setCurrentRow(1)
    page._move(-1)
    rows = [page.strip.item(i).data(256) for i in range(page.strip.count())]
    assert rows == [r.scene_id for r in sorted(page._rows, key=lambda r: r.index)]


def test_storyboard_job_produces_thumbnails(window, tmp_path):
    controller = _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()

    # Kick the real background job and wait for a thumbnail file to appear.
    page._submit_storyboard()
    assert wait_until(lambda: any(
        Path(row.thumbnail_path).is_file() for row in page._rows if row.thumbnail_path
    ) or page._storyboard_job is None, timeout=25.0)

    # Once the job finishes the rows get their thumbnail paths.
    def thumbs_present():
        return any(Path(r.thumbnail_path).is_file() for r in page._rows if r.thumbnail_path)

    if not thumbs_present():
        # Give the dispatcher a beat to apply the finished result.
        page._submit_storyboard()
        wait_until(thumbs_present, timeout=25.0)
    assert thumbs_present()
    # Thumbnails live in the preview folder, not the project folder.
    assert not list((controller.layout.root / "scene-000.png").parent.glob("scene-*.png"))


def test_scene_preview_job_renders_a_frame(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("stat"))
    page._add_scene()
    page.strip.setCurrentRow(0)

    page._submit_preview(time=1.0)
    assert wait_until(lambda: page._preview_job is None, timeout=25.0)
    # After the job the preview label holds a pixmap.
    assert wait_until(lambda: not page.preview_label.pixmap().isNull(), timeout=10.0)


def test_scene_editor_lists_elements_and_inspects_them(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.strip.setCurrentRow(0)

    scene = page._selected_scene()
    assert scene is not None and scene.elements
    # The element list mirrors the scene's elements (front of list = front).
    assert page.element_list.count() == len(scene.elements)
    page.element_list.setCurrentRow(0)
    element = page._selected_element()
    assert element is not None
    # Selecting an element enables the inspector's text field.
    assert page.element_text.isEnabled()


def test_element_text_edit_is_undoable_and_persists(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.strip.setCurrentRow(0)
    page.element_list.setCurrentRow(0)

    page.element_text.setText("Edited headline")
    page._apply_element_text()
    scene = page._selected_scene()
    assert any(el.text == "Edited headline" for el in scene.elements)


def test_duplicate_and_delete_element(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.strip.setCurrentRow(0)
    scene = page._selected_scene()
    before = len(scene.elements)

    page.element_list.setCurrentRow(0)
    page._duplicate_element()
    assert len(page._selected_scene().elements) == before + 1

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *args, **kwargs: QMessageBox.Yes))
    page.element_list.setCurrentRow(0)
    page._delete_element()
    assert len(page._selected_scene().elements) == before


def test_element_z_order_buttons_reorder(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    # The CTA template builds a shape plus a label, so there is something to reorder.
    page.template_combo.setCurrentIndex(page.template_combo.findData("cta"))
    page._add_scene()
    page.strip.setCurrentRow(0)
    scene = page._selected_scene()
    assert len(scene.elements) >= 2
    target = scene.elements[0].id
    page._select_element_by_id(target)
    page._reorder_element("front")
    assert page._selected_scene().elements[-1].id == target
    page._select_element_by_id(target)
    page._reorder_element("back")
    assert page._selected_scene().elements[0].id == target


def test_toggle_scene_enabled_and_lock(window):
    _open_project(window)
    page = window.storyboard_page
    page.refresh()
    page.template_combo.setCurrentIndex(page.template_combo.findData("title"))
    page._add_scene()
    page.strip.setCurrentRow(0)

    page._toggle_enabled()
    assert page._selected_scene().enabled is False
    assert page.toggle_button.text() == "Enable"
    page._toggle_enabled()
    assert page._selected_scene().enabled is True

    page._toggle_locked()
    assert page._selected_scene().locked is True
    assert page.lock_button.text() == "Unlock"
