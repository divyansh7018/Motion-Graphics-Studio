"""Welcome page: what this build can do right now, and what is next.

Directive sections 52 and 54 shape this page: a creator opening the app sees
the beginner workflow, and anything that is not implemented yet is stated
plainly as "not in this build yet" instead of being shown as a dead button.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...core.version import APP_STAGE_LABEL, APP_VERSION, PRIMARY_PLATFORM
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page


class WelcomePage(Page):
    """Landing page with the current state of the build."""

    open_system_check = Signal()
    open_settings = Signal()
    open_data_folder = Signal()
    open_output_folder = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Welcome",
            "Motion Graphics Studio builds videos locally on this PC - no account, no internet connection, no GPU.",
            parent,
        )
        self.context = context

        self._build_status_card()
        self._build_workflow_card()
        self._build_folders_card()
        self.refresh()

    # -- cards -------------------------------------------------------------

    def _build_status_card(self) -> None:
        card = self.add_card("This build")
        self.status_grid = KeyValueGrid()
        card.add(self.status_grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        self.check_button = QPushButton("Run system check")
        mark_primary(self.check_button)
        self.check_button.clicked.connect(self.open_system_check.emit)
        buttons.addWidget(self.check_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings.emit)
        buttons.addWidget(self.settings_button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

    def _build_workflow_card(self) -> None:
        card = self.add_card("The local workflow")
        hint = HintLabel(
            "Everything below runs on this computer. Items marked 'later stage' are not built yet - "
            "they will appear as they are finished and tested."
        )
        card.add(hint)

        self.workflow_rows: list[QLabel] = []
        steps = [
            ("Project creation", "next stage (B)"),
            ("Script", "later stage"),
            ("Kokoro voice", "later stage (C)"),
            ("Voice preview", "later stage (C)"),
            ("Images", "later stage (H)"),
            ("Scenes and storyboard", "later stage (D)"),
            ("Preview", "later stage (D)"),
            ("Audio mixing", "later stage (E)"),
            ("Rendering to MP4 (FFmpeg)", "later stage (F)"),
            ("Quality control and output", "later stage (G)"),
            ("Video library", "later stage (G)"),
        ]
        grid = QVBoxLayout()
        grid.setSpacing(2)
        for name, state in steps:
            row = QHBoxLayout()
            bullet = QLabel("•")
            bullet.setFixedWidth(14)
            row.addWidget(bullet)
            label = QLabel(name)
            row.addWidget(label)
            row.addStretch(1)
            state_label = QLabel(state)
            state_label.setObjectName("Hint")
            row.addWidget(state_label)
            wrapper = QWidget()
            wrapper.setLayout(row)
            grid.addWidget(wrapper)
            self.workflow_rows.append(state_label)
        card.body().addLayout(grid)

        note = HintLabel(
            "Implemented in this build: application shell, settings, folder management, "
            "logging, background job system with cancellation, and the system check."
        )
        card.add(note)

    def _build_folders_card(self) -> None:
        card = self.add_card("Folders")
        self.folders_grid = KeyValueGrid()
        card.add(self.folders_grid)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        data_button = QPushButton("Open data folder")
        data_button.clicked.connect(self.open_data_folder.emit)
        row.addWidget(data_button)
        output_button = QPushButton("Open output folder")
        output_button.clicked.connect(self.open_output_folder.emit)
        row.addWidget(output_button)
        row.addStretch(1)
        card.body().addLayout(row)

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> None:
        context = self.context
        paths = context.paths

        self.status_grid.clear()
        self.status_grid.add("Application", f"Motion Graphics Studio {APP_VERSION}")
        self.status_grid.add("Build stage", APP_STAGE_LABEL)
        self.status_grid.add("Target machine", PRIMARY_PLATFORM)
        self.status_grid.add("Session", self._session_text())
        self.status_grid.add("System check", self._readiness_text())

        self.folders_grid.clear()
        self.folders_grid.add("Data folder", str(paths.data_root), monospace=True)
        self.folders_grid.add("Chosen because", paths.reason)
        self.folders_grid.add("Projects", str(paths.projects_dir), monospace=True)
        self.folders_grid.add("Output", str(paths.output_dir), monospace=True)
        self.folders_grid.add("Logs", str(paths.logs_dir), monospace=True)

    def _session_text(self) -> str:
        from ...core.logging_setup import current_session_id

        return current_session_id()

    def _readiness_text(self) -> str:
        report = self.context.last_report
        if report is None:
            return "not run yet"
        return report.headline()
