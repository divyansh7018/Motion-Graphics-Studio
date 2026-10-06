#!/usr/bin/env python
"""Setup and verification tool.

Run this once after unpacking the application:

    python installer/setup_windows.py

What it does (directive section 64):

1. checks the Python version and architecture,
2. verifies/installs the required packages from ``requirements.txt``,
3. creates the application folder layout,
4. searches for FFmpeg and FFprobe and explains exactly what to do if missing,
5. checks the optional voice engine (Kokoro) and says what to install,
6. writes a short report to ``docs/SETUP_REPORT.txt`` in the data folder.

The script **never** claims success unless it has verified it: if a step fails,
the summary says so and the exit code is non-zero.

It is deliberately dependency-free (standard library only) so it can run before
anything is installed.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep library logging quiet until the real log folder exists: without this,
# Python's last-resort handler would print log lines into the middle of the
# setup output.
try:
    from app.core import logging_setup

    logging_setup.setup_logging(log_dir=None)
except Exception:  # pragma: no cover - the setup tool must run even if this fails
    pass

MIN_PYTHON = (3, 10)

OK = "  [ok]  "
WARN = "  [warn]"
BAD = "  [FAIL]"
INFO = "        "


class Report:
    """Collects the steps so the summary is honest about what failed."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.lines: list[str] = []

    def ok(self, title: str, detail: str = "") -> None:
        self._line(f"{OK} {title}")
        if detail:
            self._line(f"{INFO}{detail}")

    def warn(self, title: str, detail: str = "") -> None:
        self.warnings.append(title)
        self._line(f"{WARN} {title}")
        if detail:
            self._line(f"{INFO}{detail}")

    def fail(self, title: str, detail: str = "") -> None:
        self.failures.append(title)
        self._line(f"{BAD} {title}")
        if detail:
            for line in detail.splitlines():
                self._line(f"{INFO}{line}")

    def info(self, text: str) -> None:
        self._line(f"{INFO}{text}")

    def _line(self, text: str) -> None:
        self.lines.append(text)
        print(text, flush=True)

    def summary(self) -> str:
        if self.failures:
            return f"Setup is NOT complete: {len(self.failures)} step(s) failed."
        if self.warnings:
            return f"Setup complete, with {len(self.warnings)} optional item(s) to look at."
        return "Setup complete - everything is ready."


def check_python(report: Report) -> bool:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    is_64 = sys.maxsize > 2**32

    if (version.major, version.minor) < MIN_PYTHON:
        report.fail(
            f"Python {text} is too old",
            f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer from https://www.python.org/downloads/",
        )
        return False
    if not is_64:
        report.fail("Python is 32-bit", "Install the 64-bit version - video rendering needs 64-bit Python.")
        return False

    report.ok(f"Python {text} (64-bit)", sys.executable)
    return True


def check_packages(report: Report, install: bool) -> bool:
    """Verify the required packages, optionally installing them first."""
    requirements = REPO_ROOT / "requirements.txt"
    if not requirements.exists():
        report.warn("requirements.txt was not found", "Skipping package installation.")
        return True

    if install:
        report.info("Installing required packages (this can take a few minutes)...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(requirements)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            report.fail(
                "Package installation failed",
                (result.stdout or "")[-1500:] + (result.stderr or "")[-1500:],
            )
            return False
        report.ok("Required packages installed")

    missing: list[str] = []
    for name in ("PySide6", "PIL", "numpy"):
        status = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            missing.append(name)
    if missing:
        report.fail(
            f"Missing packages: {', '.join(missing)}",
            "Run:  python installer/setup_windows.py --install",
        )
        return False

    report.ok("Required packages present", "PySide6, Pillow, numpy")
    return True


def setup_folders(report: Report, data_root_arg: str | None) -> Path | None:
    """Create the folder layout using the application's own resolver."""
    try:
        from app.core.paths import AppPaths

        cli_override = Path(data_root_arg) if data_root_arg else None
        paths = AppPaths.bootstrap(source_root=REPO_ROOT, cli_override=cli_override)
        created = paths.ensure()
    except Exception as exc:  # noqa: BLE001 - the setup tool reports, never crashes
        report.fail("Could not create the application folders", str(exc))
        return None

    report.ok(f"Application folders ready ({len(created)} created)", str(paths.data_root))
    # From here on, log into the data folder so problems can be investigated.
    from app.core import logging_setup

    logging_setup.setup_logging(paths.logs_dir, level="INFO", console=False, force=True)
    logging_setup.log_system_banner(paths.describe(), data_root=str(paths.data_root))
    return paths


