"""
Host Details drawer.

A right-side panel that opens when a host row is clicked and dismisses
when the X close button is pressed (or when toggled). The drawer is
self-contained: it shows the live host information, exposes a tidy set
of quick actions, and embeds a proper SSH connection form so the user
can move from "I see this host" to "I'm connected" without leaving the
scanner page.

Public surface:
    DetailPanel.show_host(host)        — populate and slide in
    DetailPanel.show_empty()           — clear & hide
    DetailPanel.toggle_for(host)       — open if closed / closed if same host
    DetailPanel.is_open                — bool

Signals:
    panel_closed                       — user closed the drawer
    rescan_requested(str ip)
    ssh_requested(str ip)              — open dedicated SSH page for host
    quick_connect_requested(dict)      — start an inline SSH session
"""

from __future__ import annotations

import webbrowser

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QFormLayout, QLabel,
    QPushButton, QApplication, QFrame, QSizePolicy, QLineEdit, QSpinBox,
    QFileDialog, QToolButton, QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QSize
from PyQt6.QtGui import QFont

from scanner.host_scanner import HostInfo
from scanner.service_mapper import describe_ports
from gui.themes import theme, ThemeManager


# ── Tiny info field (label / value pair) ─────────────────────────────────────


class _InfoField(QWidget):
    """Compact label-on-top + value-below pair used in the host info grid."""

    def __init__(self, label: str, mono: bool = False, parent=None):
        super().__init__(parent)
        self._mono = mono
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._lbl = QLabel(label.upper())
        self._lbl.setObjectName("lbl_field_label")

        self._val = QLabel("—")
        self._val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._val.setMinimumHeight(20)
        self._val.setWordWrap(True)
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
            self._val.setStyleSheet(
                f"color: {color}; font-size: {size}; font-weight: 600;"
            )
        else:
            self._restyle(theme())


# ── Drawer panel ─────────────────────────────────────────────────────────────


