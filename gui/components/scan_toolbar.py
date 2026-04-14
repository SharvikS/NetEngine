"""
Scan toolbar — interface selector, port presets, scan controls,
search bar, and status filters.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QProgressBar, QSpinBox, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal

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
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(12)

        # ── Top row: brand + controls + actions ──────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(18)
        top.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Brand block
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel("NETSCOPE")
        title.setObjectName("lbl_title")
        sub = QLabel("Network IP Scanner")
        sub.setObjectName("lbl_subtitle")
        brand.addWidget(title)
        brand.addWidget(sub)
        top.addLayout(brand)

        top.addSpacing(8)
        top.addWidget(self._make_separator_v())
        top.addSpacing(8)

        # Interface selector
        iface_col = QVBoxLayout()
        iface_col.setSpacing(4)
        iface_col.addWidget(_vlabel("INTERFACE"))
        self._combo_iface = QComboBox()
        self._combo_iface.setMinimumWidth(280)
        self._combo_iface.setMaximumWidth(360)
        self._combo_iface.setToolTip("Select network interface to scan")
        iface_col.addWidget(self._combo_iface)
        top.addLayout(iface_col)

        # Port preset selector
        ports_col = QVBoxLayout()
        ports_col.setSpacing(4)
        ports_col.addWidget(_vlabel("PORT PRESET"))
        self._combo_ports = QComboBox()
        self._combo_ports.setMinimumWidth(190)
        for label, _ in PORT_PRESETS:
            self._combo_ports.addItem(label)
        ports_col.addWidget(self._combo_ports)
        top.addLayout(ports_col)

        # Workers
        workers_col = QVBoxLayout()
        workers_col.setSpacing(4)
        workers_col.addWidget(_vlabel("THREADS"))
        self._spin_workers = QSpinBox()
        self._spin_workers.setRange(10, 500)
        self._spin_workers.setValue(100)
        self._spin_workers.setFixedWidth(86)
        self._spin_workers.setToolTip("Concurrent host scan threads")
        workers_col.addWidget(self._spin_workers)
        top.addLayout(workers_col)

        top.addStretch()

        # Action buttons (aligned to bottom of top row so they sit beside selectors)
        btn_box = QVBoxLayout()
        btn_box.setSpacing(2)
        btn_box.addWidget(_vlabel("CONTROLS"))

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_scan = QPushButton("START SCAN")
        self._btn_scan.setObjectName("btn_scan")
        self._btn_scan.setFixedHeight(38)
        self._btn_scan.setMinimumWidth(132)
        self._btn_scan.setToolTip("Start scanning the selected subnet")
        self._btn_scan.clicked.connect(self._on_scan_clicked)

        self._btn_pause = QPushButton("PAUSE")
        self._btn_pause.setFixedHeight(38)
        self._btn_pause.setMinimumWidth(86)
        self._btn_pause.setEnabled(False)
        self._btn_pause.clicked.connect(self._on_pause_clicked)

        self._btn_stop = QPushButton("STOP")
        self._btn_stop.setObjectName("btn_stop")
        self._btn_stop.setFixedHeight(38)
        self._btn_stop.setMinimumWidth(86)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_clicked)

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setObjectName("btn_action")
        self._btn_refresh.setToolTip("Refresh interface list")
        self._btn_refresh.setFixedHeight(38)
        self._btn_refresh.clicked.connect(self._load_interfaces)

        self._btn_export = QPushButton("Export")
        self._btn_export.setObjectName("btn_action")
        self._btn_export.setFixedHeight(38)
        self._btn_export.setToolTip("Export results to CSV or JSON")
        self._btn_export.clicked.connect(self.export_requested.emit)

        for btn in [self._btn_scan, self._btn_pause, self._btn_stop,
                    self._btn_refresh, self._btn_export]:
            btn_row.addWidget(btn)

        btn_box.addLayout(btn_row)
        top.addLayout(btn_box)

        root.addLayout(top)

        # ── Bottom row: search + filter + activity + progress ────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by IP, hostname, vendor, port…")
        self._search.setMinimumWidth(300)
        self._search.setMaximumWidth(440)
        self._search.textChanged.connect(self.filter_changed.emit)
        bottom.addWidget(self._search)

        self._filter_lbl = QLabel("Show:")
        bottom.addWidget(self._filter_lbl)

        self._combo_filter = QComboBox()
        self._combo_filter.addItems(["All Hosts", "Alive Only", "Dead Only"])
        self._combo_filter.setMinimumWidth(126)
        self._combo_filter.currentIndexChanged.connect(self._on_filter_changed)
        bottom.addWidget(self._combo_filter)

        bottom.addSpacing(12)

        # Activity bar
        self._activity = ScanActivity(width=140, height=14)
        bottom.addWidget(self._activity)

        bottom.addStretch()

        # Progress block
        progress_box = QVBoxLayout()
        progress_box.setSpacing(3)

        self._lbl_progress = QLabel("")
        self._lbl_progress.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._lbl_progress.setMinimumWidth(160)

        self._progress = QProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        self._progress.setMinimumWidth(260)
        self._progress.setMaximumWidth(320)
        self._progress.setTextVisible(False)

        progress_box.addWidget(self._lbl_progress)
        progress_box.addWidget(self._progress)
        bottom.addLayout(progress_box)

        root.addLayout(bottom)

    def _make_separator_v(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setMinimumHeight(40)
        sep.setStyleSheet(f"background-color: {theme().border};")
        self._sep_v = sep
        return sep

    # ── Theme ─────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._filter_lbl.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px; font-weight: 600;"
        )
        self._lbl_progress.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px; font-family: 'Consolas', monospace;"
        )
        if hasattr(self, "_sep_v"):
            self._sep_v.setStyleSheet(f"background-color: {t.border};")

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
