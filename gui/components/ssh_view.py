"""
SSH Sessions workspace.

Layout
------
    ┌────────────────────────────────────────────────────────────────────┐
    │  ┌──── LEFT (scrollable) ────┐  ┌──── RIGHT (workspace) ────────┐ │
    │  │ Title                     │  │ Quick connect bar             │ │
    │  │ Saved sessions group      │  │ ─────────────────────────────│ │
    │  │   ▸ search                │  │ QTabWidget                    │ │
    │  │   ▸ list                  │  │   • SshSessionTab             │ │
    │  │   ▸ actions               │  │   • SshSessionTab             │ │
    │  │ Connection details        │  │   • …                         │ │
    │  │   (CollapsibleSection)    │  │                               │ │
    │  │ Status block              │  │                               │ │
    │  └───────────────────────────┘  └───────────────────────────────┘ │
    └────────────────────────────────────────────────────────────────────┘

Behaviour
---------
* The connection-details section is a `CollapsibleSection` so the user
  can hide it after they're connected and reclaim vertical space.
* Connecting always opens a NEW tab in the right-side workspace —
  multiple sessions can run in parallel.
* Each tab is an independent `SshSessionTab` with its own SSH session
  + terminal. Closing a tab tears down only that session.
* The quick connect bar accepts `[user@]host[:port]` and spawns a
  fresh tab without touching the form.
* The scanner page integrates via two entry points:
    `prefill_host(ip)`         → fills the form (no auto-tab)
    `connect_with_profile(d)`  → spawns a new tab immediately
* Saved sessions live in `utils.settings`. They support pin/favorite,
  last-connected timestamp, and a search filter.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QSize, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit, QSpinBox, QFileDialog, QMessageBox,
    QGroupBox, QSplitter, QTabWidget, QFrame, QSizePolicy, QLayout,
    QScrollArea, QInputDialog, QCheckBox, QTabBar, QStackedWidget,
    QToolButton,
)
from PyQt6.QtGui import QIcon, QColor

from gui.components.collapsible import CollapsibleSection
from gui.components.live_widgets import StatusDot
from gui.components.ssh_session_tab import (
    SshSessionTab,
    STATE_CONNECTING, STATE_CONNECTED, STATE_FAILED, STATE_CLOSED, STATE_IDLE,
)
from gui.themes import theme, ThemeManager
from scanner.ssh_client import SSHProfile, HAS_PARAMIKO
from utils import settings


class SSHView(QWidget):
    """Multi-session SSH workspace page."""

    status_message = pyqtSignal(str)

    # Layout constants for the connection form (centralised so every
    # row reads the same numbers).
    _ROW_HEIGHT     = 40
    _LABEL_COL_W    = 60
    _ROW_VSPACING   = 14
    _GROUP_PAD_X    = 18
    _GROUP_PAD_TOP  = 16
    _GROUP_PAD_BOT  = 16
    _ACTION_BTN_H   = 48

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def __init__(self, parent=None):
        super().__init__(parent)
        # Manager collapsed state — when True the SSH workspace gets the
        # full width and a thin expand rail is shown on the left.
        self._manager_collapsed = False
        self._build_ui()
        self._reload_sessions()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        if not HAS_PARAMIKO:
            self._show_paramiko_warning()

    # ── Build root ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(0)

        # Left side is a stacked container — full manager panel or a
        # slim collapse rail. A QStackedWidget inside the splitter keeps
        # the layout clean without leaving empty gaps when collapsed.
        self._left_stack = QStackedWidget()
        self._left_stack.setObjectName("ssh_left_stack")

        self._full_panel = self._build_left_panel()
        self._collapsed_rail = self._build_collapsed_rail()

        self._left_stack.addWidget(self._full_panel)     # idx 0
        self._left_stack.addWidget(self._collapsed_rail) # idx 1
        self._left_stack.setCurrentIndex(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setHandleWidth(2)
        self._splitter.setChildrenCollapsible(False)

        self._splitter.addWidget(self._left_stack)
        self._splitter.addWidget(self._build_right_panel())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([360, 900])

        root.addWidget(self._splitter)

    # ── Left panel (sessions list + form) ────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        # The whole left panel scrolls so the form / saved list always
        # stay accessible at any window height.
        scroll = QScrollArea()
        scroll.setObjectName("ssh_left_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumWidth(340)
        scroll.setMaximumWidth(520)

        container = QWidget()
        container.setObjectName("ssh_left_container")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 4, 18, 4)
        lay.setSpacing(14)

        # ── Manager header: section title + collapse toggle ──────────────
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        title = QLabel("CONNECTION MANAGER")
        title.setObjectName("lbl_section")
        header_row.addWidget(title)
        header_row.addStretch(1)

        self._btn_collapse_panel = QToolButton()
        self._btn_collapse_panel.setObjectName("ssh_panel_toggle")
        self._btn_collapse_panel.setText("⟨")
        self._btn_collapse_panel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_collapse_panel.setToolTip(
            "Collapse manager — open full SSH workspace"
        )
        self._btn_collapse_panel.setFixedSize(28, 24)
        self._btn_collapse_panel.clicked.connect(
            lambda: self.set_manager_collapsed(True)
        )
        header_row.addWidget(self._btn_collapse_panel)

        lay.addLayout(header_row)

        # Sessions group + form group
        lay.addWidget(self._build_saved_sessions_group())
        lay.addWidget(self._build_connection_section())
        lay.addWidget(self._build_status_block())
        lay.addStretch(1)

        scroll.setWidget(container)
        return scroll

    # ----- Collapsed rail ----------------------------------------------------

    def _build_collapsed_rail(self) -> QWidget:
        """Slim vertical strip shown when the manager is collapsed."""
        rail = QFrame()
        rail.setObjectName("ssh_collapse_rail")
        rail.setFixedWidth(44)
        rail.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(rail)
        lay.setContentsMargins(6, 16, 6, 16)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._btn_expand_panel = QToolButton()
        self._btn_expand_panel.setObjectName("ssh_panel_toggle")
        self._btn_expand_panel.setText("⟩")
        self._btn_expand_panel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_expand_panel.setToolTip("Show connection manager")
        self._btn_expand_panel.setFixedSize(30, 30)
        self._btn_expand_panel.clicked.connect(
            lambda: self.set_manager_collapsed(False)
        )
        lay.addWidget(
            self._btn_expand_panel, 0, Qt.AlignmentFlag.AlignHCenter
        )

        # Vertical "SSH" label for visual identity when collapsed.
        self._rail_label = QLabel("SSH")
        self._rail_label.setObjectName("ssh_rail_label")
        self._rail_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6)
        lay.addWidget(self._rail_label)

        lay.addStretch(1)

        return rail

    # ----- Panel collapse API ------------------------------------------------

    def set_manager_collapsed(self, collapsed: bool) -> None:
        """Toggle the connection-manager panel between full and rail modes."""
        if collapsed == self._manager_collapsed:
            return
        self._manager_collapsed = collapsed

        total = sum(self._splitter.sizes()) or self.width() or 1200
        if collapsed:
            self._left_stack.setCurrentIndex(1)
            # Force the stacked container to the rail width; it will
            # remain slim while the right panel expands to the rest.
            self._left_stack.setMinimumWidth(44)
            self._left_stack.setMaximumWidth(44)
            self._splitter.setSizes([44, max(total - 44, 600)])
            self.status_message.emit("SSH workspace expanded")
        else:
            self._left_stack.setCurrentIndex(0)
            self._left_stack.setMinimumWidth(340)
            self._left_stack.setMaximumWidth(520)
            # Clamp to a sensible range of the total width so narrow
            # laptop windows still give the terminal enough room.
            target = min(max(340, int(total * 0.32)), 460)
            self._splitter.setSizes([target, max(total - target, 480)])
            self.status_message.emit("Connection manager restored")

    def toggle_manager(self) -> None:
        self.set_manager_collapsed(not self._manager_collapsed)

    # ----- Saved sessions ----------------------------------------------------

    def _build_saved_sessions_group(self) -> QGroupBox:
        box = QGroupBox("SAVED SESSIONS")
        box.setObjectName("ssh_sessions_group")
        gl = QVBoxLayout(box)
        gl.setContentsMargins(self._GROUP_PAD_X, 24, self._GROUP_PAD_X, 14)
        gl.setSpacing(10)

        # Search field
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter saved sessions…")
        self._search.setMinimumHeight(32)
        self._search.textChanged.connect(self._on_filter_changed)
        gl.addWidget(self._search)

        # List
        self._sessions_list = QListWidget()
        self._sessions_list.setMinimumHeight(150)
        self._sessions_list.setMaximumHeight(220)
        self._sessions_list.itemSelectionChanged.connect(self._on_session_selected)
        self._sessions_list.itemDoubleClicked.connect(self._on_session_activated)
        gl.addWidget(self._sessions_list)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self._btn_new = QPushButton("New")
        self._btn_new.setObjectName("btn_action")
        self._btn_new.setMinimumHeight(30)
        self._btn_new.clicked.connect(self._on_new_session)
        btn_row.addWidget(self._btn_new)

        self._btn_save = QPushButton("Save")
        self._btn_save.setObjectName("btn_action")
        self._btn_save.setMinimumHeight(30)
        self._btn_save.clicked.connect(self._on_save_session)
        btn_row.addWidget(self._btn_save)

        self._btn_pin = QPushButton("Pin")
        self._btn_pin.setObjectName("btn_action")
        self._btn_pin.setMinimumHeight(30)
        self._btn_pin.clicked.connect(self._on_toggle_pin)
        btn_row.addWidget(self._btn_pin)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setObjectName("btn_danger")
        self._btn_delete.setMinimumHeight(30)
        self._btn_delete.clicked.connect(self._on_delete_session)
        btn_row.addWidget(self._btn_delete)

        btn_row.addStretch(1)
        gl.addLayout(btn_row)

        return box

    # ----- Connection details (collapsible) ---------------------------------

    def _build_connection_section(self) -> CollapsibleSection:
        section = CollapsibleSection("CONNECTION DETAILS")
        self._conn_section = section

        body = QVBoxLayout()
        body.setContentsMargins(
            self._GROUP_PAD_X, self._GROUP_PAD_TOP,
            self._GROUP_PAD_X, self._GROUP_PAD_BOT,
        )
        body.setSpacing(self._ROW_VSPACING)
        body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        # Build inputs
        self._in_name = self._make_line_edit("Friendly name (optional)")

        self._in_host = self._make_line_edit("hostname or IP")

        self._in_port = QSpinBox()
        self._in_port.setRange(1, 65535)
        self._in_port.setValue(22)
        self._in_port.setFixedWidth(120)
        self._in_port.setMinimumHeight(self._ROW_HEIGHT)
        self._in_port.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        self._in_user = self._make_line_edit("username")

        self._in_pass = self._make_line_edit("password (or use key)")
        self._in_pass.setEchoMode(QLineEdit.EchoMode.Password)

        self._in_key = self._make_line_edit("path to private key (optional)")
        self._btn_browse = QPushButton("Browse")
        self._btn_browse.setObjectName("btn_action")
        self._btn_browse.setFixedHeight(self._ROW_HEIGHT)
        self._btn_browse.setFixedWidth(82)
        self._btn_browse.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_browse.clicked.connect(self._on_browse_key)

        # Port row sub-layout
        port_field = QHBoxLayout()
        port_field.setContentsMargins(0, 0, 0, 0)
        port_field.setSpacing(0)
        port_field.addWidget(self._in_port, 0)
        port_field.addStretch(1)

        # Key row sub-layout
        key_field = QHBoxLayout()
        key_field.setContentsMargins(0, 0, 0, 0)
        key_field.setSpacing(8)
        key_field.addWidget(self._in_key, 1)
        key_field.addWidget(self._btn_browse, 0)

        body.addLayout(self._make_form_row("Name", self._in_name))
        body.addLayout(self._make_form_row("Host", self._in_host))
        body.addLayout(self._make_form_row("Port", port_field))
        body.addLayout(self._make_form_row("User", self._in_user))
        body.addLayout(self._make_form_row("Pass", self._in_pass))
        body.addLayout(self._make_form_row("Key",  key_field))

        # "Save credentials" toggle (off by default for safety)
        self._chk_save_creds = QCheckBox("Remember password (this profile only)")
        self._chk_save_creds.setChecked(False)
        body.addSpacing(4)
        body.addWidget(self._chk_save_creds)

        body.addSpacing(8)
        body.addLayout(self._build_action_row())

        section.set_content_layout(body)
        return section

    def _build_action_row(self) -> QHBoxLayout:
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.setContentsMargins(0, 0, 0, 0)

        self._btn_connect = QPushButton("CONNECT")
        self._btn_connect.setObjectName("btn_primary")
        self._btn_connect.setMinimumHeight(self._ACTION_BTN_H)
        self._btn_connect.setMaximumHeight(self._ACTION_BTN_H)
        self._btn_connect.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._btn_connect.clicked.connect(self._on_connect_clicked)

        self._btn_duplicate = QPushButton("DUPLICATE")
        self._btn_duplicate.setObjectName("btn_action")
        self._btn_duplicate.setMinimumHeight(self._ACTION_BTN_H)
        self._btn_duplicate.setMaximumHeight(self._ACTION_BTN_H)
        self._btn_duplicate.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._btn_duplicate.setToolTip(
            "Open a second tab using the same connection details"
        )
        self._btn_duplicate.clicked.connect(self._on_duplicate_clicked)

        action_row.addWidget(self._btn_connect, 1)
        action_row.addWidget(self._btn_duplicate, 1)
        return action_row

    def _make_line_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumHeight(self._ROW_HEIGHT)
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        return edit

    def _make_form_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("ssh_form_label")
        lbl.setFixedWidth(self._LABEL_COL_W)
        lbl.setMinimumHeight(self._ROW_HEIGHT)
        lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        return lbl

    def _make_form_row(self, label_text: str, field) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(self._make_form_label(label_text), 0)
        if isinstance(field, QWidget):
            row.addWidget(field, 1)
        else:
            row.addLayout(field, 1)
        return row

    # ----- Status block ------------------------------------------------------

    def _build_status_block(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("ssh_status_wrap")

        sl = QHBoxLayout(wrap)
        sl.setContentsMargins(14, 12, 14, 12)
        sl.setSpacing(10)

        self._dot = StatusDot(size=10)
        sl.addWidget(self._dot)

        self._lbl_session_state = QLabel("No active session")
        sl.addWidget(self._lbl_session_state)
        sl.addStretch(1)

        self._lbl_session_count = QLabel("0 tabs")
        sl.addWidget(self._lbl_session_count)

        self._status_wrap = wrap
        return wrap

    # ── Right panel (workspace) ──────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        right.setObjectName("ssh_right_panel")
        right_lay = QVBoxLayout(right)
        # Symmetric inner padding so the quick-connect card has proper
        # breathing room on every side and the border never touches
        # the splitter or window edge.
        right_lay.setContentsMargins(14, 4, 4, 4)
        right_lay.setSpacing(12)

        # ── Quick connect bar ────────────────────────────────────────────
        quick_bar = QFrame()
        quick_bar.setObjectName("ssh_quick_bar")
        # Size the bar from its content — a min/max pair is enough to
        # keep it visually stable without clipping the 1px border on
        # any theme / HiDPI scale.
        quick_bar.setMinimumHeight(60)
        quick_bar.setMaximumHeight(64)
        quick_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        qb = QHBoxLayout(quick_bar)
        qb.setContentsMargins(14, 12, 14, 12)
        qb.setSpacing(10)

        # Mirror toggle — lets the user re-open the manager from the
        # right-hand workspace too, so they always have a way back.
        self._btn_workspace_toggle = QToolButton()
        self._btn_workspace_toggle.setObjectName("ssh_panel_toggle")
        self._btn_workspace_toggle.setText("☰")
        self._btn_workspace_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_workspace_toggle.setFixedSize(30, 30)
        self._btn_workspace_toggle.setToolTip(
            "Toggle connection manager panel"
        )
        self._btn_workspace_toggle.clicked.connect(self.toggle_manager)
        qb.addWidget(self._btn_workspace_toggle)

        qb_label = QLabel("QUICK CONNECT")
        qb_label.setObjectName("lbl_field_label")
        qb.addWidget(qb_label)

        self._quick_input = QLineEdit()
        self._quick_input.setPlaceholderText(
            "user@host:port — press Enter to open a new session"
        )
        self._quick_input.setMinimumHeight(34)
        self._quick_input.returnPressed.connect(self._on_quick_connect)
        qb.addWidget(self._quick_input, stretch=1)

        self._btn_quick_go = QPushButton("OPEN")
        self._btn_quick_go.setObjectName("btn_primary")
        self._btn_quick_go.setMinimumHeight(34)
        self._btn_quick_go.setFixedWidth(86)
        self._btn_quick_go.clicked.connect(self._on_quick_connect)
        qb.addWidget(self._btn_quick_go)

        self._quick_bar = quick_bar
        right_lay.addWidget(quick_bar)

        # ── Tab area ─────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(False)
        self._tabs.tabBar().setExpanding(False)
        self._tabs.tabBar().setUsesScrollButtons(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.tabBarDoubleClicked.connect(self._on_tab_rename)
        self._tabs.currentChanged.connect(self._on_active_tab_changed)

        # Empty-state placeholder shown when no tabs are open.
        self._empty_state = QFrame()
        self._empty_state.setObjectName("ssh_empty_state")
        es_lay = QVBoxLayout(self._empty_state)
        es_lay.setContentsMargins(40, 60, 40, 40)
        es_lay.setSpacing(8)
        es_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._es_title = QLabel("No active SSH sessions")
        self._es_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._es_title.setObjectName("ssh_empty_title")
        es_lay.addWidget(self._es_title)

        self._es_hint = QLabel(
            "Use the connection form on the left, double-click a saved\n"
            "session, or type a target in the quick-connect bar above."
        )
        self._es_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._es_hint.setObjectName("ssh_empty_hint")
        es_lay.addWidget(self._es_hint)

        # Stack the empty state on top of the tab widget; switch via the
        # _refresh_workspace_visibility() helper.
        self._workspace_stack = QFrame()
        ws_lay = QVBoxLayout(self._workspace_stack)
        ws_lay.setContentsMargins(0, 0, 0, 0)
        ws_lay.setSpacing(0)
        ws_lay.addWidget(self._empty_state)
        ws_lay.addWidget(self._tabs)

        right_lay.addWidget(self._workspace_stack, stretch=1)

        self._refresh_workspace_visibility()
        return right

    # ── Public API used by main_window / scanner ────────────────────────────

    def prefill_host(self, ip: str) -> None:
        """
        Populate the connection form with `ip` and focus the User
        field. Does NOT auto-connect — the user clicks Connect.
        """
        self._in_host.setText(ip)
        if not self._in_name.text().strip():
            self._in_name.setText(f"Scan-{ip}")
        self._in_user.setFocus()
        # Make sure the form is visible if it was collapsed
        self._conn_section.set_collapsed(False)

    def connect_with_profile(self, profile: dict) -> None:
        """
        Open a brand-new tab and connect using `profile` immediately.
        Used by the scanner host-details drawer's quick connect.
        """
        if not profile or not profile.get("host"):
            return
        ssh_profile = SSHProfile(
            name=profile.get("name", "") or "",
            host=profile.get("host", ""),
            port=int(profile.get("port", 22) or 22),
            user=profile.get("user", ""),
            password=profile.get("password", ""),
            key_path=profile.get("key_path", ""),
        )
        if not ssh_profile.user:
            # Without a user the connection will fail; populate the form
            # so the user can fill it in instead of forcing a doomed
            # connect attempt.
            self.prefill_host(ssh_profile.host)
            self._in_port.setValue(ssh_profile.port)
            self._in_name.setText(ssh_profile.name)
            self.status_message.emit(
                f"Fill in user for {ssh_profile.host} and press Connect"
            )
            return
        self._open_session_tab(ssh_profile)

    def disconnect_active(self) -> None:
        """Close whichever tab is currently active. Drawer disconnect uses this."""
        idx = self._tabs.currentIndex()
        if idx >= 0:
            self._on_tab_close_requested(idx)

    # ── Saved sessions ──────────────────────────────────────────────────────

    def _reload_sessions(self) -> None:
        sessions = self._all_sessions_sorted()
        filt = self._search.text().strip().lower()
        self._sessions_list.clear()
        for entry in sessions:
            if filt and filt not in self._search_haystack(entry):
                continue
            label = self._format_session_label(entry)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._sessions_list.addItem(item)

    @staticmethod
    def _all_sessions_sorted() -> list[dict]:
        sessions = list(settings.get_ssh_hosts())
        sessions.sort(
            key=lambda s: (
                not bool(s.get("favorite")),
                (s.get("name") or s.get("host") or "").lower(),
            )
        )
        return sessions

    @staticmethod
    def _search_haystack(entry: dict) -> str:
        return " ".join(str(entry.get(k, "")) for k in
                        ("name", "host", "user", "auth_method", "port")).lower()

    @staticmethod
    def _format_session_label(entry: dict) -> str:
        star = "★ " if entry.get("favorite") else "   "
        name = entry.get("name") or f"{entry.get('user', '')}@{entry.get('host', '')}"
        host = entry.get("host", "")
        port = entry.get("port", 22)
        user = entry.get("user", "")
        last = entry.get("last_connected", "")
        suffix = ""
        if last:
            suffix = f"   · last {last}"
        return f"{star}{name}\n     {user}@{host}:{port}{suffix}"

    def _on_filter_changed(self, _text: str) -> None:
        self._reload_sessions()

    def _on_session_selected(self) -> None:
        items = self._sessions_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        self._populate_form_from_entry(entry)

    def _on_session_activated(self, _item) -> None:
        # Double-click on a saved session = open it in a new tab.
        items = self._sessions_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        self._populate_form_from_entry(entry)
        self._on_connect_clicked()

    def _populate_form_from_entry(self, entry: dict) -> None:
        self._in_name.setText(entry.get("name", ""))
        self._in_host.setText(entry.get("host", ""))
        self._in_port.setValue(int(entry.get("port", 22) or 22))
        self._in_user.setText(entry.get("user", ""))
        # Don't load passwords from disk — operator must re-type unless
        # save_credentials was true and a value is on file.
        if entry.get("save_credentials") and entry.get("password"):
            self._in_pass.setText(entry.get("password", ""))
        else:
            self._in_pass.clear()
        self._in_key.setText(entry.get("key_path", ""))
        self._chk_save_creds.setChecked(bool(entry.get("save_credentials")))

    def _on_new_session(self) -> None:
        for w in (self._in_name, self._in_host, self._in_user,
                  self._in_pass, self._in_key):
            w.clear()
        self._in_port.setValue(22)
        self._chk_save_creds.setChecked(False)
        self._sessions_list.clearSelection()
        self._in_name.setFocus()

    def _on_save_session(self) -> None:
        host = self._in_host.text().strip()
        if not host:
            QMessageBox.warning(self, "Save session", "Host is required.")
            return
        name = self._in_name.text().strip() or (
            f"{self._in_user.text().strip() or 'host'}@{host}"
        )
        self._in_name.setText(name)

        save_creds = self._chk_save_creds.isChecked()
        existing = next(
            (s for s in settings.get_ssh_hosts() if s.get("name") == name),
            {},
        )
        entry = {
            "name":     name,
            "host":     host,
            "port":     int(self._in_port.value()),
            "user":     self._in_user.text().strip(),
            "key_path": self._in_key.text().strip(),
            "auth_method": self._infer_auth_method(),
            "save_credentials": save_creds,
            "favorite": existing.get("favorite", False),
            "last_connected": existing.get("last_connected", ""),
        }
        if save_creds:
            entry["password"] = self._in_pass.text()
        settings.save_ssh_host(entry)
        self._reload_sessions()
        self.status_message.emit(f"Saved session '{name}'")

    def _on_delete_session(self) -> None:
        items = self._sessions_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        name = entry.get("name", "")
        if not name:
            return
        confirm = QMessageBox.question(
            self, "Delete session", f"Remove saved session '{name}'?"
        )
        if confirm == QMessageBox.StandardButton.Yes:
            settings.delete_ssh_host(name)
            self._reload_sessions()

    def _on_toggle_pin(self) -> None:
        items = self._sessions_list.selectedItems()
        if not items:
            return
        entry = items[0].data(Qt.ItemDataRole.UserRole) or {}
        name = entry.get("name", "")
        if not name:
            return
        entry = dict(entry)
        entry["favorite"] = not bool(entry.get("favorite"))
        settings.save_ssh_host(entry)
        self._reload_sessions()
        # Re-select the same item to keep continuity
        for i in range(self._sessions_list.count()):
            item = self._sessions_list.item(i)
            if item and (item.data(Qt.ItemDataRole.UserRole) or {}).get("name") == name:
                self._sessions_list.setCurrentItem(item)
                break

    def _infer_auth_method(self) -> str:
        if self._in_key.text().strip():
            return "key"
        if self._in_pass.text():
            return "password"
        return "agent"

    def _record_last_connected(self, profile: SSHProfile) -> None:
        # Update the timestamp on a saved session matching this name (if any).
        if not profile.name:
            return
        existing = next(
            (s for s in settings.get_ssh_hosts() if s.get("name") == profile.name),
            None,
        )
        if existing is None:
            return
        existing = dict(existing)
        existing["last_connected"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        settings.save_ssh_host(existing)
        self._reload_sessions()

    # ── Connect / quick connect / duplicate ─────────────────────────────────

    def _profile_from_form(self) -> Optional[SSHProfile]:
        host = self._in_host.text().strip()
        user = self._in_user.text().strip()
        if not host:
            QMessageBox.warning(self, "SSH", "Host is required.")
            return None
        if not user:
            QMessageBox.warning(self, "SSH", "User is required.")
            return None
        return SSHProfile(
            name=self._in_name.text().strip(),
            host=host,
            port=int(self._in_port.value()),
            user=user,
            password=self._in_pass.text(),
            key_path=self._in_key.text().strip(),
        )

    def _on_connect_clicked(self) -> None:
        profile = self._profile_from_form()
        if profile is None:
            return
        if not HAS_PARAMIKO:
            QMessageBox.critical(
                self, "SSH unavailable",
                "paramiko is not installed.\n\nRun: pip install paramiko"
            )
            return
        self._open_session_tab(profile)

    def _on_duplicate_clicked(self) -> None:
        profile = self._profile_from_form()
        if profile is None:
            return
        # Duplicate gives the new tab a derived name to keep tabs distinct.
        if profile.name:
            profile.name = f"{profile.name} (copy)"
        self._open_session_tab(profile)

    def _on_quick_connect(self) -> None:
        text = self._quick_input.text().strip()
        if not text:
            return
        try:
            profile = self._parse_quick_connect(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Quick connect", str(exc))
            return
        if not profile.user:
            self.status_message.emit("Quick connect needs a username — use user@host")
            QMessageBox.warning(
                self, "Quick connect",
                "Quick connect requires a username — use user@host[:port]."
            )
            return
        self._open_session_tab(profile)
        self._quick_input.clear()

    @staticmethod
    def _parse_quick_connect(text: str) -> SSHProfile:
        """
        Parse `[user@]host[:port]` into an SSHProfile.

        Examples:
            root@10.0.0.1
            10.0.0.1
            admin@router.local:2222
        """
        user = ""
        if "@" in text:
            user, _, rest = text.partition("@")
            user = user.strip()
        else:
            rest = text
        port = 22
        host = rest.strip()
        if ":" in host:
            host_part, _, port_part = host.partition(":")
            host = host_part.strip()
            try:
                port = int(port_part.strip())
            except ValueError:
                raise ValueError(f"Invalid port: {port_part!r}")
        if not host:
            raise ValueError("Host is required")
        return SSHProfile(
            name="",
            host=host,
            port=port,
            user=user,
            password="",
            key_path="",
        )

    # ── Tab management ──────────────────────────────────────────────────────

    def _open_session_tab(self, profile: SSHProfile) -> None:
        tab = SshSessionTab(profile, self)
        idx = self._tabs.addTab(tab, tab.title_text())
        self._tabs.setTabToolTip(idx, tab.summary_text())
        self._tabs.setCurrentIndex(idx)

        # Wire per-tab signals so the parent view can react.
        tab.state_changed.connect(
            lambda state, t=tab: self._on_tab_state_changed(t, state)
        )
        tab.title_changed.connect(
            lambda title, t=tab: self._on_tab_title_changed(t, title)
        )
        tab.log_appended.connect(self._on_session_log)

        # Reflect the new tab in the workspace state.
        self._refresh_workspace_visibility()
        self._update_session_count()

        # Begin the connection.
        tab.start_connection()

        # Optional: collapse the form once the user actually opens a tab
        # so the workspace gets more vertical room.
        if self._tabs.count() == 1:
            self._conn_section.set_collapsed(True)

        self._record_last_connected(profile)

    def _on_tab_close_requested(self, idx: int) -> None:
        widget = self._tabs.widget(idx)
        if isinstance(widget, SshSessionTab):
            try:
                widget.shutdown()
            except Exception:
                pass
        self._tabs.removeTab(idx)
        if widget is not None:
            widget.deleteLater()
        self._refresh_workspace_visibility()
        self._update_session_count()
        if self._tabs.count() == 0:
            self._lbl_session_state.setText("No active session")
            self._dot.set_active(False)
            self._dot.set_color(theme().text_dim)
            # Re-expand the form so the user can immediately compose a
            # new connection.
            self._conn_section.set_collapsed(False)

    def _on_tab_rename(self, idx: int) -> None:
        if idx < 0:
            return
        widget = self._tabs.widget(idx)
        if not isinstance(widget, SshSessionTab):
            return
        current = widget.profile.name or widget.title_text()
        new_name, ok = QInputDialog.getText(
            self, "Rename session", "New name:", text=current
        )
        if ok and new_name.strip():
            widget.set_title(new_name.strip())
            self._tabs.setTabText(idx, widget.title_text())

    def _on_tab_state_changed(self, tab: SshSessionTab, state: str) -> None:
        idx = self._tabs.indexOf(tab)
        if idx < 0:
            return
        t = theme()
        color = {
            STATE_CONNECTING: t.amber,
            STATE_CONNECTED:  t.green,
            STATE_FAILED:     t.red,
            STATE_CLOSED:     t.text_dim,
            STATE_IDLE:       t.text_dim,
        }.get(state, t.text_dim)
        self._tabs.tabBar().setTabTextColor(idx, QColor(color))

        # Mirror the active tab's state in the bottom-left status block.
        if idx == self._tabs.currentIndex():
            self._mirror_active_state(tab, state)

    def _on_tab_title_changed(self, tab: SshSessionTab, title: str) -> None:
        idx = self._tabs.indexOf(tab)
        if idx < 0:
            return
        self._tabs.setTabText(idx, title)
        self._tabs.setTabToolTip(idx, tab.summary_text())

    def _on_active_tab_changed(self, idx: int) -> None:
        if idx < 0:
            self._lbl_session_state.setText("No active session")
            self._dot.set_active(False)
            self._dot.set_color(theme().text_dim)
            return
        widget = self._tabs.widget(idx)
        if isinstance(widget, SshSessionTab):
            self._mirror_active_state(widget, widget.state())
            widget.terminal.setFocus()

    def _mirror_active_state(self, tab: SshSessionTab, state: str) -> None:
        t = theme()
        if state == STATE_CONNECTED:
            self._dot.set_active(True, color=t.green)
            self._lbl_session_state.setText(
                f"Connected · {tab.profile.user}@{tab.profile.host}:{tab.profile.port}"
            )
        elif state == STATE_CONNECTING:
            self._dot.set_active(True, color=t.amber)
            self._lbl_session_state.setText(
                f"Connecting to {tab.profile.host}…"
            )
        elif state == STATE_FAILED:
            self._dot.set_active(False)
            self._dot.set_color(t.red)
            self._lbl_session_state.setText("Connection failed")
        else:
            self._dot.set_active(False)
            self._dot.set_color(t.text_dim)
            self._lbl_session_state.setText("Disconnected")

    def _update_session_count(self) -> None:
        n = self._tabs.count()
        self._lbl_session_count.setText(
            "1 tab" if n == 1 else f"{n} tabs"
        )

    def _refresh_workspace_visibility(self) -> None:
        has_tabs = self._tabs.count() > 0
        self._empty_state.setVisible(not has_tabs)
        self._tabs.setVisible(has_tabs)

    def _on_session_log(self, line: str) -> None:
        self.status_message.emit(line)

    # ── Browse key ──────────────────────────────────────────────────────────

    def _on_browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SSH private key", "", "All files (*)"
        )
        if path:
            self._in_key.setText(path)

    # ── Theme + paramiko warning ────────────────────────────────────────────

    def _restyle(self, t):
        self._lbl_session_state.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px; font-weight: 600;"
            f" background: transparent;"
        )
        self._lbl_session_count.setStyleSheet(
            f"color: {t.text_dim}; font-size: 11px; background: transparent;"
        )
        # Panel-toggle buttons (header + rail + workspace mirror)
        toggle_qss = (
            f"QToolButton#ssh_panel_toggle {{"
            f"  background-color: {t.bg_raised};"
            f"  color: {t.accent};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 6px;"
            f"  font-size: 15px;"
            f"  font-weight: 800;"
            f"  padding: 0;"
            f"}}"
            f"QToolButton#ssh_panel_toggle:hover {{"
            f"  background-color: {t.accent_bg};"
            f"  border-color: {t.accent};"
            f"  color: {t.accent};"
            f"}}"
        )
        self._btn_collapse_panel.setStyleSheet(toggle_qss)
        self._btn_expand_panel.setStyleSheet(toggle_qss)
        self._btn_workspace_toggle.setStyleSheet(toggle_qss)

        # Collapsed rail container
        self._collapsed_rail.setStyleSheet(
            f"#ssh_collapse_rail {{"
            f"  background-color: {t.bg_raised};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 8px;"
            f"}}"
        )
        self._rail_label.setStyleSheet(
            f"#ssh_rail_label {{"
            f"  color: {t.text_dim};"
            f"  font-size: 10px;"
            f"  font-weight: 800;"
            f"  letter-spacing: 1.6px;"
            f"  background: transparent;"
            f"}}"
        )
        self._status_wrap.setStyleSheet(
            f"#ssh_status_wrap {{"
            f"  background-color: {t.bg_input};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 6px;"
            f"}}"
        )
        self._quick_bar.setStyleSheet(
            f"#ssh_quick_bar {{"
            f"  background-color: {t.bg_raised};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 8px;"
            f"}}"
            f"#ssh_quick_bar QLabel {{ background: transparent; }}"
        )
        self._empty_state.setStyleSheet(
            f"#ssh_empty_state {{"
            f"  background-color: {t.bg_base};"
            f"  border: 1px dashed {t.border_lt};"
            f"  border-radius: 8px;"
            f"}}"
        )
        self._es_title.setStyleSheet(
            f"color: {t.accent}; font-size: 16px; font-weight: 800;"
            f" letter-spacing: 0.6px;"
        )
        self._es_hint.setStyleSheet(
            f"color: {t.text_dim}; font-size: 12px;"
        )

    def _show_paramiko_warning(self) -> None:
        self._btn_connect.setEnabled(False)
        self._btn_connect.setToolTip(
            "Install paramiko to enable SSH (pip install paramiko)"
        )
        self._btn_quick_go.setEnabled(False)

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        for i in range(self._tabs.count() - 1, -1, -1):
            widget = self._tabs.widget(i)
            if isinstance(widget, SshSessionTab):
                try:
                    widget.shutdown()
                except Exception:
                    pass
            self._tabs.removeTab(i)
