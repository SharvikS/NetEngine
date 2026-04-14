"""
Single SSH session tab.

Each tab is a self-contained mini-workspace:

  * a header strip with status dot, friendly title, the
    `user@host:port` summary, an inline status label and per-session
    Reconnect / Disconnect buttons
  * an embedded `TerminalWidget` bound to a paramiko `SSHSession`
    that runs on a daemon worker thread

Tabs are independent — closing or reconnecting one never touches the
others, and each tab keeps its own command history (via the underlying
TerminalWidget) and connection log.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QSizePolicy,
)

from gui.components.terminal_widget import TerminalWidget
from gui.components.live_widgets import StatusDot
from gui.themes import theme, ThemeManager
from scanner.ssh_client import SSHProfile, SSHSession, HAS_PARAMIKO


# State machine for one tab — drives the header strip and the parent
# tab bar's color/title/icon. Kept as bare strings so the parent view
# doesn't need to import this enum.
STATE_IDLE        = "idle"
STATE_CONNECTING  = "connecting"
STATE_CONNECTED   = "connected"
STATE_FAILED      = "failed"
STATE_CLOSED      = "closed"


class SshSessionTab(QWidget):
    """A single SSH session shown as a tab in the SSH workspace."""

    # Outward signals — the parent SSH view listens to these to update
    # the QTabWidget tab title, color and any per-tab status badge.
    state_changed   = pyqtSignal(str)            # one of the STATE_* constants
    title_changed   = pyqtSignal(str)            # new tab title
    log_appended    = pyqtSignal(str)            # short status line for the parent log
    closed          = pyqtSignal()               # session is fully torn down

    # Internal cross-thread bridges
    _connect_failed_sig    = pyqtSignal(str)
    _connect_succeeded_sig = pyqtSignal(object)  # SSHSession instance

    def __init__(self, profile: SSHProfile, parent=None):
        super().__init__(parent)
        self.profile = profile
        self._state = STATE_IDLE
        self._session: Optional[SSHSession] = None
        self._worker: Optional[threading.Thread] = None
        self._opened_at: Optional[datetime] = None

        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Cross-thread bridges
        self._connect_failed_sig.connect(self._on_connect_failed)
        self._connect_succeeded_sig.connect(self._on_connect_succeeded)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header strip ─────────────────────────────────────────────────
        self._header = QFrame()
        self._header.setObjectName("session_tab_header")
        self._header.setFixedHeight(46)

        head_lay = QHBoxLayout(self._header)
        head_lay.setContentsMargins(16, 0, 14, 0)
        head_lay.setSpacing(10)

        self._dot = StatusDot(size=10)
        head_lay.addWidget(self._dot)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.setContentsMargins(0, 4, 0, 4)

        self._title_lbl = QLabel(self.title_text())
        self._title_lbl.setObjectName("session_tab_title")
        title_box.addWidget(self._title_lbl)

        self._sub_lbl = QLabel(self.summary_text())
        self._sub_lbl.setObjectName("session_tab_subtitle")
        title_box.addWidget(self._sub_lbl)

        head_lay.addLayout(title_box, stretch=1)

        self._state_lbl = QLabel("Idle")
        self._state_lbl.setObjectName("session_tab_state")
        head_lay.addWidget(self._state_lbl)

        self._btn_reconnect = QPushButton("Reconnect")
        self._btn_reconnect.setObjectName("btn_action")
        self._btn_reconnect.setMinimumHeight(30)
        self._btn_reconnect.setEnabled(False)
        self._btn_reconnect.clicked.connect(self.reconnect)
        head_lay.addWidget(self._btn_reconnect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setObjectName("btn_danger")
        self._btn_disconnect.setMinimumHeight(30)
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self.disconnect_session)
        head_lay.addWidget(self._btn_disconnect)

        root.addWidget(self._header)

        # Header divider
        self._head_div = QFrame()
        self._head_div.setFrameShape(QFrame.Shape.HLine)
        self._head_div.setFixedHeight(1)
        root.addWidget(self._head_div)

        # ── Terminal ─────────────────────────────────────────────────────
        # `ssh_only=True` means the terminal starts in idle mode with
        # no welcome banner, no local prompt, and no automatic fallback
        # to local shell on SSH detach. This tab owns the content; the
        # TerminalWidget just renders what we ask it to.
        self.terminal = TerminalWidget(self, ssh_only=True)
        self.terminal.session_closed.connect(self._on_terminal_session_closed)
        root.addWidget(self.terminal, stretch=1)

    # ── Public API ───────────────────────────────────────────────────────────

    def title_text(self) -> str:
        if self.profile.name:
            return self.profile.name
        if self.profile.user and self.profile.host:
            return f"{self.profile.user}@{self.profile.host}"
        return self.profile.host or "session"

    def summary_text(self) -> str:
        bits = []
        if self.profile.user:
            bits.append(self.profile.user + "@")
        bits.append(self.profile.host or "—")
        if self.profile.port and int(self.profile.port) != 22:
            bits.append(":" + str(self.profile.port))
        return "".join(bits)

    def state(self) -> str:
        return self._state

    def set_title(self, new_name: str) -> None:
        """Rename this session (used when the user double-clicks the tab)."""
        new_name = (new_name or "").strip()
        if not new_name:
            return
        self.profile.name = new_name
        self._title_lbl.setText(self.title_text())
        self.title_changed.emit(self.title_text())

    def start_connection(self) -> None:
        """
        Spin up the SSH connection on a worker thread.

        Pre-connect reset is *silent*: the previous session (if any) is
        torn down without writing a "[ssh session closed]" line, and
        the buffer is wiped so the new connection starts from a clean
        slate. The only output the user sees is the single
        "[connecting to …]" line and, on success, the "[connected …]"
        banner from `attach_ssh`.
        """
        if not HAS_PARAMIKO:
            self._set_state(STATE_FAILED, "paramiko not installed — pip install paramiko")
            self.terminal._append(
                "[paramiko is not installed — SSH unavailable. "
                "Run: pip install paramiko]\n"
            )
            return

        if self._worker is not None and self._worker.is_alive():
            # Already connecting; ignore double-click
            return

        # Silent pre-connect reset. detach_ssh(silent=True) never writes
        # to the buffer and never emits session_closed, so there is no
        # intermediate local-prompt flash and no stale CLOSED state
        # transition to race with the CONNECTING transition below.
        self.terminal.detach_ssh(silent=True)
        self.terminal.clear()
        self.terminal._append(
            f"[connecting to {self.profile.user or '?'}@"
            f"{self.profile.host}:{self.profile.port}…]\n"
        )

        self._set_state(STATE_CONNECTING, "Connecting…")
        self._btn_reconnect.setEnabled(False)
        self._btn_disconnect.setEnabled(True)

        self._session = None
        self._worker = threading.Thread(
            target=self._connect_worker,
            args=(self.profile,),
            daemon=True,
        )
        self._worker.start()

    def reconnect(self) -> None:
        """
        Silently tear down the current session and start a new one.
        No "[ssh session closed]" noise between the old and new
        connection — just the new "[connecting…]" / "[connected…]"
        pair.
        """
        self.disconnect_session(silent=True)
        self.start_connection()

    def disconnect_session(self, *, silent: bool = False) -> None:
        """
        Drop the current SSH session.

        Parameters
        ----------
        silent : bool, default False
            * ``False`` (default, user-initiated) — shows a single
              "[ssh session closed]" line in the terminal so the
              operator has a visible confirmation, and emits
              ``session_closed`` from the terminal which drives the
              tab state to ``STATE_CLOSED``.
            * ``True`` (reconnect / shutdown / pre-connect) — tears
              down the channel quietly with no output and no
              ``session_closed`` emission. The caller owns the next
              state transition.
        """
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self.terminal.detach_ssh(silent=silent)

    def shutdown(self) -> None:
        """
        Called by the parent view before destroying the tab. Always
        silent — the tab's buffer is about to be thrown away.
        """
        self.disconnect_session(silent=True)
        try:
            self.terminal.shutdown()
        except Exception:
            pass
        self.closed.emit()

    # ── Worker plumbing ──────────────────────────────────────────────────────

    def _connect_worker(self, profile: SSHProfile) -> None:
        session = SSHSession()
        try:
            session.start(profile, timeout=8.0)
        except Exception as exc:
            self._connect_failed_sig.emit(str(exc))
            return
        self._connect_succeeded_sig.emit(session)

    @pyqtSlot(str)
    def _on_connect_failed(self, message: str) -> None:
        self._set_state(STATE_FAILED, "Failed")
        self.terminal._append(f"\n[connection failed]\n  {message}\n")
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self.log_appended.emit(
            f"FAILED  {self.profile.user}@{self.profile.host}: {message}"
        )

    @pyqtSlot(object)
    def _on_connect_succeeded(self, session) -> None:
        self._session = session
        self._opened_at = datetime.now()
        self.terminal.attach_ssh(
            session,
            banner=(
                f"[connected to {self.profile.user}@"
                f"{self.profile.host}:{self.profile.port}]\n"
            ),
        )
        self._set_state(STATE_CONNECTED, "Connected")
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(True)
        self.log_appended.emit(
            f"CONNECTED  {self.profile.user}@{self.profile.host}"
        )

    @pyqtSlot()
    def _on_terminal_session_closed(self) -> None:
        # Triggered by the terminal when the SSH read loop ends or the
        # user manually disconnects.
        if self._state in (STATE_CONNECTING, STATE_FAILED):
            return
        self._session = None
        self._set_state(STATE_CLOSED, "Disconnected")
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self.log_appended.emit(
            f"CLOSED  {self.profile.user}@{self.profile.host}"
        )

    # ── State helper ─────────────────────────────────────────────────────────

    def _set_state(self, new_state: str, label: str) -> None:
        self._state = new_state
        self._state_lbl.setText(label)
        t = theme()
        color = {
            STATE_IDLE:       t.text_dim,
            STATE_CONNECTING: t.amber,
            STATE_CONNECTED:  t.green,
            STATE_FAILED:     t.red,
            STATE_CLOSED:     t.text_dim,
        }.get(new_state, t.text_dim)

        if new_state == STATE_CONNECTING:
            self._dot.set_active(True, color=color)
        elif new_state == STATE_CONNECTED:
            self._dot.set_active(True, color=color)
        else:
            self._dot.set_active(False)
            self._dot.set_color(color)

        self._state_lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 700;"
            f" letter-spacing: 0.6px; background: transparent;"
        )
        self.state_changed.emit(new_state)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._header.setStyleSheet(
            f"#session_tab_header {{"
            f"  background-color: {t.bg_raised};"
            f"  border: none;"
            f"}}"
            f"#session_tab_header QLabel {{ background: transparent; }}"
        )
        self._head_div.setStyleSheet(f"background-color: {t.border};")
        self._title_lbl.setStyleSheet(
            f"color: {t.accent}; font-size: 13px; font-weight: 700;"
            f" font-family: 'Consolas', monospace; background: transparent;"
        )
        self._sub_lbl.setStyleSheet(
            f"color: {t.text_dim}; font-size: 10px;"
            f" font-family: 'Consolas', monospace; background: transparent;"
        )
        # Re-apply the state color so the dot and label match the theme.
        self._set_state(self._state, self._state_lbl.text() or "Idle")
