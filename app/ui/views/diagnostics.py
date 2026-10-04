"""Diagnostics page: machine facts, folders, settings and the recent log.

This is the page a user opens when asked "please send the log".  It reads the
log file with a bounded tail (never the whole file) so a large log cannot slow
the interface down (directive section 48).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QWidget

from ...core import logging_setup
from ..notifications import copy_to_clipboard, open_folder, show_info
from ..theme import METRICS
from ..widgets.common import HintLabel, KeyValueGrid, Page

LOG_TAIL_LINES = 500


class DiagnosticsPage(Page):
    """Read-only view of everything useful for troubleshooting."""

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Diagnostics",
            "Technical information about this installation. Nothing here changes your project.",
            parent,
        )
        self.context = context

        self._build_actions()
        self._build_details_card()
        self._build_log_card()
        self.refresh()

    # -- building ----------------------------------------------------------

    def _build_actions(self) -> None:
        card = self.add_card()
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)

        copy_button = QPushButton("Copy diagnostics")
        copy_button.setToolTip("Copies folders, settings, machine details and the last system check as text.")
        copy_button.clicked.connect(self._copy_all)
        row.addWidget(copy_button)

        log_folder = QPushButton("Open log folder")
        log_folder.clicked.connect(lambda: open_folder(self, self.context.paths.logs_dir))
        row.addWidget(log_folder)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)

        row.addStretch(1)
        card.body().addLayout(row)

    def _build_details_card(self) -> None:
        card = self.add_card("Overview")
        self.details_grid = KeyValueGrid()
        card.add(self.details_grid)

    def _build_log_card(self) -> None:
        card = self.add_card(f"Log (last {LOG_TAIL_LINES} lines)")
        card.add(HintLabel(
            "Every important action is recorded here with a timestamp: project saves, voice generation, "
            "rendering, FFmpeg output, quality checks and errors."
        ))

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setMinimumHeight(220)
        card.add(self.log_view)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        reload_button = QPushButton("Reload log")
        reload_button.clicked.connect(self._reload_log)
        row.addWidget(reload_button)

        copy_log = QPushButton("Copy log")
        copy_log.clicked.connect(self._copy_log)
        row.addWidget(copy_log)

        clear_button = QPushButton("Clear log file")
        clear_button.setToolTip("Starts a fresh log file. Older logs are kept as rotated backups.")
        clear_button.clicked.connect(self._clear_log)
        row.addWidget(clear_button)

        row.addStretch(1)
        card.body().addLayout(row)
        self.log_path_label = QLabel("")
        self.log_path_label.setObjectName("Hint")
        self.log_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.add(self.log_path_label)

    # -- data --------------------------------------------------------------

    def refresh(self) -> None:
        context = self.context
        paths = context.paths

        self.details_grid.clear()
        self.details_grid.add("Version", f"{self._version_string()}")
        self.details_grid.add("Session", logging_setup.current_session_id())
        self.details_grid.add("Log level", context.settings.logging.level)
        self.details_grid.add("Data folder", str(paths.data_root), monospace=True)
        self.details_grid.add("Folder chosen because", paths.reason)
        self.details_grid.add("Source folder", str(paths.source_root), monospace=True)
        self.details_grid.add("Settings file", str(paths.settings_file), monospace=True)
        self.details_grid.add("Projects folder", str(paths.projects_dir), monospace=True)
        self.details_grid.add("Output folder", str(paths.output_dir), monospace=True)
        self.details_grid.add("Temporary folder", str(paths.temp_dir), monospace=True)

        environment = context.environment
        if environment is not None:
            for line in environment.summary_lines():
                if ":" in line:
                    label, value = line.split(":", 1)
                    self.details_grid.add(label.strip(), value.strip())
                else:
                    self.details_grid.add("", line)

        report = context.last_report
        self.details_grid.add("Last system check", report.headline() if report is not None else "not run yet")
        self._reload_log()

    @staticmethod
    def _version_string() -> str:
        from ...core.version import full_version_string

        return full_version_string()

    def _reload_log(self) -> None:
        path = logging_setup.active_log_file()
        if path is None:
            self.log_view.setPlainText("File logging is not available (the data folder may be read-only).")
            self.log_path_label.setText("")
            return
        self.log_path_label.setText(f"Log file: {path}")
        try:
            lines = _tail_lines(path, LOG_TAIL_LINES)
        except OSError as exc:
            self.log_view.setPlainText(f"The log file could not be read: {exc}")
            return
        self.log_view.setPlainText("\n".join(lines))
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _copy_log(self) -> None:
        copy_to_clipboard(self.log_view.toPlainText())
        self.context.notify("Log copied to the clipboard.")

    def _copy_all(self) -> None:
        copy_to_clipboard(self.context.diagnostics_text())
        self.context.notify("Diagnostics copied to the clipboard.")

    def _clear_log(self) -> None:
        from ..notifications import ask_confirm

        if not ask_confirm(
            self,
            "Start a fresh log file?",
            "The current log is rotated (kept as studio.log.1) so nothing is lost. "
            "This is useful when you want a clean log of one action.",
            confirm_label="Clear log",
        ):
            return
        target = logging_setup.active_log_file()
        if target is None:
            return
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            backup = target.with_suffix(".log.manual")
            backup.write_text(text, encoding="utf-8")
            target.write_text("", encoding="utf-8")
        except OSError as exc:
            show_info(self, "The log file could not be cleared.", str(exc))
            return
        logging_setup.log_event("APP_START", "Log cleared by the user")
        self._reload_log()


def _tail_lines(path: Path, count: int) -> list[str]:
    """Return the last *count* lines without loading a huge file into memory."""
    chunk_size = 64 * 1024
    lines: list[bytes] = []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        while position > 0 and len(lines) <= count:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
        if position == 0 and not lines:
            lines = buffer.splitlines()
    tail = lines[-count:]
    return [line.decode("utf-8", errors="replace") for line in tail]
