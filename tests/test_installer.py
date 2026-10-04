"""The standalone setup tool, exercised the way a user runs it (directive 60).

``installer/setup_windows.py`` is the first thing a new machine runs, so it is
tested as a real subprocess: no imports from it, no monkeypatching - exactly the
command line that appears in the README.  It must work from any working
directory, must never print library log lines on top of its own output, and must
leave a readable report behind.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "installer" / "setup_windows.py"


def _run_setup(data_root: Path) -> subprocess.CompletedProcess[str]:
    """Run the setup tool from an unrelated working directory."""
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--data-root", str(data_root)],
        cwd=str(data_root),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_setup_runs_from_any_folder_and_writes_a_report(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = _run_setup(data_root)

    # 0 = everything required is present, 1 = something marked [FAIL] needs
    # attention.  Anything else (a traceback, a bad exit code) is a bug.
    assert result.returncode in (0, 1), result.stdout + result.stderr

    report = data_root / "config" / "setup_report.txt"
    assert report.exists(), "the setup tool must leave a report the user can send"
    text = report.read_text(encoding="utf-8")
    assert "Python" in text
    assert "Motion Graphics Studio" in text


def test_setup_never_leaks_log_lines_and_creates_the_log_folder(tmp_path: Path) -> None:
    """Regression test.

    The setup tool initialised the application logging before the log folder
    existed, so each event went to stdout as a bare ``EVENT=...`` line and no log
    file was kept.  Both halves are checked here.
    """
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = _run_setup(data_root)
    combined = result.stdout + result.stderr

    assert "EVENT=" not in combined, "application log lines must not be printed as console output"
    assert "Traceback" not in combined

    log_file = data_root / "logs" / "studio.log"
    assert log_file.exists(), "the setup tool must log into the data folder once it exists"
    assert "APP_START" in log_file.read_text(encoding="utf-8")


def test_setup_explains_how_to_fix_a_missing_ffmpeg(tmp_path: Path) -> None:
    """A missing dependency must never be reported without a way to fix it."""
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = _run_setup(data_root)
    text = (data_root / "config" / "setup_report.txt").read_text(encoding="utf-8")

    if "[FAIL]" not in text:
        # A machine that already has everything: the report must still say so.
        assert "[ok]" in text
        return

    assert "Choose one of these" in text, "a failure must come with fix options"
    numbered = [line for line in text.splitlines() if line.strip()[:2] in {"1.", "2.", "3."}]
    assert len(numbered) >= 2, "at least two concrete fix options are required"
    assert result.returncode == 1, "a [FAIL] item must be reflected in the exit code"
