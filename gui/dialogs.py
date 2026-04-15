"""
Application dialogs:
  - PortScanDialog:    run a custom port scan on a single host
  - PingDialog:        live streaming ping with statistics
  - ScanHistoryDialog: browse past saved scan sessions
  - ExportDialog:      choose format and save results
  - AboutDialog:       version info
  - SettingsDialog:    theme picker + preferred editor + general preferences
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QLineEdit, QProgressBar, QFileDialog, QComboBox,
    QMessageBox, QFormLayout, QGroupBox, QSpinBox, QTreeWidget,
    QTreeWidgetItem, QSplitter, QFrame, QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QSize
from PyQt6.QtGui import QFont, QColor

from scanner.host_scanner import HostInfo
from scanner.port_scanner import scan_ports
from scanner.service_mapper import EXTENDED_SCAN_PORTS
from gui.themes import theme, ThemeManager
from utils import settings
from utils import editor_launcher as _ed


# ── Port Scan Dialog ──────────────────────────────────────────────────────────

class _PortScanWorker(QThread):
    result_ready = pyqtSignal(list)
    progress     = pyqtSignal(int, int)
    finished     = pyqtSignal()

    def __init__(self, ip: str, ports: list[int], parent=None):
        super().__init__(parent)
        self.ip = ip
        self.ports = ports
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        open_ports = scan_ports(
            self.ip, self.ports,
            max_workers=50,
            timeout=0.5,
            progress_cb=lambda d, t: self.progress.emit(d, t),
            stop_event=self._stop,
        )
        self.result_ready.emit(open_ports)
        self.finished.emit()


class PortScanDialog(QDialog):
    def __init__(self, host: HostInfo, parent=None):
        super().__init__(parent)
        self.host = host
        self._worker: _PortScanWorker | None = None
        self.setWindowTitle(f"Port Scan — {host.ip}")
        self.setMinimumSize(560, 460)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(18, 18, 18, 18)

        self._hdr = QLabel(f"Port Scan: {self.host.ip}")
        lay.addWidget(self._hdr)

        # Port range input
        range_row = QHBoxLayout()
        range_row.setSpacing(8)
        range_lbl = QLabel("Port range:")
        self._range_input = QLineEdit("1-1024")
        self._range_input.setPlaceholderText("e.g. 1-1024  or  80,443,8080")
        self._range_input.setMaximumWidth(220)

        self._preset_combo = QComboBox()
        self._preset_combo.addItems(["Custom", "Extended (56)", "Top 1024"])
        self._preset_combo.currentIndexChanged.connect(self._apply_preset)

        range_row.addWidget(range_lbl)
        range_row.addWidget(self._range_input)
        range_row.addSpacing(8)
        range_row.addWidget(QLabel("preset:"))
        range_row.addWidget(self._preset_combo)
        range_row.addStretch()
        lay.addLayout(range_row)

        # Progress
        self._prog = QProgressBar()
        self._prog.setMinimum(0)
        self._prog.setValue(0)
        self._prog.setTextVisible(True)
        lay.addWidget(self._prog)

        # Results
        self._results = QTextEdit()
        self._results.setReadOnly(True)
        self._results.setFont(QFont("Consolas", 11))
        lay.addWidget(self._results)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Start Scan")
        self._btn_start.setObjectName("btn_primary")
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setEnabled(False)
        btn_close = QPushButton("Close")

        self._btn_start.clicked.connect(self._start_scan)
        self._btn_stop.clicked.connect(self._stop_scan)
        btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 16px; font-weight: 700;"
        )

    def _apply_preset(self, idx: int):
        if idx == 1:
            self._range_input.setText(",".join(str(p) for p in EXTENDED_SCAN_PORTS))
        elif idx == 2:
            self._range_input.setText("1-1024")

    def _parse_ports(self) -> list[int]:
        text = self._range_input.text().strip()
        ports = set()
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                lo, _, hi = part.partition("-")
                try:
                    ports.update(range(int(lo), int(hi) + 1))
                except ValueError:
                    pass
            elif part.isdigit():
                ports.add(int(part))
        return sorted(p for p in ports if 1 <= p <= 65535)

    def _start_scan(self):
        ports = self._parse_ports()
        if not ports:
            self._results.setPlainText("No valid ports specified.")
            return

        self._results.clear()
        self._results.append(f"Scanning {len(ports)} ports on {self.host.ip}…\n")
        self._prog.setMaximum(len(ports))
        self._prog.setValue(0)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

        self._worker = _PortScanWorker(self.host.ip, ports, self)
        self._worker.progress.connect(lambda d, t: self._prog.setValue(d))
        self._worker.result_ready.connect(self._show_results)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _stop_scan(self):
        if self._worker:
            self._worker.stop()

    def _show_results(self, open_ports: list[int]):
        if not open_ports:
            self._results.append("No open ports found.")
            return
        self._results.append(f"Found {len(open_ports)} open port(s):\n")
        from scanner.service_mapper import get_service
        for p in open_ports:
            self._results.append(f"  {p:5d}  {get_service(p)}")

    def _on_done(self):
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._prog.setValue(self._prog.maximum())

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        super().closeEvent(event)


# ── Ping Dialog ───────────────────────────────────────────────────────────────

_IS_WIN = platform.system() == "Windows"
_PING_TTL_RE  = re.compile(r"TTL=(\d+)", re.IGNORECASE)
_PING_TIME_RE = re.compile(r"(?:time|Zeit)[=<](\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_PING_LOSS_RE = re.compile(r"(\d+)%\s+(?:packet\s+)?loss", re.IGNORECASE)
_PING_STAT_RE = re.compile(
    r"(?:Minimum|min)\s*=?\s*(\d+)\s*ms.*?(?:Maximum|max)\s*=?\s*(\d+)\s*ms"
    r".*?(?:Average|avg)\s*=?\s*(\d+)\s*ms",
    re.IGNORECASE | re.DOTALL,
)


class _PingWorker(QThread):
    """Runs `ping` with -t / continuous mode and streams each output line."""

    line_received  = pyqtSignal(str)       # raw line from ping
    ping_result    = pyqtSignal(float, int) # latency_ms, ttl  (per reply)
    stats_ready    = pyqtSignal(int, int, int, int)  # sent, received, min_ms, max_ms
    finished_ping  = pyqtSignal()

    def __init__(self, ip: str, count: int, parent=None):
        super().__init__(parent)
        self.ip    = ip
        self.count = count  # 0 = continuous until stop()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        try:
            if _IS_WIN:
                args = ["ping", "-n", str(self.count) if self.count else "65535",
                        "-w", "2000", self.ip]
            else:
                args = ["ping", "-c", str(self.count) if self.count else "65535",
                        "-W", "2", self.ip]

            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=0x08000000 if _IS_WIN else 0,
            )

            sent = received = min_ms = max_ms = 0
            latencies: list[float] = []

            assert self._proc.stdout is not None
            for raw_line in iter(self._proc.stdout.readline, ""):
                if self._stop.is_set():
                    break
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                self.line_received.emit(line)

                # Parse per-reply latency + TTL
                tm = _PING_TIME_RE.search(line)
                ttl_m = _PING_TTL_RE.search(line)
                if tm:
                    lat = float(tm.group(1))
                    ttl = int(ttl_m.group(1)) if ttl_m else -1
                    latencies.append(lat)
                    received += 1
                    self.ping_result.emit(lat, ttl)

                # Look for statistics block (Windows: "Sent = X, Received = Y")
                if "sent" in line.lower() or "packets" in line.lower():
                    sm = re.search(r"Sent\s*=\s*(\d+)", line, re.IGNORECASE)
                    rm = re.search(r"Received\s*=\s*(\d+)", line, re.IGNORECASE)
                    if sm:
                        sent = int(sm.group(1))
                    if rm:
                        received = int(rm.group(1))

            self._proc.stdout.close()
            self._proc.wait()

            if latencies:
                min_ms = int(min(latencies))
                max_ms = int(max(latencies))
            self.stats_ready.emit(sent or len(latencies), received, min_ms, max_ms)
        except Exception as exc:
            self.line_received.emit(f"[error: {exc}]")
        finally:
            self.finished_ping.emit()


class PingDialog(QDialog):
    """
    Live ping dialog: streams ping output in real-time, shows per-reply
    latency coloring, and summarises min/max/avg when the ping ends.
    """

    def __init__(self, host: HostInfo, parent=None):
        super().__init__(parent)
        self.host    = host
        self._worker: _PingWorker | None = None
        self._replies: list[float] = []
        self.setWindowTitle(f"Ping — {host.ip}")
        self.setMinimumSize(580, 460)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        # Auto-start
        self._start_ping()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(18, 18, 18, 18)

        # Header
        self._hdr = QLabel(f"Ping: {self.host.ip}")
        lay.addWidget(self._hdr)

        # Options row
        opts = QHBoxLayout()
        opts.setSpacing(10)
        opts.addWidget(QLabel("Count:"))
        self._spin_count = QSpinBox()
        self._spin_count.setRange(1, 9999)
        self._spin_count.setValue(20)
        self._spin_count.setFixedWidth(80)
        opts.addWidget(self._spin_count)
        opts.addStretch()

        self._lbl_stat = QLabel("")
        opts.addWidget(self._lbl_stat)
        lay.addLayout(opts)

        # Live output area
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Consolas", 11))
        self._output.setMinimumHeight(280)
        lay.addWidget(self._output)

        # Stats bar
        stats_frame = QFrame()
        stats_frame.setObjectName("detail_card")
        stats_lay = QHBoxLayout(stats_frame)
        stats_lay.setContentsMargins(12, 8, 12, 8)
        stats_lay.setSpacing(24)

        self._lbl_sent  = self._make_stat_pair("SENT", "—")
        self._lbl_recv  = self._make_stat_pair("RECEIVED", "—")
        self._lbl_loss  = self._make_stat_pair("LOSS", "—")
        self._lbl_min   = self._make_stat_pair("MIN", "—")
        self._lbl_max   = self._make_stat_pair("MAX", "—")
        self._lbl_avg   = self._make_stat_pair("AVG", "—")

        for w in (self._lbl_sent, self._lbl_recv, self._lbl_loss,
                  self._lbl_min, self._lbl_max, self._lbl_avg):
            stats_lay.addWidget(w)
        stats_lay.addStretch()
        lay.addWidget(stats_frame)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("Restart")
        self._btn_start.setObjectName("btn_primary")
        self._btn_stop  = QPushButton("Stop")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setEnabled(False)
        btn_close = QPushButton("Close")

        self._btn_start.clicked.connect(self._start_ping)
        self._btn_stop.clicked.connect(self._stop_ping)
        btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

    def _make_stat_pair(self, label: str, value: str) -> QLabel:
        """Returns a QLabel formatted as 'LABEL\nvalue'."""
        lbl = QLabel(f"{label}\n{value}")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lbl

    def _update_stat(self, lbl: QLabel, value: str):
        parts = lbl.text().split("\n", 1)
        lbl.setText(f"{parts[0]}\n{value}")

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 16px; font-weight: 700;"
        )
        self._output.setStyleSheet(
            f"background-color: {t.term_bg}; color: {t.term_fg};"
            f" border: 1px solid {t.term_border}; border-radius: 6px;"
        )
        for lbl in (self._lbl_sent, self._lbl_recv, self._lbl_loss,
                    self._lbl_min, self._lbl_max, self._lbl_avg):
            lbl.setStyleSheet(
                f"color: {t.text}; font-size: 12px; font-weight: 700;"
                f" font-family: 'Consolas', monospace;"
            )

    # ── Ping control ──────────────────────────────────────────────────────────

    def _start_ping(self):
        self._stop_ping()
        self._replies.clear()
        self._output.clear()
        count = self._spin_count.value()
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._lbl_stat.setText(f"Pinging {self.host.ip} ({count}× )…")

        self._worker = _PingWorker(self.host.ip, count, self)
        self._worker.line_received.connect(self._on_line)
        self._worker.ping_result.connect(self._on_reply)
        self._worker.stats_ready.connect(self._on_stats)
        self._worker.finished_ping.connect(self._on_done)
        self._worker.start()

    def _stop_ping(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        self._worker = None

    # ── Result handlers ───────────────────────────────────────────────────────

    def _on_line(self, line: str):
        t = theme()
        # Color-code replies vs timeouts vs headers
        lower = line.lower()
        if "request timed out" in lower or "100%" in lower or "unreachable" in lower:
            color = t.red
        elif "ttl=" in lower or "bytes from" in lower or "time=" in lower:
            color = t.green
        elif "ping statistics" in lower or "packets transmitted" in lower:
            color = t.accent
        else:
            color = t.term_fg

        self._output.moveCursor(self._output.textCursor().MoveOperation.End)
        from PyQt6.QtGui import QTextCharFormat, QColor
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor = self._output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(line + "\n", fmt)
        cursor.setCharFormat(QTextCharFormat())
        self._output.setTextCursor(cursor)
        self._output.ensureCursorVisible()

    def _on_reply(self, latency_ms: float, ttl: int):
        self._replies.append(latency_ms)
        sent = len(self._replies)
        avg  = sum(self._replies) / sent
        self._update_stat(self._lbl_sent, str(sent))
        self._update_stat(self._lbl_recv, str(sent))
        self._update_stat(self._lbl_loss, "0%")
        self._update_stat(self._lbl_min,  f"{min(self._replies):.0f} ms")
        self._update_stat(self._lbl_max,  f"{max(self._replies):.0f} ms")
        self._update_stat(self._lbl_avg,  f"{avg:.1f} ms")

    def _on_stats(self, sent: int, received: int, min_ms: int, max_ms: int):
        loss = max(0, sent - received)
        loss_pct = f"{loss / sent * 100:.0f}%" if sent else "—"
        self._update_stat(self._lbl_sent, str(sent))
        self._update_stat(self._lbl_recv, str(received))
        self._update_stat(self._lbl_loss, loss_pct)
        if min_ms:
            self._update_stat(self._lbl_min, f"{min_ms} ms")
        if max_ms:
            self._update_stat(self._lbl_max, f"{max_ms} ms")

    def _on_done(self):
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_stat.setText("Complete")
        self._on_line("—" * 40)

    def closeEvent(self, event):
        self._stop_ping()
        super().closeEvent(event)


# ── Scan History Dialog ────────────────────────────────────────────────────────

class ScanHistoryDialog(QDialog):
    """
    Browse and inspect the last 20 saved scans stored in ~/.netscope/history/.
    Shows a tree of scans on the left and host details on the right.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan History")
        self.setMinimumSize(900, 580)
        self._records: list[dict] = []
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        self._load_history()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        self._hdr = QLabel("Scan History")
        lay.addWidget(self._hdr)

        # Splitter: sessions list left, host list right
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # Left: scans tree
        left = QFrame()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)

        lbl_scans = QLabel("SESSIONS")
        lbl_scans.setObjectName("lbl_section")
        left_lay.addWidget(lbl_scans)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Network", "Date", "Alive / Total", "Duration"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionBehavior(
            self._tree.SelectionBehavior.SelectRows
        )
        self._tree.itemSelectionChanged.connect(self._on_session_selected)
        left_lay.addWidget(self._tree)
        splitter.addWidget(left)

        # Right: host table
        right = QFrame()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)

        lbl_hosts = QLabel("HOSTS IN SESSION")
        lbl_hosts.setObjectName("lbl_section")
        right_lay.addWidget(lbl_hosts)

        self._hosts_tree = QTreeWidget()
        self._hosts_tree.setHeaderLabels(
            ["IP", "Status", "Hostname", "MAC", "Vendor", "Latency", "Ports", "OS"]
        )
        self._hosts_tree.setRootIsDecorated(False)
        self._hosts_tree.setAlternatingRowColors(True)
        right_lay.addWidget(self._hosts_tree)
        splitter.addWidget(right)

        splitter.setSizes([300, 580])
        lay.addWidget(splitter, stretch=1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        self._lbl_info = QLabel("")
        btn_row.addWidget(self._lbl_info)
        btn_row.addStretch()

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._load_history)
        btn_close   = QPushButton("Close")
        btn_close.setObjectName("btn_primary")
        btn_close.clicked.connect(self.accept)

        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 16px; font-weight: 700;"
        )
        self._lbl_info.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px;"
        )

    # ── Data ──────────────────────────────────────────────────────────────────

    def _load_history(self):
        try:
            from utils.history import load_history
            self._records = load_history()
        except Exception:
            self._records = []

        self._tree.clear()
        self._hosts_tree.clear()

        if not self._records:
            self._lbl_info.setText("No scan history found.")
            return

        self._lbl_info.setText(f"{len(self._records)} session(s) on record")

        for rec in self._records:
            ts   = rec.get("timestamp", "")[:19].replace("T", " ")
            net  = rec.get("network", "—")
            alive = rec.get("alive_count", 0)
            total = rec.get("host_count", 0)
            dur   = f"{rec.get('elapsed_s', 0):.1f}s"

            item = QTreeWidgetItem([net, ts, f"{alive} / {total}", dur])
            item.setData(0, Qt.ItemDataRole.UserRole, rec)
            self._tree.addTopLevelItem(item)

        self._tree.resizeColumnToContents(0)
        self._tree.resizeColumnToContents(1)

    def _on_session_selected(self):
        t = theme()
        items = self._tree.selectedItems()
        if not items:
            return
        rec: dict = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not rec:
            return

        self._hosts_tree.clear()
        hosts = rec.get("hosts", [])

        for h in hosts:
            status   = h.get("status", "—").upper()
            latency  = (f"{h['latency_ms']:.1f} ms"
                        if h.get("latency_ms", -1) >= 0 else "—")
            ports_raw = h.get("open_ports", [])
            # Show with service names
            from scanner.service_mapper import _SERVICES
            def _p(p):
                svc = _SERVICES.get(p)
                return f"{p}/{svc}" if svc else str(p)
            ports_str = ", ".join(_p(p) for p in sorted(ports_raw)) if ports_raw else "—"

            item = QTreeWidgetItem([
                h.get("ip", ""),
                status,
                h.get("hostname", "") or "—",
                h.get("mac", "") or "—",
                h.get("vendor", "") or "—",
                latency,
                ports_str,
                h.get("os_hint", "") or "—",
            ])

            # Color status
            color = (t.green if status == "ALIVE"
                     else t.red if status == "DEAD"
                     else t.text_dim)
            item.setForeground(1, QColor(color))

            self._hosts_tree.addTopLevelItem(item)

        self._hosts_tree.resizeColumnToContents(0)
        self._hosts_tree.resizeColumnToContents(1)
        self._hosts_tree.resizeColumnToContents(2)


