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
    QMainWindow, QWidget, QHBoxLayout, QStackedWidget,
)
from PyQt6.QtCore import pyqtSlot, QEvent, QTimer
from PyQt6.QtGui import QAction, QKeySequence, QActionGroup

from gui.themes import theme, ThemeManager
from gui.motion import cross_fade, fade_in
from gui.components.sidebar import Sidebar
from gui.components.scanner_view import ScannerView
from gui.components.terminal_view import TerminalView
from gui.components.ssh_view import SSHView
from gui.components.network_config_view import NetworkConfigView
from gui.components.monitor_view import MonitorView
from gui.components.tools_view import ToolsView
from gui.components.api_console_view import ApiConsoleView
from gui.components.assistant_view import AssistantView
from gui.components.app_status_bar import (
    AppStatusBar, LEVEL_IDLE, LEVEL_BUSY, LEVEL_OK, LEVEL_WARN, LEVEL_ERROR,
)
from utils import settings


PAGE_LABELS = [
    "Scanner",
    "Terminal",
    "SSH Sessions",
    "Adapter",
    "Monitor",
    "Tools",
    "API Console",
    "Assistant",
]
PAGE_SCANNER   = 0
PAGE_TERMINAL  = 1
PAGE_SSH       = 2
PAGE_ADAPTER   = 3
PAGE_MONITOR   = 4
PAGE_TOOLS     = 5
PAGE_API       = 6
PAGE_ASSISTANT = 7


