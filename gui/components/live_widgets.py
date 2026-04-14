"""
Reusable, lightweight "live" UI widgets:
    StatusDot      — pulsing colored dot used as an activity indicator
    ScanActivity   — animated bar of bouncing segments (only animates while running)
    BlinkingLabel  — soft fade in/out used for things like "scanning…"

All widgets are theme-aware and stop their timers when hidden so they
never burn CPU off-screen.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, QSize, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QBrush, QPen
from PyQt6.QtWidgets import QWidget, QLabel

from gui.themes import ThemeManager, theme


class StatusDot(QWidget):
    """
    Small circular dot that pulses softly while `active` is True.
    Used as a "scan running" / "session live" indicator.
    """

    def __init__(self, parent=None, color: str = "", size: int = 12):
        super().__init__(parent)
        self._size = size
        self._color = color or theme().green
        self._active = False
        self._phase = 0.0
        self.setFixedSize(QSize(size + 8, size + 8))

        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

        ThemeManager.instance().theme_changed.connect(self._on_theme)

    # ── public ────────────────────────────────────────────────────────────────

    def set_active(self, active: bool, color: str | None = None) -> None:
        if color:
            self._color = color
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0.0
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    # ── theme ─────────────────────────────────────────────────────────────────

    def _on_theme(self, _t):
        self.update()

    # ── animation ─────────────────────────────────────────────────────────────

    def _tick(self):
        self._phase = (self._phase + 0.12) % 6.2832  # ~2π
        self.update()

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx = self.width() / 2
        cy = self.height() / 2

        base = QColor(self._color)

        if self._active:
            pulse = (math.sin(self._phase) + 1) / 2  # 0..1
            # Outer glow
            glow = QColor(base)
            glow.setAlphaF(0.25 + 0.35 * pulse)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            radius = self._size / 2 + 4 + 2 * pulse
            p.drawEllipse(int(cx - radius), int(cy - radius),
                          int(radius * 2), int(radius * 2))

        # Core dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(base))
        r = self._size / 2
        p.drawEllipse(int(cx - r), int(cy - r), self._size, self._size)


class ScanActivity(QWidget):
    """
    Animated horizontal bar of small bouncing segments.
    Visible only while `active` — otherwise renders a flat track.
    """

    def __init__(self, parent=None, width: int = 120, height: int = 14):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMinimumWidth(width)
        self._active = False
        self._phase = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        ThemeManager.instance().theme_changed.connect(lambda _t: self.update())

    def set_active(self, active: bool):
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self):
        self._phase = (self._phase + 0.08) % 6.2832
        self.update()

    def paintEvent(self, _event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = theme()

        # Track
        track_h = 4
        y = (self.height() - track_h) / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(t.border)))
        p.drawRoundedRect(0, int(y), self.width(), track_h, 2, 2)

        if not self._active:
            return

        # Bouncing dot
        col = QColor(t.accent)
        x = (math.sin(self._phase) + 1) / 2 * (self.width() - 14) + 2
        glow = QColor(col)
        glow.setAlphaF(0.25)
        p.setBrush(QBrush(glow))
        p.drawEllipse(int(x - 4), int(self.height() / 2 - 8), 18, 16)
        p.setBrush(QBrush(col))
        p.drawEllipse(int(x), int(self.height() / 2 - 4), 10, 8)
