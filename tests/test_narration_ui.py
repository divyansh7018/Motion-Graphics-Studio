"""The Script and Narration pages (directive sections 8-10, 44-47, 54).

These run with Qt's offscreen platform, against a real project in ``tmp_path``.
The point is not pixel layout: it is that every control does something real, that
nothing is shown as available when it is not, and that a missing engine produces
an explanation rather than a dead button.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.paths import AppPaths
from app.core.settings import SettingsStore
from app.jobs.manager import JobManager
from app.ui.context import AppContext
from app.ui.project_controller import ProjectController
from app.ui.views.narration_view import DEFAULT_PREVIEW_TEXT, NarrationPage
from app.ui.views.script_view import ScriptPage

VOICE_IDS = ["af_bella", "am_adam", "hf_alpha", "hm_ishaan"]


@pytest.fixture()
def context(tmp_path: Path, qapp):
    """A real application context pointed at a throw-away data folder."""
    paths = AppPaths(data_root=tmp_path / "appdata",
                     source_root=Path(__file__).resolve().parents[1],
                     reason="pytest-ui")
    paths.ensure()
    store = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    settings = store.load().settings
    jobs = JobManager()
    instance = AppContext(paths=paths, settings_store=store, settings=settings, jobs=jobs)
    instance.projects = ProjectController(instance)
    return instance


@pytest.fixture()
def kokoro_dir(tmp_path: Path) -> Path:
    root = tmp_path / "models" / "kokoro"
    (root / "voices").mkdir(parents=True)
    (root / "kokoro-82m-v1.0.onnx").write_bytes(b"ONNXFAKE" * 4096)
    for voice in VOICE_IDS:
        (root / "voices" / f"{voice}.pt").write_bytes(b"VOICEFAKE" * 64)
    (root / "voices" / "voices.json").write_text(
        json.dumps({"af_bella": {"gender": "female"}, "hm_ishaan": {"gender": "male"}}),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def open_project(context, tmp_path: Path):
    """Create and open a real project so the pages have something to show."""
    folder = tmp_path / "projects"
    folder.mkdir(parents=True, exist_ok=True)
    from app.project.service import CreateRequest

    project = context.projects.service.create_project(
        CreateRequest(name="UI Test", folder=folder)
    )
    assert context.projects.is_open
    return project


def _use_model(context, kokoro_dir: Path) -> None:
    context.settings.voice.model_dir = str(kokoro_dir)


def _scan(context, page) -> None:
    """Run the page's voice scan the way the application does.

    In the application the scan runs on a worker thread and the panel is filled
    in ``on_scan_finished``.  Here the same job body is executed synchronously
    through the real :class:`JobManager`, so the assertions see exactly what the
    user would see once the scan lands.
    """
    from app.tts.jobs import scan_spec

    spec = scan_spec(
        settings=context.settings,
        paths=context.paths,
        favourites=list(getattr(context.settings.voice, "favourite_voices", []) or []),
    )
    spec.payload["model_dir"] = getattr(context.settings.voice, "model_dir", "") or ""
    result = context.jobs.run_synchronously(spec)
    page.on_scan_finished(result)


def _scanned_page(context) -> NarrationPage:
    page = NarrationPage(context)
    _scan(context, page)
    return page


# --------------------------------------------------------------------------
# Script page
# --------------------------------------------------------------------------

def test_the_script_page_explains_itself_with_no_project(context, qapp) -> None:
    page = ScriptPage(context)

    assert "No project is open" in page.status.text()
    assert page.editor.toPlainText() == ""
    assert page.counts_grid.value("Words") == "-"


def test_the_script_page_shows_the_saved_script(context, open_project, qapp) -> None:
    context.projects.service.set_script_text("नमस्ते। This is the script.")
    page = ScriptPage(context)

    assert page.editor.toPlainText() == "नमस्ते। This is the script."


def test_counts_update_as_the_user_types(context, open_project, qapp) -> None:
    page = ScriptPage(context)

    page.editor.setPlainText("One two three four five.")

    assert page.counts_grid.value("Words") == "5"
    assert "estimated" in page.counts_grid.value("Estimated spoken length").lower()


def test_the_duration_is_labelled_as_an_estimate(context, open_project, qapp) -> None:
    """Section 17: the estimate must never look like a measured duration."""
    page = ScriptPage(context)
    page.editor.setPlainText("Some words here to estimate.")

    assert "estimated" in page.counts_grid.value("Estimated spoken length").lower()


def test_applying_stores_the_script_exactly_as_typed(context, open_project, qapp) -> None:
    page = ScriptPage(context)
    text = "Line one.\n\nनमस्ते — with “quotes” and 1,000 numbers."

    page.editor.setPlainText(text)
    page._apply()

    stored = context.projects.project.script.source_text
    assert stored == text, "the script must never be reformatted"


def test_converting_to_scenes_keeps_every_word(context, open_project, qapp) -> None:
    page = ScriptPage(context)
    page.editor.setPlainText("First paragraph here.\n\nSecond paragraph here.")

    page._convert_to_scenes()
    converted = page.editor.toPlainText()

    assert "[SCENE 01]" in converted
    assert "First paragraph here." in converted
    assert "Second paragraph here." in converted


def test_a_structured_script_is_detected_on_load(context, open_project, qapp) -> None:
    context.projects.service.set_script_text(
        "[SCENE 01]\n\nNarration:\nHello there.\n\nOn Screen:\nA headline.\n"
    )
    page = ScriptPage(context)

    assert page.mode_combo.currentData() == "structured"
    assert page.convert_button.isEnabled() is False


def test_importing_replaces_only_the_editor_not_the_project(context, open_project, tmp_path, qapp) -> None:
    """The project changes only when the user presses Apply (section 19)."""
    source = tmp_path / "incoming.txt"
    source.write_text("Imported text.", encoding="utf-8")
    context.projects.service.set_script_text("Original.")
    page = ScriptPage(context)
    page.editor.setPlainText("")

    from app.script.io import import_script

    result = import_script(source)
    assert result.ok
    page.editor.setPlainText(result.text)

    assert context.projects.project.script.source_text == "Original."
    page._apply()
    assert context.projects.project.script.source_text == "Imported text."


def test_export_refuses_to_overwrite_an_existing_file(context, open_project, tmp_path, qapp) -> None:
    from app.script.io import export_script

    target = tmp_path / "existing.txt"
    target.write_text("keep me", encoding="utf-8")
    context.projects.service.set_script_text("New text.")

    result = export_script(target, context.projects.project.script.source_text)

    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "keep me"


def test_the_script_page_shows_narration_state(context, open_project, qapp) -> None:
    page = ScriptPage(context)

    assert page.narration_grid.value("Status").lower().startswith("not generated")
    assert "No narration" in page.narration_hint.text()


# --------------------------------------------------------------------------
# Narration page - no engine installed
# --------------------------------------------------------------------------

def test_the_narration_page_reports_a_missing_engine(context, qapp, monkeypatch) -> None:
    # Forced, not assumed: the real package may be installed on this machine.
    import app.tts.capabilities as capabilities

    monkeypatch.setattr(
        capabilities, "probe_package",
        lambda name="kokoro": (False, "", "No module named 'kokoro'"),
    )
    page = _scanned_page(context)

    assert page.engine_grid.value("Package") == "not installed"
    assert page.engine_grid.value("Voices discovered") == "0"
    assert page.voice_table.rowCount() == 0


def test_buttons_are_disabled_with_a_reason_when_no_voice_exists(context, qapp) -> None:
    """Section 54: no dead buttons - disabled controls must say why."""
    page = _scanned_page(context)

    assert page.generate_button.isEnabled() is False
    assert page.preview_button.isEnabled() is False
    assert page.generate_button.toolTip(), "a disabled button must explain itself"


def test_generating_without_a_project_is_refused(context, qapp) -> None:
    page = _scanned_page(context)

    page._generate()

    assert "Open a project" in page.status_hint.text()


# --------------------------------------------------------------------------
# Narration page - a model folder is present
# --------------------------------------------------------------------------

def test_voices_are_listed_from_the_model_folder(context, kokoro_dir, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)

    assert page.voice_table.rowCount() == len(VOICE_IDS)
    assert page.engine_grid.value("Voices discovered") == str(len(VOICE_IDS))


def test_the_language_filter_narrows_the_voice_list(context, kokoro_dir, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)
    index = page.language_combo.findData("h")
    assert index >= 0, "Hindi voices exist in the fixture"
    assert page.language_combo.itemText(index) == "Hindi"

    page.language_combo.setCurrentIndex(index)

    listed = [page.voice_table.item(row, 0).text() for row in range(page.voice_table.rowCount())]
    assert listed == ["hf alpha", "hm ishaan"]


def test_the_gender_filter_narrows_the_voice_list(context, kokoro_dir, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)
    page.language_combo.setCurrentIndex(page.language_combo.findData("h"))

    page.gender_combo.setCurrentIndex(page.gender_combo.findData("male"))

    listed = [page.voice_table.item(row, 0).text() for row in range(page.voice_table.rowCount())]
    assert listed == ["hm ishaan"]


def test_search_narrows_the_voice_list(context, kokoro_dir, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)

    page.search_edit.setText("bella")

    assert page.voice_table.rowCount() == 1
    assert "bella" in page.voice_table.item(0, 0).text()

    page.search_edit.setText("zzzznothing")
    assert page.voice_table.rowCount() == 0
    assert "No voices match" in page.voice_hint.text()


def test_a_voice_can_be_selected_into_the_project(context, kokoro_dir, open_project, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)
    page.voice_table.selectRow(0)

    page._apply_voice()

    assert context.projects.project.voice.voice == "af_bella"
    assert "af_bella" in page.voice_hint.text()


def test_changing_language_clears_an_incompatible_voice(context, kokoro_dir, open_project, qapp) -> None:
    """Section 7: a language change must not leave a mismatched voice."""
    _use_model(context, kokoro_dir)
    context.projects.service.set_voice_settings(voice="af_bella", language="a")
    page = _scanned_page(context)

    page.language_combo.setCurrentIndex(page.language_combo.findData("h"))

    assert context.projects.project.voice.voice == "", "the incompatible voice is cleared"
    assert "cleared" in page.voice_hint.text()


def test_a_favourite_can_be_toggled(context, kokoro_dir, qapp) -> None:
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)
    page.voice_table.selectRow(0)

    page._toggle_favourite()

    assert "af_bella" in context.settings.voice.favourite_voices
    assert "★" in page.voice_table.item(0, 0).text()


def test_technical_details_are_hidden_until_asked_for(context, kokoro_dir, qapp) -> None:
    """Section 47: voice ids belong in an optional advanced view."""
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)

    assert page.advanced_grid.isVisibleTo(page) is False

    page.advanced_box.setChecked(True)
    page.voice_table.selectRow(0)
    page._refresh_advanced()

    assert page.advanced_grid.isVisibleTo(page) is True
    assert page.advanced_grid.value("Voice id") == "af_bella"


def test_the_preview_text_defaults_to_something_sensible(context, qapp) -> None:
    page = _scanned_page(context)

    assert page.preview_edit.toPlainText() == DEFAULT_PREVIEW_TEXT


def test_preview_without_a_voice_asks_for_one(context, open_project, qapp) -> None:
    page = _scanned_page(context)

    page._preview()

    assert "Choose a voice" in page.preview_hint.text()


def test_speed_presets_stay_inside_the_engine_range(context, open_project, qapp) -> None:
    from app.tts.engine import MAX_SPEED, MIN_SPEED

    page = _scanned_page(context)

    assert page.custom_speed.minimum() == MIN_SPEED
    assert page.custom_speed.maximum() == MAX_SPEED
    for index in range(page.speed_combo.count()):
        value = page.speed_combo.itemData(index)
        assert MIN_SPEED <= value <= MAX_SPEED


def test_changing_speed_is_stored_on_the_project(context, open_project, qapp) -> None:
    page = _scanned_page(context)

    page.custom_speed.setValue(1.25)
    page.volume_spin.setValue(80)

    assert context.projects.project.voice.speed == 1.25
    assert context.projects.project.voice.volume == 0.8


def test_the_status_panel_shows_the_narration_state(context, open_project, qapp) -> None:
    page = _scanned_page(context)

    assert page.status_grid.value("Status").lower().startswith("not generated")
    assert "No narration" in page.status_hint.text()


def test_a_new_voice_file_appears_after_a_refresh(context, kokoro_dir, qapp) -> None:
    """Sections 5-6: refresh must pick up new voices with no code change."""
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)
    assert page.voice_table.rowCount() == len(VOICE_IDS)

    (kokoro_dir / "voices" / "bf_extra.pt").write_bytes(b"VOICEFAKE" * 64)
    page.refresh_catalogue()          # asks the worker for a fresh scan
    _scan(context, page)              # ...which lands here in the test

    assert page.voice_table.rowCount() == len(VOICE_IDS) + 1


# --------------------------------------------------------------------------
# The two core user actions, driven through the real job manager
# --------------------------------------------------------------------------

@pytest.fixture()
def dialogs(monkeypatch):
    """Neutralise modal dialogs and record what they said.

    A real ``QMessageBox`` blocks forever headless, and the tests care about the
    message, not the window.
    """
    from PySide6 import QtWidgets

    shown: list[tuple[str, str]] = []

    def record(kind):
        def handler(*args, **kwargs):
            title = args[1] if len(args) > 1 else ""
            text = args[2] if len(args) > 2 else ""
            shown.append((kind, f"{title} {text}"))
            return QtWidgets.QMessageBox.StandardButton.Ok

        return staticmethod(handler)

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, record(name))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    return shown


@pytest.fixture()
def narration_window(qapp, tmp_path: Path, ready_engine):
    """A real MainWindow with the Narration page, so job dispatch is real.

    ``MainWindow._on_job_finished`` is what routes a finished job back to the
    page.  Testing the page alone would skip that wiring - and the wiring is
    exactly where the Generate crash was hiding.
    """
    from app.core.paths import AppPaths
    from app.jobs.manager import JobManager
    from app.ui.context import StartupInfo, create_context
    from app.ui.main_window import MainWindow

    paths = AppPaths(data_root=tmp_path / "appdata",
                     source_root=Path(__file__).resolve().parents[1], reason="pytest-ui")
    paths.ensure()
    jobs = JobManager()
    context = create_context(paths, jobs, startup=StartupInfo(
        data_root_reason=paths.reason, directories_created=0, stale_temp_removed=0))
    window = MainWindow(context, jobs)
    yield window, context
    window.deleteLater()
    jobs.shutdown(timeout_ms=3000)


def _pick_language(page, code: str) -> None:
    """Choose a narration language in the dropdown the way a user would."""
    index = page.language_combo.findData(code)
    assert index >= 0, f"language {code!r} is not offered"
    page.language_combo.setCurrentIndex(index)


def _choose_voice(page, voice_id: str) -> None:
    """Select a voice in the table the way a user would, then apply it."""
    from PySide6.QtCore import Qt

    for row in range(page.voice_table.rowCount()):
        item = page.voice_table.item(row, 0)
        if item is not None and item.data(Qt.UserRole) == voice_id:
            page.voice_table.selectRow(row)
            page._apply_voice()
            return
    raise AssertionError(f"voice {voice_id!r} is not in the table")


def wait_until(predicate, timeout: float = 20.0) -> bool:
    """Pump the Qt event loop until *predicate* holds, or time runs out."""
    import time

    from PySide6.QtCore import QCoreApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture()
def ready_engine(monkeypatch):
    """Route the job's engine factory to the test double."""
    import app.tts.jobs as tts_jobs

    from tests.fake_tts import FakeKokoroEngine

    engine = FakeKokoroEngine(sample_rate=24000)
    monkeypatch.setattr(tts_jobs, "build_engine", lambda paths=None, model_path=None: engine)
    return engine


