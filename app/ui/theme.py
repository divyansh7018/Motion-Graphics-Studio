"""Visual theme and shared UI metrics (directive sections 52, 53).

One place defines every colour, spacing value and control style, so pages
cannot drift apart visually.  Two tokens exist for semantic status colours
(``ok`` / ``warn`` / ``bad``) which are the only colours a page may use for
meaning - never raw hex values in a view.

The theme is applied by :func:`apply_theme`, which sets both a ``QPalette``
(so native dialogs match) and a stylesheet (so widgets look consistent across
Windows versions).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

# --------------------------------------------------------------------------
# Spacing / sizing scale (4 px grid)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Metrics:
    """Shared spacing and sizing values."""

    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32
    radius: int = 6
    radius_small: int = 4
    control_height: int = 30
    button_min_width: int = 96
    sidebar_width: int = 200
    window_min_width: int = 1024
    window_min_height: int = 660
    font_size_body: int = 10
    font_size_small: int = 9
    font_size_title: int = 15
    font_size_heading: int = 12


METRICS = Metrics()


# --------------------------------------------------------------------------
# Colour schemes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ColorScheme:
    name: str
    background: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    ok: str
    warn: str
    bad: str
    info: str
    selection: str
    shadow: str


DARK = ColorScheme(
    name="dark",
    background="#191b1e",
    surface="#22252a",
    surface_alt="#2a2e34",
    border="#3a3f46",
    text="#e8eaed",
    text_muted="#9aa3ad",
    accent="#4c8dff",
    accent_hover="#639bff",
    accent_pressed="#3a7ae6",
    accent_text="#ffffff",
    ok="#4cc38a",
    warn="#e3b341",
    bad="#f2686b",
    info="#5a9bff",
    selection="#2d4b7a",
    shadow="rgba(0, 0, 0, 0.45)",
)

LIGHT = ColorScheme(
    name="light",
    background="#f4f5f7",
    surface="#ffffff",
    surface_alt="#eceef1",
    border="#d3d7de",
    text="#1b1e23",
    text_muted="#5b636e",
    accent="#2f6fdb",
    accent_hover="#3b7ce8",
    accent_pressed="#275fbe",
    accent_text="#ffffff",
    ok="#1a7f45",
    warn="#9a6b00",
    bad="#c0392b",
    info="#2f6fdb",
    selection="#cfe0ff",
    shadow="rgba(0, 0, 0, 0.18)",
)

SCHEMES: dict[str, ColorScheme] = {"dark": DARK, "light": LIGHT}


def scheme_for(theme: str) -> ColorScheme:
    """Resolve a theme name to a colour scheme (``system`` resolves too)."""
    from ..core.settings import detect_system_theme

    name = (theme or "dark").lower()
    if name == "system":
        name = detect_system_theme()
    return SCHEMES.get(name, DARK)


def status_color(theme: str, kind: str) -> str:
    """Colour for a semantic status: ok | warn | bad | info | muted."""
    scheme = scheme_for(theme)
    return {
        "ok": scheme.ok,
        "warn": scheme.warn,
        "bad": scheme.bad,
        "info": scheme.info,
        "muted": scheme.text_muted,
    }.get(kind, scheme.text)


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

def preferred_font_family() -> str:
    """Pick a font that exists on this machine (Segoe UI on Windows)."""
    available = set(QFontDatabase.families())
    for candidate in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Arial"):
        if candidate in available:
            return candidate
    return QFont().defaultFamily()


def apply_font(app: QApplication) -> None:
    family = preferred_font_family()
    font = QFont(family)
    font.setPointSize(METRICS.font_size_body)
    font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

def build_palette(scheme: ColorScheme) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(scheme.background))
    palette.setColor(QPalette.WindowText, QColor(scheme.text))
    palette.setColor(QPalette.Base, QColor(scheme.surface))
    palette.setColor(QPalette.AlternateBase, QColor(scheme.surface_alt))
    palette.setColor(QPalette.ToolTipBase, QColor(scheme.surface_alt))
    palette.setColor(QPalette.ToolTipText, QColor(scheme.text))
    palette.setColor(QPalette.Text, QColor(scheme.text))
    palette.setColor(QPalette.Button, QColor(scheme.surface))
    palette.setColor(QPalette.ButtonText, QColor(scheme.text))
    palette.setColor(QPalette.BrightText, QColor(scheme.bad))
    palette.setColor(QPalette.Link, QColor(scheme.accent))
    palette.setColor(QPalette.Highlight, QColor(scheme.selection))
    palette.setColor(QPalette.HighlightedText, QColor(scheme.text))
    palette.setColor(QPalette.PlaceholderText, QColor(scheme.text_muted))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(scheme.text_muted))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(scheme.text_muted))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(scheme.text_muted))
    return palette


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------

def build_stylesheet(scheme: ColorScheme) -> str:
    m = METRICS
    return f"""
