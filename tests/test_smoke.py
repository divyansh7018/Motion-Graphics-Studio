"""The built-in smoke test itself (directive sections 57, 58).

The smoke test is the check that answers "does this build actually work?".
These tests run it against a temporary data folder with a stand-in FFmpeg
toolchain, so they are fast, deterministic and independent of what is installed
on the machine.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


from app.diagnostics.smoke import cleanup_smoke_files, run_smoke_test


def _install_fake_tools(paths, tmp_path: Path) -> None:
    """Put scripted ffmpeg/ffprobe executables in the app's tools folder.

    ``ffmpeg`` copies a dummy file (it only has to exit 0) and ``ffprobe``
    prints a plausible JSON document, which lets the encode/validate step run
    on any machine.
    """
    tools = paths.tools_dir
    tools.mkdir(parents=True, exist_ok=True)

    ffprobe_json = (
        '{"streams": [{"index": 0, "codec_name": "h264", "codec_type": "video",'
        ' "width": 640, "height": 360, "avg_frame_rate": "25/1"}],'
        ' "format": {"duration": "1.000000", "size": "1234", "format_name": "mov,mp4"}}'
    )

    if os.name == "nt":  # pragma: no cover - Windows test path
        (tools / "ffmpeg.bat").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        (tools / "ffprobe.bat").write_text(f"@echo off\r\necho {ffprobe_json}\r\nexit /b 0\r\n", encoding="utf-8")
        return

    ffmpeg_script = tools / "ffmpeg"
    ffmpeg_script.write_text(
        "#!/bin/sh\n"
        "# stand-in ffmpeg: create the requested output file and succeed\n"
        "case \"$1\" in\n"
        "  -version|--version) echo ffmpeg version 6.1.1-static; exit 0 ;;\n"
        "esac\n"
        'for last in "$@"; do :; done\n'
        "# Never treat an option as a destination path: writing to a relative name\n"
        "# such as '-version' would drop a stray file in the current directory.\n"
        'case "$last" in\n'
        "  -*) ;;\n"
        '  *) printf "fake" > "$last" ;;\n'
        "esac\n"
        "echo ffmpeg version 6.1.1-static\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ffmpeg_script.chmod(ffmpeg_script.stat().st_mode | stat.S_IEXEC)

    ffprobe_script = tools / "ffprobe"
    ffprobe_script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-version\" ]; then echo 'ffprobe version 6.1.1-static'; exit 0; fi\n"
        f"cat <<'JSON'\n{ffprobe_json}\nJSON\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ffprobe_script.chmod(ffprobe_script.stat().st_mode | stat.S_IEXEC)


def test_smoke_test_passes_with_a_working_toolchain(paths, settings, tmp_path) -> None:
    _install_fake_tools(paths, tmp_path)

    result = run_smoke_test(paths, settings, verbose=False)

    assert result.passed, result.to_text()
    assert len(result.steps) >= 10
    names = [step.name for step in result.steps]
    assert "Application folders" in names
    assert "Settings save / load / recovery" in names
    assert "One action = one job" in names
    assert "Cancellation reaches a terminal state" in names
    assert "PASSED" in result.summary()


def test_smoke_test_reports_failure_without_raising(paths, settings) -> None:
    """A broken environment produces a report, never a traceback."""
    result = run_smoke_test(paths, settings, verbose=False)
    # No ffmpeg in this data root: the encode step must fail or be skipped,
    # but every other step still has to be reported.
    assert len(result.steps) >= 10
    assert result.summary()


def test_smoke_test_does_not_delete_projects_or_assets(paths, settings, tmp_path) -> None:
    _install_fake_tools(paths, tmp_path)
    canary_project = paths.projects_dir / "keep" / "project.json"
    canary_project.parent.mkdir(parents=True, exist_ok=True)
    canary_project.write_text('{"keep": true}', encoding="utf-8")
    canary_asset = paths.assets_dir / "keep.png"
    canary_asset.write_bytes(b"png")

    run_smoke_test(paths, settings, verbose=False)

    assert canary_project.exists()
    assert canary_asset.exists()


def test_smoke_cleanup_removes_scratch_only(paths) -> None:
    scratch = paths.workspace_dir / "smoke"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "file.bin").write_bytes(b"x")
    keep = paths.workspace_dir / "keep.txt"
    keep.write_text("keep", encoding="utf-8")

    assert cleanup_smoke_files(paths) == 1

    assert not scratch.exists()
    assert keep.exists()


def test_smoke_result_is_serialisable(paths, settings, tmp_path) -> None:
    _install_fake_tools(paths, tmp_path)
    result = run_smoke_test(paths, settings, verbose=False)
    text = result.to_text()
    assert "Files produced" in text or result.artifacts == []
    for step in result.steps:
        assert step.glyph in ("OK  ", "FAIL", "-")


def test_smoke_test_never_writes_into_the_working_directory(paths, settings, tmp_path, monkeypatch) -> None:
    """The stand-in toolchain must not drop files into whatever CWD the app has.

    Regression test: a fake ``ffmpeg`` that blindly redirected its last argument
    to a file created a stray ``-version`` file whenever the version probe ran,
    which is exactly the sort of path bug real users would hit from a shortcut.
    """
    _install_fake_tools(paths, tmp_path)
    working_dir = tmp_path / "somewhere-else"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)

    result = run_smoke_test(paths, settings, verbose=False)

    assert result.passed, result.to_text()
    assert list(working_dir.iterdir()) == []


def test_smoke_test_can_be_run_twice_in_the_same_folder(paths, settings, tmp_path) -> None:
    """Regression test: the second run must not fail because of the first run.

    Leftovers from a previous run made the "never overwrite an existing video"
    step fire, so users were told the build was broken when it was fine.
    """
    _install_fake_tools(paths, tmp_path)

    first = run_smoke_test(paths, settings, verbose=False)
    second = run_smoke_test(paths, settings, verbose=False)

    assert first.passed, first.to_text()
    assert second.passed, second.to_text()