class MainWindow(QMainWindow):

    # Auto-collapse the sidebar when the window is narrower than this.
    # Picked so a 1366x768 laptop in non-fullscreen windowed mode still
    # gets the full sidebar, but smaller panes automatically snap to
    # compact.
    _SIDEBAR_AUTO_COMPACT_BELOW = 1120

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Net Engine — Network Toolkit")
        self.resize(1380, 860)
        # Minimum tuned for a 14" 1366x768 laptop in non-fullscreen
        # windowed mode: ~900x600 still leaves room for a compact
        # sidebar, scan toolbar, host table and status bar.
        self.setMinimumSize(900, 600)

        self._theme_actions: list[QAction] = []
        # Track whether the user has manually overridden auto-collapse
        # — once they click the toggle we stop fighting them on resize.
        self._sidebar_user_pref: bool | None = None

        self._build_ui()
        self._build_menu()
        self._wire_signals()

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._restyle(theme())
        self._sync_theme_menu(ThemeManager.instance().current.name)

        # Initialise the status bar for the default page (Scanner).
        self._refresh_status_for_page(PAGE_SCANNER)
        self._status_bar.set_activity("Ready", LEVEL_IDLE)

        # Reveal the workspace with a soft fade — sets the tone that
        # the app is interactive from the very first frame.
        self._intro_pending = True

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

        self._scanner_view   = ScannerView()
        self._terminal_view  = TerminalView()
        self._ssh_view       = SSHView()
        self._adapter_view   = NetworkConfigView()
        self._monitor_view   = MonitorView()
        self._tools_view     = ToolsView()
        self._api_view       = ApiConsoleView()
        self._assistant_view = AssistantView()

        self._stack.addWidget(self._scanner_view)
        self._stack.addWidget(self._terminal_view)
        self._stack.addWidget(self._ssh_view)
        self._stack.addWidget(self._adapter_view)
        self._stack.addWidget(self._monitor_view)
        self._stack.addWidget(self._tools_view)
        self._stack.addWidget(self._api_view)
        self._stack.addWidget(self._assistant_view)

        # Structured application status bar.
        self._status_bar = AppStatusBar(self)
        self.setStatusBar(self._status_bar)

        # Cache of per-page scan metrics / ssh metrics so switching
        # pages can instantly show the correct context without waiting
        # for the next update.
        self._scan_alive = 0
        self._scan_total = 0
        self._scan_elapsed = ""
        self._ssh_tabs = 0
        self._ssh_state_text = ""
        self._ssh_host_text = ""

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

        self._act_toggle_sidebar = QAction("Toggle Sidebar", self)
        self._act_toggle_sidebar.setShortcut(QKeySequence("Ctrl+B"))
        self._act_toggle_sidebar.triggered.connect(self._on_toggle_sidebar)
        view_menu.addAction(self._act_toggle_sidebar)
        view_menu.addSeparator()

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
        act_about = QAction("About Net Engine", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _wire_signals(self):
        self._sidebar.page_changed.connect(self._switch_page)
        self._sidebar.toggled.connect(self._on_sidebar_toggled)

        # ── Scanner → status bar ─────────────────────────────────────────
        self._scanner_view.status_message.connect(self._on_scanner_activity)
        self._scanner_view.elapsed_changed.connect(self._on_scan_elapsed)
        self._scanner_view.scan_finished_summary.connect(self._on_scan_summary)
        self._scanner_view.scan_state_changed.connect(self._on_scan_state_changed)
        self._scanner_view.host_summary_changed.connect(self._on_host_summary)
        self._scanner_view.ssh_to_host.connect(self._open_ssh_with_host)
        self._scanner_view.ssh_quick_connect.connect(self._on_quick_ssh_connect)
        self._scanner_view.ssh_quick_disconnect.connect(self._on_quick_ssh_disconnect)

        # ── SSH / other views → status bar primary zone ──────────────────
        self._ssh_view.status_message.connect(self._on_ssh_activity)
        self._adapter_view.status_message.connect(
            lambda t: self._status_bar.set_activity(t, LEVEL_IDLE)
        )
        self._monitor_view.status_message.connect(
            lambda t: self._status_bar.set_activity(t, LEVEL_IDLE)
        )
        self._tools_view.status_message.connect(
            lambda t: self._status_bar.set_activity(t, LEVEL_IDLE)
        )
        self._api_view.status_message.connect(
            lambda t: self._status_bar.set_activity(t, LEVEL_IDLE)
        )

        # ── Assistant → status bar + Terminal insert ────────────────────
        self._assistant_view.status_message.connect(
            lambda t: self._status_bar.set_activity(t, LEVEL_IDLE)
        )
        self._assistant_view.insert_to_terminal.connect(
            self._on_ai_insert_to_terminal
        )

    # ── Theme ────────────────────────────────────────────────────────────────

    def _on_theme_changed(self, _t):
        self._restyle(theme())
        self._sync_theme_menu(theme().name)

    def _restyle(self, t):
        # Status bar widget handles its own theme via ThemeManager;
        # sync the label that reflects the current theme name.
        self._status_bar.set_theme_label(t.name)

    def _set_theme(self, name: str):
        ThemeManager.instance().set_theme(name)
        settings.set_value("theme", name)

    def _sync_theme_menu(self, current_name: str):
        for act in self._theme_actions:
            act.setChecked(act.text() == current_name)

    # ── Page switching ───────────────────────────────────────────────────────

    @pyqtSlot(int)
    def _switch_page(self, idx: int):
        # Smooth cross-fade rather than an abrupt index swap so the
        # workspace feels continuous between sections.
        cross_fade(self._stack, idx)
        self._sidebar.set_current(idx)
        self._refresh_status_for_page(idx)

        # Notify pages that opt into a context-entry lifecycle. The
        # terminal page uses this to re-show its welcome banner after
        # a meaningful gap. The hook is fire-and-forget — pages decide
        # for themselves whether anything is worth doing.
        target = self._stack.widget(idx)
        if target is not None and hasattr(target, "on_entered"):
            try:
                target.on_entered()
            except Exception:
                pass

    def _refresh_status_for_page(self, idx: int) -> None:
        """Repopulate the status bar's context zone for the active page."""
        labels = {
            PAGE_SCANNER:   "SCANNER",
            PAGE_TERMINAL:  "TERMINAL",
            PAGE_SSH:       "SSH",
            PAGE_ADAPTER:   "ADAPTER",
            PAGE_MONITOR:   "MONITOR",
            PAGE_TOOLS:     "TOOLS",
            PAGE_API:       "API",
            PAGE_ASSISTANT: "AI",
        }
        self._status_bar.set_mode(labels.get(idx, ""))

        if idx == PAGE_SCANNER:
            self._status_bar.set_scan_metrics(
                alive=self._scan_alive,
                total=self._scan_total,
                elapsed=self._scan_elapsed,
            )
        elif idx == PAGE_SSH:
            self._status_bar.set_ssh_metrics(
                active_tabs=self._ssh_tabs,
                current_host=self._ssh_host_text or None,
                state=self._ssh_state_text or None,
            )
        else:
            self._status_bar.clear_context()

    # ── Scanner signal handlers ──────────────────────────────────────────────

    def _on_scanner_activity(self, text: str) -> None:
        level = LEVEL_IDLE
        low = (text or "").lower()
        if any(k in low for k in ("scanning", "pinging", "resolving")):
            level = LEVEL_BUSY
        elif any(k in low for k in ("error", "failed")):
            level = LEVEL_ERROR
        elif "complete" in low or "done" in low:
            level = LEVEL_OK
        self._status_bar.set_activity(text, level)

    def _on_scan_elapsed(self, text: str) -> None:
        self._scan_elapsed = text or ""
        if self._stack.currentIndex() == PAGE_SCANNER:
            self._status_bar.set_scan_metrics(
                alive=self._scan_alive,
                total=self._scan_total,
                elapsed=self._scan_elapsed,
            )

    def _on_scan_state_changed(self, active: bool) -> None:
        self._sidebar.set_scan_active(active)
        if active:
            self._status_bar.set_activity("Scanning…", LEVEL_BUSY)
        else:
            self._status_bar.set_activity("Ready", LEVEL_IDLE)

    def _on_host_summary(self, alive: int, total: int) -> None:
        self._scan_alive = alive
        self._scan_total = total
        self._sidebar.set_host_summary(alive, total)
        if self._stack.currentIndex() == PAGE_SCANNER:
            self._status_bar.set_scan_metrics(
                alive=alive, total=total, elapsed=self._scan_elapsed
            )

    @pyqtSlot(int, int)
    def _on_scan_summary(self, alive: int, total: int):
        self._scan_alive = alive
        self._scan_total = total
        if alive:
            self._status_bar.push_transient(
                f"Scan complete — {alive} alive of {total}",
                LEVEL_OK, timeout_ms=5000,
            )
        if self._stack.currentIndex() == PAGE_SCANNER:
            self._status_bar.set_scan_metrics(
                alive=alive, total=total, elapsed=self._scan_elapsed
            )

    # ── SSH signal handlers ──────────────────────────────────────────────────

    def _on_ssh_activity(self, text: str) -> None:
        low = (text or "").lower()
        level = LEVEL_IDLE
        if any(k in low for k in ("connecting", "opening", "handshake")):
            level = LEVEL_BUSY
        elif any(k in low for k in ("connected", "authenticated", "ready")):
            level = LEVEL_OK
            self._ssh_state_text = "connected"
        elif any(k in low for k in ("disconnect", "closed")):
            level = LEVEL_WARN
            self._ssh_state_text = "disconnected"
        elif any(k in low for k in ("error", "failed", "refused", "denied")):
            level = LEVEL_ERROR
            self._ssh_state_text = "failed"
        self._status_bar.set_activity(text, level)

        # Update SSH metrics from the view
        try:
            self._ssh_tabs = self._ssh_view._tabs.count()
            idx = self._ssh_view._tabs.currentIndex()
            if idx >= 0:
                w = self._ssh_view._tabs.widget(idx)
                profile = getattr(w, "profile", None)
                if profile is not None:
                    self._ssh_host_text = (
                        f"{profile.user}@{profile.host}:{profile.port}"
                    )
            else:
                self._ssh_host_text = ""
        except Exception:
            pass

        if self._stack.currentIndex() == PAGE_SSH:
            self._status_bar.set_ssh_metrics(
                active_tabs=self._ssh_tabs,
                current_host=self._ssh_host_text or None,
                state=self._ssh_state_text or None,
            )

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
            self._status_bar.set_activity(
                f"Quick connect failed: {exc}", LEVEL_ERROR
            )
            return
        self._switch_page(PAGE_SSH)

    def _on_quick_ssh_disconnect(self):
        # The host-details drawer's Disconnect closes whichever SSH
        # session tab is currently active.
        try:
            self._ssh_view.disconnect_active()
        except Exception:
            pass

    # ── AI assistant ─────────────────────────────────────────────────────────

    def _on_ai_insert_to_terminal(self, command: str) -> None:
        """Receive a suggested command from the AI Assistant page,
        switch to the Terminal page, and pre-fill its input. The
        command is NEVER auto-executed — the user still has to press
        Enter in the terminal after reviewing it."""
        if not command:
            return
        self._switch_page(PAGE_TERMINAL)
        try:
            self._terminal_view.insert_pending_command(command)
            self._status_bar.push_transient(
                "Command inserted — review and press Enter to run",
                LEVEL_IDLE, timeout_ms=4000,
            )
        except Exception as exc:
            self._status_bar.set_activity(
                f"Could not insert command: {exc}", LEVEL_WARN,
            )

    # ── Sidebar toggle / responsive ──────────────────────────────────────────

    def _on_toggle_sidebar(self) -> None:
        """User explicitly toggled the sidebar — record their preference."""
        new_compact = not self._sidebar.is_compact()
        self._sidebar_user_pref = new_compact
        self._sidebar.set_compact(new_compact)

    def _on_sidebar_toggled(self, compact: bool) -> None:
        label = "Sidebar collapsed" if compact else "Sidebar expanded"
        self._status_bar.push_transient(label, LEVEL_IDLE, 2000)

    def resizeEvent(self, event) -> None:  # Qt override
        super().resizeEvent(event)
        # Responsive auto-collapse: if the user has NOT manually
        # expressed a preference, switch the sidebar to compact when
        # the window is narrower than the threshold, and back to
        # expanded when it grows again.
        if self._sidebar_user_pref is None:
            should_be_compact = self.width() < self._SIDEBAR_AUTO_COMPACT_BELOW
            if should_be_compact != self._sidebar.is_compact():
                self._sidebar.set_compact(should_be_compact)

    def changeEvent(self, event) -> None:  # Qt override
        super().changeEvent(event)
        # Nudge the brand header after a window state transition so
        # it pulls a fresh sizeHint against the post-transition DPR.
        # BrandHeader.sizeHint() reads live QFontMetrics on every
        # call, so a single updateGeometry() is all that is needed —
        # no cache invalidation, no refresh hooks. The singleShot
        # defers one event-loop tick so the layout has already
        # reacted to the state change before we reflow.
        if event.type() == QEvent.Type.WindowStateChange:
            QTimer.singleShot(0, self._sidebar.refresh_brand_metrics)

    def showEvent(self, event):
        super().showEvent(event)
        # Play the intro animation exactly once on first show.
        # Scope the opacity effect to the content stack ONLY — applying
        # it to the whole central widget would wrap the sidebar in a
        # QGraphicsOpacityEffect during the 360ms fade, and that effect
        # uses an offscreen pixmap that does not re-rasterize cleanly
        # on Windows HiDPI / fullscreen transitions. Leaving the
        # sidebar out of the effect keeps the brand header rendering
        # on the native path at all times.
        if getattr(self, "_intro_pending", False):
            self._intro_pending = False
            try:
                fade_in(self._stack)
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
            "_assistant_view",
        ):
            try:
                view = getattr(self, view_attr, None)
                if view is not None and hasattr(view, "shutdown"):
                    view.shutdown()
            except Exception:
                pass
        event.accept()