/* ---------- base ---------- */
QWidget {{
    color: {scheme.text};
    background: transparent;
}}
QMainWindow, QDialog {{
    background: {scheme.background};
}}
QWidget#Page {{
    background: {scheme.background};
}}
QWidget#Card {{
    background: {scheme.surface};
    border: 1px solid {scheme.border};
    border-radius: {m.radius}px;
}}
QLabel#PageTitle {{
    font-size: {m.font_size_title}pt;
    font-weight: 600;
}}
QLabel#SectionTitle {{
    font-size: {m.font_size_heading}pt;
    font-weight: 600;
}}
QLabel#Hint, QLabel#Muted {{
    color: {scheme.text_muted};
}}
QLabel#Hint {{
    font-size: {m.font_size_small}pt;
}}
QLabel#StatusOk {{ color: {scheme.ok}; }}
QLabel#StatusWarn {{ color: {scheme.warn}; }}
QLabel#StatusBad {{ color: {scheme.bad}; }}
QLabel#Mono {{
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: {m.font_size_small}pt;
}}

/* ---------- sidebar ---------- */
QWidget#Sidebar {{
    background: {scheme.surface};
    border-right: 1px solid {scheme.border};
}}
QLabel#Brand {{
    font-size: {m.font_size_heading}pt;
    font-weight: 700;
    padding: {m.md}px;
    color: {scheme.text};
}}
QListWidget#NavList {{
    background: transparent;
    border: none;
    outline: none;
    padding: {m.xs}px;
}}
QListWidget#NavList::item {{
    padding: {m.sm}px {m.md}px;
    border-radius: {m.radius_small}px;
    color: {scheme.text};
}}
QListWidget#NavList::item:hover {{
    background: {scheme.surface_alt};
}}
QListWidget#NavList::item:selected {{
    background: {scheme.accent};
    color: {scheme.accent_text};
}}
QListWidget#NavList::item:disabled {{
    color: {scheme.text_muted};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {scheme.surface_alt};
    color: {scheme.text};
    border: 1px solid {scheme.border};
    border-radius: {m.radius_small}px;
    padding: {m.xs}px {m.md}px;
    min-height: {m.control_height - 8}px;
}}
QPushButton:hover {{ background: {scheme.border}; }}
QPushButton:pressed {{ background: {scheme.surface}; }}
QPushButton:disabled {{
    color: {scheme.text_muted};
    background: {scheme.surface};
    border-color: {scheme.surface_alt};
}}
QPushButton[primary="true"] {{
    background: {scheme.accent};
    color: {scheme.accent_text};
    border: 1px solid {scheme.accent};
    font-weight: 600;
}}
QPushButton[primary="true"]:hover {{ background: {scheme.accent_hover}; }}
QPushButton[primary="true"]:pressed {{ background: {scheme.accent_pressed}; }}
QPushButton[primary="true"]:disabled {{
    background: {scheme.surface_alt};
    color: {scheme.text_muted};
    border-color: {scheme.border};
}}
QPushButton[danger="true"] {{
    color: {scheme.bad};
    border-color: {scheme.bad};
}}
QPushButton[danger="true"]:hover {{
    background: {scheme.bad};
    color: {scheme.accent_text};
}}
QPushButton[flat="true"] {{
    background: transparent;
    border: none;
    color: {scheme.accent};
    padding: {m.xs}px;
}}
QPushButton[flat="true"]:hover {{ text-decoration: underline; }}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {scheme.surface};
    border: 1px solid {scheme.border};
    border-radius: {m.radius_small}px;
    padding: {m.xs}px {m.sm}px;
    min-height: {m.control_height - 10}px;
    selection-background-color: {scheme.selection};
    selection-color: {scheme.text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {scheme.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {scheme.text_muted};
    background: {scheme.surface_alt};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {scheme.surface};
    border: 1px solid {scheme.border};
    selection-background-color: {scheme.selection};
    selection-color: {scheme.text};
}}
QPlainTextEdit#LogView {{
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: {m.font_size_small}pt;
}}

