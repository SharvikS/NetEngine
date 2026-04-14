"""
Monitor page — multi-target ping monitor + ad-hoc port tester.

Two side-by-side cards inside a single page:
    1. MULTI-PING MONITOR — track several hosts continuously,
       showing live sent / lost / loss% / last-RTT and an OK/FAIL state.
    2. PORT TESTER — quickly probe a comma-separated list of TCP ports
       on a host without going through the full subnet scanner.

Both flows are theme-aware and integrate with the rest of NetScope's
styling. No external Cooper-isms are exposed in user-facing strings.
"""

from __future__ import annotations

import socket
import threading
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QBrush, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPlainTextEdit, QMessageBox, QFrame,
)

from gui.themes import theme, ThemeManager
from scanner.live_ping import LivePingWorker


# ── Port test worker ─────────────────────────────────────────────────────────


class _PortProbeWorker(QThread):
    """One-shot TCP connect probe for a single (host, port)."""

    done = pyqtSignal(str, int, bool, str)

    def __init__(self, host: str, port: int, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)

    def run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2.5)
                s.connect((self.host, self.port))
            self.done.emit(self.host, self.port, True, "open")
        except Exception as exc:
            self.done.emit(self.host, self.port, False, str(exc))


# ── Monitor view ─────────────────────────────────────────────────────────────


