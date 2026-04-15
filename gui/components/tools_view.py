"""
Tools page — quick OS diagnostics, custom command runner, activity log.

Provides one-click access to common networking diagnostics
(ipconfig, arp, route, netsh) plus a free-form command runner so the
operator can run anything they need without leaving the app.

The output area is read-only and uses a monospace font; the activity
log keeps a rolling history of every command executed.
"""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QGroupBox, QPlainTextEdit, QFileDialog, QMessageBox, QFrame,
)

from gui.themes import theme, ThemeManager
from gui.qt_safety import disconnect_signal


_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0


# Default command set per OS
_WIN_COMMANDS = [
    ("ipconfig /all",   "ipconfig /all"),
    ("arp -a",          "arp -a"),
    ("route print",     "route print"),
    ("netsh interfaces", "netsh interface show interface"),
    ("getmac /v",       "getmac /v /fo list"),
]
_NIX_COMMANDS = [
    ("ifconfig / ip a", "ip addr || ifconfig"),
    ("arp",             "arp -a || ip neigh"),
    ("route",           "ip route || netstat -rn"),
    ("dns",             "cat /etc/resolv.conf"),
]


# ── Worker ───────────────────────────────────────────────────────────────────


class _CommandWorker(QThread):
    """Run a shell command line and emit its combined output once."""

    done = pyqtSignal(str, int, str)   # cmd, return_code, output

    def __init__(self, command: str, parent=None):
        super().__init__(parent)
        self.command = command

    def run(self) -> None:
        try:
            r = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=_NO_WINDOW,
            )
            output = (r.stdout or "") + (r.stderr or "")
            self.done.emit(self.command, r.returncode, output.rstrip())
        except subprocess.TimeoutExpired:
            self.done.emit(self.command, -1, "[command timed out after 30s]")
        except Exception as exc:
            self.done.emit(self.command, -1, f"[error] {exc}")


# ── Tools view ───────────────────────────────────────────────────────────────


class ToolsView(QWidget):
    """Diagnostics + custom command runner + rolling activity log."""

    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers: list[_CommandWorker] = []
        self._shutting_down = False
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)

        title = QLabel("DIAGNOSTIC TOOLS")
        title.setObjectName("lbl_section")
        root.addWidget(title)

        subtitle = QLabel(
            "One-click OS diagnostics and a sandboxed command runner."
        )
        subtitle.setObjectName("lbl_subtitle")
        root.addWidget(subtitle)

        # ── Quick commands ──────────────────────────────────────────────────
        quick_box = QGroupBox("QUICK COMMANDS")
        ql = QVBoxLayout(quick_box)
        ql.setContentsMargins(16, 24, 16, 16)
        ql.setSpacing(10)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        commands = _WIN_COMMANDS if _IS_WINDOWS else _NIX_COMMANDS
        for label, cmd in commands:
            btn = QPushButton(label)
            btn.setObjectName("btn_action")
            btn.setMinimumHeight(32)
            btn.clicked.connect(
                lambda _checked, c=cmd, l=label: self._run_command(c, l)
            )
            button_row.addWidget(btn)
        button_row.addStretch()
        ql.addLayout(button_row)

        custom_row = QHBoxLayout()
        custom_row.setSpacing(8)
        custom_row.addWidget(QLabel("Run"))

        self._in_cmd = QLineEdit()
        self._in_cmd.setPlaceholderText("Type any command and press Enter…")
        self._in_cmd.setMinimumHeight(32)
        self._in_cmd.returnPressed.connect(self._on_run_custom)
        custom_row.addWidget(self._in_cmd, stretch=1)

        self._btn_run = QPushButton("Execute")
        self._btn_run.setObjectName("btn_primary")
        self._btn_run.setMinimumHeight(32)
        self._btn_run.clicked.connect(self._on_run_custom)
        custom_row.addWidget(self._btn_run)

        self._btn_clear = QPushButton("Clear Output")
        self._btn_clear.setObjectName("btn_action")
        self._btn_clear.setMinimumHeight(32)
        self._btn_clear.clicked.connect(self._on_clear_output)
        custom_row.addWidget(self._btn_clear)

        self._btn_export = QPushButton("Export…")
        self._btn_export.setObjectName("btn_action")
        self._btn_export.setMinimumHeight(32)
        self._btn_export.clicked.connect(self._on_export)
        custom_row.addWidget(self._btn_export)

        ql.addLayout(custom_row)

        root.addWidget(quick_box)

        # ── Output ──────────────────────────────────────────────────────────
        out_box = QGroupBox("OUTPUT")
        ol = QVBoxLayout(out_box)
        ol.setContentsMargins(16, 24, 16, 16)
        ol.setSpacing(0)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMinimumHeight(220)
        f = QFont("Consolas", 11)
        f.setFixedPitch(True)
        self._output.setFont(f)
        ol.addWidget(self._output)

        root.addWidget(out_box, stretch=1)

        # ── Activity log ────────────────────────────────────────────────────
        log_box = QGroupBox("ACTIVITY LOG")
        ll = QVBoxLayout(log_box)
        ll.setContentsMargins(16, 24, 16, 16)
        ll.setSpacing(0)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(140)
        lf = QFont("Consolas", 10)
        lf.setFixedPitch(True)
        self._log.setFont(lf)
        ll.addWidget(self._log)

        root.addWidget(log_box)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        # Group boxes / inputs follow the global stylesheet.
        pass

    # ── Slots ────────────────────────────────────────────────────────────────

    def _on_run_custom(self):
        cmd = self._in_cmd.text().strip()
        if not cmd:
            return
        self._run_command(cmd)
        self._in_cmd.clear()

    def _on_clear_output(self):
        self._output.clear()

    def _on_export(self):
        if not self._output.toPlainText().strip():
            QMessageBox.information(self, "Export", "Output is empty.")
            return
        default_name = (
            f"netengine_diag_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export diagnostics", default_name, "Text Files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._output.toPlainText())
            self._log_line(f"Exported to {path}")
            self.status_message.emit(f"Exported diagnostics → {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _run_command(self, cmd: str, label: Optional[str] = None) -> None:
        if self._shutting_down:
            return
        display = label or cmd
        try:
            self._output.appendPlainText(f"\n$ {display}")
        except RuntimeError:
            return
        self._log_line(f"run: {display}")

        worker = _CommandWorker(cmd, self)
        worker.done.connect(self._on_command_done)
        self._workers.append(worker)
        worker.start()

    @pyqtSlot(str, int, str)
    def _on_command_done(self, cmd: str, rc: int, output: str):
        if self._shutting_down:
            return
        try:
            self._output.appendPlainText(output if output else "(no output)")
            if rc != 0 and rc != -1:
                self._output.appendPlainText(f"[exit code {rc}]")
        except RuntimeError:
            return
        # Drop finished workers
        self._workers = [w for w in self._workers if w.isRunning()]

    def _log_line(self, msg: str) -> None:
        if self._shutting_down:
            return
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self._log.appendPlainText(f"[{ts}] {msg}")
        except RuntimeError:
            return

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def shutdown(self):
        self._shutting_down = True
        for w in self._workers:
            disconnect_signal(getattr(w, "done", None))
            try:
                w.quit()
            except Exception:
                pass
            try:
                w.wait(500)
            except Exception:
                pass
        self._workers.clear()
