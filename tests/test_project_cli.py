"""The ``project`` CLI commands (directive section 32).

These run the real CLI entry point against a throw-away data folder, which also
proves the CLI and the GUI share one implementation: the files these commands
write are exactly what the service writes.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.cli.main import EXIT_BLOCKED, EXIT_OK, EXIT_PROBLEMS, build_parser, main


def run_cli(data_root: Path, *args: str) -> int:
    return main(["--data-root", str(data_root), *args])


def project_file(data_root: Path, name: str) -> Path:
    return data_root / "projects" / name / "project.json"


def test_the_project_commands_are_documented_in_the_parser() -> None:
    parser = build_parser()
    text = parser.format_help()
    assert "project" in text

    help_text = parser.parse_args(["project", "create", "--name", "X"])
    assert help_text.project_command == "create"


def test_create_writes_a_real_project_folder(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    code = run_cli(data_root, "project", "create", "--name", "CLI Video", "--template", "youtube")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Created 'CLI Video'" in output
    assert project_file(data_root, "CLI Video").exists()
    data = json.loads(project_file(data_root, "CLI Video").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "CLI Video"
    assert data["format"]["width"] == 1920
    assert data["project"]["project_version"] == 1


def test_create_honours_explicit_format_and_quality(tmp_path: Path) -> None:
    data_root = tmp_path / "cli"
    code = run_cli(
        data_root, "project", "create", "--name", "Vertical",
        "--width", "1080", "--height", "1920", "--fps", "60", "--quality", "ultra",
    )

    assert code == EXIT_OK
    data = json.loads(project_file(data_root, "Vertical").read_text(encoding="utf-8"))
    assert (data["format"]["width"], data["format"]["height"], data["format"]["fps"]) == (1080, 1920, 60)
    assert data["format"]["quality_preset"] == "ultra"
    assert data["format"]["crf"] == 15


def test_create_refuses_a_bad_name_without_creating_anything(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    code = run_cli(data_root, "project", "create", "--name", "")
    output = capsys.readouterr().out

    assert code == EXIT_PROBLEMS
    assert "needs a name" in output
    assert list((data_root / "projects").iterdir()) == []


def test_create_imports_a_script_file_verbatim(tmp_path: Path) -> None:
    data_root = tmp_path / "cli"
    script = tmp_path / "script.txt"
    script.write_text("First line.\n\nSecond   block.", encoding="utf-8")

    code = run_cli(data_root, "project", "create", "--name", "Scripted", "--script", str(script))

    assert code == EXIT_OK
    data = json.loads(project_file(data_root, "Scripted").read_text(encoding="utf-8"))
    assert data["script"]["source_text"] == "First line.\n\nSecond   block."


def test_open_reports_the_project_and_its_issues(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Opened")
    capsys.readouterr()

    code = run_cli(data_root, "project", "open", str(data_root / "projects" / "Opened"))
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Name        : Opened" in output
    assert "Validation:" in output


def test_validate_lists_every_problem(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Broken")
    capsys.readouterr()

    target = project_file(data_root, "Broken")
    data = json.loads(target.read_text(encoding="utf-8"))
    data["format"]["fps"] = 45
    data["format"]["codec"] = "vp9_cpu"
    data["voice"]["engine"] = "something-else"
    target.write_text(json.dumps(data), encoding="utf-8")

    code = run_cli(data_root, "project", "validate", str(target.parent))
    output = capsys.readouterr().out

    assert code == EXIT_PROBLEMS
    assert "issue(s) found" in output
    for expected in ("fps", "vp9_cpu", "something-else"):
        assert expected in output


def test_validate_for_render_is_stricter(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Empty")
    capsys.readouterr()

    ok = run_cli(data_root, "project", "validate", str(data_root / "projects" / "Empty"))
    capsys.readouterr()
    strict = run_cli(data_root, "project", "validate", "--for-render", str(data_root / "projects" / "Empty"))
    output = capsys.readouterr().out

    assert ok == EXIT_OK
    assert strict == EXIT_PROBLEMS
    assert "no scenes" in output.lower()


def test_validate_on_a_damaged_file_is_blocked_not_crashed(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Damaged")
    capsys.readouterr()
    project_file(data_root, "Damaged").write_text("{ broken", encoding="utf-8")

    code = run_cli(data_root, "project", "validate", str(data_root / "projects" / "Damaged"))
    output = capsys.readouterr().out

    assert code == EXIT_BLOCKED
    assert "JSON" in output


def test_list_shows_recent_projects(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "First")
    run_cli(data_root, "project", "create", "--name", "Second")
    capsys.readouterr()

    code = run_cli(data_root, "project", "list")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "2 project(s)" in output
    assert "First" in output and "Second" in output


def test_list_on_a_fresh_install_explains_what_to_do(tmp_path: Path, capsys) -> None:
    code = run_cli(tmp_path / "cli", "project", "list")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "No projects yet" in output
    assert "project create" in output


def test_duplicate_creates_an_independent_copy(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Source")
    capsys.readouterr()

    code = run_cli(data_root, "project", "duplicate", str(data_root / "projects" / "Source"), "--name", "Copy")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Duplicated as 'Copy'" in output
    source = json.loads(project_file(data_root, "Source").read_text(encoding="utf-8"))
    copy = json.loads(project_file(data_root, "Copy").read_text(encoding="utf-8"))
    assert source["project"]["id"] != copy["project"]["id"]


def test_rename_updates_the_saved_project(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "Before")
    capsys.readouterr()

    code = run_cli(data_root, "project", "rename", str(data_root / "projects" / "Before"), "--name", "After")
    capsys.readouterr()

    assert code == EXIT_OK
    data = json.loads(project_file(data_root, "Before").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "After"


def test_rename_folder_moves_the_project(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    run_cli(data_root, "project", "create", "--name", "OldFolder")
    capsys.readouterr()

    code = run_cli(data_root, "project", "rename", str(data_root / "projects" / "OldFolder"), "--folder", "NewFolder")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "NewFolder" in output
    assert project_file(data_root, "NewFolder").exists()
    assert not (data_root / "projects" / "OldFolder").exists()


def test_recovery_finds_and_restores_an_autosave(tmp_path: Path, capsys) -> None:
    from app.project.service import CreateRequest, ProjectService
    from app.core.paths import AppPaths
    from app.core.settings import Settings

    data_root = tmp_path / "cli"
    paths = AppPaths(data_root=data_root, source_root=Path(__file__).resolve().parents[1], reason="test")
    paths.ensure()
    service = ProjectService(paths, Settings())
    service.create_project(CreateRequest(name="Crashed"))
    layout = service.current_layout
    service.save()
    service.set_script_text("Work that was never saved.")
    service.autosave()
    service.close_project()
    capsys.readouterr()

    found = run_cli(data_root, "project", "recovery")
    output = capsys.readouterr().out
    assert found == EXIT_PROBLEMS
    assert "recovery data" in output

    restored = run_cli(data_root, "project", "recovery", "--path", str(layout.root), "--restore", "1")
    output = capsys.readouterr().out
    assert restored == EXIT_OK
    assert "Restored" in output
    data = json.loads(layout.project_file.read_text(encoding="utf-8"))
    assert data["script"]["source_text"] == "Work that was never saved."


def test_recovery_reports_when_there_is_nothing_to_recover(tmp_path: Path, capsys) -> None:
    code = run_cli(tmp_path / "cli", "project", "recovery")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "No recoverable projects" in output


def test_opening_a_folder_that_is_not_a_project_says_so(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli"
    not_a_project = tmp_path / "random"
    not_a_project.mkdir()

    code = run_cli(data_root, "project", "open", str(not_a_project))
    output = capsys.readouterr().out

    assert code == EXIT_PROBLEMS
    assert "not a project" in output


def test_the_stage_a_commands_still_work(tmp_path: Path, capsys) -> None:
    """Adding the project group must not disturb the existing CLI."""
    data_root = tmp_path / "cli"
    assert run_cli(data_root, "setup") == EXIT_OK
    capsys.readouterr()
    assert run_cli(data_root, "info") == EXIT_OK
