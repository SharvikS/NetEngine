"""
Terminal page — wraps the embedded TerminalWidget with a retro-styled
header, status bar, and frame so it feels like a dedicated terminal zone
rather than just another generic panel in the application.
"""

from __future__ import annotations

import platform

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame, QComboBox,
    QSizePolicy,
)

from gui.components.terminal_widget import (
    TerminalWidget, available_shell_names, default_shell_name,
    shell_is_installed,
)
from gui.components.live_widgets import StatusDot
from gui.themes import theme, ThemeManager
from utils import settings


class TerminalView(QWidget):
    """Standalone page hosting an embedded local terminal."""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Heartbeat timer that pulses the status dot whenever output
        # appears. Created BEFORE _build_ui so the textChanged slot
        # connected inside _build_ui can fire safely — _build_ui
        # initialises the active shell, which may append a prompt and
        # therefore emit textChanged before __init__ returns.
        self._heart = QTimer(self)
        self._heart.setInterval(120)
        self._heart.setSingleShot(True)

        self._build_ui()
        # _heart's timeout target depends on widgets created in _build_ui
        self._heart.timeout.connect(lambda: self._dot.set_active(False))

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

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
        #
        # The header is a single QHBoxLayout row. Every control in the
        # row is added with an explicit AlignVCenter so items with
        # slightly different natural heights (QLabel text, StatusDot,
        # QComboBox, QPushButton) all sit on the same horizontal
        # centre line. Previously the layout relied on the default
        # alignment, which lined items up by top-edge and caused the
        # combo + Clear button to look "floating" when their QSS
        # min-height differed from their setFixedHeight. The
        # centre-line alignment is the stable fix.
        header = QWidget()
        header.setObjectName("retro_terminal_header")
        header.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 10, 16, 10)
        header_lay.setSpacing(10)

        VCENTER = Qt.AlignmentFlag.AlignVCenter

        # "traffic lights" decoration
        for color_attr in ("red", "amber", "green"):
            dot = QLabel("●")
            dot.setObjectName(f"tl_{color_attr}")
            dot.setFixedWidth(14)
            header_lay.addWidget(dot, 0, VCENTER)
            self._make_traffic_light(dot, color_attr)
        header_lay.addSpacing(10)

        self._title = QLabel("NET ENGINE :: TERMINAL")
        header_lay.addWidget(self._title, 0, VCENTER)

        header_lay.addSpacing(12)
        self._dot = StatusDot(size=8)
        header_lay.addWidget(self._dot, 0, VCENTER)

        self._subtitle = QLabel(self._backend_label())
        header_lay.addWidget(self._subtitle, 0, VCENTER)

        header_lay.addStretch(1)

        # ── Shell selector + Clear button (the control row) ─────────
        #
        # Both controls are forced to the same fixed height so they
        # share a pixel-perfect baseline. Any QSS min-height below is
        # overridden by setFixedHeight, guaranteeing a consistent row
        # regardless of theme or DPI.
        CONTROL_H = 30

        self._shell_label = QLabel("SHELL")
        self._shell_label.setObjectName("lbl_field_label")
        header_lay.addWidget(self._shell_label, 0, VCENTER)

        self._shell_combo = QComboBox()
        self._shell_combo.setObjectName("term_shell_combo")
        self._shell_combo.setFixedHeight(CONTROL_H)
        self._shell_combo.setMinimumWidth(140)
        self._shell_combo.setSizePolicy(QSizePolicy.Policy.Fixed,
                                        QSizePolicy.Policy.Fixed)
        self._shell_combo.setToolTip("Switch the embedded shell backend")
        # Track the combo explicitly so resizing the window never causes
        # Qt to decide the combo should shrink-shadow itself below the
        # Clear button baseline (which is what produced the "floating"
        # alignment look).
        for name in available_shell_names():
            label = name
            if not shell_is_installed(name):
                label = f"{name}  (unavailable)"
            self._shell_combo.addItem(label, userData=name)
        self._shell_combo.currentIndexChanged.connect(self._on_shell_changed)
        header_lay.addWidget(self._shell_combo, 0, VCENTER)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setObjectName("btn_action")
        self._btn_clear.setFixedHeight(CONTROL_H)
        self._btn_clear.setMinimumWidth(84)
        self._btn_clear.setSizePolicy(QSizePolicy.Policy.Fixed,
                                      QSizePolicy.Policy.Fixed)
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.clicked.connect(self._on_clear)
        header_lay.addWidget(self._btn_clear, 0, VCENTER)

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

        # Restore the user's previously chosen shell, if any. Done after
        # the footer is constructed so we can update the mode label too.
        saved_shell = settings.get("terminal_shell", "") or default_shell_name()
        self._select_shell(saved_shell)

    @staticmethod
    def _backend_label() -> str:
        return default_shell_name().lower()

    # ── Shell selector ──────────────────────────────────────────────────────

    def _select_shell(self, name: str) -> None:
        """Programmatically select a shell from the combo (without recursion)."""
        idx = -1
        for i in range(self._shell_combo.count()):
            if self._shell_combo.itemData(i) == name:
                idx = i
                break
        if idx < 0 and self._shell_combo.count() > 0:
            idx = 0
        if idx >= 0:
            self._shell_combo.blockSignals(True)
            self._shell_combo.setCurrentIndex(idx)
            self._shell_combo.blockSignals(False)
            target = self._shell_combo.itemData(idx)
            if target:
                self.terminal.set_shell(target)
                self._update_subtitle(target)
                self._lbl_mode.setText("LOCAL · " + target.upper())

    def _on_shell_changed(self, _idx: int):
        target = self._shell_combo.currentData()
        if not target:
            return
        ok = self.terminal.set_shell(target)
        if not ok:
            # Revert combo to whichever shell is actually active.
            current = self.terminal.shell_name()
            self._select_shell(current)
            return
        settings.set_value("terminal_shell", target)
        self._update_subtitle(target)
        self._lbl_mode.setText("LOCAL · " + target.upper())

    def _update_subtitle(self, name: str) -> None:
        self._subtitle.setText(name.lower())

    def _make_traffic_light(self, label: QLabel, color_attr: str):
        # Lazily restyled in _restyle
        label.setProperty("traffic_color", color_attr)

    def _on_clear(self):
        self.terminal.clear()
        self.terminal._show_local_prompt(banner=False)

    # ── Public API for cross-page actions ───────────────────────────────────

    def insert_pending_command(self, command: str) -> None:
        """Pre-fill the embedded terminal's current input with *command*.

        Used by the AI assistant's "Insert into Terminal" action.
        Deliberately does NOT submit the command — the user must
        press Enter themselves after reviewing it. If the terminal
        is busy or not in local mode the call is a no-op.
        """
        if not command:
            return
        term = self.terminal
        if getattr(term, "_mode", "local") != "local":
            return
        if getattr(term, "_busy", False):
            return
        try:
            term._replace_current_input(command)
        except Exception:
            # Defensive: we never want an AI-insert to crash the app.
            return
        term.setFocus()

    def _on_terminal_activity(self):
        self._dot.set_active(True, color=theme().term_glow)
        self._heart.start()

    def _restyle(self, t):
        # Frame: solid border on a deep background. The old build
        # installed a QGraphicsDropShadowEffect on this frame to give
        # it an ambient glow; that was the root cause of the shell
        # selector + Clear button "disappear until hovered" bug.
        #
        # QGraphicsDropShadowEffect routes the entire subtree through
        # an offscreen pixmap. When a descendant (like the QComboBox)
        # updates its display after a selection change, Qt does not
        # reliably invalidate the parent effect's cached pixmap on
        # Windows HiDPI, so the control renders stale / blank until
        # a hover event triggers a full re-rasterization of the
        # parent. Painting the terminal frame natively (no effect)
        # puts every control back on Qt's ordinary render path and
        # eliminates the stale-cache issue completely.
        self._frame.setStyleSheet(
            f"#retro_terminal_frame {{"
            f"  background-color: {t.term_bg};"
            f"  border: 2px solid {t.term_border};"
            f"  border-radius: 10px;"
            f"}}"
        )
        # Defensive: if an older build had attached a drop shadow,
        # strip it so upgrades don't inherit the bug.
        if self._frame.graphicsEffect() is not None:
            self._frame.setGraphicsEffect(None)

        # Header background + a local-scope rule for the two controls
        # in the row. The per-id rules are redundant with themes.py
        # at the application level, but repeating them here means
        # the combo + Clear button have a guaranteed paint path even
        # if a parent stylesheet is re-applied mid-session (e.g. on
        # theme switch, which is what made the combo go blank
        # intermittently before).
        accent = t.accent
        accent_dim = t.accent_dim
        accent_bg = t.accent_bg
        self._header.setStyleSheet(
            f"#retro_terminal_header {{"
            f"  background-color: {t.bg_deep};"
            f"  border-bottom: 1px solid {t.term_border};"
            f"  border-top-left-radius: 8px;"
            f"  border-top-right-radius: 8px;"
            f"}}"
            f"#retro_terminal_header QLabel {{ background: transparent; }}"
            # Shell combo — explicit, always-visible base state.
            f"QComboBox#term_shell_combo {{"
            f"  background-color: {t.bg_input};"
            f"  color: {t.text};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 6px;"
            f"  padding: 4px 30px 4px 12px;"
            f"  font-family: 'JetBrains Mono','Cascadia Mono','Consolas',monospace;"
            f"  font-size: 12px;"
            f"  font-weight: 700;"
            f"}}"
            f"QComboBox#term_shell_combo:hover {{"
            f"  border-color: {accent_dim};"
            f"  background-color: {t.bg_raised};"
            f"}}"
            f"QComboBox#term_shell_combo:focus {{"
            f"  border-color: {accent};"
            f"}}"
            f"QComboBox#term_shell_combo::drop-down {{"
            f"  subcontrol-origin: padding;"
            f"  subcontrol-position: center right;"
            f"  border: none; width: 22px;"
            f"}}"
            f"QComboBox#term_shell_combo::down-arrow {{"
            f"  image: none;"
            f"  border-left: 5px solid transparent;"
            f"  border-right: 5px solid transparent;"
            f"  border-top: 6px solid {accent};"
            f"  width: 0; height: 0; margin-right: 8px;"
            f"}}"
            # Clear button — explicit, always-visible base state.
            f"QPushButton#btn_action {{"
            f"  background-color: {t.bg_raised};"
            f"  color: {t.text};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 6px;"
            f"  padding: 4px 18px;"
            f"  font-family: 'JetBrains Mono','Cascadia Mono','Consolas',monospace;"
            f"  font-size: 11px;"
            f"  font-weight: 800;"
            f"}}"
            f"QPushButton#btn_action:hover {{"
            f"  color: {accent};"
            f"  border-color: {accent_dim};"
            f"  background-color: {accent_bg};"
            f"}}"
            f"QPushButton#btn_action:pressed {{"
            f"  color: {accent};"
            f"  border-color: {accent};"
            f"  background-color: {t.bg_hover};"
            f"}}"
            f"QPushButton#btn_action:focus {{"
            f"  border-color: {accent};"
            f"  outline: none;"
            f"}}"
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

        accent2 = t.accent2 or t.accent
        mono_family = (
            "'JetBrains Mono', 'Cascadia Mono', 'Cascadia Code',"
            " 'Fira Code', 'Consolas', monospace"
        )
        self._title.setStyleSheet(
            f"color: {t.term_glow};"
            f" font-family: {mono_family};"
            f" font-size: 13px; font-weight: 900; letter-spacing: 2.4px;"
            f" background: transparent;"
        )
        self._subtitle.setStyleSheet(
            f"color: {accent2}; font-size: 10px; letter-spacing: 1.6px;"
            f" font-weight: 800; font-family: {mono_family};"
            f" background: transparent;"
        )
        self._lbl_user.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px; letter-spacing: 0.6px;"
            f" font-family: {mono_family}; background: transparent;"
        )
        self._lbl_host.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px; letter-spacing: 0.6px;"
            f" font-family: {mono_family}; background: transparent;"
        )
        self._lbl_mode.setStyleSheet(
            f"color: {t.term_glow}; font-size: 11px; font-weight: 800;"
            f" letter-spacing: 1.6px; font-family: {mono_family};"
            f" background: transparent;"
        )

        import os
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        self._lbl_user.setText(f"USER  {user}")
        self._lbl_host.setText(f"HOST  {platform.node()}")

    # ── Lifecycle hook from MainWindow ──────────────────────────────────────

    def on_entered(self) -> None:
        """
        Called by MainWindow whenever the user navigates onto the
        terminal page. Re-shows the welcome banner if the embedded
        TerminalWidget thinks the moment is right (idle, no pending
        input, last banner old enough). All policy lives in
        `TerminalWidget.refresh_intro()` so this method is intentionally
        a one-line forwarder.
        """
        try:
            self.terminal.refresh_intro()
        except Exception:
            pass

    def shutdown(self):
        self.terminal.shutdown()
