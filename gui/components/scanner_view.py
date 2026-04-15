"""
Scanner page — toolbar, stats bar, host table and detail panel.
Owns the ScanController lifecycle.
"""

from __future__ import annotations

import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QMessageBox,
    QSizePolicy,
)

from scanner.host_scanner import HostInfo, ScanConfig, ScanController
from scanner.service_mapper import DEFAULT_SCAN_PORTS
from gui.themes import theme, ThemeManager
from gui.components.scan_toolbar import ScanToolbar
from gui.components.host_table import HostTableWidget
from gui.components.detail_panel import DetailPanel
from gui.components.live_widgets import StatusDot
from gui.qt_safety import stop_timer, disconnect_signal


class _StatCard(QWidget):
    """A single big-number / label tile inside the stats bar."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(140)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._label_text = label
        self._lbl = QLabel(label.upper())
        self._lbl.setObjectName("lbl_field_label")

        self._val = QLabel("0")
        self._val.setMinimumHeight(34)
        self._val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(self._lbl)
        lay.addWidget(self._val)

    def set_value(self, value: str) -> None:
        self._val.setText(value)

    def set_value_style(self, color: str) -> None:
        self._val.setStyleSheet(
            f"color: {color}; font-size: 26px; font-weight: 800;"
            f" font-family: 'Segoe UI', sans-serif; padding: 0;"
        )


class ScannerView(QWidget):
    """Top-level Scanner page."""

    status_message        = pyqtSignal(str)
    elapsed_changed       = pyqtSignal(str)
    scan_finished_summary = pyqtSignal(int, int)   # alive, total
    ssh_to_host           = pyqtSignal(str)        # ip
    scan_state_changed    = pyqtSignal(bool)       # True while scanning
    host_summary_changed  = pyqtSignal(int, int)   # alive, total
    ssh_quick_connect     = pyqtSignal(dict)       # inline connect from drawer
    ssh_quick_disconnect  = pyqtSignal()           # inline disconnect

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controller: ScanController | None = None
        self._scan_start: datetime | None = None
        self._elapsed_timer: QTimer | None = None
        self._total_hosts_in_range = 0
        # Flag flipped by shutdown(). Every async slot (controller
        # results, rescan worker, elapsed timer tick) checks it and
        # early-returns so a late callback can never touch a widget
        # that's mid-destruction.
        self._shutting_down = False

        self._build_ui()
        self._connect_signals()

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toolbar = ScanToolbar()
        root.addWidget(self._toolbar)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        self._sep = sep
        root.addWidget(sep)

        # Horizontal split: scanner content on the left, host details
        # drawer on the right (drawer is hidden until a host is selected).
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Stats bar + table
        self._stats_bar = self._make_stats_bar()
        table_container = QWidget()
        tc_layout = QVBoxLayout(table_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.setSpacing(0)
        tc_layout.addWidget(self._stats_bar)
        self._table = HostTableWidget()
        tc_layout.addWidget(self._table)
        body_lay.addWidget(table_container, stretch=1)

        # Detail panel — right-side drawer, hidden by default
        self._detail = DetailPanel()
        body_lay.addWidget(self._detail, stretch=0)

        root.addWidget(body, stretch=1)

    def _make_stats_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("stats_bar")
        bar.setMinimumHeight(80)
        bar.setMaximumHeight(96)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 14, 24, 14)
        lay.setSpacing(0)

        self._card_total = _StatCard("Total Scanned")
        self._card_alive = _StatCard("Alive Hosts")
        self._card_dead  = _StatCard("Offline")
        self._card_ports = _StatCard("Open Ports")

        cards = [self._card_total, self._card_alive, self._card_dead, self._card_ports]
        for i, card in enumerate(cards):
            lay.addWidget(card)
            if i < len(cards) - 1:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedWidth(1)
                sep.setMinimumHeight(46)
                sep.setStyleSheet(f"background-color: {theme().border};")
                self._stat_seps = getattr(self, "_stat_seps", []) + [sep]
                lay.addSpacing(12)
                lay.addWidget(sep)
                lay.addSpacing(12)

        lay.addStretch()

        # Live activity dot at far right
        self._live_dot = StatusDot(size=10)
        self._live_dot.set_color(theme().text_dim)
        live_box = QVBoxLayout()
        live_box.setSpacing(2)
        live_box.setContentsMargins(0, 0, 0, 0)
        self._live_lbl_top = QLabel("STATUS")
        self._live_lbl_top.setObjectName("lbl_field_label")
        self._live_lbl_bot = QLabel("Idle")
        self._live_lbl_bot.setStyleSheet(
            f"color: {theme().text_dim}; font-size: 13px; font-weight: 600;"
        )
        live_box.addWidget(self._live_lbl_top)

        live_row = QHBoxLayout()
        live_row.setSpacing(8)
        live_row.addWidget(self._live_dot)
        live_row.addWidget(self._live_lbl_bot)
        live_box.addLayout(live_row)
        lay.addLayout(live_box)

        return bar

    def _restyle(self, t):
        self._sep.setStyleSheet(f"background-color: {t.border};")
        self._card_total.set_value_style(t.text)
        self._card_alive.set_value_style(t.green)
        self._card_dead.set_value_style(t.red)
        self._card_ports.set_value_style(t.accent)
        for sep in getattr(self, "_stat_seps", []):
            sep.setStyleSheet(f"background-color: {t.border};")
        self._live_dot.set_color(t.text_dim)
        self._set_live_state(False)

    def _set_live_state(self, scanning: bool):
        t = theme()
        if scanning:
            self._live_dot.set_active(True, color=t.accent)
            self._live_lbl_bot.setText("Scanning…")
            self._live_lbl_bot.setStyleSheet(
                f"color: {t.accent}; font-size: 13px; font-weight: 700;"
            )
        else:
            self._live_dot.set_active(False)
            self._live_dot.set_color(t.text_dim)
            self._live_lbl_bot.setText("Idle")
            self._live_lbl_bot.setStyleSheet(
                f"color: {t.text_dim}; font-size: 13px; font-weight: 600;"
            )

    # ── Wiring ───────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self._toolbar.scan_requested.connect(self._start_scan)
        self._toolbar.stop_requested.connect(self._on_stop)
        self._toolbar.pause_requested.connect(self._on_pause)
        self._toolbar.resume_requested.connect(self._on_resume)
        self._toolbar.filter_changed.connect(self._table.set_filter)
        self._toolbar.status_filter_changed.connect(self._table.set_status_filter)
        self._toolbar.export_requested.connect(self.do_export)
        self._table.host_selected.connect(self._on_host_selected)
        # Re-clicking a row toggles the drawer; keeps interaction snappy.
        self._table.host_activated.connect(self._on_host_activated)
        # Context-menu quick actions
        self._table.host_open_ssh.connect(
            lambda h: self.ssh_to_host.emit(h.ip)
        )
        self._table.host_rescan.connect(lambda h: self._rescan_host(h.ip))
        self._table.host_port_scan.connect(self._open_port_scan_dialog)

        self._detail.rescan_requested.connect(self._rescan_host)
        self._detail.ssh_requested.connect(self.ssh_to_host.emit)
        self._detail.quick_connect_requested.connect(self._on_quick_connect)
        self._detail.panel_closed.connect(self._on_drawer_closed)

    # ── Scan control ─────────────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _start_scan(self, cfg: dict):
        if self._shutting_down:
            return
        # Stop any existing scan cleanly AND disconnect its signals
        # so a late result from the old controller can't land in our
        # slots after the new controller's state is active.
        if self._controller is not None:
            disconnect_signal(self._controller.host_result)
            disconnect_signal(self._controller.progress)
            disconnect_signal(self._controller.finished_scan)
            disconnect_signal(self._controller.error)
            if self._controller.isRunning():
                try:
                    self._controller.stop()
                except Exception:
                    pass
                try:
                    self._controller.wait(3000)
                except Exception:
                    pass
            self._controller = None
        # Stop any in-flight elapsed timer from a previous scan.
        stop_timer(self._elapsed_timer)
        self._elapsed_timer = None

        self._table.clear_hosts()
        self._detail.show_empty()
        self._reset_stats()

        config = ScanConfig(
            network=cfg["network"],
            cidr=cfg["cidr"],
            ports=cfg.get("ports", DEFAULT_SCAN_PORTS),
            max_host_workers=cfg.get("max_workers", 100),
        )

        from scanner.network import get_ip_range
        self._total_hosts_in_range = len(get_ip_range(config.network, config.cidr))

        self._controller = ScanController(config, self)
        self._controller.host_result.connect(self._on_host_result)
        self._controller.progress.connect(self._toolbar.update_progress)
        self._controller.finished_scan.connect(self._on_scan_done)
        self._controller.error.connect(self._on_error)

        self._scan_start = datetime.now()
        self._toolbar.set_scanning(True)
        self._set_live_state(True)
        self.scan_state_changed.emit(True)
        self.status_message.emit(
            f"Scanning {config.network}/{config.cidr} "
            f"({self._total_hosts_in_range} hosts)…"
        )

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(500)

        self._controller.start()

    def _on_stop(self):
        if self._controller:
            self._controller.stop()

    def _on_pause(self):
        if self._controller:
            self._controller.pause()
            self.status_message.emit("Scan paused")

    def _on_resume(self):
        if self._controller:
            self._controller.resume()
            self.status_message.emit("Scanning…")

    @pyqtSlot(str)
    def _rescan_host(self, ip: str):
        if self._shutting_down:
            return
        if not self._controller:
            cfg = ScanConfig()
        else:
            cfg = self._controller.config

        stop_ev = threading.Event()

        def _work():
            try:
                from scanner.network import get_arp_cache
                from scanner.host_scanner import scan_single_host
                result = scan_single_host(ip, cfg, get_arp_cache(), stop_ev)
            except Exception:
                return
            # Bounce back to the main thread via QTimer.singleShot so
            # the table update runs on the Qt event loop thread. A
            # direct call from a daemon worker would mutate Qt widgets
            # off-thread which is undefined behaviour.
            if self._shutting_down:
                return
            try:
                QTimer.singleShot(0, lambda r=result: self._on_host_result(r))
            except RuntimeError:
                # View tore down between the shutdown check and the
                # bounce — harmless, drop the result.
                pass

        threading.Thread(target=_work, daemon=True).start()

    # ── Result handlers ──────────────────────────────────────────────────────

    @pyqtSlot(object)
    def _on_host_result(self, host: HostInfo):
        if self._shutting_down or host is None:
            return
        try:
            self._table.upsert_host(host)
            self._update_stats()
        except RuntimeError:
            return

    @pyqtSlot(float)
    def _on_scan_done(self, elapsed: float):
        # Always stop the elapsed timer even if we're shutting down —
        # a live timer would keep firing otherwise.
        stop_timer(self._elapsed_timer)
        if self._shutting_down:
            return

        try:
            self._toolbar.set_scanning(False)
            self._set_live_state(False)
            self.scan_state_changed.emit(False)
            alive = self._table.alive_count()
            total = self._table.total_count()
            self.status_message.emit(
                f"Scan complete — {alive} alive of {total} scanned  ·  {elapsed:.1f}s"
            )
            self.elapsed_changed.emit(f"{elapsed:.1f}s")
            self.scan_finished_summary.emit(alive, total)
            self.host_summary_changed.emit(alive, total)
        except RuntimeError:
            return

        try:
            from utils.history import save_scan
            cfg = self._controller.config if self._controller else None
            if cfg:
                save_scan(self._table.get_all_hosts(), cfg.network, cfg.cidr, elapsed)
        except Exception:
            pass

    @pyqtSlot(str)
    def _on_error(self, msg: str):
        if self._shutting_down:
            return
        try:
            self.status_message.emit(f"Error: {msg}")
            self._toolbar.set_scanning(False)
            self._set_live_state(False)
            self.scan_state_changed.emit(False)
        except RuntimeError:
            return

    # ── UI updates ───────────────────────────────────────────────────────────

    def _update_elapsed(self):
        if self._shutting_down or self._scan_start is None:
            return
        try:
            elapsed = (datetime.now() - self._scan_start).total_seconds()
            self.elapsed_changed.emit(f"{elapsed:.1f}s")
        except RuntimeError:
            return

    def _update_stats(self):
        hosts = self._table.get_all_hosts()
        total = len(hosts)
        alive = sum(1 for h in hosts if h.is_alive)
        dead  = sum(1 for h in hosts if h.status == "dead")
        ports = sum(len(h.open_ports) for h in hosts)
        self._card_total.set_value(self._fmt(total))
        self._card_alive.set_value(self._fmt(alive))
        self._card_dead.set_value(self._fmt(dead))
        self._card_ports.set_value(self._fmt(ports))
        self.host_summary_changed.emit(alive, total)

    @staticmethod
    def _fmt(n: int) -> str:
        return f"{n:,}"

    def _reset_stats(self):
        for card in (self._card_total, self._card_alive,
                     self._card_dead, self._card_ports):
            card.set_value("0")

    @pyqtSlot(object)
    def _on_host_selected(self, host):
        if self._shutting_down:
            return
        # Update the drawer if it's already open; never force-open on
        # passive selection so the drawer stays out of the user's way
        # until they actively click a row.
        try:
            if host and self._detail.is_open:
                self._detail.show_host(host)
        except RuntimeError:
            return

    @pyqtSlot(object)
    def _on_host_activated(self, host):
        """User clicked / activated a host row → toggle the drawer."""
        if self._shutting_down:
            return
        try:
            if host:
                self._detail.toggle_for(host)
        except RuntimeError:
            return

    @pyqtSlot()
    def _on_drawer_closed(self):
        if self._shutting_down:
            return
        # Clear the table selection so the next click is always treated
        # as a fresh open instead of being mistaken for a re-select.
        # Block the selection signal while clearing so we don't
        # re-enter _on_host_selected from inside the close path.
        try:
            sm = self._table.selectionModel()
            if sm is not None:
                sm.blockSignals(True)
                try:
                    self._table.clearSelection()
                finally:
                    sm.blockSignals(False)
        except Exception:
            pass

    @pyqtSlot(dict)
    def _on_quick_connect(self, profile: dict):
        """
        Forward an inline connect request from the host details drawer
        to the SSH page so it can run the actual connection.
        """
        if profile.get("_disconnect"):
            self.ssh_quick_disconnect.emit()
            return
        self.ssh_quick_connect.emit(profile)

    def clear_results(self):
        self._table.clear_hosts()
        self._detail.show_empty()
        self._reset_stats()
        self.host_summary_changed.emit(0, 0)
        self.status_message.emit("Ready")

    # ── Export passthrough ───────────────────────────────────────────────────

    def do_export(self):
        hosts = self._table.get_all_hosts()
        if not hosts:
            QMessageBox.information(self, "Export", "No scan results to export.")
            return
        from gui.dialogs import ExportDialog
        ExportDialog(hosts, self).exec()

    def shutdown(self):
        # Flip the flag FIRST so every async callback races into a
        # harmless early-return instead of touching Qt objects that
        # are about to be destroyed.
        self._shutting_down = True

        # Stop the elapsed-time ticker so it doesn't fire into a
        # deleted view.
        stop_timer(self._elapsed_timer)
        self._elapsed_timer = None

        # Disconnect the controller's signals BEFORE asking it to
        # stop. as_completed() inside the controller's run() keeps
        # iterating even after stop_event fires, and any queued
        # host_result / progress / finished_scan signals would land
        # in our slots while the view is being torn down. Breaking
        # the connection first means those emits are no-ops.
        if self._controller is not None:
            disconnect_signal(self._controller.host_result)
            disconnect_signal(self._controller.progress)
            disconnect_signal(self._controller.finished_scan)
            disconnect_signal(self._controller.error)

            if self._controller.isRunning():
                try:
                    self._controller.stop()
                except Exception:
                    pass
                try:
                    self._controller.wait(3000)
                except Exception:
                    pass

    def selected_host(self) -> HostInfo | None:
        return self._detail._current_host

    def get_table(self):
        """Expose the host table for outside coordination (e.g., context menu)."""
        return self._table

    def _open_port_scan_dialog(self, host):
        if host is None:
            return
        from gui.dialogs import PortScanDialog
        dlg = PortScanDialog(host, self.window())
        dlg.exec()
