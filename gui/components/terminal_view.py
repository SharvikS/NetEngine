"""
Terminal page — wraps the embedded TerminalWidget with a retro-styled
header, status bar, and frame so it feels like a dedicated terminal zone
rather than just another generic panel in the application.
"""

from __future__ import annotations

import platform

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame,
)

from gui.components.terminal_widget import TerminalWidget
from gui.components.live_widgets import StatusDot
from gui.themes import theme, ThemeManager


class TerminalView(QWidget):
    """Standalone page hosting an embedded local terminal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Heartbeat timer that pulses the status dot whenever output appears
        self._heart = QTimer(self)
        self._heart.setInterval(120)
        self._heart.setSingleShot(True)
        self._heart.timeout.connect(lambda: self._dot.set_active(False))

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 22)
        root.setSpacing(0)

        # ── Frame container ──
        self._frame = QFrame()
        self._frame.setObjectName("retro_terminal_frame")
        frame_lay = QVBoxLayout(self._frame)
        frame_lay.setContentsMargins(2, 2, 2, 2)
        frame_lay.setSpacing(0)

        # ── Header bar ──
        header = QWidget()
        header.setObjectName("retro_terminal_header")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 10, 16, 10)
        header_lay.setSpacing(12)

        # "traffic lights" decoration
        for color_attr in ("red", "amber", "green"):
            dot = QLabel("●")
            dot.setObjectName(f"tl_{color_attr}")
            dot.setFixedWidth(14)
            header_lay.addWidget(dot)
            self._make_traffic_light(dot, color_attr)
        header_lay.addSpacing(10)

        self._title = QLabel("NETSCOPE :: TERMINAL")
        header_lay.addWidget(self._title)

        header_lay.addSpacing(12)
        self._dot = StatusDot(size=8)
        header_lay.addWidget(self._dot)

        self._subtitle = QLabel(self._backend_label())
        header_lay.addWidget(self._subtitle)

        header_lay.addStretch()

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setObjectName("btn_action")
        self._btn_clear.setFixedHeight(28)
        self._btn_clear.clicked.connect(self._on_clear)
        header_lay.addWidget(self._btn_clear)

        self._header = header
        frame_lay.addWidget(header)

        # ── Terminal body ──
        self.terminal = TerminalWidget(self)
        self.terminal.textChanged.connect(self._on_terminal_activity)
        frame_lay.addWidget(self.terminal, stretch=1)

        # ── Footer bar ──
        footer = QWidget()
        footer.setObjectName("retro_terminal_footer")
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(16, 6, 16, 6)
        footer_lay.setSpacing(20)

        self._lbl_user = QLabel("")
        self._lbl_host = QLabel("")
        self._lbl_mode = QLabel("LOCAL")

        footer_lay.addWidget(self._lbl_user)
        footer_lay.addWidget(self._lbl_host)
        footer_lay.addStretch()
        footer_lay.addWidget(self._lbl_mode)

        self._footer = footer
        frame_lay.addWidget(footer)

        root.addWidget(self._frame, stretch=1)

    @staticmethod
    def _backend_label() -> str:
        return "powershell" if platform.system() == "Windows" else "bash"

    def _make_traffic_light(self, label: QLabel, color_attr: str):
        # Lazily restyled in _restyle
        label.setProperty("traffic_color", color_attr)

    def _on_clear(self):
        self.terminal.clear()
        self.terminal._show_local_prompt(banner=False)

    def _on_terminal_activity(self):
        self._dot.set_active(True, color=theme().term_glow)
        self._heart.start()

    def _restyle(self, t):
        # Frame: glow border with deep background
        self._frame.setStyleSheet(
            f"#retro_terminal_frame {{"
            f"  background-color: {t.term_bg};"
            f"  border: 2px solid {t.term_border};"
            f"  border-radius: 10px;"
            f"}}"
        )
        self._header.setStyleSheet(
            f"#retro_terminal_header {{"
            f"  background-color: {t.bg_deep};"
            f"  border-bottom: 1px solid {t.term_border};"
            f"  border-top-left-radius: 8px;"
            f"  border-top-right-radius: 8px;"
            f"}}"
            f"#retro_terminal_header QLabel {{ background: transparent; }}"
        )
        self._footer.setStyleSheet(
            f"#retro_terminal_footer {{"
            f"  background-color: {t.bg_deep};"
            f"  border-top: 1px solid {t.term_border};"
            f"  border-bottom-left-radius: 8px;"
            f"  border-bottom-right-radius: 8px;"
            f"}}"
            f"#retro_terminal_footer QLabel {{ background: transparent; }}"
        )

        # Traffic lights — repaint via stylesheet on direct labels
        traffic_colors = {"red": t.red, "amber": t.amber, "green": t.green}
        for child in self._header.findChildren(QLabel):
            attr = child.property("traffic_color")
            if attr in traffic_colors:
                child.setStyleSheet(
                    f"color: {traffic_colors[attr]}; font-size: 12px; background: transparent;"
                )

        self._title.setStyleSheet(
            f"color: {t.term_glow};"
            f" font-family: 'Consolas', 'Cascadia Mono', monospace;"
            f" font-size: 12px; font-weight: 800; letter-spacing: 1.2px;"
            f" background: transparent;"
        )
        self._subtitle.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px;"
            f" font-family: 'Consolas', monospace; background: transparent;"
        )
        self._lbl_user.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px;"
            f" font-family: 'Consolas', monospace; background: transparent;"
        )
        self._lbl_host.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px;"
            f" font-family: 'Consolas', monospace; background: transparent;"
        )
        self._lbl_mode.setStyleSheet(
            f"color: {t.term_glow}; font-size: 11px; font-weight: 700;"
            f" letter-spacing: 0.8px; background: transparent;"
        )

        import os
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        self._lbl_user.setText(f"USER  {user}")
        self._lbl_host.setText(f"HOST  {platform.node()}")

    def shutdown(self):
        self.terminal.shutdown()
