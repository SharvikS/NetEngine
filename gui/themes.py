"""
NetScope theme system.

Single source of truth for every color used in the UI. A `ThemeManager`
holds the active `Theme` and emits a signal whenever the user switches
themes; widgets that hold inline colors listen and re-style themselves.

Built-in themes:
    Dark   — polished cyan-on-deep-blue (default)
    Neon   — magenta / cyan synthwave
    Space  — deep cosmic blue / amethyst
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication


# ── Theme dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Theme:
    name: str
    is_dark: bool

    bg_deep:    str
    bg_base:    str
    bg_raised:  str
    bg_hover:   str
    bg_select:  str
    bg_input:   str

    border:     str
    border_lt:  str

    accent:     str
    accent_dim: str
    accent_bg:  str

    green:      str
    green_dim:  str
    amber:      str
    red:        str
    red_dim:    str

    text:       str
    text_dim:   str
    text_mono:  str
    white:      str

    # Retro terminal palette (used by terminal widget)
    term_bg:    str
    term_fg:    str
    term_glow:  str
    term_border:str

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def status_colors(self) -> dict[str, str]:
        return {
            "alive":   self.green,
            "dead":    self.red,
            "unknown": self.text_dim,
        }

    def latency_color(self, ms: float) -> str:
        if ms < 0:
            return self.text_dim
        if ms <= 5:
            return self.green
        if ms <= 50:
            return self.amber
        return self.red


# ── Built-in themes ───────────────────────────────────────────────────────────

DARK = Theme(
    name="Dark",
    is_dark=True,
    bg_deep    = "#070b14",
    bg_base    = "#0c1322",
    bg_raised  = "#131c30",
    bg_hover   = "#1a2641",
    bg_select  = "#11305a",
    bg_input   = "#0a1428",
    border     = "#1f2d44",
    border_lt  = "#2c3e5c",
    accent     = "#00d4ff",
    accent_dim = "#0099bb",
    accent_bg  = "#082236",
    green      = "#00e57a",
    green_dim  = "#00994f",
    amber      = "#ffaa00",
    red        = "#ff4d63",
    red_dim    = "#992330",
    text       = "#dde7f4",
    text_dim   = "#7689a4",
    text_mono  = "#7ec8e3",
    white      = "#ffffff",
    term_bg    = "#04080f",
    term_fg    = "#7ee0ff",
    term_glow  = "#00d4ff",
    term_border= "#0a3a55",
)

NEON = Theme(
    name="Neon",
    is_dark=True,
    bg_deep    = "#08020f",
    bg_base    = "#11061d",
    bg_raised  = "#1a0a2c",
    bg_hover   = "#260f3c",
    bg_select  = "#3a1657",
    bg_input   = "#10051a",
    border     = "#2a1340",
    border_lt  = "#48206c",
    accent     = "#ff2bd6",
    accent_dim = "#b3168f",
    accent_bg  = "#2b0728",
    green      = "#39ff88",
    green_dim  = "#1ea65a",
    amber      = "#ffd400",
    red        = "#ff3a6e",
    red_dim    = "#8a1738",
    text       = "#ffe6fb",
    text_dim   = "#9b6cb6",
    text_mono  = "#7ef9ff",
    white      = "#ffffff",
    term_bg    = "#070110",
    term_fg    = "#ff7df1",
    term_glow  = "#ff2bd6",
    term_border= "#5b1670",
)

SPACE = Theme(
    name="Space",
    is_dark=True,
    bg_deep    = "#040616",
    bg_base    = "#0a0e23",
    bg_raised  = "#101632",
    bg_hover   = "#1a2247",
    bg_select  = "#23306a",
    bg_input   = "#080c1f",
    border     = "#1c2548",
    border_lt  = "#2d3a66",
    accent     = "#9d7bff",
    accent_dim = "#6c4cd6",
    accent_bg  = "#160f33",
    green      = "#5effc8",
    green_dim  = "#34a886",
    amber      = "#ffc857",
    red        = "#ff5d8a",
    red_dim    = "#8c2848",
    text       = "#e6e9ff",
    text_dim   = "#7a82b8",
    text_mono  = "#a9b9ff",
    white      = "#ffffff",
    term_bg    = "#04061a",
    term_fg    = "#c9b9ff",
    term_glow  = "#9d7bff",
    term_border= "#3a2d6c",
)

BUILT_IN_THEMES: list[Theme] = [DARK, NEON, SPACE]


# ── QSS builder ───────────────────────────────────────────────────────────────

def build_qss(t: Theme) -> str:
    """Compile the application stylesheet from a theme palette."""
    return f"""
