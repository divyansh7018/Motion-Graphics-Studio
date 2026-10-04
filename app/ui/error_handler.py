"""Global error boundaries (directive sections 40, 41).

Two hooks are installed at startup:

* :func:`install_exception_hook` catches unhandled exceptions on the Qt main
  thread.  Instead of the application dying - taking unsaved work with it - the
  error is logged, shown with a friendly explanation, and the user keeps
  working.
* :func:`guard_slot` wraps a callback so a failure inside a UI action cannot
  propagate into Qt's event loop.

The job system already converts worker-thread failures into results, so a
failing subsystem never reaches this module - which is exactly the point: this
is the last line of defence, not the first.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import traceback
from typing import Callable, Optional

from PySide6.QtCore import QCoreApplication, QTimer

from ..core.errors import to_friendly
from ..core.events import Event
from ..core.logging_setup import get_logger, log_event

LOGGER = get_logger("crash")

#: Set once a fatal dialog is on screen so repeated errors cannot stack dialogs.
_dialog_open = False


def install_exception_hook(app: Optional[QCoreApplication] = None) -> None:
    """Install the unhandled-exception hook for the main thread."""

    def hook(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        log_event(
            Event.APP_CRASH,
            "Unhandled error in the user interface",
            level=logging.ERROR,
            logger=LOGGER,
            error_type=exc_type.__name__,
        )
        LOGGER.error("Unhandled exception:\n%s", text)

        friendly = dataclasses.replace(
            to_friendly(exc_value, "An unexpected error occurred in the interface."),
            technical=text,
        )
        _show_friendly_later(friendly)

    sys.excepthook = hook


def _show_friendly_later(friendly) -> None:
    """Show the dialog on the next event-loop turn (never from inside a handler)."""
    global _dialog_open

    def show() -> None:
        global _dialog_open
        if _dialog_open:
            log_event("ERROR", "Additional error suppressed while a dialog is open", logger=LOGGER)
            return
        _dialog_open = True
        try:
            from .notifications import show_error

            show_error(None, friendly, title=friendly.title)
        finally:
            _dialog_open = False

    QTimer.singleShot(0, show)


def guard_slot(callback: Callable, description: str = "This action") -> Callable:
    """Wrap a UI callback so an exception is reported instead of crashing Qt."""

    def wrapper(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - the whole point of a boundary
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            text = traceback.format_exc()
            log_event(
                Event.ERROR,
                f"{description} failed",
                level=logging.ERROR,
                logger=LOGGER,
                error_type=type(exc).__name__,
            )
            LOGGER.error("Guarded action failed:\n%s", text)
            friendly = dataclasses.replace(
                to_friendly(exc, f"{description} could not be completed."),
                technical=text,
            )
            _show_friendly_later(friendly)
            return None

    return wrapper
