"""
Net Engine theme system.

Single source of truth for every color used in the UI. A `ThemeManager`
holds the active `Theme` and emits a signal whenever the user switches
themes; widgets that hold inline colors listen and re-style themselves.

Built-in themes:
    Dark                  — polished cyan-on-deep-blue (default)
    Neon                  — magenta / cyan synthwave
    Space                 — deep cosmic blue / amethyst
    Liquid Glass          — Apple-inspired frosted translucent layering
    Light (WinSCP)        — clean grey/white panels, soft blue accent
    OG Black              — pure black, no glow, maximum readability
    Retro Terminal        — classic green-on-black CRT aesthetic
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

# Liquid Glass — Windows-Terminal-style transparent theme.
#
# All surfaces use solid colours — the glass effect comes from
# ``setWindowOpacity`` on the QMainWindow plus optional DWM Acrylic
# blur behind the window (Windows 10+).  This avoids the broken
# rgba-in-QSS approach where nested semi-opaque widgets stack to
# near-opaque and the transparency slider has no visible effect.
#
# The palette is a dark navy — similar to Dark but cooler/bluer — so
# the window still reads as a premium dark app even when transparency
# is turned off.
LIQUID_GLASS = Theme(
    name="Liquid Glass",
    is_dark=True,
    bg_deep    = "#0a1220",
    bg_base    = "#0f1a2c",
    bg_raised  = "#162438",
    bg_hover   = "#1e3050",
    bg_select  = "#1a3a5c",
    bg_input   = "#0c1628",
    border     = "#1e3050",
    border_lt  = "#2a4068",
    accent     = "#7fd4ff",
    accent_dim = "#4a9fd1",
    accent_bg  = "#0f2038",
    green      = "#5fe7a6",
    green_dim  = "#2c8a5c",
    amber      = "#ffc857",
    red        = "#ff6b82",
    red_dim    = "#992236",
    text       = "#f2f6ff",
    text_dim   = "#8899b0",
    text_mono  = "#bfe6ff",
    white      = "#ffffff",
    term_bg    = "#0a1220",
    term_fg    = "#d9efff",
    term_glow  = "#7fd4ff",
    term_border= "#1e3050",
    accent2    = "#bd9bff",
    accent2_dim= "#7858bd",
)

# Light (WinSCP-inspired) — clean grey/white panels, soft blue accent.
#
# Designed to feel like WinSCP's default UI: light grey workspace, pure
# white panels, subtle grey borders, and a single soft blue for focus,
# selection and primary action highlights. No neon glows. No gradients
# that try to mimic a "glossy" look — flat surfaces with thin borders
# are what carries the layout. The shared `build_qss()` emits gradients
# for buttons/tabs, but the two stops are picked so close together in
# light themes that the result reads as essentially flat.
#
# Text stays very dark grey (#1a1a1a) rather than full black so large
# blocks don't read as harsh; the secondary grey (#555) is the "dim"
# label colour. The terminal palette flips to light-on-light — a
# white surface with dark text and a blue caret — so the embedded
# terminal widget matches the surrounding chrome instead of punching a
# dark hole through it.
LIGHT = Theme(
    name="Light (WinSCP)",
    is_dark=False,
    bg_deep    = "#ebecee",   # window root / menu bar / status bar
    bg_base    = "#f5f6f7",   # page workspace background
    bg_raised  = "#ffffff",   # panels, cards, headers
    bg_hover   = "#eef2f9",   # hover wash (very pale blue-grey)
    bg_select  = "#d6e4fb",   # selection wash
    bg_input   = "#ffffff",   # form fields / text inputs
    border     = "#dcdcdc",
    border_lt  = "#c8c8c8",
    accent     = "#2d7ff9",
    accent_dim = "#1a5fc4",
    accent_bg  = "#e6f0ff",
    green      = "#1e8a3e",
    green_dim  = "#157031",
    amber      = "#c47a00",
    red        = "#d93025",
    red_dim    = "#a52a2a",
    text       = "#1a1a1a",
    text_dim   = "#555555",
    text_mono  = "#1a5fc4",
    white      = "#ffffff",
    term_bg    = "#ffffff",
    term_fg    = "#1a1a1a",
    term_glow  = "#2d7ff9",
    term_border= "#c8c8c8",
    accent2    = "#1a6fb8",
    accent2_dim= "#104a7e",
)

# ── OG Black accent presets ──────────────────────────────────────────────────
#
# OG Black is a theme *family*: the surfaces are always true-black, but
# the user picks an accent colour from a fixed palette. The five-tuple
# is (accent, accent_dim, accent_bg, accent2, accent2_dim).

OG_BLACK_ACCENTS: dict[str, tuple[str, str, str, str, str]] = {
    "Blue":   ("#4a9eff", "#2d6fc0", "#0d1a2e", "#6ab4ff", "#3a7acc"),
    "Cyan":   ("#22d3ee", "#0891b2", "#051e26", "#67e8f9", "#06b6d4"),
    "Orange": ("#f97316", "#c2410c", "#261004", "#fb923c", "#9a3412"),
    "Green":  ("#22c55e", "#15803d", "#052e16", "#4ade80", "#166534"),
    "Purple": ("#a855f7", "#7c3aed", "#1a0a30", "#c084fc", "#6d28d9"),
    "Red":    ("#ef4444", "#b91c1c", "#250606", "#f87171", "#991b1b"),
}


def _build_og_black(accent_name: str = "Blue") -> Theme:
    """Build an OG Black theme with the chosen accent colour."""
    a = OG_BLACK_ACCENTS.get(accent_name, OG_BLACK_ACCENTS["Blue"])
    accent, accent_dim, accent_bg, accent2, accent2_dim = a
    return Theme(
        name="OG Black",
        is_dark=True,
        bg_deep    = "#000000",
        bg_base    = "#0c0c0c",
        bg_raised  = "#1a1a1a",
        bg_hover   = "#262626",
        bg_select  = accent_bg,
        bg_input   = "#111111",
        border     = "#2e2e2e",
        border_lt  = "#404040",
        accent     = accent,
        accent_dim = accent_dim,
        accent_bg  = accent_bg,
        green      = "#4caf50",
        green_dim  = "#2e7d32",
        amber      = "#ff9800",
        red        = "#ef4444",
        red_dim    = "#b91c1c",
        text       = "#e8e8e8",
        text_dim   = "#808080",
        text_mono  = "#b0b8c0",
        white      = "#ffffff",
        term_bg    = "#000000",
        term_fg    = "#cccccc",
        term_glow  = accent,
        term_border= "#333333",
        accent2    = accent2,
        accent2_dim= accent2_dim,
    )


OG_BLACK = _build_og_black("Blue")

# Retro Terminal — classic green-on-black CRT aesthetic.
#
# Every UI surface is a shade of deep green-black, every text element
# is phosphor green, and the accent is the classic terminal green
# (#00ff41). The QSS builder overrides the body font to monospace so
# the entire application reads like a terminal session.
RETRO = Theme(
    name="Retro Terminal",
    is_dark=True,
    bg_deep    = "#000000",
    bg_base    = "#020a02",
    bg_raised  = "#071407",
    bg_hover   = "#0d220d",
    bg_select  = "#0a3a0a",
    bg_input   = "#010801",
    border     = "#0a3a0a",
    border_lt  = "#0f5a0f",
    accent     = "#00ff41",
    accent_dim = "#00aa2a",
    accent_bg  = "#021a02",
    green      = "#00ff41",
    green_dim  = "#00aa2a",
    amber      = "#cccc00",
    red        = "#ff2020",
    red_dim    = "#881010",
    text       = "#33ff77",
    text_dim   = "#1a8844",
    text_mono  = "#55ff88",
    white      = "#ffffff",
    term_bg    = "#000000",
    term_fg    = "#00ff41",
    term_glow  = "#00ff41",
    term_border= "#0a3a0a",
    accent2    = "#55ff88",
    accent2_dim= "#228844",
)

BUILT_IN_THEMES: list[Theme] = [
    DARK, NEON, SPACE, LIQUID_GLASS, LIGHT, OG_BLACK, RETRO,
]


# ── QSS builder ───────────────────────────────────────────────────────────────

def build_qss(t: Theme) -> str:
    """Compile the application stylesheet from a theme palette.

    All surfaces are fully opaque.  The Liquid Glass transparency
    effect is applied at the *window* level via ``setWindowOpacity``
    (+ optional DWM Acrylic on Windows), so QSS never touches alpha.
    """
    # ── Per-theme surfaces ───────────────────────────────────────────
    if t.name == "Liquid Glass":
        root_bg = (
            "qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 #0b1426, stop:0.5 #122037, stop:1 #0a1a2d)"
        )
    else:
        root_bg = t.bg_deep

    sidebar_bg   = t.bg_deep
    menubar_bg   = t.bg_deep
    statusbar_bg = t.bg_deep

    if t.name in ("OG Black", "Retro Terminal"):
        toolbar_bg = t.bg_raised
        raised_bg  = t.bg_raised
    else:
        toolbar_bg = (
            f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {t.bg_raised}, stop:1 {t.bg_base})"
        )
        raised_bg = (
            f"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {t.bg_hover}, stop:1 {t.bg_raised})"
        )

    # Typography — Retro Terminal uses monospace for EVERYTHING so the
    # whole application reads like a terminal session. Other themes keep
    # a proportional body font for readability.
    MONO = (
        "'JetBrains Mono', 'Cascadia Mono', 'Cascadia Code', "
        "'Fira Code', 'Consolas', 'Courier New', monospace"
    )
    if t.name == "Retro Terminal":
        SANS = MONO
    else:
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
/* Tab close button — minimal, theme-matching. */
QTabBar::close-button {{
    subcontrol-position: right;
    padding: 4px;
}}
QTabBar::close-button:hover {{
    background: {t.bg_hover};
    border-radius: 3px;
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


# ── Theme manager (singleton) ────────────────────────────────────────────────

class ThemeManager(QObject):
    """Holds the active Theme and notifies listeners on change.

    Window-level effects
    --------------------
    Liquid Glass uses ``setWindowOpacity`` on every QMainWindow so the
    desktop is visible through the app (like Windows Terminal's
    *opacity* knob). On Windows 10+ it also tries DWM Acrylic to blur
    the content behind the window. All QSS backgrounds stay fully
    opaque — only the window compositing layer is transparent.
    """

    theme_changed = pyqtSignal(object)   # Theme

    _instance: "Optional[ThemeManager]" = None

    def __init__(self):
        super().__init__()
        self._themes: dict[str, Theme] = {t.name: t for t in BUILT_IN_THEMES}
        self._current: Theme = DARK
        self._app: Optional[QApplication] = None
        self._glass_opacity: int = 88      # 60–100 → maps to setWindowOpacity
        self._og_accent: str = "Blue"

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
        # Window effects must run after the signal handlers finish —
        # some handlers re-polish widgets which can reset opacity.
        self._apply_window_effects()

    @property
    def glass_opacity(self) -> int:
        return self._glass_opacity

    def set_glass_opacity(self, value: int) -> None:
        value = max(60, min(100, value))
        if value == self._glass_opacity:
            return
        self._glass_opacity = value
        self._apply_window_effects()

    @property
    def og_accent(self) -> str:
        return self._og_accent

    def set_og_accent(self, name: str) -> None:
        if name not in OG_BLACK_ACCENTS or name == self._og_accent:
            return
        self._og_accent = name
        new = _build_og_black(name)
        self._themes["OG Black"] = new
        if self._current.name == "OG Black":
            self._current = new
            self._apply()
            self.theme_changed.emit(self._current)

    # internal ----------------------------------------------------------------

    def _apply(self):
        if self._app is not None:
            self._app.setStyleSheet(build_qss(self._current))
            # Qt's stylesheet polish pass resets windowOpacity.
            # A deferred re-apply after 150 ms lets the polish
            # (and all theme_changed signal handlers) finish first.
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(150, self._apply_window_effects)

    def _apply_window_effects(self) -> None:
        """Apply window-level opacity for Liquid Glass."""
        if self._app is None:
            return
        from PyQt6.QtWidgets import QMainWindow
        is_glass = self._current.name == "Liquid Glass"
        opacity = self._glass_opacity / 100.0 if is_glass else 1.0
        for w in self._app.topLevelWidgets():
            if not isinstance(w, QMainWindow):
                continue
            try:
                w.setWindowOpacity(opacity)
            except Exception:
                pass


def theme() -> Theme:
    """Shortcut for the currently active theme palette."""
    return ThemeManager.instance().current