/* ── Global ───────────────────────────────────────────────────── */
QWidget {{
    background-color: {t.bg_base};
    color: {t.text};
    font-family: 'Segoe UI', 'Inter', Arial, sans-serif;
    font-size: 13px;
    border: none;
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {t.bg_deep};
}}

QSplitter::handle {{
    background-color: {t.border};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* ── Toolbar / Header ─────────────────────────────────────────── */
#toolbar {{
    background-color: {t.bg_raised};
    border-bottom: 1px solid {t.border};
    padding: 6px 10px;
    min-height: 96px;
}}

#sidebar {{
    background-color: {t.bg_deep};
    border-right: 1px solid {t.border};
}}

#sidebar QPushButton {{
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    color: {t.text_dim};
    text-align: left;
    padding: 13px 22px 13px 19px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.7px;
    border-radius: 0;
    min-height: 42px;
}}
#sidebar QPushButton:hover {{
    background-color: {t.bg_raised};
    color: {t.text};
}}
#sidebar QPushButton:checked {{
    background-color: {t.bg_raised};
    color: {t.accent};
    border-left: 3px solid {t.accent};
}}

/* ── Buttons ──────────────────────────────────────────────────── */
QPushButton {{
    background-color: {t.bg_hover};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 30px;
}}

QPushButton:hover {{
    background-color: {t.bg_select};
    border-color: {t.accent_dim};
    color: {t.text};
}}

QPushButton:pressed {{
    background-color: {t.bg_select};
    border-color: {t.accent};
}}

QPushButton:disabled {{
    background-color: {t.bg_base};
    color: {t.text_dim};
    border-color: {t.border};
}}

QPushButton#btn_scan, QPushButton#btn_primary {{
    background-color: {t.accent_bg};
    color: {t.accent};
    border: 1px solid {t.accent_dim};
    font-weight: 700;
    padding: 8px 22px;
    letter-spacing: 0.6px;
}}
QPushButton#btn_scan:hover, QPushButton#btn_primary:hover {{
    background-color: {t.accent_bg};
    color: {t.accent};
    border-color: {t.accent};
}}

QPushButton#btn_stop, QPushButton#btn_danger {{
    background-color: {t.bg_raised};
    color: {t.red};
    border: 1px solid {t.red_dim};
    font-weight: 600;
}}
QPushButton#btn_stop:hover, QPushButton#btn_danger:hover {{
    border-color: {t.red};
    color: {t.red};
}}

QPushButton#btn_action {{
    background-color: transparent;
    color: {t.text_dim};
    border: 1px solid {t.border};
    border-radius: 4px;
    padding: 6px 14px;
    font-size: 12px;
    min-height: 28px;
}}
QPushButton#btn_action:hover {{
    color: {t.accent};
    border-color: {t.accent_dim};
    background-color: {t.accent_bg};
}}

/* ── ComboBox ─────────────────────────────────────────────────── */
QComboBox {{
    background-color: {t.bg_input};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 6px 32px 6px 12px;
    min-height: 30px;
}}
QComboBox:hover  {{ border-color: {t.accent_dim}; }}
QComboBox:focus  {{ border-color: {t.accent}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid {t.text_dim};
    width: 0; height: 0;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 4px;
    selection-background-color: {t.bg_select};
    selection-color: {t.accent};
    outline: none;
    padding: 4px;
}}

/* ── LineEdit / Search ────────────────────────────────────────── */
QLineEdit {{
    background-color: {t.bg_input};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 7px 12px;
    min-height: 30px;
    font-size: 13px;
    selection-background-color: {t.bg_select};
}}
QLineEdit:hover  {{ border-color: {t.accent_dim}; }}
QLineEdit:focus  {{ border-color: {t.accent}; }}
QLineEdit:disabled {{ color: {t.text_dim}; background-color: {t.bg_base}; }}

