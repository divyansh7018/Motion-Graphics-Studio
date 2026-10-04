"""Command line interface.

The CLI exists so the same core can be driven without a GUI - useful for
support ("run a system check and send me the report"), for automated tests and
for the end-to-end smoke test required by directive section 57.

Commands
--------
``setup``      create/verify the application folders and print a readiness report
``check``      run the system check (``--deep`` adds the FFmpeg encode self-test)
``info``       print folders, settings and machine facts
``clean``      remove cached/temporary files (``--all`` for everything)
``smoke-test`` run the Stage A end-to-end smoke test
``project``    create, open, validate, list, duplicate, rename, recover projects
``gui``        start the graphical application (same as the normal launcher)

The ``project`` commands share :class:`app.project.service.ProjectService` with
the GUI, so the two can never drift apart (directive section 32).

Every command returns an exit code: ``0`` success, ``1`` problems, ``2``
blocked (a required component is missing).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..core import logging_setup, maintenance
from ..core.errors import AppError, to_friendly
from ..core.paths import AppPaths, PathResolutionError
from ..core.settings import SettingsStore, settings_summary
from ..core.version import full_version_string

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BLOCKED = 2
EXIT_ERROR = 3


# --------------------------------------------------------------------------
# Shared setup
# --------------------------------------------------------------------------

def _bootstrap(args) -> tuple[AppPaths, object]:
    """Resolve folders, create them, start logging and load settings."""
    cli_override = Path(args.data_root) if getattr(args, "data_root", None) else None
    paths = AppPaths.bootstrap(cli_override=cli_override)
    paths.ensure()
    log_file = logging_setup.setup_logging(
        paths.logs_dir,
        level=getattr(args, "log_level", None) or "INFO",
        console=True,
    )
    if log_file is None:
        print("Warning: the log file could not be created - check that the data folder is writable.")
    logging_setup.log_system_banner(paths.describe(), data_root=str(paths.data_root))
    store = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    load_result = store.load()
    return paths, load_result


def _print_header(paths: AppPaths) -> None:
    print(full_version_string())
    print(paths.describe())
    print(f"Log: {logging_setup.active_log_file() or 'unavailable'}")
    print()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def command_setup(args) -> int:
    """Create folders and report readiness without needing the GUI."""
    paths, load_result = _bootstrap(args)
    _print_header(paths)

    statuses = paths.directory_statuses()
    problems = [status for status in statuses if not (status.exists and status.writable)]
    print(f"Folders: {len(statuses) - len(problems)}/{len(statuses)} ready")
    for status in statuses:
        mark = "ok " if status.exists and status.writable else "BAD"
        print(f"  [{mark}] {status.name:<12} {status.path}")
    print()

    if load_result.notes:
        print("Settings notes:")
        for note in load_result.notes:
            print(f"  - {note}")
        print()
    print(settings_summary(load_result.settings))
    print()

    if problems:
        print("Setup is NOT complete: fix the folders above and run 'setup' again.")
        return EXIT_PROBLEMS

    print("Folder setup complete.")
    print("Run 'check' to verify FFmpeg, the voice engine and Python packages.")
    return EXIT_OK


def command_check(args) -> int:
    """Run the readiness check in-process (no Qt needed)."""
    from ..checks import items as check_items
    from ..checks.status import run_system_check

    paths, load_result = _bootstrap(args)
    _print_header(paths)

    context = check_items.build_context(paths, load_result.settings, deep=bool(args.deep))

    def progress(done: int, total: int, title: str) -> None:
        if total:
            print(f"  [{done}/{total}] {title}")

    report = run_system_check(context, order=check_items.DISPLAY_ORDER, progress=progress if args.verbose else None)
    print()
    print(report.to_text())
    print()

    if report.blockers:
        return EXIT_BLOCKED
    if report.missing:
        return EXIT_PROBLEMS
    return EXIT_OK


def command_info(args) -> int:
    paths, load_result = _bootstrap(args)
    _print_header(paths)

    from ..core import env as env_module

    environment = env_module.probe_environment()
    print("Machine:")
    for line in environment.summary_lines():
        print(f"  {line}")
    print()
    print("Settings:")
    print(settings_summary(load_result.settings))
    print()
    storage = maintenance.storage_report(paths)
    print("Storage:")
    for key, value in storage.items():
        if isinstance(value, dict):
            print(f"  {key:<15} {value.get('size', value.get('text', ''))}")
    return EXIT_OK


def command_clean(args) -> int:
    paths, _load_result = _bootstrap(args)
    _print_header(paths)

    targets = list(maintenance.CLEARABLE_TARGETS) if args.all else ["temp_files"]
    from ..jobs.progress import ProgressReporter

    reporter = ProgressReporter(callback=lambda progress: None, unit="folders")
    report = maintenance.run_cleanup(paths, targets, progress=reporter)
    for outcome in report.outcomes:
        print(f"  {outcome.summary()}")
    print()
    print(report.summary())
    return EXIT_OK if not report.errors else EXIT_PROBLEMS


def command_smoke_test(args) -> int:
    """End-to-end smoke test of the pieces that exist in this build."""
    from ..diagnostics.smoke import run_smoke_test

    paths, load_result = _bootstrap(args)
    _print_header(paths)
    result = run_smoke_test(paths, load_result.settings, verbose=True)
    print()
    print(result.summary())
    return EXIT_OK if result.passed else EXIT_PROBLEMS


def command_gui(args) -> int:
    from ..main import main as gui_main

    argv: list[str] = []
    if getattr(args, "data_root", None):
        argv.extend(["--data-root", args.data_root])
    if getattr(args, "log_level", None):
        argv.extend(["--log-level", args.log_level])
    return gui_main(argv)


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motion-studio",
        description="Motion Graphics Studio command line tools.",
    )
    parser.add_argument("--data-root", help="Folder for projects, assets, cache and output.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser("setup", help="Create and verify the application folders.")
    setup_parser.set_defaults(func=command_setup)

    check_parser = subparsers.add_parser("check", help="Run the system check.")
    check_parser.add_argument("--deep", action="store_true", help="Include the FFmpeg encode self-test.")
    check_parser.add_argument("--verbose", action="store_true", help="Print each check as it runs.")
    check_parser.set_defaults(func=command_check)

    info_parser = subparsers.add_parser("info", help="Print folders, machine facts and settings.")
    info_parser.set_defaults(func=command_info)

    clean_parser = subparsers.add_parser("clean", help="Remove cached and temporary files.")
    clean_parser.add_argument("--all", action="store_true", help="Clear every cache folder.")
    clean_parser.set_defaults(func=command_clean)

    smoke_parser = subparsers.add_parser("smoke-test", help="Run the built-in end-to-end smoke test.")
    smoke_parser.set_defaults(func=command_smoke_test)

    from .project import build_project_parser

    build_project_parser(subparsers)

    gui_parser = subparsers.add_parser("gui", help="Start the graphical application.")
    gui_parser.set_defaults(func=command_gui)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        print()
        print("Run 'motion-studio setup' first, then 'motion-studio check'.")
        return EXIT_OK

    try:
        if getattr(args, "command", None) == "project":
            return _dispatch_project(args)
        return int(args.func(args))
    except PathResolutionError as exc:
        print(f"The application folders could not be prepared:\n{exc}")
        return EXIT_BLOCKED
    except AppError as exc:
        friendly = exc.friendly()
        print()
        print(friendly.to_message())
        return EXIT_PROBLEMS
    except KeyboardInterrupt:
        print("\nCancelled.")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI must never dump a raw traceback
        friendly = to_friendly(exc, "The command failed.")
        print()
        print(friendly.to_message())
        return EXIT_ERROR
    finally:
        logging_setup.shutdown_logging()


def _dispatch_project(args) -> int:
    """Run a ``project`` subcommand against a bootstrapped application."""
    from .project import run_project_command

    paths, load_result = _bootstrap(args)
    if getattr(args, "project_command", None):
        _print_header(paths)
    return run_project_command(args, paths, load_result.settings)


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
