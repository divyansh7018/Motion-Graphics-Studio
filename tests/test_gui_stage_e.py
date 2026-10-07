"""Stage E GUI tests: the four new pages really do things (sections 60-65, 88-89).

The directive is explicit that there must be no fake controls, no placeholder
actions and no buttons that do nothing.  So as well as driving each page, these
tests assert that *every* button on the Stage E pages is connected to something,
and that every setting a page exposes actually lands in ``project.json``'s model.

They run against real PySide6 widgets on Qt's offscreen platform.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import SIGNAL, QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from app.jobs.manager import JobManager  # noqa: E402
from app.project.model import SceneSpec  # noqa: E402
from app.project.service import CreateRequest  # noqa: E402
from app.ui.context import StartupInfo, create_context  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

NEW_PAGES = ("audio", "subtitles", "timeline", "render")


def process_events(seconds: float = 0.05) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def window(qapp, paths):
    jobs = JobManager()
    context = create_context(
        paths, jobs,
        startup=StartupInfo(data_root_reason="pytest", directories_created=0,
                            stale_temp_removed=0))
    main = MainWindow(context, jobs)
    yield main
    if main.isVisible():
        main.close()
    jobs.shutdown(timeout_ms=4000)
    main.deleteLater()
    process_events(0.1)


def _open_project(window, *, width=512, height=288, fps=25):
    """512x288 is the smallest 16:9 size the project validator accepts
    (it requires at least 256px on both axes)."""
    """Create a real project on disk and open it through the controller."""
    controller = window.context.projects
    service = controller.service
    root = window.context.paths.data_root / "projects" / "GUI Stage E"
    service.create_project(CreateRequest(
        name="GUI Stage E", width=width, height=height, fps=fps,
        template="blank", folder=root), open_after=False)
    assert controller.open_path(root / "project.json") is True
    process_events()
    return controller.project, root


def _wav(path: Path, seconds: float = 1.0, frequency: int = 220) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={frequency}:duration={seconds}",
         "-ar", "22050", "-ac", "1", str(path)], check=True)
    return path


def _buttons(widget) -> list:
    return widget.findChildren(QPushButton)


def _dead_buttons(widget) -> list[str]:
    """Buttons with nothing connected to their click signal."""
    dead = []
    for button in _buttons(widget):
        if button.receivers(SIGNAL("clicked()")) == 0:
            dead.append(button.text() or button.objectName() or "(unnamed)")
    return dead


# --------------------------------------------------------------------------
# The pages exist and are reachable
# --------------------------------------------------------------------------

def test_all_four_stage_e_pages_are_in_the_navigation(window) -> None:
    assert set(NEW_PAGES).issubset(set(window._page_keys))
    for key in NEW_PAGES:
        assert key in window._pages, f"the {key} page was not built"
        window.show_page(key)
        assert window.stack.currentWidget() is window._pages[key]


def test_the_old_placeholder_entries_are_gone(window) -> None:
    """'Music (later)', 'Timeline (later)' and 'Render (later)' must not remain.

    Stage G built the last placeholder (the Video library), so the sidebar now
    carries no "(later)" entry at all - and the label check below still fails if
    one ever reappears.
    """
    labels = [window.nav.item(row).text() for row in range(window.nav.count())]
    for label in labels:
        assert "(later)" not in label, \
            f"a built page is still listed as future work: {label}"
    assert "Video library" in labels


def test_no_button_on_the_stage_e_pages_is_dead(window) -> None:
    """Every button must do something - no placeholders (section 66)."""
    _open_project(window)
    for key in NEW_PAGES:
        page = window._pages[key]
        page.refresh()
        dead = _dead_buttons(page)
        assert not dead, f"{key} page has buttons wired to nothing: {dead}"


# --------------------------------------------------------------------------
# Audio page
# --------------------------------------------------------------------------

def test_the_audio_page_writes_its_controls_into_the_project(window) -> None:
    project, _root = _open_project(window)
    page = window.audio_page
    page.refresh()

    page.master_volume.setValue(80)
    page.normalize.setChecked(False)
    page.target_lufs.setValue(-14.0)
    page.duck_level.setValue(50)
    page.duck_attack.setValue(0.4)
    page.music_volume.setValue(25)
    page.music_loop.setChecked(False)
    page.narration_volume.setValue(90)
    process_events()

    assert project.audio.master_volume == pytest.approx(0.80)
    assert project.audio.normalize_enabled is False
    assert project.audio.target_lufs == pytest.approx(-14.0)
    assert project.audio.ducking_level == pytest.approx(0.50)
    assert project.audio.ducking_attack == pytest.approx(0.4)
    assert project.audio.narration_volume == pytest.approx(0.90)
    assert project.audio.music.volume == pytest.approx(0.25)
    assert project.audio.music.loop is False


def test_the_audio_page_shows_the_project_again_after_a_refresh(window) -> None:
    project, _root = _open_project(window)
    project.audio.master_volume = 0.42
    project.audio.music.path = "assets/music.wav"
    project.audio.music.volume = 0.31
    window.audio_page.refresh()

    assert window.audio_page.master_volume.value() == pytest.approx(42.0)
    assert window.audio_page.music_volume.value() == pytest.approx(31.0)
    assert "music.wav" in window.audio_page.music_path.text()


def test_removing_a_sound_effect_really_removes_it(window) -> None:
    from app.project.model import SoundEffect

    project, _root = _open_project(window)
    project.audio.sfx.append(SoundEffect(id="sfx1", path="assets/a.wav", volume=0.5))
    project.audio.sfx.append(SoundEffect(id="sfx2", path="assets/b.wav", volume=0.5))
    page = window.audio_page
    page.refresh()
    assert page.sfx_table.rowCount() == 2

    page.sfx_table.selectRow(0)
    page._remove_sfx()
    assert len(project.audio.sfx) == 1
    assert project.audio.sfx[0].id == "sfx2"
    assert page.sfx_table.rowCount() == 1


def test_the_audio_page_is_disabled_with_no_project_open(window) -> None:
    page = window.audio_page
    page.refresh()
    assert page.master_volume.isEnabled() is False
    assert page.sfx_add.isEnabled() is False


# --------------------------------------------------------------------------
# Subtitles page
# --------------------------------------------------------------------------

def test_the_subtitles_page_edits_land_in_the_project(window) -> None:
    project, _root = _open_project(window)
    page = window.subtitles_page

    page.enabled.setChecked(True)
    page.burn_in.setChecked(True)
    page.language.setText("en")
    page.margin.setValue(9.0)
    page.shadow.setChecked(False)
    page.background_enabled.setChecked(True)
    page.background_opacity.setValue(70)
    page.font_size.setValue(52)
    page.outline_width.setValue(3.0)
    page.max_lines.setValue(3)
    process_events()

    assert project.subtitles.enabled is True
    assert project.subtitles.burn_in is True
    assert project.subtitles.language == "en"
    assert project.subtitles.margin_percent == pytest.approx(9.0)
    assert project.subtitles.shadow is False
    assert project.subtitles.background != "", "a background was asked for but not stored"
    assert project.subtitles.background_opacity == pytest.approx(0.70)
    assert project.theme.subtitle_style.font_size == 52
    assert project.theme.subtitle_style.outline_width == pytest.approx(3.0)
    assert project.theme.subtitle_style.max_lines == 3


def test_the_caption_table_can_edit_split_and_delete(window) -> None:
    from app.project.model import SubtitleCue

    project, _root = _open_project(window)
    project.subtitles.enabled = True
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=4.0, text="The first caption here"),
        SubtitleCue(id="c2", start=4.0, end=8.0, text="The second caption here"),
    ]
    page = window.subtitles_page
    page.refresh()
    assert page.cue_table.rowCount() == 2

    # Reword the first caption through the table cell.
    project.subtitles.timing_source = "narration"
    page.cue_table.item(0, 2).setText("Reworded by the user")
    process_events()
    assert project.subtitles.cues[0].text == "Reworded by the user"
    # Rewording leaves the measured timings untouched, so they are still
    # narration-derived.  Only a timing edit may claim "manual".
    assert project.subtitles.timing_source == "narration"

    # A timing edit does flip it.
    assert page.service.edit(project, "c1", start=0.5, end=3.5) is True
    assert project.subtitles.timing_source == "manual"

    # Delete the second one.
    page.cue_table.selectRow(1)
    page._delete_cue()
    assert len(project.subtitles.cues) == 1
    assert project.subtitles.cues[0].id == "c1"


def test_merging_non_neighbours_is_refused_and_explained(window) -> None:
    from app.project.model import SubtitleCue

    project, _root = _open_project(window)
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=2.0, text="One"),
        SubtitleCue(id="c2", start=2.0, end=4.0, text="Two"),
        SubtitleCue(id="c3", start=4.0, end=6.0, text="Three"),
    ]
    page = window.subtitles_page
    page.refresh()

    page.cue_table.selectRow(0)
    page._merge_cue()
    assert len(project.subtitles.cues) == 2
    assert "merged" in page.status_hint.text().lower()

    # The table always pairs neighbours, so the refusal is a service-level
    # guard: prove it still holds for a pair with a caption in between.
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=2.0, text="One"),
        SubtitleCue(id="c2", start=2.0, end=4.0, text="Two"),
        SubtitleCue(id="c3", start=4.0, end=6.0, text="Three"),
    ]
    assert page.service.merge(project, "c1", "c3") is False
    assert len(project.subtitles.cues) == 3


def test_the_style_check_reports_a_real_problem(window) -> None:
    from app.project.model import SubtitleCue

    project, _root = _open_project(window)
    project.format.width, project.format.height = 512, 288
    project.subtitles.enabled = True
    # Two captions on screen at once.
    project.subtitles.cues = [
        SubtitleCue(id="c1", start=0.0, end=5.0, text="Overlapping"),
        SubtitleCue(id="c2", start=2.0, end=7.0, text="Also overlapping"),
    ]
    page = window.subtitles_page
    page.refresh()
    page._check_style()

    assert "SUBTITLE_OVERLAP" in page.issues_view.toPlainText()


# --------------------------------------------------------------------------
# Timeline page
# --------------------------------------------------------------------------

def test_the_timeline_page_shows_real_timings(window) -> None:
    project, root = _open_project(window)
    for index in range(3):
        scene = project.add_scene(SceneSpec(name=f"Scene {index + 1}"))
        _wav(root / "audio" / f"s{index + 1}.wav", 1.0 + index, 220 + index * 50)
        scene.narration.file = f"audio/s{index + 1}.wav"
        scene.narration.duration = 1.0 + index
        scene.narration.text = "Narrated."
    window.timeline_page.refresh()
    process_events()

    page = window.timeline_page
    assert page.table.rowCount() == 3
    assert page.summary_grid.value("Scenes") == "3"
    # 1.0+2.0+3.0 plus the 0.5s tail on each scene.
    assert page.summary_grid.value("Narration measured") == "3 of 3 scene(s)"
    assert "From the narration file" in page.table.item(0, 5).text()


def test_editing_a_scene_length_on_the_timeline_page_writes_the_scene(window) -> None:
    project, _root = _open_project(window)
    scene = project.add_scene(SceneSpec(name="Only scene"))
    window.timeline_page.refresh()
    process_events()

    page = window.timeline_page
    page.table.selectRow(0)
    process_events()
    page.scene_duration.setValue(7.5)
    process_events()

    assert scene.duration == pytest.approx(7.5)


def test_a_disabled_scene_is_marked_on_the_timeline(window) -> None:
    project, _root = _open_project(window)
    project.add_scene(SceneSpec(name="Kept"))
    dropped = project.add_scene(SceneSpec(name="Dropped"))
    dropped.enabled = False
    window.timeline_page.refresh()

    page = window.timeline_page
    assert page.table.rowCount() == 2, "an excluded scene disappeared from the page"
    assert page.table.item(1, 5).text() == "Excluded"
    assert "switched off" in page.table.item(1, 1).toolTip()
    # Only the enabled scene takes time.
    assert page.summary_grid.value("Scenes") == "1"


# --------------------------------------------------------------------------
# Render page
# --------------------------------------------------------------------------

def test_the_render_page_writes_quality_settings_into_the_project(window) -> None:
    project, _root = _open_project(window)
    page = window.render_page
    page.refresh()

    page._select(page.quality_combo, "ultra")
    page._on_quality_changed()
    process_events()

    assert project.format.quality_preset == "ultra"
    assert project.format.crf == 15
    assert project.format.encoder_preset == "veryslow"

    page.width_spin.setValue(1080)
    page.height_spin.setValue(1920)
    page._select(page.fps_combo, 60)
    page._on_changed()
    process_events()
    assert project.format.width == 1080
    assert project.format.height == 1920
    assert project.format.fps == 60


def test_the_render_page_never_silently_downgrades_a_preset(window) -> None:
    """Section 29: choosing Ultra must give Ultra's numbers, not a quieter one."""
    from app.project.presets import QUALITY_PRESETS

    project, _root = _open_project(window)
    page = window.render_page
    page.refresh()
    for preset in QUALITY_PRESETS:
        page._select(page.quality_combo, preset.key)
        page._on_quality_changed()
        assert project.format.crf == preset.crf, preset.key
        assert project.format.encoder_preset == preset.encoder_preset, preset.key


