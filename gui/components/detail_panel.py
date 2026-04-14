"""
Detail panel — shown at the bottom when a host row is selected.
Displays full host info and action buttons.
"""

from __future__ import annotations

import webbrowser

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QApplication, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from scanner.host_scanner import HostInfo
from scanner.service_mapper import describe_ports
from gui.themes import theme, ThemeManager


class _Field(QWidget):
    """A label-value pair field."""

    def __init__(self, label: str, mono: bool = False, parent=None):
        super().__init__(parent)
        self._mono = mono
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self._lbl = QLabel(label.upper())
        self._lbl.setObjectName("lbl_field_label")

        self._val = QLabel("—")
        self._val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._val.setMinimumHeight(20)
        self._val.setWordWrap(False)
        self._val.setTextFormat(Qt.TextFormat.PlainText)
        if mono:
            f = QFont("Consolas", 11)
            f.setFixedPitch(True)
            self._val.setFont(f)

        layout.addWidget(self._lbl)
        layout.addWidget(self._val)

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _restyle(self, t):
        if self._mono:
            self._val.setStyleSheet(f"color: {t.text_mono}; font-size: 12px;")
        else:
            self._val.setStyleSheet(f"color: {t.text}; font-size: 13px;")

    def set_value(self, value: str, color: str = ""):
        self._val.setText(value)
        if color:
            size = "12px" if self._mono else "13px"
            self._val.setStyleSheet(f"color: {color}; font-size: {size}; font-weight: 600;")
        else:
            self._restyle(theme())


