"""
Reusable collapsible section widget.

A `CollapsibleSection` is a small framed group with a clickable header
that toggles a body widget visible/hidden. Used by the SSH workspace
to host the connection-details form so the user can collapse it after
they're connected and reclaim vertical space for the terminal area.

Designed to feel native to NetScope:
  * Theme-aware via ThemeManager.theme_changed
  * Header has its own background and a chevron toggle
  * Body content is provided through `set_content_layout(layout)`
  * `set_collapsed(bool)` and the public signal `toggled(bool)` let
    the parent react to the user opening/closing the section.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QToolButton,
    QSizePolicy, QLayout,
)

from gui.themes import theme, ThemeManager


class CollapsibleSection(QWidget):
    """
    Card-style group with a clickable header chevron that hides or
    reveals the body. The body is laid out by the caller via
    `set_content_layout`.
    """

    toggled = pyqtSignal(bool)   # True when collapsed, False when expanded

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("collapsible_section")
        self._collapsed = False
        self._title_text = title.upper()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────
        self._header = QFrame()
        self._header.setObjectName("collapsible_header")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFixedHeight(38)

        head_lay = QHBoxLayout(self._header)
        head_lay.setContentsMargins(14, 0, 10, 0)
        head_lay.setSpacing(10)

        self._chevron = QToolButton()
        self._chevron.setObjectName("collapsible_chevron")
        self._chevron.setArrowType(Qt.ArrowType.DownArrow)
        self._chevron.setAutoRaise(True)
        self._chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chevron.setFixedSize(20, 20)
        self._chevron.clicked.connect(self.toggle)
        head_lay.addWidget(self._chevron)

        self._title_lbl = QLabel(self._title_text)
        self._title_lbl.setObjectName("collapsible_title")
        head_lay.addWidget(self._title_lbl)

        head_lay.addStretch(1)

        self._hint = QLabel("")
        self._hint.setObjectName("collapsible_hint")
        head_lay.addWidget(self._hint)

        # Make the whole header act like a button for toggle.
        self._header.mousePressEvent = self._on_header_clicked

        outer.addWidget(self._header)

        # ── Body container ───────────────────────────────────────────────
        self._body = QFrame()
        self._body.setObjectName("collapsible_body")
        self._body_lay_holder = QVBoxLayout(self._body)
        self._body_lay_holder.setContentsMargins(0, 0, 0, 0)
        self._body_lay_holder.setSpacing(0)
        outer.addWidget(self._body)

        # Theme integration
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Public API ───────────────────────────────────────────────────────────

    def set_content_layout(self, layout: QLayout) -> None:
        """Install the section's body content."""
        # Replace any prior body layout — used when the parent rebuilds.
        old = self._body.layout()
        if old is not None:
            QWidget().setLayout(old)
        wrapper = QVBoxLayout()
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.setSpacing(0)
        wrapper.addLayout(layout)
        self._body.setLayout(wrapper)

    def set_hint(self, text: str) -> None:
        """Right-aligned hint text in the header (e.g. status string)."""
        self._hint.setText(text)

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._chevron.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self.toggled.emit(collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    # ── Event ────────────────────────────────────────────────────────────────

    def _on_header_clicked(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle()
        # call default to keep header behavior consistent
        QFrame.mousePressEvent(self._header, event)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self.setStyleSheet(
            f"#collapsible_section {{"
            f"  background-color: {t.bg_base};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 8px;"
            f"}}"
            f"#collapsible_header {{"
            f"  background-color: {t.bg_raised};"
            f"  border: none;"
            f"  border-top-left-radius: 8px;"
            f"  border-top-right-radius: 8px;"
            f"  border-bottom: 1px solid {t.border};"
            f"}}"
            f"#collapsible_header:hover {{"
            f"  background-color: {t.bg_hover};"
            f"}}"
            f"#collapsible_title {{"
            f"  color: {t.accent};"
            f"  font-size: 11px;"
            f"  font-weight: 800;"
            f"  letter-spacing: 0.8px;"
            f"  background: transparent;"
            f"}}"
            f"#collapsible_hint {{"
            f"  color: {t.text_dim};"
            f"  font-size: 11px;"
            f"  background: transparent;"
            f"}}"
            f"#collapsible_chevron {{"
            f"  background: transparent;"
            f"  border: none;"
            f"  color: {t.accent};"
            f"}}"
            f"#collapsible_body {{"
            f"  background-color: {t.bg_base};"
            f"  border: none;"
            f"  border-bottom-left-radius: 8px;"
            f"  border-bottom-right-radius: 8px;"
            f"}}"
        )
