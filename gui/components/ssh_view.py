"""
SSH / SCP page.

Layout:
    ┌──────────── splitter ────────────────────────────────────┐
    │  LEFT: saved hosts + connection form                     │
    │  RIGHT: tabbed area:                                      │
    │     [SSH SESSION]   embedded retro terminal               │
    │     [FILE TRANSFER] SCP/SFTP panel                        │
    └──────────────────────────────────────────────────────────┘

The SSH and SCP flows are now visually separated, never overlap, and
each has its own breathing room.
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit, QSpinBox, QFileDialog, QMessageBox,
    QGroupBox, QFormLayout, QSplitter, QTabWidget, QFrame,
)

from gui.components.terminal_widget import TerminalWidget
from gui.components.scp_panel import SCPPanel
from gui.components.live_widgets import StatusDot
from gui.themes import theme, ThemeManager
from scanner.ssh_client import SSHProfile, SSHSession, HAS_PARAMIKO
from utils import settings


class SSHView(QWidget):
    """SSH connection manager + interactive terminal panel."""

    status_message = pyqtSignal(str)

    # Internal signals for thread-safe handoff from worker → UI
    _connect_failed_sig    = pyqtSignal(str)
    _connect_succeeded_sig = pyqtSignal(object, object)   # session, profile

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def __init__(self, parent=None):
        super().__init__(parent)
        self._session: Optional[SSHSession] = None
        self._build_ui()
        self._reload_hosts()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Cross-thread bridges
        self._connect_failed_sig.connect(self._apply_connect_failed)
        self._connect_succeeded_sig.connect(self._apply_connect_success)

        if not HAS_PARAMIKO:
            self._show_paramiko_warning()

    # ── UI build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 900])

        root.addWidget(splitter)

    # ----- Left panel -----------------------------------------------------------

    def _build_left_panel(self) -> QWidget:
        left = QWidget()
        left.setMinimumWidth(360)
        left.setMaximumWidth(440)
        lay = QVBoxLayout(left)
        lay.setContentsMargins(0, 0, 16, 0)
        lay.setSpacing(14)

        # Section title
        title = QLabel("CONNECTION MANAGER")
        title.setObjectName("lbl_section")
        lay.addWidget(title)

        # Saved hosts group
        hosts_box = QGroupBox("SAVED HOSTS")
        hosts_lay = QVBoxLayout(hosts_box)
        hosts_lay.setContentsMargins(14, 22, 14, 14)
        hosts_lay.setSpacing(10)

        self._host_list = QListWidget()
        self._host_list.setMinimumHeight(120)
        self._host_list.setMaximumHeight(170)
        self._host_list.itemSelectionChanged.connect(self._on_host_selected)
        hosts_lay.addWidget(self._host_list)

        host_btns = QHBoxLayout()
        host_btns.setSpacing(6)
        self._btn_new = QPushButton("New")
        self._btn_new.setObjectName("btn_action")
        self._btn_new.clicked.connect(self._on_new_host)
        self._btn_save = QPushButton("Save")
        self._btn_save.setObjectName("btn_action")
        self._btn_save.clicked.connect(self._on_save_host)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setObjectName("btn_danger")
        self._btn_delete.clicked.connect(self._on_delete_host)
        host_btns.addWidget(self._btn_new)
        host_btns.addWidget(self._btn_save)
        host_btns.addWidget(self._btn_delete)
        host_btns.addStretch()
        hosts_lay.addLayout(host_btns)

        lay.addWidget(hosts_box)

        # Connection details group
        form_box = QGroupBox("CONNECTION DETAILS")
        form = QFormLayout(form_box)
        form.setContentsMargins(14, 22, 14, 14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._in_name = QLineEdit()
        self._in_name.setPlaceholderText("Friendly name (optional)")
        self._in_host = QLineEdit()
        self._in_host.setPlaceholderText("hostname or IP")
        self._in_port = QSpinBox()
        self._in_port.setRange(1, 65535)
        self._in_port.setValue(22)
        self._in_port.setMinimumWidth(90)
        self._in_user = QLineEdit()
        self._in_user.setPlaceholderText("username")
        self._in_pass = QLineEdit()
        self._in_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._in_pass.setPlaceholderText("password (or use key)")

        key_row = QWidget()
        key_lay = QHBoxLayout(key_row)
        key_lay.setContentsMargins(0, 0, 0, 0)
        key_lay.setSpacing(6)
        self._in_key = QLineEdit()
        self._in_key.setPlaceholderText("path to private key (optional)")
        self._btn_browse = QPushButton("Browse")
        self._btn_browse.setObjectName("btn_action")
        self._btn_browse.clicked.connect(self._on_browse_key)
        key_lay.addWidget(self._in_key, stretch=1)
        key_lay.addWidget(self._btn_browse)

        form.addRow("Name:",   self._in_name)
        form.addRow("Host:",   self._in_host)
        form.addRow("Port:",   self._in_port)
        form.addRow("User:",   self._in_user)
        form.addRow("Pass:",   self._in_pass)
        form.addRow("Key:",    key_row)
        lay.addWidget(form_box)

        # Connect / disconnect buttons
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._btn_connect = QPushButton("CONNECT")
        self._btn_connect.setObjectName("btn_primary")
        self._btn_connect.setMinimumHeight(36)
        self._btn_connect.clicked.connect(self._on_connect)

        self._btn_disconnect = QPushButton("DISCONNECT")
        self._btn_disconnect.setObjectName("btn_danger")
        self._btn_disconnect.setMinimumHeight(36)
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._on_disconnect)

        action_row.addWidget(self._btn_connect, stretch=1)
        action_row.addWidget(self._btn_disconnect, stretch=1)
        lay.addLayout(action_row)

        # Status block
        status_wrap = QFrame()
        status_wrap.setObjectName("ssh_status_wrap")
        status_lay = QHBoxLayout(status_wrap)
        status_lay.setContentsMargins(14, 10, 14, 10)
        status_lay.setSpacing(10)
        self._dot = StatusDot(size=10)
        self._dot.set_color(theme().text_dim)
        status_lay.addWidget(self._dot)
        self._lbl_session_state = QLabel("Not connected")
        status_lay.addWidget(self._lbl_session_state)
        status_lay.addStretch()
        self._status_wrap = status_wrap
        lay.addWidget(status_wrap)

        lay.addStretch()
        return left

    # ----- Right panel ----------------------------------------------------------

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(16, 0, 0, 0)
        right_lay.setSpacing(0)

        # Tabs separate SSH session terminal from SCP file transfer
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(False)

        # SSH SESSION tab
        ssh_tab = QWidget()
        ssh_lay = QVBoxLayout(ssh_tab)
        ssh_lay.setContentsMargins(14, 16, 14, 14)
        ssh_lay.setSpacing(8)

        self.terminal = TerminalWidget(self)
        self.terminal.session_closed.connect(self._on_terminal_session_closed)
        ssh_lay.addWidget(self.terminal, stretch=1)

        self._tabs.addTab(ssh_tab, "SSH SESSION")

        # SCP TRANSFER tab
        scp_tab = QWidget()
        scp_lay = QVBoxLayout(scp_tab)
        scp_lay.setContentsMargins(14, 16, 14, 14)
        scp_lay.setSpacing(0)

        self._scp_panel = SCPPanel(self._collect_profile)
        scp_lay.addWidget(self._scp_panel)
        scp_lay.addStretch()

        self._tabs.addTab(scp_tab, "FILE TRANSFER")

        right_lay.addWidget(self._tabs, stretch=1)
        return right

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._lbl_session_state.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        self._status_wrap.setStyleSheet(
            f"#ssh_status_wrap {{"
            f"  background-color: {t.bg_input};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 6px;"
            f"}}"
        )

    def _show_paramiko_warning(self):
        self._btn_connect.setEnabled(False)
        self._btn_connect.setToolTip(
            "Install paramiko to enable SSH (pip install paramiko)"
        )
        self.terminal._append(
            "[paramiko is not installed — SSH/SCP unavailable. "
            "Run: pip install paramiko]\n"
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def prefill_host(self, ip: str) -> None:
        """Pre-fill the form with an IP from the scanner page."""
        self._in_host.setText(ip)
        self._in_name.setText(f"Scan-{ip}")
        self._in_user.setFocus()

    # ── Saved hosts ──────────────────────────────────────────────────────────

    def _reload_hosts(self):
        self._host_list.clear()
        for entry in settings.get_ssh_hosts():
            label = entry.get("name") or f"{entry.get('user','')}@{entry.get('host','')}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._host_list.addItem(item)

    def _on_host_selected(self):
        items = self._host_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        self._in_name.setText(entry.get("name", ""))
        self._in_host.setText(entry.get("host", ""))
        self._in_port.setValue(int(entry.get("port", 22) or 22))
        self._in_user.setText(entry.get("user", ""))
        self._in_pass.clear()
        self._in_key.setText(entry.get("key_path", ""))

    def _on_new_host(self):
        for w in (self._in_name, self._in_host, self._in_user,
                  self._in_pass, self._in_key):
            w.clear()
        self._in_port.setValue(22)
        self._host_list.clearSelection()
        self._in_name.setFocus()

    def _on_save_host(self):
        name = self._in_name.text().strip()
        host = self._in_host.text().strip()
        if not host:
            QMessageBox.warning(self, "SSH", "Host is required.")
            return
        if not name:
            name = f"{self._in_user.text().strip() or 'host'}@{host}"
            self._in_name.setText(name)
        entry = {
            "name": name,
            "host": host,
            "port": int(self._in_port.value()),
            "user": self._in_user.text().strip(),
            "key_path": self._in_key.text().strip(),
        }
        settings.save_ssh_host(entry)
        self._reload_hosts()
        self.status_message.emit(f"Saved SSH host '{name}'")

    def _on_delete_host(self):
        items = self._host_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        name = entry.get("name", "")
        if not name:
            return
        confirm = QMessageBox.question(
            self, "Delete host",
            f"Remove '{name}' from saved hosts?",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            settings.delete_ssh_host(name)
            self._reload_hosts()

    def _on_browse_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH private key", "",
            "All files (*)",
        )
        if path:
            self._in_key.setText(path)

    # ── Profile collection ───────────────────────────────────────────────────

    def _collect_profile(self) -> SSHProfile:
        return SSHProfile(
            name=self._in_name.text().strip(),
            host=self._in_host.text().strip(),
            port=int(self._in_port.value()),
            user=self._in_user.text().strip(),
            password=self._in_pass.text(),
            key_path=self._in_key.text().strip(),
        )

    # ── Connect / disconnect ─────────────────────────────────────────────────

    def _on_connect(self):
        if not HAS_PARAMIKO:
            QMessageBox.critical(
                self, "SSH unavailable",
                "paramiko is not installed.\n\nRun: pip install paramiko"
            )
            return

        profile = self._collect_profile()
        if not profile.host or not profile.user:
            QMessageBox.warning(self, "SSH", "Host and user are required.")
            return

        t = theme()
        self._lbl_session_state.setText(f"Connecting to {profile.host}…")
        self._dot.set_active(True, color=t.amber)
        self._btn_connect.setEnabled(False)
        self._tabs.setCurrentIndex(0)  # Show terminal tab while connecting

        # Run connect on a worker thread so the UI never freezes.
        threading.Thread(
            target=self._do_connect_worker, args=(profile,), daemon=True
        ).start()

    def _do_connect_worker(self, profile: SSHProfile):
        session = SSHSession()
        try:
            session.start(profile, timeout=8.0)
        except Exception as exc:
            self._connect_failed_sig.emit(str(exc))
            return
        self._connect_succeeded_sig.emit(session, profile)

    @pyqtSlot(str)
    def _apply_connect_failed(self, message: str):
        QMessageBox.critical(self, "SSH", f"Connection failed:\n{message}")
        self._lbl_session_state.setText("Not connected")
        self._dot.set_active(False)
        self._dot.set_color(theme().text_dim)
        self._btn_connect.setEnabled(True)

    @pyqtSlot(object, object)
    def _apply_connect_success(self, session: SSHSession, profile: SSHProfile):
        self._session = session
        self.terminal.attach_ssh(
            session,
            banner=f"\n[connected to {profile.user}@{profile.host}:{profile.port}]\n",
        )
        t = theme()
        self._lbl_session_state.setText(
            f"Connected · {profile.user}@{profile.host}:{profile.port}"
        )
        self._dot.set_active(True, color=t.green)
        self._btn_disconnect.setEnabled(True)
        self.status_message.emit(f"SSH connected to {profile.host}")

    def _on_disconnect(self):
        if self._session is not None:
            self._session.close()
        self.terminal.detach_ssh()

    @pyqtSlot()
    def _on_terminal_session_closed(self):
        self._lbl_session_state.setText("Not connected")
        self._dot.set_active(False)
        self._dot.set_color(theme().text_dim)
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._session = None

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def shutdown(self):
        self._on_disconnect()
        self.terminal.shutdown()