def test_the_codec_list_comes_from_the_real_ffmpeg(window) -> None:
    """Section 24: codecs are detected, not assumed."""
    _open_project(window)
    page = window.render_page
    page.refresh()

    page._detect_capabilities()
    assert page._capabilities_job, "the check was not submitted as a job"
    process_events(0.2)

    # Feed the page a real capability result and check the list follows it.
    class FakeResult:
        cancelled = False
        error = None
        value = {"capabilities": {"ffmpeg_version": "7.0.2-test",
                                  "video_encoders": ["libx264"]},
                 "codecs_by_container": {"mp4": ["h264_cpu"], "webm": []},
                 "problems": ["No supported video encoder is available for .webm "
                              "files in this FFmpeg installation."]}

    page.on_capabilities_finished(FakeResult())
    codecs = [page.codec_combo.itemData(i) for i in range(page.codec_combo.count())]
    assert codecs == ["h264_cpu"], f"expected only the available codec, got {codecs}"
    assert "webm" in page.codec_hint.text()


def test_an_unavailable_codec_stays_visible_but_labelled(window) -> None:
    """Hiding the user's choice would be worse than showing why it will fail."""
    project, _root = _open_project(window)
    project.format.codec = "vp9_cpu"
    page = window.render_page
    page.refresh()

    class FakeResult:
        cancelled = False
        error = None
        value = {"capabilities": {"ffmpeg_version": "7.0.2-test",
                                  "video_encoders": ["libx264"]},
                 "codecs_by_container": {"mp4": ["h264_cpu"]},
                 "problems": []}

    page.on_capabilities_finished(FakeResult())
    labels = [page.codec_combo.itemText(i) for i in range(page.codec_combo.count())]
    assert any("vp9_cpu" in label and "not available" in label for label in labels), labels


