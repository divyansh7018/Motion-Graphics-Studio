"""The ``script``, ``voice`` and ``narration`` CLI commands (directive section 61).

Every test runs the real command functions against a real project folder, so the
exit codes and messages are the ones a user would see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.main import main
from app.project.service import CreateRequest, ProjectService

VOICE_IDS = ["af_bella", "am_adam", "hf_alpha", "hm_ishaan"]


class Args:
    """A stand-in for argparse's namespace."""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


@pytest.fixture()
def project(tmp_path: Path, paths, settings) -> Path:
    service = ProjectService(paths, settings)
    folder = tmp_path / "CLI Test"
    service.create_project(CreateRequest(name="CLI Test", folder=folder))
    return folder


@pytest.fixture()
def kokoro_dir(tmp_path: Path) -> Path:
    root = tmp_path / "models" / "kokoro"
    (root / "voices").mkdir(parents=True)
    (root / "kokoro-82m-v1.0.onnx").write_bytes(b"ONNXFAKE" * 4096)
    for voice in VOICE_IDS:
        (root / "voices" / f"{voice}.pt").write_bytes(b"VOICEFAKE" * 64)
    return root


def run_cli(tmp_path: Path, *argv: str) -> int:
    """Run the CLI in-process with its own data root."""
    return main(["--data-root", str(tmp_path / "appdata"), *argv])


def run_cli_process(tmp_path: Path, *argv: str):
    """Run the CLI as a separate process and return ``(code, output)``.

    Used where a test needs several invocations against the same project.  The
    project lock is advisory and keyed on the process id, so calling the CLI
    repeatedly *in-process* would look like a second window on the same project -
    an artefact of the test, not of the command.
    """
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "app.cli.main", "--data-root", str(tmp_path / "appdata"), *argv],
        capture_output=True, text=True, timeout=180,
    )
    return completed.returncode, completed.stdout + completed.stderr


# --------------------------------------------------------------------------
# script
# --------------------------------------------------------------------------

