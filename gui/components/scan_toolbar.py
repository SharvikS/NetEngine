"""
Scan toolbar — interface selector, port presets, scan controls,
search bar, and status filters.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QProgressBar, QSpinBox, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from scanner.network import get_all_interfaces
from scanner.service_mapper import (
    DEFAULT_SCAN_PORTS, EXTENDED_SCAN_PORTS, TOP_1000_PORTS,
)
from gui.themes import theme, ThemeManager
from gui.components.live_widgets import ScanActivity


# ── Port preset options ───────────────────────────────────────────────────────

PORT_PRESETS: list[tuple[str, list[int]]] = [
    ("Default (20 ports)",  DEFAULT_SCAN_PORTS),
    ("Extended (56 ports)", EXTENDED_SCAN_PORTS),
    ("Top 1024",            TOP_1000_PORTS),
    ("None — ping only",    []),
]


def _vlabel(text: str) -> QLabel:
    """A small uppercase section label used above each control."""
    lbl = QLabel(text)
    lbl.setObjectName("lbl_field_label")
    return lbl


class ScanToolbar(QWidget):
    """
    Emits signals when the user interacts with scan controls.
    The main window connects to these and drives the scan controller.
    """

    scan_requested        = pyqtSignal(dict)
    stop_requested        = pyqtSignal()
    pause_requested       = pyqtSignal()
    resume_requested      = pyqtSignal()
    filter_changed        = pyqtSignal(str)
    status_filter_changed = pyqtSignal(str)
    export_requested      = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolbar")
        self._interfaces: list[dict] = []
        self._paused = False
        self._build_ui()
        self._load_interfaces()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(8)

        # The toolbar is organised into three rows so every element
        # has its own horizontal budget and nothing collides even at
        # ~900px window widths:
        #
        #   Row 1 — field selectors (interface / ports / threads)
        #   Row 2 — action buttons   (scan / pause / stop / refresh / export / history)
        #   Row 3 — search + filter + activity + progress
        #
        # Each row uses Expanding size policies and modest minimums so
        # controls shrink before they overlap.

        # ── Row 1: selectors ────────────────────────────────────────────────
        sel_row = QHBoxLayout()
        sel_row.setSpacing(12)
        sel_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        iface_col = QVBoxLayout()
        iface_col.setSpacing(4)
        iface_col.addWidget(_vlabel("INTERFACE"))
        self._combo_iface = QComboBox()
        self._combo_iface.setMinimumWidth(180)
        self._combo_iface.setMaximumWidth(360)
        self._combo_iface.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._combo_iface.setToolTip("Select network interface to scan")
        iface_col.addWidget(self._combo_iface)
        sel_row.addLayout(iface_col, 3)

        ports_col = QVBoxLayout()
        ports_col.setSpacing(4)
        ports_col.addWidget(_vlabel("PORT PRESET"))
        self._combo_ports = QComboBox()
        self._combo_ports.setMinimumWidth(150)
        self._combo_ports.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for label, _ in PORT_PRESETS:
            self._combo_ports.addItem(label)
        ports_col.addWidget(self._combo_ports)
        sel_row.addLayout(ports_col, 2)

        workers_col = QVBoxLayout()
        workers_col.setSpacing(4)
        workers_col.addWidget(_vlabel("THREADS"))
        self._spin_workers = QSpinBox()
        self._spin_workers.setRange(10, 500)
        self._spin_workers.setValue(100)
        self._spin_workers.setFixedWidth(80)
        self._spin_workers.setToolTip("Concurrent host scan threads")
        workers_col.addWidget(self._spin_workers)
        sel_row.addLayout(workers_col, 0)

        sel_row.addStretch(0)

        root.addLayout(sel_row)

        # ── Row 2: action buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.setContentsMargins(0, 0, 0, 0)

        self._btn_scan = QPushButton("START SCAN")
        self._btn_scan.setObjectName("btn_scan")
        self._btn_scan.setMinimumHeight(32)
        self._btn_scan.setMinimumWidth(104)
        self._btn_scan.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_scan.setToolTip("Start scanning the selected subnet")
        self._btn_scan.clicked.connect(self._on_scan_clicked)

        self._btn_pause = QPushButton("PAUSE")
        self._btn_pause.setMinimumHeight(32)
        self._btn_pause.setMinimumWidth(64)
        self._btn_pause.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause_clicked)

        self._btn_stop = QPushButton("STOP")
        self._btn_stop.setObjectName("btn_stop")
        self._btn_stop.setMinimumHeight(32)
        self._btn_stop.setMinimumWidth(64)
        self._btn_stop.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_clicked)

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setObjectName("btn_action")
        self._btn_refresh.setToolTip("Refresh interface list")
        self._btn_refresh.setMinimumHeight(32)
        self._btn_refresh.setMinimumWidth(64)
        self._btn_refresh.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_refresh.clicked.connect(self._load_interfaces)

        self._btn_export = QPushButton("Export")
        self._btn_export.setObjectName("btn_action")
        self._btn_export.setMinimumHeight(32)
        self._btn_export.setMinimumWidth(64)
        self._btn_export.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_export.setToolTip("Export results to CSV or JSON")
        self._btn_export.clicked.connect(self.export_requested.emit)

        self._btn_history = QPushButton("History")
        self._btn_history.setObjectName("btn_action")
        self._btn_history.setMinimumHeight(32)
        self._btn_history.setMinimumWidth(64)
        self._btn_history.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_history.setToolTip("Browse saved scan history")
        self._btn_history.clicked.connect(self._show_history)

        for btn in [self._btn_scan, self._btn_pause, self._btn_stop,
                    self._btn_refresh, self._btn_export, self._btn_history]:
            btn_row.addWidget(btn)
        btn_row.addStretch(1)

        root.addLayout(btn_row)

        # ── Bottom row: search + filter + activity + progress ────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by IP, hostname, vendor, port…")
        self._search.setMinimumWidth(200)
        self._search.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        # Debounce rapid typing so the filter proxy is not
        # invalidated on every keystroke. Without this guard, typing
        # fast while scan results are streaming in can fire 20+
        # filter rebuilds per second against a live model, which
        # stresses Qt's sort/filter machinery. 150 ms feels
        # instantaneous to the user and batches bursts nicely.
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(150)
        self._filter_debounce.timeout.connect(self._emit_filter_changed)
        self._search.textChanged.connect(
            lambda _t: self._filter_debounce.start()
        )
        bottom.addWidget(self._search, 3)

        self._filter_lbl = QLabel("Show:")
        bottom.addWidget(self._filter_lbl)

        self._combo_filter = QComboBox()
        self._combo_filter.addItems(["All Hosts", "Alive Only", "Dead Only"])
        self._combo_filter.setMinimumWidth(110)
        self._combo_filter.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._combo_filter.currentIndexChanged.connect(self._on_filter_changed)
        bottom.addWidget(self._combo_filter, 0)

        bottom.addSpacing(8)

        # Activity bar
        self._activity = ScanActivity(width=120, height=14)
        bottom.addWidget(self._activity)

        # Progress block — compact and always at the end of the row.
        progress_box = QVBoxLayout()
        progress_box.setSpacing(3)

        self._lbl_progress = QLabel("")
        self._lbl_progress.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._lbl_progress.setMinimumWidth(120)

        self._progress = QProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        self._progress.setMinimumWidth(180)
        self._progress.setMaximumWidth(280)
        self._progress.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._progress.setTextVisible(False)

        progress_box.addWidget(self._lbl_progress)
        progress_box.addWidget(self._progress)
        bottom.addLayout(progress_box, 2)

        root.addLayout(bottom)

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._filter_lbl.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px; font-weight: 600;"
        )
        self._lbl_progress.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px; font-family: 'Consolas', monospace;"
        )

    # ── Interface loading ─────────────────────────────────────────────────────

    def _load_interfaces(self):
        self._interfaces = get_all_interfaces()
        self._combo_iface.clear()
        if not self._interfaces:
            self._combo_iface.addItem("No interfaces found")
            return
        for iface in self._interfaces:
            self._combo_iface.addItem(iface["display"])

    # ── Scan controls ─────────────────────────────────────────────────────────

    def _on_scan_clicked(self):
        idx = self._combo_iface.currentIndex()
        if idx < 0 or idx >= len(self._interfaces):
            return

        iface = self._interfaces[idx]
        preset_idx = self._combo_ports.currentIndex()
        _, ports = PORT_PRESETS[preset_idx]

        config = {
            "network":     iface["network"],
            "cidr":        iface["cidr"],
            "host_ip":     iface["ip"],
            "ports":       ports,
            "max_workers": self._spin_workers.value(),
        }
        self.scan_requested.emit(config)

    def _on_pause_clicked(self):
        if self._paused:
            self._paused = False
            self._btn_pause.setText("PAUSE")
            self.resume_requested.emit()
        else:
            self._paused = True
            self._btn_pause.setText("RESUME")
            self.pause_requested.emit()

    def _on_stop_clicked(self):
        self._paused = False
        self._btn_pause.setText("PAUSE")
        self.stop_requested.emit()

    def _on_filter_changed(self, idx: int):
        mapping = {0: "all", 1: "alive", 2: "dead"}
        self.status_filter_changed.emit(mapping.get(idx, "all"))

    def _emit_filter_changed(self) -> None:
        """Debounced hand-off of the search text to the host table."""
        try:
            self.filter_changed.emit(self._search.text())
        except RuntimeError:
            return

    # ── Progress update ───────────────────────────────────────────────────────

    def update_progress(self, scanned: int, total: int):
        pct = int(scanned / total * 100) if total else 0
        self._progress.setValue(pct)
        self._lbl_progress.setText(f"{scanned:>5} / {total:<5}  ({pct}%)")

    def set_scanning(self, scanning: bool):
        self._btn_scan.setEnabled(not scanning)
        self._btn_stop.setEnabled(scanning)
        self._btn_pause.setEnabled(scanning)
        self._combo_iface.setEnabled(not scanning)
        self._combo_ports.setEnabled(not scanning)
        self._spin_workers.setEnabled(not scanning)
        self._activity.set_active(scanning)
        if not scanning:
            self._paused = False
            self._btn_pause.setText("PAUSE")
            self._progress.setValue(0)
            self._lbl_progress.setText("")

    def _show_history(self):
        from gui.dialogs import ScanHistoryDialog
        dlg = ScanHistoryDialog(self.window())
        dlg.exec()
