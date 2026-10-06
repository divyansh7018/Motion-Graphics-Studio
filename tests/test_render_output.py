"""Output naming, staging and render history (Stage E, sections 51-56).

The rule these tests defend: a render never destroys a file the user already
has, and the finished video only appears at its final name after it has been
verified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.project.model import SceneSpec, build_project
from app.render.engine import RenderRequest
from app.render.output import (
    HistoryEntry,
    OutputService,
    render_template,
    sanitize_component,
    sequence_from_name,
)


@pytest.fixture()
def project(tmp_path: Path):
    project = build_project("My Video")
    project.export.output_dir = str(tmp_path / "renders")
    return project


@pytest.fixture()
def service(tmp_path: Path) -> OutputService:
    return OutputService(tmp_path)


# -- template --------------------------------------------------------------

def test_template_adds_a_sequence_when_the_user_forgot_one():
    assert render_template("{name}", project="Show", sequence=3) == "Show_3"


def test_template_expands_every_placeholder():
    from datetime import datetime

    text = render_template("{name}_{channel}_{date}_{time}_{quality}_{resolution}_{seq}",
                           project="Show", sequence=7, quality="high",
                           resolution="1920x1080", channel="Chan",
                           when=datetime(2026, 3, 4, 5, 6))
    assert text == "Show_Chan_2026-03-04_0506_high_1920x1080_7"


def test_template_accepts_the_alias_spellings():
    """{project}/{n}/{sequence} mean the same thing as {name}/{seq}."""
    assert render_template("{project}-{n}", project="Show", sequence=2) == "Show-2"
    assert render_template("{sequence}", project="Show", sequence=9) == "9"


def test_template_leaves_an_unknown_placeholder_alone():
    """A typo in a template must not stop a finished render being saved."""
    assert "{oops}" in render_template("{name}_{oops}_{seq}", project="Show", sequence=1)


def test_the_render_name_matches_what_the_ui_previewed(service, project):
    """The export dialog previews a name with preview_filename; the render must
    produce that same name, or the user is shown one thing and given another."""
    from app.project.presets import preview_filename

    decision = service.decide(project.export, project_name=project.project.name)
    previewed = preview_filename(project.export.filename_template,
                                 project.project.name, seq=decision.sequence)
    assert decision.filename == previewed


def test_sanitize_removes_windows_illegal_characters():
    assert sanitize_component('A<B>C:D"E/F\\G|H?I*J') == "A_B_C_D_E_F_G_H_I_J"
    assert sanitize_component("  spaced   out  ") == "spaced out"
    assert sanitize_component("") == "Video"


def test_sequence_is_read_back_from_a_file_name():
    assert sequence_from_name("Show_Video12.mp4") == 12
    assert sequence_from_name("Show.mp4") == 0


# -- choosing a name -------------------------------------------------------

def test_first_render_uses_the_first_take(service, project):
    decision = service.decide(project.export, project_name="My Video")
    assert decision.filename == "My Video_1.mp4"
    assert decision.sequence == 1


def test_an_existing_take_is_never_reused(service, project, tmp_path: Path):
    target = Path(project.export.output_dir)
    target.mkdir(parents=True)
    (target / "My Video_1.mp4").write_bytes(b"x")
    decision = service.decide(project.export, project_name="My Video")
    assert decision.filename == "My Video_2.mp4"
    assert not (target / decision.filename).exists()


def test_numbering_walks_past_several_existing_takes(service, project, tmp_path: Path):
    target = Path(project.export.output_dir)
    target.mkdir(parents=True)
    for number in (1, 2, 3):
        (target / f"My Video_{number}.mp4").write_bytes(b"x")
    assert service.decide(project.export, project_name="My Video").sequence == 4


def test_the_stored_sequence_number_is_respected(service, project):
    project.export.next_sequence_number = 9
    assert service.decide(project.export, project_name="My Video").sequence == 9


def test_a_custom_template_is_used(service, project):
    project.export.filename_template = "{name}_final_{seq}"
    assert service.decide(project.export, project_name="Show").filename == "Show_final_1.mp4"


def test_the_container_decides_the_extension(service, project):
    project.export.container = "mkv"
    assert service.decide(project.export, project_name="Show").filename.endswith(".mkv")


# -- staging and finalising ------------------------------------------------

def test_staging_lives_beside_the_target(service, project):
    """A move inside one folder is atomic; across drives it is a copy."""
    decision = service.decide(project.export, project_name="Show")
    staged = service.staging_path(decision)
    assert staged.parent == decision.directory
    assert staged.name.startswith(".")


def test_finalize_moves_the_file_into_place(service, project):
    decision = service.decide(project.export, project_name="Show")
    staged = service.staging_path(decision)
    staged.write_bytes(b"finished video")
    saved = service.finalize(staged, decision)
    assert saved == decision.path
    assert saved.read_bytes() == b"finished video"
    assert not staged.exists()


def test_finalize_refuses_to_clobber_a_file_that_appeared(service, project):
    """The name was free when chosen; if something else took it, we move on."""
    decision = service.decide(project.export, project_name="Show")
    decision.directory.mkdir(parents=True, exist_ok=True)
    decision.path.write_bytes(b"someone else's video")
    staged = service.staging_path(decision)
    staged.write_bytes(b"our video")

    saved = service.finalize(staged, decision)
    assert saved != decision.path
    assert decision.path.read_bytes() == b"someone else's video"
    assert saved.read_bytes() == b"our video"


def test_finalize_bumps_past_every_taken_name(service, project):
    decision = service.decide(project.export, project_name="Show")
    decision.directory.mkdir(parents=True, exist_ok=True)
    for number in (1, 2, 3):
        (decision.directory / f"Show_{number}.mp4").write_bytes(b"x")
    staged = service.staging_path(decision)
    staged.write_bytes(b"our video")
    assert service.finalize(staged, decision).name == "Show_4.mp4"


# -- history ---------------------------------------------------------------

def test_history_starts_empty(service):
    assert service.history() == []


def test_history_records_and_reads_back(service):
    service.record(HistoryEntry(at="2026-01-01T00:00:00", path="/tmp/a.mp4",
                                status="COMPLETED", duration=12.5, width=1920,
                                height=1080, fps=30.0, size_bytes=1234,
                                quality="high", resolution="1920x1080", qc="PASS"))
    entries = service.history()
    assert len(entries) == 1
    assert entries[0].path == "/tmp/a.mp4"
    assert entries[0].duration == 12.5
    assert entries[0].qc == "PASS"


def test_history_is_newest_first_and_bounded(service):
    for index in range(OutputService.MAX_HISTORY + 10):
        service.record(HistoryEntry(at=f"2026-01-01T00:00:{index:02d}",
                                    path=f"/tmp/{index}.mp4", status="COMPLETED"))
    entries = service.history()
    assert len(entries) == OutputService.MAX_HISTORY
    assert entries[0].path == f"/tmp/{OutputService.MAX_HISTORY + 9}.mp4"


def test_a_corrupt_history_file_is_ignored_not_fatal(service, tmp_path: Path):
    service.history_path.write_text("{not json", encoding="utf-8")
    assert service.history() == []


def test_history_ignores_unknown_keys(tmp_path: Path):
    entry = HistoryEntry.from_dict({"path": "/tmp/x.mp4", "made_up": 1})
    assert entry.path == "/tmp/x.mp4"


def test_the_configured_output_folder_is_actually_used(tmp_path: Path) -> None:
    """The export section owns the folder - not the format spec.

    An earlier version handed ``project.format`` to the output service, which
    has no ``output_dir``, so the folder the user picked was silently ignored
    and every render landed in ``renders/``.
    """
    from app.render.output import OutputService, export_settings

    project = build_project("Output Folder")
    project.export.output_dir = "my_exports"
    project.export.filename_template = "{name}_take{seq}"
    service = OutputService(tmp_path)

    decision = service.decide(export_settings(project), project_name="Output Folder",
                              quality="high", resolution="1280x720")
    assert decision.directory == tmp_path / "my_exports"
    assert decision.filename == "Output Folder_take1.mp4"

    # The old call would have fallen back to the default folder.
    assert service.output_directory(project.format) == tmp_path / "renders"


def test_export_settings_prefers_the_export_section(tmp_path: Path) -> None:
    from app.render.output import export_settings

    project = build_project("Export Section")
    assert export_settings(project) is project.export


def test_an_unusable_output_folder_is_refused_before_rendering(tmp_path: Path) -> None:
    """Section 55: never guess a folder, report the problem."""
    from app.render.engine import RenderEngine
    from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

    project = build_project("Bad Folder")
    project.format.width, project.format.height, project.format.fps = 512, 288, 25
    project.add_scene(SceneSpec(name="Only"))
    project.export.output_dir = "/proc/definitely/not/writable"

    engine = RenderEngine(FFmpegTools(discover_ffmpeg()), project_dir=tmp_path)
    result = engine.render(RenderRequest(project=project, quick_qc=True))

    assert result.failed is True
    assert result.path is None
    codes = [getattr(issue, "code", "") for issue in result.errors]
    assert "OUTPUT_FOLDER_UNAVAILABLE" in codes, codes
