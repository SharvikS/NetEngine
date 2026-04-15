"""
Net Engine theme system.

Single source of truth for every color used in the UI. A `ThemeManager`
holds the active `Theme` and emits a signal whenever the user switches
themes; widgets that hold inline colors listen and re-style themselves.

Built-in themes:
    Dark   — polished cyan-on-deep-blue (default)
    Neon   — magenta / cyan synthwave
    Space  — deep cosmic blue / amethyst
    Glass  — Apple-inspired frosted translucent layering
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

    # Secondary accent — used sparingly for typographic highlights,
    # subtitle/kicker words, and tertiary status indicators. Defaults
    # to the primary accent so a theme can omit it and stay backwards
    # compatible. Pair the two colours so they read as one identity:
    # cyan + magenta, magenta + cyan, purple + mint, blue + violet, …
    accent2:     str = ""
    accent2_dim: str = ""

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
    accent2    = "#ff5cd0",
    accent2_dim= "#a8338a",
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
    accent2    = "#7ef9ff",
    accent2_dim= "#3ec5d4",
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
    accent2    = "#5effc8",
    accent2_dim= "#2a8c6f",
)

# Glass — Apple-inspired translucent theme.
#
# Qt/QSS cannot do real backdrop blur on arbitrary widgets, so "glass"
# is simulated with layered semi-transparent fills over a deep gradient
# base. `bg_deep` is the solid backdrop painted behind everything; the
# other surfaces sit above it with rgba alpha channels to produce soft
# layered depth. Text colors stay fully opaque for readability.
GLASS = Theme(
    name="Glass",
    is_dark=True,
    bg_deep    = "#0a1220",                # solid gradient backdrop (see QSS)
    bg_base    = "rgba(18, 26, 44, 170)",  # main workspace — frosted
    bg_raised  = "rgba(30, 42, 66, 185)",  # cards, headers
    bg_hover   = "rgba(60, 84, 128, 160)", # hover wash
    bg_select  = "rgba(80, 140, 220, 130)",# selection highlight
    bg_input   = "rgba(12, 20, 36, 195)",  # inputs — slightly denser
    border     = "rgba(255, 255, 255, 32)",
    border_lt  = "rgba(255, 255, 255, 64)",
    accent     = "#7fd4ff",
    accent_dim = "#4a9fd1",
    accent_bg  = "rgba(80, 170, 240, 48)",
    green      = "#5fe7a6",
    green_dim  = "#2c8a5c",
    amber      = "#ffc857",
    red        = "#ff6b82",
    red_dim    = "#992236",
    text       = "#f2f6ff",
    text_dim   = "#aeb8cc",
    text_mono  = "#bfe6ff",
    white      = "#ffffff",
    term_bg    = "#0a1220",
    term_fg    = "#d9efff",
    term_glow  = "#7fd4ff",
    term_border= "rgba(127, 212, 255, 90)",
    accent2    = "#bd9bff",
    accent2_dim= "#7858bd",
)

BUILT_IN_THEMES: list[Theme] = [DARK, NEON, SPACE, GLASS]


# ── QSS builder ───────────────────────────────────────────────────────────────

def build_qss(t: Theme) -> str:
    """Compile the application stylesheet from a theme palette."""
    # Glass theme paints a soft diagonal gradient on the root window so
    # the rgba `bg_base` surfaces layer over it and produce a frosted
    # depth effect. Other themes stay on a flat `bg_deep`.
    if t.name == "Glass":
        root_bg = (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #0b1426, stop:0.5 #122037, stop:1 #0a1a2d)"
        )
        # Sidebar / menu / status share a darker frosted layer so they
        # clearly sit behind the workspace cards.
        sidebar_bg  = "rgba(10, 16, 30, 205)"
        menubar_bg  = "rgba(12, 20, 36, 210)"
        statusbar_bg = "rgba(10, 16, 30, 215)"
        toolbar_bg = (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(28, 40, 64, 200), stop:1 rgba(18, 28, 48, 200))"
        )
        raised_bg = (
            "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(34, 48, 76, 195), stop:1 rgba(24, 36, 58, 190))"
        )
    else:
        root_bg = t.bg_deep
        sidebar_bg  = t.bg_deep
        menubar_bg  = t.bg_deep
        statusbar_bg = t.bg_deep
        # Subtle vertical gradient on raised surfaces — gives the UI a
        # touch of depth without abandoning the flat technical look.
        toolbar_bg = (
            f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {t.bg_raised}, stop:1 {t.bg_base})"
        )
        raised_bg = (
            f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {t.bg_hover}, stop:1 {t.bg_raised})"
        )

    # Typography — single font stack used for every "engineered" piece
    # of UI text (nav, tabs, headers, status, terminal-adjacent labels)
    # so the whole app shares one technical voice. Body text stays on
    # Segoe UI for maximum readability.
    MONO = (
        "'JetBrains Mono', 'Cascadia Mono', 'Cascadia Code', "
        "'Fira Code', 'Consolas', 'Courier New', monospace"
    )
    SANS = "'Segoe UI', 'Inter', Arial, sans-serif"

    # Secondary accent — falls back to the primary accent so legacy
    # themes that haven't defined accent2 still render correctly.
    accent2 = t.accent2 or t.accent
    accent2_dim = t.accent2_dim or t.accent_dim

    return f"""
