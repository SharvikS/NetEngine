"""
Centralized clipboard helpers.

Qt's QClipboard access can fail or raise on locked / headless / remote
desktop sessions. Every copy path in the app funnels through the
helpers here so the failure modes stay consistent: a successful copy
returns ``True`` and posts the text on the system clipboard
immediately; a failed copy returns ``False`` without crashing the
caller.

Paste is symmetric — ``read_text`` returns the current clipboard text
or ``""`` if nothing is readable.

A small normalisation step strips the Unicode paragraph separator
(``\\u2029``) that Qt's ``QTextCursor.selectedText()`` emits in place
of ``\\n``. Without that step, multi-line terminal selections land on
the clipboard as a single glyph-free run.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtGui import QClipboard
from PyQt6.QtWidgets import QApplication


_PARAGRAPH_SEP = "\u2029"
_LINE_SEP = "\u2028"


def _normalise(text: str) -> str:
    """Convert Qt's selectedText() line markers into plain ``\\n``."""
    if not text:
        return ""
    # selectedText() uses U+2029 as a paragraph separator and
    # U+2028 as a line separator; flatten both to \n so consumers
    # (editors, terminals, shells) see normal text.
    return text.replace(_PARAGRAPH_SEP, "\n").replace(_LINE_SEP, "\n")


def copy_text(text: str) -> bool:
    """
    Place ``text`` on the system clipboard immediately.

    Returns ``True`` on success, ``False`` if the clipboard could not
    be reached (no QApplication instance, locked session, transient
    Windows clipboard owner conflict, etc.). Never raises.
    """
    if not isinstance(text, str):
        return False
    try:
        app = QApplication.instance()
        if app is None:
            return False
        clipboard = app.clipboard()
        if clipboard is None:
            return False
        clean = _normalise(text)
        # Writing to the Clipboard mode posts to the system clipboard
        # on Windows / macOS; on X11 we additionally publish to the
        # primary selection so middle-click paste works in the rest
        # of the desktop.
        clipboard.setText(clean, QClipboard.Mode.Clipboard)
        try:
            if clipboard.supportsSelection():
                clipboard.setText(clean, QClipboard.Mode.Selection)
        except Exception:
            pass
        return True
    except Exception:
        return False


def read_text() -> str:
    """
    Return the current system clipboard text, or ``""`` on failure.

    Never raises — callers that need to paste into a shell or line
    edit can treat this as "best-effort, empty on failure".
    """
    try:
        app = QApplication.instance()
        if app is None:
            return ""
        clipboard = app.clipboard()
        if clipboard is None:
            return ""
        text = clipboard.text(QClipboard.Mode.Clipboard)
        return text or ""
    except Exception:
        return ""


def copy_selected_text(widget) -> Optional[str]:
    """
    Copy the widget's current QTextCursor selection to the clipboard.

    Returns the copied text on success, ``None`` if nothing was
    selected or the copy failed. Used by the terminal widget's
    mouse-release auto-copy path and by the Ctrl+Shift+C handler.
    """
    try:
        cursor = widget.textCursor()
    except Exception:
        return None
    try:
        if not cursor.hasSelection():
            return None
        text = cursor.selectedText()
    except Exception:
        return None
    if not text:
        return None
    if copy_text(text):
        return _normalise(text)
    return None