def check_media_tools(report: Report, paths) -> None:
    from app.tools.ffmpeg import discover_ffmpeg

    discovery = discover_ffmpeg(
        source_root=REPO_ROOT,
        extra_dirs=(paths.tools_dir, paths.data_root),
    )
    if discovery.is_complete:
        report.ok(
            f"FFmpeg {discovery.ffmpeg.version or ''} and FFprobe found",
            str(discovery.ffmpeg.path),
        )
        return

    # Every failure here is a [FAIL] that blocks video work, so every one of them
    # must carry the same concrete fix steps - "what happened, why it matters and
    # what to do" is the rule for every error the application reports.
    if discovery.has_ffmpeg and not discovery.has_ffprobe:
        report.fail(
            "FFprobe is missing",
            "\n".join([
                "FFmpeg was found, but FFprobe is needed to verify finished videos.",
                "",
            ] + _ffmpeg_hint(paths)),
        )
    elif discovery.has_ffprobe and not discovery.has_ffmpeg:
        report.fail(
            "FFmpeg is missing",
            "\n".join([
                "FFprobe was found, but FFmpeg is needed to create videos.",
                "",
            ] + _ffmpeg_hint(paths)),
        )
    else:
        report.fail("FFmpeg and FFprobe were not found", "\n".join(_ffmpeg_hint(paths)))

    for error in discovery.errors[:3]:
        report.info(error)


def _ffmpeg_hint(paths) -> list[str]:
    return [
        "Choose one of these:",
        f"  1. Copy ffmpeg.exe and ffprobe.exe into:  {paths.tools_dir}",
        "  2. Download 'release essentials' from https://www.gyan.dev/ffmpeg/builds/",
        "     and unzip the bin folder next to the application",
        "  3. Install FFmpeg and add it to the Windows PATH",
        "Then open the application and press 'Re-check system'.",
    ]


def check_voice_engine(report: Report, paths) -> None:
    from app.tools.kokoro import install_instructions, probe_kokoro

    status = probe_kokoro(model_dir=paths.kokoro_model_dir, deep_import_check=False)
    if status.usable:
        report.ok("Kokoro voice engine ready", f"{status.package.version or 'installed'}")
        return

    lines = [status.headline(), "", "To enable narration:"]
    lines.extend(f"  {line}" for line in install_instructions())
    report.warn("Kokoro voice engine is not installed (narration unavailable)", "\n".join(lines))


def check_disk_space(report: Report, paths) -> None:
    from app.core.env import LOW_DISK_BLOCK_BYTES, LOW_DISK_WARNING_BYTES, disk_free_bytes
    from app.core.atomicio import human_size

    free = disk_free_bytes(paths.data_root)
    if free == 0:
        report.warn("Free disk space could not be read")
        return
    if free < LOW_DISK_BLOCK_BYTES:
        report.fail(f"Only {human_size(free)} of free disk space", "Rendering needs several gigabytes free.")
    elif free < LOW_DISK_WARNING_BYTES:
        report.warn(f"{human_size(free)} free", "Enough for short videos; consider freeing space for longer ones.")
    else:
        report.ok(f"{human_size(free)} free on the data drive")


def write_report(report: Report, paths, destination: Path | None = None) -> Path | None:
    if paths is None:
        return None
    target = destination or (paths.config_dir / "setup_report.txt")
    try:
        from app.core.version import full_version_string

        content = [
            "Motion Graphics Studio - setup report",
            f"Version: {full_version_string()}",
            f"Date: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Python: {platform.python_version()} ({platform.architecture()[0]})",
            f"Platform: {platform.system()} {platform.release()}",
            f"Data folder: {paths.data_root}",
            "",
            report.summary(),
            "",
            *report.lines,
        ]
        target.write_text("\n".join(content) + "\n", encoding="utf-8")
        return target
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up and verify Motion Graphics Studio.")
    parser.add_argument("--install", action="store_true", help="Install the required packages with pip first.")
    parser.add_argument("--data-root", help="Folder to use for projects, assets and output.")
    args = parser.parse_args(argv)

    print("Motion Graphics Studio - setup")
    print("=" * 60)

    report = Report()
    if not check_python(report):
        print()
        print(report.summary())
        return 1

    check_packages(report, install=args.install)
    paths = setup_folders(report, args.data_root)
    if paths is not None:
        check_media_tools(report, paths)
        check_voice_engine(report, paths)
        check_disk_space(report, paths)

    report_file = write_report(report, paths)

    print()
    print("=" * 60)
    print(report.summary())
    if report_file is not None:
        print(f"Report saved to: {report_file}")
    if report.failures:
        print()
        print("Fix the items marked [FAIL], then run this setup again.")
        return 1
    if report.warnings:
        print()
        print("The application will start. The items marked [warn] are optional and can be added later.")
    print()
    print("Start the application with:   run_studio.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