class MonitorView(QWidget):
    """
    Multi-target ping monitor + port tester. Self-contained and safe to
    instantiate before any scan has been performed.
    """

    status_message = pyqtSignal(str)

    PING_COLS = ("Target", "Sent", "Lost", "Loss %", "Last RTT", "Status")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers: dict[str, LivePingWorker] = {}
        self._port_workers: list[_PortProbeWorker] = []

        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)

        # ── Header ───────────────────────────────────────────────────────────
        title = QLabel("LIVE NETWORK MONITOR")
        title.setObjectName("lbl_section")
        root.addWidget(title)

        subtitle = QLabel(
            "Continuously track host availability and probe TCP ports."
        )
        subtitle.setObjectName("lbl_subtitle")
        root.addWidget(subtitle)

        # ── Multi-ping group ─────────────────────────────────────────────────
        ping_box = QGroupBox("MULTI-PING MONITOR")
        pl = QVBoxLayout(ping_box)
        pl.setContentsMargins(16, 24, 16, 16)
        pl.setSpacing(12)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        controls.addWidget(QLabel("Target"))

        self._in_target = QLineEdit()
        self._in_target.setPlaceholderText("IP address or hostname")
        self._in_target.setMinimumHeight(32)
        self._in_target.returnPressed.connect(self._on_add_target)
        controls.addWidget(self._in_target, stretch=1)

        self._btn_add = QPushButton("Add")
        self._btn_add.setObjectName("btn_action")
        self._btn_add.setMinimumHeight(32)
        self._btn_add.clicked.connect(self._on_add_target)
        controls.addWidget(self._btn_add)

        self._btn_start = QPushButton("Start All")
        self._btn_start.setObjectName("btn_primary")
        self._btn_start.setMinimumHeight(32)
        self._btn_start.clicked.connect(self._on_start_all)
        controls.addWidget(self._btn_start)

        self._btn_stop = QPushButton("Stop All")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setMinimumHeight(32)
        self._btn_stop.clicked.connect(self._on_stop_all)
        controls.addWidget(self._btn_stop)

        self._btn_remove = QPushButton("Remove")
        self._btn_remove.setObjectName("btn_action")
        self._btn_remove.setMinimumHeight(32)
        self._btn_remove.clicked.connect(self._on_remove)
        controls.addWidget(self._btn_remove)

        pl.addLayout(controls)

        self._table = QTableWidget(0, len(self.PING_COLS))
        self._table.setHorizontalHeaderLabels(list(self.PING_COLS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(34)
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(self.PING_COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        pl.addWidget(self._table, stretch=1)

        root.addWidget(ping_box, stretch=1)

        # ── Port tester ─────────────────────────────────────────────────────
        port_box = QGroupBox("PORT TESTER")
        pol = QVBoxLayout(port_box)
        pol.setContentsMargins(16, 24, 16, 16)
        pol.setSpacing(12)

        port_row = QHBoxLayout()
        port_row.setSpacing(8)

        port_row.addWidget(QLabel("Host"))
        self._in_pt_host = QLineEdit()
        self._in_pt_host.setPlaceholderText("IP address or hostname")
        self._in_pt_host.setMinimumHeight(32)
        port_row.addWidget(self._in_pt_host, stretch=1)

        port_row.addWidget(QLabel("Ports"))
        self._in_pt_ports = QLineEdit("22, 80, 443, 3389, 8080")
        self._in_pt_ports.setMinimumHeight(32)
        self._in_pt_ports.setToolTip("Comma-separated TCP ports to test")
        port_row.addWidget(self._in_pt_ports, stretch=2)

        self._btn_test = QPushButton("Test Ports")
        self._btn_test.setObjectName("btn_primary")
        self._btn_test.setMinimumHeight(32)
        self._btn_test.clicked.connect(self._on_test_ports)
        port_row.addWidget(self._btn_test)

        pol.addLayout(port_row)

        self._port_log = QPlainTextEdit()
        self._port_log.setReadOnly(True)
        self._port_log.setMinimumHeight(120)
        self._port_log.setMaximumHeight(180)
        f = QFont("Consolas", 11)
        f.setFixedPitch(True)
        self._port_log.setFont(f)
        pol.addWidget(self._port_log)

        root.addWidget(port_box)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        # Tables and group boxes inherit from app stylesheet; nothing
        # widget-specific to repaint here.
        pass

    # ── Public API ───────────────────────────────────────────────────────────

    def add_target(self, target: str) -> None:
        target = (target or "").strip()
        if not target:
            return
        # Avoid duplicates
        if self._row_for(target) >= 0:
            return
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(target))
        for c, txt in enumerate(("0", "0", "0%", "—", "Idle"), start=1):
            item = QTableWidgetItem(txt)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            self._table.setItem(row, c, item)

    # ── Slots ────────────────────────────────────────────────────────────────

    def _on_add_target(self):
        target = self._in_target.text().strip()
        if not target:
            return
        self.add_target(target)
        self._in_target.clear()

    def _on_start_all(self):
        self._on_stop_all()
        for row in range(self._table.rowCount()):
            target = self._table.item(row, 0).text()
            self._set_status(row, "Running…", theme().amber)
            worker = LivePingWorker(target, self)
            worker.stats.connect(self._on_stats)
            worker.finished_target.connect(self._on_finished)
            self._workers[target] = worker
            worker.start()
        self.status_message.emit(
            f"Monitoring {self._table.rowCount()} target(s)"
        )

    def _on_stop_all(self):
        for w in list(self._workers.values()):
            try:
                w.stop()
            except Exception:
                pass
        self._workers.clear()
        for row in range(self._table.rowCount()):
            self._set_status(row, "Idle", theme().text_dim)

    def _on_remove(self):
        rows = sorted({i.row() for i in self._table.selectedIndexes()},
                      reverse=True)
        for row in rows:
            target = self._table.item(row, 0).text()
            w = self._workers.pop(target, None)
            if w is not None:
                try:
                    w.stop()
                except Exception:
                    pass
            self._table.removeRow(row)

    @pyqtSlot(str, int, int, str)
    def _on_stats(self, target: str, sent: int, lost: int, last_rtt: str):
        row = self._row_for(target)
        if row < 0:
            return
        loss_pct = (lost / sent * 100.0) if sent else 0.0
        self._table.item(row, 1).setText(str(sent))
        self._table.item(row, 2).setText(str(lost))
        self._table.item(row, 3).setText(f"{loss_pct:.1f}%")
        self._table.item(row, 4).setText(last_rtt)

        t = theme()
        if last_rtt == "timeout":
            self._set_status(row, "FAIL", t.red)
        else:
            color = t.green if lost == 0 else t.amber
            self._set_status(row, "OK" if lost == 0 else "DEGRADED", color)

    @pyqtSlot(str)
    def _on_finished(self, target: str):
        self._workers.pop(target, None)
        row = self._row_for(target)
        if row >= 0 and not self._workers:
            self._set_status(row, "Stopped", theme().text_dim)

    # ── Port tester ──────────────────────────────────────────────────────────

    def _on_test_ports(self):
        host = self._in_pt_host.text().strip()
        if not host:
            QMessageBox.warning(self, "Port test", "Host is required.")
            return
        ports_raw = self._in_pt_ports.text().strip()
        ports: list[int] = []
        for chunk in ports_raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                p = int(chunk)
                if 1 <= p <= 65535:
                    ports.append(p)
            except ValueError:
                pass
        if not ports:
            QMessageBox.warning(
                self, "Port test", "Enter at least one valid port."
            )
            return

        self._port_log.clear()
        self._port_log.appendPlainText(
            f"Testing {len(ports)} port(s) on {host}…"
        )

        # Start a worker per port (small N, unbounded threads is fine here)
        for p in ports:
            w = _PortProbeWorker(host, p, self)
            w.done.connect(self._on_port_done)
            self._port_workers.append(w)
            w.start()

    @pyqtSlot(str, int, bool, str)
    def _on_port_done(self, host: str, port: int, ok: bool, msg: str):
        marker = "[OPEN  ]" if ok else "[CLOSED]"
        line = f"{marker}  {host}:{port}"
        if not ok:
            line += f"   ({msg})"
        self._port_log.appendPlainText(line)
        # Cleanup any finished workers
        self._port_workers = [
            w for w in self._port_workers if w.isRunning()
        ]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _row_for(self, target: str) -> int:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.text() == target:
                return row
        return -1

    def _set_status(self, row: int, text: str, color: str):
        item = self._table.item(row, 5)
        if item is None:
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, item)
        item.setText(text)
        item.setForeground(QBrush(QColor(color)))

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def shutdown(self):
        self._on_stop_all()
        for w in self._port_workers:
            try:
                w.quit()
                w.wait(500)
            except Exception:
                pass
        self._port_workers.clear()