/* ---------- containers ---------- */
QGroupBox {{
    border: 1px solid {scheme.border};
    border-radius: {m.radius}px;
    margin-top: {m.md}px;
    padding: {m.md}px {m.sm}px {m.sm}px {m.sm}px;
    background: {scheme.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {m.md}px;
    padding: 0 {m.xs}px;
    color: {scheme.text_muted};
    font-weight: 600;
}}
QTabWidget::pane {{
    border: 1px solid {scheme.border};
    border-radius: {m.radius}px;
    background: {scheme.surface};
    top: -1px;
}}
QTabBar::tab {{
    background: {scheme.surface_alt};
    color: {scheme.text_muted};
    border: 1px solid {scheme.border};
    border-bottom: none;
    border-top-left-radius: {m.radius_small}px;
    border-top-right-radius: {m.radius_small}px;
    padding: {m.xs}px {m.md}px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {scheme.surface};
    color: {scheme.text};
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: {scheme.border}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}

/* ---------- item views ---------- */
QListWidget, QTreeWidget, QTableWidget, QListView, QTreeView, QTableView {{
    background: {scheme.surface};
    border: 1px solid {scheme.border};
    border-radius: {m.radius}px;
    outline: none;
    alternate-background-color: {scheme.surface_alt};
}}
QListWidget::item, QTreeWidget::item, QTableWidget::item {{
    padding: {m.xs}px;
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{
    background: {scheme.selection};
    color: {scheme.text};
}}
QHeaderView::section {{
    background: {scheme.surface_alt};
    color: {scheme.text_muted};
    border: none;
    border-right: 1px solid {scheme.border};
    border-bottom: 1px solid {scheme.border};
    padding: {m.xs}px {m.sm}px;
    font-weight: 600;
}}

/* ---------- progress / status ---------- */
QProgressBar {{
    background: {scheme.surface_alt};
    border: 1px solid {scheme.border};
    border-radius: {m.radius_small}px;
    text-align: center;
    min-height: 16px;
    color: {scheme.text};
}}
QProgressBar::chunk {{
    background: {scheme.accent};
    border-radius: {m.radius_small - 1}px;
}}
QStatusBar {{
    background: {scheme.surface};
    border-top: 1px solid {scheme.border};
    color: {scheme.text_muted};
}}
QStatusBar::item {{ border: none; }}
QToolBar {{
    background: {scheme.surface};
    border-bottom: 1px solid {scheme.border};
    spacing: {m.xs}px;
    padding: {m.xs}px;
}}
QMenuBar {{ background: {scheme.surface}; border-bottom: 1px solid {scheme.border}; }}
QMenuBar::item {{ padding: {m.xs}px {m.md}px; background: transparent; }}
QMenuBar::item:selected {{ background: {scheme.surface_alt}; }}
QMenu {{
    background: {scheme.surface};
    border: 1px solid {scheme.border};
    padding: {m.xs}px;
}}
QMenu::item {{ padding: {m.xs}px {m.lg}px; border-radius: {m.radius_small}px; }}
QMenu::item:selected {{ background: {scheme.accent}; color: {scheme.accent_text}; }}
QMenu::separator {{ height: 1px; background: {scheme.border}; margin: {m.xs}px {m.sm}px; }}
QToolTip {{
    background: {scheme.surface_alt};
    color: {scheme.text};
    border: 1px solid {scheme.border};
    padding: {m.xs}px {m.sm}px;
}}

/* ---------- misc ---------- */
QCheckBox, QRadioButton {{ spacing: {m.sm}px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QSlider::groove:horizontal {{
    height: 4px;
    background: {scheme.surface_alt};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {scheme.accent};
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QPushButton#Banner {{ text-align: left; padding: {m.sm}px {m.md}px; }}
QFrame#Separator {{ background: {scheme.border}; max-height: 1px; }}
QScrollBar:vertical {{
    background: transparent; width: 12px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {scheme.border}; border-radius: 6px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {scheme.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {scheme.border}; border-radius: 6px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {scheme.text_muted}; }}
"""


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def apply_theme(app: QApplication, theme: str) -> ColorScheme:
    """Apply *theme* ("dark" | "light" | "system") to the whole application."""
    scheme = scheme_for(theme)
    apply_font(app)
    app.setStyle("Fusion")  # consistent baseline across Windows versions
    app.setPalette(build_palette(scheme))
    app.setStyleSheet(build_stylesheet(scheme))
    return scheme


def set_button_role(button, role: str) -> None:
    """Tag a button so the stylesheet styles it (primary / danger / flat)."""
    button.setProperty(role, True)
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    button.update()


def mark_primary(button) -> None:
    set_button_role(button, "primary")


def mark_danger(button) -> None:
    set_button_role(button, "danger")


def mark_flat(button) -> None:
    set_button_role(button, "flat")


def clear_button_role(button, role: str) -> None:
    button.setProperty(role, False)
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)


def status_glyph_color(theme: str, status_value: str) -> str:
    """Colour used for a system-check status glyph."""
    return {
        "ready": status_color(theme, "ok"),
        "warning": status_color(theme, "warn"),
        "optional": status_color(theme, "warn"),
        "missing": status_color(theme, "bad"),
        "blocked": status_color(theme, "bad"),
    }.get(status_value, status_color(theme, "muted"))


__all__ = [
    "DARK",
    "LIGHT",
    "METRICS",
    "SCHEMES",
    "ColorScheme",
    "Metrics",
    "apply_font",
    "apply_theme",
    "build_palette",
    "build_stylesheet",
    "clear_button_role",
    "mark_danger",
    "mark_flat",
    "mark_primary",
    "preferred_font_family",
    "scheme_for",
    "set_button_role",
    "status_color",
    "status_glyph_color",
]