def test_script_import_stores_the_text_exactly(tmp_path: Path, capsys) -> None:
    run_cli(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    source = tmp_path / "in.txt"
    source.write_text("नमस्ते दोस्तों। आज हम बात करेंगे निवेश की।", encoding="utf-8")

    code = run_cli(tmp_path, "script", "import", str(tmp_path / "proj"), str(source))

    assert code == 0
    stored = json.loads((tmp_path / "proj" / "project.json").read_text(encoding="utf-8"))
    assert stored["script"]["source_text"] == "नमस्ते दोस्तों। आज हम बात करेंगे निवेश की।"


def test_script_show_reports_counts(tmp_path: Path) -> None:
    run_cli_process(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    source = tmp_path / "in.txt"
    source.write_text("One two three four five.", encoding="utf-8")
    run_cli_process(tmp_path, "script", "import", str(tmp_path / "proj"), str(source))

    code, out = run_cli_process(tmp_path, "script", "show", str(tmp_path / "proj"))

    assert code == 0, out
    assert "5 words" in out
    assert "estimated" in out


def test_script_export_refuses_to_overwrite(tmp_path: Path, capsys) -> None:
    run_cli(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    target = tmp_path / "out.txt"
    target.write_text("keep me", encoding="utf-8")

    code = run_cli(tmp_path, "script", "export", str(tmp_path / "proj"), str(target))

    assert code == 1
    assert target.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().out


def test_script_export_with_force_replaces_the_file(tmp_path: Path) -> None:
    run_cli_process(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    target = tmp_path / "out.txt"

    first, out1 = run_cli_process(tmp_path, "script", "export", str(tmp_path / "proj"), str(target))
    again, out2 = run_cli_process(tmp_path, "script", "export", str(tmp_path / "proj"), str(target), "--force")

    assert first == 0, out1
    assert again == 0, out2
    assert target.exists()


def test_script_convert_prints_structured_output(tmp_path: Path) -> None:
    run_cli_process(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    source = tmp_path / "in.txt"
    source.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
    run_cli_process(tmp_path, "script", "import", str(tmp_path / "proj"), str(source))

    code, out = run_cli_process(tmp_path, "script", "convert", str(tmp_path / "proj"), "--to", "structured")

    assert code == 0, out
    assert "[SCENE 01]" in out
    assert "First paragraph." in out


def test_a_missing_project_is_reported_not_crashed(tmp_path: Path, capsys) -> None:
    code = run_cli(tmp_path, "script", "show", str(tmp_path / "nowhere"))

    assert code == 1
    out = capsys.readouterr().out
    assert "No project.json" in out
    assert "Traceback" not in out


def test_importing_a_binary_file_is_refused(tmp_path: Path, capsys) -> None:
    run_cli(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))
    bad = tmp_path / "movie.txt"
    bad.write_bytes(bytes(range(256)) * 8)

    code = run_cli(tmp_path, "script", "import", str(tmp_path / "proj"), str(bad))

    assert code == 1
    assert "text" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------
# voice
# --------------------------------------------------------------------------

def test_voice_check_reports_a_missing_engine(tmp_path: Path, capsys, monkeypatch) -> None:
    # Forced, not assumed: with the real package installed this machine reports
    # it as present, which is correct but not what this test is about.
    import app.tts.capabilities as capabilities

    monkeypatch.setattr(
        capabilities, "probe_package",
        lambda name="kokoro": (False, "", "No module named 'kokoro'"),
    )
    code = run_cli(tmp_path, "voice", "check")
    out = capsys.readouterr().out

    assert code == 2
    assert "not installed" in out
    assert "What to do" in out


def test_voice_check_reports_a_real_installation(tmp_path: Path, capsys) -> None:
    """With the package present the report must say so, and still name the gap."""
    import importlib.util

    if importlib.util.find_spec("kokoro") is None:
        pytest.skip("the kokoro package is not installed on this machine")

    code = run_cli(tmp_path, "voice", "check")
    out = capsys.readouterr().out

    assert "Kokoro      :" in out
    assert "Package     :" in out
    assert "Runtime     :" in out
    # Either the engine is fully ready, or the user is told what to do next.
    if "Initialised : yes" in out:
        assert code == 0
    else:
        assert code == 2
        assert "What to do" in out


def test_voice_list_reads_the_model_folder(tmp_path: Path, kokoro_dir: Path, capsys) -> None:
    code = run_cli(tmp_path, "voice", "list", "--model-dir", str(kokoro_dir))
    out = capsys.readouterr().out

    assert code == 0
    assert f"Voices   : {len(VOICE_IDS)}" in out
    for voice in VOICE_IDS:
        assert voice in out


def test_voice_list_can_filter_by_language(tmp_path: Path, kokoro_dir: Path, capsys) -> None:
    run_cli(tmp_path, "voice", "list", "--model-dir", str(kokoro_dir), "--language", "h")
    out = capsys.readouterr().out

    assert "hf_alpha" in out
    assert "hm_ishaan" in out
    assert "af_bella" not in out


def test_voice_preview_is_blocked_without_an_engine(tmp_path: Path, capsys) -> None:
    code = run_cli(tmp_path, "voice", "preview", "--voice", "af_bella")

    assert code == 2
    assert "✗" in capsys.readouterr().out


# --------------------------------------------------------------------------
# narration
# --------------------------------------------------------------------------

def test_narration_status_of_a_new_project(tmp_path: Path, capsys) -> None:
    run_cli(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))

    code = run_cli(tmp_path, "narration", "status", str(tmp_path / "proj"))
    out = capsys.readouterr().out

    assert code == 0
    assert "not_generated" in out
    assert "No narration has been generated yet." in out


def test_narration_generate_is_blocked_without_an_engine(tmp_path: Path, capsys) -> None:
    run_cli(tmp_path, "project", "create", "--name", "CLI Test", "--folder", str(tmp_path / "proj"))

    code = run_cli(tmp_path, "narration", "generate", str(tmp_path / "proj"), "--voice", "hf_alpha")
    out = capsys.readouterr().out

    assert code == 2, "a missing engine is a blocker, not a generic failure"
    assert "Kokoro" in out
    assert "→" in out, "the user is told what to do"


def test_the_cli_help_lists_the_new_commands(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    out = capsys.readouterr().out
    for command in ("script", "voice", "narration"):
        assert command in out
