"""Small shared widgets.

These exist so every page uses the same spacing, the same heading style and the
same row layout.  Consistency (directive section 53) is a property of the code
structure here, not something each page has to remember to do.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from ..theme import METRICS, status_glyph_color


class Card(QFrame):
    """A bordered surface that groups related controls."""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(METRICS.lg, METRICS.lg, METRICS.lg, METRICS.lg)
        self._layout.setSpacing(METRICS.sm)
        if title:
            heading = QLabel(title)
            heading.setObjectName("SectionTitle")
            self._layout.addWidget(heading)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget

    def add_row(self, widgets: Sequence[QWidget], spacing: int = METRICS.sm) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(spacing)
        for widget in widgets:
            row.addWidget(widget)
        self._layout.addLayout(row)
        return row


class Page(QWidget):
    """Base class for every page: consistent margins and a scrollable body."""

    def __init__(self, title: str, subtitle: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(METRICS.xl, METRICS.xl, METRICS.xl, METRICS.xl)
        self._outer.setSpacing(METRICS.md)

        self.header = QVBoxLayout()
        self.header.setSpacing(METRICS.xs)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.header.addWidget(self.title_label)
        if subtitle:
            self.subtitle_label = QLabel(subtitle)
            self.subtitle_label.setObjectName("Hint")
            self.subtitle_label.setWordWrap(True)
            self.header.addWidget(self.subtitle_label)
        else:
            self.subtitle_label = None  # type: ignore[assignment]
        self._outer.addLayout(self.header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._content = QWidget()
        self._content.setObjectName("Page")
        self._body = QVBoxLayout(self._content)
        self._body.setContentsMargins(0, 0, METRICS.sm, 0)
        self._body.setSpacing(METRICS.md)
        self._scroll.setWidget(self._content)
        self._outer.addWidget(self._scroll, 1)

    # -- layout access -----------------------------------------------------

    def body(self) -> QVBoxLayout:
        return self._body

    def add(self, widget: QWidget) -> QWidget:
        self._body.addWidget(widget)
        return widget

    def add_card(self, title: str = "") -> Card:
        card = Card(title)
        self._body.addWidget(card)
        return card

    def add_stretch(self) -> None:
        self._body.addStretch(1)

    def set_subtitle(self, text: str) -> None:
        if self.subtitle_label is not None:
            self.subtitle_label.setText(text)


class Row(QWidget):
    """A horizontal row of labelled controls with a fixed label column."""

    def __init__(self, label: str, widget: QWidget, hint: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(METRICS.xs)

        row = QHBoxLayout()
        row.setSpacing(METRICS.md)
        label_widget = QLabel(label)
        label_widget.setMinimumWidth(180)
        label_widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        row.addWidget(label_widget)
        row.addWidget(widget, 1)
        layout.addLayout(row)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("Hint")
            hint_label.setWordWrap(True)
            hint_label.setContentsMargins(180 + METRICS.md, 0, 0, 0)
            layout.addWidget(hint_label)


class HintLabel(QLabel):
    """Muted, wrapping explanatory text."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Hint")
        self.setWordWrap(True)


class StatusLine(QWidget):
    """A glyph + text line used by the system check list."""

    clicked = Signal()

    def __init__(
        self,
        status_value: str,
        glyph: str,
        title: str,
        summary: str,
        theme: str = "dark",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, METRICS.xs, 0, METRICS.xs)
        layout.setSpacing(METRICS.sm)

        self.glyph_label = QLabel(glyph)
        self.glyph_label.setFixedWidth(18)
        font = self.glyph_label.font()
        font.setBold(True)
        self.glyph_label.setFont(font)
        self.glyph_label.setStyleSheet(f"color: {status_glyph_color(theme, status_value)};")
        layout.addWidget(self.glyph_label, 0, Qt.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setWordWrap(True)
        self.summary_label = QLabel(summary)
        self.summary_label.setObjectName("Hint")
        self.summary_label.setWordWrap(True)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.summary_label)
        layout.addLayout(text_column, 1)

    def update_content(self, status_value: str, glyph: str, title: str, summary: str) -> None:
        self.glyph_label.setText(glyph)
        self.glyph_label.setStyleSheet(f"color: {status_glyph_color(self._theme, status_value)};")
        self.title_label.setText(title)
        self.summary_label.setText(summary)


class ButtonRow(QWidget):
    """A right-aligned row of buttons with a consistent size policy."""

    def __init__(self, buttons: Sequence[QPushButton] = (), parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(METRICS.sm)
        self._layout.addStretch(1)
        for button in buttons:
            self.add_button(button)

    def add_button(self, button: QPushButton) -> QPushButton:
        button.setMinimumWidth(METRICS.button_min_width)
        self._layout.addWidget(button)
        return button


class KeyValueGrid(QWidget):
    """Two-column label/value list used by the diagnostic pages."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(METRICS.md)
        self._grid.setVerticalSpacing(METRICS.xs)
        self._grid.setColumnStretch(1, 1)
        self._row = 0

    def add(self, label: str, value: str, monospace: bool = False) -> None:
        label_widget = QLabel(label)
        label_widget.setObjectName("Muted")
        value_widget = QLabel(value)
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if monospace:
            value_widget.setObjectName("Mono")
        self._grid.addWidget(label_widget, self._row, 0, Qt.AlignTop)
        self._grid.addWidget(value_widget, self._row, 1, Qt.AlignTop)
        self._row += 1

    def clear(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._row = 0


class ElidedLabel(QLabel):
    """A label that shortens long text with an ellipsis instead of expanding."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full_text = text
        super().setText(text)
        self.setToolTip(text)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.ElideMiddle, max(40, self.width()))
        super().setText(elided)
        super().resizeEvent(event)


def separator() -> QFrame:
    line = QFrame()
    line.setObjectName("Separator")
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    return line


def checkbox(text: str, checked: bool = False, hint: str = "") -> QCheckBox:
    box = QCheckBox(text)
    box.setChecked(checked)
    if hint:
        box.setToolTip(hint)
    return box


def spacer(height: int = METRICS.md) -> QSpacerItem:
    return QSpacerItem(1, height, QSizePolicy.Minimum, QSizePolicy.Fixed)