/* ── SpinBox ──────────────────────────────────────────────────── */
QSpinBox {{
    background-color: {t.bg_input};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 6px 8px;
    min-height: 30px;
}}
QSpinBox:focus {{ border-color: {t.accent}; }}
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 16px;
}}

/* ── Table ────────────────────────────────────────────────────── */
QTableView, QTableWidget {{
    background-color: {t.bg_base};
    alternate-background-color: {t.bg_raised};
    color: {t.text};
    border: none;
    gridline-color: {t.border};
    selection-background-color: {t.bg_select};
    selection-color: {t.text};
    font-size: 13px;
}}
QTableView::item {{
    padding: 6px 12px;
    border: none;
}}
QTableView::item:hover    {{ background-color: {t.bg_hover}; }}
QTableView::item:selected {{ background-color: {t.bg_select}; color: {t.text}; }}

QHeaderView::section {{
    background-color: {t.bg_raised};
    color: {t.text_dim};
    border: none;
    border-bottom: 2px solid {t.border_lt};
    border-right: 1px solid {t.border};
    padding: 9px 12px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}}
QHeaderView::section:hover   {{ background-color: {t.bg_hover}; color: {t.text}; }}
QHeaderView::section:pressed {{ background-color: {t.bg_select}; }}
QHeaderView {{ background-color: {t.bg_raised}; }}

QTableCornerButton::section {{
    background-color: {t.bg_raised};
    border: none;
    border-bottom: 2px solid {t.border_lt};
}}

/* ── List Widget ──────────────────────────────────────────────── */
QListWidget {{
    background-color: {t.bg_input};
    border: 1px solid {t.border};
    border-radius: 6px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 4px;
    color: {t.text};
}}
QListWidget::item:hover {{ background-color: {t.bg_hover}; }}
QListWidget::item:selected {{
    background-color: {t.bg_select};
    color: {t.accent};
}}

/* ── Progress Bar ─────────────────────────────────────────────── */
QProgressBar {{
    background-color: {t.bg_raised};
    border: 1px solid {t.border};
    border-radius: 5px;
    text-align: center;
    color: {t.text};
    min-height: 10px;
    max-height: 10px;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {t.accent};
    border-radius: 5px;
}}

QProgressBar#scp_progress {{
    min-height: 18px;
    max-height: 18px;
    font-size: 11px;
    font-weight: 600;
}}

/* ── Status Bar ───────────────────────────────────────────────── */
QStatusBar {{
    background-color: {t.bg_deep};
    color: {t.text_dim};
    border-top: 1px solid {t.border};
    font-size: 12px;
    padding: 4px 12px;
    min-height: 26px;
}}
QStatusBar::item {{ border: none; }}

/* ── Labels ───────────────────────────────────────────────────── */
QLabel {{
    background: transparent;
    color: {t.text};
}}
QLabel#lbl_title {{
    color: {t.accent};
    font-size: 18px;
    font-weight: 800;
    letter-spacing: 1.4px;
}}
QLabel#lbl_subtitle {{
    color: {t.text_dim};
    font-size: 11px;
    letter-spacing: 0.4px;
}}
QLabel#lbl_section {{
    color: {t.text_dim};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.7px;
    text-transform: uppercase;
}}
QLabel#lbl_field_label {{
    color: {t.text_dim};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
}}

/* ── Detail Drawer ────────────────────────────────────────────── */
#detail_drawer {{
    background-color: {t.bg_raised};
    border-left: 1px solid {t.border};
}}
#detail_drawer QLabel {{ font-size: 13px; }}
#detail_drawer_header {{
    background-color: {t.bg_deep};
}}
#detail_card {{
    background-color: {t.bg_base};
    border: 1px solid {t.border};
    border-radius: 8px;
}}

#stats_bar {{
    background-color: {t.bg_raised};
    border-bottom: 1px solid {t.border};
}}

/* ── ScrollBar ────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {t.bg_base};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.border_lt};
    border-radius: 6px;
    min-height: 28px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.text_dim}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QScrollBar:horizontal {{
    background: {t.bg_base};
    height: 12px;
}}
QScrollBar::handle:horizontal {{
    background: {t.border_lt};
    border-radius: 6px;
    min-width: 28px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t.text_dim}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ── CheckBox ─────────────────────────────────────────────────── */