/* ── Global ───────────────────────────────────────────────────── */
QWidget {{
    background-color: {t.bg_base};
    color: {t.text};
    font-family: {SANS};
    font-size: 13px;
    border: none;
    outline: none;
}}

QMainWindow, QDialog {{
    background-color: {root_bg};
}}

QSplitter::handle {{
    background-color: {t.border};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical   {{ height: 1px; }}

/* ── Toolbar / Header ─────────────────────────────────────────── */
#toolbar {{
    background: {toolbar_bg};
    border-bottom: 1px solid {t.border};
    padding: 4px 8px;
}}

#sidebar {{
    background-color: {sidebar_bg};
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
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_hover}, stop:1 {t.bg_raised});
    color: {t.text};
    border: 1px solid {t.border_lt};
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 30px;
}}

QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_select}, stop:1 {t.bg_hover});
    border-color: {t.accent_dim};
    color: {t.text};
}}

QPushButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.accent_bg}, stop:1 {t.bg_select});
    border-color: {t.accent};
    padding-top: 8px;
    padding-bottom: 6px;
}}

QPushButton:disabled {{
    background: {t.bg_base};
    color: {t.text_dim};
    border-color: {t.border};
}}

QPushButton#btn_scan, QPushButton#btn_primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.accent_bg}, stop:1 {t.bg_raised});
    color: {t.accent};
    border: 1px solid {t.accent_dim};
    font-family: {MONO};
    font-size: 12px;
    font-weight: 800;
    padding: 8px 22px;
    letter-spacing: 1.6px;
    text-transform: uppercase;
}}
QPushButton#btn_scan:hover, QPushButton#btn_primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_select}, stop:1 {t.accent_bg});
    color: {t.accent};
    border-color: {t.accent};
}}
QPushButton#btn_scan:pressed, QPushButton#btn_primary:pressed {{
    background: {t.accent_bg};
    border-color: {t.accent};
    padding-top: 9px;
    padding-bottom: 7px;
}}

QPushButton#btn_stop, QPushButton#btn_danger {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
    color: {t.red};
    border: 1px solid {t.red_dim};
    font-family: {MONO};
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 1.4px;
    text-transform: uppercase;
}}
QPushButton#btn_stop:hover, QPushButton#btn_danger:hover {{
    border-color: {t.red};
    color: {t.red};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_hover}, stop:1 {t.bg_raised});
}}

