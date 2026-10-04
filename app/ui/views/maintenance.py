"""Maintenance page: cache sizes and safe cleanup (directive sections 31, 72).

Only the application's own regenerable folders can be cleared here.  Projects,
assets, themes, models and exported videos are never in the cleanup list, and
the confirmation dialog states exactly what will happen.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ...core.atomicio import human_size
from ...core.maintenance import CLEARABLE_TARGETS, TARGET_LABELS, storage_report
from ..notifications import ask_confirm, open_folder
from ..theme import METRICS, mark_primary
from ..widgets.common import HintLabel, KeyValueGrid, Page


class MaintenancePage(Page):
    """Cache inspection and cleanup."""

    cleanup_requested = Signal(list)   # target names

    def __init__(self, context, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Maintenance",
            "Cached files can be recreated by the application. Clearing them never touches your projects or exported videos.",
            parent,
        )
        self.context = context

        self._build_storage_card()
        self._build_actions_card()
        self.refresh()

    # -- cards -------------------------------------------------------------

    def _build_storage_card(self) -> None:
        card = self.add_card("Disk usage")
        self.storage_grid = KeyValueGrid()
        card.add(self.storage_grid)

        row = QHBoxLayout()
        row.setSpacing(METRICS.sm)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        open_data = QPushButton("Open data folder")
        open_data.clicked.connect(lambda: open_folder(self, self.context.paths.data_root))
        row.addWidget(open_data)
        row.addStretch(1)
        card.body().addLayout(row)

    def _build_actions_card(self) -> None:
        card = self.add_card("Clean up")
        card.add(HintLabel(
            "Cleaning is safe to run at any time. It does not delete projects, assets, or finished videos."
        ))

        rows = [
            ("preview_cache", "Clear preview cache", "Removes preview clips and storyboard thumbnails."),
            ("render_cache", "Clear render cache", "Removes cached frames and intermediate files."),
            ("temp_files", "Clear temporary files", "Removes working files left behind by cancelled or failed tasks."),
            ("workspace", "Clear working files", "Removes per-project scratch data that can be recreated."),
        ]
        for target, label, hint in rows:
            row = QHBoxLayout()
            row.setSpacing(METRICS.sm)
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, t=target: self._request_cleanup([t]))
            row.addWidget(button)
            description = QLabel(hint)
            description.setObjectName("Hint")
            description.setWordWrap(True)
            row.addWidget(description, 1)
            card.body().addLayout(row)

        card.body().addWidget(self._spacer())
        clear_all = QPushButton("Clear all non-project cache")
        mark_primary(clear_all)
        clear_all.clicked.connect(self._request_clean_all)
        card.body().addWidget(clear_all)

        self.last_result = HintLabel("")
        card.add(self.last_result)

    @staticmethod
    def _spacer() -> QWidget:
        widget = QWidget()
        widget.setFixedHeight(METRICS.sm)
        return widget

    # -- actions -----------------------------------------------------------

    def _request_cleanup(self, targets: list[str]) -> None:
        names = ", ".join(TARGET_LABELS.get(name, name) for name in targets)
        if not ask_confirm(
            self,
            f"Clear {names.lower()}?",
            "This removes cached files that the application can recreate. "
            "Your projects, assets and exported videos are not affected.",
            confirm_label="Clear",
        ):
            return
        self.last_result.setText("")
        self.cleanup_requested.emit(targets)

    def _request_clean_all(self) -> None:
        total = sum(
            int(storage_report(self.context.paths).get(name, {}).get("size_bytes", 0))
            for name in CLEARABLE_TARGETS
        )
        if not ask_confirm(
            self,
            "Clear all cached and temporary files?",
            f"This frees about {human_size(total)}. Projects, assets, themes, models and exported videos are kept. "
            "Previews will be rebuilt the next time you open them.",
            confirm_label="Clear everything",
            dangerous=True,
        ):
            return
        self.last_result.setText("")
        self.cleanup_requested.emit(list(CLEARABLE_TARGETS))

    # -- display -----------------------------------------------------------

    def refresh(self) -> None:
        report = storage_report(self.context.paths)
        self.storage_grid.clear()
        for name in CLEARABLE_TARGETS:
            entry = report.get(name)
            if not isinstance(entry, dict):
                continue
            self.storage_grid.add(
                TARGET_LABELS.get(name, name),
                f"{entry.get('size', '0 B')}   ({entry.get('path', '')})",
            )
        free = report.get("free_space", {})
        if isinstance(free, dict):
            self.storage_grid.add("Free disk space", str(free.get("text", "-")))
        self.storage_grid.add("Projects folder", str(self.context.paths.projects_dir))
        self.storage_grid.add("Output folder", str(self.context.paths.output_dir))

    def show_result(self, message: str) -> None:
        self.last_result.setText(message)
        self.refresh()
