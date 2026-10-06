"""FFmpeg/FFprobe discovery and safe invocation (directive sections 29, 42)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from app.core.errors import FFmpegNotFoundError
from app.tools import ffmpeg as ffmpeg_tools
from app.tools.ffmpeg import (
    FFmpegDiscovery,
    FFmpegTools,
    ToolInfo,
    format_command,
    run_capture,
)

FAKE_VERSION_OUTPUT = "ffmpeg version 6.1.1-static Copyright (c) 2000-2023\nbuilt with gcc 12\n"
FAKE_PROBE_OUTPUT = "ffprobe version 6.1.1-static Copyright (c) 2007-2023\nbuilt with gcc 12\n"


def _write_script(path: Path, output: str, exit_code: int = 0) -> Path:
    """Create a tiny executable that mimics a CLI tool.

    The script prints *output* verbatim (newline terminated, so the heredoc
    always closes) and exits with *exit_code*.
    """
    text = output if output.endswith("\n") else output + "\n"
    if os.name == "nt":  # pragma: no cover - Windows test path
        path.write_text(f"@echo off\r\n< nul set /p x= 2>nul\r\necho {output}\r\nexit /b {exit_code}\r\n", encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{text}EOF\nexit {exit_code}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_tool_environment(monkeypatch):
    """Make discovery hermetic: it must only see what a test sets up.

    Discovery legitimately falls back to ``PATH`` as its last step.  On a
    machine that really has FFmpeg installed - which is exactly what a Stage E
    build machine looks like - the "missing tools" tests would otherwise find
    the real binary and fail.  Disabling only that final ``PATH`` lookup keeps
    every assertion intact (each test still proves the behaviour it names) while
    leaving the rest of the environment usable, so the fake tools the other tests
    install are still found and executed.
    """
    monkeypatch.setattr(ffmpeg_tools, "_which", lambda name: (None, "not on PATH"))
    yield


def test_missing_tools_produce_a_clear_state(tmp_path: Path) -> None:
    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path)
    assert not discovery.has_ffmpeg
    assert not discovery.has_ffprobe
    assert not discovery.is_complete
    assert "not found" in discovery.summary().lower()


def test_tools_found_in_the_bundled_tools_folder(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_script(tools_dir / "ffmpeg", FAKE_VERSION_OUTPUT)
    _write_script(tools_dir / "ffprobe", FAKE_PROBE_OUTPUT)

    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path)

    assert discovery.is_complete
    assert discovery.ffmpeg.version == "6.1.1-static"
    assert discovery.ffprobe.version == "6.1.1-static"
    assert "6.1.1" in discovery.summary()


def test_explicit_path_from_settings_wins(tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    custom = _write_script(custom_dir / "ffmpeg", FAKE_VERSION_OUTPUT)
    bundled = tmp_path / "tools"
    bundled.mkdir()
    _write_script(bundled / "ffmpeg", "ffmpeg version 1.0-bundled\n")

    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path, ffmpeg_exe_setting=str(custom))

    assert discovery.ffmpeg.path == custom
    assert discovery.ffmpeg.version == "6.1.1-static"
    assert discovery.ffmpeg.source == "settings (explicit path)"


def test_folder_setting_is_reported_as_a_mistake(tmp_path: Path) -> None:
    folder = tmp_path / "ffmpeg-folder"
    folder.mkdir()

    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path, ffmpeg_exe_setting=str(folder))

    assert not discovery.has_ffmpeg
    assert any("points to a folder" in error for error in discovery.errors)


def test_file_that_cannot_run_is_rejected(tmp_path: Path) -> None:
    """A file that exists but is not a working executable must not be accepted."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    broken = tools_dir / "ffmpeg"
    _write_script(broken, "this is not ffmpeg", exit_code=1)

    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path)

    assert not discovery.has_ffmpeg
    assert discovery.errors
    assert "could not be run" in discovery.errors[0]


def test_discovery_records_where_it_looked(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path)
    assert any(str(tools_dir) in entry for entry in discovery.searched)


# --------------------------------------------------------------------------
# Safe command execution
# --------------------------------------------------------------------------

def test_run_capture_reports_missing_executable() -> None:
    result = run_capture(["/definitely/not/a/real/program-xyz"])
    assert result.ok is False
    assert "could not be started" in result.error


