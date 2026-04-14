"""
SCP / SFTP file transfer panel.

Used inside the SSH view's "FILE TRANSFER" tab. Reuses the connection
profile entered in the SSH form (no duplicated credential fields).
"""

from __future__ import annotations

import threading
from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QMetaObject
from PyQt6.QtWidgets import (
    QGroupBox, QFormLayout, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton,
    QFileDialog, QProgressBar, QLabel, QRadioButton, QButtonGroup, QWidget,
    QMessageBox, QFrame,
)

from gui.themes import theme, ThemeManager
from scanner.ssh_client import SSHProfile, SCPTransfer, HAS_PARAMIKO


class SCPPanel(QWidget):
    """
    SCP transfer panel.

    `profile_provider()` returns a fresh SSHProfile (typically the one being
    edited in the parent SSH view), so the panel always uses the most recent
    credentials without needing its own form.
    """

    transfer_done = pyqtSignal(bool, str)   # success, message

    def __init__(self, profile_provider: Callable[[], SSHProfile], parent=None):
        super().__init__(parent)
        self._profile_provider = profile_provider
        self._transfer: SCPTransfer | None = None
        self._worker: threading.Thread | None = None
        self._build_ui()
        self.transfer_done.connect(self._on_transfer_done)

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # Hint
        self._hint = QLabel(
            "Transfers reuse the credentials from the connection panel on the left."
        )
        self._hint.setObjectName("lbl_subtitle")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        # Direction group
        dir_box = QGroupBox("DIRECTION")
        dir_lay = QHBoxLayout(dir_box)
        dir_lay.setContentsMargins(16, 22, 16, 14)
        dir_lay.setSpacing(28)

        self._rb_upload = QRadioButton("Upload  (local → remote)")
        self._rb_upload.setChecked(True)
        self._rb_download = QRadioButton("Download  (remote → local)")
        self._dir_group = QButtonGroup(self)
        self._dir_group.addButton(self._rb_upload, 0)
        self._dir_group.addButton(self._rb_download, 1)

        dir_lay.addWidget(self._rb_upload)
        dir_lay.addWidget(self._rb_download)
        dir_lay.addStretch()
        root.addWidget(dir_box)

        # Paths
        paths_box = QGroupBox("PATHS")
        form = QFormLayout(paths_box)
        form.setContentsMargins(16, 22, 16, 14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(14)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        local_row = QWidget()
        local_lay = QHBoxLayout(local_row)
        local_lay.setContentsMargins(0, 0, 0, 0)
        local_lay.setSpacing(6)
        self._in_local = QLineEdit()
        self._in_local.setPlaceholderText("e.g. C:\\Users\\me\\file.zip")
        self._btn_browse = QPushButton("Browse")
        self._btn_browse.setObjectName("btn_action")
        self._btn_browse.clicked.connect(self._on_browse)
        local_lay.addWidget(self._in_local, stretch=1)
        local_lay.addWidget(self._btn_browse)

        self._in_remote = QLineEdit()
        self._in_remote.setPlaceholderText("e.g. /home/user/file.zip")

        form.addRow("Local file:",  local_row)
        form.addRow("Remote path:", self._in_remote)
        root.addWidget(paths_box)

        # Action row
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.setContentsMargins(0, 0, 0, 0)

        self._btn_start = QPushButton("START TRANSFER")
        self._btn_start.setObjectName("btn_primary")
        self._btn_start.setMinimumHeight(36)
        self._btn_start.clicked.connect(self._on_start)

        self._btn_cancel = QPushButton("CANCEL")
        self._btn_cancel.setObjectName("btn_danger")
        self._btn_cancel.setMinimumHeight(36)
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)

        action_row.addWidget(self._btn_start)
        action_row.addWidget(self._btn_cancel)
        action_row.addStretch()
        root.addLayout(action_row)

        # Progress group
        prog_box = QGroupBox("TRANSFER PROGRESS")
        prog_lay = QVBoxLayout(prog_box)
        prog_lay.setContentsMargins(16, 22, 16, 14)
        prog_lay.setSpacing(10)

        self._progress = QProgressBar()
        self._progress.setObjectName("scp_progress")
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        prog_lay.addWidget(self._progress)

        self._status = QLabel("Idle")
        prog_lay.addWidget(self._status)

        root.addWidget(prog_box)

        if not HAS_PARAMIKO:
            self._btn_start.setEnabled(False)
            self._status.setText("paramiko not installed — SCP unavailable")

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._status.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px;"
            f" font-family: 'Consolas', monospace;"
        )
        self._hint.setStyleSheet(f"color: {t.text_dim}; font-size: 11px;")

    # ── Actions ─────────────────────────────────────────────────────────────

    def _on_browse(self):
        if self._rb_upload.isChecked():
            path, _ = QFileDialog.getOpenFileName(self, "Choose local file")
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save downloaded file as",
                self._in_local.text() or "downloaded.bin",
            )
        if path:
            self._in_local.setText(path)

    def _on_start(self):
        if self._worker is not None and self._worker.is_alive():
            return
        if not HAS_PARAMIKO:
            QMessageBox.critical(
                self, "SCP",
                "paramiko is not installed. Run: pip install paramiko"
            )
            return

        local_path = self._in_local.text().strip()
        remote_path = self._in_remote.text().strip()
        if not local_path or not remote_path:
            QMessageBox.warning(self, "SCP", "Both local and remote paths are required.")
            return

        profile = self._profile_provider()
        if not profile.host or not profile.user:
            QMessageBox.warning(
                self, "SCP",
                "SSH host and user must be filled out in the connection panel on the left."
            )
            return

        upload = self._rb_upload.isChecked()
        self._progress.setValue(0)
        self._status.setText("Starting transfer…")
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        self._transfer = SCPTransfer(profile)
        self._worker = threading.Thread(
            target=self._do_transfer,
            args=(self._transfer, upload, local_path, remote_path),
            daemon=True,
        )
        self._worker.start()

    def _on_cancel(self):
        if self._transfer is not None:
            self._transfer.cancel()
        self._status.setText("Cancelling…")

    # ── Worker ──────────────────────────────────────────────────────────────

    def _do_transfer(
        self, transfer: SCPTransfer, upload: bool,
        local_path: str, remote_path: str,
    ):
        def progress(done: int, total: int):
            self._progress_done = done
            self._progress_total = total
            QMetaObject.invokeMethod(
                self, "_apply_progress",
                Qt.ConnectionType.QueuedConnection,
            )

        try:
            if upload:
                transfer.upload(local_path, remote_path, progress_cb=progress)
                msg = f"Uploaded → {remote_path}"
            else:
                transfer.download(remote_path, local_path, progress_cb=progress)
                msg = f"Downloaded → {local_path}"
            self.transfer_done.emit(True, msg)
        except InterruptedError as exc:
            self.transfer_done.emit(False, str(exc))
        except FileNotFoundError as exc:
            self.transfer_done.emit(False, str(exc))
        except Exception as exc:
            self.transfer_done.emit(False, f"{type(exc).__name__}: {exc}")

    @pyqtSlot()
    def _apply_progress(self):
        done = getattr(self, "_progress_done", 0)
        total = getattr(self, "_progress_total", 0)
        if total > 0:
            pct = int(done / total * 100)
            self._progress.setValue(pct)
            self._status.setText(f"{_human(done)} / {_human(total)}  ({pct}%)")
        else:
            self._status.setText(f"{_human(done)} transferred")

    @pyqtSlot(bool, str)
    def _on_transfer_done(self, ok: bool, message: str):
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        if ok:
            self._progress.setValue(100)
            self._status.setText(message)
        else:
            self._status.setText(f"Failed: {message}")
            QMessageBox.warning(self, "SCP transfer", message)
        self._transfer = None
        self._worker = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _human(n: int) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