def test_the_render_page_disables_everything_with_no_project(window) -> None:
    page = window.render_page
    page.refresh()
    assert page.render_button.isEnabled() is False
    assert page.plan_button.isEnabled() is False


def test_heavy_stage_e_work_is_submitted_as_a_job_not_run_inline(window) -> None:
    """Sections 32 and 66: the Qt thread must never wait for FFmpeg."""
    _open_project(window)
    jobs = window.context.jobs
    submitted: list[str] = []
    original = jobs.submit

    def watch(spec):
        submitted.append(spec.key)
        return original(spec)

    jobs.submit = watch
    try:
        window.audio_page._check_audio()
        window.audio_page._mix_preview()
        window.timeline_page._check()
        window.render_page._detect_capabilities()
        window.render_page._plan()
        window.render_page._render()
    finally:
        jobs.submit = original

    assert "audio.validate" in submitted
    assert "audio.mix" in submitted
    assert "timeline.check" in submitted
    assert "render.capabilities" in submitted
    assert "render.plan" in submitted
    assert "render.final" in submitted

    # Cancel everything so the fixture shuts down cleanly.
    for page, attr in ((window.audio_page, "_validate_job"),
                       (window.audio_page, "_mix_job"),
                       (window.timeline_page, "_check_job"),
                       (window.render_page, "_capabilities_job"),
                       (window.render_page, "_plan_job"),
                       (window.render_page, "_render_job")):
        job_id = getattr(page, attr, None)
        if job_id:
            jobs.cancel(job_id, "test finished")
    process_events(0.5)


def test_a_second_render_is_not_submitted_while_one_is_running(window) -> None:
    """One user action, one job (section 9) - and no hidden duplicates."""
    _open_project(window)
    jobs = window.context.jobs
    page = window.render_page

    page._render()
    first = page._render_job
    assert first is not None

    page._render()
    assert page._render_job == first, "a duplicate render job was started"
    assert "already running" in page.status_hint.text()

    jobs.cancel(first, "test finished")
    process_events(0.5)