class DetailPanel(QFrame):
    """
    Right-side host details drawer.

    Hidden by default; calling show_host() makes the drawer visible and
    populates it with the selected host. The header bar carries the
    title and a circular X close button. Clicking close emits
    `panel_closed` and hides the panel.
    """

    # Outward signals
    panel_closed            = pyqtSignal()
    rescan_requested        = pyqtSignal(str)            # ip
    ssh_requested           = pyqtSignal(str)            # ip — switch to SSH page
    quick_connect_requested = pyqtSignal(dict)           # inline connect with profile

    DRAWER_WIDTH_MIN = 340
    DRAWER_WIDTH_MAX = 420
    DRAWER_WIDTH = DRAWER_WIDTH_MAX  # kept for backwards-compat references

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("detail_drawer")
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Responsive width — shrink to 340px on narrow windows, grow
        # to 420px when there's plenty of room. The parent layout's
        # stretch factor keeps the host table the primary expander.
        self.setMinimumWidth(self.DRAWER_WIDTH_MIN)
        self.setMaximumWidth(self.DRAWER_WIDTH_MAX)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(0)

        self._current_host: HostInfo | None = None
        self._open: bool = False

        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Hidden until a host is picked
        self.hide()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._open

    def show_host(self, host: HostInfo) -> None:
        """Populate with `host` and reveal the drawer."""
        self._current_host = host
        self._populate(host)
        self._enable_actions(True)
        self._open = True
        self.show()

    def show_empty(self) -> None:
        """Clear contents and hide the drawer."""
        self._current_host = None
        self._clear_fields()
        self._enable_actions(False)
        self._open = False
        self.hide()

    def toggle_for(self, host: HostInfo) -> None:
        """
        Toggle behaviour:
          • not visible → open with `host`
          • visible and same host → close
          • visible and different host → repopulate with `host`
        """
        if not self._open:
            self.show_host(host)
            return
        if (self._current_host is not None
                and self._current_host.ip == host.ip):
            self.close_drawer()
            return
        self.show_host(host)

    def close_drawer(self) -> None:
        """Programmatic close (same effect as the X button)."""
        self._current_host = None
        self._clear_fields()
        self._enable_actions(False)
        self._open = False
        self.hide()
        self.panel_closed.emit()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header bar (title + close X) ─────────────────────────────────────
        self._header = QWidget()
        self._header.setObjectName("detail_drawer_header")
        self._header.setFixedHeight(54)

        head_lay = QHBoxLayout(self._header)
        head_lay.setContentsMargins(20, 0, 14, 0)
        head_lay.setSpacing(12)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.setContentsMargins(0, 0, 0, 0)

        self._kicker = QLabel("HOST DETAILS")
        self._kicker.setObjectName("lbl_field_label")
        title_box.addWidget(self._kicker)

        self._hdr = QLabel("—")
        self._hdr.setMinimumHeight(22)
        title_box.addWidget(self._hdr)
        head_lay.addLayout(title_box, stretch=1)

        self._btn_close = QToolButton()
        self._btn_close.setObjectName("btn_drawer_close")
        self._btn_close.setText("✕")
        self._btn_close.setToolTip("Close host details")
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.setFixedSize(QSize(30, 30))
        self._btn_close.clicked.connect(self.close_drawer)
        head_lay.addWidget(self._btn_close, alignment=Qt.AlignmentFlag.AlignVCenter)

        outer.addWidget(self._header)

        # Header divider
        self._head_div = QFrame()
        self._head_div.setFrameShape(QFrame.Shape.HLine)
        self._head_div.setFixedHeight(1)
        outer.addWidget(self._head_div)

        # ── Scrollable body ──────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setObjectName("detail_drawer_scroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        body = QWidget()
        body.setObjectName("detail_drawer_body")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(20, 18, 20, 22)
        body_lay.setSpacing(20)

        # ── Section 1: Host info card ─────────────────────────────────────────
        info_card = QFrame()
        info_card.setObjectName("detail_card")
        info_lay = QVBoxLayout(info_card)
        info_lay.setContentsMargins(16, 14, 16, 14)
        info_lay.setSpacing(12)

        info_title = QLabel("INFORMATION")
        info_title.setObjectName("lbl_field_label")
        info_lay.addWidget(info_title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(14)
        grid.setContentsMargins(0, 0, 0, 0)

        self._f_status   = _InfoField("Status")
        self._f_latency  = _InfoField("Latency", mono=True)
        self._f_ip       = _InfoField("IP Address", mono=True)
        self._f_mac      = _InfoField("MAC Address", mono=True)
        self._f_hostname = _InfoField("Hostname")
        self._f_vendor   = _InfoField("Vendor")
        self._f_ttl      = _InfoField("TTL / OS")
        self._f_scanned  = _InfoField("Last Scanned")
        self._f_ports    = _InfoField("Open Ports")

        # Two-column grid for compact info; ports row spans full width
        grid.addWidget(self._f_status,    0, 0)
        grid.addWidget(self._f_latency,   0, 1)
        grid.addWidget(self._f_ip,        1, 0)
        grid.addWidget(self._f_mac,       1, 1)
        grid.addWidget(self._f_hostname,  2, 0)
        grid.addWidget(self._f_vendor,    2, 1)
        grid.addWidget(self._f_ttl,       3, 0)
        grid.addWidget(self._f_scanned,   3, 1)
        grid.addWidget(self._f_ports,     4, 0, 1, 2)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        info_lay.addLayout(grid)

        body_lay.addWidget(info_card)

        # ── Section 2: Quick actions ──────────────────────────────────────────
        actions_card = QFrame()
        actions_card.setObjectName("detail_card")
        actions_lay = QVBoxLayout(actions_card)
        actions_lay.setContentsMargins(16, 14, 16, 14)
        actions_lay.setSpacing(10)

        actions_title = QLabel("QUICK ACTIONS")
        actions_title.setObjectName("lbl_field_label")
        actions_lay.addWidget(actions_title)

        actions_grid = QGridLayout()
        actions_grid.setHorizontalSpacing(8)
        actions_grid.setVerticalSpacing(8)
        actions_grid.setContentsMargins(0, 0, 0, 0)

        self._btn_rescan   = self._make_action_btn("Rescan")
        self._btn_portscan = self._make_action_btn("Port Scan")
        self._btn_ssh_page = self._make_action_btn("Open SSH Page")
        self._btn_copy_ip  = self._make_action_btn("Copy IP")
        self._btn_open_web = self._make_action_btn("Open in Browser")
        self._btn_ping     = self._make_action_btn("Ping")

        self._btn_rescan.clicked.connect(self._do_rescan)
        self._btn_portscan.clicked.connect(self._do_port_scan)
        self._btn_ssh_page.clicked.connect(self._do_ssh)
        self._btn_copy_ip.clicked.connect(self._do_copy_ip)
        self._btn_open_web.clicked.connect(self._do_open_web)
        self._btn_ping.clicked.connect(self._do_ping)

        actions_grid.addWidget(self._btn_rescan,   0, 0)
        actions_grid.addWidget(self._btn_portscan, 0, 1)
        actions_grid.addWidget(self._btn_ping,     1, 0)
        actions_grid.addWidget(self._btn_open_web, 1, 1)
        actions_grid.addWidget(self._btn_copy_ip,  2, 0)
        actions_grid.addWidget(self._btn_ssh_page, 2, 1)
        actions_grid.setColumnStretch(0, 1)
        actions_grid.setColumnStretch(1, 1)

        actions_lay.addLayout(actions_grid)
        body_lay.addWidget(actions_card)

        # ── Section 3: Connection form ───────────────────────────────────────
        form_card = QFrame()
        form_card.setObjectName("detail_card")
        form_lay = QVBoxLayout(form_card)
        form_lay.setContentsMargins(16, 14, 16, 16)
        form_lay.setSpacing(12)

        form_title = QLabel("CONNECT")
        form_title.setObjectName("lbl_field_label")
        form_lay.addWidget(form_title)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFormAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        self._in_name = QLineEdit()
        self._in_name.setPlaceholderText("Friendly name (optional)")

        self._in_host = QLineEdit()
        self._in_host.setPlaceholderText("hostname or IP")

        self._in_port = QSpinBox()
        self._in_port.setRange(1, 65535)
        self._in_port.setValue(22)
        self._in_port.setMinimumWidth(96)

        self._in_user = QLineEdit()
        self._in_user.setPlaceholderText("username")

        self._in_pass = QLineEdit()
        self._in_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._in_pass.setPlaceholderText("password (or use key)")

        # Key + browse row
        key_row = QWidget()
        key_lay = QHBoxLayout(key_row)
        key_lay.setContentsMargins(0, 0, 0, 0)
        key_lay.setSpacing(6)

        self._in_key = QLineEdit()
        self._in_key.setPlaceholderText("path to private key (optional)")

        self._btn_browse = QPushButton("Browse")
        self._btn_browse.setObjectName("btn_action")
        self._btn_browse.setFixedHeight(30)
        self._btn_browse.clicked.connect(self._on_browse_key)

        key_lay.addWidget(self._in_key, stretch=1)
        key_lay.addWidget(self._btn_browse)

        form.addRow("Name", self._in_name)
        form.addRow("Host", self._in_host)
        form.addRow("Port", self._in_port)
        form.addRow("User", self._in_user)
        form.addRow("Pass", self._in_pass)
        form.addRow("Key",  key_row)
        form_lay.addLayout(form)

        # Connect / disconnect actions
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._btn_connect = QPushButton("CONNECT")
        self._btn_connect.setObjectName("btn_primary")
        self._btn_connect.setMinimumHeight(36)
        self._btn_connect.clicked.connect(self._do_connect)

        self._btn_disconnect = QPushButton("DISCONNECT")
        self._btn_disconnect.setObjectName("btn_danger")
        self._btn_disconnect.setMinimumHeight(36)
        self._btn_disconnect.clicked.connect(self._do_disconnect)

        action_row.addWidget(self._btn_connect, stretch=1)
        action_row.addWidget(self._btn_disconnect, stretch=1)
        form_lay.addLayout(action_row)

        body_lay.addWidget(form_card)
        body_lay.addStretch(1)

        self._scroll.setWidget(body)
        outer.addWidget(self._scroll, stretch=1)

    def _make_action_btn(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("btn_action")
        btn.setMinimumHeight(30)
        btn.setEnabled(False)
        return btn

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t) -> None:
        # Drawer chrome
        self.setStyleSheet(
            f"#detail_drawer {{"
            f"  background-color: {t.bg_raised};"
            f"  border-left: 1px solid {t.border};"
            f"}}"
            f"#detail_drawer_header {{"
            f"  background-color: {t.bg_deep};"
            f"}}"
            f"#detail_drawer_header QLabel {{ background: transparent; }}"
            f"#detail_drawer_body {{ background-color: {t.bg_raised}; }}"
            f"#detail_drawer_scroll {{ background-color: {t.bg_raised}; }}"
            f"#detail_drawer_scroll > QWidget > QWidget {{"
            f"  background-color: {t.bg_raised};"
            f"}}"
            f"#detail_card {{"
            f"  background-color: {t.bg_base};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 8px;"
            f"}}"
            f"QToolButton#btn_drawer_close {{"
            f"  background-color: transparent;"
            f"  color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 15px;"
            f"  font-size: 14px;"
            f"  font-weight: 700;"
            f"}}"
            f"QToolButton#btn_drawer_close:hover {{"
            f"  color: {t.red};"
            f"  border-color: {t.red_dim};"
            f"  background-color: {t.bg_raised};"
            f"}}"
            f"QToolButton#btn_drawer_close:pressed {{"
            f"  background-color: {t.bg_select};"
            f"}}"
        )

        self._head_div.setStyleSheet(f"background-color: {t.border};")
        self._kicker.setStyleSheet(
            f"color: {t.text_dim}; font-size: 10px;"
            f" font-weight: 700; letter-spacing: 0.8px;"
        )
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 17px; font-weight: 800;"
            f" font-family: 'Consolas', monospace;"
        )

    # ── Populate ──────────────────────────────────────────────────────────────

    def _populate(self, host: HostInfo) -> None:
        t = theme()
        self._hdr.setText(host.ip)

        self._f_ip.set_value(host.ip)
        self._f_mac.set_value(host.mac or "—")
        self._f_vendor.set_value(host.vendor or "—")
        self._f_hostname.set_value(host.hostname or "—")

        status_color = t.status_colors.get(host.status, t.text_dim)
        self._f_status.set_value(host.status.upper(), color=status_color)
        self._f_latency.set_value(
            host.latency_display,
            color=t.latency_color(host.latency_ms),
        )

        ttl_str = (
            f"{host.ttl}  ({host.os_hint})"
            if host.ttl > 0 and host.os_hint
            else (str(host.ttl) if host.ttl > 0 else "—")
        )
        self._f_ttl.set_value(ttl_str)

        ports_str = (
            describe_ports(host.open_ports) if host.open_ports else "None found"
        )
        self._f_ports.set_value(ports_str)
        self._f_scanned.set_value(
            host.scanned_at.strftime("%H:%M:%S") if host.scanned_at else "—"
        )

        # Pre-fill the connection form when the host changes
        self._in_host.setText(host.ip)
        if not self._in_name.text().strip() or self._in_name.property("auto"):
            self._in_name.setText(f"Scan-{host.ip}")
            self._in_name.setProperty("auto", True)

        # Pick a sensible default port: prefer 22 if open
        if 22 in host.open_ports:
            self._in_port.setValue(22)
        elif host.open_ports:
            # if SSH not open but other ports are, leave default unless user touched it
            pass

    def _clear_fields(self) -> None:
        self._hdr.setText("—")
        for f in (self._f_ip, self._f_mac, self._f_vendor,
                  self._f_status, self._f_latency, self._f_ttl,
                  self._f_ports, self._f_hostname, self._f_scanned):
            f.set_value("—")

    def _enable_actions(self, enabled: bool) -> None:
        for btn in (self._btn_rescan, self._btn_portscan, self._btn_ssh_page,
                    self._btn_copy_ip, self._btn_open_web, self._btn_ping,
                    self._btn_connect, self._btn_disconnect, self._btn_browse):
            btn.setEnabled(enabled)

    # ── Action callbacks ──────────────────────────────────────────────────────

    def _do_rescan(self) -> None:
        if self._current_host:
            self.rescan_requested.emit(self._current_host.ip)

    def _do_port_scan(self) -> None:
        if not self._current_host:
            return
        from gui.dialogs import PortScanDialog
        dlg = PortScanDialog(self._current_host, self.window())
        dlg.exec()

    def _do_ssh(self) -> None:
        if self._current_host:
            self.ssh_requested.emit(self._current_host.ip)

    def _do_copy_ip(self) -> None:
        if not self._current_host:
            return
        QApplication.clipboard().setText(self._current_host.ip)
        self._btn_copy_ip.setText("Copied")
        QTimer.singleShot(1500, lambda: self._btn_copy_ip.setText("Copy IP"))

    def _do_open_web(self) -> None:
        if not self._current_host:
            return
        webbrowser.open(f"http://{self._current_host.ip}")

    def _do_ping(self) -> None:
        if not self._current_host:
            return
        from gui.dialogs import PingDialog
        dlg = PingDialog(self._current_host, self.window())
        dlg.exec()

    def _on_browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH private key", "", "All files (*)",
        )
        if path:
            self._in_key.setText(path)

    def _do_connect(self) -> None:
        """Emit a profile dict that the parent view will hand to the SSH page."""
        host = self._in_host.text().strip()
        if not host:
            return
        profile = {
            "name":     self._in_name.text().strip(),
            "host":     host,
            "port":     int(self._in_port.value()),
            "user":     self._in_user.text().strip(),
            "password": self._in_pass.text(),
            "key_path": self._in_key.text().strip(),
        }
        self.quick_connect_requested.emit(profile)

    def _do_disconnect(self) -> None:
        # The actual SSHView owns the connection state — emit an empty profile
        # with a sentinel to indicate disconnect.
        self.quick_connect_requested.emit({"_disconnect": True})
