"""
MainWindow — sidebar navigation + stacked pages.

Pages:
    0  Scanner
    1  Terminal
    2  SSH Sessions     (multi-session SSH workspace)
    3  Network Adapter
    4  Monitor
    5  Tools
    6  API Console

Theme switching lives in the View → Theme menu and the Settings dialog.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QStatusBar, QLabel,
)
from PyQt6.QtCore import pyqtSlot, QTimer
from PyQt6.QtGui import QAction, QKeySequence, QActionGroup

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False

from gui.themes import theme, ThemeManager
from gui.components.sidebar import Sidebar
from gui.components.scanner_view import ScannerView
from gui.components.terminal_view import TerminalView
from gui.components.ssh_view import SSHView
from gui.components.network_config_view import NetworkConfigView
from gui.components.monitor_view import MonitorView
from gui.components.tools_view import ToolsView
from gui.components.api_console_view import ApiConsoleView
from gui.components.live_widgets import StatusDot
from utils import settings


PAGE_LABELS = [
    "Scanner",
    "Terminal",
    "SSH Sessions",
    "Adapter",
    "Monitor",
    "Tools",
    "API Console",
]
PAGE_SCANNER  = 0
PAGE_TERMINAL = 1
PAGE_SSH      = 2
PAGE_ADAPTER  = 3
PAGE_MONITOR  = 4
PAGE_TOOLS    = 5
PAGE_API      = 6


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NetScope — Network IP Scanner")
        self.resize(1380, 860)
        self.setMinimumSize(1080, 680)

        self._theme_actions: list[QAction] = []

        self._build_ui()
        self._build_menu()
        self._wire_signals()

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._restyle(theme())
        self._sync_theme_menu(ThemeManager.instance().current.name)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        self._sidebar = Sidebar(PAGE_LABELS)
        root.addWidget(self._sidebar)

        # Stacked pages
        self._stack = QStackedWidget()
        root.addWidget(self._stack, stretch=1)

        self._scanner_view  = ScannerView()
        self._terminal_view = TerminalView()
        self._ssh_view      = SSHView()
        self._adapter_view  = NetworkConfigView()
        self._monitor_view  = MonitorView()
        self._tools_view    = ToolsView()
        self._api_view      = ApiConsoleView()

        self._stack.addWidget(self._scanner_view)
        self._stack.addWidget(self._terminal_view)
        self._stack.addWidget(self._ssh_view)
        self._stack.addWidget(self._adapter_view)
        self._stack.addWidget(self._monitor_view)
        self._stack.addWidget(self._tools_view)
        self._stack.addWidget(self._api_view)

        # Status bar
        self._status_bar = QStatusBar()
        self._status_bar.setSizeGripEnabled(False)
        self.setStatusBar(self._status_bar)

        self._status_dot = StatusDot(size=8)
        self._status_dot.set_color(theme().green)
        self._status_bar.addWidget(self._status_dot)

        self._lbl_status   = QLabel("Ready")
        self._lbl_elapsed  = QLabel("")
        self._lbl_summary  = QLabel("")
        self._lbl_health   = QLabel("")
        self._lbl_theme    = QLabel("")
        self._status_bar.addWidget(self._lbl_status, 1)
        self._status_bar.addPermanentWidget(self._lbl_summary)
        self._status_bar.addPermanentWidget(self._make_separator())
        self._status_bar.addPermanentWidget(self._lbl_elapsed)
        self._status_bar.addPermanentWidget(self._make_separator())
        self._status_bar.addPermanentWidget(self._lbl_health)
        self._status_bar.addPermanentWidget(self._make_separator())
        self._status_bar.addPermanentWidget(self._lbl_theme)

        # Live system health timer (CPU + memory) — uses psutil if available.
        if _HAS_PSUTIL:
            self._health_timer = QTimer(self)
            self._health_timer.setInterval(2000)
            self._health_timer.timeout.connect(self._tick_health)
            self._health_timer.start()
            self._tick_health()

    def _make_separator(self) -> QLabel:
        sep = QLabel("·")
        sep.setStyleSheet(f"color: {theme().text_dim}; padding: 0 8px;")
        return sep

    def _build_menu(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")

        act_export = QAction("&Export Results…", self)
        act_export.setShortcut(QKeySequence("Ctrl+E"))
        act_export.triggered.connect(self._scanner_view.do_export)
        file_menu.addAction(act_export)

        act_settings = QAction("&Settings…", self)
        act_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self._show_settings)
        file_menu.addAction(act_settings)

        file_menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # Scan
        scan_menu = mb.addMenu("&Scan")
        act_start = QAction("Start Scan", self)
        act_start.setShortcut(QKeySequence("F5"))
        act_start.triggered.connect(self._scanner_view._toolbar._on_scan_clicked)
        scan_menu.addAction(act_start)
        act_stop = QAction("Stop Scan", self)
        act_stop.setShortcut(QKeySequence("Escape"))
        act_stop.triggered.connect(self._scanner_view._on_stop)
        scan_menu.addAction(act_stop)
        act_clear = QAction("Clear Results", self)
        act_clear.triggered.connect(self._scanner_view.clear_results)
        scan_menu.addAction(act_clear)

        # View
        view_menu = mb.addMenu("&View")
        for i, label in enumerate(PAGE_LABELS):
            act = QAction(label, self)
            act.setShortcut(QKeySequence(f"Ctrl+{i+1}"))
            act.triggered.connect(lambda _checked, idx=i: self._switch_page(idx))
            view_menu.addAction(act)
        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for tname in ThemeManager.instance().theme_names():
            act = QAction(tname, self)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked, n=tname: self._set_theme(n))
            theme_group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions.append(act)

        # Help
        help_menu = mb.addMenu("&Help")
        act_about = QAction("About NetScope", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _wire_signals(self):
        self._sidebar.page_changed.connect(self._switch_page)

        self._scanner_view.status_message.connect(self._lbl_status.setText)
        self._scanner_view.elapsed_changed.connect(self._lbl_elapsed.setText)
        self._scanner_view.scan_finished_summary.connect(self._on_scan_summary)
        self._scanner_view.scan_state_changed.connect(self._sidebar.set_scan_active)
        self._scanner_view.host_summary_changed.connect(
            lambda alive, total: self._sidebar.set_host_summary(alive, total)
        )
        self._scanner_view.ssh_to_host.connect(self._open_ssh_with_host)
        self._scanner_view.ssh_quick_connect.connect(self._on_quick_ssh_connect)
        self._scanner_view.ssh_quick_disconnect.connect(self._on_quick_ssh_disconnect)

        self._ssh_view.status_message.connect(self._lbl_status.setText)
        self._adapter_view.status_message.connect(self._lbl_status.setText)
        self._monitor_view.status_message.connect(self._lbl_status.setText)
        self._tools_view.status_message.connect(self._lbl_status.setText)
        self._api_view.status_message.connect(self._lbl_status.setText)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _on_theme_changed(self, _t):
        self._restyle(theme())
        self._sync_theme_menu(theme().name)

    def _restyle(self, t):
        self._lbl_status.setStyleSheet(f"color: {t.text_dim}; font-size: 12px;")
        self._lbl_elapsed.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px; font-family: 'Consolas', monospace;"
        )
        self._lbl_summary.setStyleSheet(
            f"color: {t.green}; font-size: 12px; font-weight: 700;"
        )
        self._lbl_health.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px;"
            f" font-family: 'Consolas', monospace;"
        )
        self._lbl_theme.setStyleSheet(
            f"color: {t.accent}; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;"
        )
        self._lbl_theme.setText(t.name.upper())
        self._status_dot.set_color(t.green)
        self._status_dot.set_active(False)

    def _tick_health(self):
        if not _HAS_PSUTIL:
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            self._lbl_health.setText(
                f"CPU {cpu:>4.0f}%  ·  MEM {mem:>4.0f}%"
            )
        except Exception:
            self._lbl_health.setText("")

    def _set_theme(self, name: str):
        ThemeManager.instance().set_theme(name)
        settings.set_value("theme", name)

    def _sync_theme_menu(self, current_name: str):
        for act in self._theme_actions:
            act.setChecked(act.text() == current_name)

    # ── Page switching ───────────────────────────────────────────────────────

    @pyqtSlot(int)
    def _switch_page(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._sidebar.set_current(idx)

    @pyqtSlot(int, int)
    def _on_scan_summary(self, alive: int, total: int):
        if alive:
            self._lbl_summary.setText(f"{alive} alive")
        else:
            self._lbl_summary.setText("")

    def _open_ssh_with_host(self, ip: str):
        """Pre-fill the SSH form with the selected host and switch tab."""
        self._ssh_view.prefill_host(ip)
        self._switch_page(PAGE_SSH)

    def _on_quick_ssh_connect(self, profile: dict):
        """
        Handle inline connect requests from the host details drawer:
        push the profile into the SSH page and trigger a connect, then
        switch to the SSH page so the user sees the live session.
        """
        if not profile:
            return
        try:
            self._ssh_view.connect_with_profile(profile)
        except Exception as exc:
            self._lbl_status.setText(f"Quick connect failed: {exc}")
            return
        self._switch_page(PAGE_SSH)

    def _on_quick_ssh_disconnect(self):
        # The host-details drawer's Disconnect closes whichever SSH
        # session tab is currently active.
        try:
            self._ssh_view.disconnect_active()
        except Exception:
            pass

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _show_about(self):
        from gui.dialogs import AboutDialog
        AboutDialog(self).exec()

    def _show_settings(self):
        from gui.dialogs import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.exec()

    def closeEvent(self, event):
        for view_attr in (
            "_scanner_view", "_terminal_view", "_ssh_view",
            "_monitor_view", "_tools_view", "_api_view",
        ):
            try:
                view = getattr(self, view_attr, None)
                if view is not None and hasattr(view, "shutdown"):
                    view.shutdown()
            except Exception:
                pass
        event.accept()
