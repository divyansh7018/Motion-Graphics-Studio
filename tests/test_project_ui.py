"""Interface tests for the Stage B pages (directive sections 3-31, 54).

These drive real widgets with a real data folder, but never a modal dialog:
the ask_* helpers are patched so a test can state which button the user pressed
instead of blocking on ``exec()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PySide6.QtWidgets import QApplication

from app.core.paths import AppPaths
from app.core.settings import Settings
from app.jobs.manager import JobManager
from app.project.service import CreateRequest, ProjectService
from app.ui.context import StartupInfo, create_context
from app.ui import project_controller as pc
from app.ui.dialogs import project_dialogs as dialogs
from app.ui.main_window import MainWindow
from app.ui.wizard.new_project import NewProjectWizard
from app.tools.kokoro import VoiceCatalogue, VoiceInfo

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_window(tmp_path: Path, monkeypatch) -> MainWindow:
    paths = AppPaths(
        data_root=tmp_path / "data",
        source_root=Path(__file__).resolve().parents[1],
        reason="unit test",
    )
    paths.ensure()
    jobs = JobManager()
    context = create_context(
        paths,
        jobs,
        startup=StartupInfo(data_root_reason=paths.reason, directories_created=0, stale_temp_removed=0),
    )
    # The controller imported the dialog helpers by name, so patch them where
    # they are used - otherwise a real modal dialog blocks the test forever.
    monkeypatch.setattr(pc, "ask_unsaved_changes", lambda *a, **k: dialogs.UnsavedChoice.SAVE)
    monkeypatch.setattr(pc, "ask_recovery", lambda *a, **k: dialogs.RecoveryChoice.IGNORE)
    monkeypatch.setattr(pc, "ask_external_change", lambda *a, **k: dialogs.ConflictChoice.CANCEL)
    monkeypatch.setattr(
        pc, "ask_missing_asset", lambda *a, **k: (dialogs.MissingAssetChoice.CANCEL, None)
    )
    monkeypatch.setattr(pc, "ask_confirm", lambda *a, **k: True)
    return MainWindow(context, jobs)


def create_project(tmp_path: Path, name: str = "UI Project", **kwargs):
    """Create a project directly through the service (no dialogs)."""
    paths = AppPaths(
        data_root=tmp_path / "data",
        source_root=Path(__file__).resolve().parents[1],
        reason="unit test",
    )
    paths.ensure()
    service = ProjectService(paths, Settings())
    project = service.create_project(CreateRequest(name=name, **kwargs))
    return service, project, paths


def catalogue_with_voices() -> VoiceCatalogue:
    return VoiceCatalogue(
        voices=[
            VoiceInfo(id="af_heart", language="en-us", gender="female", source="test"),
            VoiceInfo(id="am_adam", language="en-us", gender="male", source="test"),
            VoiceInfo(id="bf_emma", language="en-gb", gender="female", source="test"),
        ],
        source="test",
    )


def wait_for(condition, timeout: float = 5.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

def test_dashboard_offers_new_and_open_project(tmp_path, monkeypatch) -> None:
    window = make_window(tmp_path, monkeypatch)
    page = window.welcome_page

    assert page.new_button.isEnabled()
    assert page.open_button.isEnabled()
    assert "Dashboard" in page.title_label.text()


def test_dashboard_lists_recent_projects_as_cards(tmp_path, monkeypatch) -> None:
    service, _project, paths = create_project(tmp_path, "Carded Project", channel_name="YouTube")
    window = make_window(tmp_path, monkeypatch)
    # Point the window at the same data root the project was created in.
    window.context.paths.__dict__.update(paths.__dict__)
    window.context.projects.service.paths = paths
    window.welcome_page._refresh_recent()

    assert len(window.welcome_page.cards) == 1
    card = window.welcome_page.cards[0]
    assert "Carded Project" in card.name.text()
    assert "YouTube" in card.meta.text()
    assert "No preview yet" in card.thumb.text()


def test_removing_a_project_from_recent_keeps_its_files(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Kept Files")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service.paths = paths
    window.welcome_page._refresh_recent()
    assert len(window.welcome_page.cards) == 1

    entry = window.welcome_page.cards[0].entry
    window.welcome_page._on_remove(entry)

    assert window.welcome_page.cards == []
    assert (paths.projects_dir / "Kept Files" / "project.json").is_file(), "files must survive"


# --------------------------------------------------------------------------
# wizard
# --------------------------------------------------------------------------

def test_wizard_has_nine_steps(tmp_path) -> None:
    wizard = NewProjectWizard([], VoiceCatalogue(), Settings())
    assert wizard.pageIds().__len__() == 9
    assert "1. Name your project" in wizard.page(0).title()
    assert "9. Create the project" in wizard.page(8).title()


def test_wizard_refuses_names_it_cannot_use(tmp_path) -> None:
    wizard = NewProjectWizard([], VoiceCatalogue(), Settings())
    wizard.name_page.name_edit.setText("")
    assert not wizard.name_page.isComplete()
    assert "needs a name" in wizard.name_page.problem.text()

    wizard.name_page.name_edit.setText("CON")
    assert not wizard.name_page.isComplete()
    assert "reserved Windows name" in wizard.name_page.problem.text()

    wizard.name_page.name_edit.setText("...")
    assert not wizard.name_page.isComplete()

    wizard.name_page.name_edit.setText("Proper Name")
    assert wizard.name_page.isComplete()
    assert wizard.name_page.problem.text() == ""


def test_wizard_shows_the_folder_name_it_will_use(tmp_path) -> None:
    """Characters Windows forbids in a folder name are replaced, not hidden."""
    wizard = NewProjectWizard([], VoiceCatalogue(), Settings())
    wizard.name_page.name_edit.setText("A/valid:name?")
    assert wizard.name_page.isComplete()
    assert "A_valid_name" in wizard.name_page.folder_hint.text()


def test_wizard_produces_the_chosen_settings(tmp_path) -> None:
    wizard = NewProjectWizard([], catalogue_with_voices(), Settings())
    wizard.name_page.name_edit.setText("Vertical Short")
    wizard.template_page.radios.set_current("shorts")
    wizard.format_page.radios.set_current("9:16")
    wizard.resolution_page.initializePage()
    wizard.resolution_page.width_spin.setValue(1080)
    wizard.resolution_page.height_spin.setValue(1920)
    wizard.fps_page.radios.set_current("60")
    wizard.quality_page.radios.set_current("ultra")
    wizard.voice_page._refresh_languages()
    wizard.voice_page.language_combo.setCurrentIndex(
        wizard.voice_page.language_combo.findData("en-gb")
    )
    wizard.voice_page._refresh_voices()
    wizard.voice_page.voice_combo.setCurrentIndex(
        wizard.voice_page.voice_combo.findData("bf_emma")
    )

    request = wizard.create_request()

    assert request.name == "Vertical Short"
    assert request.template == "shorts"
    assert (request.width, request.height, request.fps) == (1080, 1920, 60)
    assert request.quality["quality_preset"] == "ultra"
    assert request.voice == "bf_emma"
    assert request.language == "en-gb"
    assert request.gender == "female"


def test_wizard_shows_only_voices_that_exist(tmp_path) -> None:
    empty = NewProjectWizard([], VoiceCatalogue(reason="nothing installed"), Settings())
    assert empty.voice_page.voice_combo.count() == 1, "only 'first available'"
    assert "nothing installed" in empty.voice_page.status.text()

    full = NewProjectWizard([], catalogue_with_voices(), Settings())
    assert full.voice_page.voice_combo.count() == 4
    assert full.voice_page.language_combo.findData("en-us") >= 0


def test_wizard_never_invents_a_scene_or_a_media_file(tmp_path, monkeypatch) -> None:
    service, _project, paths = create_project(tmp_path, "Placeholder Check")
    assert service.current.scenes == []
    assert service.current.assets == []
    assert not list((paths.projects_dir / "Placeholder Check" / "assets").iterdir())


# --------------------------------------------------------------------------
# project page
# --------------------------------------------------------------------------

def test_project_page_shows_the_open_project(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Shown Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service

    window.open_project_path(paths.projects_dir / "Shown Project")

    page = window.project_page
    assert page.project is not None
    assert page.header_grid.value("Name").startswith("Shown Project")
    assert page.script_edit.toPlainText() == project.script.source_text
    assert window.save_project_action.isEnabled()


def test_editing_the_script_marks_the_project_dirty(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Dirty Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Dirty Project")
    assert not window.context.projects.dirty

    window.project_page.script_edit.setPlainText("New narration.")
    window.project_page._apply_script()

    assert window.context.projects.dirty
    assert "*" in window.windowTitle()


def test_saving_writes_the_script_and_clears_the_dirty_flag(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Saved Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Saved Project")
    window.project_page.script_edit.setPlainText("Saved narration text.")
    window.project_page._apply_script()

    window.save_project()

    assert not window.context.projects.dirty
    assert "*" not in window.windowTitle()
    stored = (paths.projects_dir / "Saved Project" / "project.json").read_text(encoding="utf-8")
    assert "Saved narration text." in stored
    assert (paths.projects_dir / "Saved Project" / "script.txt").read_text(encoding="utf-8") == "Saved narration text."


def test_unsaved_changes_dialog_can_cancel_the_close(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Cancel Close")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Cancel Close")
    window.project_page.script_edit.setPlainText("Unsaved work.")
    window.project_page._apply_script()

    monkeypatch.setattr(pc, "ask_unsaved_changes", lambda *a, **k: dialogs.UnsavedChoice.CANCEL)
    assert not window.context.projects.close(window)
    assert window.context.projects.is_open

    monkeypatch.setattr(pc, "ask_unsaved_changes", lambda *a, **k: dialogs.UnsavedChoice.DISCARD)
    assert window.context.projects.close(window)
    assert not window.context.projects.is_open


def test_project_settings_changes_persist_after_save(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Settings Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Settings Project")

    settings_page = window.project_settings_page
    settings_page.fps_combo.setCurrentIndex(settings_page.fps_combo.findData(60))
    settings_page.quality_combo.setCurrentIndex(settings_page.quality_combo.findData("ultra"))
    settings_page.width_spin.setValue(3840)
    settings_page.height_spin.setValue(2160)
    window._save_project_settings()

    import json

    stored = json.loads(
        (paths.projects_dir / "Settings Project" / "project.json").read_text(encoding="utf-8")
    )
    fmt = stored["format"]
    assert (fmt["width"], fmt["height"]) == (3840, 2160)
    assert fmt["fps"] == 60
    assert fmt["quality_preset"] == "ultra"


def test_settings_page_only_offers_codecs_that_fit_the_container(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Codec Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Codec Project")

    page = window.project_settings_page
    page.container_combo.setCurrentText("webm")
    codecs = [page.codec_combo.itemData(i) for i in range(page.codec_combo.count())]
    assert "h264_cpu" not in codecs
    assert "vp9_cpu" in codecs


def test_discard_restores_the_saved_settings(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Discard Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Discard Project")

    window.project_settings_page.fps_combo.setCurrentIndex(
        window.project_settings_page.fps_combo.findData(24)
    )
    window._save_project_settings()
    assert window.context.projects.project.format.fps == 24

    window.project_settings_page.fps_combo.setCurrentIndex(
        window.project_settings_page.fps_combo.findData(50)
    )
    window._discard_project_settings()

    assert window.context.projects.project.format.fps == 24


# --------------------------------------------------------------------------
# recovery + missing assets through the interface
# --------------------------------------------------------------------------

def test_recovery_dialog_can_restore_work(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Recover UI")
    service.save()
    service.set_script_text("Work that survived the crash.")
    service.autosave()
    service.close_project()

    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    choices: list = []
    monkeypatch.setattr(
        pc,
        "ask_recovery",
        lambda parent, candidate: choices.append(candidate) or dialogs.RecoveryChoice.RESTORE,
    )

    found = window.context.projects.check_recovery(window)

    assert found is not None
    assert choices, "the dialog must be offered"
    assert window.context.projects.project.script.source_text == "Work that survived the crash."


def test_recovery_dialog_can_ignore_and_keep_the_file(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Ignore UI")
    service.save()
    service.set_script_text("Ignored work.")
    service.autosave()
    layout = service.current_layout
    service.close_project()

    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    monkeypatch.setattr(pc, "ask_recovery", lambda *a, **k: dialogs.RecoveryChoice.IGNORE)

    assert window.context.projects.check_recovery(window) is None
    assert not window.context.projects.is_open
    assert list(layout.autosave_dir.glob("*.ignored.json")), "ignored data must stay on disk"


def test_missing_asset_dialog_relinks_without_losing_the_reference(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Relink UI")
    source = tmp_path / "logo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    service.import_asset(source)
    asset = service.current.assets[0]
    (service.current_layout.root / asset.path).unlink()

    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(service.current_layout.root)

    # The missing flag only changes when the files are actually re-checked.
    window.project_page._recheck_assets()
    assert window.project_page.project.assets[0].missing

    replacement = tmp_path / "logo-moved.png"
    replacement.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
    monkeypatch.setattr(
        pc,
        "ask_missing_asset",
        lambda parent, name, expected: (dialogs.MissingAssetChoice.RELINK, replacement),
    )

    assert window.context.projects.fix_missing_asset(window, asset.id)
    assert not window.project_page.project.assets[0].missing


def test_external_change_dialog_is_offered_on_conflict(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Conflict UI")
    service.save()
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Conflict UI")

    # Another program rewrites project.json while we have the project open.
    # A bare store is used on purpose: no service, so no lock is taken.
    window.project_page.script_edit.setPlainText("Our edit.")
    window.project_page._apply_script()

    from app.project.store import ProjectStore

    store = ProjectStore()
    folder = paths.projects_dir / "Conflict UI"
    from app.project.layout import ProjectLayout

    loaded = store.load(folder / "project.json")
    assert loaded.project is not None
    external_layout = ProjectLayout.from_project_file(loaded.path)
    loaded.project.script.source_text = "Their edit."
    store.save(loaded.project, external_layout, backup=False)

    offered: list = []

    def fake_conflict(parent, friendly):
        offered.append(friendly)
        return dialogs.ConflictChoice.RELOAD

    monkeypatch.setattr(pc, "ask_external_change", fake_conflict)
    window.save_project()

    assert offered, "the conflict dialog must appear"
    assert not window.context.projects.dirty
    assert window.context.projects.project.script.source_text == "Their edit."


# --------------------------------------------------------------------------
# browser + window plumbing
# --------------------------------------------------------------------------

def test_browser_lists_every_project_in_the_folder(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Browser One")
    service.close_project()
    service.create_project(CreateRequest(name="Browser Two"))
    service.close_project()

    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.projects_page.context.paths = paths
    window.projects_page.scan()
    assert wait_for(lambda: window.projects_page.rows), "the scan job did not finish"

    names = {row["name"] for row in window.projects_page.rows}
    assert {"Browser One", "Browser Two"} <= names


def test_browser_shows_a_folder_without_a_project_file(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Real Project")
    (paths.projects_dir / "Not a project").mkdir(parents=True)

    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.projects_page.context.paths = paths
    window.projects_page.scan()
    assert wait_for(lambda: window.projects_page.rows)

    row = next(row for row in window.projects_page.rows if row["name"] == "Not a project")
    assert row["readable"] is False
    assert row["format"] == "no project.json"


def test_project_actions_track_whether_a_project_is_open(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Actions Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service

    assert not window.save_project_action.isEnabled()
    window.open_project_path(paths.projects_dir / "Actions Project")
    assert window.save_project_action.isEnabled()
    assert window.duplicate_project_action.isEnabled()

    monkeypatch.setattr(pc, "ask_unsaved_changes", lambda *a, **k: dialogs.UnsavedChoice.DISCARD)
    window.close_project()
    assert not window.save_project_action.isEnabled()


def test_recent_menu_is_populated_from_the_recent_list(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Menu Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window._refresh_recent_menu()

    labels = [action.text() for action in window.recent_menu.actions()]
    assert any("Menu Project" in label for label in labels)


def test_closing_the_window_releases_the_project_lock(tmp_path, monkeypatch) -> None:
    service, project, paths = create_project(tmp_path, "Lock Project")
    window = make_window(tmp_path, monkeypatch)
    window.context.projects.service = service
    window.open_project_path(paths.projects_dir / "Lock Project")
    assert (paths.projects_dir / "Lock Project" / ".project.lock.json").is_file()

    window.close()

    assert not window.context.projects.is_open
    assert not (paths.projects_dir / "Lock Project" / ".project.lock.json").exists()
