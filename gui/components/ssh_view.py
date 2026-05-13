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

from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QLineEdit, QFileDialog, QMessageBox,
    QGroupBox, QSplitter, QTabWidget, QFrame, QSizePolicy, QLayout,
    QScrollArea, QInputDialog, QCheckBox, QTabBar, QStackedWidget,
    QToolButton, QComboBox,
)
from PyQt6.QtGui import QColor, QIcon, QIntValidator, QKeySequence, QShortcut

from gui.components.collapsible import CollapsibleSection
from gui.components.live_widgets import StatusDot
from gui.components.ssh_session_tab import (
    SshSessionTab,
    STATE_CONNECTING, STATE_CONNECTED, STATE_FAILED, STATE_CLOSED, STATE_IDLE,
)
from gui.themes import theme, ThemeManager
from scanner.ssh_client import SSHProfile, HAS_PARAMIKO
from scanner.serial_client import (
    SerialProfile, HAS_PYSERIAL,
    BAUD_PRESETS, DATA_BITS_OPTIONS, STOP_BITS_OPTIONS,
    PARITY_OPTIONS, FLOW_OPTIONS, LINE_ENDINGS,
    list_serial_ports,
)
from utils import settings


class SSHView(QWidget):
    """Multi-session SSH workspace page."""

    status_message = pyqtSignal(str)
    # Emitted when the user toggles the terminal focus mode. The main
    # window listens to this and hides/shows the sidebar, status bar,
    # and menu bar so the terminal area takes over the whole window.
    terminal_focus_mode_changed = pyqtSignal(bool)
    # Emitted whenever the set of open SSH tabs changes: a tab is
    # opened, closed, renamed, or transitions between connecting /
    # connected / failed / closed. The payload is not carried in the
    # signal — listeners call ``list_live_sessions()`` to snapshot the
    # current state. The File Transfer page listens to this so its
    # session picker stays in sync.
    sessions_changed = pyqtSignal()

    # Layout constants for the connection form (centralised so every
    # row reads the same numbers). Bumped on the 2026-04 stabilization
    # pass — old defaults left the Serial form's COM-port combo and
    # baud row visibly clipped on a 360-px-wide panel.
    _ROW_HEIGHT     = 36
    _LABEL_COL_W    = 72
    _ROW_VSPACING   = 12
    _GROUP_PAD_X    = 16
    _GROUP_PAD_TOP  = 16
    _GROUP_PAD_BOT  = 16
    _ACTION_BTN_H   = 44

    # Minimum / preferred / maximum widths for the left manager panel.
    # Wide enough that "Local echo" labels, "RTS/CTS" combo entries,
    # and a typical USB-serial device label all fit at the smallest
    # size without truncation.
    _LEFT_MIN_W     = 380
    _LEFT_MAX_W     = 560
    _LEFT_PREF_W    = 420

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def __init__(self, parent=None):
        super().__init__(parent)
        # Manager collapsed state — when True the SSH workspace gets the
        # full width and a thin expand rail is shown on the left.
        self._manager_collapsed = False
        # Terminal focus mode state. When True everything except the
        # tab bar + terminal is hidden so the user has a maximised
        # remote shell. The toggle preserves all SSH session state —
        # no reconnects, no channel churn.
        self._terminal_focus_mode = False
        # Re-entry guard for the Delete modal flow. Rapid double-
        # clicks on Delete (or a signal re-entrance via Qt's nested
        # modal event loop) can otherwise call _on_delete_session
        # twice concurrently and corrupt the list-widget state.
        self._delete_in_flight = False
        self._build_ui()
        self._reload_sessions()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # F11 toggles terminal focus mode. The shortcut is scoped to
        # the SSH view + children so it only fires while this page is
        # active and focused. F11 is safe — terminal shells don't
        # consume it. We deliberately do NOT bind Escape here because
        # Escape is needed by vim / less / etc. running inside the
        # remote shell.
        self._sc_focus = QShortcut(QKeySequence("F11"), self)
        self._sc_focus.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._sc_focus.activated.connect(self.toggle_terminal_focus_mode)

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
        self._splitter.setSizes([self._LEFT_PREF_W, 900])

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
        scroll.setMinimumWidth(self._LEFT_MIN_W)
        scroll.setMaximumWidth(self._LEFT_MAX_W)

        container = QWidget()
        container.setObjectName("ssh_left_container")
        lay = QVBoxLayout(container)
        # Slightly less aggressive right padding so the form has more
        # horizontal room for combos at the smallest panel width.
        lay.setContentsMargins(6, 6, 12, 6)
        lay.setSpacing(12)

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
            self._left_stack.setMinimumWidth(self._LEFT_MIN_W)
            self._left_stack.setMaximumWidth(self._LEFT_MAX_W)
            # Clamp to a sensible range of the total width so narrow
            # laptop windows still give the terminal enough room.
            target = min(
                max(self._LEFT_MIN_W, int(total * 0.32)),
                self._LEFT_MAX_W,
            )
            self._splitter.setSizes([target, max(total - target, 480)])
            self.status_message.emit("Connection manager restored")

    def toggle_manager(self) -> None:
        self.set_manager_collapsed(not self._manager_collapsed)

    # ----- Terminal focus mode ----------------------------------------------

    def is_terminal_focus_mode(self) -> bool:
        return self._terminal_focus_mode

    def toggle_terminal_focus_mode(self) -> None:
        self.set_terminal_focus_mode(not self._terminal_focus_mode)

    def _on_focus_toggle_clicked(self) -> None:
        """
        Handler for the corner toggle button. We don't trust the
        button's own checked state as the source of truth — we drive
        it from ``set_terminal_focus_mode`` instead so the button,
        the F11 shortcut, page-switch auto-exit, and the
        last-tab-close auto-exit all share one transition path.
        """
        self.set_terminal_focus_mode(not self._terminal_focus_mode)

    def set_terminal_focus_mode(self, on: bool) -> None:
        """
        Enter or leave terminal focus mode.

        Focus mode hides every surrounding panel inside this view
        (connection manager, quick-connect bar, per-tab headers) so
        the SSH terminal area and the tab bar take over the full
        workspace. The main window listens to
        ``terminal_focus_mode_changed`` and also hides the sidebar,
        status bar, and menu bar.

        The transition does NOT touch any SSH session. Tabs, channels,
        reader threads, terminal buffers, and command history all
        survive unchanged across the mode flip.

        Safe to call with the same value repeatedly — it is a no-op
        if the requested state already matches.
        """
        on = bool(on)
        if on == self._terminal_focus_mode:
            # Keep the button visual in sync even on a no-op call so
            # programmatic state changes and the button's own
            # toggle-on-click are always consistent.
            try:
                self._btn_focus_toggle.setChecked(on)
            except RuntimeError:
                pass
            return

        self._terminal_focus_mode = on

        # ── Hide/restore the surrounding panels inside this view ───────
        try:
            self._left_stack.setVisible(not on)
            self._quick_bar.setVisible(not on)
        except RuntimeError:
            pass

        # ── Tell each tab to hide/show its header strip ─────────────────
        for i in range(self._tabs.count()):
            try:
                widget = self._tabs.widget(i)
            except RuntimeError:
                continue
            if isinstance(widget, SshSessionTab):
                try:
                    widget.set_focus_mode(on)
                except Exception:
                    pass

        # ── Update the toggle button visual + tooltip ──────────────────
        try:
            self._btn_focus_toggle.setChecked(on)
            self._btn_focus_toggle.setToolTip(
                "Exit terminal focus mode  (F11)"
                if on else
                "Enter terminal focus mode  (F11)"
            )
        except RuntimeError:
            pass

        # ── Push focus to the active terminal so the user can type ─────
        # immediately — especially on F11 entry where the shortcut
        # itself may momentarily steal focus.
        if on:
            try:
                idx = self._tabs.currentIndex()
                if idx >= 0:
                    widget = self._tabs.widget(idx)
                    if isinstance(widget, SshSessionTab):
                        widget.terminal.setFocus()
            except RuntimeError:
                pass

        self.status_message.emit(
            "Terminal focus mode" if on else "Terminal focus mode exited"
        )
        self.terminal_focus_mode_changed.emit(on)

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

        # ── Connection-type toggle ───────────────────────────────────────
        # A small header at the top of the form switches the rest of the
        # field set between SSH (host/port/user/pass/key) and Serial /
        # UART (port/baud/parity/etc.). The Name field is shared so a
        # saved profile of either kind can be given a friendly label.
        self._cmb_type = QComboBox()
        self._cmb_type.addItem("SSH",    "ssh")
        self._cmb_type.addItem("Serial", "serial")
        self._cmb_type.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_type.currentIndexChanged.connect(self._on_type_changed)
        body.addLayout(self._make_form_row("Type", self._cmb_type))

        # Build inputs
        self._in_name = self._make_line_edit("Friendly name (optional)")
        body.addLayout(self._make_form_row("Name", self._in_name))

        # Stack the SSH form and the Serial form. Switching the type
        # combobox flips the stack to whichever form is relevant —
        # neither layout fights for space when hidden.
        self._form_stack = QStackedWidget()
        self._form_stack.setObjectName("ssh_form_stack")
        self._form_stack.addWidget(self._build_ssh_form())     # idx 0
        self._form_stack.addWidget(self._build_serial_form())  # idx 1
        body.addWidget(self._form_stack)

        body.addSpacing(8)
        body.addLayout(self._build_action_row())

        section.set_content_layout(body)
        return section

    def _current_kind(self) -> str:
        """Return the selected connection type — 'ssh' or 'serial'."""
        try:
            data = self._cmb_type.currentData()
        except RuntimeError:
            return "ssh"
        return str(data or "ssh")

    def _on_type_changed(self, _idx: int) -> None:
        kind = self._current_kind()
        self._form_stack.setCurrentIndex(0 if kind == "ssh" else 1)
        # Refresh COM port enumeration the first time Serial is shown
        # so the dropdown isn't blank when the user lands on it.
        if kind == "serial":
            self._refresh_serial_ports()

    # ── SSH form ─────────────────────────────────────────────────────────────

    def _build_ssh_form(self) -> QWidget:
        wrap = QWidget()
        wrap.setObjectName("ssh_form_ssh")
        body = QVBoxLayout(wrap)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(self._ROW_VSPACING)

        self._in_host = self._make_line_edit("hostname or IP")

        # A QLineEdit + QIntValidator instead of a QSpinBox. QSpinBox
        # fights common editing operations (can't be cleared to empty,
        # clamps on focus-out, paste of partial input is bounced) and
        # used to crash the app when the motion layer installed a
        # graphics effect on it — see the note in gui/motion.py about
        # QSpinBox and _AUTO_POLISH.
        #
        # This QLineEdit accepts any integer in the range [1, 65535]
        # mid-edit, allows the field to be temporarily empty while the
        # user retypes, and only parses + range-checks the value at
        # commit time (save, connect). Invalid values are surfaced as
        # a message box instead of crashing.
        self._in_port = QLineEdit()
        self._in_port.setObjectName("ssh_port_input")
        self._in_port.setValidator(QIntValidator(1, 65535, self._in_port))
        self._in_port.setMaxLength(5)
        self._in_port.setText("22")
        self._in_port.setPlaceholderText("22")
        self._in_port.setFixedWidth(120)
        self._in_port.setMinimumHeight(self._ROW_HEIGHT)
        self._in_port.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._in_port.setAlignment(Qt.AlignmentFlag.AlignLeft)

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

        return wrap

    # ── Serial form ──────────────────────────────────────────────────────────

    def _build_serial_form(self) -> QWidget:
        """
        Serial / UART form.

        Layout is split into two zones:

          * **Essentials** — Port (with Refresh), Baud, Enter line
            ending, Local echo. These are the only fields a typical
            UART console / AT-command session ever needs to touch.

          * **Advanced settings** — Data bits, Stop bits, Parity, Flow
            control. Tucked behind a collapsed toggle so the form looks
            clean for the 99% case (8-N-1, no flow control). The
            advanced widgets are still constructed eagerly so saved-
            session loading + ``_serial_profile_from_form`` always have
            them available regardless of whether the user has expanded
            the section.
        """
        wrap = QWidget()
        wrap.setObjectName("ssh_form_serial")
        body = QVBoxLayout(wrap)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(self._ROW_VSPACING)

        # ── Port selector ───────────────────────────────────────────────
        # Editable combobox so the user can paste a name pyserial didn't
        # enumerate (rare but happens with virtual COM-port drivers on
        # Windows). Refresh button on the right re-runs
        # list_serial_ports().
        self._cmb_serial_port = QComboBox()
        self._cmb_serial_port.setEditable(True)
        self._cmb_serial_port.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_serial_port.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._cmb_serial_port.lineEdit().setPlaceholderText(
            "COM3 / /dev/ttyUSB0"
        )
        # Reserve enough character width that short port names always
        # render fully even with the panel at its minimum size.
        self._cmb_serial_port.setMinimumContentsLength(14)
        # Let the dropdown popup grow wider than the field so long
        # USB-serial device descriptions are readable.
        try:
            from PyQt6.QtWidgets import QSizePolicy as _SP
            self._cmb_serial_port.view().setMinimumWidth(360)
            self._cmb_serial_port.view().setSizePolicy(
                _SP.Policy.Expanding, _SP.Policy.Preferred
            )
        except Exception:
            pass
        self._cmb_serial_port.setToolTip(
            "COM port to open (type a name if not listed)"
        )
        # When the user picks an item from the dropdown, swap the line
        # edit text to the raw device name (e.g. "COM3") instead of the
        # descriptive label ("COM3 — Silicon Labs CP210x USB to UART
        # Bridge"). Without this, pyserial would try to open the
        # literal label string and fail with FileNotFoundError — the
        # exact symptom this rework is fixing. Selection by index is
        # the dropdown-pick signal; ``editTextChanged`` would also fire
        # but that's the user typing, which we leave alone.
        self._cmb_serial_port.activated.connect(
            self._on_serial_port_picked
        )
        self._cmb_serial_port.editTextChanged.connect(
            lambda _t: self._update_serial_port_hint()
        )

        self._btn_serial_refresh = QPushButton("Refresh")
        self._btn_serial_refresh.setObjectName("btn_action")
        self._btn_serial_refresh.setFixedHeight(self._ROW_HEIGHT)
        # Slimmer than the SSH "Browse" button so the COM combo always
        # has visible-text width even on the smallest panel size.
        self._btn_serial_refresh.setFixedWidth(74)
        self._btn_serial_refresh.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_serial_refresh.setToolTip("Re-enumerate available COM ports")
        self._btn_serial_refresh.clicked.connect(self._refresh_serial_ports)

        port_field = QHBoxLayout()
        port_field.setContentsMargins(0, 0, 0, 0)
        port_field.setSpacing(8)
        port_field.addWidget(self._cmb_serial_port, 1)
        port_field.addWidget(self._btn_serial_refresh, 0)

        # ── Baud (essential) ────────────────────────────────────────────
        # Editable so non-standard baud rates work.
        self._cmb_baud = QComboBox()
        self._cmb_baud.setEditable(True)
        self._cmb_baud.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_baud.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._cmb_baud.setMinimumContentsLength(8)
        for rate in BAUD_PRESETS:
            self._cmb_baud.addItem(str(rate), rate)
        self._cmb_baud.setCurrentText("115200")
        self._cmb_baud.lineEdit().setValidator(QIntValidator(1, 4_000_000))

        # ── Line ending (essential) ─────────────────────────────────────
        # What Enter sends. Default CRLF for serial — AT-command devices
        # and most UART consoles expect CRLF; users with bare-LF Linux
        # consoles can switch to LF.
        self._cmb_ending = QComboBox()
        self._cmb_ending.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_ending.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        ending_labels = {"cr": "CR (\\r)", "lf": "LF (\\n)", "crlf": "CRLF (\\r\\n)"}
        for e in LINE_ENDINGS:
            self._cmb_ending.addItem(ending_labels.get(e, e), e)
        self._cmb_ending.setCurrentText(ending_labels["crlf"])

        # ── Local echo (essential) ──────────────────────────────────────
        # Off by default; many UART consoles echo already, so forcing
        # this on would double every keystroke. Toggle on for AT-command
        # modems that don't echo.
        self._chk_local_echo = QCheckBox("Local echo (show typed characters)")
        self._chk_local_echo.setChecked(False)

        # ── Advanced widgets (built but not displayed yet) ──────────────
        # These four are device-specific and almost always 8/1/None/None
        # for typical UART work. Hide behind the Advanced toggle so the
        # form looks clean for the common case.
        # All advanced combos use Expanding horizontal policy so the
        # parent form-row layout stretches them to fill available
        # width, matching the SSH form's behaviour and preventing
        # short combos from looking awkwardly narrow.
        _adv_pol = (QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._cmb_data = QComboBox()
        self._cmb_data.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_data.setSizePolicy(*_adv_pol)
        for bits in DATA_BITS_OPTIONS:
            self._cmb_data.addItem(str(bits), bits)
        self._cmb_data.setCurrentText("8")

        self._cmb_stop = QComboBox()
        self._cmb_stop.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_stop.setSizePolicy(*_adv_pol)
        for sb in STOP_BITS_OPTIONS:
            label = "1" if sb == 1.0 else ("1.5" if abs(sb - 1.5) < 0.01 else "2")
            self._cmb_stop.addItem(label, sb)
        self._cmb_stop.setCurrentText("1")

        self._cmb_parity = QComboBox()
        self._cmb_parity.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_parity.setSizePolicy(*_adv_pol)
        for p in PARITY_OPTIONS:
            self._cmb_parity.addItem(p.title(), p)
        self._cmb_parity.setCurrentText("None")

        self._cmb_flow = QComboBox()
        self._cmb_flow.setMinimumHeight(self._ROW_HEIGHT)
        self._cmb_flow.setSizePolicy(*_adv_pol)
        flow_labels = {
            "none": "None",
            "rts_cts": "RTS/CTS",
            "xon_xoff": "XON/XOFF",
            "dsr_dtr": "DSR/DTR",
        }
        for f in FLOW_OPTIONS:
            self._cmb_flow.addItem(flow_labels.get(f, f), f)
        self._cmb_flow.setCurrentText("None")

        # ── Description hint shown under the Port row ───────────────────
        # PuTTY shows the human-readable device description ("USB-SERIAL
        # CH340", "Silicon Labs CP210x USB to UART Bridge", …) next to
        # the port name. We do the same so the user can confirm at a
        # glance which physical device they're about to open. Hidden
        # when there's no description for the current port name.
        self._lbl_serial_hint = QLabel("")
        self._lbl_serial_hint.setObjectName("ssh_serial_hint")
        self._lbl_serial_hint.setWordWrap(True)
        self._lbl_serial_hint.setVisible(False)

        # ── Build essential rows ────────────────────────────────────────
        body.addLayout(self._make_form_row("Port",   port_field))
        body.addLayout(self._make_form_row("",       self._lbl_serial_hint))
        body.addLayout(self._make_form_row("Baud",   self._cmb_baud))
        body.addLayout(self._make_form_row("Enter",  self._cmb_ending))
        body.addSpacing(2)
        body.addWidget(self._chk_local_echo)

        # ── Advanced toggle + collapsible body ──────────────────────────
        self._btn_serial_advanced = QToolButton()
        self._btn_serial_advanced.setObjectName("ssh_serial_adv_toggle")
        self._btn_serial_advanced.setText("▸  Advanced settings")
        self._btn_serial_advanced.setCheckable(True)
        self._btn_serial_advanced.setChecked(False)
        self._btn_serial_advanced.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_serial_advanced.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self._btn_serial_advanced.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._btn_serial_advanced.setMinimumHeight(28)
        self._btn_serial_advanced.toggled.connect(
            self._on_serial_advanced_toggled
        )

        self._serial_adv_body = QFrame()
        self._serial_adv_body.setObjectName("ssh_serial_adv_body")
        adv_lay = QVBoxLayout(self._serial_adv_body)
        adv_lay.setContentsMargins(0, 6, 0, 0)
        adv_lay.setSpacing(self._ROW_VSPACING)
        adv_lay.addLayout(self._make_form_row("Data",   self._cmb_data))
        adv_lay.addLayout(self._make_form_row("Stop",   self._cmb_stop))
        adv_lay.addLayout(self._make_form_row("Parity", self._cmb_parity))
        adv_lay.addLayout(self._make_form_row("Flow",   self._cmb_flow))
        self._serial_adv_body.setVisible(False)

        body.addSpacing(6)
        body.addWidget(self._btn_serial_advanced)
        body.addWidget(self._serial_adv_body)

        # Populate the COM-port dropdown right away so the form is
        # ready the moment the user switches the type to Serial.
        self._refresh_serial_ports()

        return wrap

    def _on_serial_advanced_toggled(self, checked: bool) -> None:
        """Show / hide the advanced serial settings block."""
        try:
            on = bool(checked)
            self._serial_adv_body.setVisible(on)
            self._btn_serial_advanced.setText(
                "▾  Advanced settings" if on else "▸  Advanced settings"
            )
        except RuntimeError:
            return

    @staticmethod
    def _normalize_port_name(text: str) -> str:
        """
        Strip any descriptive suffix from a possibly-decorated COM port
        string, returning just the raw OS device name that pyserial
        expects.

        Examples:
            "COM3"                                  -> "COM3"
            "COM3 — Silicon Labs CP210x..."         -> "COM3"
            "COM3 - USB Serial Device"              -> "COM3"
            "COM3 (USB Serial Device)"              -> "COM3"
            "/dev/ttyUSB0"                          -> "/dev/ttyUSB0"
            "/dev/ttyUSB0 - FT232R"                 -> "/dev/ttyUSB0"
            ""                                      -> ""

        Defensive guard for the case where an old saved profile or a
        manually-pasted decorated label would otherwise be sent to
        pyserial verbatim.
        """
        text = (text or "").strip()
        if not text:
            return ""
        # Em-dash separator used by SerialPortInfo.label, plus the
        # ASCII variants for old saves and manual paste.
        for sep in (" — ", " – ", " - ", " (", "\t"):
            if sep in text:
                text = text.split(sep, 1)[0].strip()
                break
        return text

    def _serial_port_value(self) -> str:
        """
        Return the canonical raw port name for whatever the user has
        in the COM dropdown right now.

        Resolution order:
          1. If currentIndex is set and itemData is a string, that data
             value is the device name pyserial wants — use it. This
             covers the dropdown-pick path even if the line edit was
             never swapped (defence in depth).
          2. Otherwise, run the line-edit text through
             ``_normalize_port_name`` so a label-shaped string still
             yields a clean device name.

        Always returns a stripped string; empty string means no port.
        """
        try:
            idx = self._cmb_serial_port.currentIndex()
            text = (self._cmb_serial_port.currentText() or "").strip()
        except (RuntimeError, AttributeError):
            return ""
        if idx is not None and idx >= 0:
            try:
                data = self._cmb_serial_port.itemData(idx)
                label = self._cmb_serial_port.itemText(idx)
            except (RuntimeError, AttributeError):
                data, label = None, ""
            if isinstance(data, str) and data:
                # The user is showing this dropdown item — use its raw
                # device name unless they typed something else after.
                if (label or "") == text or (data or "") == text:
                    return data.strip()
        return self._normalize_port_name(text)

    def _on_serial_port_picked(self, idx: int) -> None:
        """
        Slot for ``QComboBox.activated`` on the COM port dropdown.

        Replaces the line-edit text with the raw device name from the
        item's userData. Without this the line edit shows the long
        descriptive label and ``currentText()`` would return that label
        verbatim — pyserial would then try to open
        "COM3 — Silicon Labs..." as a literal port name and fail.

        Wrapped in defensive try/except: a torn-down combo or a
        spurious activation index is silently ignored.
        """
        if idx is None or idx < 0:
            return
        try:
            data = self._cmb_serial_port.itemData(idx)
        except (RuntimeError, AttributeError):
            return
        if not isinstance(data, str) or not data:
            return
        try:
            self._cmb_serial_port.blockSignals(True)
            try:
                self._cmb_serial_port.setEditText(data)
            finally:
                self._cmb_serial_port.blockSignals(False)
        except (RuntimeError, AttributeError):
            return
        self._update_serial_port_hint()

    def _update_serial_port_hint(self) -> None:
        """
        Show a small description hint under the port row matching the
        currently-typed port. Mirrors PuTTY's "show device description
        next to the COM number" UX.
        """
        try:
            text = self._normalize_port_name(
                self._cmb_serial_port.currentText() or ""
            )
        except (RuntimeError, AttributeError):
            return
        desc = ""
        if text:
            try:
                for i in range(self._cmb_serial_port.count()):
                    data = self._cmb_serial_port.itemData(i)
                    if isinstance(data, str) and data == text:
                        label = self._cmb_serial_port.itemText(i) or ""
                        # SerialPortInfo.label is "device — description"
                        # when a description exists; split on the same
                        # em-dash separator we used to build it.
                        for sep in (" — ", " – ", " - "):
                            if sep in label:
                                desc = label.split(sep, 1)[1].strip()
                                break
                        break
            except (RuntimeError, AttributeError):
                desc = ""
        try:
            self._lbl_serial_hint.setText(desc)
            self._lbl_serial_hint.setVisible(bool(desc))
        except (RuntimeError, AttributeError):
            return

    def _refresh_serial_ports(self) -> None:
        """
        Re-enumerate COM ports and repopulate the dropdown.

        Defensive on every step so a flaky USB driver or a freshly-
        torn-down widget can never crash the call:

          * ``list_serial_ports()`` already swallows pyserial errors.
          * Every Qt access is wrapped — a torn-down combo on shutdown
            or theme reload is a clean early-return, never a crash.
          * The user's currently-typed text is preserved so a refresh
            never wipes a manual entry mid-edit.
        """
        try:
            current = (self._cmb_serial_port.currentText() or "").strip()
        except (RuntimeError, AttributeError):
            return

        try:
            ports = list_serial_ports()
        except Exception:
            ports = []

        try:
            self._cmb_serial_port.blockSignals(True)
            try:
                self._cmb_serial_port.clear()
                for info in ports:
                    try:
                        self._cmb_serial_port.addItem(info.label, info.device)
                    except Exception:
                        continue
            finally:
                self._cmb_serial_port.blockSignals(False)
        except (RuntimeError, AttributeError):
            return

        # Restore the previously-typed value so a refresh doesn't wipe
        # what the user was about to commit. Manual entries (devices
        # the OS didn't enumerate) survive the refresh.
        if current:
            try:
                self._cmb_serial_port.setEditText(current)
            except (RuntimeError, AttributeError):
                return

        # Refresh the description hint after the port list rebuild so
        # the visible description tracks whatever port the user has
        # currently typed.
        self._update_serial_port_hint()

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
        self._tabs.setObjectName("ssh_tabs")
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(False)
        self._tabs.tabBar().setExpanding(False)
        self._tabs.tabBar().setUsesScrollButtons(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.tabBarDoubleClicked.connect(self._on_tab_rename)
        self._tabs.currentChanged.connect(self._on_active_tab_changed)

        # ── Focus-mode toggle (tab bar corner widget) ────────────────────
        # Small always-visible toggle sitting in the top-right corner
        # of the tab bar. Checked state == focus mode active. Also
        # bindable via F11 from anywhere in this view.
        self._btn_focus_toggle = QToolButton()
        self._btn_focus_toggle.setObjectName("ssh_focus_toggle")
        self._btn_focus_toggle.setText("⛶")
        self._btn_focus_toggle.setCheckable(True)
        self._btn_focus_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_focus_toggle.setFixedSize(30, 26)
        self._btn_focus_toggle.setToolTip(
            "Enter terminal focus mode  (F11)"
        )
        self._btn_focus_toggle.clicked.connect(
            self._on_focus_toggle_clicked
        )
        self._tabs.setCornerWidget(
            self._btn_focus_toggle, Qt.Corner.TopRightCorner
        )

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
            self._set_port(ssh_profile.port)
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

    def list_live_sessions(self) -> list[dict]:
        """
        Snapshot every open SSH tab as a list of dicts. Each dict has:

            id    : stable Python id() of the SshSessionTab (so callers
                    can compare against later snapshots)
            title : tab title (friendly name)
            label : "user@host:port" summary
            state : STATE_* string from ssh_session_tab
            tab   : the SshSessionTab widget itself — borrow only,
                    do NOT hold across tear-down. Used by
                    FileTransferView to resolve the underlying
                    SSHSession for SFTP.

        Only tabs in STATE_CONNECTED are usable for SFTP — the caller
        filters on ``state`` as needed.
        """
        out: list[dict] = []
        try:
            count = self._tabs.count()
        except RuntimeError:
            return out
        for i in range(count):
            try:
                w = self._tabs.widget(i)
            except RuntimeError:
                continue
            if not isinstance(w, SshSessionTab):
                continue
            # Serial tabs use the same widget class but cannot host
            # an SFTP subsystem — keep them out of the live-session
            # roster so File Transfer's session picker only sees SSH
            # tabs. The SSH workspace tab bar itself is the
            # authoritative listing for the user.
            if getattr(w, "_is_serial", False):
                continue
            try:
                out.append({
                    "id":    id(w),
                    "title": w.title_text(),
                    "label": w.summary_text(),
                    "state": w.state(),
                    "tab":   w,
                })
            except RuntimeError:
                continue
        return out

    # ── Saved sessions ──────────────────────────────────────────────────────

    def _reload_sessions(self) -> None:
        # Block signals on the list for the entire rebuild so the
        # itemSelectionChanged signal does NOT fire as Qt clears the
        # old items and inserts the new ones. Without this guard, the
        # clear() step can cascade into _on_session_selected while
        # the list is in a half-torn-down state — on some Windows /
        # Qt6 builds that's a hard crash rather than a clean empty
        # return.
        try:
            sessions = self._all_sessions_sorted()
        except Exception:
            sessions = []
        try:
            filt = self._search.text().strip().lower()
        except RuntimeError:
            return

        list_widget = self._sessions_list
        try:
            list_widget.blockSignals(True)
            try:
                list_widget.clearSelection()
                list_widget.clear()
                for entry in sessions:
                    if filt and filt not in self._search_haystack(entry):
                        continue
                    label = self._format_session_label(entry)
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, entry)
                    list_widget.addItem(item)
            finally:
                list_widget.blockSignals(False)
        except RuntimeError:
            return

    @staticmethod
    def _all_sessions_sorted() -> list[dict]:
        # Combine SSH and Serial entries into one list. Each carries a
        # ``kind`` tag so the rest of the saved-session machinery can
        # route operations to the right settings bucket.
        sessions: list[dict] = []
        for entry in settings.get_ssh_hosts():
            entry = dict(entry)
            entry.setdefault("kind", "ssh")
            sessions.append(entry)
        for entry in settings.get_serial_hosts():
            entry = dict(entry)
            entry["kind"] = "serial"
            sessions.append(entry)
        sessions.sort(
            key=lambda s: (
                not bool(s.get("favorite")),
                (s.get("name") or s.get("host") or s.get("port") or "").lower(),
            )
        )
        return sessions

    @staticmethod
    def _search_haystack(entry: dict) -> str:
        if entry.get("kind") == "serial":
            keys = ("name", "port", "baud", "kind")
        else:
            keys = ("name", "host", "user", "auth_method", "port", "kind")
        return " ".join(str(entry.get(k, "")) for k in keys).lower()

    @staticmethod
    def _format_session_label(entry: dict) -> str:
        star = "★ " if entry.get("favorite") else "   "
        last = entry.get("last_connected", "")
        suffix = f"   · last {last}" if last else ""
        if entry.get("kind") == "serial":
            name = entry.get("name") or entry.get("port") or "serial"
            port = entry.get("port", "—")
            baud = entry.get("baud", "—")
            return f"{star}{name}   [SERIAL]\n     {port} @ {baud}{suffix}"
        name = entry.get("name") or (
            f"{entry.get('user', '')}@{entry.get('host', '')}"
        )
        host = entry.get("host", "")
        port = entry.get("port", 22)
        user = entry.get("user", "")
        return f"{star}{name}   [SSH]\n     {user}@{host}:{port}{suffix}"

    def _on_filter_changed(self, _text: str) -> None:
        self._reload_sessions()

    def _snapshot_current_entry(self) -> dict:
        """
        Copy the currently-selected saved-session dict without
        holding a long-lived QListWidgetItem wrapper. See
        _extract_selected_session_name for the rationale — same
        dangling-wrapper crash vector.
        """
        try:
            row = self._sessions_list.currentRow()
        except RuntimeError:
            return {}
        if row is None or row < 0:
            return {}
        try:
            item = self._sessions_list.item(row)
        except RuntimeError:
            return {}
        if item is None:
            return {}
        try:
            data = item.data(Qt.ItemDataRole.UserRole)
        except RuntimeError:
            data = None
        item = None  # release the wrapper immediately
        if isinstance(data, dict):
            return dict(data)
        return {}

    def _on_session_selected(self) -> None:
        entry = self._snapshot_current_entry()
        if not entry:
            return
        try:
            self._populate_form_from_entry(entry)
        except RuntimeError:
            return

    def _on_session_activated(self, _item) -> None:
        # Double-click on a saved session = open it in a new tab.
        # _item comes from Qt as a QListWidgetItem; we deliberately
        # do NOT keep it, instead re-reading the selection via the
        # row-index path so no wrapper outlives this handler.
        entry = self._snapshot_current_entry()
        if not entry:
            return
        try:
            self._populate_form_from_entry(entry)
            self._on_connect_clicked()
        except RuntimeError:
            return

    def _populate_form_from_entry(self, entry: dict) -> None:
        kind = entry.get("kind") or "ssh"
        # Switch the form to the right kind first so the user can see
        # the populated fields without an extra click.
        target_idx = 1 if kind == "serial" else 0
        try:
            if self._cmb_type.currentIndex() != target_idx:
                self._cmb_type.setCurrentIndex(target_idx)
        except RuntimeError:
            return

        self._in_name.setText(entry.get("name", ""))

        if kind == "serial":
            # Normalise on load so old saved entries that may carry a
            # descriptive label (from before the raw-name fix) get
            # cleaned up automatically.
            port = self._normalize_port_name(str(entry.get("port", "") or ""))
            self._cmb_serial_port.setEditText(port)
            self._update_serial_port_hint()
            try:
                baud = int(entry.get("baud", 115200) or 115200)
            except (TypeError, ValueError):
                baud = 115200
            self._cmb_baud.setEditText(str(baud))
            data_bits = int(entry.get("data_bits", 8) or 8)
            stop_bits = float(entry.get("stop_bits", 1.0) or 1.0)
            parity    = str(entry.get("parity", "none") or "none")
            flow      = str(entry.get("flow_control", "none") or "none")
            self._select_combo_by_data(self._cmb_data,   data_bits)
            self._select_combo_by_data(self._cmb_stop,   stop_bits)
            self._select_combo_by_data(self._cmb_parity, parity)
            self._select_combo_by_data(self._cmb_flow,   flow)
            self._select_combo_by_data(
                self._cmb_ending,
                str(entry.get("line_ending", "crlf") or "crlf"),
            )
            self._chk_local_echo.setChecked(bool(entry.get("local_echo")))

            # Auto-expand Advanced settings when the saved profile
            # uses anything other than the 8-N-1 / no-flow defaults so
            # the user can see what's loaded without an extra click.
            try:
                has_advanced = (
                    data_bits != 8
                    or abs(stop_bits - 1.0) > 0.01
                    or parity != "none"
                    or flow != "none"
                )
                self._btn_serial_advanced.setChecked(has_advanced)
            except (RuntimeError, AttributeError):
                pass
            return

        # SSH branch
        self._in_host.setText(entry.get("host", ""))
        self._set_port(entry.get("port", 22) or 22)
        self._in_user.setText(entry.get("user", ""))
        # Don't load passwords from disk — operator must re-type unless
        # save_credentials was true and a value is on file.
        if entry.get("save_credentials") and entry.get("password"):
            self._in_pass.setText(entry.get("password", ""))
        else:
            self._in_pass.clear()
        self._in_key.setText(entry.get("key_path", ""))
        self._chk_save_creds.setChecked(bool(entry.get("save_credentials")))

    @staticmethod
    def _select_combo_by_data(cmb: QComboBox, value) -> None:
        """Set ``cmb``'s current selection to the entry whose data == value."""
        try:
            for i in range(cmb.count()):
                if cmb.itemData(i) == value:
                    cmb.setCurrentIndex(i)
                    return
        except RuntimeError:
            return

    def _on_new_session(self) -> None:
        try:
            self._in_name.clear()
            for w in (self._in_host, self._in_user,
                      self._in_pass, self._in_key):
                w.clear()
            self._set_port(22)
            self._chk_save_creds.setChecked(False)
            # Reset the Serial form to defaults too so a New press
            # gives the user a clean slate regardless of which type
            # was last shown.
            self._cmb_serial_port.setEditText("")
            self._cmb_baud.setCurrentText("115200")
            self._select_combo_by_data(self._cmb_data, 8)
            self._select_combo_by_data(self._cmb_stop, 1.0)
            self._select_combo_by_data(self._cmb_parity, "none")
            self._select_combo_by_data(self._cmb_flow, "none")
            self._select_combo_by_data(self._cmb_ending, "crlf")
            self._chk_local_echo.setChecked(False)
            try:
                self._btn_serial_advanced.setChecked(False)
            except (RuntimeError, AttributeError):
                pass
            self._sessions_list.clearSelection()
            self._in_name.setFocus()
        except RuntimeError:
            return

    def _on_save_session(self) -> None:
        if self._current_kind() == "serial":
            self._save_serial_session()
        else:
            self._save_ssh_session()

    def _save_ssh_session(self) -> None:
        try:
            host = self._in_host.text().strip()
        except RuntimeError:
            return
        if not host:
            QMessageBox.warning(self, "Save session", "Host is required.")
            return
        port = self._validate_port()
        if port is None:
            return
        try:
            name = self._in_name.text().strip() or (
                f"{self._in_user.text().strip() or 'host'}@{host}"
            )
            self._in_name.setText(name)
            save_creds = self._chk_save_creds.isChecked()
        except RuntimeError:
            return

        try:
            existing = next(
                (s for s in settings.get_ssh_hosts() if s.get("name") == name),
                {},
            )
            entry = {
                "kind":     "ssh",
                "name":     name,
                "host":     host,
                "port":     port,
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
        except Exception as exc:
            try:
                QMessageBox.warning(
                    self.window() or self,
                    "Save session",
                    f"Could not save '{name}': {exc}",
                )
            except Exception:
                pass
            return
        try:
            self._reload_sessions()
            self.status_message.emit(f"Saved session '{name}'")
        except RuntimeError:
            return

    def _save_serial_session(self) -> None:
        # Save uses the canonical raw device name, never the label —
        # otherwise an old saved profile would carry the descriptive
        # text and fail to open on next load.
        try:
            port = self._serial_port_value()
        except RuntimeError:
            return
        if not port:
            QMessageBox.warning(
                self, "Save session", "COM / serial port is required.",
            )
            return
        profile = self._serial_profile_from_form()
        if profile is None:
            return
        try:
            name = self._in_name.text().strip() or port
            self._in_name.setText(name)
        except RuntimeError:
            return
        try:
            existing = next(
                (s for s in settings.get_serial_hosts() if s.get("name") == name),
                {},
            )
            entry = profile.to_dict()
            entry["name"] = name
            entry["favorite"] = existing.get("favorite", False)
            entry["last_connected"] = existing.get("last_connected", "")
            settings.save_serial_host(entry)
        except Exception as exc:
            try:
                QMessageBox.warning(
                    self.window() or self,
                    "Save session",
                    f"Could not save '{name}': {exc}",
                )
            except Exception:
                pass
            return
        try:
            self._reload_sessions()
            self.status_message.emit(f"Saved Serial session '{name}'")
        except RuntimeError:
            return

    def _extract_selected_session_name(self) -> str:
        """
        Read the name of the currently-selected saved session into a
        plain Python string WITHOUT holding a long-lived
        QListWidgetItem wrapper.
        """
        ident = self._extract_selected_session_ident()
        return ident[0] if ident else ""

    def _extract_selected_session_ident(self) -> tuple[str, str]:
        """
        Read the (name, kind) of the currently-selected saved session.

        Two-tuple instead of a one-string return so delete / pin can
        route to the right settings bucket without re-snapshotting.
        The item wrapper is created transiently inside this method
        and dropped before returning, so no dangling sip wrapper
        survives past the call — important because _reload_sessions()
        later calls list.clear() which destroys the C++ side of every
        QListWidgetItem, and cleaning up a dangling Python wrapper to
        a destroyed Qt-owned item is a known segfault vector on
        Windows/PyQt6.
        """
        try:
            row = self._sessions_list.currentRow()
        except RuntimeError:
            return ("", "")
        if row is None or row < 0:
            return ("", "")
        try:
            item = self._sessions_list.item(row)
        except RuntimeError:
            return ("", "")
        if item is None:
            return ("", "")
        try:
            data = item.data(Qt.ItemDataRole.UserRole)
        except RuntimeError:
            data = None
        # Drop the Python wrapper immediately — Python's refcount
        # will release it before this function returns, so later
        # list.clear() cannot leave a dangling wrapper behind.
        item = None
        if not isinstance(data, dict):
            return ("", "")
        return (
            str(data.get("name", "")),
            str(data.get("kind", "ssh")) or "ssh",
        )

    def _on_delete_session(self) -> None:
        # Re-entry guard. QMessageBox.question spins a nested event
        # loop, and Qt can deliver queued signals during that time —
        # including another clicked() on the same Delete button if
        # the user mashes it. Without this flag the second invocation
        # runs inside the first one's modal and corrupts the list.
        if self._delete_in_flight:
            return

        name, kind = self._extract_selected_session_ident()
        if not name:
            return

        self._delete_in_flight = True
        try:
            try:
                self._btn_delete.setEnabled(False)
            except RuntimeError:
                return
            try:
                confirm = QMessageBox.question(
                    self.window() or self,
                    "Delete session",
                    f"Remove saved session '{name}'?",
                )
            except Exception:
                confirm = QMessageBox.StandardButton.No
        finally:
            try:
                self._btn_delete.setEnabled(True)
            except RuntimeError:
                pass
            self._delete_in_flight = False

        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Bounce the actual delete + UI refresh onto the NEXT event
        # loop iteration. The click-slot stack is still unwinding at
        # this point (Qt's signal-slot dispatcher is inside our
        # call), and any list mutation right now can race with:
        #   • residual modal-dialog cleanup on the dialog's parent
        #   • motion-filter ripple overlays on the Delete button
        #   • sip reaping of any QListWidgetItem wrappers still
        #     referenced on the stack above us
        # Running via QTimer.singleShot(0, ...) guarantees we're in a
        # fresh stack frame with no such ambient state.
        QTimer.singleShot(
            0, lambda n=name, k=kind: self._finalize_delete(n, k)
        )

    def _finalize_delete(self, name: str, kind: str = "ssh") -> None:
        """
        Perform the actual persistent delete + UI refresh. Called via
        QTimer.singleShot from _on_delete_session, so it always runs
        in a clean event loop iteration.
        """
        if not name:
            return
        try:
            if (kind or "ssh") == "serial":
                settings.delete_serial_host(name)
            else:
                settings.delete_ssh_host(name)
        except Exception as exc:
            try:
                QMessageBox.warning(
                    self.window() or self,
                    "Delete session",
                    f"Could not remove '{name}': {exc}",
                )
            except Exception:
                pass
            return
        try:
            self._reload_sessions()
        except RuntimeError:
            return
        try:
            self.status_message.emit(f"Removed saved session '{name}'")
        except RuntimeError:
            return

    def _on_toggle_pin(self) -> None:
        entry = self._snapshot_current_entry()
        if not entry:
            return
        name = entry.get("name", "")
        if not name:
            return
        entry["favorite"] = not bool(entry.get("favorite"))
        try:
            if (entry.get("kind") or "ssh") == "serial":
                settings.save_serial_host(entry)
            else:
                settings.save_ssh_host(entry)
        except Exception:
            return
        # Bounce the reload + re-selection so it runs in a fresh
        # event loop iteration — same rationale as _on_delete_session.
        QTimer.singleShot(0, lambda n=name: self._reselect_after_reload(n))

    def _reselect_after_reload(self, name: str) -> None:
        try:
            self._reload_sessions()
        except RuntimeError:
            return
        # Re-select the same item by name. Iterate using row indices
        # and drop each transient wrapper before touching the next.
        try:
            count = self._sessions_list.count()
        except RuntimeError:
            return
        for i in range(count):
            try:
                it = self._sessions_list.item(i)
                if it is None:
                    continue
                data = it.data(Qt.ItemDataRole.UserRole)
            except RuntimeError:
                return
            if isinstance(data, dict) and data.get("name") == name:
                try:
                    self._sessions_list.setCurrentRow(i)
                except RuntimeError:
                    return
                return

    def _infer_auth_method(self) -> str:
        if self._in_key.text().strip():
            return "key"
        if self._in_pass.text():
            return "password"
        return "agent"

    def _record_last_connected(self, profile) -> None:
        """
        Update the timestamp on a saved session matching this profile
        name (if any). Routes to the SSH or Serial bucket based on the
        concrete profile type.
        """
        if not getattr(profile, "name", ""):
            return
        if isinstance(profile, SerialProfile):
            store_get = settings.get_serial_hosts
            store_save = settings.save_serial_host
        else:
            store_get = settings.get_ssh_hosts
            store_save = settings.save_ssh_host
        existing = next(
            (s for s in store_get() if s.get("name") == profile.name),
            None,
        )
        if existing is None:
            return
        existing = dict(existing)
        existing["last_connected"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            store_save(existing)
        except Exception:
            return
        self._reload_sessions()

    # ── Connect / quick connect / duplicate ─────────────────────────────────

    # ── Port field helpers ──────────────────────────────────────────────────

    def _read_port(self, *, default: int = 22) -> int:
        """
        Parse the port field's current text into a valid integer.

        Returns ``default`` when the field is empty — empty is a valid
        mid-edit state that must never crash the caller. The validator
        guarantees any non-empty value is an int in [1, 65535], but we
        still wrap int() in a try/except as a defence against a future
        validator swap.
        """
        text = (self._in_port.text() or "").strip()
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            return default
        if value < 1 or value > 65535:
            return default
        return value

    def _validate_port(self) -> Optional[int]:
        """
        Read-and-validate the port for a commit operation (Save / Connect).

        Returns the port as int, or ``None`` if the current text does
        not name a valid port in [1, 65535]. The caller is responsible
        for surfacing a warning and aborting the commit. This is the
        only place where the port is strictly validated — editing
        states remain fully permissive.
        """
        text = (self._in_port.text() or "").strip()
        if not text:
            QMessageBox.warning(
                self, "SSH",
                "Port is required — enter a value between 1 and 65535.",
            )
            return None
        try:
            value = int(text)
        except ValueError:
            QMessageBox.warning(
                self, "SSH",
                f"Port must be a whole number — got {text!r}.",
            )
            return None
        if value < 1 or value > 65535:
            QMessageBox.warning(
                self, "SSH",
                f"Port must be in the range 1..65535 — got {value}.",
            )
            return None
        return value

    def _set_port(self, value) -> None:
        """Safely write a port value into the field."""
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = 22
        if n < 1 or n > 65535:
            n = 22
        self._in_port.setText(str(n))

    def _profile_from_form(self):
        """
        Build the profile object the active form describes.

        Returns an :class:`SSHProfile` or :class:`SerialProfile`
        depending on the type combobox, or ``None`` if any required
        field is missing or invalid (caller already saw the warning).
        """
        if self._current_kind() == "serial":
            return self._serial_profile_from_form()
        return self._ssh_profile_from_form()

    def _ssh_profile_from_form(self) -> Optional[SSHProfile]:
        host = self._in_host.text().strip()
        user = self._in_user.text().strip()
        if not host:
            QMessageBox.warning(self, "SSH", "Host is required.")
            return None
        if not user:
            QMessageBox.warning(self, "SSH", "User is required.")
            return None
        port = self._validate_port()
        if port is None:
            return None
        return SSHProfile(
            name=self._in_name.text().strip(),
            host=host,
            port=port,
            user=user,
            password=self._in_pass.text(),
            key_path=self._in_key.text().strip(),
        )

    def _serial_profile_from_form(self) -> Optional[SerialProfile]:
        # Always resolve the raw OS device name. Without this, picking
        # "COM3 — Silicon Labs CP210x..." from the dropdown would feed
        # the descriptive label to pyserial and the open call would
        # fail — that's the exact bug PuTTY users would not see
        # because PuTTY shows raw port names directly.
        port = self._serial_port_value()
        if not port:
            QMessageBox.warning(self, "Serial", "COM / serial port is required.")
            return None
        try:
            baud = int((self._cmb_baud.currentText() or "0").strip())
        except ValueError:
            QMessageBox.warning(self, "Serial", "Baud rate must be a whole number.")
            return None
        if baud <= 0:
            QMessageBox.warning(
                self, "Serial",
                f"Baud rate must be a positive integer — got {baud}.",
            )
            return None
        try:
            data_bits = int(self._cmb_data.currentData() or 8)
            stop_bits = float(self._cmb_stop.currentData() or 1.0)
            parity    = str(self._cmb_parity.currentData() or "none")
            flow      = str(self._cmb_flow.currentData() or "none")
            ending    = str(self._cmb_ending.currentData() or "crlf")
        except RuntimeError:
            return None
        return SerialProfile(
            name=self._in_name.text().strip(),
            port=port,
            baud=baud,
            data_bits=data_bits,
            stop_bits=stop_bits,
            parity=parity,
            flow_control=flow,
            line_ending=ending,
            local_echo=bool(self._chk_local_echo.isChecked()),
        )

    def _on_connect_clicked(self) -> None:
        profile = self._profile_from_form()
        if profile is None:
            return
        if isinstance(profile, SerialProfile):
            if not HAS_PYSERIAL:
                QMessageBox.critical(
                    self, "Serial unavailable",
                    "pyserial is not installed.\n\nRun: pip install pyserial",
                )
                return
        else:
            if not HAS_PARAMIKO:
                QMessageBox.critical(
                    self, "SSH unavailable",
                    "paramiko is not installed.\n\nRun: pip install paramiko",
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

    def _open_session_tab(self, profile) -> None:
        # Defend against any failure to construct the tab or wire it
        # up — a crash here used to take the whole window with it.
        is_serial = isinstance(profile, SerialProfile)
        kind_label = "Serial" if is_serial else "SSH"
        try:
            tab = SshSessionTab(profile, self)
        except Exception as exc:
            QMessageBox.critical(
                self, kind_label,
                f"Could not create {kind_label} session tab:\n{exc}",
            )
            return

        try:
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

            # If the workspace is already in focus mode, hide the new
            # tab's header immediately so it matches the existing
            # layout instead of flashing in with a visible header.
            if self._terminal_focus_mode:
                try:
                    tab.set_focus_mode(True)
                except Exception:
                    pass

            # Reflect the new tab in the workspace state.
            self._refresh_workspace_visibility()
            self._update_session_count()
            try:
                self.sessions_changed.emit()
            except Exception:
                pass

            # Begin the connection. If this somehow raises, the tab
            # is already in the QTabWidget so we still need to close
            # it cleanly.
            tab.start_connection()
        except Exception as exc:
            try:
                tab.shutdown()
            except Exception:
                pass
            try:
                ix = self._tabs.indexOf(tab)
                if ix >= 0:
                    self._tabs.removeTab(ix)
            except Exception:
                pass
            try:
                tab.deleteLater()
            except Exception:
                pass
            QMessageBox.critical(
                self, "SSH",
                f"Failed to start SSH session:\n{exc}",
            )
            self._refresh_workspace_visibility()
            self._update_session_count()
            return

        # Optional: collapse the form once the user actually opens a tab
        # so the workspace gets more vertical room.
        if self._tabs.count() == 1:
            try:
                self._conn_section.set_collapsed(True)
            except Exception:
                pass

        try:
            self._record_last_connected(profile)
        except Exception:
            pass

    def _on_tab_close_requested(self, idx: int) -> None:
        if idx < 0 or idx >= self._tabs.count():
            return
        widget = self._tabs.widget(idx)
        # Shut the SSH session down BEFORE we remove the tab from the
        # QTabWidget. Shutting down first makes sure the connect
        # worker's signals are invalidated and the reader thread is
        # told to stop, so Qt is free to delete the widget without
        # any thread still trying to poke it.
        if isinstance(widget, SshSessionTab):
            try:
                widget.shutdown()
            except Exception:
                pass
        try:
            self._tabs.removeTab(idx)
        except Exception:
            pass
        if widget is not None:
            try:
                widget.deleteLater()
            except Exception:
                pass
        try:
            self._refresh_workspace_visibility()
            self._update_session_count()
        except Exception:
            pass
        try:
            self.sessions_changed.emit()
        except Exception:
            pass
        if self._tabs.count() == 0:
            # Auto-exit focus mode when the last tab goes away — the
            # corner toggle lives on the tab bar and would disappear
            # with it, leaving the user unable to find the exit. We
            # exit before touching the other widgets so restoring the
            # chrome and re-expanding the form happens in one pass.
            if self._terminal_focus_mode:
                try:
                    self.set_terminal_focus_mode(False)
                except Exception:
                    pass
            try:
                self._lbl_session_state.setText("No active session")
                self._dot.set_active(False)
                self._dot.set_color(theme().text_dim)
                # Re-expand the form so the user can immediately compose a
                # new connection.
                self._conn_section.set_collapsed(False)
            except Exception:
                pass

    def _on_tab_rename(self, idx: int) -> None:
        if idx < 0:
            return
        try:
            widget = self._tabs.widget(idx)
        except RuntimeError:
            return
        if not isinstance(widget, SshSessionTab):
            return
        current = widget.profile.name or widget.title_text()
        new_name, ok = QInputDialog.getText(
            self, "Rename session", "New name:", text=current
        )
        if ok and new_name.strip():
            try:
                widget.set_title(new_name.strip())
                self._tabs.setTabText(idx, widget.title_text())
            except RuntimeError:
                return

    def _on_tab_state_changed(self, tab: SshSessionTab, state: str) -> None:
        # Every step is wrapped — this is a slot fired from a Qt signal
        # that may dispatch during the tab's own teardown, and we never
        # want a transient widget-destruction or attribute-mismatch to
        # propagate up into Qt's signal dispatcher.
        try:
            idx = self._tabs.indexOf(tab)
        except (RuntimeError, AttributeError):
            return
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
        try:
            self._tabs.tabBar().setTabTextColor(idx, QColor(color))
        except (RuntimeError, AttributeError):
            return

        # Mirror the active tab's state in the bottom-left status block.
        # Catch *every* exception here — _mirror_active_state used to
        # blow up with AttributeError on Serial profiles because it
        # accessed ``profile.host`` directly, and a slot exception
        # cascades back through Qt's dispatch chain into a hard crash
        # on some Qt6 builds.
        try:
            if idx == self._tabs.currentIndex():
                self._mirror_active_state(tab, state)
        except Exception:
            pass

        # Notify downstream listeners (File Transfer page) that a
        # session has transitioned. Wrapped in try so a listener
        # raising can't disturb the tab's state machine.
        try:
            self.sessions_changed.emit()
        except Exception:
            pass

    def _on_tab_title_changed(self, tab: SshSessionTab, title: str) -> None:
        idx = self._tabs.indexOf(tab)
        if idx < 0:
            return
        self._tabs.setTabText(idx, title)
        self._tabs.setTabToolTip(idx, tab.summary_text())
        try:
            self.sessions_changed.emit()
        except Exception:
            pass

    def _on_active_tab_changed(self, idx: int) -> None:
        if idx < 0:
            try:
                self._lbl_session_state.setText("No active session")
                self._dot.set_active(False)
                self._dot.set_color(theme().text_dim)
            except Exception:
                pass
            return
        try:
            widget = self._tabs.widget(idx)
        except RuntimeError:
            return
        if isinstance(widget, SshSessionTab):
            try:
                self._mirror_active_state(widget, widget.state())
                widget.terminal.setFocus()
            except RuntimeError:
                return

    def _mirror_active_state(self, tab: SshSessionTab, state: str) -> None:
        """
        Reflect ``tab``'s state in the bottom-left status block.

        Uses ``tab.summary_text()`` and ``tab.title_text()`` instead of
        digging into the profile directly — those helpers already
        branch on SSH vs Serial, so this method works for both profile
        types without any isinstance check.

        Wrapped in defensive try/except because the QLabel could be
        torn down between a state_changed emission and our handler
        running, and a stale Python wrapper would raise RuntimeError on
        setText.
        """
        t = theme()
        try:
            if state == STATE_CONNECTED:
                self._dot.set_active(True, color=t.green)
                self._lbl_session_state.setText(
                    f"Connected · {tab.summary_text()}"
                )
            elif state == STATE_CONNECTING:
                self._dot.set_active(True, color=t.amber)
                self._lbl_session_state.setText(
                    f"Connecting to {tab.title_text()}…"
                )
            elif state == STATE_FAILED:
                self._dot.set_active(False)
                self._dot.set_color(t.red)
                self._lbl_session_state.setText("Connection failed")
            else:
                self._dot.set_active(False)
                self._dot.set_color(t.text_dim)
                self._lbl_session_state.setText("Disconnected")
        except (RuntimeError, AttributeError):
            return

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

        # Description hint label under the COM port row.
        try:
            self._lbl_serial_hint.setStyleSheet(
                f"#ssh_serial_hint {{"
                f"  color: {t.text_dim};"
                f"  font-size: 10px;"
                f"  font-family: 'Consolas', monospace;"
                f"  background: transparent;"
                f"  padding: 0px;"
                f"}}"
            )
        except (RuntimeError, AttributeError):
            pass

        # Advanced-settings toggle inside the Serial form. Plain text
        # button with a chevron prefix; the chevron flips ▸/▾ via the
        # toggled handler, so the QSS only needs to handle colours.
        try:
            self._btn_serial_advanced.setStyleSheet(
                f"QToolButton#ssh_serial_adv_toggle {{"
                f"  background: transparent;"
                f"  color: {t.text_dim};"
                f"  border: none;"
                f"  text-align: left;"
                f"  padding: 4px 2px;"
                f"  font-size: 11px;"
                f"  font-weight: 700;"
                f"  letter-spacing: 0.6px;"
                f"}}"
                f"QToolButton#ssh_serial_adv_toggle:hover {{"
                f"  color: {t.accent};"
                f"}}"
                f"QToolButton#ssh_serial_adv_toggle:checked {{"
                f"  color: {t.accent};"
                f"}}"
            )
        except (RuntimeError, AttributeError):
            pass

        # Focus-mode corner toggle. The checked state uses the accent
        # colour so the user can tell at a glance whether the mode is
        # currently active — paired with the tooltip that also swaps
        # between "Enter…" / "Exit…".
        self._btn_focus_toggle.setStyleSheet(
            f"QToolButton#ssh_focus_toggle {{"
            f"  background-color: {t.bg_raised};"
            f"  color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt};"
            f"  border-radius: 5px;"
            f"  font-size: 14px;"
            f"  font-weight: 800;"
            f"  padding: 0 2px;"
            f"  margin: 3px 4px 3px 0;"
            f"}}"
            f"QToolButton#ssh_focus_toggle:hover {{"
            f"  background-color: {t.accent_bg};"
            f"  border-color: {t.accent};"
            f"  color: {t.accent};"
            f"}}"
            f"QToolButton#ssh_focus_toggle:checked {{"
            f"  background-color: {t.accent_bg};"
            f"  border-color: {t.accent};"
            f"  color: {t.accent};"
            f"}}"
            f"QToolButton#ssh_focus_toggle:checked:hover {{"
            f"  background-color: {t.bg_hover};"
            f"  border-color: {t.accent};"
            f"  color: {t.accent};"
            f"}}"
        )

    def _show_paramiko_warning(self) -> None:
        self._btn_connect.setEnabled(False)
        self._btn_connect.setToolTip(
            "Install paramiko to enable SSH (pip install paramiko)"
        )
        self._btn_quick_go.setEnabled(False)

    # ── Cleanup ─────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        # Tear every tab down explicitly so any worker threads /
        # reader threads are told to stop before Qt begins deleting
        # the widget tree. Defensive — the main window's closeEvent
        # calls us, but we must not propagate any crash up into it.
        try:
            count = self._tabs.count()
        except Exception:
            return
        for i in range(count - 1, -1, -1):
            try:
                widget = self._tabs.widget(i)
            except Exception:
                widget = None
            if isinstance(widget, SshSessionTab):
                try:
                    widget.shutdown()
                except Exception:
                    pass
            try:
                self._tabs.removeTab(i)
            except Exception:
                pass
            if widget is not None:
                try:
                    widget.deleteLater()
                except Exception:
                    pass