def test_generate_runs_one_job_and_produces_ready_narration(
    narration_window, kokoro_dir, dialogs
) -> None:
    """Sections 28-30: one press, one job, real audio, honest status."""
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="Gen Test", folder=kokoro_dir.parent / "Gen Test")
    )
    context.projects.service.set_script_text("Hello. This is a local narration test.")

    page = window.narration_page
    _scan(context, page)
    _pick_language(page, "h")
    _choose_voice(page, "hf_alpha")

    page._generate()

    assert context.jobs.active_count() == 1, "one press starts exactly one job"
    assert not dialogs, f"generation should not need to ask anything: {dialogs}"

    assert wait_until(lambda: context.jobs.active_count() == 0), "the job never finished"
    assert wait_until(lambda: page.status_grid.value("Status").lower().startswith("ready")), \
        f"narration never became ready: {page.status_hint.text()}"

    wav = context.projects.layout.root / "audio" / "narration" / "narration_full.wav"
    assert wav.is_file()
    assert page.generate_button.isEnabled() is True, "the button must come back"


def test_generate_refuses_when_the_script_is_empty(
    narration_window, kokoro_dir, dialogs
) -> None:
    """Section 48: a clear explanation instead of a silent no-op."""
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="Empty Test", folder=kokoro_dir.parent / "Empty Test")
    )
    context.projects.service.set_script_text("")

    page = window.narration_page
    _scan(context, page)
    _pick_language(page, "h")
    _choose_voice(page, "hf_alpha")

    page._generate()

    assert any("nothing to narrate" in text.lower() for _kind, text in dialogs), dialogs
    assert context.jobs.active_count() == 0, "no job may be started"