class DetailPanel(QWidget):
    """
    Bottom panel showing details for the currently selected host.
    """

    rescan_requested = pyqtSignal(str)
    ssh_requested    = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("detail_panel")
        self.setMinimumHeight(200)
        self.setMaximumHeight(260)
        self._current_host: HostInfo | None = None
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        self.show_empty()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(28)

        # ── Left: identity ────────────────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(6)
        left.setContentsMargins(0, 0, 0, 0)

        self._lbl_kicker = QLabel("HOST DETAILS")
        self._lbl_kicker.setObjectName("lbl_field_label")
        left.addWidget(self._lbl_kicker)

        self._hdr = QLabel("Select a host")
        self._hdr.setMinimumHeight(28)
        left.addWidget(self._hdr)

        self._sub = QLabel("")
        self._sub.setMinimumHeight(18)
        left.addWidget(self._sub)
        left.addStretch()

        root.addLayout(left, stretch=2)

        # Vertical separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFixedWidth(1)
        sep1.setStyleSheet(f"background-color: {theme().border};")
        self._sep1 = sep1
        root.addWidget(sep1)

        # ── Center: fields grid ───────────────────────────────────────────────
        center = QWidget()
        grid = QGridLayout(center)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(36)
        grid.setVerticalSpacing(12)

        self._f_ip       = _Field("IP Address", mono=True)
        self._f_mac      = _Field("MAC Address", mono=True)
        self._f_hostname = _Field("Hostname")

        self._f_status   = _Field("Status")
        self._f_latency  = _Field("Latency", mono=True)
        self._f_vendor   = _Field("Vendor")

        self._f_ttl      = _Field("TTL / OS")
        self._f_ports    = _Field("Open Ports")
        self._f_scanned  = _Field("Last Scanned")

        # 3 columns × 3 rows
        grid.addWidget(self._f_ip,        0, 0)
        grid.addWidget(self._f_status,    0, 1)
        grid.addWidget(self._f_ttl,       0, 2)

        grid.addWidget(self._f_mac,       1, 0)
        grid.addWidget(self._f_latency,   1, 1)
        grid.addWidget(self._f_ports,     1, 2)

        grid.addWidget(self._f_hostname,  2, 0)
        grid.addWidget(self._f_vendor,    2, 1)
        grid.addWidget(self._f_scanned,   2, 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)

        root.addWidget(center, stretch=6)

        # Vertical separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFixedWidth(1)
        sep2.setStyleSheet(f"background-color: {theme().border};")
        self._sep2 = sep2
        root.addWidget(sep2)

        # ── Right: actions ────────────────────────────────────────────────────
        actions_wrap = QWidget()
        actions_wrap.setFixedWidth(180)
        actions = QVBoxLayout(actions_wrap)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(7)

        self._act_label = QLabel("ACTIONS")
        self._act_label.setObjectName("lbl_field_label")
        actions.addWidget(self._act_label)

        self._btn_rescan   = self._make_btn("Rescan")
        self._btn_portscan = self._make_btn("Port Scan")
        self._btn_ssh      = self._make_btn("Open SSH")
        self._btn_copy_ip  = self._make_btn("Copy IP")
        self._btn_open_web = self._make_btn("Open in Browser")

        self._btn_rescan.clicked.connect(self._do_rescan)
        self._btn_portscan.clicked.connect(self._do_port_scan)
        self._btn_ssh.clicked.connect(self._do_ssh)
        self._btn_copy_ip.clicked.connect(self._do_copy_ip)
        self._btn_open_web.clicked.connect(self._do_open_web)

        for btn in [self._btn_rescan, self._btn_portscan, self._btn_ssh,
                    self._btn_copy_ip, self._btn_open_web]:
            actions.addWidget(btn)

        actions.addStretch()
        root.addWidget(actions_wrap)

    def _make_btn(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("btn_action")
        btn.setMinimumHeight(28)
        btn.setEnabled(False)
        return btn

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 18px; font-weight: 800;"
            f" font-family: 'Consolas', monospace;"
        )
        self._sub.setStyleSheet(f"color: {t.text_dim}; font-size: 12px;")
        if hasattr(self, "_sep1"):
            self._sep1.setStyleSheet(f"background-color: {t.border};")
        if hasattr(self, "_sep2"):
            self._sep2.setStyleSheet(f"background-color: {t.border};")

    # ── Public API ────────────────────────────────────────────────────────────

    def show_host(self, host: HostInfo):
        self._current_host = host
        t = theme()

        self._hdr.setText(host.ip)
        self._sub.setText(host.hostname or "No hostname resolved")

        self._f_ip.set_value(host.ip)
        self._f_mac.set_value(host.mac or "—")
        self._f_vendor.set_value(host.vendor or "—")

        status_color = t.status_colors.get(host.status, t.text_dim)
        self._f_status.set_value(host.status.upper(), color=status_color)
        self._f_latency.set_value(host.latency_display, color=t.latency_color(host.latency_ms))

        ttl_str = (
            f"{host.ttl}  ({host.os_hint})"
            if host.ttl > 0 and host.os_hint else
            (str(host.ttl) if host.ttl > 0 else "—")
        )
        self._f_ttl.set_value(ttl_str)

        ports_str = describe_ports(host.open_ports) if host.open_ports else "None found"
        self._f_ports.set_value(ports_str)
        self._f_hostname.set_value(host.hostname or "—")
        self._f_scanned.set_value(
            host.scanned_at.strftime("%H:%M:%S") if host.scanned_at else "—"
        )

        for btn in [self._btn_rescan, self._btn_portscan, self._btn_ssh,
                    self._btn_copy_ip, self._btn_open_web]:
            btn.setEnabled(True)

    def show_empty(self):
        self._current_host = None
        self._hdr.setText("Select a host")
        self._sub.setText("Click a row in the table above")
        for f in [self._f_ip, self._f_mac, self._f_vendor,
                  self._f_status, self._f_latency, self._f_ttl,
                  self._f_ports, self._f_hostname, self._f_scanned]:
            f.set_value("—")
        for btn in [self._btn_rescan, self._btn_portscan, self._btn_ssh,
                    self._btn_copy_ip, self._btn_open_web]:
            btn.setEnabled(False)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _do_rescan(self):
        if self._current_host:
            self.rescan_requested.emit(self._current_host.ip)

    def _do_port_scan(self):
        if not self._current_host:
            return
        from gui.dialogs import PortScanDialog
        dlg = PortScanDialog(self._current_host, self.window())
        dlg.exec()

    def _do_ssh(self):
        if self._current_host:
            self.ssh_requested.emit(self._current_host.ip)

    def _do_copy_ip(self):
        if self._current_host:
            QApplication.clipboard().setText(self._current_host.ip)
            self._btn_copy_ip.setText("Copied")
            QTimer.singleShot(1500, lambda: self._btn_copy_ip.setText("Copy IP"))

    def _do_open_web(self):
        if not self._current_host:
            return
        webbrowser.open(f"http://{self._current_host.ip}")
