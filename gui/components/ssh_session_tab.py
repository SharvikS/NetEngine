"""
Single connection tab (SSH or Serial/UART).

Each tab is a self-contained mini-workspace:

  * a header strip with status dot, friendly title, the
    `user@host:port` (SSH) or `COM3 @ 115200 8N1` (Serial) summary,
    an inline status label and per-session Reconnect / Disconnect
    buttons
  * an embedded `TerminalWidget` bound to either a paramiko
    `SSHSession` or a pyserial-backed `SerialSession`, both of which
    expose the same ``is_open / send / read_loop / resize / close``
    surface so the terminal widget can drive either transport
    identically

The class is named ``SshSessionTab`` for backwards-compatibility but
it is the single tab type for both connection kinds — the profile it
is constructed with chooses the backend.

Tabs are independent — closing or reconnecting one never touches the
others, and each tab keeps its own command history (via the underlying
TerminalWidget) and connection log.

Lifecycle & crash safety
------------------------
The tab is driven by an explicit state machine:

    IDLE → CONNECTING → CONNECTED → CLOSED
                   ↘ FAILED ↗ (user may Reconnect)

Every transition is guarded:

* Only one connect worker can be in flight at a time — a monotonic
  ``_connect_token`` is stamped on each attempt and ignored in the
  result slots if it no longer matches.
* Once ``shutdown()`` runs, ``_destroyed`` is set and every callback,
  slot, and button handler early-returns. Any connect worker still
  inside paramiko's blocking ``client.connect()`` is allowed to finish
  naturally; its result signal is dropped because the token no longer
  matches and ``_destroyed`` is set. No signal ever lands on a tab
  whose QObject has been torn down.
* ``disconnect_session()`` is idempotent and safe to call from any
  state. Reconnect serialises through ``disconnect_session`` →
  ``start_connection`` without ever racing the two phases against
  each other.
* The UI-state guards (buttons disabled, state transitions) are the
  **single** source of truth for "is a connect action allowed right
  now" — the handlers never rely on button state alone.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from gui.components.terminal_widget import TerminalWidget
from gui.components.live_widgets import StatusDot
from gui.themes import theme, ThemeManager
from scanner.ssh_client import (
    SSHProfile, SSHSession, HAS_PARAMIKO, friendly_error,
)
from scanner.serial_client import (
    SerialProfile, SerialSession, HAS_PYSERIAL,
)


# State machine for one tab — drives the header strip and the parent
# tab bar's color/title/icon. Kept as bare strings so the parent view
# doesn't need to import this enum.
STATE_IDLE        = "idle"
STATE_CONNECTING  = "connecting"
STATE_CONNECTED   = "connected"
STATE_FAILED      = "failed"
STATE_CLOSED      = "closed"
STATE_DESTROYED   = "destroyed"


class SshSessionTab(QWidget):
    """
    A single connection tab — SSH or Serial — shown in the workspace.

    Construct with either an :class:`SSHProfile` or a
    :class:`SerialProfile`. The tab inspects the profile type and
    routes to the appropriate backend; everything else (state machine,
    UI scaffolding, terminal bridge) is shared.
    """

    # Outward signals — the parent SSH view listens to these to update
    # the QTabWidget tab title, color and any per-tab status badge.
    state_changed   = pyqtSignal(str)            # one of the STATE_* constants
    title_changed   = pyqtSignal(str)            # new tab title
    log_appended    = pyqtSignal(str)            # short status line for the parent log
    closed          = pyqtSignal()               # session is fully torn down

    # Internal cross-thread bridges. Each carries the connect token so
    # the GUI thread can discard stale worker results. Session payload
    # is typed as object because it can be either an SSHSession or a
    # SerialSession.
    _connect_failed_sig    = pyqtSignal(int, str)
    _connect_succeeded_sig = pyqtSignal(int, object)  # token, session instance

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        # Cache the profile kind once at construction. Both profile
        # types carry an explicit ``kind`` attribute on SerialProfile;
        # SSHProfile does not, so we default unknowns to "ssh".
        self._is_serial: bool = isinstance(profile, SerialProfile)
        self._state = STATE_IDLE
        self._session = None
        self._worker: Optional[threading.Thread] = None
        self._opened_at: Optional[datetime] = None

        # Monotonic token bumped on every connect attempt. Worker
        # result slots compare against this so a late success/fail
        # from a cancelled attempt is dropped on the floor instead of
        # clobbering the current state.
        self._connect_token: int = 0

        # Set by shutdown() — every handler short-circuits once true
        # so no signal or click can touch a tab that's about to be
        # destroyed.
        self._destroyed: bool = False

        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Cross-thread bridges
        self._connect_failed_sig.connect(self._on_connect_failed)
        self._connect_succeeded_sig.connect(self._on_connect_succeeded)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header strip ─────────────────────────────────────────────────
        self._header = QFrame()
        self._header.setObjectName("session_tab_header")
        self._header.setFixedHeight(46)

        head_lay = QHBoxLayout(self._header)
        head_lay.setContentsMargins(16, 0, 14, 0)
        head_lay.setSpacing(10)

        self._dot = StatusDot(size=10)
        head_lay.addWidget(self._dot)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.setContentsMargins(0, 4, 0, 4)

        self._title_lbl = QLabel(self.title_text())
        self._title_lbl.setObjectName("session_tab_title")
        title_box.addWidget(self._title_lbl)

        self._sub_lbl = QLabel(self.summary_text())
        self._sub_lbl.setObjectName("session_tab_subtitle")
        title_box.addWidget(self._sub_lbl)

        head_lay.addLayout(title_box, stretch=1)

        self._state_lbl = QLabel("Idle")
        self._state_lbl.setObjectName("session_tab_state")
        head_lay.addWidget(self._state_lbl)

        self._btn_reconnect = QPushButton("Reconnect")
        self._btn_reconnect.setObjectName("btn_action")
        self._btn_reconnect.setMinimumHeight(30)
        self._btn_reconnect.setEnabled(False)
        self._btn_reconnect.clicked.connect(self._on_reconnect_clicked)
        head_lay.addWidget(self._btn_reconnect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setObjectName("btn_danger")
        self._btn_disconnect.setMinimumHeight(30)
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._on_disconnect_clicked)
        head_lay.addWidget(self._btn_disconnect)

        root.addWidget(self._header)

        # Header divider
        self._head_div = QFrame()
        self._head_div.setFrameShape(QFrame.Shape.HLine)
        self._head_div.setFixedHeight(1)
        root.addWidget(self._head_div)

        # ── Terminal ─────────────────────────────────────────────────────
        # `ssh_only=True` means the terminal starts in idle mode with
        # no welcome banner, no local prompt, and no automatic fallback
        # to local shell on SSH detach. This tab owns the content; the
        # TerminalWidget just renders what we ask it to.
        self.terminal = TerminalWidget(self, ssh_only=True)
        self.terminal.session_closed.connect(self._on_terminal_session_closed)
        root.addWidget(self.terminal, stretch=1)

    # ── Public API ───────────────────────────────────────────────────────────

    def title_text(self) -> str:
        if self.profile.name:
            return self.profile.name
        if self._is_serial:
            return f"{self.profile.port or 'serial'} @ {self.profile.baud}"
        if self.profile.user and self.profile.host:
            return f"{self.profile.user}@{self.profile.host}"
        return self.profile.host or "session"

    def summary_text(self) -> str:
        if self._is_serial:
            return self.profile.summary()
        bits = []
        if self.profile.user:
            bits.append(self.profile.user + "@")
        bits.append(self.profile.host or "—")
        if self.profile.port and int(self.profile.port) != 22:
            bits.append(":" + str(self.profile.port))
        return "".join(bits)

    def _log_target(self) -> str:
        """Short ``user@host`` (SSH) or ``COM3`` (Serial) used in log lines."""
        if self._is_serial:
            return self.profile.port or "serial"
        return f"{self.profile.user}@{self.profile.host}"

    def state(self) -> str:
        return self._state

    def set_title(self, new_name: str) -> None:
        """Rename this session (used when the user double-clicks the tab)."""
        if self._destroyed:
            return
        new_name = (new_name or "").strip()
        if not new_name:
            return
        self.profile.name = new_name
        self._title_lbl.setText(self.title_text())
        self.title_changed.emit(self.title_text())

    def set_focus_mode(self, on: bool) -> None:
        """
        Toggle the tab's terminal-focus layout.

        In focus mode the session-tab header strip + its divider are
        hidden so the embedded terminal claims the full area. The
        header carries reconnect/disconnect buttons and the state
        label; in focus mode the QTabWidget tab bar (with its close
        button) stays visible and the SSHView-level toggle is the
        escape hatch, so losing the in-tab controls is acceptable.

        The terminal keeps its SSH session attached — nothing is
        detached or rebuilt. Exiting focus mode just re-shows the
        header; the active shell, buffer, and SSH channel are
        untouched across the transition.
        """
        if self._destroyed:
            return
        try:
            self._header.setVisible(not on)
            self._head_div.setVisible(not on)
        except RuntimeError:
            # Widget torn down mid-transition — harmless.
            return

    def start_connection(self) -> None:
        """
        Spin up the SSH connection on a worker thread.

        Guarded against:

        * double-click / rapid spam — if the tab is already in
          ``CONNECTING`` state or has a live session, the call is a
          no-op.
        * shutdown in progress — once ``_destroyed`` is set no new
          connect is ever started.
        * paramiko missing — reports a clean FAILED state instead of
          attempting the connect.

        Pre-connect reset is *silent*: the previous session (if any) is
        torn down without writing a "[ssh session closed]" line, and
        the buffer is wiped so the new connection starts from a clean
        slate. The only output the user sees is the single
        "[connecting to …]" line and, on success, the "[connected …]"
        banner from `attach_ssh`.
        """
        if self._destroyed:
            return

        if self._is_serial:
            if not HAS_PYSERIAL:
                self._set_state(
                    STATE_FAILED,
                    "pyserial not installed — pip install pyserial",
                )
                try:
                    self.terminal._append(
                        "[pyserial is not installed — Serial unavailable. "
                        "Run: pip install pyserial]\n"
                    )
                except Exception:
                    pass
                return
        else:
            if not HAS_PARAMIKO:
                self._set_state(
                    STATE_FAILED,
                    "paramiko not installed — pip install paramiko",
                )
                try:
                    self.terminal._append(
                        "[paramiko is not installed — SSH unavailable. "
                        "Run: pip install paramiko]\n"
                    )
                except Exception:
                    pass
                return

        # Disallow starting a new connect while one is already in
        # progress or already connected. Reconnect goes through
        # ``reconnect()`` which tears the old one down first.
        if self._state in (STATE_CONNECTING, STATE_CONNECTED):
            return

        # Silent pre-connect reset. detach_ssh(silent=True) never
        # writes to the buffer and never emits session_closed, so
        # there is no intermediate local-prompt flash and no stale
        # CLOSED state transition to race with the CONNECTING
        # transition below.
        try:
            self.terminal.detach_ssh(silent=True)
            self.terminal.clear()
            if self._is_serial:
                self.terminal._append(
                    f"[opening {self.profile.port or '?'} @ "
                    f"{self.profile.baud}…]\n"
                )
            else:
                self.terminal._append(
                    f"[connecting to {self.profile.user or '?'}@"
                    f"{self.profile.host}:{self.profile.port}…]\n"
                )
        except Exception:
            pass

        self._set_state(STATE_CONNECTING, "Connecting…")
        self._btn_reconnect.setEnabled(False)
        self._btn_disconnect.setEnabled(True)

        # Bump the token *before* starting the worker — any in-flight
        # worker from a previous attempt will now be stale and its
        # results will be discarded when they reach the GUI thread.
        self._connect_token += 1
        token = self._connect_token

        self._session = None
        if self._is_serial:
            worker_name = f"serial-open-{self.profile.port}-{token}"
        else:
            worker_name = f"ssh-connect-{self.profile.host}-{token}"
        worker = threading.Thread(
            target=self._connect_worker,
            args=(self.profile, token),
            daemon=True,
            name=worker_name,
        )
        self._worker = worker
        worker.start()

    def reconnect(self) -> None:
        """
        Silently tear down the current session and start a new one.
        No "[ssh session closed]" noise between the old and new
        connection — just the new "[connecting…]" / "[connected…]"
        pair.

        Guarded against spamming: a reconnect while already in
        CONNECTING state cancels the outstanding attempt (its token
        is bumped to stale) before launching the new one.
        """
        if self._destroyed:
            return
        self.disconnect_session(silent=True)
        self.start_connection()

    def disconnect_session(self, *, silent: bool = False) -> None:
        """
        Drop the current SSH session.

        Idempotent — safe to call from any state, including during
        shutdown or when no session has ever been started. Will also
        invalidate any in-flight connect worker so its result is
        dropped when it eventually returns.

        Parameters
        ----------
        silent : bool, default False
            * ``False`` (default, user-initiated) — shows a single
              "[ssh session closed]" line in the terminal so the
              operator has a visible confirmation, and emits
              ``session_closed`` from the terminal which drives the
              tab state to ``STATE_CLOSED``.
            * ``True`` (reconnect / shutdown / pre-connect) — tears
              down the channel quietly with no output and no
              ``session_closed`` emission. The caller owns the next
              state transition.
        """
        was_connecting = (self._state == STATE_CONNECTING)
        was_connected  = (self._state == STATE_CONNECTED)

        # Invalidate any pending connect worker. Its result slot will
        # check the token, see it doesn't match, and drop the result.
        # This is how we cancel a blocking paramiko connect without
        # actually unblocking the worker — we just ignore its output.
        self._connect_token += 1

        session = self._session
        self._session = None

        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        # Detach the terminal from whatever it was connected to. This
        # must come after we null out ``_session`` so the terminal
        # can't observe a half-dead reference. detach_ssh is
        # idempotent too — a no-op if the terminal already detached
        # itself on remote closure.
        try:
            self.terminal.detach_ssh(silent=silent)
        except Exception:
            pass

        # Handle cancel-during-connect: terminal.detach_ssh(silent=False)
        # would be a no-op here because the terminal is still in idle
        # mode (attach_ssh hasn't run yet), so session_closed never
        # fires and _on_terminal_session_closed can't move us out of
        # CONNECTING. Drive the transition explicitly instead so the
        # UI doesn't get stuck on "Connecting…".
        if was_connecting and not silent and not self._destroyed:
            try:
                self.terminal._append("\n[connect cancelled]\n")
            except Exception:
                pass
            self._set_state(STATE_CLOSED, "Cancelled")
            try:
                self._btn_reconnect.setEnabled(True)
                self._btn_disconnect.setEnabled(False)
            except Exception:
                pass
            self.log_appended.emit(f"CANCELLED  {self._log_target()}")
            return

        # Silent cancel-during-connect (reconnect/shutdown): don't
        # write anything, just let the caller drive the next state.
        # But we must still reset the button state so a shutdown
        # doesn't leave the widget in a half-disabled state.
        if was_connecting and silent:
            # Let start_connection (our caller on the reconnect path)
            # transition us forward. Button state already correct.
            return

        # Cancel-during-CONNECTED (silent reconnect) — let the caller
        # drive the next transition. For user-initiated disconnects
        # from CONNECTED, the terminal's session_closed signal will
        # fire from detach_ssh(silent=False) and land in
        # _on_terminal_session_closed.
        _ = was_connected  # retained for readability

    def shutdown(self) -> None:
        """
        Called by the parent view before destroying the tab. Always
        silent — the tab's buffer is about to be thrown away.

        After this call the tab:
          * accepts no new connect / disconnect / reconnect clicks
          * drops every worker signal (token bumped, _destroyed set)
          * has closed its SSHSession (if any) exactly once
          * has told the terminal widget to stop forwarding I/O

        Does NOT join the connect worker — paramiko's blocking
        client.connect() can take up to ``timeout`` seconds to unwind,
        and we can't block the GUI thread while the user is closing
        a tab. The worker is daemonised so it will not prevent
        interpreter exit, and its eventual result signal is discarded
        because _destroyed is set.
        """
        if self._destroyed:
            return
        self._destroyed = True
        self._connect_token += 1  # invalidate any pending worker

        # Disconnect every signal we own BEFORE tearing down child
        # widgets. A daemon connect worker may still be blocked
        # inside paramiko.SSHClient.connect() for up to `timeout`
        # seconds; when it finally returns it will try to emit one
        # of the cross-thread bridges. With no receivers the emit
        # is a cheap no-op — with a destroyed Python wrapper on
        # the slot side it can segfault. Breaking the connection
        # here converts that risk into a clean drop.
        for sig_attr in (
            "_connect_failed_sig",
            "_connect_succeeded_sig",
            "state_changed",
            "title_changed",
            "log_appended",
            "closed",
        ):
            sig = getattr(self, sig_attr, None)
            if sig is None:
                continue
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
            except Exception:
                pass

        try:
            # Disconnect from the terminal's signal so a late
            # session_closed emission from the terminal during its
            # own shutdown never lands here and flips state on a
            # dying object.
            try:
                self.terminal.session_closed.disconnect(
                    self._on_terminal_session_closed
                )
            except Exception:
                pass
        except Exception:
            pass

        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        try:
            self.terminal.shutdown()
        except Exception:
            pass

        # Wait briefly (bounded) for the connect worker to finish.
        # The worker may be blocked inside paramiko.SSHClient.connect()
        # but we've already closed the session and disconnected the
        # bridge signals so there's no risk of it mutating state.
        # The join is a best-effort hold so Python's interpreter exit
        # doesn't race with a still-running daemon thread during GC.
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout=0.25)
            except Exception:
                pass

        # Mark state as destroyed so the parent view's state-mirror
        # helper can tell the tab is gone if it races with a last
        # state_changed emission.
        self._state = STATE_DESTROYED

    # ── Button handlers (guarded) ───────────────────────────────────────────

    def _on_reconnect_clicked(self) -> None:
        if self._destroyed:
            return
        # Allow reconnect from any non-destroyed state — disconnect
        # first then start_connection. The state guards inside
        # start_connection keep a rapid double-click safe.
        self.reconnect()

    def _on_disconnect_clicked(self) -> None:
        if self._destroyed:
            return
        # Non-silent: the user pressed the button, so they deserve
        # the "[ssh session closed]" confirmation line.
        self.disconnect_session(silent=False)

    # ── Worker plumbing ──────────────────────────────────────────────────────

    def _connect_worker(self, profile, token: int) -> None:
        if isinstance(profile, SerialProfile):
            session = SerialSession()
            timeout = 5.0
        else:
            session = SSHSession()
            timeout = 8.0
        try:
            session.start(profile, timeout=timeout)
        except Exception as exc:
            # Clean up the half-built session even though start()
            # already tries — belt-and-braces because start() can
            # raise before assigning the channel.
            try:
                session.close()
            except Exception:
                pass
            if self._destroyed:
                return
            # Translate paramiko / socket exceptions into a short,
            # plain-English message so the user can tell at a glance
            # whether the failure was DNS, refused, auth, or timeout.
            # Serial sessions reuse this code path but their friendly
            # text is already produced by SerialSession.start, so we
            # only translate for the SSH branch.
            if isinstance(profile, SerialProfile):
                message = str(exc) or type(exc).__name__
            else:
                message = friendly_error(exc)
            try:
                self._connect_failed_sig.emit(token, message)
            except RuntimeError:
                # Cross-thread bridge has been disconnected by
                # shutdown(), or the underlying QObject has been
                # torn down — drop the result silently.
                pass
            except Exception:
                pass
            return

        # Emit the success signal. If the tab was torn down or the
        # token has since been bumped, the slot will immediately
        # close the fresh session instead of attaching it.
        if self._destroyed:
            try:
                session.close()
            except Exception:
                pass
            return
        try:
            self._connect_succeeded_sig.emit(token, session)
        except RuntimeError:
            # Cross-thread bridge was disconnected during shutdown
            # or the QObject was destroyed. Close the orphan
            # session so paramiko doesn't leak it.
            try:
                session.close()
            except Exception:
                pass
        except Exception:
            try:
                session.close()
            except Exception:
                pass

    @pyqtSlot(int, str)
    def _on_connect_failed(self, token: int, message: str) -> None:
        if self._destroyed:
            return
        if token != self._connect_token:
            # Stale result from a cancelled attempt — ignore.
            return
        self._set_state(STATE_FAILED, "Failed")
        try:
            self.terminal._append(f"\n[connection failed]\n  {message}\n")
        except Exception:
            pass
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self.log_appended.emit(f"FAILED  {self._log_target()}: {message}")

    @pyqtSlot(int, object)
    def _on_connect_succeeded(self, token: int, session) -> None:
        if self._destroyed or session is None:
            # Tab is gone or we somehow got a None session — tear the
            # fresh session back down so we don't leak it.
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
            return
        if token != self._connect_token:
            # Stale — user cancelled this attempt before it finished.
            # Close the orphan session so paramiko doesn't leak it.
            try:
                session.close()
            except Exception:
                pass
            return

        self._session = session
        self._opened_at = datetime.now()
        try:
            if self._is_serial:
                self.terminal.attach_ssh(
                    session,
                    banner=f"[opened {self.profile.summary()}]\n",
                    line_ending=self.profile.line_ending,
                    local_echo=self.profile.local_echo,
                    backend_kind="serial",
                )
            else:
                self.terminal.attach_ssh(
                    session,
                    banner=(
                        f"[connected to {self.profile.user}@"
                        f"{self.profile.host}:{self.profile.port}]\n"
                    ),
                    line_ending="cr",
                    local_echo=False,
                    backend_kind="ssh",
                )
        except Exception as exc:
            # attach_ssh going sideways is extremely unlikely, but if
            # it does we must not leave the tab in a half-attached
            # state — close the session and report failure instead.
            try:
                session.close()
            except Exception:
                pass
            self._session = None
            self._set_state(STATE_FAILED, "Failed")
            self._btn_reconnect.setEnabled(True)
            self._btn_disconnect.setEnabled(False)
            self.log_appended.emit(
                f"FAILED  {self._log_target()}: attach error: {exc}"
            )
            return

        self._set_state(STATE_CONNECTED, "Connected")
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(True)
        self.log_appended.emit(f"CONNECTED  {self._log_target()}")

    @pyqtSlot()
    def _on_terminal_session_closed(self) -> None:
        # Triggered by the terminal when the SSH read loop ends or the
        # user manually disconnects.
        if self._destroyed:
            return
        # Don't clobber CONNECTING / FAILED with a CLOSED transition —
        # those paths own their own state already. A late emission
        # from a prior session must never regress a newly-initiated
        # connect.
        if self._state in (STATE_CONNECTING, STATE_FAILED):
            return
        self._session = None
        self._set_state(STATE_CLOSED, "Disconnected")
        self._btn_reconnect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self.log_appended.emit(f"CLOSED  {self._log_target()}")

    # ── State helper ─────────────────────────────────────────────────────────

    def _set_state(self, new_state: str, label: str) -> None:
        if self._destroyed:
            return
        self._state = new_state
        try:
            self._state_lbl.setText(label)
        except RuntimeError:
            # Widget destroyed mid-transition.
            return
        t = theme()
        color = {
            STATE_IDLE:       t.text_dim,
            STATE_CONNECTING: t.amber,
            STATE_CONNECTED:  t.green,
            STATE_FAILED:     t.red,
            STATE_CLOSED:     t.text_dim,
        }.get(new_state, t.text_dim)

        try:
            if new_state == STATE_CONNECTING:
                self._dot.set_active(True, color=color)
            elif new_state == STATE_CONNECTED:
                self._dot.set_active(True, color=color)
            else:
                self._dot.set_active(False)
                self._dot.set_color(color)

            self._state_lbl.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: 700;"
                f" letter-spacing: 0.6px; background: transparent;"
            )
        except RuntimeError:
            return

        try:
            self.state_changed.emit(new_state)
        except Exception:
            pass

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        if self._destroyed:
            return
        try:
            self._header.setStyleSheet(
                f"#session_tab_header {{"
                f"  background-color: {t.bg_raised};"
                f"  border: none;"
                f"}}"
                f"#session_tab_header QLabel {{ background: transparent; }}"
            )
            self._head_div.setStyleSheet(f"background-color: {t.border};")
            self._title_lbl.setStyleSheet(
                f"color: {t.accent}; font-size: 13px; font-weight: 700;"
                f" font-family: 'Consolas', monospace; background: transparent;"
            )
            self._sub_lbl.setStyleSheet(
                f"color: {t.text_dim}; font-size: 10px;"
                f" font-family: 'Consolas', monospace; background: transparent;"
            )
        except RuntimeError:
            return
        # Re-apply the state color so the dot and label match the theme.
        self._set_state(self._state, self._state_lbl.text() or "Idle")
