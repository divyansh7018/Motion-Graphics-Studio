"""System Check page (directive sections 46, 64, 65).

Shows one row per checked item with ✓ / ⚠ / ✗ and, for anything that is not
ready, the three-part explanation (what happened / why / what to do).  The
check itself runs as a background job, so the window stays responsive and the
user can cancel it.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..notifications import copy_to_clipboard, open_log_folder
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, Page, StatusLine, separator


class CheckItemWidget(QFrame):
    """One expandable check row."""

    def __init__(self, result, theme: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.result = result
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.xs)

        self.line = StatusLine(
            status_value=result.status.value,
            glyph=result.status.glyph,
            title=result.title,
            summary=result.summary,
            theme=theme,
        )
        layout.addWidget(self.line)

        self.details_widget = QWidget()
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(26, 0, 0, METRICS.sm)
        details_layout.setSpacing(2)

        if result.why:
            details_layout.addWidget(self._wrapped(f"Why: {result.why}"))
        for index, action in enumerate(result.actions, start=1):
            details_layout.addWidget(self._wrapped(f"{index}. {action}"))
        for detail in result.details:
            label = QLabel(detail)
            label.setObjectName("Mono")
            label.setWordWrap(True)
            details_layout.addWidget(label)
        if result.technical:
            technical = QLabel(result.technical)
            technical.setObjectName("Hint")
            technical.setWordWrap(True)
            details_layout.addWidget(technical)
        if result.required_for:
            required = QLabel(f"Needed for: {result.required_for}")
            required.setObjectName("Hint")
            details_layout.addWidget(required)

        self.details_widget.setVisible(bool(result.status.is_problem or result.status.value in ("warning", "optional")))
        layout.addWidget(self.details_widget)

        if self.details_widget.layout().count() == 0:
            self.details_widget.setVisible(False)

    @staticmethod
    def _wrapped(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        return label


class SystemCheckPage(Page):
    """Runs and displays the readiness check."""

    check_requested = Signal(bool)   # deep
    open_settings = Signal()

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "System check",
            "These checks run on this computer. Everything marked ✓ works offline; "
            "⚠ items are optional or worth improving; ✗ items must be fixed.",
            parent,
        )
        self.context = context
        self._items: list[CheckItemWidget] = []

        self._build_header()
        self._build_machine_card()
        self._build_results_card()

        # Show an existing report immediately (for example the startup check).
        if context.last_report is not None:
            self.show_report(context.last_report)

    # -- building ----------------------------------------------------------

    def _build_header(self) -> None:
        card = self.add_card()
        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)

        self.headline = QLabel("Not checked yet.")
        self.headline.setObjectName("SectionTitle")
        self.headline.setWordWrap(True)
        row.addWidget(self.headline, 1)

        self.recheck_button = QPushButton("Re-check system")
        mark_primary(self.recheck_button)
        self.recheck_button.setToolTip("Re-runs every check, including a short FFmpeg encode test.")
        self.recheck_button.clicked.connect(lambda: self.check_requested.emit(True))
        row.addWidget(self.recheck_button)

        self.settings_button = QPushButton("Open settings")
        self.settings_button.clicked.connect(self.open_settings.emit)
        row.addWidget(self.settings_button)
        card.body().addLayout(row)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Hint")
        card.add(self.progress_label)

    def _build_machine_card(self) -> None:
        card = self.add_card("This computer")
        self.machine_label = QLabel("")
        self.machine_label.setObjectName("Mono")
        self.machine_label.setWordWrap(True)
        self.machine_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.add(self.machine_label)

    def _build_results_card(self) -> None:
        card = self.add_card("Checks")
        self.results_layout = QVBoxLayout()
        self.results_layout.setSpacing(METRICS.xs)
        card.body().addLayout(self.results_layout)

        self.empty_label = HintLabel("Press 'Re-check system' to test this computer.")
        self.results_layout.addWidget(self.empty_label)

        card.add(separator())
        buttons = QHBoxLayout()
        buttons.setSpacing(METRICS.sm)
        copy_button = QPushButton("Copy report")
        copy_button.setToolTip("Copies the full report as text - useful when asking for help.")
        copy_button.clicked.connect(self._copy_report)
        buttons.addWidget(copy_button)

        log_button = QPushButton("Open log folder")
        log_button.clicked.connect(lambda: open_log_folder(self))
        buttons.addWidget(log_button)
        buttons.addStretch(1)
        card.body().addLayout(buttons)

    # -- data --------------------------------------------------------------

    def show_report(self, report) -> None:
        self.headline.setText(report.headline())
        self.progress_label.setText(f"Checked {len(report.results)} items in {report.duration_seconds:.1f}s.")

        if report.environment is not None:
            self.machine_label.setText("\n".join(report.environment.summary_lines()))
        else:
            self.machine_label.setText("\n".join(self.context.environment.summary_lines()))

        self._clear_results()
        self.empty_label.setVisible(False)

        theme = self.context.theme
        for result in report.results:
            widget = CheckItemWidget(result, theme)
            self.results_layout.addWidget(widget)
            self._items.append(widget)
            self.results_layout.addWidget(separator())

    def _clear_results(self) -> None:
        """Remove every result row, keeping the placeholder label alive.

        Widgets are detached and deleted so nothing is left parented to a layout
        that no longer exists.  The placeholder must be skipped: it is reused by
        every report, and deleting it here would remove it from the page and
        leave ``show_report`` holding a destroyed widget.
        """
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is None or widget is self.empty_label:
                continue
            widget.setParent(None)
            widget.deleteLater()
        self._items.clear()
        self.results_layout.addWidget(self.empty_label)

    def set_running(self, running: bool, message: str = "") -> None:
        self.recheck_button.setEnabled(not running)
        self.recheck_button.setText("Checking..." if running else "Re-check system")
        self.progress_label.setText(message)

    def _copy_report(self) -> None:
        report = self.context.last_report
        text = report.to_text() if report is not None else "No system check has been run yet."
        text = f"{self.context.diagnostics_text()}\n\n{text}"
        copy_to_clipboard(text)
        self.context.notify("Report copied to the clipboard.")