QCheckBox {{ color: {t.text}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    background-color: {t.bg_input};
    border: 1px solid {t.border_lt};
    border-radius: 3px;
}}
QCheckBox::indicator:checked {{
    background-color: {t.accent_bg};
    border-color: {t.accent};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent_dim}; }}

/* ── RadioButton ──────────────────────────────────────────────── */
QRadioButton {{ color: {t.text}; spacing: 8px; padding: 4px 0; }}
QRadioButton::indicator {{
    width: 14px; height: 14px;
    background-color: {t.bg_input};
    border: 1px solid {t.border_lt};
    border-radius: 7px;
}}
QRadioButton::indicator:checked {{
    background-color: {t.accent};
    border-color: {t.accent};
}}

/* ── Tooltip ──────────────────────────────────────────────────── */
QToolTip {{
    background-color: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 4px;
    padding: 5px 10px;
    font-size: 12px;
}}

/* ── Tab Widget ───────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {t.border};
    border-radius: 6px;
    background-color: {t.bg_base};
    top: -1px;
}}
QTabBar {{
    qproperty-drawBase: 0;
    background: transparent;
}}
QTabBar::tab {{
    background-color: {t.bg_raised};
    color: {t.text_dim};
    border: 1px solid {t.border};
    border-bottom: none;
    padding: 9px 22px;
    margin-right: 3px;
    border-radius: 5px 5px 0 0;
    min-width: 110px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QTabBar::tab:selected {{
    background-color: {t.bg_base};
    color: {t.accent};
    border-bottom: 2px solid {t.accent};
}}
QTabBar::tab:hover:!selected {{
    background-color: {t.bg_hover};
    color: {t.text};
}}

/* ── Menu ─────────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {t.bg_deep};
    color: {t.text};
    border-bottom: 1px solid {t.border};
    padding: 3px 6px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 5px 12px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{ background-color: {t.bg_hover}; }}
QMenu {{
    background-color: {t.bg_raised};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 24px 7px 18px;
    border-radius: 4px;
    color: {t.text};
}}
QMenu::item:selected {{
    background-color: {t.bg_select};
    color: {t.accent};
}}
QMenu::separator {{
    height: 1px;
    background: {t.border};
    margin: 4px 8px;
}}

/* ── GroupBox ─────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {t.border};
    border-radius: 8px;
    margin-top: 14px;
    padding: 18px 14px 14px 14px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    color: {t.text_dim};
    background-color: {t.bg_base};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 8px;
    color: {t.accent};
    background-color: {t.bg_deep};
}}

/* ── Generic plain text ──────────────────────────────────────── */
QPlainTextEdit, QTextEdit {{
    background-color: {t.bg_input};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: 6px;
    selection-background-color: {t.bg_select};
    padding: 6px;
}}
"""


# ── Theme manager (singleton) ─────────────────────────────────────────────────

class ThemeManager(QObject):
    """Holds the active Theme and notifies listeners on change."""

    theme_changed = pyqtSignal(object)   # Theme

    _instance: "Optional[ThemeManager]" = None

    def __init__(self):
        super().__init__()
        self._themes: dict[str, Theme] = {t.name: t for t in BUILT_IN_THEMES}
        self._current: Theme = DARK
        self._app: Optional[QApplication] = None

    # singleton accessor ------------------------------------------------------

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # public API --------------------------------------------------------------

    def attach(self, app: QApplication):
        self._app = app
        self._apply()

    def theme_names(self) -> list[str]:
        return list(self._themes.keys())

    @property
    def current(self) -> Theme:
        return self._current

    def set_theme(self, name: str):
        if name not in self._themes or self._themes[name] is self._current:
            return
        self._current = self._themes[name]
        self._apply()
        self.theme_changed.emit(self._current)

    # internal ----------------------------------------------------------------

    def _apply(self):
        if self._app is not None:
            self._app.setStyleSheet(build_qss(self._current))


def theme() -> Theme:
    """Shortcut for the currently active theme palette."""
    return ThemeManager.instance().current
