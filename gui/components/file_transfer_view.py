"""
File Transfer workspace — WinSCP-style dual-pane manager.

Protocol
--------
**SCP is the primary transfer protocol.** Every byte that moves
between the local machine and the remote host goes through
``scanner.scp_transfer.ScpTransferEngine`` — a hand-rolled SCP
protocol implementation layered on top of paramiko's transport.

Remote browsing is served by one of two backends, chosen at session
bind time in this order:

    1. ``SftpBrowser`` — structured directory listing via the SFTP
       subsystem. Preferred when available: one round trip per
       operation, clean type detection, fast.

    2. ``ShellBrowser`` — shell-level listing via
       ``SSHSession.exec_command``. Used when the remote host does
       not ship the SFTP subsystem (BusyBox / OpenWrt routers, most
       tiny embedded devices). Drives ``ls -1A`` + ``stat -c``
       through a portable one-liner that works on any POSIX shell
       plus the BusyBox toolbox.

Either way, the file transfer view uses the same duck-typed
``browser.listdir / browser.mkdir / browser.rename / …`` interface,
so the two backends are interchangeable at the UI level. SCP still
owns every byte-level transfer — the browser is strictly for
metadata operations.

Layout
------
    ┌──────────────────────────────────────────────────────────────┐
    │  session picker · host · SFTP / SCP state                     │
    ├──────────────────────────────────────────────────────────────┤
    │  toolbar: ↻ ▲ ▼ + ✎ ✕ ≡  …                                   │
    ├──────────────────────────────────────────────────────────────┤
    │  ┌─────────── LOCAL ────────────┐ ┌──────── REMOTE ──────────┐│
    │  │ path bar + nav               │ │ path bar + nav           ││
    │  │ ┌─ file table (sortable) ─┐  │ │ ┌─ file table (sortable)┐││
    │  └──────────────────────────────┘ └──────────────────────────┘│
    ├──────────────────────────────────────────────────────────────┤
    │  transfer queue (Op / Name / Size / Progress / Status)        │
    └──────────────────────────────────────────────────────────────┘

Threading
---------
* Browsing calls (listdir / mkdir / delete / rename) run on the
  global ``QThreadPool`` — short, bounded, fire-and-forget.
* Actual transfers run on a dedicated SCP worker thread owned by
  ``TransferManager``. Jobs are processed sequentially so two
  transfers never fight for the same paramiko transport.
* Every worker result comes back via Qt signals — no widget is
  ever touched from a background thread.

SSH session integration
-----------------------
The view borrows the live ``SSHView`` instance passed in at
construction time. It listens to ``sessions_changed`` so its session
picker always reflects the state of the SSH workspace, and auto-
binds to the first connected session if none is chosen yet. Picking
a disconnected tab shows a "waiting" state; if the bound session
drops mid-transfer the queue surfaces the underlying error and the
UI rebinds to whichever tab is live.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import (
    Qt, QThreadPool, QRunnable, QObject, QTimer, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QFrame, QAbstractItemView, QSizePolicy, QToolButton, QMenu,
    QInputDialog, QMessageBox, QProgressBar, QApplication,
    QStackedWidget,
)

from gui.themes import theme, ThemeManager
from scanner.sftp_client import SftpBrowser, SftpEntry, SftpError
from scanner.shell_browser import ShellBrowser
from scanner.scp_transfer import ScpTransferEngine
from scanner.transfer_manager import (
    TransferManager, TransferJob, JobKind, JobStatus,
)
from scanner.remote_edit_tracker import RemoteEditTracker, TrackedEdit
from utils.editor_launcher import open_file as _launch_editor, EditorError


# ── Small value types ────────────────────────────────────────────────────

from dataclasses import dataclass, field


@dataclass
class _SessionBag:
    """
    Per-session state that follows an SSH session across view
    context switches. Saved in ``_session_bags`` before switching
    away from a session and restored when switching back.
    """
    remote_path: str = ""
    local_path: str = ""


@dataclass
class _NavHistory:
    """Back/forward stack for one pane, capped at ``_MAX`` entries."""
    back: list[str] = field(default_factory=list)
    fwd: list[str] = field(default_factory=list)
    _MAX: int = 50

    def push(self, path: str) -> None:
        """Push ``path`` onto the back stack and clear forward."""
        if not path:
            return
        if self.back and self.back[-1] == path:
            return
        self.back.append(path)
        if len(self.back) > self._MAX:
            self.back.pop(0)
        self.fwd.clear()

    def go_back(self, current: str) -> str | None:
        if not self.back:
            return None
        target = self.back.pop()
        if current:
            self.fwd.append(current)
            if len(self.fwd) > self._MAX:
                self.fwd.pop(0)
        return target

    def go_forward(self, current: str) -> str | None:
        if not self.fwd:
            return None
        target = self.fwd.pop()
        if current:
            self.back.append(current)
            if len(self.back) > self._MAX:
                self.back.pop(0)
        return target

    def can_back(self) -> bool:
        return bool(self.back)

    def can_forward(self) -> bool:
        return bool(self.fwd)


@dataclass
class _PendingOpen:
    """Metadata stashed while a remote-open download is in flight."""
    local_target: str
    remote_source: str
    session_id: int


# ── Background browsing workers ───────────────────────────────────────────

class _BrowseSignals(QObject):
    """Cross-thread channel used by short browsing QRunnables."""
    listdir_done  = pyqtSignal(str, object, str)          # path, entries|None, error
    mkdir_done    = pyqtSignal(str, bool, str)            # path, ok, error
    delete_done   = pyqtSignal(str, bool, str)            # path, ok, error
    rename_done   = pyqtSignal(str, str, bool, str)       # old, new, ok, error


class _ListDirJob(QRunnable):
    """
    Background listdir.

    ``browser`` is duck-typed — it can be an ``SftpBrowser`` or a
    ``ShellBrowser``. Both implement the same ``normalize`` and
    ``listdir`` methods, so the worker doesn't need to know which
    backend is active.
    """
    def __init__(self, browser, path: str, sig: _BrowseSignals):
        super().__init__()
        self._browser = browser
        self._path = path
        self._sig = sig
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            resolved = self._browser.normalize(self._path)
            entries = self._browser.listdir(resolved)
            self._sig.listdir_done.emit(resolved, entries, "")
        except SftpError as exc:
            self._sig.listdir_done.emit(self._path, None, str(exc))
        except Exception as exc:
            self._sig.listdir_done.emit(self._path, None, f"{exc}")


class _MkdirJob(QRunnable):
    def __init__(self, browser, path: str, sig: _BrowseSignals):
        super().__init__()
        self._browser = browser
        self._path = path
        self._sig = sig
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self._browser.mkdir(self._path)
            self._sig.mkdir_done.emit(self._path, True, "")
        except SftpError as exc:
            self._sig.mkdir_done.emit(self._path, False, str(exc))
        except Exception as exc:
            self._sig.mkdir_done.emit(self._path, False, f"{exc}")


class _DeleteJob(QRunnable):
    def __init__(
        self,
        browser,
        path: str,
        is_dir: bool,
        sig: _BrowseSignals,
    ):
        super().__init__()
        self._browser = browser
        self._path = path
        self._is_dir = is_dir
        self._sig = sig
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            if self._is_dir:
                self._browser.rmtree(self._path)
            else:
                self._browser.remove_file(self._path)
            self._sig.delete_done.emit(self._path, True, "")
        except SftpError as exc:
            self._sig.delete_done.emit(self._path, False, str(exc))
        except Exception as exc:
            self._sig.delete_done.emit(self._path, False, f"{exc}")


class _RenameJob(QRunnable):
    def __init__(
        self,
        browser,
        old_path: str,
        new_path: str,
        sig: _BrowseSignals,
    ):
        super().__init__()
        self._browser = browser
        self._old = old_path
        self._new = new_path
        self._sig = sig
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self._browser.rename(self._old, self._new)
            self._sig.rename_done.emit(self._old, self._new, True, "")
        except SftpError as exc:
            self._sig.rename_done.emit(self._old, self._new, False, str(exc))
        except Exception as exc:
            self._sig.rename_done.emit(self._old, self._new, False, f"{exc}")


# ── Formatting helpers ────────────────────────────────────────────────────

def _format_size(n: int) -> str:
    if n is None or n < 0:
        return ""
    if n < 1024:
        return f"{n} B"
    x = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        x /= 1024.0
        if x < 1024:
            return f"{x:,.1f} {unit}"
    return f"{x:,.1f} PB"


def _format_mtime(ts: float) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _guess_type(name: str, is_dir: bool) -> str:
    if is_dir:
        return "Folder"
    _, ext = os.path.splitext(name)
    if not ext:
        return "File"
    return ext.lstrip(".").upper() + " file"


def _join_remote(base: str, name: str) -> str:
    if not base:
        return "/" + name
    if base == "/":
        return "/" + name
    if base.endswith("/"):
        return base + name
    return base + "/" + name


def _parent_remote(path: str) -> str:
    if not path or path == "/":
        return "/"
    p = path.rstrip("/")
    idx = p.rfind("/")
    if idx <= 0:
        return "/"
    return p[:idx]


def _default_local_start() -> str:
    home = os.path.expanduser("~")
    return home if os.path.isdir(home) else (os.getcwd() or "/")


def _list_local(path: str) -> list[tuple[str, bool, int, float]]:
    """List a local directory — raises PermissionError / FileNotFoundError / OSError."""
    out: list[tuple[str, bool, int, float]] = []
    with os.scandir(path) as it:
        for ent in it:
            try:
                info = ent.stat(follow_symlinks=True)
            except OSError:
                out.append((ent.name, ent.is_dir(), 0, 0.0))
                continue
            out.append((
                ent.name,
                bool(stat.S_ISDIR(info.st_mode)),
                int(info.st_size or 0),
                float(info.st_mtime or 0.0),
            ))
    out.sort(key=lambda r: (not r[1], r[0].lower()))
    return out


# ── Custom numeric/date table items for sorting ───────────────────────────

class _NumItem(QTableWidgetItem):
    """Sort by underlying numeric value, display the formatted string."""
    def __init__(self, display: str, value: float):
        super().__init__(display)
        self.setData(Qt.ItemDataRole.UserRole, float(value))
        self.setTextAlignment(
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        )

    def __lt__(self, other) -> bool:
        try:
            a = float(self.data(Qt.ItemDataRole.UserRole) or 0.0)
            b = float(other.data(Qt.ItemDataRole.UserRole) or 0.0)
            return a < b
        except Exception:
            return super().__lt__(other)


class _NameItem(QTableWidgetItem):
    """Sort name column with directories always first, then case-insensitive name."""
    def __init__(self, display: str, is_dir: bool, name: str):
        super().__init__(display)
        self.setData(Qt.ItemDataRole.UserRole + 3, bool(is_dir))
        self.setData(Qt.ItemDataRole.UserRole + 4, name.lower())

    def __lt__(self, other) -> bool:
        try:
            a_dir = bool(self.data(Qt.ItemDataRole.UserRole + 3))
            b_dir = bool(other.data(Qt.ItemDataRole.UserRole + 3))
            if a_dir != b_dir:
                return a_dir  # dirs sort first ascending; Qt flips for desc
            return (self.data(Qt.ItemDataRole.UserRole + 4) or "") < (
                other.data(Qt.ItemDataRole.UserRole + 4) or ""
            )
        except Exception:
            return super().__lt__(other)


# ── Main view ─────────────────────────────────────────────────────────────

class FileTransferView(QWidget):
    """
    WinSCP-style dual-pane file manager page.

    Construction:
        FileTransferView(ssh_view)   # borrow the SSHView instance

    Public API (used by MainWindow):
        status_message   pyqtSignal(str)
        on_entered()     MainWindow calls this when the page becomes active
        shutdown()       release SFTP/SCP channels before Qt tears the widget down
    """

    status_message = pyqtSignal(str)

    _HEADERS = ["Name", "Size", "Type", "Modified"]
    _COL_NAME, _COL_SIZE, _COL_TYPE, _COL_MTIME = range(4)

    def __init__(self, ssh_view, parent=None):
        super().__init__(parent)
        self._ssh_view = ssh_view

        # Session binding + channels.
        self._bound_session_id: Optional[int] = None
        self._bound_tab = None
        # Browser is duck-typed: either SftpBrowser or ShellBrowser.
        # The latter is used on BusyBox/OpenWrt devices that don't
        # expose the SFTP subsystem, so remote browsing still works.
        self._browser: Optional[object] = None
        self._browser_kind: str = ""          # "sftp" / "shell" / ""
        self._scp_engine: Optional[ScpTransferEngine] = None

        # Browsing worker signals.
        self._browse_sig = _BrowseSignals()
        self._browse_sig.listdir_done.connect(self._on_remote_listdir_done)
        self._browse_sig.mkdir_done.connect(self._on_remote_mkdir_done)
        self._browse_sig.delete_done.connect(self._on_remote_delete_done)
        self._browse_sig.rename_done.connect(self._on_remote_rename_done)

        self._thread_pool = QThreadPool.globalInstance()

        # Sequential transfer queue.
        self._transfers = TransferManager(self)
        self._transfers.job_enqueued.connect(self._on_job_enqueued)
        self._transfers.job_started.connect(self._on_job_started)
        self._transfers.job_progress.connect(self._on_job_progress)
        self._transfers.job_finished.connect(self._on_job_finished)
        self._transfers.queue_changed.connect(self._rebuild_queue_table)

        # Browsing state.
        self._local_path = _default_local_start()
        self._remote_path = ""
        self._remote_loading = False
        self._remote_pending_path = ""
        self._remote_pending_push_history = True

        # Debounce for progress updates into the queue table.
        self._progress_last: dict[int, int] = {}

        # Queue collapse state — starts expanded so new users see the
        # queue exist and understand what the panel does. The flag is
        # session-local; no persistence needed.
        self._queue_collapsed: bool = False

        # Local pane collapse state. Persisted via utils.settings so
        # the last choice survives app restarts. When collapsed the
        # full local pane widget stays alive inside a QStackedWidget
        # so its path / selection / scroll position are preserved
        # across toggles without a reload.
        try:
            from utils import settings as _ft_settings
            self._local_collapsed: bool = bool(
                _ft_settings.get("file_transfer_local_collapsed", False)
            )
        except Exception:
            self._local_collapsed = False
        self._local_saved_sizes: list[int] = []

        # Deferred-open registry: when the user double-clicks a remote
        # file, we enqueue a DOWNLOAD_FILE job and record the full
        # pending-open metadata here. When the job finishes the
        # handler pops the entry and hands it to the editor launcher.
        # We keep the remote source alongside the local target so the
        # RemoteEditTracker can register the edit with the correct
        # remote origin after the editor launches.
        self._open_after_download: dict[int, _PendingOpen] = {}

        # Per-session temp cache for remote opens. Lazily created on
        # the first open; torn down in shutdown().
        self._tmp_cache_dir: Optional[str] = None

        # Per-SSH-session state bag. Each time the user switches
        # sessions, the current remote path + local path are saved
        # here, and any saved state for the incoming session is
        # restored. This is the "remember where I was" behaviour of
        # a mature dual-pane file manager — switching sessions and
        # coming back drops you back onto the folder you left.
        self._session_bags: dict[int, _SessionBag] = {}

        # Path history for each pane. Navigation via double-click,
        # path-bar submit, parent (↑) button all push the outgoing
        # path here so the Back / Forward buttons behave like a
        # browser. Max 50 entries — the cap lives in _NavHistory.
        self._local_nav = _NavHistory()
        self._remote_nav = _NavHistory()

        # Remote-edit watcher. Tracks temp files opened in the
        # external editor so save-edit cycles are caught and the
        # user can push changes back through the normal SCP queue.
        self._edit_tracker = RemoteEditTracker()
        # Upload jobs launched from the reupload bar are recorded
        # here so the finish handler can call mark_uploaded on the
        # tracker after success. Maps job id → temp path.
        self._pending_reuploads: dict[int, str] = {}
        # Entries currently displayed in the reupload bar. Lets us
        # avoid spamming the notification on every poll cycle when
        # the same set of files is already showing.
        self._reupload_visible: set[str] = set()
        # Polling timer — cheap (one stat() per tracked file) and
        # tuned low enough to feel "immediate" after a save without
        # hammering the disk.
        self._edit_poll_timer = QTimer(self)
        self._edit_poll_timer.setInterval(2500)
        self._edit_poll_timer.timeout.connect(self._poll_edit_changes)

        self._destroyed = False

        self._build_ui()
        self._wire_events()

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._on_theme_changed(theme())

        try:
            self._ssh_view.sessions_changed.connect(self._refresh_session_picker)
        except Exception:
            pass

        # Kick off initial state.
        self._in_local_path.setText(self._local_path)
        self._load_local(self._local_path, push_history=False)
        self._refresh_session_picker()

        # Start watching for editor saves. The timer is safe even
        # when the tracker is empty — check_for_changes returns
        # [] immediately.
        self._edit_poll_timer.start()

    # ─── UI build ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(self._build_header_row())
        # Reupload notification bar — hidden by default, appears
        # between the session header and the main toolbar when the
        # edit tracker detects that one or more temp files have
        # been saved since they were opened.
        root.addWidget(self._build_reupload_bar())
        root.addWidget(self._build_toolbar_row())

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(3)
        self._splitter.setChildrenCollapsible(False)

        # Local pane is hosted inside a QStackedWidget so collapse is
        # a page-swap — the full pane widget (with its path, table,
        # selection, scroll position) stays alive behind the scenes
        # while a thin rail takes its place in the splitter. Restore
        # is instant; nothing needs to be re-read from disk.
        self._local_stack = QStackedWidget()
        self._local_stack.setObjectName("ft_local_stack")
        self._local_full_pane = self._build_local_pane()
        self._local_rail = self._build_local_collapsed_rail()
        self._local_stack.addWidget(self._local_full_pane)   # index 0
        self._local_stack.addWidget(self._local_rail)        # index 1
        self._local_stack.setCurrentIndex(0)

        self._splitter.addWidget(self._local_stack)
        self._splitter.addWidget(self._build_remote_pane())
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([640, 640])
        root.addWidget(self._splitter, stretch=1)

        root.addWidget(self._build_queue_panel(), stretch=0)

        # Apply the persisted collapsed flag after the whole layout
        # exists so the initial splitter sizes are in place. We skip
        # the settings write because the value hasn't changed.
        if self._local_collapsed:
            self._local_collapsed = False  # _apply_local_collapse flips it
            self._apply_local_collapse(True, persist=False)

    def _build_header_row(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ft_header")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        lbl = QLabel("SSH SESSION")
        lbl.setObjectName("lbl_field_label")
        lay.addWidget(lbl)

        self._cb_sessions = QComboBox()
        self._cb_sessions.setMinimumWidth(300)
        self._cb_sessions.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        lay.addWidget(self._cb_sessions, stretch=1)

        self._btn_reopen = QPushButton("Reopen channel")
        self._btn_reopen.setObjectName("btn_action")
        self._btn_reopen.setToolTip(
            "Tear down the browsing (SFTP) channel and reopen it. "
            "The SCP engine picks up the same SSH transport automatically."
        )
        lay.addWidget(self._btn_reopen)

        self._lbl_state = QLabel("No SSH session")
        self._lbl_state.setObjectName("ft_sftp_state")
        lay.addWidget(self._lbl_state)
        return frame

    def _build_reupload_bar(self) -> QFrame:
        """
        Slim notification strip shown above the main toolbar when the
        edit tracker detects saved changes in an opened-for-edit
        temp file. Offers "Upload changes" / "Dismiss" actions.

        The bar starts hidden (``setVisible(False)``). When visible
        it takes ~34 px of vertical space; when hidden it collapses
        to zero height so the layout reflows without a gap.
        """
        bar = QFrame()
        bar.setObjectName("ft_reupload_bar")
        bar.setFixedHeight(34)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 4, 14, 4)
        lay.setSpacing(10)

        self._reupload_icon = QLabel("⟳")
        self._reupload_icon.setObjectName("ft_reupload_icon")
        lay.addWidget(self._reupload_icon)

        self._reupload_label = QLabel("")
        self._reupload_label.setObjectName("ft_reupload_label")
        self._reupload_label.setWordWrap(False)
        lay.addWidget(self._reupload_label, stretch=1)

        self._btn_reupload = QPushButton("Upload changes")
        self._btn_reupload.setObjectName("btn_primary")
        self._btn_reupload.clicked.connect(self._on_reupload_upload_clicked)
        lay.addWidget(self._btn_reupload)

        self._btn_reupload_dismiss = QPushButton("Dismiss")
        self._btn_reupload_dismiss.setObjectName("btn_action")
        self._btn_reupload_dismiss.clicked.connect(self._on_reupload_dismiss_clicked)
        lay.addWidget(self._btn_reupload_dismiss)

        bar.setVisible(False)
        self._reupload_bar = bar
        return bar

    def _build_toolbar_row(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ft_toolbar")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(4)

        def tb(text: str, tooltip: str) -> QToolButton:
            b = QToolButton()
            b.setObjectName("ft_tbtn")
            b.setText(text)
            b.setToolTip(tooltip)
            b.setAutoRaise(False)
            b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            b.setMinimumHeight(30)
            b.setMinimumWidth(36)
            return b

        self._btn_refresh  = tb("↻  Refresh",       "Refresh both panes  (F5)")
        self._btn_upload   = tb("▲  Upload",        "Upload selected local file(s) via SCP  (F6)")
        self._btn_download = tb("▼  Download",      "Download selected remote file(s) via SCP  (F6)")
        self._btn_upload_folder   = tb("▲  Upload folder",   "Recursively upload the selected local folder")
        self._btn_download_folder = tb("▼  Download folder", "Recursively download the selected remote folder")
        self._btn_mkdir    = tb("+  New folder",    "Create a new folder in the active pane  (F7)")
        self._btn_rename   = tb("✎  Rename",        "Rename the selected entry  (F2)")
        self._btn_delete   = tb("✕  Delete",        "Delete selected entries  (Del)")
        self._btn_copy_path = tb("≡  Copy path",    "Copy the selected entry path to the clipboard")
        self._btn_cancel_transfer = tb("■  Cancel transfer",
            "Cancel the currently-running transfer")

        for b in (
            self._btn_refresh, self._btn_upload, self._btn_download,
            self._btn_upload_folder, self._btn_download_folder,
            self._btn_mkdir, self._btn_rename, self._btn_delete,
            self._btn_copy_path, self._btn_cancel_transfer,
        ):
            lay.addWidget(b)

        lay.addStretch(1)

        self._lbl_active_pane = QLabel("Active pane: LOCAL")
        self._lbl_active_pane.setObjectName("ft_active_pane")
        lay.addWidget(self._lbl_active_pane)

        return frame

    def _build_local_pane(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ft_pane")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Title row: section label + collapse chevron on the right.
        # Putting the collapse control in the pane's own header keeps
        # it discoverable without crowding the workspace toolbar.
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title = QLabel("LOCAL")
        title.setObjectName("lbl_section")
        title_row.addWidget(title)
        title_row.addStretch(1)

        self._btn_local_collapse = QToolButton()
        self._btn_local_collapse.setObjectName("ft_local_collapse")
        self._btn_local_collapse.setText("‹")
        self._btn_local_collapse.setToolTip(
            "Collapse local pane — hide the local file browser and "
            "give the remote pane the full width. Local state (path, "
            "selection, scroll position) is preserved and restored "
            "when you expand."
        )
        self._btn_local_collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_local_collapse.setFixedSize(26, 22)
        self._btn_local_collapse.clicked.connect(self._on_local_collapse_toggle)
        title_row.addWidget(self._btn_local_collapse)

        lay.addLayout(title_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._btn_local_back = QToolButton()
        self._btn_local_back.setObjectName("btn_action")
        self._btn_local_back.setText("←")
        self._btn_local_back.setToolTip("Back  (go to previous local path)")
        self._btn_local_back.setEnabled(False)
        toolbar.addWidget(self._btn_local_back)

        self._btn_local_fwd = QToolButton()
        self._btn_local_fwd.setObjectName("btn_action")
        self._btn_local_fwd.setText("→")
        self._btn_local_fwd.setToolTip("Forward")
        self._btn_local_fwd.setEnabled(False)
        toolbar.addWidget(self._btn_local_fwd)

        self._btn_local_up = QToolButton()
        self._btn_local_up.setObjectName("btn_action")
        self._btn_local_up.setText("↑")
        self._btn_local_up.setToolTip("Parent directory  (Alt+↑)")
        toolbar.addWidget(self._btn_local_up)

        self._btn_local_home = QToolButton()
        self._btn_local_home.setObjectName("btn_action")
        self._btn_local_home.setText("⌂")
        self._btn_local_home.setToolTip("Home")
        toolbar.addWidget(self._btn_local_home)

        self._btn_local_refresh = QToolButton()
        self._btn_local_refresh.setObjectName("btn_action")
        self._btn_local_refresh.setText("↻")
        self._btn_local_refresh.setToolTip("Refresh")
        toolbar.addWidget(self._btn_local_refresh)

        self._in_local_path = QLineEdit()
        self._in_local_path.setPlaceholderText(r"C:\Users\…  or  /home/…")
        toolbar.addWidget(self._in_local_path, stretch=1)

        lay.addLayout(toolbar)

        self._tbl_local = QTableWidget()
        _configure_table(self._tbl_local, self._HEADERS)
        lay.addWidget(self._tbl_local, stretch=1)

        self._lbl_local_status = QLabel("")
        self._lbl_local_status.setObjectName("ft_pane_status")
        lay.addWidget(self._lbl_local_status)
        return frame

    def _build_local_collapsed_rail(self) -> QFrame:
        """
        Thin vertical strip shown in place of the local pane when the
        user collapses it. Holds a single full-height expand button
        and a rotated "LOCAL" label so the collapsed state reads
        clearly — the user always knows where the hidden pane went
        and how to bring it back.

        The rail widget is the alternate page of ``_local_stack``;
        QStackedWidget swaps it in on collapse and back out on
        expand.  Width is kept at 32 px so the remote pane reclaims
        essentially the full workspace width.
        """
        rail = QFrame()
        rail.setObjectName("ft_local_rail")
        rail.setFixedWidth(32)
        rail.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )

        rlay = QVBoxLayout(rail)
        rlay.setContentsMargins(2, 8, 2, 8)
        rlay.setSpacing(6)

        self._btn_local_expand = QToolButton()
        self._btn_local_expand.setObjectName("ft_local_expand")
        self._btn_local_expand.setText("›")
        self._btn_local_expand.setToolTip(
            "Expand local pane — restore the local file browser. "
            "The previous path, selection, and scroll position come "
            "back intact."
        )
        self._btn_local_expand.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_local_expand.setFixedSize(26, 26)
        self._btn_local_expand.clicked.connect(self._on_local_collapse_toggle)
        rlay.addWidget(
            self._btn_local_expand, 0, Qt.AlignmentFlag.AlignHCenter
        )

        # Vertical "LOCAL" label built from stacked single characters.
        # Using per-character QLabels avoids the QLabel-rotation-
        # via-paintEvent path and works cleanly with every Qt theme
        # without any custom styling hooks.
        for ch in "LOCAL":
            c = QLabel(ch)
            c.setObjectName("ft_local_rail_char")
            c.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            f = c.font()
            f.setBold(True)
            f.setPointSize(max(8, f.pointSize() - 1))
            c.setFont(f)
            rlay.addWidget(c, 0, Qt.AlignmentFlag.AlignHCenter)

        rlay.addStretch(1)
        return rail

    def _build_remote_pane(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ft_pane")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel("REMOTE")
        title.setObjectName("lbl_section")
        lay.addWidget(title)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self._btn_remote_back = QToolButton()
        self._btn_remote_back.setObjectName("btn_action")
        self._btn_remote_back.setText("←")
        self._btn_remote_back.setToolTip("Back  (go to previous remote path)")
        self._btn_remote_back.setEnabled(False)
        toolbar.addWidget(self._btn_remote_back)

        self._btn_remote_fwd = QToolButton()
        self._btn_remote_fwd.setObjectName("btn_action")
        self._btn_remote_fwd.setText("→")
        self._btn_remote_fwd.setToolTip("Forward")
        self._btn_remote_fwd.setEnabled(False)
        toolbar.addWidget(self._btn_remote_fwd)

        self._btn_remote_up = QToolButton()
        self._btn_remote_up.setObjectName("btn_action")
        self._btn_remote_up.setText("↑")
        self._btn_remote_up.setToolTip("Parent directory  (Alt+↑)")
        toolbar.addWidget(self._btn_remote_up)

        self._btn_remote_home = QToolButton()
        self._btn_remote_home.setObjectName("btn_action")
        self._btn_remote_home.setText("⌂")
        self._btn_remote_home.setToolTip("Remote home")
        toolbar.addWidget(self._btn_remote_home)

        self._btn_remote_refresh = QToolButton()
        self._btn_remote_refresh.setObjectName("btn_action")
        self._btn_remote_refresh.setText("↻")
        self._btn_remote_refresh.setToolTip("Refresh")
        toolbar.addWidget(self._btn_remote_refresh)

        self._in_remote_path = QLineEdit()
        self._in_remote_path.setPlaceholderText("/home/user")
        toolbar.addWidget(self._in_remote_path, stretch=1)

        lay.addLayout(toolbar)

        self._tbl_remote = QTableWidget()
        _configure_table(self._tbl_remote, self._HEADERS)
        lay.addWidget(self._tbl_remote, stretch=1)

        self._lbl_remote_status = QLabel("Not connected")
        self._lbl_remote_status.setObjectName("ft_pane_status")
        lay.addWidget(self._lbl_remote_status)
        return frame

    def _build_queue_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ft_queue_panel")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        # Collapse/expand toggle lives at the very left so the user's
        # eye catches it immediately. A plain arrow + short label
        # keeps it self-explanatory across every theme.
        self._btn_queue_collapse = QToolButton()
        self._btn_queue_collapse.setObjectName("ft_queue_collapse")
        self._btn_queue_collapse.setText("▼")
        self._btn_queue_collapse.setToolTip(
            "Collapse transfer queue — hide the table and free up "
            "vertical space for the file panes. Active transfers "
            "keep running."
        )
        self._btn_queue_collapse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_queue_collapse.setFixedWidth(28)
        header_row.addWidget(self._btn_queue_collapse)

        title = QLabel("TRANSFER QUEUE")
        title.setObjectName("lbl_section")
        header_row.addWidget(title)

        # Inline job-count so the user sees something useful even
        # when the table body is collapsed.
        self._lbl_queue_count = QLabel("no jobs")
        self._lbl_queue_count.setObjectName("ft_queue_count")
        header_row.addWidget(self._lbl_queue_count)

        header_row.addStretch(1)
        self._btn_clear_queue = QPushButton("Clear finished")
        self._btn_clear_queue.setObjectName("btn_action")
        header_row.addWidget(self._btn_clear_queue)
        lay.addLayout(header_row)

        self._tbl_queue = QTableWidget()
        self._tbl_queue.setColumnCount(6)
        self._tbl_queue.setHorizontalHeaderLabels(
            ["Op", "Name", "Source → Destination", "Size", "Progress", "Status"]
        )
        self._tbl_queue.verticalHeader().setVisible(False)
        self._tbl_queue.setShowGrid(False)
        self._tbl_queue.setAlternatingRowColors(True)
        self._tbl_queue.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._tbl_queue.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._tbl_queue.setFixedHeight(150)
        qhead = self._tbl_queue.horizontalHeader()
        qhead.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        qhead.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        qhead.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        qhead.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        qhead.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        qhead.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        qhead.resizeSection(4, 160)

        # Progress bars live as cell widgets indexed by row after every
        # rebuild_queue_table() call; _progress_bars maps job id → row.
        self._queue_row_for_job: dict[int, int] = {}
        lay.addWidget(self._tbl_queue)
        return frame

    def _wire_events(self) -> None:
        self._cb_sessions.currentIndexChanged.connect(self._on_session_picker_changed)
        self._btn_reopen.clicked.connect(self._on_reopen_sftp)

        # Toolbar
        self._btn_refresh.clicked.connect(self._refresh_both)
        self._btn_upload.clicked.connect(self._on_upload_clicked)
        self._btn_download.clicked.connect(self._on_download_clicked)
        self._btn_upload_folder.clicked.connect(self._on_upload_folder_clicked)
        self._btn_download_folder.clicked.connect(self._on_download_folder_clicked)
        self._btn_mkdir.clicked.connect(self._on_mkdir_clicked)
        self._btn_rename.clicked.connect(self._on_rename_clicked)
        self._btn_delete.clicked.connect(self._on_delete_clicked)
        self._btn_copy_path.clicked.connect(self._on_copy_path_clicked)
        self._btn_cancel_transfer.clicked.connect(self._transfers.cancel_current)
        self._btn_clear_queue.clicked.connect(self._transfers.clear_finished)
        self._btn_queue_collapse.clicked.connect(self._on_queue_collapse_toggle)

        # Local pane
        self._btn_local_back.clicked.connect(self._on_local_back)
        self._btn_local_fwd.clicked.connect(self._on_local_forward)
        self._btn_local_up.clicked.connect(self._on_local_up)
        self._btn_local_home.clicked.connect(self._on_local_home)
        self._btn_local_refresh.clicked.connect(
            lambda: self._load_local(self._local_path, push_history=False)
        )
        self._in_local_path.returnPressed.connect(
            lambda: self._load_local(self._in_local_path.text().strip())
        )
        self._tbl_local.itemDoubleClicked.connect(self._on_local_double_clicked)
        self._tbl_local.itemSelectionChanged.connect(self._on_local_selection_changed)
        self._tbl_local.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tbl_local.customContextMenuRequested.connect(self._on_local_context_menu)

        # Remote pane
        self._btn_remote_back.clicked.connect(self._on_remote_back)
        self._btn_remote_fwd.clicked.connect(self._on_remote_forward)
        self._btn_remote_up.clicked.connect(self._on_remote_up)
        self._btn_remote_home.clicked.connect(self._on_remote_home)
        self._btn_remote_refresh.clicked.connect(
            lambda: self._load_remote(self._remote_path or "~", push_history=False)
        )
        self._in_remote_path.returnPressed.connect(
            lambda: self._load_remote(self._in_remote_path.text().strip() or "~")
        )
        self._tbl_remote.itemDoubleClicked.connect(self._on_remote_double_clicked)
        self._tbl_remote.itemSelectionChanged.connect(self._on_remote_selection_changed)
        self._tbl_remote.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tbl_remote.customContextMenuRequested.connect(self._on_remote_context_menu)

        # Shortcuts
        sc_refresh = QShortcut(QKeySequence("F5"), self)
        sc_refresh.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_refresh.activated.connect(self._refresh_both)

        sc_delete = QShortcut(QKeySequence("Delete"), self)
        sc_delete.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_delete.activated.connect(self._on_delete_clicked)

        sc_rename = QShortcut(QKeySequence("F2"), self)
        sc_rename.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_rename.activated.connect(self._on_rename_clicked)

        sc_mkdir = QShortcut(QKeySequence("F7"), self)
        sc_mkdir.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_mkdir.activated.connect(self._on_mkdir_clicked)

        # F6 triggers upload when the local pane is active, download
        # when the remote pane is active — mirrors WinSCP's F5/F6
        # behaviour where the direction flips based on focus.
        sc_transfer = QShortcut(QKeySequence("F6"), self)
        sc_transfer.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_transfer.activated.connect(self._on_f6)

        sc_up = QShortcut(QKeySequence("Alt+Up"), self)
        sc_up.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_up.activated.connect(self._on_alt_up)

        self._update_action_buttons()

    # ─── Theme ───────────────────────────────────────────────────────────

    def _on_theme_changed(self, t) -> None:
        alt = _shade_alt(t)
        row_sheet = (
            f"QTableWidget {{"
            f"  background-color: {t.bg_raised};"
            f"  alternate-background-color: {alt};"
            f"  color: {t.text};"
            f"  gridline-color: {t.border};"
            f"  selection-background-color: {t.bg_select};"
            f"  selection-color: {t.text};"
            f"}}"
            f"QTableWidget::item {{ padding: 5px 10px; }}"
        )
        for tbl in (self._tbl_local, self._tbl_remote, self._tbl_queue):
            tbl.setStyleSheet(row_sheet)

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def on_entered(self) -> None:
        if self._destroyed:
            return
        self._refresh_session_picker()
        if not self._browser or not self._browser.is_open:
            self._bind_first_connected_if_possible()

    def shutdown(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        # Stop the edit-change poll timer *before* tearing down the
        # tracker so a stray tick can't fire into a half-dead view.
        try:
            if self._edit_poll_timer is not None:
                self._edit_poll_timer.stop()
        except Exception:
            pass
        try:
            self._edit_tracker.clear()
        except Exception:
            pass
        try:
            self._transfers.shutdown()
        except Exception:
            pass
        self._close_browser()
        # Clean up the session-scoped temp cache used for remote
        # file opens. ignore_errors=True because an editor still
        # holding a file handle on Windows would otherwise throw
        # inside shutil.rmtree and interfere with app shutdown —
        # the OS will reclaim %TEMP% entries eventually either way.
        cache = self._tmp_cache_dir
        self._tmp_cache_dir = None
        if cache:
            try:
                shutil.rmtree(cache, ignore_errors=True)
            except Exception:
                pass

    # ─── Session picker & binding ────────────────────────────────────────

    def _refresh_session_picker(self) -> None:
        if self._destroyed:
            return
        try:
            sessions = self._ssh_view.list_live_sessions() if self._ssh_view else []
        except Exception:
            sessions = []

        previous = self._bound_session_id

        self._cb_sessions.blockSignals(True)
        try:
            self._cb_sessions.clear()
            if not sessions:
                self._cb_sessions.addItem("— no SSH sessions open —", None)
                self._cb_sessions.setEnabled(False)
                self._lbl_state.setText("No SSH session")
                self._close_browser()
                self._set_connected_state(False)
                return
            self._cb_sessions.setEnabled(True)
            for s in sessions:
                label = f"{s['title']} · {s['label']}  [{s['state']}]"
                self._cb_sessions.addItem(label, s["id"])
            if previous is not None:
                for i in range(self._cb_sessions.count()):
                    if self._cb_sessions.itemData(i) == previous:
                        self._cb_sessions.setCurrentIndex(i)
                        break
        finally:
            self._cb_sessions.blockSignals(False)

        if self._bound_session_id is None:
            self._bind_first_connected_if_possible()
        else:
            still_alive = False
            for s in sessions:
                if s["id"] == self._bound_session_id:
                    still_alive = (s["state"] == "connected")
                    break
            if not still_alive:
                # Save the outgoing session's paths + purge any
                # in-flight edit tracker entries for this session
                # so a future reconnect of the same tab lands
                # cleanly without stale reupload prompts.
                if self._bound_session_id is not None:
                    self._save_session_bag(self._bound_session_id)
                    try:
                        self._edit_tracker.remove_session(
                            self._bound_session_id
                        )
                    except Exception:
                        pass
                    self._hide_reupload_bar()
                self._close_browser()
                self._lbl_state.setText("Session disconnected")
                self._set_connected_state(False)
                self._bound_session_id = None
                self._bound_tab = None
                self._bind_first_connected_if_possible()

    def _bind_first_connected_if_possible(self) -> None:
        try:
            sessions = self._ssh_view.list_live_sessions() if self._ssh_view else []
        except Exception:
            sessions = []
        for s in sessions:
            if s["state"] == "connected":
                self._bind_session(s["tab"], s["id"])
                for i in range(self._cb_sessions.count()):
                    if self._cb_sessions.itemData(i) == s["id"]:
                        self._cb_sessions.blockSignals(True)
                        self._cb_sessions.setCurrentIndex(i)
                        self._cb_sessions.blockSignals(False)
                        break
                return

    def _on_session_picker_changed(self, idx: int) -> None:
        if idx < 0:
            return
        data = self._cb_sessions.itemData(idx)
        if data is None:
            return
        try:
            sessions = self._ssh_view.list_live_sessions() if self._ssh_view else []
        except Exception:
            sessions = []
        for s in sessions:
            if s["id"] == data:
                if s["state"] != "connected":
                    self._close_browser()
                    self._bound_session_id = data
                    self._bound_tab = s["tab"]
                    self._set_connected_state(False)
                    self._lbl_state.setText(f"Waiting · {s['state']}")
                    return
                self._bind_session(s["tab"], data)
                return

    def _bind_session(self, tab, sid: int) -> None:
        """
        Bind the File Transfer view to a live SSH tab.

        The SCP engine is always attached — SCP has no dependency
        on SFTP. Browsing is attempted in two tiers:

          1. SFTP subsystem (fast, structured).
          2. Shell-over-exec fallback (works on BusyBox / OpenWrt
             and any other remote that disables SFTP).

        The remote pane stays enabled in both cases. The only time
        we fall back to a transfer-only experience is if **both**
        the SFTP probe and the shell probe fail — e.g. the remote
        is live but refuses exec entirely.

        Per-session state (last remote path, last local path) is
        saved before we switch away from the current session and
        restored when the target session is picked again, giving
        the File Transfer view the same "context per tab" feel as
        WinSCP's multi-session workspace.
        """
        # Save the outgoing session's current paths so coming back
        # lands on the same folder pair.
        if self._bound_session_id is not None and self._bound_session_id != sid:
            self._save_session_bag(self._bound_session_id)

        self._close_browser()
        self._bound_session_id = sid
        self._bound_tab = tab

        session = getattr(tab, "_session", None)
        if session is None or not getattr(session, "is_open", False):
            self._lbl_state.setText("Session not connected")
            self._set_connected_state(False)
            return

        # SCP is the transfer engine in every configuration.
        self._scp_engine = ScpTransferEngine(session)
        self._transfers.bind_engine(self._scp_engine)

        try:
            summary = tab.summary_text()
        except Exception:
            summary = "connected"

        # Tier 1: SFTP.
        sftp_browser = SftpBrowser(session)
        sftp_err = ""
        try:
            sftp_browser.open()
        except SftpError as exc:
            sftp_err = str(exc)
            try:
                sftp_browser.close()
            except Exception:
                pass
            sftp_browser = None
        except Exception as exc:
            sftp_err = f"{exc}"
            try:
                sftp_browser.close()
            except Exception:
                pass
            sftp_browser = None

        if sftp_browser is not None:
            self._browser = sftp_browser
            self._browser_kind = "sftp"
            self._lbl_state.setText(f"SCP transfers · SFTP browsing · {summary}")
            self.status_message.emit(f"Remote ready (SFTP browser) · {summary}")
            self._set_connected_state(True)
            # Restore the remembered remote path for this session,
            # falling back to the remote home.
            bag = self._session_bags.get(sid)
            restore_path = bag.remote_path if (bag and bag.remote_path) else "~"
            self._load_remote(restore_path, push_history=False)
            if bag and bag.local_path and os.path.isdir(bag.local_path):
                self._load_local(bag.local_path, push_history=False)
            return

        # Tier 2: shell fallback. This is the key difference from the
        # previous behaviour — we NEVER leave the remote pane disabled
        # just because SFTP is missing.
        shell_browser = ShellBrowser(session)
        try:
            shell_browser.open()
        except SftpError as exc:
            # Neither SFTP nor shell exec works — fall back to a
            # transfer-only mode with a clear explanation.
            try:
                shell_browser.close()
            except Exception:
                pass
            self._browser = None
            self._browser_kind = ""
            self._set_connected_state(False, sftp_ok=False)
            self._lbl_state.setText(
                f"SCP ready (no browsing: SFTP {sftp_err or 'unavailable'}; "
                f"shell {exc})"
            )
            self.status_message.emit(
                "SCP ready — remote browsing unavailable on this host"
            )
            self._tbl_remote.setRowCount(0)
            self._lbl_remote_status.setText(
                "Remote browsing unavailable on this host. "
                "Type a remote path in the toolbar to target transfers."
            )
            return

        self._browser = shell_browser
        self._browser_kind = "shell"
        self._lbl_state.setText(
            f"SCP transfers · SSH-shell browsing · {summary}"
        )
        self.status_message.emit(
            f"Remote ready (shell browser, no SFTP on this host) · {summary}"
        )
        self._set_connected_state(True)
        bag = self._session_bags.get(sid)
        restore_path = bag.remote_path if (bag and bag.remote_path) else "~"
        self._load_remote(restore_path, push_history=False)
        if bag and bag.local_path and os.path.isdir(bag.local_path):
            self._load_local(bag.local_path, push_history=False)

    def _save_session_bag(self, sid: int) -> None:
        """Snapshot the current local+remote paths into the session's bag."""
        bag = self._session_bags.setdefault(sid, _SessionBag())
        bag.remote_path = self._remote_path or ""
        bag.local_path = self._local_path or ""

    def _on_reopen_sftp(self) -> None:
        tab = self._bound_tab
        sid = self._bound_session_id
        if tab is None or sid is None:
            return
        self._bind_session(tab, sid)

    def _close_browser(self) -> None:
        b = self._browser
        self._browser = None
        self._browser_kind = ""
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
        self._scp_engine = None
        try:
            self._transfers.bind_engine(None)
        except Exception:
            pass
        self._tbl_remote.setRowCount(0)
        self._lbl_remote_status.setText("Not connected")
        self._set_connected_state(False)

    def _set_connected_state(self, on: bool, *, sftp_ok: bool = True) -> None:
        self._btn_reopen.setEnabled(self._bound_tab is not None)
        # Remote pane widgets — browsing needs SFTP, so they follow
        # ``on`` AND ``sftp_ok``.
        browsing_enabled = on and sftp_ok
        for w in (
            self._btn_remote_up, self._btn_remote_home,
            self._btn_remote_refresh, self._tbl_remote,
        ):
            w.setEnabled(browsing_enabled)
        # Remote path field stays editable when SCP is available but
        # SFTP is not — the user can still type the target directory.
        self._in_remote_path.setEnabled(on)
        self._update_action_buttons()

    # ─── Local pane ─────────────────────────────────────────────────────

    def _load_local(self, path: str, *, push_history: bool = True) -> None:
        if self._destroyed:
            return
        path = os.path.abspath(os.path.expanduser(path or _default_local_start()))
        self._lbl_local_status.setText("Loading…")
        try:
            entries = _list_local(path)
        except FileNotFoundError:
            self._lbl_local_status.setText(f"Not found: {path}")
            self.status_message.emit(f"Local path not found: {path}")
            return
        except PermissionError:
            self._lbl_local_status.setText(f"Permission denied: {path}")
            self.status_message.emit(f"Local permission denied: {path}")
            return
        except OSError as exc:
            self._lbl_local_status.setText(f"{exc}")
            return

        if push_history and self._local_path and self._local_path != path:
            self._local_nav.push(self._local_path)

        self._local_path = path
        self._in_local_path.setText(path)
        self._update_nav_buttons()

        self._populate_table(
            self._tbl_local,
            [
                (name, is_dir, size, mtime, os.path.join(path, name))
                for (name, is_dir, size, mtime) in entries
            ],
            is_remote=False,
        )
        self._lbl_local_status.setText(
            f"{len(entries)} item{'s' if len(entries) != 1 else ''}"
        )
        self._update_action_buttons()

    def _on_local_up(self) -> None:
        parent = os.path.dirname(self._local_path.rstrip("\\/"))
        if parent and parent != self._local_path:
            self._load_local(parent)

    def _on_local_home(self) -> None:
        self._load_local(_default_local_start())

    def _on_local_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        path = self._row_path(self._tbl_local, row)
        is_dir = self._is_dir_row(self._tbl_local, row)
        if not path:
            return
        if is_dir:
            self._load_local(path)
            return
        # Local file: open directly against the real on-disk path.
        # No temp copy, no download, no staging — the editor sees the
        # same bytes the user sees in the file pane. Launcher is
        # fire-and-forget so a slow editor never blocks the Qt loop.
        try:
            tool = _launch_editor(path)
        except EditorError as exc:
            self._warn("Open file", str(exc))
            return
        except Exception as exc:
            self._warn("Open file", f"Could not launch editor: {exc}")
            return
        self.status_message.emit(
            f"Opened local file {os.path.basename(path)} in {tool}"
        )

    def _on_local_selection_changed(self) -> None:
        self._lbl_active_pane.setText("Active pane: LOCAL")
        self._update_action_buttons()

    def _on_local_context_menu(self, pos) -> None:
        menu = QMenu(self)

        rows = self._selected_rows(self._tbl_local)
        single_file_row = None
        if len(rows) == 1 and not self._is_dir_row(self._tbl_local, rows[0]):
            single_file_row = rows[0]

        a_open = menu.addAction("Open in local editor")
        a_open.setEnabled(single_file_row is not None)
        menu.addSeparator()

        a_refresh = menu.addAction("Refresh  (F5)")
        a_up = menu.addAction("Parent directory  (Alt+↑)")
        menu.addSeparator()
        a_upload = menu.addAction("Upload selected  (F6)")
        a_upload.setEnabled(self._btn_upload.isEnabled())
        a_upload_tree = menu.addAction("Upload folder (recursive)")
        a_upload_tree.setEnabled(self._btn_upload_folder.isEnabled())
        menu.addSeparator()
        a_rename = menu.addAction("Rename  (F2)")
        a_rename.setEnabled(False)  # local rename not implemented
        a_rename.setToolTip("Local rename not supported in this MVP.")
        a_delete = menu.addAction("Delete  (Del)")
        a_delete.setEnabled(False)
        a_delete.setToolTip("Local delete is disabled for safety.")
        menu.addSeparator()
        a_copy = menu.addAction("Copy path")

        chosen = menu.exec(self._tbl_local.viewport().mapToGlobal(pos))
        if chosen == a_open and single_file_row is not None:
            path = self._row_path(self._tbl_local, single_file_row)
            if path:
                try:
                    tool = _launch_editor(path)
                except EditorError as exc:
                    self._warn("Open file", str(exc))
                except Exception as exc:
                    self._warn("Open file", f"Could not launch editor: {exc}")
                else:
                    self.status_message.emit(
                        f"Opened {os.path.basename(path)} in {tool}"
                    )
            return
        if chosen == a_refresh:
            self._load_local(self._local_path)
        elif chosen == a_up:
            self._on_local_up()
        elif chosen == a_upload:
            self._on_upload_clicked()
        elif chosen == a_upload_tree:
            self._on_upload_folder_clicked()
        elif chosen == a_copy:
            self._on_copy_path_clicked()

    # ─── Remote pane ────────────────────────────────────────────────────

    def _load_remote(self, path: str, *, push_history: bool = True) -> None:
        if self._destroyed:
            return
        if self._browser is None or not self._browser.is_open:
            self._lbl_remote_status.setText("Not connected")
            return
        if self._remote_loading:
            self._remote_pending_path = path
            self._remote_pending_push_history = push_history
            return
        self._remote_loading = True
        self._remote_pending_path = path
        self._remote_pending_push_history = push_history
        self._lbl_remote_status.setText("Loading…")
        self._thread_pool.start(_ListDirJob(self._browser, path, self._browse_sig))

    @pyqtSlot(str, object, str)
    def _on_remote_listdir_done(self, path: str, entries, error: str) -> None:
        if self._destroyed:
            return
        self._remote_loading = False
        if error:
            self._lbl_remote_status.setText(error)
            self.status_message.emit(f"Remote listdir failed: {error}")
            return
        if entries is None:
            self._lbl_remote_status.setText("Unknown error")
            return

        if (
            self._remote_pending_push_history
            and self._remote_path
            and self._remote_path != path
        ):
            self._remote_nav.push(self._remote_path)
        self._remote_pending_push_history = True

        self._remote_path = path
        self._in_remote_path.setText(path)
        self._update_nav_buttons()

        rows = []
        for e in entries:
            assert isinstance(e, SftpEntry)
            rows.append((
                e.name,
                e.is_dir,
                0 if e.is_dir else e.size,
                e.mtime,
                _join_remote(path, e.name),
            ))
        self._populate_table(self._tbl_remote, rows, is_remote=True)
        self._lbl_remote_status.setText(
            f"{len(entries)} item{'s' if len(entries) != 1 else ''}"
        )
        self._update_action_buttons()

        if self._remote_pending_path and self._remote_pending_path != path:
            target = self._remote_pending_path
            self._remote_pending_path = ""
            self._load_remote(target)
        else:
            self._remote_pending_path = ""

    def _on_remote_up(self) -> None:
        self._load_remote(_parent_remote(self._remote_path or "/"))

    def _on_remote_home(self) -> None:
        self._load_remote("~")

    def _on_remote_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        path = self._row_path(self._tbl_remote, row)
        is_dir = self._is_dir_row(self._tbl_remote, row)
        if not path:
            return
        if is_dir:
            self._load_remote(path)
            return
        # File: stream it into the session temp cache and hand it to
        # the editor launcher once the SCP download completes. The
        # enqueue is non-blocking; the GUI stays responsive.
        size = self._row_size(self._tbl_remote, row)
        self._open_remote_file(path, size)

    def _on_remote_selection_changed(self) -> None:
        self._lbl_active_pane.setText("Active pane: REMOTE")
        self._update_action_buttons()

    def _on_remote_context_menu(self, pos) -> None:
        menu = QMenu(self)

        rows = self._selected_rows(self._tbl_remote)
        single_file_row = None
        if len(rows) == 1 and not self._is_dir_row(self._tbl_remote, rows[0]):
            single_file_row = rows[0]

        a_open = menu.addAction("Open (download + local editor)")
        a_open.setEnabled(
            single_file_row is not None and self._scp_engine is not None
        )
        menu.addSeparator()

        a_refresh = menu.addAction("Refresh  (F5)")
        a_up = menu.addAction("Parent directory  (Alt+↑)")
        menu.addSeparator()
        a_download = menu.addAction("Download selected  (F6)")
        a_download.setEnabled(self._btn_download.isEnabled())
        a_download_tree = menu.addAction("Download folder (recursive)")
        a_download_tree.setEnabled(self._btn_download_folder.isEnabled())
        menu.addSeparator()
        a_mkdir = menu.addAction("New folder…  (F7)")
        a_mkdir.setEnabled(self._btn_mkdir.isEnabled())
        a_rename = menu.addAction("Rename…  (F2)")
        a_rename.setEnabled(self._btn_rename.isEnabled())
        a_delete = menu.addAction("Delete  (Del)")
        a_delete.setEnabled(self._btn_delete.isEnabled())
        menu.addSeparator()
        a_copy = menu.addAction("Copy path")

        chosen = menu.exec(self._tbl_remote.viewport().mapToGlobal(pos))
        if chosen == a_open and single_file_row is not None:
            path = self._row_path(self._tbl_remote, single_file_row)
            size = self._row_size(self._tbl_remote, single_file_row)
            self._open_remote_file(path, size)
            return
        if chosen == a_refresh:
            self._load_remote(self._remote_path or "~")
        elif chosen == a_up:
            self._on_remote_up()
        elif chosen == a_download:
            self._on_download_clicked()
        elif chosen == a_download_tree:
            self._on_download_folder_clicked()
        elif chosen == a_mkdir:
            self._on_mkdir_clicked()
        elif chosen == a_rename:
            self._on_rename_clicked()
        elif chosen == a_delete:
            self._on_delete_clicked()
        elif chosen == a_copy:
            self._on_copy_path_clicked()

    # ─── Toolbar actions ────────────────────────────────────────────────

    def _active_pane(self) -> str:
        """Return 'local' or 'remote' based on the last interacted-with table."""
        if self._tbl_remote.hasFocus():
            return "remote"
        if self._tbl_local.hasFocus():
            return "local"
        # Fall back to whichever label is showing.
        return "remote" if "REMOTE" in self._lbl_active_pane.text() else "local"

    def _refresh_both(self) -> None:
        if self._local_path:
            self._load_local(self._local_path)
        if self._browser and self._browser.is_open:
            self._load_remote(self._remote_path or "~")

    def _on_upload_clicked(self) -> None:
        if not self._scp_engine:
            return
        remote_dir = self._remote_path_for_upload()
        if not remote_dir:
            self._warn(
                "Remote directory unknown",
                "Pick a remote directory first, or type one in the "
                "remote path bar and press Enter.",
            )
            return
        paths = [
            self._row_path(self._tbl_local, r)
            for r in self._selected_rows(self._tbl_local)
            if not self._is_dir_row(self._tbl_local, r)
        ]
        paths = [p for p in paths if p]
        if not paths:
            return
        for lp in paths:
            if self._browser and self._browser.is_open:
                target = _join_remote(remote_dir, os.path.basename(lp))
                if self._remote_exists_blocking(target):
                    if not self._confirm_overwrite(target, remote=True):
                        continue
            try:
                size = os.path.getsize(lp)
            except OSError:
                size = 0
            self._transfers.enqueue(
                JobKind.UPLOAD_FILE,
                source=lp,
                destination=remote_dir,
                display_name=f"▲ {os.path.basename(lp)}",
                size_hint=size,
            )

    def _on_download_clicked(self) -> None:
        if not self._scp_engine:
            return
        local_dir = self._local_path or _default_local_start()
        paths = [
            self._row_path(self._tbl_remote, r)
            for r in self._selected_rows(self._tbl_remote)
            if not self._is_dir_row(self._tbl_remote, r)
        ]
        paths = [p for p in paths if p]
        if not paths:
            return
        for rp in paths:
            local_target = os.path.join(local_dir, os.path.basename(rp))
            if os.path.exists(local_target):
                if not self._confirm_overwrite(local_target, remote=False):
                    continue
            size = self._remote_size(rp)
            self._transfers.enqueue(
                JobKind.DOWNLOAD_FILE,
                source=rp,
                destination=local_dir,
                display_name=f"▼ {os.path.basename(rp)}",
                size_hint=size,
            )

    def _on_upload_folder_clicked(self) -> None:
        if not self._scp_engine:
            return
        remote_dir = self._remote_path_for_upload()
        if not remote_dir:
            self._warn("Remote directory unknown", "Pick or type a remote target directory.")
            return
        rows = [
            self._row_path(self._tbl_local, r)
            for r in self._selected_rows(self._tbl_local)
            if self._is_dir_row(self._tbl_local, r)
        ]
        rows = [p for p in rows if p]
        if not rows:
            return
        for lp in rows:
            target = _join_remote(remote_dir, os.path.basename(lp.rstrip("\\/")))
            if self._browser and self._browser.is_open and self._remote_exists_blocking(target):
                if not self._confirm_overwrite(target, remote=True, is_dir=True):
                    continue
            self._transfers.enqueue(
                JobKind.UPLOAD_TREE,
                source=lp,
                destination=remote_dir,
                display_name=f"▲ {os.path.basename(lp.rstrip(chr(92)+'/'))} (folder)",
                size_hint=0,
            )

    def _on_download_folder_clicked(self) -> None:
        if not self._scp_engine:
            return
        local_dir = self._local_path or _default_local_start()
        rows = [
            self._row_path(self._tbl_remote, r)
            for r in self._selected_rows(self._tbl_remote)
            if self._is_dir_row(self._tbl_remote, r)
        ]
        rows = [p for p in rows if p]
        if not rows:
            return
        for rp in rows:
            leaf = rp.rstrip("/").rsplit("/", 1)[-1] or rp
            target = os.path.join(local_dir, leaf)
            if os.path.exists(target):
                if not self._confirm_overwrite(target, remote=False, is_dir=True):
                    continue
            self._transfers.enqueue(
                JobKind.DOWNLOAD_TREE,
                source=rp,
                destination=local_dir,
                display_name=f"▼ {leaf} (folder)",
                size_hint=0,
            )

    def _on_mkdir_clicked(self) -> None:
        pane = self._active_pane()
        if pane == "local":
            self._mkdir_local()
        else:
            self._mkdir_remote()

    def _mkdir_local(self) -> None:
        if not self._local_path:
            return
        name, ok = QInputDialog.getText(
            self, "New folder", f"Create folder in:\n{self._local_path}"
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        full = os.path.join(self._local_path, name)
        try:
            os.makedirs(full, exist_ok=False)
        except FileExistsError:
            self._warn("Already exists", f"'{name}' already exists here.")
            return
        except OSError as exc:
            self._warn("Create folder", str(exc))
            return
        self.status_message.emit(f"Created local folder: {name}")
        self._load_local(self._local_path)

    def _mkdir_remote(self) -> None:
        if not self._browser or not self._browser.is_open:
            return
        if not self._remote_path:
            self._warn("Remote directory unknown", "Open a remote directory first.")
            return
        name, ok = QInputDialog.getText(
            self, "New folder", f"Create folder in:\n{self._remote_path}"
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        target = _join_remote(self._remote_path, name)
        self._thread_pool.start(_MkdirJob(self._browser, target, self._browse_sig))

    @pyqtSlot(str, bool, str)
    def _on_remote_mkdir_done(self, path: str, ok: bool, error: str) -> None:
        if self._destroyed:
            return
        if ok:
            self.status_message.emit(f"Created remote folder: {path}")
            self._load_remote(self._remote_path or "~")
        else:
            self._warn("Create folder", error or "Unknown error")

    def _on_rename_clicked(self) -> None:
        if self._active_pane() != "remote":
            self._warn("Rename", "Local rename is not supported in this MVP.")
            return
        if not self._browser or not self._browser.is_open:
            return
        rows = self._selected_rows(self._tbl_remote)
        if len(rows) != 1:
            self._warn("Rename", "Select exactly one remote entry.")
            return
        row = rows[0]
        old_path = self._row_path(self._tbl_remote, row)
        if not old_path:
            return
        old_name = old_path.rstrip("/").rsplit("/", 1)[-1]
        new_name, ok = QInputDialog.getText(
            self, "Rename", f"New name for:\n{old_path}", text=old_name
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        parent = _parent_remote(old_path)
        new_path = _join_remote(parent, new_name)
        self._thread_pool.start(
            _RenameJob(self._browser, old_path, new_path, self._browse_sig)
        )

    @pyqtSlot(str, str, bool, str)
    def _on_remote_rename_done(self, old: str, new: str, ok: bool, error: str) -> None:
        if self._destroyed:
            return
        if ok:
            self.status_message.emit(f"Renamed: {old} → {new}")
            self._load_remote(self._remote_path or "~")
        else:
            self._warn("Rename failed", error or "Unknown error")

    def _on_delete_clicked(self) -> None:
        if self._active_pane() != "remote":
            self._warn("Delete", "Local delete is disabled for safety in this MVP.")
            return
        if not self._browser or not self._browser.is_open:
            return
        rows = self._selected_rows(self._tbl_remote)
        if not rows:
            return
        paths = [
            (self._row_path(self._tbl_remote, r), self._is_dir_row(self._tbl_remote, r))
            for r in rows
        ]
        paths = [(p, d) for (p, d) in paths if p]
        if not paths:
            return
        preview = "\n".join(p for (p, _) in paths[:6])
        more = "" if len(paths) <= 6 else f"\n… and {len(paths) - 6} more"
        res = QMessageBox.question(
            self, "Delete remote entries",
            f"Delete {len(paths)} remote entr{'y' if len(paths)==1 else 'ies'}?\n\n{preview}{more}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if res != QMessageBox.StandardButton.Yes:
            return
        for path, is_dir in paths:
            self._thread_pool.start(
                _DeleteJob(self._browser, path, is_dir, self._browse_sig)
            )

    @pyqtSlot(str, bool, str)
    def _on_remote_delete_done(self, path: str, ok: bool, error: str) -> None:
        if self._destroyed:
            return
        if ok:
            self.status_message.emit(f"Deleted: {path}")
            self._load_remote(self._remote_path or "~")
        else:
            self._warn("Delete failed", f"{path}\n\n{error}")

    def _on_copy_path_clicked(self) -> None:
        if self._active_pane() == "remote":
            rows = self._selected_rows(self._tbl_remote)
            paths = [self._row_path(self._tbl_remote, r) for r in rows]
        else:
            rows = self._selected_rows(self._tbl_local)
            paths = [self._row_path(self._tbl_local, r) for r in rows]
        paths = [p for p in paths if p]
        if not paths:
            return
        QApplication.clipboard().setText("\n".join(paths))
        self.status_message.emit(
            f"Copied {len(paths)} path{'s' if len(paths) != 1 else ''} to clipboard"
        )

    # ─── Remote file "Open" flow (download-to-temp, then launch) ────────

    _OPEN_WARN_THRESHOLD_BYTES = 20 * 1024 * 1024   # 20 MB

    def _open_remote_file(self, remote_path: str, size_hint: int) -> None:
        """
        Download ``remote_path`` into a fresh per-open temp directory
        and, once the SCP transfer finishes, open it in the user's
        preferred local editor.

        The download is placed inside a unique temp subdirectory so
        two concurrent opens of files with the same name (e.g. two
        ``config`` files from different remote paths) never clash.

        Large files prompt a confirmation first: SCP will happily
        pull a 4 GB log file into a temp cache, but we'd rather make
        that deliberate than let the user accidentally freeze their
        editor.
        """
        if self._scp_engine is None:
            self._warn(
                "Open remote file",
                "Connect to an SSH session first — SCP is required "
                "to fetch the remote file into a local temp cache.",
            )
            return
        if not remote_path:
            return

        # Large-file guard. If we don't know the size (size_hint == 0,
        # e.g. shell browser failed to stat) we don't warn — erring
        # on the side of honouring the user's action.
        if size_hint and size_hint > self._OPEN_WARN_THRESHOLD_BYTES:
            friendly = _format_size(size_hint)
            res = QMessageBox.question(
                self,
                "Open large remote file?",
                f"{os.path.basename(remote_path)} is {friendly}.\n\n"
                f"Opening will download the whole file into a local "
                f"temporary cache and then launch an editor. "
                f"Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if res != QMessageBox.StandardButton.Yes:
                return

        try:
            subdir = self._ensure_open_subdir()
        except OSError as exc:
            self._warn("Open remote file", f"Could not create temp cache: {exc}")
            return

        local_target = os.path.join(subdir, os.path.basename(remote_path))
        jid = self._transfers.enqueue(
            JobKind.DOWNLOAD_FILE,
            source=remote_path,
            destination=subdir,
            display_name=f"▼ open · {os.path.basename(remote_path)}",
            size_hint=size_hint,
        )
        self._open_after_download[jid] = _PendingOpen(
            local_target=local_target,
            remote_source=remote_path,
            session_id=int(self._bound_session_id or 0),
        )
        # Make the remote-staging step explicit — this is the only
        # place in the view where an open triggers an SCP download,
        # and we want the status line to say so clearly.
        self.status_message.emit(
            f"Staging remote file {os.path.basename(remote_path)} "
            f"for open (SCP download to local temp cache)…"
        )

    def _ensure_open_subdir(self) -> str:
        """
        Create (if needed) a session-scoped temp directory and return
        a fresh unique subdir inside it for a single open.
        """
        if self._tmp_cache_dir is None or not os.path.isdir(self._tmp_cache_dir):
            self._tmp_cache_dir = tempfile.mkdtemp(prefix="netengine-ft-")
        return tempfile.mkdtemp(dir=self._tmp_cache_dir)

    def _on_f6(self) -> None:
        if self._active_pane() == "local":
            self._on_upload_clicked()
        else:
            self._on_download_clicked()

    def _on_alt_up(self) -> None:
        if self._active_pane() == "remote":
            self._on_remote_up()
        else:
            self._on_local_up()

    # ─── Path history (back / forward) ──────────────────────────────────

    def _on_local_back(self) -> None:
        target = self._local_nav.go_back(self._local_path)
        if target:
            self._load_local(target, push_history=False)

    def _on_local_forward(self) -> None:
        target = self._local_nav.go_forward(self._local_path)
        if target:
            self._load_local(target, push_history=False)

    def _on_remote_back(self) -> None:
        target = self._remote_nav.go_back(self._remote_path)
        if target:
            self._load_remote(target, push_history=False)

    def _on_remote_forward(self) -> None:
        target = self._remote_nav.go_forward(self._remote_path)
        if target:
            self._load_remote(target, push_history=False)

    def _update_nav_buttons(self) -> None:
        """Sync the back/forward button enabled state to the history stacks."""
        if self._destroyed:
            return
        try:
            self._btn_local_back.setEnabled(self._local_nav.can_back())
            self._btn_local_fwd.setEnabled(self._local_nav.can_forward())
            self._btn_remote_back.setEnabled(self._remote_nav.can_back())
            self._btn_remote_fwd.setEnabled(self._remote_nav.can_forward())
        except RuntimeError:
            pass

    # ─── Remote edit tracking — reupload flow ───────────────────────────

    def _poll_edit_changes(self) -> None:
        """
        Periodic scan driven by ``_edit_poll_timer``. Walks every
        registered TrackedEdit and surfaces a notification bar if
        any of them have been saved by the external editor since
        the last ack.
        """
        if self._destroyed:
            return
        try:
            changed = self._edit_tracker.check_for_changes()
        except Exception:
            return
        if not changed:
            return
        # Build the new visible set; no-op if it matches what the
        # bar is already showing.
        new_set = {e.temp_path for e in changed}
        if new_set == self._reupload_visible:
            return
        self._reupload_visible = new_set
        self._show_reupload_bar(changed)

    def _show_reupload_bar(self, entries: list[TrackedEdit]) -> None:
        if self._destroyed or not entries:
            return
        names = [e.basename for e in entries[:3]]
        more = "" if len(entries) <= 3 else f" (+{len(entries) - 3} more)"
        label = (
            f"{len(entries)} remote file{'s' if len(entries) != 1 else ''} "
            f"changed on disk: {', '.join(names)}{more} — upload back "
            f"via SCP?"
        )
        self._reupload_label.setText(label)
        self._reupload_bar.setVisible(True)
        self.status_message.emit(
            f"Editor save detected — {len(entries)} file(s) ready to reupload"
        )

    def _hide_reupload_bar(self) -> None:
        self._reupload_visible.clear()
        try:
            self._reupload_bar.setVisible(False)
        except RuntimeError:
            pass

    def _on_reupload_upload_clicked(self) -> None:
        """Queue SCP upload jobs for every currently-listed tracked entry."""
        if self._destroyed or self._scp_engine is None:
            self._warn(
                "Reupload",
                "No SCP engine bound — pick a connected SSH session first.",
            )
            return
        entries = [
            self._edit_tracker.get(tp) for tp in sorted(self._reupload_visible)
        ]
        entries = [e for e in entries if e is not None]
        if not entries:
            self._hide_reupload_bar()
            return
        for entry in entries:
            # Destination for SCP is the **parent directory** on the
            # remote side — the engine's put_file takes a remote
            # directory and derives the filename from the local
            # basename, which matches the original remote name.
            remote_dir = _parent_remote(entry.remote_path)
            try:
                size = os.path.getsize(entry.temp_path)
            except OSError:
                size = 0
            jid = self._transfers.enqueue(
                JobKind.UPLOAD_FILE,
                source=entry.temp_path,
                destination=remote_dir,
                display_name=f"⟳ reupload · {entry.basename}",
                size_hint=size,
            )
            self._pending_reuploads[jid] = entry.temp_path
        self._hide_reupload_bar()

    def _on_reupload_dismiss_clicked(self) -> None:
        """User chose to ignore this batch of changes — suppress until next save."""
        for tp in list(self._reupload_visible):
            self._edit_tracker.acknowledge(tp)
        self._hide_reupload_bar()

    def _register_tracked_edit(self, pending: _PendingOpen) -> None:
        """Hand a successfully-opened edit to the tracker."""
        if not pending:
            return
        try:
            self._edit_tracker.add(
                pending.local_target,
                pending.remote_source,
                pending.session_id,
            )
        except Exception:
            pass

    # ─── Helper: overwrite / exists checks ──────────────────────────────

    def _remote_path_for_upload(self) -> str:
        """Pick the remote directory to upload into."""
        if self._remote_path:
            return self._remote_path
        typed = self._in_remote_path.text().strip()
        return typed

    def _remote_exists_blocking(self, path: str) -> bool:
        """Quick synchronous exists() against SFTP — safe for small checks."""
        if not self._browser or not self._browser.is_open:
            return False
        try:
            return self._browser.exists(path)
        except SftpError:
            return False

    def _remote_size(self, path: str) -> int:
        if not self._browser or not self._browser.is_open:
            return 0
        try:
            entry = self._browser.stat_entry(path)
            return entry.size if entry else 0
        except SftpError:
            return 0

    def _confirm_overwrite(
        self,
        path: str,
        *,
        remote: bool,
        is_dir: bool = False,
    ) -> bool:
        kind = "folder" if is_dir else "file"
        side = "remote" if remote else "local"
        box = QMessageBox(self)
        box.setWindowTitle("Overwrite?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"The {side} {kind} already exists:\n\n{path}\n\n"
            f"Overwrite with the incoming copy?"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    # ─── Table population ────────────────────────────────────────────────

    def _populate_table(
        self,
        tbl: QTableWidget,
        rows: list[tuple[str, bool, int, float, str]],
        *,
        is_remote: bool,
    ) -> None:
        """rows = list of (name, is_dir, size, mtime, path)."""
        was_sorting = tbl.isSortingEnabled()
        tbl.setSortingEnabled(False)
        tbl.setRowCount(len(rows))
        for row, (name, is_dir, size, mtime, path) in enumerate(rows):
            prefix = "📁 " if is_dir else "📄 "
            name_item = _NameItem(prefix + name, is_dir, name)
            name_item.setData(Qt.ItemDataRole.UserRole + 1, path)
            name_item.setData(Qt.ItemDataRole.UserRole + 2, is_dir)
            tbl.setItem(row, self._COL_NAME, name_item)

            size_disp = "" if is_dir else _format_size(size)
            tbl.setItem(row, self._COL_SIZE, _NumItem(size_disp, 0.0 if is_dir else float(size)))

            tbl.setItem(row, self._COL_TYPE, QTableWidgetItem(_guess_type(name, is_dir)))

            mtime_item = _NumItem(_format_mtime(mtime), float(mtime or 0))
            mtime_item.setTextAlignment(
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
            tbl.setItem(row, self._COL_MTIME, mtime_item)
        tbl.setSortingEnabled(was_sorting or True)

    # ─── Row helpers ────────────────────────────────────────────────────

    def _selected_rows(self, tbl: QTableWidget) -> list[int]:
        return sorted({idx.row() for idx in tbl.selectionModel().selectedRows()})

    def _is_dir_row(self, tbl: QTableWidget, row: int) -> bool:
        item = tbl.item(row, 0)
        if item is None:
            return False
        return bool(item.data(Qt.ItemDataRole.UserRole + 2))

    def _row_path(self, tbl: QTableWidget, row: int) -> str:
        item = tbl.item(row, 0)
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole + 1) or "")

    def _row_size(self, tbl: QTableWidget, row: int) -> int:
        item = tbl.item(row, self._COL_SIZE)
        if item is None:
            return 0
        try:
            return int(float(item.data(Qt.ItemDataRole.UserRole) or 0))
        except (TypeError, ValueError):
            return 0

    def _update_action_buttons(self) -> None:
        has_engine = self._scp_engine is not None
        has_browser = bool(self._browser and self._browser.is_open)

        local_rows = self._selected_rows(self._tbl_local)
        remote_rows = self._selected_rows(self._tbl_remote)

        has_local_file = any(not self._is_dir_row(self._tbl_local, r) for r in local_rows)
        has_local_dir  = any(self._is_dir_row(self._tbl_local, r) for r in local_rows)
        has_remote_file = any(not self._is_dir_row(self._tbl_remote, r) for r in remote_rows)
        has_remote_dir  = any(self._is_dir_row(self._tbl_remote, r) for r in remote_rows)

        # Uploads need the SCP engine and a target path (either the
        # browsed remote_path or a typed-in one).
        target_known = bool(
            self._remote_path or self._in_remote_path.text().strip()
        )
        self._btn_upload.setEnabled(has_engine and target_known and has_local_file)
        self._btn_upload_folder.setEnabled(has_engine and target_known and has_local_dir)
        self._btn_download.setEnabled(has_engine and has_browser and has_remote_file)
        self._btn_download_folder.setEnabled(has_engine and has_browser and has_remote_dir)

        self._btn_mkdir.setEnabled(
            (self._active_pane() == "local" and bool(self._local_path))
            or (self._active_pane() == "remote" and has_browser)
        )
        self._btn_rename.setEnabled(has_browser and len(remote_rows) == 1)
        self._btn_delete.setEnabled(has_browser and bool(remote_rows))
        self._btn_copy_path.setEnabled(bool(local_rows or remote_rows))

    # ─── Transfer queue panel ───────────────────────────────────────────

    @pyqtSlot(int)
    def _on_job_enqueued(self, job_id: int) -> None:
        self._rebuild_queue_table()

    @pyqtSlot(int)
    def _on_job_started(self, job_id: int) -> None:
        self._rebuild_queue_table()
        self.status_message.emit("Transfer started")

    @pyqtSlot(int, int, int, str)
    def _on_job_progress(self, job_id: int, done: int, total: int, current: str) -> None:
        # Debounce to at most ~10 updates/sec per job.
        now = int(time.monotonic() * 1000)
        last = self._progress_last.get(job_id, 0)
        if now - last < 100 and done < total:
            return
        self._progress_last[job_id] = now

        row = self._queue_row_for_job.get(job_id)
        if row is None:
            return
        bar = self._tbl_queue.cellWidget(row, 4)
        if isinstance(bar, QProgressBar):
            if total > 0:
                bar.setRange(0, 100)
                pct = min(100, int(100 * done / total))
                bar.setValue(pct)
                bar.setFormat(f"{pct}%  {current or ''}".strip())
            else:
                bar.setRange(0, 0)  # indeterminate
                bar.setFormat(current or "…")

    @pyqtSlot(int, str, str)
    def _on_job_finished(self, job_id: int, status: str, message: str) -> None:
        self._rebuild_queue_table()

        # Deferred open — if this job was started by the "open remote
        # file" flow, pull the pending-open metadata out of the
        # registry regardless of status so we never leak entries.
        pending_open = self._open_after_download.pop(job_id, None)

        # Reupload job — if this upload was started from the reupload
        # bar, pop the temp-path entry so the tracker bookkeeping
        # runs on the success branch below.
        reupload_temp = self._pending_reuploads.pop(job_id, None)

        if status == "done":
            self.status_message.emit("Transfer complete")
            job = self._transfers.get_job(job_id)
            if pending_open:
                # A remote-open transfer finished successfully. Launch
                # the editor on the staged file and register the edit
                # with the tracker so a later save triggers the
                # reupload flow. Do NOT refresh the local pane — the
                # temp cache isn't the user's browsed directory.
                self._launch_pending_open(pending_open)
            elif reupload_temp:
                # Successful reupload → update the tracker's ack/
                # uploaded mtimes so the next save triggers a fresh
                # prompt. Don't refresh the remote pane; the user
                # did not explicitly navigate, and refreshing would
                # disturb the active selection.
                self._edit_tracker.mark_uploaded(reupload_temp)
                self.status_message.emit(
                    f"Reuploaded {os.path.basename(reupload_temp)} "
                    f"back to remote"
                )
            elif job is not None:
                if job.kind in (JobKind.UPLOAD_FILE, JobKind.UPLOAD_TREE):
                    if self._browser and self._browser.is_open:
                        self._load_remote(
                            self._remote_path or "~",
                            push_history=False,
                        )
                else:
                    self._load_local(
                        self._local_path or _default_local_start(),
                        push_history=False,
                    )
        elif status == "cancelled":
            self.status_message.emit("Transfer cancelled")
            if pending_open:
                self.status_message.emit(
                    f"Open cancelled · {os.path.basename(pending_open.local_target)}"
                )
        else:
            self.status_message.emit(f"Transfer failed: {message}")
            if pending_open:
                self._warn(
                    "Open remote file",
                    f"Download of "
                    f"{os.path.basename(pending_open.local_target)} "
                    f"failed:\n\n{message}",
                )
            elif reupload_temp:
                self._warn(
                    "Reupload failed",
                    f"Re-upload of {os.path.basename(reupload_temp)} "
                    f"failed:\n\n{message}",
                )

    def _launch_pending_open(self, pending: _PendingOpen) -> None:
        """Hand a successfully-downloaded temp file to the editor launcher."""
        local_path = pending.local_target
        if not os.path.isfile(local_path):
            self._warn(
                "Open remote file",
                f"Download reported success but the staged file is "
                f"missing:\n\n{local_path}",
            )
            return
        try:
            tool = _launch_editor(local_path)
        except EditorError as exc:
            self._warn("Open remote file", str(exc))
            return
        except Exception as exc:
            self._warn("Open remote file", f"Could not launch editor: {exc}")
            return
        # Register with the edit tracker so a later save triggers the
        # reupload prompt. This is what turns the "open remote file"
        # flow into a WinSCP-style edit-and-save round trip.
        self._register_tracked_edit(pending)
        self.status_message.emit(
            f"Opened remote file {os.path.basename(local_path)} "
            f"in {tool} (editing tracked — save to trigger reupload)"
        )

    def _on_local_collapse_toggle(self) -> None:
        """
        Toggle the local pane between its full view and a thin
        collapsed rail. Wrapper that just flips the current state
        and delegates to ``_apply_local_collapse`` so the initial
        load path (applied from persisted settings) can reuse the
        same code without a redundant settings write.
        """
        self._apply_local_collapse(not self._local_collapsed, persist=True)

    def _apply_local_collapse(self, collapse: bool, *, persist: bool) -> None:
        """
        Switch the local pane between full and collapsed-rail pages.

        The full local pane widget is never destroyed — QStackedWidget
        keeps it alive on a background page, so path, selection,
        scroll position, and every in-flight browse worker survive
        the toggle intact.

        Splitter sizes are snapshotted on collapse and restored on
        expand so the user's preferred horizontal split is preserved
        across toggles.
        """
        if self._destroyed:
            return
        if collapse == self._local_collapsed:
            return

        RAIL_WIDTH = 32

        if collapse:
            # Snapshot the current splitter sizes so expanding later
            # puts the local pane back where the user had it. If the
            # sizes ever come back as [0, 0] (layout not yet resolved
            # — for example during a deferred apply at startup) we
            # fall back to the default 50/50 split.
            sizes = self._splitter.sizes()
            if len(sizes) == 2 and all(s > 0 for s in sizes):
                self._local_saved_sizes = list(sizes)
            self._local_stack.setCurrentIndex(1)
            # Hard-pin the stack width. QStackedWidget's minimum
            # width is the **max** across all its pages, so the
            # hidden full pane would otherwise keep the stack at
            # ~200 px even on the rail page. Locking minimum AND
            # maximum to the rail's width forces the splitter to
            # hand every spare pixel to the remote side.
            self._local_stack.setMinimumWidth(RAIL_WIDTH)
            self._local_stack.setMaximumWidth(RAIL_WIDTH)
            total = sum(self._splitter.sizes()) or (
                self._splitter.width() or 1280
            )
            remote_w = max(100, total - RAIL_WIDTH)
            self._splitter.setSizes([RAIL_WIDTH, remote_w])
            self._local_collapsed = True
        else:
            self._local_stack.setCurrentIndex(0)
            # Release the width lock applied during collapse so the
            # stack can grow back to its natural minimum.
            self._local_stack.setMinimumWidth(0)
            self._local_stack.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX
            if self._local_saved_sizes:
                # Scale the saved sizes to the current total width
                # so a window resize while collapsed doesn't cause
                # the split to jump. QSplitter does its own scaling
                # but a pre-scale keeps the ratio exact.
                total_now = sum(self._splitter.sizes()) or (
                    self._splitter.width() or 1280
                )
                total_saved = sum(self._local_saved_sizes) or total_now
                if total_saved != total_now and total_saved > 0:
                    factor = total_now / total_saved
                    scaled = [max(1, int(s * factor)) for s in self._local_saved_sizes]
                    self._splitter.setSizes(scaled)
                else:
                    self._splitter.setSizes(self._local_saved_sizes)
            self._local_collapsed = False

        if persist:
            try:
                from utils import settings as _s
                _s.set_value("file_transfer_local_collapsed", self._local_collapsed)
            except Exception:
                pass

        self.status_message.emit(
            "Local pane collapsed" if self._local_collapsed
            else "Local pane expanded"
        )

    def _on_queue_collapse_toggle(self) -> None:
        """
        Toggle the queue panel between collapsed (header row only)
        and expanded (full table visible).

        Collapsing does **not** touch the transfer manager — active
        jobs keep running on the worker thread and the job registry
        stays intact, so when the user expands the panel again they
        see the current live state with no data loss. The file
        panes automatically grow into the vacated vertical space
        because the splitter above this panel has stretch=1 and
        this panel has stretch=0.
        """
        if self._destroyed:
            return
        self._queue_collapsed = not self._queue_collapsed
        self._tbl_queue.setVisible(not self._queue_collapsed)
        if self._queue_collapsed:
            self._btn_queue_collapse.setText("▲")
            self._btn_queue_collapse.setToolTip(
                "Expand transfer queue — show the full job table "
                "with progress bars and statuses."
            )
        else:
            self._btn_queue_collapse.setText("▼")
            self._btn_queue_collapse.setToolTip(
                "Collapse transfer queue — hide the table and free "
                "up vertical space for the file panes. Active "
                "transfers keep running."
            )

    def _rebuild_queue_table(self) -> None:
        if self._destroyed:
            return
        jobs = self._transfers.list_jobs()
        self._update_queue_count_label(jobs)
        self._tbl_queue.setRowCount(len(jobs))
        self._queue_row_for_job.clear()
        for row, job in enumerate(jobs):
            self._queue_row_for_job[job.id] = row

            op_map = {
                JobKind.UPLOAD_FILE:   "SCP ▲",
                JobKind.UPLOAD_TREE:   "SCP ▲▲",
                JobKind.DOWNLOAD_FILE: "SCP ▼",
                JobKind.DOWNLOAD_TREE: "SCP ▼▼",
            }
            self._tbl_queue.setItem(row, 0, QTableWidgetItem(op_map.get(job.kind, "SCP")))
            self._tbl_queue.setItem(row, 1, QTableWidgetItem(job.display_name or ""))
            src_dst = f"{job.source}  →  {job.destination}"
            self._tbl_queue.setItem(row, 2, QTableWidgetItem(src_dst))

            size_text = _format_size(job.size_hint) if job.size_hint else ""
            self._tbl_queue.setItem(row, 3, QTableWidgetItem(size_text))

            bar = QProgressBar()
            bar.setObjectName("scp_progress")
            bar.setTextVisible(True)
            if job.status == JobStatus.RUNNING:
                if job.bytes_total > 0:
                    bar.setRange(0, 100)
                    bar.setValue(
                        min(100, int(100 * job.bytes_done / max(1, job.bytes_total)))
                    )
                    bar.setFormat(f"{int(100 * job.bytes_done / max(1, job.bytes_total))}%")
                else:
                    bar.setRange(0, 0)
                    bar.setFormat("…")
            elif job.status == JobStatus.DONE:
                bar.setRange(0, 100)
                bar.setValue(100)
                bar.setFormat("done")
            elif job.status == JobStatus.FAILED:
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setFormat("failed")
            elif job.status == JobStatus.CANCELLED:
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setFormat("cancelled")
            else:
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setFormat("queued")
            self._tbl_queue.setCellWidget(row, 4, bar)

            status_text = job.status.value.upper()
            if job.message and job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                status_text += f" · {job.message}"
            self._tbl_queue.setItem(row, 5, QTableWidgetItem(status_text))

    # ─── Small utilities ─────────────────────────────────────────────────

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _update_queue_count_label(self, jobs: list) -> None:
        """Keep the inline queue summary fresh so it's meaningful even when collapsed."""
        if self._destroyed or not hasattr(self, "_lbl_queue_count"):
            return
        if not jobs:
            self._lbl_queue_count.setText("no jobs")
            return
        running = sum(1 for j in jobs if j.status == JobStatus.RUNNING)
        queued = sum(1 for j in jobs if j.status == JobStatus.QUEUED)
        done = sum(1 for j in jobs if j.status == JobStatus.DONE)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        cancelled = sum(1 for j in jobs if j.status == JobStatus.CANCELLED)
        parts = []
        if running:
            parts.append(f"{running} running")
        if queued:
            parts.append(f"{queued} queued")
        if done:
            parts.append(f"{done} done")
        if failed:
            parts.append(f"{failed} failed")
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        self._lbl_queue_count.setText(" · ".join(parts) or f"{len(jobs)} jobs")


# ── Table config ──────────────────────────────────────────────────────────

def _configure_table(tbl: QTableWidget, headers: list[str]) -> None:
    tbl.setColumnCount(len(headers))
    tbl.setHorizontalHeaderLabels(headers)
    tbl.verticalHeader().setVisible(False)
    tbl.setShowGrid(False)
    tbl.setAlternatingRowColors(True)
    tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tbl.setWordWrap(False)
    tbl.setSortingEnabled(True)
    header = tbl.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(False)
    header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
    tbl.verticalHeader().setDefaultSectionSize(26)


def _shade_alt(t) -> str:
    """Compute a subtle alt-row colour that works across dark and light themes."""
    if getattr(t, "is_dark", True):
        return t.bg_raised if t.bg_raised != t.bg_base else t.bg_hover
    return t.bg_base