def test_run_capture_reports_failure_output() -> None:
    result = run_capture([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert result.returncode == 3
    assert not result.ok
    assert "boom" in result.describe_failure()


def test_run_capture_times_out_without_hanging() -> None:
    result = run_capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.7)
    assert result.ok is False
    assert "did not finish" in result.error


def test_run_capture_cancel_stops_the_child_process() -> None:
    token = ffmpeg_tools.FFmpegCancelToken()
    import threading

    threading.Timer(0.3, token.cancel).start()
    result = run_capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30, cancel_token=token)
    assert result.cancelled or not result.ok


def test_run_capture_is_safe_with_arguments_containing_spaces(tmp_path: Path) -> None:
    """No shell is involved, so quoting cannot break out (never shell=True)."""
    output = run_capture([sys.executable, "-c", "import sys; print(sys.argv[1])", "a b; rm -rf /"])
    assert output.ok
    assert "a b; rm -rf /" in output.stdout


def test_long_output_is_bounded() -> None:
    result = run_capture([sys.executable, "-c", "print('x' * 200000)"], timeout=30)
    assert result.ok
    assert len(result.stdout) < 70000
    assert "output truncated" in result.stdout


def test_format_command_quotes_paths_with_spaces() -> None:
    text = format_command(["ffmpeg", "-i", "C:/my videos/clip.mp4"])
    assert '"C:/my videos/clip.mp4"' in text


# --------------------------------------------------------------------------
# FFmpegTools wrapper
# --------------------------------------------------------------------------

def test_tools_wrapper_raises_friendly_error_when_missing(tmp_path: Path) -> None:
    discovery = ffmpeg_tools.discover_ffmpeg(source_root=tmp_path)
    tools = FFmpegTools(discovery)

    with pytest.raises(FFmpegNotFoundError) as excinfo:
        tools.ensure_available()

    friendly = excinfo.value.friendly()
    assert "FFmpeg" in friendly.what_happened
    assert any("FFmpeg folder" in action or "tools" in action for action in friendly.actions)


def test_tools_wrapper_runs_and_probes(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_script(tools_dir / "ffmpeg", FAKE_VERSION_OUTPUT)
    # A tiny ffprobe stand-in that prints JSON, so the parsing path is exercised.
    probe_script = tools_dir / "ffprobe"
    if os.name == "nt":  # pragma: no cover
        probe_script.write_text('@echo off\r\necho {"streams": []}\r\n', encoding="utf-8")
    else:
        probe_script.write_text("#!/bin/sh\ncat <<'EOF'\n{\"streams\": [], \"format\": {\"duration\": \"1.0\"}}\nEOF\n", encoding="utf-8")
        probe_script.chmod(probe_script.stat().st_mode | stat.S_IEXEC)

    tools = FFmpegTools(ffmpeg_tools.discover_ffmpeg(source_root=tmp_path))
    tools.ensure_available()

    result = tools.run(["-version"])
    assert result.ok
    assert "ffmpeg version" in result.stdout

    payload, error = tools.probe_json(["-show_streams", "dummy.mp4"])
    assert error == ""
    assert payload is not None and "streams" in payload


def test_tools_wrapper_reports_invalid_probe_json(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    _write_script(tools_dir / "ffmpeg", FAKE_VERSION_OUTPUT)
    _write_script(tools_dir / "ffprobe", "not json at all")

    tools = FFmpegTools(ffmpeg_tools.discover_ffmpeg(source_root=tmp_path))
    payload, error = tools.probe_json(["-show_streams", "dummy.mp4"])

    assert payload is None
    assert "could not be read" in error


def test_discovery_dataclass_helpers(tmp_path: Path) -> None:
    discovery = FFmpegDiscovery()
    assert discovery.summary().startswith("FFmpeg and FFprobe were not found")

    real_file = tmp_path / "ffmpeg"
    real_file.write_bytes(b"x")
    discovery.ffmpeg = ToolInfo(name="ffmpeg", path=real_file, version="7.0")
    assert discovery.has_ffmpeg
    assert not discovery.is_complete
    assert "FFprobe is missing" in discovery.summary()

    # A ToolInfo pointing at a file that does not exist is not "available".
    discovery.ffmpeg = ToolInfo(name="ffmpeg", path=tmp_path / "gone", version="7.0")
    assert not discovery.has_ffmpeg