def test_a_second_generate_is_refused_while_one_is_running(
    narration_window, kokoro_dir, dialogs
) -> None:
    """Sections 28-29: no duplicate generation, no second model instance."""
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="Twice Test", folder=kokoro_dir.parent / "Twice Test")
    )
    context.projects.service.set_script_text("A sentence that takes a moment to speak.")

    page = window.narration_page
    _scan(context, page)
    _pick_language(page, "h")
    _choose_voice(page, "hf_alpha")

    page._generate()
    page.generate_button.setEnabled(True)   # pretend the user clicked again
    page._generate()

    assert context.jobs.active_count() <= 1, "still at most one job"
    wait_until(lambda: context.jobs.active_count() == 0)


def test_preview_speaks_without_touching_the_project(
    narration_window, kokoro_dir, dialogs
) -> None:
    """Sections 9-10: preview is separate from generation and leaves no trace."""
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="Prev Test", folder=kokoro_dir.parent / "Prev Test")
    )
    context.projects.service.set_script_text("A script that must not be narrated here.")

    page = window.narration_page
    _scan(context, page)
    _pick_language(page, "h")
    _choose_voice(page, "hf_alpha")
    page.preview_edit.setPlainText("A short preview sentence.")
    before = sorted(p.name for p in context.projects.layout.root.rglob("*.wav"))

    page._preview()
    assert wait_until(lambda: "Spoke" in page.preview_hint.text()), page.preview_hint.text()

    after = sorted(p.name for p in context.projects.layout.root.rglob("*.wav"))
    assert after == before, "a preview must never write into the project"
    assert "Nothing was added to the project" in page.preview_hint.text()