# ── Export Dialog ─────────────────────────────────────────────────────────────

class ExportDialog(QDialog):
    def __init__(self, hosts: list[HostInfo], parent=None):
        super().__init__(parent)
        self._hosts = hosts
        self.setWindowTitle("Export Results")
        self.setMinimumWidth(420)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(14)
        lay.setContentsMargins(18, 18, 18, 18)

        self._hdr = QLabel("Export Scan Results")
        lay.addWidget(self._hdr)

        self._info = QLabel(f"{len(self._hosts)} host(s) ready to export.")
        lay.addWidget(self._info)

        format_row = QHBoxLayout()
        format_lbl = QLabel("Format:")
        self._combo_fmt = QComboBox()
        self._combo_fmt.addItems(["CSV", "JSON"])
        format_row.addWidget(format_lbl)
        format_row.addWidget(self._combo_fmt)
        format_row.addStretch()
        lay.addLayout(format_row)

        btns = QHBoxLayout()
        btn_save = QPushButton("Save File…")
        btn_save.setObjectName("btn_primary")
        btn_close = QPushButton("Cancel")
        btn_save.clicked.connect(self._do_save)
        btn_close.clicked.connect(self.reject)
        btns.addWidget(btn_save)
        btns.addStretch()
        btns.addWidget(btn_close)
        lay.addLayout(btns)

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 15px; font-weight: 700;"
        )
        self._info.setStyleSheet(f"color: {t.text_dim}; font-size: 12px;")

    def _do_save(self):
        fmt = self._combo_fmt.currentText().lower()
        ext = f"*.{fmt}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Scan Results", f"scan_results.{fmt}",
            f"{fmt.upper()} Files ({ext})"
        )
        if not path:
            return

        from utils.export import export_hosts
        try:
            export_hosts(self._hosts, Path(path), fmt)
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))


