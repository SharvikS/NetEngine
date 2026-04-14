"""
Network Adapter page — view and modify the IPv4 configuration of an
Ethernet adapter (Windows-only via netsh).
"""

from __future__ import annotations

import platform
import subprocess
import threading

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QMetaObject
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel,
    QComboBox, QPushButton, QLineEdit, QRadioButton, QButtonGroup, QMessageBox,
    QPlainTextEdit, QFrame, QListWidget, QListWidgetItem, QInputDialog,
)

from gui.themes import theme, ThemeManager
from scanner import net_config
from utils import settings


class NetworkConfigView(QWidget):
    """Read & modify Ethernet adapter IPv4 settings."""

    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        self._refresh_adapters()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)

        # Header
        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel("NETWORK ADAPTER CONFIGURATION")
        title.setObjectName("lbl_section")
        header.addWidget(title)

        self._lbl_admin = QLabel("")
        self._lbl_admin.setObjectName("lbl_subtitle")
        header.addWidget(self._lbl_admin)
        header.addStretch()

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setObjectName("btn_action")
        self._btn_refresh.clicked.connect(self._refresh_adapters)
        header.addWidget(self._btn_refresh)
        root.addLayout(header)

        if not net_config.is_windows():
            warn = QLabel(
                "Network adapter configuration is only available on Windows."
            )
            warn.setStyleSheet("font-size: 13px;")
            root.addWidget(warn)
            root.addStretch()
            return

        # Two-column layout: read on left, apply on right
        cols = QHBoxLayout()
        cols.setSpacing(16)

        left_col = QVBoxLayout()
        left_col.setSpacing(14)

        # Adapter picker
        picker_box = QGroupBox("ADAPTER")
        picker_lay = QHBoxLayout(picker_box)
        picker_lay.setContentsMargins(16, 22, 16, 14)
        picker_lay.setSpacing(10)
        self._combo_adapter = QComboBox()
        self._combo_adapter.setMinimumWidth(280)
        self._combo_adapter.currentTextChanged.connect(self._on_adapter_changed)
        picker_lay.addWidget(self._combo_adapter, stretch=1)
        left_col.addWidget(picker_box)

        # Current config (read-only)
        current_box = QGroupBox("CURRENT CONFIGURATION")
        current = QFormLayout(current_box)
        current.setContentsMargins(16, 22, 16, 14)
        current.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        current.setVerticalSpacing(10)
        current.setHorizontalSpacing(14)
        self._lbl_status   = QLabel("—")
        self._lbl_mode     = QLabel("—")
        self._lbl_ip       = QLabel("—")
        self._lbl_mask     = QLabel("—")
        self._lbl_gateway  = QLabel("—")
        self._lbl_dns      = QLabel("—")
        for lbl in (self._lbl_status, self._lbl_mode, self._lbl_ip,
                    self._lbl_mask, self._lbl_gateway, self._lbl_dns):
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setMinimumHeight(20)

        current.addRow("Link:",     self._lbl_status)
        current.addRow("Mode:",     self._lbl_mode)
        current.addRow("Address:",  self._lbl_ip)
        current.addRow("Mask:",     self._lbl_mask)
        current.addRow("Gateway:",  self._lbl_gateway)
        current.addRow("DNS:",      self._lbl_dns)
        left_col.addWidget(current_box)
        left_col.addStretch()

        cols.addLayout(left_col, stretch=1)

        # Saved profile manager
        profiles_box = QGroupBox("SAVED PROFILES")
        prof_lay = QVBoxLayout(profiles_box)
        prof_lay.setContentsMargins(16, 22, 16, 14)
        prof_lay.setSpacing(10)

        self._profile_list = QListWidget()
        self._profile_list.setMinimumHeight(110)
        self._profile_list.setMaximumHeight(160)
        self._profile_list.itemDoubleClicked.connect(self._on_profile_apply)
        prof_lay.addWidget(self._profile_list)

        prof_btns = QHBoxLayout()
        prof_btns.setSpacing(6)
        b_apply = QPushButton("Apply")
        b_apply.setObjectName("btn_primary")
        b_apply.setMinimumHeight(30)
        b_apply.clicked.connect(self._on_profile_apply)
        prof_btns.addWidget(b_apply)

        b_save = QPushButton("Save Current")
        b_save.setObjectName("btn_action")
        b_save.setMinimumHeight(30)
        b_save.clicked.connect(self._on_profile_save)
        prof_btns.addWidget(b_save)

        b_del = QPushButton("Remove")
        b_del.setObjectName("btn_danger")
        b_del.setMinimumHeight(30)
        b_del.clicked.connect(self._on_profile_delete)
        prof_btns.addWidget(b_del)
        prof_btns.addStretch()
        prof_lay.addLayout(prof_btns)

        left_col.addWidget(profiles_box)

        self._reload_profiles()

        right_col = QVBoxLayout()
        right_col.setSpacing(14)

        # Apply config
        apply_box = QGroupBox("APPLY NEW CONFIGURATION")
        apply_lay = QVBoxLayout(apply_box)
        apply_lay.setContentsMargins(16, 22, 16, 14)
        apply_lay.setSpacing(14)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(28)
        self._rb_dhcp = QRadioButton("DHCP (automatic)")
        self._rb_static = QRadioButton("Static (manual)")
        self._rb_dhcp.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_dhcp, 0)
        self._mode_group.addButton(self._rb_static, 1)
        self._rb_dhcp.toggled.connect(self._on_mode_toggle)
        mode_row.addWidget(self._rb_dhcp)
        mode_row.addWidget(self._rb_static)
        mode_row.addStretch()
        apply_lay.addLayout(mode_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(14)

        self._in_ip = QLineEdit()
        self._in_ip.setPlaceholderText("192.168.1.100")
        self._in_mask = QLineEdit()
        self._in_mask.setPlaceholderText("255.255.255.0")
        self._in_gateway = QLineEdit()
        self._in_gateway.setPlaceholderText("192.168.1.1")
        self._in_dns1 = QLineEdit()
        self._in_dns1.setPlaceholderText("1.1.1.1")
        self._in_dns2 = QLineEdit()
        self._in_dns2.setPlaceholderText("8.8.8.8 (optional)")

        form.addRow("IP Address:",      self._in_ip)
        form.addRow("Subnet Mask:",     self._in_mask)
        form.addRow("Default Gateway:", self._in_gateway)
        form.addRow("Primary DNS:",     self._in_dns1)
        form.addRow("Secondary DNS:",   self._in_dns2)
        apply_lay.addLayout(form)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        self._btn_apply = QPushButton("APPLY")
        self._btn_apply.setObjectName("btn_primary")
        self._btn_apply.setMinimumHeight(34)
        self._btn_apply.clicked.connect(self._on_apply)
        self._btn_load = QPushButton("Load Current")
        self._btn_load.setObjectName("btn_action")
        self._btn_load.setMinimumHeight(34)
        self._btn_load.clicked.connect(self._load_current_into_form)
        button_row.addWidget(self._btn_apply)
        button_row.addWidget(self._btn_load)
        button_row.addStretch()
        apply_lay.addLayout(button_row)

        right_col.addWidget(apply_box)

        # Output log
        out_box = QGroupBox("ACTIVITY LOG")
        out_lay = QVBoxLayout(out_box)
        out_lay.setContentsMargins(16, 22, 16, 14)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(120)
        self._log.setMaximumHeight(180)
        out_lay.addWidget(self._log)
        right_col.addWidget(out_box)

        cols.addLayout(right_col, stretch=1)

        root.addLayout(cols, stretch=1)

        self._on_mode_toggle()

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        admin = net_config.is_admin()
        if admin:
            self._lbl_admin.setText("running as administrator")
            self._lbl_admin.setStyleSheet(
                f"color: {t.green}; font-size: 11px; font-weight: 600;"
            )
        else:
            self._lbl_admin.setText("not elevated — apply will fail")
            self._lbl_admin.setStyleSheet(
                f"color: {t.amber}; font-size: 11px; font-weight: 600;"
            )

    # ── Adapter discovery ────────────────────────────────────────────────────

    def _refresh_adapters(self):
        if not net_config.is_windows():
            return
        names = net_config.list_ethernet_adapters()
        self._combo_adapter.blockSignals(True)
        self._combo_adapter.clear()
        self._combo_adapter.addItems(names)
        self._combo_adapter.blockSignals(False)
        if names:
            self._combo_adapter.setCurrentIndex(0)
            self._on_adapter_changed(names[0])

    def _on_adapter_changed(self, name: str):
        if not name:
            return
        self._read_current(name)

    def _read_current(self, name: str):
        cfg = net_config.get_adapter_config(name)
        t = theme()
        self._lbl_status.setText("UP" if cfg.is_up else "DOWN")
        self._lbl_status.setStyleSheet(
            f"color: {t.green if cfg.is_up else t.red}; font-weight: 600;"
        )
        self._lbl_mode.setText("DHCP" if cfg.dhcp_enabled else "Static")
        self._lbl_ip.setText(cfg.ip or "—")
        self._lbl_mask.setText(cfg.mask or "—")
        self._lbl_gateway.setText(cfg.gateway or "—")
        self._lbl_dns.setText(", ".join(cfg.dns_servers) if cfg.dns_servers else "—")
        self._current_cfg = cfg

    def _load_current_into_form(self):
        cfg = getattr(self, "_current_cfg", None)
        if cfg is None:
            return
        if cfg.dhcp_enabled:
            self._rb_dhcp.setChecked(True)
        else:
            self._rb_static.setChecked(True)
        self._in_ip.setText(cfg.ip)
        self._in_mask.setText(cfg.mask)
        self._in_gateway.setText(cfg.gateway)
        if cfg.dns_servers:
            self._in_dns1.setText(cfg.dns_servers[0])
            if len(cfg.dns_servers) > 1:
                self._in_dns2.setText(cfg.dns_servers[1])
        self._on_mode_toggle()

    def _on_mode_toggle(self):
        is_static = self._rb_static.isChecked()
        for w in (self._in_ip, self._in_mask, self._in_gateway,
                  self._in_dns1, self._in_dns2):
            w.setEnabled(is_static)

    # ── Saved profiles ───────────────────────────────────────────────────────

    def _reload_profiles(self):
        self._profile_list.clear()
        for entry in settings.get_ip_profiles():
            label = (
                f"{entry.get('name', 'profile')}  →  "
                f"{entry.get('ip', '?')} / {entry.get('mask', '?')}"
                f"  gw {entry.get('gateway', '—')}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._profile_list.addItem(item)

    def _on_profile_save(self):
        ip = self._in_ip.text().strip()
        mask = self._in_mask.text().strip()
        if not ip or not mask:
            QMessageBox.warning(
                self, "Save profile",
                "Fill in IP address and mask before saving a profile."
            )
            return
        name, ok = QInputDialog.getText(
            self, "Save profile", "Profile name:"
        )
        if not ok or not name.strip():
            return
        entry = {
            "name":    name.strip(),
            "ip":      ip,
            "mask":    mask,
            "gateway": self._in_gateway.text().strip(),
            "dns1":    self._in_dns1.text().strip(),
            "dns2":    self._in_dns2.text().strip(),
        }
        settings.save_ip_profile(entry)
        self._reload_profiles()
        self.status_message.emit(f"Saved adapter profile '{name.strip()}'")

    def _on_profile_apply(self, *_args):
        item = self._profile_list.currentItem()
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole) or {}
        self._rb_static.setChecked(True)
        self._in_ip.setText(entry.get("ip", ""))
        self._in_mask.setText(entry.get("mask", ""))
        self._in_gateway.setText(entry.get("gateway", ""))
        self._in_dns1.setText(entry.get("dns1", ""))
        self._in_dns2.setText(entry.get("dns2", ""))
        self._on_mode_toggle()
        self.status_message.emit(
            f"Loaded profile '{entry.get('name', '')}'  —  press APPLY to commit"
        )

    def _on_profile_delete(self):
        item = self._profile_list.currentItem()
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole) or {}
        name = entry.get("name", "")
        if not name:
            return
        confirm = QMessageBox.question(
            self, "Delete profile",
            f"Remove saved profile '{name}'?"
        )
        if confirm == QMessageBox.StandardButton.Yes:
            settings.delete_ip_profile(name)
            self._reload_profiles()

    # ── Apply ────────────────────────────────────────────────────────────────

    def _on_apply(self):
        adapter = self._combo_adapter.currentText().strip()
        if not adapter:
            QMessageBox.warning(self, "Network", "Select an adapter first.")
            return

        if not net_config.is_admin():
            QMessageBox.warning(
                self, "Insufficient privileges",
                "Modifying adapter settings requires Administrator.\n"
                "Re-launch NetScope as Administrator and try again."
            )
            return

        if self._rb_dhcp.isChecked():
            confirm = QMessageBox.question(
                self, "Apply DHCP",
                f"Switch '{adapter}' to DHCP for IP and DNS?"
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            self._run_in_thread(lambda: net_config.set_dhcp(adapter), adapter)
            return

        # Static
        ip = self._in_ip.text().strip()
        mask = self._in_mask.text().strip()
        gw = self._in_gateway.text().strip()
        dns1 = self._in_dns1.text().strip()
        dns2 = self._in_dns2.text().strip()
        if not ip or not mask:
            QMessageBox.warning(self, "Network", "IP address and mask are required.")
            return

        confirm = QMessageBox.question(
            self, "Apply static",
            f"Apply static configuration to '{adapter}'?\n\n"
            f"  IP:        {ip}\n  Mask:      {mask}\n  Gateway:   {gw or '(none)'}\n"
            f"  DNS:       {dns1 or '(dhcp)'}{', ' + dns2 if dns2 else ''}"
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._run_in_thread(
            lambda: net_config.set_static(adapter, ip, mask, gw, dns1, dns2),
            adapter,
        )

    def _run_in_thread(self, func, adapter):
        self._btn_apply.setEnabled(False)
        self._log.appendPlainText(f"> applying configuration to {adapter}…")

        def worker():
            try:
                func()
                self._completion = ("ok", "")
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or exc.stdout or str(exc)).strip()
                self._completion = ("error", err)
            except Exception as exc:
                self._completion = ("error", str(exc))
            QMetaObject.invokeMethod(
                self, "_apply_completed",
                Qt.ConnectionType.QueuedConnection,
            )

        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot()
    def _apply_completed(self):
        kind, msg = getattr(self, "_completion", ("error", "?"))
        self._btn_apply.setEnabled(True)
        if kind == "ok":
            self._log.appendPlainText("> success.")
            self.status_message.emit("Network configuration applied")
            adapter = self._combo_adapter.currentText().strip()
            if adapter:
                self._read_current(adapter)
        else:
            self._log.appendPlainText(f"> failed: {msg}")
            QMessageBox.critical(self, "Apply failed", msg or "Unknown error")
