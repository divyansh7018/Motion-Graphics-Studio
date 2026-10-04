"""Command line behaviour (directive sections 57, 64, 69).

The CLI is the support tool and the automation entry point, so it must work
without a display, without Qt widgets and from any working directory.
"""

from __future__ import annotations

from pathlib import Path


from app.cli.main import EXIT_BLOCKED, EXIT_OK, EXIT_PROBLEMS, build_parser, main


def _run_cli(data_root: Path, *args: str) -> int:
    return main(["--data-root", str(data_root), *args])


def test_setup_creates_folders_and_reports(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli-data"
    code = _run_cli(data_root, "setup")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Folder setup complete." in output
    assert (data_root / "projects").is_dir()
    assert (data_root / "output").is_dir()
    assert (data_root / "logs").is_dir()


def test_info_prints_machine_and_settings(tmp_path: Path, capsys) -> None:
    code = _run_cli(tmp_path / "cli-info", "info")
    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Machine:" in output
    assert "Settings:" in output
    assert "Storage:" in output


def test_clean_reports_what_it_removed(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli-clean"
    _run_cli(data_root, "setup")
    (data_root / "temp" / "leftover.tmp").write_bytes(b"x" * 100)

    code = _run_cli(data_root, "clean")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "removed 1 file" in output.lower()
    assert not (data_root / "temp" / "leftover.tmp").exists()


def test_check_reports_ffmpeg_state(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli-check"
    code = _run_cli(data_root, "check")
    output = capsys.readouterr().out

    assert "FFmpeg" in output
    # Exit code reflects the real state of the machine (0 = ready, 1 = missing
    # optional/required parts, 2 = blocked).  Any of them is a valid answer, but
    # the run must not crash.
    assert code in (EXIT_OK, EXIT_PROBLEMS, EXIT_BLOCKED)


def test_smoke_test_command_runs_end_to_end(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli-smoke"
    code = _run_cli(data_root, "smoke-test")
    output = capsys.readouterr().out

    assert code in (EXIT_OK, EXIT_PROBLEMS)
    assert "Smoke test" in output
    assert "Application folders" in output


def test_corrupt_settings_do_not_stop_the_cli(tmp_path: Path, capsys) -> None:
    data_root = tmp_path / "cli-corrupt"
    _run_cli(data_root, "setup")
    (data_root / "config" / "settings.json").write_text("not json", encoding="utf-8")

    code = _run_cli(data_root, "info")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Settings" in output


def test_cli_works_from_another_working_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    """Section 69: the CLI must not depend on the current working directory."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    data_root = tmp_path / "cli-cwd"
    code = _run_cli(data_root, "setup")
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert str(data_root) in output
    assert (data_root / "config").is_dir()


def test_unreadable_data_root_gives_a_friendly_error(tmp_path: Path, capsys) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    real_mkdir = Path.mkdir

    def exploding_mkdir(self, *args, **kwargs):  # noqa: ANN001
        if str(self).startswith(str(blocked)):
            raise PermissionError(13, "Access is denied")
        return real_mkdir(self, *args, **kwargs)

    import app.core.paths as paths_module

    monkeypatch_attr = paths_module.Path.mkdir
    paths_module.Path.mkdir = exploding_mkdir
    try:
        code = _run_cli(blocked, "setup")
        output = capsys.readouterr().out + capsys.readouterr().err
    finally:
        paths_module.Path.mkdir = monkeypatch_attr

    assert code == EXIT_BLOCKED
    assert "could not be prepared" in output.lower()


def test_parser_exposes_the_documented_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for command in ("setup", "check", "info", "clean", "smoke-test", "gui"):
        assert command in help_text


def test_version_flag_does_not_need_a_data_root(capsys) -> None:
    from app.main import main as gui_main

    assert gui_main(["--version"]) == 0
    assert "Motion Graphics Studio" in capsys.readouterr().out
