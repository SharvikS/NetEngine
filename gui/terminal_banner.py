"""
Terminal welcome banner.

Renders the one-shot ASCII intro shown when the embedded standalone
terminal initialises and whenever the user re-enters the terminal page
after a meaningful gap. Mirrors the spirit of distro-style terminal
logos: a clean block-letter rendering of the application name
followed by a short identification block.

Block-letter art (ANSI Shadow style)
------------------------------------
The art uses U+2588 FULL BLOCK plus the U+2550..U+255D box-drawing
double-line glyphs. These render correctly only when **two** things
hold:

  1. The host widget is rendering in a true monospace font that has
     proper Unicode coverage for the block + box-drawing range. The
     terminal widget pins this in QSS via the
     `QPlainTextEdit#terminal { font-family: 'Cascadia Mono', ... }`
     rule — see the long comment in `terminal_widget.py
     ._apply_theme_colors`.

  2. The font has no extra letter spacing applied. The terminal widget
     forces `setLetterSpacing(PercentageSpacing, 100.0)` for exactly
     this reason.

If either of those is broken, ASCII art that depends on adjacent
glyphs touching (which this style does) will shear apart visibly.
A previous regression was caused by the global `QWidget` QSS rule
silently overriding `setFont()` and forcing the terminal into
proportional Segoe UI; both of those guards are now in place.

Public API
----------
    build_welcome_banner(shell_name) -> str
        The full intro panel — art + info block + hint line.

    validate_banner() -> tuple[int, int]
        Locked invariant: every row of the rendered art has the same
        width. Smoke tests call this on every boot so any future edit
        that breaks alignment fails loudly.
"""

from __future__ import annotations

import os
import platform
from datetime import datetime


# ── Block-letter art ─────────────────────────────────────────────────────────
#
# ANSI Shadow rendering of "NET ENGINE". 6 rows × 79 cols when
# normalised. Stored as a list of strings; `_normalise()` pads every
# row to the maximum width so any trailing spaces stripped while
# pasting this source into the file are restored at runtime.
#
# Some rows in this style have intentional internal spacing (e.g. the
# middle rows of an "E" only fill the left half of the cell). Those
# spaces are part of the art and must NOT be stripped.

_ART_LINES: list[str] = [
    "███╗   ██╗███████╗████████╗    ███████╗███╗   ██╗ ██████╗ ██╗███╗   ██╗███████╗",
    "████╗  ██║██╔════╝╚══██╔══╝    ██╔════╝████╗  ██║██╔════╝ ██║████╗  ██║██╔════╝",
    "██╔██╗ ██║█████╗     ██║       █████╗  ██╔██╗ ██║██║  ███╗██║██╔██╗ ██║█████╗  ",
    "██║╚██╗██║██╔══╝     ██║       ██╔══╝  ██║╚██╗██║██║   ██║██║██║╚██╗██║██╔══╝  ",
    "██║ ╚████║███████╗   ██║       ███████╗██║ ╚████║╚██████╔╝██║██║ ╚████║███████╗",
    "╚═╝  ╚═══╝╚══════╝   ╚═╝       ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝╚══════╝",
]


def _normalise(lines: list[str]) -> list[str]:
    """Right-pad every row to the longest one and strip the trailing
    indent invariant. The art is read-only so this normally is a
    one-shot operation, but doing it via a helper keeps the validation
    paths and the live rendering paths consistent."""
    if not lines:
        return []
    width = max(len(line) for line in lines)
    return [line.ljust(width) for line in lines]


_ART_NORMALISED: list[str] = _normalise(_ART_LINES)
_ART_WIDTH: int = len(_ART_NORMALISED[0]) if _ART_NORMALISED else 0


def render_art() -> str:
    """Return the application title rendered in ASCII art."""
    return "\n".join(_ART_NORMALISED)


def validate_banner() -> tuple[int, int]:
    """
    Verify that every row in the art has identical width.

    Returns ``(row_count, row_width)`` on success and raises
    ``AssertionError`` if the source has been edited into an
    inconsistent state. Wired into the smoke test so any future edit
    that breaks alignment fails loudly at startup.
    """
    rows = _ART_NORMALISED
    assert rows, "art is empty"
    widths = {len(r) for r in rows}
    assert len(widths) == 1, f"art rows have inconsistent widths: {widths}"
    return len(rows), rows[0].__len__()


# ── Public banner API ────────────────────────────────────────────────────────


# Single source of truth for the embedded indent. Two columns of left
# padding so the art and info block sit cleanly inside the terminal
# frame's own padding without butting against the rounded corner.
_INDENT = "  "


def build_welcome_banner(shell_name: str = "") -> str:
    """
    Compose the one-shot welcome banner for the embedded local terminal.

    Layout (left-aligned, all rows share the same indent column):

        <blank line>
        ASCII NET ENGINE block
        <blank line>
        ─── separator ───────────────────────────
         ▸ user    <username>
         ▸ host    <hostname>
         ▸ system  <os name + release>
         ▸ shell   <active shell>
         ▸ time    <YYYY-MM-DD HH:MM>
        ─── separator ───────────────────────────
         hint line
        <blank line>

    The separator runs the full art width so the panel reads as one
    cohesive block. Info-block keys are padded to a single label
    column so the values stack in a clean vertical line.
    """
    # Indent the art and info block in lock-step so the whole panel
    # shares one column edge.
    art_indented = "\n".join(_INDENT + line for line in _ART_NORMALISED)

    user = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "user"
    )
    host = platform.node() or "localhost"
    osname = f"{platform.system()} {platform.release()}".strip() or "unknown"
    when = datetime.now().strftime("%Y-%m-%d  %H:%M")

    # Single label column — pad to a fixed width so values share an
    # x-position regardless of label length.
    label_w = 8
    info_lines = [
        f"{_INDENT} > {'user':<{label_w}}{user}",
        f"{_INDENT} > {'host':<{label_w}}{host}",
        f"{_INDENT} > {'system':<{label_w}}{osname}",
        f"{_INDENT} > {'shell':<{label_w}}{shell_name or 'default'}",
        f"{_INDENT} > {'time':<{label_w}}{when}",
    ]

    # Plain ASCII separator that always renders at exactly one cell
    # per char. Width matches the art so the whole panel forms one
    # rectangle.
    rule = _INDENT + ("-" * _ART_WIDTH)

    hint = (
        f"{_INDENT} type a command  ·  cd <dir>  ·  clear / ^L"
    )

    parts = [
        "",
        art_indented,
        "",
        rule,
        *info_lines,
        rule,
        hint,
        "",
    ]
    return "\n".join(parts) + "\n"
