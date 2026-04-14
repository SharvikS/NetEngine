"""
Vertical navigation sidebar with one button per top-level page.

The theme picker has moved to the View → Theme menu and the Settings
dialog. The sidebar now hosts a live host count and an activity dot
that pulses while a scan is running.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QButtonGroup, QLabel,
    QFrame,
)

from gui.themes import ThemeManager, theme
from gui.components.live_widgets import StatusDot


class Sidebar(QWidget):
    """
    Sidebar with checkable navigation buttons.
    Emits `page_changed(int)` when the active page changes.
    """

    page_changed = pyqtSignal(int)

    def __init__(self, items: list[str], parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(196)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 18, 0, 16)
        root.setSpacing(0)

        # Brand
        self._brand = QLabel("NETSCOPE")
        self._brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._brand.setObjectName("lbl_title")
        root.addWidget(self._brand)

        sub = QLabel("v1.1 · Network Toolkit")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setObjectName("lbl_subtitle")
        root.addWidget(sub)
        root.addSpacing(22)

        # Section label
        section = QLabel("WORKSPACE")
        section.setObjectName("lbl_section")
        section.setContentsMargins(20, 0, 20, 6)
        root.addWidget(section)

        # Buttons
        self._buttons: list[QPushButton] = []
        for i, name in enumerate(items):
            btn = QPushButton(name.upper())
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked, idx=i: self._on_clicked(idx))
            self._group.addButton(btn, i)
            root.addWidget(btn)
            self._buttons.append(btn)
        if self._buttons:
            self._buttons[0].setChecked(True)

        root.addStretch()

        # Divider above status block
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {theme().border};")
        self._div = div
        root.addWidget(div)

        # Live status block
        status_wrap = QWidget()
        status_lay = QVBoxLayout(status_wrap)
        status_lay.setContentsMargins(20, 14, 20, 6)
        status_lay.setSpacing(8)

        self._lbl_status_label = QLabel("STATUS")
        self._lbl_status_label.setObjectName("lbl_section")
        status_lay.addWidget(self._lbl_status_label)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._dot = StatusDot(size=10)
        self._dot.set_active(False)
        self._dot.set_color(theme().text_dim)
        status_row.addWidget(self._dot)

        self._lbl_state = QLabel("Idle")
        self._lbl_state.setStyleSheet(
            f"color: {theme().text}; font-size: 12px; font-weight: 600;"
        )
        status_row.addWidget(self._lbl_state)
        status_row.addStretch()
        status_lay.addLayout(status_row)

        self._lbl_hosts = QLabel("0 hosts alive")
        self._lbl_hosts.setStyleSheet(
            f"color: {theme().text_dim}; font-size: 11px;"
        )
        status_lay.addWidget(self._lbl_hosts)

        root.addWidget(status_wrap)

        ThemeManager.instance().theme_changed.connect(self._restyle)

    # ── Public API ──────────────────────────────────────────────────────────

    def set_current(self, idx: int) -> None:
        if 0 <= idx < len(self._buttons):
            self._buttons[idx].setChecked(True)

    def set_scan_active(self, active: bool) -> None:
        t = theme()
        if active:
            self._dot.set_active(True, color=t.accent)
            self._lbl_state.setText("Scanning…")
            self._lbl_state.setStyleSheet(
                f"color: {t.accent}; font-size: 12px; font-weight: 700;"
            )
        else:
            self._dot.set_active(False)
            self._dot.set_color(t.text_dim)
            self._lbl_state.setText("Idle")
            self._lbl_state.setStyleSheet(
                f"color: {t.text}; font-size: 12px; font-weight: 600;"
            )

    def set_host_summary(self, alive: int, total: int) -> None:
        if total <= 0:
            self._lbl_hosts.setText("No scans yet")
        else:
            self._lbl_hosts.setText(f"{alive} alive · {total} scanned")

    # ── Internal ────────────────────────────────────────────────────────────

    def _on_clicked(self, idx: int) -> None:
        self.page_changed.emit(idx)

    def _restyle(self, t):
        self._div.setStyleSheet(f"background-color: {t.border};")
        self._lbl_hosts.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
        # Re-apply state with new palette
        if self._lbl_state.text() == "Scanning…":
            self.set_scan_active(True)
        else:
            self.set_scan_active(False)
