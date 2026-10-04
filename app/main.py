"""Application entry point (GUI).

Startup sequence, in this exact order (directive section 46):

1. parse command line options (data folder override, log level, safe flags)
2. create the Qt application object
3. resolve the application folders and create them
4. start logging
5. probe the machine
6. clean stale temporary files
7. single-instance guard
8. load settings, apply theme
9. install the global error handler
10. create the job manager and the main window
11. show the window, then run the readiness check **in the background**
12. shut down cleanly on exit

Everything after step 4 is logged, and every step that can fail does so with a
friendly message instead of a traceback (directive section 40).

There are deliberately **no** module-level side effects here: importing this
module does nothing until :func:`main` is called.
"""

from __future__ import annotations

import argparse
import faulthandler
import sys
from pathlib import Path
from typing import Optional, Sequence

#: Exit codes (documented so scripts can react to them).
EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_ALREADY_RUNNING = 2
EXIT_UNSUPPORTED = 3


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Motion Graphics Studio",
        description="Local, offline video production studio (Stage A foundation).",
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default=None,
        help="Folder for projects, assets, cache and output (overrides the automatic choice).",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="How much detail to write to the log file.",
    )
    parser.add_argument(
        "--no-single-instance",
        action="store_true",
        help="Allow more than one copy of the application to run at the same time.",
    )
    parser.add_argument(
        "--reset-settings",
        action="store_true",
        help="Ignore the saved settings for this run (they are not deleted).",
    )
    parser.add_argument("--version", action="store_true", help="Print the version and exit.")
    # Used by the automated startup test (tests/test_startup.py) and by
    # packaging checks: start the real application, then close it after a while.
    parser.add_argument(
        "--exit-after",
        type=float,
        default=None,
        metavar="SECONDS",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Early logging helpers (used before the logging system exists)
# --------------------------------------------------------------------------

def _early_stderr(message: str) -> None:
    """Last-resort output when even logging is unavailable."""
    try:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover
        pass


def _show_fatal_dialog(title: str, text: str, detailed: str = "") -> None:
    """Show a blocking error before the main window exists."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        # Keep a reference: the application object must outlive the dialog.
        _app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(text)
        if detailed:
            box.setDetailedText(detailed)
        box.exec()
    except Exception:  # pragma: no cover - no GUI available at all
        _early_stderr(f"{title}: {text}\n{detailed}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)

    if args.version:
        from .core.version import full_version_string

        print(full_version_string())
        return EXIT_OK

    # -- 1. Qt application object -----------------------------------------
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    from .core.version import APP_NAME, APP_PUBLISHER, APP_VERSION

    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_PUBLISHER)
    app.setApplicationDisplayName(APP_NAME)
    # Closing the last window quits; we handle cleanup in closeEvent.
    app.setQuitOnLastWindowClosed(True)

    # -- 2. Folders --------------------------------------------------------
    from .core.paths import AppPaths, PathResolutionError, default_user_data_root

    cli_override = Path(args.data_root) if args.data_root else None
    paths = AppPaths.bootstrap(cli_override=cli_override)
    try:
        created = paths.ensure()
    except PathResolutionError as exc:
        fallback = default_user_data_root()
        if Path(fallback) != Path(paths.data_root):
            _early_stderr(f"Falling back to {fallback}: {exc}")
            try:
                fallback_paths = AppPaths.bootstrap(cli_override=Path(fallback))
                fallback_paths.ensure()
                paths = fallback_paths
                created = []
            except PathResolutionError as second:
                _show_fatal_dialog(
                    f"{APP_NAME} cannot start",
                    "The application folders could not be created.",
                    f"{second}\n\nTried:\n  {exc}",
                )
                return EXIT_STARTUP_FAILED
        else:
            _show_fatal_dialog(
                f"{APP_NAME} cannot start",
                "The application folder could not be created.",
                str(exc),
            )
            return EXIT_STARTUP_FAILED

    # -- 3. Logging --------------------------------------------------------
    from .core import logging_setup

    level = args.log_level or "INFO"
    log_file = logging_setup.setup_logging(paths.logs_dir, level=level, console=False)
    logger = logging_setup.get_logger("main")
    logging_setup.log_system_banner(paths.describe(), data_root=str(paths.data_root))
    logger.info("Created %d folder(s) at startup", len(created))

    # Fatal-signal diagnostics: a hard crash still leaves a traceback behind.
    if log_file is not None:
        try:
            crash_file = open(paths.logs_dir / "crash-python.log", "a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=crash_file, all_threads=True)
        except OSError:  # pragma: no cover - diagnostics only
            crash_file = None

    _install_qt_message_handler(logger)

    # -- 4. Pre-flight -----------------------------------------------------
    if not paths.is_inside_data_root(paths.output_dir):  # pragma: no cover - invariant guard
        logger.error("Output folder resolved outside the data folder - refusing to continue")
        _show_fatal_dialog(APP_NAME, "The folder layout is invalid.", paths.describe())
        return EXIT_STARTUP_FAILED

    from .core import maintenance

    removed = maintenance.cleanup_stale_temporaries(paths)
    if removed:
        logger.info("Removed %d stale temporary file(s)", removed)

    # -- 5. Single instance ------------------------------------------------
    from .ui.single_instance import SingleInstanceGuard, detect_existing_instance

    guard: Optional[SingleInstanceGuard] = None
    allow_multi = args.no_single_instance
    from .core.settings import SettingsStore

    store = SettingsStore(paths.settings_file, paths.settings_backup_dir)
    load_result = store.load()
    settings = load_result.settings
    if args.reset_settings:
        from .core.settings import Settings as _Settings

        settings = _Settings()
        logger.info("Settings ignored for this run (--reset-settings)")

    if settings.general.single_instance and not allow_multi:
        running, error = detect_existing_instance(paths.data_root)
        if running:
            logger.info("Another instance is already running for this data folder - focusing it")
            guard = SingleInstanceGuard(paths.data_root)
            guard.notify_existing("raise")
            _show_fatal_dialog(
                APP_NAME,
                "Motion Graphics Studio is already running.",
                "Only one copy can use a data folder at a time, so that two windows cannot "
                "write the same project.\n\nUse the window that is already open, start the "
                "application with --no-single-instance, or use Settings -> Folders to point "
                "this copy at a different data folder.",
            )
            return EXIT_ALREADY_RUNNING
        if error:
            logger.warning("Single-instance check failed (%s); continuing with a warning", error)
        guard = SingleInstanceGuard(paths.data_root)
        if not guard.try_acquire():
            logger.warning("Could not create the single-instance guard: %s", guard.error)
            guard = None
    else:
        logger.info("Single-instance guard disabled")

    # -- 6. Theme + error handling ----------------------------------------
    from .ui.theme import apply_theme

    scheme = apply_theme(app, settings.general.theme)
    logger.info("Theme applied: %s", scheme.name)

    from .ui.error_handler import install_exception_hook

    install_exception_hook(app)

    # -- 7. Services -------------------------------------------------------
    from .jobs.manager import JobManager
    from .ui.context import StartupInfo, create_context

    jobs = JobManager()
    startup = StartupInfo(
        data_root_reason=paths.reason,
        directories_created=len(created),
        stale_temp_removed=removed,
        warnings=tuple(load_result.notes),
    )

    # ``--reset-settings`` keeps the defaults in memory only: the file on disk is
    # never touched, so the user's saved settings survive a diagnostic run.
    context = create_context(
        paths,
        jobs,
        startup=startup,
        load_result=None if args.reset_settings else load_result,
    )

    # -- 8. Window ---------------------------------------------------------
    from .ui.main_window import MainWindow

    window = MainWindow(context, jobs)
    if guard is not None:
        guard.message_received.connect(lambda _msg: _raise_window(window))
    window.show()
    window.raise_()
    window.activateWindow()

    logging_setup.log_event(
        "APP_READY",
        "Main window shown",
        logger=logger,
        data_root=str(paths.data_root),
        theme=scheme.name,
        seconds=None,
    )

    window.begin_startup_checks()

    if args.exit_after:
        # Deliberate, logged self-closure used by automated startup tests.
        logger.info("Startup test mode: closing automatically after %.1f seconds", args.exit_after)

        def _auto_close() -> None:
            window.close()
            app.quit()

        QTimer.singleShot(int(max(0.5, args.exit_after) * 1000), _auto_close)

    # -- 9. Run ------------------------------------------------------------
    exit_code = app.exec()

    # -- 10. Teardown ------------------------------------------------------
    if guard is not None:
        guard.release()
    logging_setup.log_event("APP_EXIT", "Application finished", logger=logger, code=exit_code)
    logging_setup.shutdown_logging()
    return exit_code


def _raise_window(window) -> None:
    """Bring the existing window to the front when a second instance starts."""
    try:
        window.showNormal()
        window.raise_()
        window.activateWindow()
        window.context.notify("Another copy of the application was started - this window is already open.", 6000)
    except Exception:  # pragma: no cover - defensive
        pass


def _install_qt_message_handler(logger) -> None:
    """Route Qt's own warnings into the application log.

    Qt reports useful problems this way (a widget that cannot be styled, a
    geometry that does not fit).  Debug messages are dropped to keep the log
    readable; the rest is written at the matching level.
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # pragma: no cover
        return

    # Qt can emit the same warning thousands of times (for example
    # "This plugin does not support propagateSizeHints()" on a headless
    # platform).  Log each distinct message once, then count it, so the log
    # stays readable and useful.
    seen: dict[str, int] = {}

    def handler(mode, context, message: str) -> None:
        try:
            if mode == QtMsgType.QtDebugMsg:
                return
            count = seen.get(message, 0)
            seen[message] = count + 1
            if count >= 2:
                if count == 2:
                    logger.debug("Qt: further repeats of '%s' are not logged individually", message)
                return
            suffix = "" if count == 0 else " (second occurrence)"
            if mode == QtMsgType.QtInfoMsg:
                logger.info("Qt: %s%s", message, suffix)
            elif mode == QtMsgType.QtWarningMsg:
                logger.warning("Qt: %s%s", message, suffix)
            else:
                logger.error("Qt: %s%s", message, suffix)
        except Exception:  # pragma: no cover - never break Qt's logging
            pass

    qInstallMessageHandler(handler)


def run() -> None:
    """Console-script entry point (``python -m app.main``)."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover - manual launch path
    run()
