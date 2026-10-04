"""GUI smoke tests (directive sections 7, 45, 46, 53, 66, 70).

These run with Qt's ``offscreen`` platform so no window appears on a build
machine.  They verify that the shell can be built, that pages can be switched,
that background work keeps the interface responsive, and that closing is clean.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.jobs.manager import JobManager  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402


def process_events(seconds: float) -> None:
    """Run the event loop for *seconds* (used for short, fixed waits)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 20.0) -> bool:
    """Run the event loop until *predicate* is true.  Returns whether it became true.

    Waiting on the condition instead of a fixed duration keeps the tests fast and
    independent of how slow the machine is - a fixed sleep would either be slow
    or flaky.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture()
def window(qapp, paths):
    jobs = JobManager()
    context = create_context(paths, jobs, startup=StartupInfo(data_root_reason="pytest", directories_created=0, stale_temp_removed=0))
    main = MainWindow(context, jobs)
    yield main
    if main.isVisible():
        main.close()
    jobs.shutdown(timeout_ms=4000)
    main.deleteLater()
    process_events(0.1)


def test_main_window_builds_with_every_page(window) -> None:
    assert window.windowTitle()
    for key in ("welcome", "system_check", "settings", "maintenance", "diagnostics"):
        assert key in window._pages, f"page '{key}' is missing"


def test_navigation_switches_pages(window) -> None:
    window.show_page("settings")
    assert window.stack.currentWidget() is window._pages["settings"]

    window.show_page("diagnostics")
    assert window.stack.currentWidget() is window._pages["diagnostics"]

    window.show_page("welcome")
    assert window.stack.currentWidget() is window._pages["welcome"]


def test_future_pages_are_disabled_not_fake_buttons(window) -> None:
    """Unimplemented workflow pages must be visibly unavailable (section 54)."""
    from PySide6.QtCore import Qt

    future_labels = []
    for row in range(window.nav.count()):
        item = window.nav.item(row)
        if item.text().endswith("(later)") or "(later)" in item.text():
            future_labels.append(item)
            assert not (item.flags() & Qt.ItemIsEnabled), "future pages must not be clickable"
            assert item.toolTip(), "future pages must explain why they are unavailable"
    assert future_labels, "the roadmap entries should be visible"

    # And the File menu entries for later stages are disabled with a tooltip.
    assert not window.new_project_action.isEnabled()
    assert "Stage B" in window.new_project_action.toolTip()


def test_system_check_runs_as_a_background_job(window) -> None:
    results = []
    window.jobs.job_finished.connect(results.append)

    window.run_system_check(deep=False)
    assert window.jobs.active_count() == 1, "the check must run as a job"

    # While it runs, the interface is still responsive: we can switch pages.
    window.show_page("welcome")
    assert window.stack.currentWidget() is window._pages["welcome"]

    assert wait_until(lambda: len(results) >= 1), "the check did not finish"
    process_events(0.2)

    assert len(results) == 1
    assert results[0].succeeded
    report = results[0].value
    assert report is not None
    assert window.context.last_report is report
    assert "ready" in window.readiness_label.text().lower()


def test_duplicate_system_check_clicks_start_only_one_job(window) -> None:
    results = []
    window.jobs.job_finished.connect(results.append)

    window.run_system_check(deep=False)
    window.run_system_check(deep=False)   # second click
    window.run_system_check(deep=False)   # third click

    assert wait_until(lambda: len(results) >= 1)
    process_events(0.5)

    assert len(results) == 1, "three clicks must not produce three jobs/results"
    assert window.jobs.active_count() == 0


def test_theme_switch_is_applied_and_saved(window, data_root) -> None:
    window._set_theme("light")
    assert window.context.settings.general.theme == "light"
    assert window.context.settings_store.load().settings.general.theme == "light"

    window._set_theme("dark")
    assert window.context.settings.general.theme == "dark"


def test_advanced_mode_toggle_is_persisted(window) -> None:
    window._toggle_advanced_mode(True)
    assert window.context.settings.general.advanced_mode is True
    assert window.context.settings_store.load().settings.general.advanced_mode is True


def test_window_state_is_remembered_on_close(qapp, paths) -> None:
    jobs = JobManager()
    context = create_context(paths, jobs)
    window = MainWindow(context, jobs)
    window.show_page("maintenance")
    window.show()
    process_events(0.1)

    window.close()
    process_events(0.2)

    saved = context.settings_store.load().settings.window
    assert saved.last_page == "maintenance"
    assert saved.width >= 1024
    jobs.shutdown(timeout_ms=2000)


def test_closing_cancels_running_jobs_without_hanging(qapp, paths) -> None:
    """Section 70: no zombie work, no frozen close."""
    from app.jobs.spec import JobSpec

    jobs = JobManager()
    context = create_context(paths, jobs)
    window = MainWindow(context, jobs)

    def long_task(context):
        for _ in range(10000):
            context.raise_if_cancelled()
            time.sleep(0.001)

    jobs.submit(JobSpec(key="gui.long", title="Long task", body=long_task))
    process_events(0.2)

    # closeEvent asks for confirmation when work is running; the test answers
    # the same way a user would ("cancel and close") by disabling the question.
    from app.ui import main_window as main_window_module

    original = main_window_module.ask_confirm
    main_window_module.ask_confirm = lambda *args, **kwargs: True
    try:
        started = time.monotonic()
        window.close()
        process_events(0.5)
        elapsed = time.monotonic() - started
    finally:
        main_window_module.ask_confirm = original

    assert elapsed < 10.0, "closing must not hang"
    assert jobs.active_count() == 0, "running jobs must be stopped"


def test_settings_page_collects_and_applies_without_losing_values(window) -> None:
    page = window.settings_page
    page.load_from_settings()

    page.speed_spin.setValue(1.4)
    page.crf_spin.setValue(18)
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))

    assert page.apply() is True

    settings = window.context.settings
    assert settings.voice.speed == pytest.approx(1.4)
    assert settings.media.crf == 18
    assert settings.general.theme == "light"

    # Values survive a reload from disk.
    reloaded = window.context.settings_store.load().settings
    assert reloaded.voice.speed == pytest.approx(1.4)
    assert reloaded.media.crf == 18


def test_settings_page_clamps_impossible_values(window) -> None:
    page = window.settings_page
    page.load_from_settings()
    page.width_spin.setValue(256)
    page.height_spin.setValue(256)
    assert page.apply() is True
    settings = window.context.settings
    assert settings.project_defaults.width == 256
    assert settings.project_defaults.height == 256


def test_welcome_page_reflects_the_report(window) -> None:
    window.run_system_check(deep=False)
    assert wait_until(lambda: window.context.last_report is not None)
    window.welcome_page.refresh()
    assert window.context.last_report is not None


def test_system_check_page_survives_repeated_reports(window) -> None:
    """Re-running the check must not destroy and re-add the page's placeholder.

    Regression test: clearing the results removed *every* widget in the layout,
    including the "nothing checked yet" label, which scheduled it for deletion
    and then put it back.  The second report then used a widget whose C++ object
    was already gone.
    """
    shiboken6 = pytest.importorskip("shiboken6")

    results = []
    window.jobs.job_finished.connect(results.append)

    window.run_system_check(deep=False)
    assert wait_until(lambda: len(results) >= 1)
    process_events(0.2)

    placeholder = window.system_check_page.empty_label
    assert shiboken6.isValid(placeholder)
    assert not placeholder.isVisible()

    window.run_system_check(deep=False)
    assert wait_until(lambda: len(results) >= 2)
    process_events(0.2)

    # Give any pending deleteLater() a chance to run before checking again.
    process_events(0.2)

    assert shiboken6.isValid(window.system_check_page.empty_label), (
        "the placeholder label was deleted by the second set of results"
    )
    assert not window.system_check_page.empty_label.isVisible()
