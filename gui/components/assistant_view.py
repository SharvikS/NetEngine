"""
AI Assistant page — local Ollama, two modes, graceful offline state.

Layout::

    +---------------------------------------------------------------+
    |  [Command] [Chat]              Ollama 0.x · model llama3.2    |
    +---------------------------------------------------------------+
    |  [status banner — only when AI is unavailable]                |
    +---------------------------------------------------------------+
    |                                                               |
    |  <mode content — QStackedWidget switches Command / Chat>      |
    |                                                               |
    +---------------------------------------------------------------+
    |  input textarea                                   [Send] [Stop]|
    +---------------------------------------------------------------+

Reliability design:

* **Zero blocking I/O on the GUI thread.** The health probe runs on a
  QThread via ``AIService.probe_status_async``; inference runs on a
  separate ``StreamWorker`` QThread. Neither path touches the network
  on the main thread.

* **Stale-signal safety.** Every worker signal is dispatched through
  a slot that first confirms the emitting worker is the one the UI
  currently cares about. Late chunks or a late ``finished`` from a
  cancelled/stopped request cannot corrupt the new mode's state.

* **Three terminal outcomes.** A request ends in ``finished`` (clean),
  ``cancelled`` (Stop pressed), or ``failed`` (typed error). The chat
  path only records an exchange on ``finished`` — cancelled / failed
  responses never pollute the chat history.

* **Crash-proof shutdown.** ``shutdown()`` cancels the worker, closes
  the HTTP session to unblock any pending network read, and waits on
  the QThread with a bounded timeout so Qt never destroys a thread
  while its ``run()`` is still executing.

* **Fully graceful offline state.** If Ollama is unreachable or the
  configured model isn't installed, the banner shows the exact
  remedy text from ``AIService.probe_status_async`` and the Send
  button is disabled. No exceptions propagate to the app.

* **Safe command workflow.** Suggested commands are rendered in a
  read-only box with a Copy button. This view never executes a
  command. The user copies, pastes, reviews, runs — their choice.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QTextCursor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QTextBrowser, QFrame, QStackedWidget, QSizePolicy, QApplication,
    QLineEdit, QComboBox, QScrollArea, QScrollBar, QGridLayout,
)

from ai.ai_service import (
    AIService,
    AIStatus,
    StreamWorker,
    make_chat_worker,
    make_command_worker,
    run_stream_worker,
)
from ai.command_assistant import parse_command_response, CommandSuggestion
from ai.ollama_client import ModelInfo
from gui.themes import theme, ThemeManager
from utils.clipboard import copy_text


# ── Small helpers ──────────────────────────────────────────────────────────


def _mono_font() -> QFont:
    f = QFont()
    f.setFamilies([
        "JetBrains Mono", "Cascadia Mono", "Cascadia Code",
        "Fira Code", "Consolas", "Segoe UI Mono", "Courier New",
    ])
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSizeF(10.0)
    return f


class _SuggestionCard(QFrame):
    """Clickable prompt suggestion card for the chat welcome screen."""

    clicked = pyqtSignal(str)

    def __init__(self, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ai_suggest_card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prompt = subtitle

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(3)

        t = QLabel(title)
        t.setObjectName("ai_suggest_title")
        s = QLabel(subtitle)
        s.setObjectName("ai_suggest_desc")
        s.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(s)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._prompt)
        super().mousePressEvent(event)


# ── View ───────────────────────────────────────────────────────────────────


class AssistantView(QWidget):
    """Top-level Assistant page wired into MainWindow's stack."""

    #: Text to show in the main status bar whenever AI state changes.
    status_message = pyqtSignal(str)

    #: Emitted when the user clicks "Insert into Terminal" on a command
    #: suggestion. MainWindow catches this to switch to the Terminal
    #: page and pre-fill the current input (without submitting).
    insert_to_terminal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._service = AIService()
        self._status: Optional[AIStatus] = None
        # Flipped in shutdown() so late callbacks from QTimer.singleShot
        # lambdas and in-flight worker signals drop silently instead
        # of touching a torn-down view.
        self._shutting_down = False

        # ── Worker / thread tracking ──────────────────────────────
        # Only ONE inference worker is considered "current" at a
        # time. Slot handlers check the worker identity before
        # touching UI state so stale signals from a cancelled or
        # superseded run can never corrupt the active panel.
        self._current_thread: Optional[QThread] = None
        self._current_worker: Optional[StreamWorker] = None
        self._probe_thread: Optional[QThread] = None
        self._probe_in_flight = False

        # Guard used while we programmatically reconcile the model
        # dropdown with the registry. The combo's ``activated``
        # signal is user-only, but we still flip this flag while
        # we rebuild the item list so any code path that races in
        # during a refresh can tell "did the user actually click?"
        # from "did we just resync the widget?".
        self._suppress_model_picks = False
        # Remember the selection the combo is *showing* so we can
        # revert it if the user picks an unusable entry.
        self._active_model_name: str = ""

        self._mode = "command"

        # Per-run state — cleared on each new request.
        self._cmd_raw_buffer = ""
        self._chat_stream_buffer = ""
        self._pending_user_msg = ""
        # Reference to the QLabel inside the AI bubble currently being
        # streamed into. Cleared when the response finishes/cancels/fails.
        self._current_ai_text_label: Optional[QLabel] = None

        # If the user hits Send before we've ever probed (or while a
        # probe is in flight), we stash the prompt here and send it
        # once the probe returns ok.
        self._pending_send: tuple[str, str] = ("", "")  # (mode, prompt)

        self._build_ui()

        # ── Model manager wiring ──────────────────────────────────
        # Subscribe to every event that can shift the dropdown's
        # "what's available / what's active" view. Each slot is a
        # thin reconcile — the manager owns state, the UI just
        # renders the latest snapshot on demand.
        mgr = self._service.model_manager
        mgr.models_changed.connect(self._on_models_changed)
        mgr.current_model_changed.connect(self._on_current_model_changed)
        mgr.refresh_started.connect(self._on_model_refresh_started)
        mgr.refresh_finished.connect(self._on_model_refresh_finished)
        mgr.refresh_failed.connect(self._on_model_refresh_failed)
        mgr.model_auto_fallback.connect(self._on_model_auto_fallback)
        # Seed the combo with whatever the manager already knows so
        # the label isn't blank until the first refresh completes.
        # Typically empty on first launch; populated by the probe.
        self._active_model_name = mgr.current
        self._rebuild_model_combo(mgr.available, mgr.current)

        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # Start the first async probe a tick after show so the window
        # is already painted by the time the background thread kicks
        # off. Probe runs off the GUI thread, so even a full 4 s
        # timeout never freezes the UI.
        self._apply_status(AIStatus.checking())
        QTimer.singleShot(150, self._kick_probe)

    # ── UI construction ────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 22)
        root.setSpacing(12)

        # ── Top bar: mode toggle + version label ───────────────────
        top = QHBoxLayout()
        top.setSpacing(8)

        self._btn_mode_command = QPushButton("Command")
        self._btn_mode_command.setObjectName("ai_mode_btn")
        self._btn_mode_command.setCheckable(True)
        self._btn_mode_command.setChecked(True)
        self._btn_mode_command.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mode_command.clicked.connect(
            lambda: self._switch_mode("command"))
        top.addWidget(self._btn_mode_command)

        self._btn_mode_chat = QPushButton("Chat")
        self._btn_mode_chat.setObjectName("ai_mode_btn")
        self._btn_mode_chat.setCheckable(True)
        self._btn_mode_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mode_chat.clicked.connect(
            lambda: self._switch_mode("chat"))
        top.addWidget(self._btn_mode_chat)

        top.addStretch(1)

        # ── Model selector ────────────────────────────────────────
        # Compact "Model:" label + dropdown + refresh button. The
        # dropdown is the primary control for switching local models;
        # the refresh button re-queries /api/tags (via the manager).
        # All three widgets live in the same row as the version label
        # so the header stays a single tidy strip instead of sprawling
        # into a second row. The label doubles as affordance — users
        # scanning for a model picker find "Model:" immediately.
        self._lbl_model_title = QLabel("Model:")
        self._lbl_model_title.setObjectName("ai_model_title")
        top.addWidget(self._lbl_model_title)

        self._model_combo = QComboBox()
        self._model_combo.setObjectName("ai_model_combo")
        self._model_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_combo.setMinimumWidth(220)
        self._model_combo.setMaxVisibleItems(14)
        self._model_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._model_combo.setToolTip(
            "Choose which locally installed Ollama model to use.\n"
            "The list is refreshed automatically on status probes and "
            "can be re-checked with the ↻ button."
        )
        # ``activated`` (not ``currentIndexChanged``) fires only on
        # user interaction, so programmatic updates of the combo
        # (from a refresh that happens to re-select the same model)
        # don't recursively trigger another switch.
        self._model_combo.activated.connect(self._on_model_picked)
        top.addWidget(self._model_combo)

        self._btn_refresh_models = QPushButton("↻")
        self._btn_refresh_models.setObjectName("ai_model_refresh")
        self._btn_refresh_models.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_refresh_models.setFixedWidth(30)
        self._btn_refresh_models.setToolTip("Refresh model list from Ollama")
        self._btn_refresh_models.clicked.connect(self._on_refresh_models)
        top.addWidget(self._btn_refresh_models)

        # Slim vertical separator so the status cluster (version +
        # retry) reads as visually distinct from the model selector.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setObjectName("ai_header_sep")
        sep.setFixedHeight(20)
        top.addWidget(sep)

        self._lbl_version = QLabel("")
        self._lbl_version.setObjectName("ai_version_label")
        self._lbl_version.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self._lbl_version)

        self._btn_retry = QPushButton("Retry")
        self._btn_retry.setObjectName("ai_retry_btn")
        self._btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_retry.clicked.connect(self._on_retry_clicked)
        self._btn_retry.setToolTip("Re-check Ollama connectivity")
        top.addWidget(self._btn_retry)

        root.addLayout(top)

        # ── Status banner (hidden when AI is healthy) ──────────────
        self._banner = QFrame()
        self._banner.setObjectName("ai_banner")
        banner_lay = QVBoxLayout(self._banner)
        banner_lay.setContentsMargins(14, 10, 14, 10)
        banner_lay.setSpacing(4)
        self._banner_title = QLabel("Local AI unavailable")
        self._banner_title.setObjectName("ai_banner_title")
        self._banner_msg = QLabel("")
        self._banner_msg.setObjectName("ai_banner_msg")
        self._banner_msg.setWordWrap(True)
        self._banner_remedy = QLabel("")
        self._banner_remedy.setObjectName("ai_banner_remedy")
        self._banner_remedy.setWordWrap(True)
        self._banner_remedy.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        banner_lay.addWidget(self._banner_title)
        banner_lay.addWidget(self._banner_msg)
        banner_lay.addWidget(self._banner_remedy)
        root.addWidget(self._banner)
        self._banner.setVisible(False)

        # ── Mode content stack ─────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_command_panel())
        self._stack.addWidget(self._build_chat_panel())
        root.addWidget(self._stack, stretch=1)

        # ── Bottom input area (unified chat-style bar) ─────────────
        input_frame = QFrame()
        input_frame.setObjectName("ai_input_frame")
        input_frame_lay = QHBoxLayout(input_frame)
        input_frame_lay.setContentsMargins(12, 8, 8, 8)
        input_frame_lay.setSpacing(8)

        self._input = QPlainTextEdit()
        self._input.setObjectName("ai_input")
        self._input.setPlaceholderText("Send a message…  (Ctrl+Enter)")
        self._input.setFixedHeight(52)
        self._input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        input_frame_lay.addWidget(self._input, stretch=1)

        btns = QVBoxLayout()
        btns.setSpacing(4)
        btns.setContentsMargins(0, 0, 0, 0)

        self._btn_send = QPushButton("↑")
        self._btn_send.setObjectName("ai_send_btn")
        self._btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_send.setFixedSize(36, 36)
        self._btn_send.setToolTip("Send  (Ctrl+Enter)")
        self._btn_send.clicked.connect(self._on_send)
        btns.addWidget(self._btn_send)

        self._btn_stop = QPushButton("■")
        self._btn_stop.setObjectName("ai_stop_btn")
        self._btn_stop.setEnabled(False)
        self._btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_stop.setFixedSize(36, 36)
        self._btn_stop.setToolTip("Stop generation")
        self._btn_stop.clicked.connect(self._on_stop)
        btns.addWidget(self._btn_stop)

        input_frame_lay.addLayout(btns)
        root.addWidget(input_frame)

        # Ctrl+Enter / Cmd+Enter sends from the input box.
        send_sc = QShortcut(QKeySequence("Ctrl+Return"), self._input)
        send_sc.activated.connect(self._on_send)
        send_sc2 = QShortcut(QKeySequence("Ctrl+Enter"), self._input)
        send_sc2.activated.connect(self._on_send)

    def _build_command_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        hint = QLabel(
            "Ask in plain English. The assistant suggests one shell "
            "command, explains it, and flags anything risky. "
            "Nothing is executed — you copy and run it yourself."
        )
        hint.setWordWrap(True)
        hint.setObjectName("ai_hint")
        lay.addWidget(hint)

        # Command box — the final suggested command, copy-pasteable.
        cmd_row_wrap = QFrame()
        cmd_row_wrap.setObjectName("ai_cmd_box")
        cmd_lay = QVBoxLayout(cmd_row_wrap)
        cmd_lay.setContentsMargins(12, 10, 12, 10)
        cmd_lay.setSpacing(8)

        cmd_label = QLabel("SUGGESTED COMMAND")
        cmd_label.setObjectName("ai_section_label")
        cmd_lay.addWidget(cmd_label)

        self._cmd_line = QLineEdit()
        self._cmd_line.setReadOnly(True)
        self._cmd_line.setObjectName("ai_cmd_line")
        self._cmd_line.setFont(_mono_font())
        self._cmd_line.setPlaceholderText(
            "(the suggested command will appear here)"
        )
        cmd_lay.addWidget(self._cmd_line)

        cmd_btn_row = QHBoxLayout()
        cmd_btn_row.setSpacing(8)
        self._btn_copy = QPushButton("Copy")
        self._btn_copy.setObjectName("ai_cmd_action")
        self._btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_copy.setEnabled(False)
        self._btn_copy.clicked.connect(self._on_copy_command)
        cmd_btn_row.addWidget(self._btn_copy)

        self._btn_insert = QPushButton("Insert into Terminal")
        self._btn_insert.setObjectName("ai_cmd_action")
        self._btn_insert.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_insert.setEnabled(False)
        self._btn_insert.setToolTip(
            "Switch to the Terminal tab and pre-fill the input with this "
            "command. You still have to press Enter to run it."
        )
        self._btn_insert.clicked.connect(self._on_insert_to_terminal)
        cmd_btn_row.addWidget(self._btn_insert)

        self._lbl_copy_state = QLabel("")
        self._lbl_copy_state.setObjectName("ai_copy_state")
        cmd_btn_row.addWidget(self._lbl_copy_state)
        cmd_btn_row.addStretch(1)
        cmd_lay.addLayout(cmd_btn_row)

        lay.addWidget(cmd_row_wrap)

        # Explanation + caution panels.
        self._cmd_explain = QLabel("")
        self._cmd_explain.setObjectName("ai_cmd_explain")
        self._cmd_explain.setWordWrap(True)
        self._cmd_explain.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._cmd_explain)

        self._cmd_caution = QLabel("")
        self._cmd_caution.setObjectName("ai_cmd_caution")
        self._cmd_caution.setWordWrap(True)
        self._cmd_caution.setVisible(False)
        lay.addWidget(self._cmd_caution)

        lay.addStretch(1)
        return w

    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Thin top bar: just the "New chat" button aligned right
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 8)
        top.addStretch(1)
        self._btn_clear_chat = QPushButton("+ New chat")
        self._btn_clear_chat.setObjectName("ai_clear_chat")
        self._btn_clear_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_chat.clicked.connect(self._on_clear_chat)
        top.addWidget(self._btn_clear_chat)
        lay.addLayout(top)

        # Main stacked: welcome screen (0) vs. message bubbles (1)
        self._chat_panel_stack = QStackedWidget()
        self._chat_panel_stack.addWidget(self._build_welcome_widget())

        # Messages scroll area
        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chat_scroll.setObjectName("ai_chat_scroll")

        self._chat_messages_widget = QWidget()
        self._chat_messages_widget.setObjectName("ai_chat_messages")
        self._chat_messages_layout = QVBoxLayout(self._chat_messages_widget)
        self._chat_messages_layout.setContentsMargins(0, 8, 0, 8)
        self._chat_messages_layout.setSpacing(16)
        self._chat_messages_layout.addStretch(1)  # push bubbles to top

        self._chat_scroll.setWidget(self._chat_messages_widget)
        self._chat_panel_stack.addWidget(self._chat_scroll)

        lay.addWidget(self._chat_panel_stack, stretch=1)
        return panel

    def _build_welcome_widget(self) -> QWidget:
        w = QWidget()
        w.setObjectName("ai_welcome")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(24, 0, 24, 0)
        outer.addStretch(2)

        title = QLabel("NetEngine AI")
        title.setObjectName("ai_welcome_title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)

        sub = QLabel("How can I help you today?")
        sub.setObjectName("ai_welcome_sub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(sub)

        outer.addSpacing(36)

        cards_widget = QWidget()
        cards_grid = QGridLayout(cards_widget)
        cards_grid.setSpacing(10)
        cards_grid.setContentsMargins(0, 0, 0, 0)

        suggestions = [
            ("Scan my network",     "What hosts are currently active?"),
            ("Check open ports",    "What ports are open on 192.168.1.1?"),
            ("SSH key setup",       "How do I set up SSH key authentication?"),
            ("Explain scan results","What do these port scan results mean?"),
        ]
        for i, (card_title, card_desc) in enumerate(suggestions):
            card = _SuggestionCard(card_title, card_desc)
            card.clicked.connect(self._on_suggestion)
            cards_grid.addWidget(card, i // 2, i % 2)

        outer.addWidget(cards_widget, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)

        disclaimer = QLabel("AI can make mistakes. Always verify important information.")
        disclaimer.setObjectName("ai_disclaimer")
        disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(disclaimer)
        outer.addSpacing(8)

        return w

    def _add_user_bubble(self, text: str) -> None:
        """Add a right-aligned user message bubble and switch to messages view."""
        if self._chat_panel_stack.currentIndex() == 0:
            self._chat_panel_stack.setCurrentIndex(1)

        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(8, 0, 8, 0)
        row_lay.addStretch(1)

        bubble = QFrame()
        bubble.setObjectName("ai_user_bubble")

        b_lay = QVBoxLayout(bubble)
        b_lay.setContentsMargins(14, 10, 14, 10)
        b_lay.setSpacing(0)

        lbl = QLabel(text)
        lbl.setObjectName("ai_bubble_text_user")
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setMaximumWidth(560)
        b_lay.addWidget(lbl)

        row_lay.addWidget(bubble)
        self._chat_messages_layout.addWidget(row)
        self._scroll_chat_to_bottom()

    def _add_ai_bubble(self) -> None:
        """Add a left-aligned AI response bubble and set _current_ai_text_label."""
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(8, 0, 8, 0)

        bubble = QFrame()
        bubble.setObjectName("ai_ai_bubble")

        b_lay = QVBoxLayout(bubble)
        b_lay.setContentsMargins(14, 10, 14, 10)
        b_lay.setSpacing(4)

        role_lbl = QLabel("AI")
        role_lbl.setObjectName("ai_bubble_role")
        b_lay.addWidget(role_lbl)

        lbl = QLabel("")
        lbl.setObjectName("ai_bubble_text_ai")
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setMaximumWidth(640)
        lbl.setMinimumWidth(80)
        b_lay.addWidget(lbl)

        row_lay.addWidget(bubble)
        row_lay.addStretch(1)

        self._chat_messages_layout.addWidget(row)
        self._current_ai_text_label = lbl
        self._scroll_chat_to_bottom()

    def _scroll_chat_to_bottom(self) -> None:
        """Scroll the chat area to the bottom after a tick."""
        def _do():
            if not self._shutting_down:
                sb = self._chat_scroll.verticalScrollBar()
                sb.setValue(sb.maximum())
        QTimer.singleShot(0, _do)

    def _on_suggestion(self, text: str) -> None:
        """Fill the input with a suggestion card prompt and send."""
        self._input.setPlainText(text)
        self._input.setFocus()
        if self._status is not None and self._status.state == "ok":
            self._on_send()

    # ── Mode switching ─────────────────────────────────────────────

    def _switch_mode(self, mode: str) -> None:
        if mode == self._mode:
            self._btn_mode_command.setChecked(mode == "command")
            self._btn_mode_chat.setChecked(mode == "chat")
            return
        # Stop any in-flight request — each mode has its own prompt
        # path, so we don't want a half-stream from the previous mode
        # landing in the wrong panel. Worker identity checks in the
        # slot handlers then ensure late signals from the old worker
        # can't corrupt the new mode's UI state.
        self._on_stop()
        self._pending_send = ("", "")
        self._mode = mode
        self._btn_mode_command.setChecked(mode == "command")
        self._btn_mode_chat.setChecked(mode == "chat")
        self._stack.setCurrentIndex(0 if mode == "command" else 1)
        self._input.setPlaceholderText(
            "Ask for a command…  (Ctrl+Enter)"
            if mode == "command"
            else "Send a message…  (Ctrl+Enter)"
        )

    # ── Status probe (async) ───────────────────────────────────────

    def _on_retry_clicked(self) -> None:
        """Retry button: force a fresh probe, ignoring the TTL cache."""
        self._apply_status(AIStatus.checking())
        self._kick_probe(force=True)

    def _kick_probe(self, *, force: bool = False) -> None:
        """Start an async health probe.

        Returns immediately. When the probe completes the result is
        delivered to ``_on_probe_result`` on the GUI thread via the
        worker's ``result`` signal.

        If a fresh cached status exists and ``force`` is False the
        service delivers it synchronously without spawning a thread;
        in that case we short-circuit and never enter the "checking"
        banner state.
        """
        if self._shutting_down or self._probe_in_flight:
            return
        # Fast path: fresh cache, no work needed.
        cached = self._service.cached_status()
        if (
            not force
            and cached is not None
            and self._service.status_is_fresh()
        ):
            self._apply_status(cached)
            return

        self._probe_in_flight = True
        thread = self._service.probe_status_async(
            self,
            self._on_probe_result,
            force=force,
        )
        self._probe_thread = thread
        if thread is None:
            # Service delivered a cached value synchronously after
            # all. Clear the in-flight flag so the next probe can run.
            self._probe_in_flight = False

    def _on_probe_result(self, status: AIStatus) -> None:
        """Slot: receives an ``AIStatus`` from the background probe.

        Always runs on the GUI thread (Qt's queued signal delivery).
        Clears the in-flight flag, applies the status to the UI, and
        — if the user queued a Send while the probe was running —
        runs that Send now.
        """
        if self._shutting_down:
            return
        self._probe_in_flight = False
        self._probe_thread = None
        try:
            self._apply_status(status)
        except RuntimeError:
            return

        pending_mode, pending_prompt = self._pending_send
        if pending_prompt and pending_mode == self._mode:
            self._pending_send = ("", "")
            if status.ok:
                self._start_inference(pending_prompt)
            else:
                try:
                    # Probe failed — unlock the input and show the banner.
                    self._input.setReadOnly(False)
                    self._btn_send.setEnabled(False)
                except RuntimeError:
                    return
        elif pending_prompt:
            # Mode changed while probe was running — discard.
            self._pending_send = ("", "")

    def _apply_status(self, status: AIStatus) -> None:
        """Render an ``AIStatus`` into the header label + banner."""
        self._status = status
        if status.state == "ok":
            self._banner.setVisible(False)
            self._lbl_version.setText(status.message)
            if self._current_worker is None:
                self._btn_send.setEnabled(True)
            # A reachable, model-installed state means the dropdown
            # is meaningful — unlock it. Refresh state machinery
            # will re-disable it on its own if a refresh is running.
            self._model_combo.setEnabled(True)
            self._btn_refresh_models.setEnabled(True)
            self.status_message.emit(f"AI ready — {status.message}")
            return

        if status.state == "checking":
            self._banner.setVisible(True)
            self._banner_title.setText("Checking local AI…")
            self._banner_msg.setText(status.message)
            self._banner_remedy.setText("")
            self._lbl_version.setText("checking…")
            self._btn_send.setEnabled(False)
            self.status_message.emit("AI — checking…")
            return

        title_by_state = {
            "disabled":    "AI disabled",
            "unreachable": "AI not reachable",
            "no_key":      "API key required",
            "no_model":    "Model not available",
            "timeout":     "AI timed out",
            "error":       "AI error",
        }
        self._banner.setVisible(True)
        self._banner_title.setText(
            title_by_state.get(status.state, "Local AI unavailable")
        )
        self._banner_msg.setText(status.message or "")
        self._banner_remedy.setText(status.remedy or "")
        self._lbl_version.setText("offline")
        self._btn_send.setEnabled(False)
        # Disable model switching when the daemon is unusable so the
        # user can't queue up a pick against a dead backend — except
        # in ``no_model`` state, where the dropdown is actually the
        # primary tool for recovering (pick a model that *is*
        # installed). Refresh stays available in all non-disabled
        # states so the user can re-check after starting Ollama.
        if status.state in ("unreachable", "timeout", "disabled", "error", "no_key"):
            self._model_combo.setEnabled(False)
        else:
            self._model_combo.setEnabled(True)
        self._btn_refresh_models.setEnabled(status.state != "disabled")
        self.status_message.emit(
            f"AI unavailable — {status.message[:80]}"
            if status.message else "AI unavailable"
        )

    # ── Model selector ─────────────────────────────────────────────

    def _rebuild_model_combo(
        self,
        models: list,
        active_name: str,
    ) -> None:
        """Reconcile the dropdown with a fresh model list.

        Rebuilds the items from scratch instead of diffing. The list
        is short (≤ a few dozen) and a full rebuild is trivially
        cheap; a diff pass would just be extra code for the same
        visible result. The ``_suppress_model_picks`` guard ensures
        that the programmatic ``setCurrentIndex`` call made during
        the rebuild does not re-enter ``_on_model_picked``.

        Shows a sentinel row when the list is empty so the combo
        never looks broken — "No models found" is a first-class
        state, not an error.
        """
        self._suppress_model_picks = True
        try:
            self._model_combo.blockSignals(True)
            self._model_combo.clear()
            if not models:
                self._model_combo.addItem("(no models found)", userData="")
                self._model_combo.setEnabled(False)
                self._model_combo.setCurrentIndex(0)
                return
            # Stable sort: first by category (chat → code → vision
            # → embedding), then by name. Keeps the list visually
            # consistent across refreshes.
            order = {"chat": 0, "code": 1, "vision": 2, "embedding": 3}
            items = sorted(
                models,
                key=lambda m: (order.get(m.category, 9), m.name.lower()),
            )
            active_index = 0
            for i, info in enumerate(items):
                label = info.display_label()
                if info.is_heavy:
                    label += "  ⚠"
                self._model_combo.addItem(label, userData=info.name)
                tooltip_parts = [info.name]
                if info.family:
                    tooltip_parts.append(f"family: {info.family}")
                if info.parameter_size:
                    tooltip_parts.append(f"size: {info.parameter_size}")
                if info.quantization:
                    tooltip_parts.append(f"quant: {info.quantization}")
                if info.size_human:
                    tooltip_parts.append(f"disk: {info.size_human}")
                if info.is_heavy:
                    tooltip_parts.append(
                        "⚠ large model — first-token latency may be slow"
                    )
                self._model_combo.setItemData(
                    i,
                    "\n".join(tooltip_parts),
                    Qt.ItemDataRole.ToolTipRole,
                )
                if info.name == active_name:
                    active_index = i
            self._model_combo.setCurrentIndex(active_index)
            self._model_combo.setEnabled(True)
        finally:
            self._model_combo.blockSignals(False)
            self._suppress_model_picks = False

    def _reselect_current_in_combo(self, name: str) -> None:
        """Snap the combo to the given name without re-entering the
        user-pick handler. Used when a model switch is vetoed or
        when the manager emits ``current_model_changed`` due to an
        auto-fallback rather than a user click."""
        if not name:
            return
        self._suppress_model_picks = True
        try:
            self._model_combo.blockSignals(True)
            for i in range(self._model_combo.count()):
                if self._model_combo.itemData(i) == name:
                    self._model_combo.setCurrentIndex(i)
                    break
        finally:
            self._model_combo.blockSignals(False)
            self._suppress_model_picks = False

    def _on_models_changed(self, models: list) -> None:
        """Slot: the manager replaced its model list."""
        if self._shutting_down:
            return
        try:
            mgr = self._service.model_manager
            self._rebuild_model_combo(models, mgr.current or self._active_model_name)
        except RuntimeError:
            return

    def _on_current_model_changed(self, name: str) -> None:
        """Slot: the active model changed (user pick or fallback)."""
        if self._shutting_down:
            return
        self._active_model_name = name or ""
        try:
            self._reselect_current_in_combo(name)
        except RuntimeError:
            return

    def _on_model_refresh_started(self) -> None:
        """Slot: a refresh worker just started. Dim the controls so
        the user can see work is happening, and avoid pile-up if the
        refresh button is clicked repeatedly."""
        if self._shutting_down:
            return
        try:
            self._btn_refresh_models.setEnabled(False)
            self._btn_refresh_models.setText("…")
        except RuntimeError:
            return

    def _on_model_refresh_finished(self, has_models: bool) -> None:
        """Slot: refresh worker reached a terminal state (success or
        failure). Restore the refresh button's idle look."""
        if self._shutting_down:
            return
        try:
            self._btn_refresh_models.setEnabled(True)
            self._btn_refresh_models.setText("↻")
            if not has_models and self._status and self._status.state not in (
                "disabled", "unreachable", "timeout"
            ):
                # Ollama is up but returned nothing. Surface this clearly
                # in the status bar so the user knows to pull something.
                self.status_message.emit(
                    "AI — no models installed. Run `ollama pull <model>`."
                )
        except RuntimeError:
            return

    def _on_model_refresh_failed(self, message: str) -> None:
        """Slot: refresh worker failed. Leave the existing list
        intact and just emit a short status bar note."""
        if self._shutting_down:
            return
        try:
            self.status_message.emit(f"Model refresh failed — {message[:80]}")
        except RuntimeError:
            return

    def _on_model_auto_fallback(self, old: str, new: str) -> None:
        """Slot: the current selection was dropped because it's
        gone, and we auto-picked another. Notify the user so they
        understand why the header label changed."""
        if self._shutting_down:
            return
        try:
            self.status_message.emit(
                f"Model '{old}' is gone — switched to '{new}'."
            )
        except RuntimeError:
            return

    def _on_refresh_models(self) -> None:
        """Clicked: the ↻ button next to the dropdown."""
        self._service.refresh_models_async(self)

    def _on_model_picked(self, index: int) -> None:
        """The user selected a row from the dropdown.

        Ignored during programmatic rebuilds (``_suppress_model_picks``)
        and when the selection equals the current active model
        (idempotent click). Otherwise:

        1. Cancel any in-flight inference so a half-streamed
           response from the old model can't corrupt the UI state
           of the new model.
        2. Mark the current worker as abandoned (by unlocking the
           input) so late signals from it are treated as stale.
        3. Ask the service to swap the model. On success the
           service rebuilds the client + assistants and the next
           status probe re-verifies the new model.
        4. Kick a fresh probe so the banner re-validates.
        """
        if self._suppress_model_picks:
            return
        if index < 0:
            return
        target = self._model_combo.itemData(index)
        if not isinstance(target, str) or not target:
            return
        if target == self._active_model_name:
            return

        # (1) + (2) — tear down any in-flight work. We don't wait
        # for the worker to actually exit; the stale-signal guards
        # in the slot handlers discard whatever it eventually emits.
        worker = self._current_worker
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass
        self._unlock_input()

        # (3) — service-level swap. Rebuilds client + assistants
        # and persists the choice. Returns False only if the name
        # isn't installed, which the dropdown already gates against.
        ok = self._service.select_model(target)
        if not ok and target != self._service.config.model:
            # Safety net: swap refused. Snap the combo back to what
            # the manager actually has as current so the UI doesn't
            # lie about the active model.
            self._reselect_current_in_combo(self._active_model_name)
            self.status_message.emit(
                f"Couldn't switch to '{target}' — not installed."
            )
            return

        self._active_model_name = target
        # (4) — re-probe to update the header/banner and verify
        # that the new model is actually usable.
        self._apply_status(AIStatus.checking())
        self._kick_probe(force=True)

    # ── Send / stop ────────────────────────────────────────────────

    def _on_send(self) -> None:
        # Single-flight guard: already running, just ignore extra clicks.
        if self._current_worker is not None:
            return
        prompt = self._input.toPlainText().strip()
        if not prompt:
            return

        # If status is fresh and OK, fire immediately.
        if self._status is not None and self._status.state == "ok":
            self._start_inference(prompt)
            return

        # Otherwise queue the prompt, show a "checking…" banner, and
        # re-probe. The probe callback will fire the send if ok.
        self._pending_send = (self._mode, prompt)
        self._input.setReadOnly(True)
        self._btn_send.setEnabled(False)
        self._apply_status(AIStatus.checking())
        self._kick_probe(force=True)

    def _start_inference(self, prompt: str) -> None:
        """Branch to the current mode's inference path."""
        self._input.setReadOnly(True)
        self._btn_send.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._lbl_copy_state.setText("")

        if self._mode == "command":
            self._start_command_request(prompt)
        else:
            self._start_chat_request(prompt)

    def _on_stop(self) -> None:
        """Request cancellation of the active worker.

        Sets the cooperative cancel flag. The worker's run loop
        breaks on the next chunk boundary (or immediately, via the
        client-level cancel_check hook passed into ``chat_stream``).
        The worker then emits ``cancelled``, which unlocks the UI.
        """
        worker = self._current_worker
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass
        self._btn_stop.setEnabled(False)

    def _unlock_input(self) -> None:
        self._input.setReadOnly(False)
        self._btn_send.setEnabled(
            bool(self._status and self._status.state == "ok")
        )
        self._btn_stop.setEnabled(False)
        self._current_worker = None
        self._current_thread = None

    # ── Command path ───────────────────────────────────────────────

    def _start_command_request(self, prompt: str) -> None:
        self._cmd_raw_buffer = ""
        self._cmd_line.setText("")
        self._cmd_explain.setText("thinking…")
        self._cmd_caution.setVisible(False)
        self._btn_copy.setEnabled(False)
        self._btn_insert.setEnabled(False)

        worker = make_command_worker(self._service, prompt)
        # Bind the worker instance into each slot so late signals
        # from a superseded run can't touch the UI.
        worker.chunk.connect(
            lambda piece, w=worker: self._on_command_chunk(w, piece))
        worker.finished.connect(
            lambda full, w=worker: self._on_command_finished(w, full))
        worker.cancelled.connect(
            lambda full, w=worker: self._on_command_cancelled(w, full))
        worker.failed.connect(
            lambda msg, w=worker: self._on_request_failed(w, msg))
        self._current_worker = worker
        self._current_thread = run_stream_worker(self, worker)

    def _on_command_chunk(self, worker: StreamWorker, piece: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            self._cmd_raw_buffer += piece
            self._cmd_explain.setText(
                "thinking…  ("
                f"{len(self._cmd_raw_buffer)} chars received)"
            )
        except RuntimeError:
            return

    def _on_command_finished(self, worker: StreamWorker, full: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            suggestion: CommandSuggestion = parse_command_response(
                self._cmd_raw_buffer or full
            )
            if suggestion.has_command:
                self._cmd_line.setText(suggestion.command)
                self._cmd_explain.setText(
                    suggestion.explanation
                    or "(model returned a command but no explanation)"
                )
                if suggestion.caution:
                    self._cmd_caution.setText(f"⚠  {suggestion.caution}")
                    self._cmd_caution.setVisible(True)
                else:
                    self._cmd_caution.setVisible(False)
                self._btn_copy.setEnabled(True)
                self._btn_insert.setEnabled(True)
            else:
                self._cmd_line.setText("")
                self._cmd_explain.setText(
                    suggestion.explanation
                    or "(the model couldn't produce a command for that request)"
                )
                self._cmd_caution.setVisible(False)
                self._btn_copy.setEnabled(False)
                self._btn_insert.setEnabled(False)
            self._unlock_input()
        except RuntimeError:
            return

    def _on_command_cancelled(self, worker: StreamWorker, _partial: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            self._cmd_explain.setText("(cancelled)")
            self._cmd_caution.setVisible(False)
            self._btn_copy.setEnabled(False)
            self._btn_insert.setEnabled(False)
            self._unlock_input()
        except RuntimeError:
            return

    def _clear_copy_state_later(self) -> None:
        """
        Clear the copy-feedback label after the 1500ms debounce. Used
        via QTimer.singleShot; guarded so a close-during-feedback
        never hits a deleted QLabel.
        """
        if self._shutting_down:
            return
        try:
            self._lbl_copy_state.setText("")
        except RuntimeError:
            return

    def _on_copy_command(self) -> None:
        if self._shutting_down:
            return
        cmd = self._cmd_line.text().strip()
        if not cmd:
            return
        ok = copy_text(cmd)
        try:
            self._lbl_copy_state.setText("copied" if ok else "copy failed")
        except RuntimeError:
            return
        QTimer.singleShot(1500, self._clear_copy_state_later)

    def _on_insert_to_terminal(self) -> None:
        cmd = self._cmd_line.text().strip()
        if cmd:
            # MainWindow handles switching pages + pre-filling the
            # terminal input. We never submit — the user presses Enter.
            self.insert_to_terminal.emit(cmd)

    # ── Chat path ──────────────────────────────────────────────────

    def _start_chat_request(self, prompt: str) -> None:
        self._pending_user_msg = prompt
        self._chat_stream_buffer = ""
        self._add_user_bubble(prompt)
        self._add_ai_bubble()
        self._input.clear()

        worker = make_chat_worker(self._service, prompt)
        worker.chunk.connect(
            lambda piece, w=worker: self._on_chat_chunk(w, piece))
        worker.finished.connect(
            lambda full, w=worker: self._on_chat_finished(w, full))
        worker.cancelled.connect(
            lambda full, w=worker: self._on_chat_cancelled(w, full))
        worker.failed.connect(
            lambda msg, w=worker: self._on_request_failed(w, msg))
        self._current_worker = worker
        self._current_thread = run_stream_worker(self, worker)

    def _on_chat_chunk(self, worker: StreamWorker, piece: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            self._chat_stream_buffer += piece
            if self._current_ai_text_label is not None:
                self._current_ai_text_label.setText(self._chat_stream_buffer)
            self._scroll_chat_to_bottom()
        except RuntimeError:
            return

    def _on_chat_finished(self, worker: StreamWorker, full: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        if full:
            try:
                self._service.chat_assistant.record_exchange(
                    self._pending_user_msg, full,
                )
            except Exception:
                pass
        try:
            self._current_ai_text_label = None
            self._scroll_chat_to_bottom()
            self._unlock_input()
        except RuntimeError:
            return

    def _on_chat_cancelled(self, worker: StreamWorker, _partial: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            if self._current_ai_text_label is not None:
                existing = self._current_ai_text_label.text()
                self._current_ai_text_label.setText(
                    (existing + "\n\n[cancelled]").strip()
                )
                self._current_ai_text_label = None
            self._unlock_input()
        except RuntimeError:
            return

    def _on_clear_chat(self) -> None:
        self._on_stop()
        # Remove all bubble widgets from the messages layout
        while self._chat_messages_layout.count():
            item = self._chat_messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_messages_layout.addStretch(1)
        self._current_ai_text_label = None
        self._chat_panel_stack.setCurrentIndex(0)
        self._service.chat_assistant.clear()

    def _append_chat_role(
        self,
        role: Optional[str],
        text: str,
    ) -> None:
        cursor = self._chat_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if role == "you":
            cursor.insertText(f"\n>>> you\n{text}\n\n")
        elif role == "assistant":
            cursor.insertText("<<< assistant\n")
        else:
            cursor.insertText("\n")
        self._chat_log.setTextCursor(cursor)
        self._chat_log.ensureCursorVisible()

    # ── Shared failure handler ─────────────────────────────────────

    def _on_request_failed(self, worker: StreamWorker, message: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            if self._mode == "command":
                self._cmd_explain.setText(f"error: {message}")
                self._cmd_caution.setVisible(False)
                self._btn_copy.setEnabled(False)
                self._btn_insert.setEnabled(False)
            else:
                if self._current_ai_text_label is not None:
                    existing = self._current_ai_text_label.text()
                    self._current_ai_text_label.setText(
                        (existing + f"\n\n[error: {message}]").strip()
                    )
                    self._current_ai_text_label = None
                else:
                    self._add_ai_bubble()
                    if self._current_ai_text_label is not None:
                        self._current_ai_text_label.setText(
                            f"[error: {message}]"
                        )
                        self._current_ai_text_label = None
            self.status_message.emit(f"AI error — {message[:80]}")
            self._unlock_input()
        except RuntimeError:
            return
        self._kick_probe(force=True)

    # ── Theme ──────────────────────────────────────────────────────

    def _restyle(self, t) -> None:
        accent2 = t.accent2 or t.accent
        mono = (
            "'JetBrains Mono', 'Cascadia Mono', 'Consolas', monospace"
        )
        self.setStyleSheet(
            # Mode toggle
            f"QPushButton#ai_mode_btn {{"
            f"  background: {t.bg_raised}; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 6px 16px; min-height: 28px;"
            f"  font-family: {mono}; font-size: 12px; font-weight: 700;"
            f"}}"
            f"QPushButton#ai_mode_btn:hover {{"
            f"  color: {t.text}; border-color: {t.accent_dim};"
            f"}}"
            f"QPushButton#ai_mode_btn:checked {{"
            f"  color: {t.accent}; border-color: {t.accent};"
            f"  background: {t.accent_bg};"
            f"}}"
            # Version / retry
            f"QLabel#ai_version_label {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 11px;"
            f"}}"
            f"QPushButton#ai_retry_btn {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  padding: 4px 12px; font-size: 11px;"
            f"}}"
            f"QPushButton#ai_retry_btn:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"}}"
            # Model selector
            f"QLabel#ai_model_title {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 11px;"
            f"  font-weight: 700;"
            f"}}"
            f"QComboBox#ai_model_combo {{"
            f"  background: {t.bg_raised}; color: {t.text};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  padding: 3px 10px; font-family: {mono}; font-size: 11px;"
            f"  min-height: 22px;"
            f"}}"
            f"QComboBox#ai_model_combo:hover {{"
            f"  border-color: {t.accent_dim}; color: {t.text};"
            f"}}"
            f"QComboBox#ai_model_combo:focus {{"
            f"  border-color: {t.accent};"
            f"}}"
            f"QComboBox#ai_model_combo:disabled {{"
            f"  color: {t.text_dim}; border-color: {t.border};"
            f"  background: transparent;"
            f"}}"
            f"QComboBox#ai_model_combo::drop-down {{"
            f"  border: 0px; width: 18px;"
            f"}}"
            f"QComboBox#ai_model_combo QAbstractItemView {{"
            f"  background: {t.bg_raised}; color: {t.text};"
            f"  border: 1px solid {t.border_lt};"
            f"  selection-background-color: {t.accent_bg};"
            f"  selection-color: {t.accent};"
            f"  font-family: {mono}; font-size: 11px;"
            f"  padding: 2px;"
            f"}}"
            f"QPushButton#ai_model_refresh {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  padding: 3px 0px; font-size: 12px; font-weight: 800;"
            f"  min-height: 22px;"
            f"}}"
            f"QPushButton#ai_model_refresh:hover:enabled {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"}}"
            f"QPushButton#ai_model_refresh:disabled {{"
            f"  color: {t.text_dim}; border-color: {t.border};"
            f"}}"
            f"QFrame#ai_header_sep {{"
            f"  color: {t.border_lt};"
            f"}}"
            # Banner
            f"QFrame#ai_banner {{"
            f"  background: {t.bg_raised};"
            f"  border: 1px solid {t.accent_dim}; border-radius: 8px;"
            f"}}"
            f"QLabel#ai_banner_title {{"
            f"  color: {accent2}; font-family: {mono}; font-size: 12px;"
            f"  font-weight: 900;"
            f"}}"
            f"QLabel#ai_banner_msg {{"
            f"  color: {t.text}; font-size: 12px;"
            f"}}"
            f"QLabel#ai_banner_remedy {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 11px;"
            f"}}"
            # Hints
            f"QLabel#ai_hint {{ color: {t.text_dim}; font-size: 12px; }}"
            # Command box
            f"QFrame#ai_cmd_box {{"
            f"  background: {t.bg_raised};"
            f"  border: 1px solid {t.border_lt}; border-radius: 8px;"
            f"}}"
            f"QLabel#ai_section_label {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 10px;"
            f"  font-weight: 800;"
            f"}}"
            f"QLineEdit#ai_cmd_line {{"
            f"  background: {t.bg_base}; color: {t.accent};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 6px 10px; font-size: 12px;"
            f"}}"
            f"QPushButton#ai_cmd_action {{"
            f"  background: {t.bg_base}; color: {t.text};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 5px 14px; font-size: 11px; font-weight: 700;"
            f"}}"
            f"QPushButton#ai_cmd_action:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"  background: {t.accent_bg};"
            f"}}"
            f"QPushButton#ai_cmd_action:disabled {{"
            f"  color: {t.text_dim}; border-color: {t.border};"
            f"  background: transparent;"
            f"}}"
            f"QLabel#ai_copy_state {{"
            f"  color: {accent2}; font-family: {mono}; font-size: 11px;"
            f"  font-weight: 700;"
            f"}}"
            f"QLabel#ai_cmd_explain {{"
            f"  color: {t.text}; font-size: 12px;"
            f"}}"
            f"QLabel#ai_cmd_caution {{"
            f"  color: {accent2}; font-size: 12px; font-weight: 700;"
            f"}}"
            # Chat panel — new chat interface
            f"QPushButton#ai_clear_chat {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 5px 14px; font-size: 11px; font-weight: 600;"
            f"}}"
            f"QPushButton#ai_clear_chat:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"}}"
            # Welcome screen
            f"QWidget#ai_welcome {{ background: transparent; }}"
            f"QLabel#ai_welcome_title {{"
            f"  color: {t.text};"
            f"  font-family: {mono}; font-size: 26px; font-weight: 900;"
            f"  letter-spacing: 1px;"
            f"}}"
            f"QLabel#ai_welcome_sub {{"
            f"  color: {t.text_dim}; font-size: 14px;"
            f"}}"
            f"QLabel#ai_disclaimer {{"
            f"  color: {t.text_dim}; font-size: 10px;"
            f"}}"
            # Suggestion cards
            f"QFrame#ai_suggest_card {{"
            f"  background: {t.bg_raised};"
            f"  border: 1px solid {t.border_lt}; border-radius: 10px;"
            f"  min-width: 220px; max-width: 300px;"
            f"}}"
            f"QFrame#ai_suggest_card:hover {{"
            f"  border-color: {t.accent_dim}; background: {t.accent_bg};"
            f"}}"
            f"QLabel#ai_suggest_title {{"
            f"  color: {t.text}; font-size: 12px; font-weight: 700;"
            f"}}"
            f"QLabel#ai_suggest_desc {{"
            f"  color: {t.text_dim}; font-size: 11px;"
            f"}}"
            # Chat scroll area + messages container
            f"QScrollArea#ai_chat_scroll {{ background: transparent; border: none; }}"
            f"QWidget#ai_chat_messages {{ background: transparent; }}"
            # User bubble (right side)
            f"QFrame#ai_user_bubble {{"
            f"  background: {t.accent_bg};"
            f"  border: 1px solid {t.accent_dim}; border-radius: 16px;"
            f"  border-bottom-right-radius: 4px;"
            f"}}"
            f"QLabel#ai_bubble_text_user {{"
            f"  color: {t.text}; font-size: 13px;"
            f"}}"
            # AI bubble (left side)
            f"QFrame#ai_ai_bubble {{"
            f"  background: {t.bg_raised};"
            f"  border: 1px solid {t.border_lt}; border-radius: 16px;"
            f"  border-bottom-left-radius: 4px;"
            f"}}"
            f"QLabel#ai_bubble_role {{"
            f"  color: {t.accent}; font-family: {mono}; font-size: 10px;"
            f"  font-weight: 800; letter-spacing: 1px;"
            f"}}"
            f"QLabel#ai_bubble_text_ai {{"
            f"  color: {t.text}; font-size: 13px;"
            f"}}"
            # Unified input frame
            f"QFrame#ai_input_frame {{"
            f"  background: {t.bg_input};"
            f"  border: 1px solid {t.border_lt}; border-radius: 14px;"
            f"}}"
            f"QFrame#ai_input_frame:focus-within {{"
            f"  border-color: {t.accent};"
            f"}}"
            f"QPlainTextEdit#ai_input {{"
            f"  background: transparent; color: {t.text};"
            f"  border: none;"
            f"  font-family: {mono}; font-size: 13px;"
            f"}}"
            f"QPushButton#ai_send_btn {{"
            f"  background: {t.accent}; color: {t.bg_base};"
            f"  border: none; border-radius: 18px;"
            f"  font-size: 18px; font-weight: 900; padding: 0px;"
            f"}}"
            f"QPushButton#ai_send_btn:hover:enabled {{ opacity: 0.85; }}"
            f"QPushButton#ai_send_btn:disabled {{"
            f"  background: {t.bg_raised}; color: {t.text_dim};"
            f"}}"
            f"QPushButton#ai_stop_btn {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 18px;"
            f"  font-size: 10px; font-weight: 900; padding: 0px;"
            f"}}"
            f"QPushButton#ai_stop_btn:hover:enabled {{"
            f"  color: {t.red}; border-color: {t.red}; background: transparent;"
            f"}}"
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    def on_entered(self) -> None:
        """Called by MainWindow when the user navigates onto this page.

        Re-checks Ollama status so the banner reflects reality if the
        user started the daemon since the last visit. The probe runs
        asynchronously on a QThread, so switching onto this page is
        instant even when Ollama is unreachable (the old synchronous
        path could block page switching for up to 8 seconds).
        """
        self._kick_probe()

    def shutdown(self) -> None:
        """Called by MainWindow on app close.

        Cancels any in-flight inference, force-closes the HTTP session
        to unblock a pending network read, and waits on the worker
        thread with a bounded timeout so Qt never tears down a QThread
        while its ``run()`` is still executing.
        """
        # 0. Flip the flag first so every late signal + QTimer
        #    singleShot callback drops silently instead of touching
        #    a torn-down view.
        self._shutting_down = True

        # 1. Flip the cooperative cancel flag.
        worker = self._current_worker
        if worker is not None:
            try:
                worker.cancel()
            except Exception:
                pass
        # 2. Drop the session — this force-unblocks a pending stream
        #    read so the worker exits its loop even if Ollama isn't
        #    producing any more tokens.
        try:
            self._service.shutdown()
        except Exception:
            pass
        # 3. Wait for the inference thread to actually finish.
        inference_thread = self._current_thread
        if inference_thread is not None:
            try:
                inference_thread.wait(3000)
            except Exception:
                pass
        # 4. And the probe thread, if one is still up.
        probe_thread = self._probe_thread
        if probe_thread is not None:
            try:
                probe_thread.wait(3000)
            except Exception:
                pass