# ── About Dialog ──────────────────────────────────────────────────────────────

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Net Engine")
        self.setFixedSize(420, 280)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)

        self._title = QLabel("NET ENGINE")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._title)

        self._ver = QLabel("Network Toolkit  ·  v1.1.0")
        self._ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._ver)

        lay.addSpacing(12)

        self._desc = QLabel(
            "Subnet scanner with embedded terminal,\n"
            "multi-session SSH client, and adapter configuration.\n"
            "Built with Python + PyQt6."
        )
        self._desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._desc)

        lay.addSpacing(12)

        btn = QPushButton("Close")
        btn.setFixedWidth(110)
        btn.clicked.connect(self.accept)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def _restyle(self, t):
        self._title.setStyleSheet(
            f"color: {t.accent}; font-size: 28px; font-weight: 800; letter-spacing: 3px;"
        )
        self._ver.setStyleSheet(f"color: {t.text_dim}; font-size: 12px;")
        self._desc.setStyleSheet(f"color: {t.text}; font-size: 12px;")


# ── Settings Dialog ───────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    """Application settings — theme picker + File Transfer editor preference."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Net Engine Settings")
        self.setMinimumSize(560, 460)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 22, 22, 22)
        lay.setSpacing(16)

        self._hdr = QLabel("Settings")
        lay.addWidget(self._hdr)

        # ── Appearance ────────────────────────────────────────────────
        appearance = QGroupBox("APPEARANCE")
        form = QFormLayout(appearance)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(16, 22, 16, 16)

        self._theme_combo = QComboBox()
        self._theme_combo.addItems(ThemeManager.instance().theme_names())
        self._theme_combo.setCurrentText(ThemeManager.instance().current.name)
        self._theme_combo.setMinimumWidth(180)
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)
        form.addRow("Theme:", self._theme_combo)

        self._theme_hint = QLabel(
            "Available: Dark (default), Neon, Space, Glass, "
            "Light (WinSCP)."
        )
        self._theme_hint.setObjectName("lbl_subtitle")
        self._theme_hint.setWordWrap(True)
        form.addRow("", self._theme_hint)

        lay.addWidget(appearance)

        # ── File Transfer → external editor ───────────────────────────
        editor_group = QGroupBox("FILE OPEN EDITOR")
        ef = QFormLayout(editor_group)
        ef.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        ef.setVerticalSpacing(12)
        ef.setHorizontalSpacing(16)
        ef.setContentsMargins(16, 22, 16, 16)

        self._editor_combo = QComboBox()
        for code in _ed.PREF_ORDER:
            self._editor_combo.addItem(_ed.PREF_LABELS[code], code)
        self._editor_combo.setMinimumWidth(260)
        current_pref, current_custom = _ed.get_editor_preference()
        idx = self._editor_combo.findData(current_pref)
        if idx >= 0:
            self._editor_combo.setCurrentIndex(idx)
        self._editor_combo.currentIndexChanged.connect(self._on_editor_pref_changed)
        ef.addRow("Preferred editor:", self._editor_combo)

        self._editor_hint = QLabel(
            "Auto uses Notepad++ first, then Notepad, then the "
            "system default. Pick a specific editor to force it, or "
            "Custom to browse for your own executable."
        )
        self._editor_hint.setObjectName("lbl_subtitle")
        self._editor_hint.setWordWrap(True)
        ef.addRow("", self._editor_hint)

        # Custom executable row — only active when Custom is picked.
        custom_row = QWidget()
        cr = QHBoxLayout(custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        self._editor_path = QLineEdit(current_custom or "")
        self._editor_path.setPlaceholderText(
            r"C:\Path\To\editor.exe  (or any script the OS can launch)"
        )
        self._editor_path.editingFinished.connect(self._on_editor_path_edited)
        cr.addWidget(self._editor_path, 1)
        self._editor_browse = QPushButton("Browse…")
        self._editor_browse.setObjectName("btn_action")
        self._editor_browse.clicked.connect(self._on_editor_browse)
        cr.addWidget(self._editor_browse)
        ef.addRow("Custom path:", custom_row)

        # Detection readout + refresh.
        detect_row = QWidget()
        dr = QHBoxLayout(detect_row)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.setSpacing(10)
        self._editor_detect = QLabel("")
        self._editor_detect.setWordWrap(True)
        dr.addWidget(self._editor_detect, 1)
        self._btn_editor_refresh = QPushButton("Refresh detection")
        self._btn_editor_refresh.setObjectName("btn_action")
        self._btn_editor_refresh.clicked.connect(self._on_editor_refresh)
        dr.addWidget(self._btn_editor_refresh)
        ef.addRow("Detected:", detect_row)

        lay.addWidget(editor_group)

        lay.addStretch()

        # ── Buttons ───────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setObjectName("btn_primary")
        btn_close.setMinimumWidth(110)
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        lay.addLayout(btns)

        # Initial readouts.
        self._sync_editor_custom_enabled()
        self._on_editor_refresh()

    # ── Theme handlers ───────────────────────────────────────────────

    def _on_theme_changed(self, name: str):
        ThemeManager.instance().set_theme(name)
        settings.set_value("theme", name)

    # ── Editor handlers ──────────────────────────────────────────────

    def _on_editor_pref_changed(self, _idx: int) -> None:
        code = self._editor_combo.currentData() or _ed.PREF_AUTO
        _ed.set_editor_preference(code)
        self._sync_editor_custom_enabled()

    def _sync_editor_custom_enabled(self) -> None:
        code = self._editor_combo.currentData() or _ed.PREF_AUTO
        is_custom = (code == _ed.PREF_CUSTOM)
        self._editor_path.setEnabled(is_custom)
        self._editor_browse.setEnabled(is_custom)

    def _on_editor_path_edited(self) -> None:
        path = self._editor_path.text().strip()
        _ed.set_editor_preference(
            self._editor_combo.currentData() or _ed.PREF_AUTO,
            custom_path=path,
        )

    def _on_editor_browse(self) -> None:
        current = self._editor_path.text().strip()
        start_dir = ""
        if current:
            start_dir = os.path.dirname(current)
        # Filter by extension on Windows so users see their .exe /
        # .cmd / .bat launchers first; on other OSes allow everything.
        if platform.system() == "Windows":
            filt = "Executables (*.exe *.cmd *.bat);;All files (*)"
        else:
            filt = "All files (*)"
        path, _sel = QFileDialog.getOpenFileName(
            self, "Choose editor executable", start_dir, filt,
        )
        if not path:
            return
        self._editor_path.setText(path)
        _ed.set_editor_preference(
            self._editor_combo.currentData() or _ed.PREF_AUTO,
            custom_path=path,
        )

    def _on_editor_refresh(self) -> None:
        _ed.clear_detection_cache()
        parts: list[str] = []
        for label, finder in (
            ("Notepad++", _ed.find_notepadpp),
            ("Notepad",   _ed.find_notepad),
            ("VS Code",   _ed.find_vscode),
        ):
            p = finder()
            parts.append(
                f"{label}: {'installed' if p else 'not installed'}"
            )
        self._editor_detect.setText("   ·   ".join(parts))

    # ── Restyle ──────────────────────────────────────────────────────

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 18px; font-weight: 800; letter-spacing: 0.8px;"
        )
        self._theme_hint.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
        if hasattr(self, "_editor_hint"):
            self._editor_hint.setStyleSheet(
                f"color: {t.text_dim}; font-size: 11px;"
            )
        if hasattr(self, "_editor_detect"):
            self._editor_detect.setStyleSheet(
                f"color: {t.text_dim}; font-size: 11px;"
            )
