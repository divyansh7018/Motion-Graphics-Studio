"""Structured logging setup (directive section 39).

Properties that matter for stability:

* **Idempotent** - calling :func:`setup_logging` twice never duplicates
  handlers.
* **Never fatal** - if the log folder cannot be written to, the application
  still runs (logging degrades to a null handler) instead of refusing to start.
* **Session aware** - every line carries a session id so the log of one run can
  be extracted with a single grep.
* **Structured events** - :func:`log_event` writes ``EVENT=<name> key=value``
  pairs into the message and the ``extra`` fields, which makes the log
  readable for humans and parseable for tools.
* **No import-time side effects** - nothing is opened until :func:`setup_logging`
  is called by the application entry point.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .version import APP_NAME, APP_VERSION

LOGGER_NAME = "mgs"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB per file
DEFAULT_BACKUP_COUNT = 5
DEFAULT_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(session_id)s | "
    "p%(process)d t%(threadName)s | %(name)s | %(message)s"
)
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Populated by setup_logging so the UI can show the active log file.
_active_log_file: Optional[Path] = None
_session_id: str = "-"
_configured = False


class _SessionFilter(logging.Filter):
    """Injects the session id into every record."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102 - stdlib API
        if not hasattr(record, "session_id"):
            record.session_id = self.session_id
        return True


def new_session_id() -> str:
    """Short, sortable, unique id identifying one application run."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def current_session_id() -> str:
    return _session_id


def active_log_file() -> Optional[Path]:
    """The log file currently in use, or ``None`` when logging is disabled."""
    return _active_log_file


def setup_logging(
    log_dir: Optional[Path] = None,
    level: str | int = "INFO",
    console: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    session_id: Optional[str] = None,
    force: bool = False,
) -> Optional[Path]:
    """Configure application logging.

    Returns the log file path, or ``None`` when file logging is unavailable
    (for example a read-only disk) - the caller must not treat that as fatal.
    """
    global _active_log_file, _session_id, _configured

    logger = logging.getLogger(LOGGER_NAME)
    resolved_level = _coerce_level(level)
    logger.setLevel(resolved_level)
    logger.propagate = False

    if _configured and not force:
        # Keep the existing handlers but allow a level change from Settings.
        for handler in logger.handlers:
            handler.setLevel(resolved_level)
        if session_id:
            _session_id = session_id
        return _active_log_file

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass

    _session_id = session_id or new_session_id()
    session_filter = _SessionFilter(_session_id)

    file_handler: Optional[logging.Handler] = None
    if log_dir is not None:
        log_dir = Path(log_dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "studio.log"
            handler = logging.handlers.RotatingFileHandler(
                filename=str(log_path),
                maxBytes=max(int(max_bytes), 64 * 1024),
                backupCount=max(int(backup_count), 0),
                encoding="utf-8",
                delay=True,  # do not create the file until the first record
            )
            handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
            handler.addFilter(session_filter)
            logger.addHandler(handler)
            _active_log_file = log_path
            file_handler = handler
        except OSError:
            _active_log_file = None

    if file_handler is None:
        # Fall back to a null handler so logging calls never raise.
        logger.addHandler(logging.NullHandler())
    else:
        # Keep errors visible on the console when one is attached (CLI use).
        if console and sys.stderr is not None:
            stream = logging.StreamHandler(stream=sys.stderr)
            stream.setFormatter(logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT))
            stream.addFilter(session_filter)
            stream.setLevel(logging.WARNING)
            logger.addHandler(stream)

    _configured = True
    return _active_log_file


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).strip().upper(), logging.INFO)


def set_level(level: str | int) -> int:
    """Change the log level of the application logger at runtime."""
    resolved = _coerce_level(level)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(resolved)
    for handler in logger.handlers:
        handler.setLevel(resolved)
    return resolved


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger of the application logger."""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def shutdown_logging() -> None:
    """Flush and close handlers (called on clean exit, section 70)."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass
        logger.removeHandler(handler)
    _configured = False


#: Level names accepted by :func:`log_event`, so a caller can say "WARNING"
#: as easily as ``logging.WARNING``.  Reporting a problem must never be the
#: thing that fails: every warning branch in this application goes through here,
#: and a mistyped level would turn "warn about it" into "crash on it".
LEVEL_NAMES: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def resolve_level(level: Any) -> int:
    """An integer logging level from an integer, a name, or nothing usable."""
    if isinstance(level, bool):  # bool is an int subclass; treat it as "unset"
        return logging.INFO
    if isinstance(level, int):
        return int(level)
    name = str(level or "").strip().upper()
    if name in LEVEL_NAMES:
        return LEVEL_NAMES[name]
    numeric = logging.getLevelName(name)
    if isinstance(numeric, int):
        return numeric
    return logging.INFO


def log_event(
    event: str,
    message: str = "",
    level: Any = logging.INFO,
    logger: Optional[logging.Logger] = None,
    **fields: Any,
) -> None:
    """Write a structured event line.

    Format::

        EVENT=JOB_START job=tts_preview id=7 note="..."

    Values containing spaces are quoted; ``None`` values are skipped.  ``level``
    may be an integer (``logging.WARNING``) or its name (``"WARNING"``).
    """
    target = logger or get_logger()
    level = resolve_level(level)
    rendered_fields = " ".join(
        f"{key}={_format_value(value)}" for key, value in sorted(fields.items()) if value is not None
    )
    text = f"EVENT={event}"
    if message:
        text += f" | {message}"
    if rendered_fields:
        text += f" | {rendered_fields}"
    extra = {"event": event}
    extra.update({f"field_{k}": v for k, v in fields.items()})
    target.log(level, text, extra=extra)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (int,)):
        return str(value)
    text = str(value)
    if len(text) > 300:
        text = text[:297] + "..."
    if any(ch.isspace() for ch in text) or text == "":
        return '"{}"'.format(text.replace('"', "'"))
    return text


def log_system_banner(paths_description: str, data_root: Optional[str] = None) -> None:
    """Log the standard startup banner."""
    logger = get_logger("startup")
    log_event(
        "APP_START",
        f"{APP_NAME} {APP_VERSION}",
        logger=logger,
        version=APP_VERSION,
        python=sys.version.split()[0],
        platform=sys.platform,
        session=current_session_id(),
        pid=os.getpid(),
        data_root=data_root,
    )
    logger.debug(paths_description)


def log_exception(message: str, exc: BaseException, event: str = "ERROR", **fields: Any) -> None:
    """Log an exception with its traceback, keeping the log grep-friendly."""
    logger = get_logger("error")
    log_event(event, message, level=logging.ERROR, logger=logger, error_type=type(exc).__name__, **fields)
    logger.exception(str(exc))


def log_path_for_user() -> str:
    """Text shown in error dialogs so users can find the log themselves."""
    path = active_log_file()
    if path is None:
        return "(file logging unavailable - check that the data folder is writable)"
    return str(path)
