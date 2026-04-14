"""
Application dialogs:
  - PortScanDialog: run a custom port scan on a single host
  - ExportDialog:   choose format and save results
  - AboutDialog:    version info
  - SettingsDialog: theme picker + general preferences
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QLineEdit, QProgressBar, QFileDialog, QComboBox,
    QMessageBox, QFormLayout, QGroupBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont

from scanner.host_scanner import HostInfo
from scanner.port_scanner import scan_ports
from scanner.service_mapper import EXTENDED_SCAN_PORTS
from gui.themes import theme, ThemeManager
from utils import settings


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
        self.setWindowTitle("About NetScope")
        self.setFixedSize(420, 280)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)

        self._title = QLabel("NETSCOPE")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._title)

        self._ver = QLabel("Network IP Scanner  v1.1.0")
        self._ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._ver)

        lay.addSpacing(12)

        self._desc = QLabel(
            "Subnet scanner with embedded terminal,\n"
            "SSH/SCP, and adapter configuration.\n"
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
    """Application settings — currently the theme picker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NetScope Settings")
        self.setMinimumSize(460, 320)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 22, 22, 22)
        lay.setSpacing(16)

        self._hdr = QLabel("Settings")
        lay.addWidget(self._hdr)

        # Appearance
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
            "Available: Dark (default), Neon, Space."
        )
        self._theme_hint.setObjectName("lbl_subtitle")
        self._theme_hint.setWordWrap(True)
        form.addRow("", self._theme_hint)

        lay.addWidget(appearance)

        lay.addStretch()

        # Buttons
        btns = QHBoxLayout()
        btns.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setObjectName("btn_primary")
        btn_close.setMinimumWidth(110)
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        lay.addLayout(btns)

    def _on_theme_changed(self, name: str):
        ThemeManager.instance().set_theme(name)
        settings.set_value("theme", name)

    def _restyle(self, t):
        self._hdr.setStyleSheet(
            f"color: {t.accent}; font-size: 18px; font-weight: 800; letter-spacing: 0.8px;"
        )
        self._theme_hint.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")
