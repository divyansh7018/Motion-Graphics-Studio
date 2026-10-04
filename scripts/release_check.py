#!/usr/bin/env python
"""Release check: prove this build works on *this* machine, for real.

Directive section 57 insists that a build must be verified by running it, not by
confirming that imports succeed.  This script does exactly that, on the machine
where it is executed:

1. create and verify the application folders
2. run the system check
3. run the end-to-end smoke test (real encode)
4. start the **real application** twice and require a clean exit both times
   (startup test + restart test)
5. kill the application hard, then verify the next start is still clean and that
   no project/settings data was damaged (crash test)
6. verify output naming: a second render never overwrites the first file
7. optionally run the automated test suite

Usage::

    python scripts/release_check.py --data-root C:\\mgs-check
    python scripts/release_check.py --data-root ./scratch/check --with-pytest
    python scripts/release_check.py --data-root ./scratch/check --skip-gui

Exit code 0 means every check passed.  Anything else prints exactly what failed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Keep library logging quiet until the log folder is known.
try:
    from app.core import logging_setup

    logging_setup.setup_logging(log_dir=None)
except Exception:  # pragma: no cover
    pass


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False

    @property
    def mark(self) -> str:
        if self.skipped:
            return "skip"
        return "PASS" if self.ok else "FAIL"


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        print(f"[{step.mark}] {step.name} ({step.seconds:.1f}s)")
        if step.detail:
            for line in step.detail.splitlines():
                print(f"       {line}")
        return step

    @property
    def failures(self) -> list[Step]:
        return [step for step in self.steps if not step.ok and not step.skipped]

    def exit_code(self) -> int:
        return 1 if self.failures else 0


def _child_env() -> dict:
    """Environment for GUI child processes (headless-safe)."""
    env = dict(os.environ)
    if os.name != "nt" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return env


def _python() -> str:
    return sys.executable


def check_folders(report: Report, data_root: Path) -> object:
    started = time.perf_counter()
    try:
        from app.core.paths import AppPaths

        paths = AppPaths.bootstrap(source_root=REPO_ROOT, cli_override=data_root)
        created = paths.ensure()
        logging_setup.setup_logging(paths.logs_dir, level="INFO", console=False, force=True)
        logging_setup.log_system_banner(paths.describe(), data_root=str(paths.data_root))
        report.add(Step("Application folders", True, f"{paths.data_root} ({len(created)} created)", time.perf_counter() - started))
        return paths
    except Exception as exc:  # noqa: BLE001
        report.add(Step("Application folders", False, str(exc), time.perf_counter() - started))
        return None


def check_system(report: Report, paths) -> None:
    started = time.perf_counter()
    try:
        from app.checks import items as check_items
        from app.checks.status import run_system_check
        from app.core.settings import SettingsStore

        settings = SettingsStore(paths.settings_file, paths.settings_backup_dir).load().settings
        context = check_items.build_context(paths, settings, deep=True)
        result = run_system_check(context, order=check_items.DISPLAY_ORDER)

        detail_lines = [result.headline()]
        for item in result.results:
            detail_lines.append(f"  {item.status.glyph} {item.title}: {item.summary}")
        # Missing optional pieces (for example the voice engine) do not fail the
        # release check; a missing required piece does.
        ok = not result.problems
        report.add(Step("System check", ok, "\n".join(detail_lines), time.perf_counter() - started))
    except Exception as exc:  # noqa: BLE001
        report.add(Step("System check", False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started))


def check_smoke(report: Report, paths) -> None:
    started = time.perf_counter()
    try:
        from app.core.settings import SettingsStore
        from app.diagnostics.smoke import run_smoke_test

        settings = SettingsStore(paths.settings_file, paths.settings_backup_dir).load().settings
        result = run_smoke_test(paths, settings, verbose=False)
        report.add(Step("End-to-end smoke test", result.passed, result.to_text(), time.perf_counter() - started))
    except Exception as exc:  # noqa: BLE001
        report.add(Step("End-to-end smoke test", False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started))


def _launch_gui(paths, seconds: float, timeout: float = 120.0) -> tuple[int, str]:
    """Start the real application, let it close itself, return (code, log tail)."""
    command = [
        _python(),
        str(REPO_ROOT / "run_studio.py"),
        "--data-root", str(paths.data_root),
        "--exit-after", str(seconds),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=_child_env(), cwd=str(REPO_ROOT))
    tail = ""
    try:
        lines = (paths.log_file).read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-6:])
    except OSError:
        pass
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "") + ("\n--- log tail ---\n" + tail if tail else "")


def check_gui_startup(report: Report, paths) -> None:
    started = time.perf_counter()
    code, output = _launch_gui(paths, seconds=6.0)
    ok = code == 0 and "APP_READY" in _log_text(paths)
    report.add(Step("Application starts and closes cleanly", ok, output.strip()[-1200:], time.perf_counter() - started))


def check_restart(report: Report, paths) -> None:
    """Section 66: close the app, reopen it, everything must still be intact."""
    started = time.perf_counter()
    from app.core.settings import SettingsStore

    store = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    settings = store.load().settings
    settings.general.theme = "light" if settings.general.theme != "light" else "dark"
    expected_theme = settings.general.theme
    settings.voice.speed = 1.15
    store.save(settings, keep_backups=10)

    code, output = _launch_gui(paths, seconds=6.0)

    reloaded = SettingsStore(paths.settings_file, paths.settings_backup_dir).load()
    problems: list[str] = []
    if code != 0:
        problems.append(f"the second start exited with code {code}")
    if reloaded.settings.general.theme != expected_theme:
        problems.append("the theme setting did not survive the restart")
    if abs(reloaded.settings.voice.speed - 1.15) > 1e-6:
        problems.append("the voice speed setting did not survive the restart")
    if reloaded.settings.window.width < 1024:
        problems.append("window geometry was not written back")
    if reloaded.notes:
        problems.append(f"settings needed repair after restart: {reloaded.notes}")

    detail = "reopened and settings intact" if not problems else "\n".join(problems)
    if problems:
        detail += "\n" + output.strip()[-800:]
    report.add(Step("Restart keeps settings intact", not problems, detail, time.perf_counter() - started))


def check_crash_recovery(report: Report, paths) -> None:
    """Section 67: kill the process hard; nothing may be corrupted."""
    started = time.perf_counter()
    from app.core.settings import SettingsStore

    before = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    settings = before.load().settings
    settings.voice.speed = 1.05
    before.save(settings, keep_backups=10)
    settings_text_before = paths.settings_file.read_text(encoding="utf-8")

    command = [
        _python(), str(REPO_ROOT / "run_studio.py"),
        "--data-root", str(paths.data_root),
        "--no-single-instance",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_child_env(), cwd=str(REPO_ROOT))
    try:
        time.sleep(4.0)  # let it start and begin the readiness check
        process.kill()   # hard kill: no clean shutdown, no chance to save
        process.wait(timeout=20)
    except Exception as exc:  # noqa: BLE001
        report.add(Step("Crash leaves data intact", False, f"could not run the crash test: {exc}", time.perf_counter() - started))
        return

    problems: list[str] = []
    after = SettingsStore(paths.settings_file, paths.settings_backup_dir).load()
    if after.source not in ("file", "defaults"):
        problems.append("the settings file needed recovery after the crash")
    if after.notes and any("could not be read" in note for note in after.notes):
        problems.append("the settings file was damaged by the crash")
    leftovers = [p for p in paths.config_dir.glob(".*tmp")] + [p for p in paths.data_root.glob(".*tmp")]
    if leftovers:
        problems.append(f"temporary files were left behind: {leftovers[:3]}")
    if not paths.settings_file.exists() and settings_text_before:
        problems.append("the settings file disappeared")

    # And the application must still start normally afterwards.
    code, output = _launch_gui(paths, seconds=5.0)
    if code != 0:
        problems.append(f"the application did not start cleanly after the crash (exit {code})")
        problems.append(output.strip()[-600:])

    detail = "killed hard, data intact, clean start afterwards" if not problems else "\n".join(problems)
    report.add(Step("Crash leaves data intact", not problems, detail, time.perf_counter() - started))


def check_output_naming(report: Report, paths) -> None:
    """Section 36/68: a second output must never replace the first."""
    started = time.perf_counter()
    try:
        from app.core.paths import unique_path
        from app.tools.ffmpeg import FFmpegTools, discover_ffmpeg

        discovery = discover_ffmpeg(source_root=REPO_ROOT, extra_dirs=(paths.tools_dir, paths.data_root))
        if not discovery.is_complete:
            report.add(Step("Output naming never overwrites", False, "skipped: " + discovery.summary(), time.perf_counter() - started, skipped=True))
            return

        tools = FFmpegTools(discovery)
        first = unique_path(paths.output_dir, "ReleaseCheckVideo", ".mp4")
        second_target = None
        for index in range(2):
            target = unique_path(paths.output_dir, "ReleaseCheckVideo", ".mp4")
            result = tools.run(
                ["-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc=size=320x180:rate=25:duration=0.5",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                 "-y", str(target)],
                timeout=120,
            )
            if not result.ok or not target.exists():
                report.add(Step("Output naming never overwrites", False, result.describe_failure(), time.perf_counter() - started))
                return
            if index == 0:
                first_size = target.stat().st_size
            else:
                second_target = target

        problems: list[str] = []
        if second_target is None or second_target.name != "ReleaseCheckVideo2.mp4":
            problems.append(f"the second render was not numbered: {second_target.name if second_target else 'missing'}")
        if not first.exists() or first.stat().st_size != first_size:
            problems.append("the first video was modified or replaced")
        detail = f"produced {first.name} and {second_target.name if second_target else '?'}, both valid"
        report.add(Step("Output naming never overwrites", not problems, "\n".join(problems) if problems else detail, time.perf_counter() - started))
    except Exception as exc:  # noqa: BLE001
        report.add(Step("Output naming never overwrites", False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started))


def check_project_lifecycle(report: Report, paths) -> None:
    """Create, save, autosave, recover, duplicate and delete a real project.

    This is the Stage B equivalent of the smoke test: it exercises the same
    service the interface uses, against the real data folder.
    """
    from app.core.settings import Settings
    from app.project.service import CreateRequest, ProjectService
    from app.project.store import ProjectStore

    started = time.perf_counter()
    lines: list[str] = []
    ok = True
    try:
        service = ProjectService(paths, Settings())
        project = service.create_project(CreateRequest(name="Release Check Video", channel_name="Release"))
        folder = service.current_layout.root
        lines.append(f"created {folder.name}")

        service.set_script_text("A line of narration for the release check.")
        result = service.save()
        ok = ok and result.ok
        lines.append(f"saved version {result.project_version}, backup={result.backup.name if result.backup else 'none'}")

        service.set_script_text("Changed after the save.")
        autosave = service.autosave()
        lines.append(f"autosave written: {autosave.name if autosave else 'nothing'}")
        service.close_project(save=False)

        # A crash leaves the autosave behind; the scan must find it.
        candidates = service.scan_recovery()
        lines.append(f"recovery candidates: {len(candidates)}")
        ok = ok and bool(candidates)
        if candidates:
            restored = service.restore_recovery(candidates[0])
            ok = ok and restored.script.source_text == "Changed after the save."
            lines.append("restored the recovered script text")
        service.ignore_recovery(candidates[0]) if candidates else None
        service.close_project(save=False)

        duplicate = service.duplicate_project(folder, name="Release Check Copy", copy_assets=True)
        ok = ok and duplicate.project.id != project.project.id
        lines.append(f"duplicated as {duplicate.project.name}")
        service.close_project(save=False)

        index = service.index_projects()
        ok = ok and len(index) >= 2
        lines.append(f"projects folder holds {len(index)} project(s)")

        service.delete_project(paths.projects_dir / "Release Check Copy", confirm=True)
        lines.append("deleted the duplicate")
    except Exception as exc:  # noqa: BLE001 - the check must report, not abort
        ok = False
        lines.append(f"raised {exc!r}")

    report.add(Step("Project lifecycle (Stage B)", ok, "\n".join(lines), time.perf_counter() - started))


def check_project_cli(report: Report, data_root: Path) -> None:
    """The command line must reach the same service as the interface."""
    started = time.perf_counter()
    env = _child_env()
    base = [_python(), "-m", "app.cli.main", "--data-root", str(data_root)]
    created = subprocess.run(
        [*base, "project", "create", "--name", "CLI Release Check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=300,
    )
    listed = subprocess.run(
        [*base, "project", "list"], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=300,
    )
    validated = subprocess.run(
        [*base, "project", "validate", str(data_root / "projects" / "CLI Release Check")],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=300,
    )
    ok = created.returncode == 0 and listed.returncode == 0 and validated.returncode == 0
    detail = "\n".join(
        line
        for line in (created.stdout + listed.stdout + validated.stdout).splitlines()
        if line.strip() and not line.startswith("Log:")
    )[-1200:]
    report.add(Step("Project command line", ok, detail, time.perf_counter() - started))


def check_pytest(report: Report) -> None:
    started = time.perf_counter()
    completed = subprocess.run(
        [_python(), "-m", "pytest", "tests", "-q"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=_child_env(), timeout=1800,
    )
    tail = "\n".join((completed.stdout or "").splitlines()[-6:])
    report.add(Step("Automated test suite", completed.returncode == 0, tail, time.perf_counter() - started))


def _log_text(paths) -> str:
    try:
        return paths.log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify this build on this machine.")
    parser.add_argument("--data-root", required=True, help="Throw-away folder used for the check.")
    parser.add_argument("--skip-gui", action="store_true", help="Skip the parts that start the interface.")
    parser.add_argument("--with-pytest", action="store_true", help="Also run the automated test suite.")
    parser.add_argument("--keep", action="store_true", help="Keep the check folder afterwards.")
    args = parser.parse_args(argv)

    data_root = Path(args.data_root).expanduser().resolve()
    print("Motion Graphics Studio - release check")
    print("=" * 70)
    print(f"Data folder: {data_root}")
    print(f"Python     : {sys.version.split()[0]} at {sys.executable}")
    print("=" * 70)

    report = Report()
    paths = check_folders(report, data_root)
    if paths is None:
        print()
        print("Cannot continue without an application folder.")
        return 1

    check_system(report, paths)
    check_smoke(report, paths)
    check_project_lifecycle(report, paths)
    check_project_cli(report, data_root)
    check_output_naming(report, paths)

    if args.skip_gui:
        report.add(Step("Interface startup / restart / crash tests", True, "skipped on request", 0.0, skipped=True))
    else:
        check_gui_startup(report, paths)
        check_restart(report, paths)
        check_crash_recovery(report, paths)

    if args.with_pytest:
        check_pytest(report)

    print("=" * 70)
    failures = report.failures
    if failures:
        print(f"RELEASE CHECK FAILED: {len(failures)} step(s) failed.")
        for step in failures:
            print(f"  - {step.name}: {step.detail.splitlines()[0] if step.detail else ''}")
    else:
        passed = len([step for step in report.steps if not step.skipped])
        print(f"RELEASE CHECK PASSED ({passed} steps).")

    log_file = logging_setup.active_log_file()
    if log_file is not None:
        print(f"Log: {log_file}")

    if not args.keep:
        shutil.rmtree(data_root, ignore_errors=True)
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