def test_preview_without_text_explains_instead_of_failing(
    narration_window, kokoro_dir, dialogs
) -> None:
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="NoText Test", folder=kokoro_dir.parent / "NoText Test")
    )

    page = window.narration_page
    _scan(context, page)
    _choose_voice(page, "hf_alpha")
    page.preview_edit.setPlainText("   ")

    page._preview()

    assert "Type something" in page.preview_hint.text()
    assert context.jobs.active_count() == 0


# --------------------------------------------------------------------------
# regression: the documented Language -> Voice -> Generate flow
# --------------------------------------------------------------------------

def test_choosing_a_language_records_it_on_the_project(context, open_project, kokoro_dir, qapp) -> None:
    """BUG REGRESSION: picking a language only changed the dropdown.

    ``project.voice.language`` stayed at the project default, so generating with
    a voice for the newly chosen language was refused with "the voice does not
    support the selected language ... change the language" - telling the user to
    do the thing they had just done (section 7).
    """
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)

    assert context.projects.project.voice.language != "h"
    _pick_language(page, "h")

    assert context.projects.project.voice.language == "h"


def test_choosing_a_voice_with_all_languages_adopts_its_language(
    context, open_project, kokoro_dir, qapp
) -> None:
    """With no language filter, the voice's own language becomes the setting."""
    _use_model(context, kokoro_dir)
    page = _scanned_page(context)

    index = page.language_combo.findData("all")
    page.language_combo.setCurrentIndex(index)
    _choose_voice(page, "hf_alpha")

    assert context.projects.project.voice.voice == "hf_alpha"
    assert context.projects.project.voice.language == "h"


def test_generate_is_reachable_from_a_real_window(narration_window, kokoro_dir, dialogs) -> None:
    """BUG REGRESSION: pressing Generate raised AttributeError.

    ``_generate`` called ``mark_narration_generating`` on the ProjectController,
    which does not have it - the method lives on ProjectService.  Every Generate
    click crashed.  Only an end-to-end test through the window catches that.
    """
    window, context = narration_window
    from app.project.service import CreateRequest

    context.settings.voice.model_dir = str(kokoro_dir)
    context.projects.service.create_project(
        CreateRequest(name="Reach Test", folder=kokoro_dir.parent / "Reach Test")
    )
    context.projects.service.set_script_text("Hello. This is a local narration test.")

    page = window.narration_page
    _scan(context, page)
    _pick_language(page, "h")
    _choose_voice(page, "hf_alpha")

    page._generate()      # must not raise

    assert not dialogs, f"no dialog should be needed: {dialogs}"
    assert context.jobs.active_count() == 1
    assert wait_until(lambda: context.jobs.active_count() == 0)
    assert (context.projects.layout.root / "audio" / "narration" / "narration_full.wav").is_file()