QPushButton#btn_action {{
    font-family: {MONO};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
    text-transform: uppercase;
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
QLineEdit:hover  {{ border-color: {t.accent_dim}; background-color: {t.bg_raised}; }}
QLineEdit:focus  {{
    border: 1px solid {t.accent};
    background-color: {t.bg_raised};
}}
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
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_hover}, stop:1 {t.bg_raised});
    color: {t.text_dim};
    border: none;
    border-bottom: 2px solid {t.border_lt};
    border-right: 1px solid {t.border};
    padding: 9px 12px;
    font-family: {MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.6px;
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
    background-color: {t.bg_deep};
    border: 1px solid {t.border};
    border-radius: 5px;
    text-align: center;
    color: {t.text};
    min-height: 10px;
    max-height: 10px;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t.accent_dim}, stop:0.5 {t.accent}, stop:1 {t.accent_dim});
    border-radius: 5px;
    margin: 0;
}}

QProgressBar#scp_progress {{
    min-height: 18px;
    max-height: 18px;
    font-size: 11px;
    font-weight: 600;
}}

/* ── Status Bar ───────────────────────────────────────────────── */
QStatusBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_deep}, stop:1 {t.bg_base});
    color: {t.text_dim};
    border-top: 1px solid {t.border_lt};
    font-family: {MONO};
    font-size: 11px;
    letter-spacing: 0.4px;
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
    font-family: {MONO};
    font-size: 18px;
    font-weight: 900;
    letter-spacing: 2.2px;
    text-transform: uppercase;
}}
QLabel#lbl_subtitle {{
    color: {accent2};
    font-family: {MONO};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.8px;
    text-transform: uppercase;
}}
QLabel#lbl_section {{
    color: {accent2};
    font-family: {MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 2.0px;
    text-transform: uppercase;
}}
QLabel#lbl_field_label {{
    color: {t.text_dim};
    font-family: {MONO};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.6px;
    text-transform: uppercase;
}}
QLabel#lbl_kicker {{
    color: {accent2};
    font-family: {MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.8px;
    text-transform: uppercase;
}}

/* ── Detail Drawer ────────────────────────────────────────────── */
#detail_drawer {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
    border-left: 1px solid {t.border_lt};
}}
#detail_drawer QLabel {{ font-size: 13px; }}
#detail_drawer_header {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_deep}, stop:1 {t.bg_raised});
    border-bottom: 1px solid {t.border_lt};
}}
#detail_card {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
    border: 1px solid {t.border_lt};
    border-radius: 8px;
}}

#stats_bar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
    border-bottom: 1px solid {t.border_lt};
}}

/* ── ScrollBar ────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {t.bg_base};
    width: 12px;
    margin: 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t.border_lt}, stop:1 {t.border});
    border-radius: 6px;
    min-height: 32px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {t.accent_dim}, stop:1 {t.border_lt});
}}
QScrollBar::handle:vertical:pressed {{ background: {t.accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QScrollBar:horizontal {{
    background: {t.bg_base};
    height: 12px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.border_lt}, stop:1 {t.border});
    border-radius: 6px;
    min-width: 32px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.accent_dim}, stop:1 {t.border_lt});
}}
QScrollBar::handle:horizontal:pressed {{ background: {t.accent}; }}
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
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
    color: {t.text_dim};
    border: 1px solid {t.border};
    border-bottom: none;
    padding: 9px 22px;
    margin-right: 3px;
    border-radius: 5px 5px 0 0;
    min-width: 110px;
    font-family: {MONO};
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.4px;
    text-transform: uppercase;
}}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.accent_bg}, stop:1 {t.bg_base});
    color: {t.accent};
    border-color: {t.accent_dim};
    border-bottom: 2px solid {t.accent};
}}
QTabBar::tab:hover:!selected {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_hover}, stop:1 {t.bg_raised});
    color: {t.text};
    border-color: {t.border_lt};
}}

/* ── Menu ─────────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {menubar_bg};
    color: {t.text};
    border-bottom: 1px solid {t.border};
    padding: 3px 6px;
    font-family: {MONO};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.0px;
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
    border: 1px solid {t.border_lt};
    border-radius: 8px;
    margin-top: 14px;
    padding: 18px 14px 14px 14px;
    font-family: {MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.6px;
    text-transform: uppercase;
    color: {t.text_dim};
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {t.bg_raised}, stop:1 {t.bg_base});
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 10px;
    color: {accent2};
    background-color: {t.bg_deep};
    font-family: {MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.8px;
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
