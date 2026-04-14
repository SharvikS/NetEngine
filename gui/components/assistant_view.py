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

Key design points:

* **No blocking I/O on the GUI thread.** Every Ollama call runs on a
  ``StreamWorker`` moved to a QThread. The UI receives chunks via
  signals.

* **Fully graceful offline state.** If Ollama is unreachable or the
  configured model isn't installed, the banner shows the exact
  remedy text from ``AIService.status()`` and the Send button is
  disabled. No exceptions propagate to the app.

* **Safe command workflow.** Suggested commands are rendered in a
  read-only box with a Copy button. This view never executes a
  command. The user copies, pastes, reviews, runs — their choice.

* **Prompts + model config are centralised.** This file contains
  zero prompt strings; everything flows through ``ai.prompts``.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QTextCursor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QTextBrowser, QFrame, QStackedWidget, QSizePolicy, QApplication,
    QLineEdit,
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
from gui.themes import theme, ThemeManager


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
        self._current_thread: Optional[QThread] = None
        self._current_worker: Optional[StreamWorker] = None
        self._mode = "command"

        # Per-run state — cleared on each new request.
        self._cmd_raw_buffer = ""
        self._chat_stream_buffer = ""
        self._pending_user_msg = ""

        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

        # First connectivity check runs a tick after show so the window
        # is already visible even if the ping takes the full 4s timeout.
        QTimer.singleShot(150, self._refresh_status)

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

        self._lbl_version = QLabel("")
        self._lbl_version.setObjectName("ai_version_label")
        self._lbl_version.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self._lbl_version)

        self._btn_retry = QPushButton("Retry")
        self._btn_retry.setObjectName("ai_retry_btn")
        self._btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_retry.clicked.connect(self._refresh_status)
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

        # ── Bottom input row ───────────────────────────────────────
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self._input = QPlainTextEdit()
        self._input.setObjectName("ai_input")
        self._input.setPlaceholderText(
            "Ask a command…  (Ctrl+Enter to send)"
        )
        self._input.setFixedHeight(68)
        self._input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        input_row.addWidget(self._input, stretch=1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(6)
        self._btn_send = QPushButton("Send")
        self._btn_send.setObjectName("ai_send_btn")
        self._btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_send.clicked.connect(self._on_send)
        btn_col.addWidget(self._btn_send)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setObjectName("ai_stop_btn")
        self._btn_stop.setEnabled(False)
        self._btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_stop.clicked.connect(self._on_stop)
        btn_col.addWidget(self._btn_stop)
        input_row.addLayout(btn_col)

        root.addLayout(input_row)

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
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        top_row = QHBoxLayout()
        hint = QLabel(
            "Ask about scan results, terminal output, session state, "
            "or anything in the app. Streaming responses, no cloud."
        )
        hint.setWordWrap(True)
        hint.setObjectName("ai_hint")
        top_row.addWidget(hint, stretch=1)

        self._btn_clear_chat = QPushButton("Clear")
        self._btn_clear_chat.setObjectName("ai_clear_chat")
        self._btn_clear_chat.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_chat.clicked.connect(self._on_clear_chat)
        top_row.addWidget(self._btn_clear_chat)

        lay.addLayout(top_row)

        self._chat_log = QTextBrowser()
        self._chat_log.setObjectName("ai_chat_log")
        self._chat_log.setOpenExternalLinks(True)
        self._chat_log.setFont(_mono_font())
        lay.addWidget(self._chat_log, stretch=1)

        return w

    # ── Mode switching ─────────────────────────────────────────────

    def _switch_mode(self, mode: str) -> None:
        if mode == self._mode:
            self._btn_mode_command.setChecked(mode == "command")
            self._btn_mode_chat.setChecked(mode == "chat")
            return
        # Stop any in-flight request — each mode has its own prompt
        # path, so we don't want a half-stream from the previous mode
        # landing in the wrong panel.
        self._on_stop()
        self._mode = mode
        self._btn_mode_command.setChecked(mode == "command")
        self._btn_mode_chat.setChecked(mode == "chat")
        self._stack.setCurrentIndex(0 if mode == "command" else 1)
        self._input.setPlaceholderText(
            "Ask a command…  (Ctrl+Enter to send)"
            if mode == "command"
            else "Ask for help…  (Ctrl+Enter to send)"
        )

    # ── Status / banner ────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """Run the (fast, short-timeout) health check on the current thread.

        The check is bounded by the client's internal ``_HEALTH_TIMEOUT``
        (4 s), and only fires on show + on user click — it is not in
        the inference path — so running it on the GUI thread is fine
        and keeps the "did it work?" logic trivial. In exchange the
        user sees the banner flip immediately after clicking Retry.
        """
        status = self._service.status()
        self._apply_status(status)

    def _apply_status(self, status: AIStatus) -> None:
        self._status = status
        if status.ok:
            self._banner.setVisible(False)
            self._lbl_version.setText(status.message)
            self._btn_send.setEnabled(True)
            self.status_message.emit(f"AI ready — {status.message}")
        else:
            self._banner.setVisible(True)
            self._banner_title.setText(
                "Local AI disabled"
                if not status.reachable and "disabled" in status.message.lower()
                else ("Ollama not reachable"
                      if not status.reachable
                      else "Model not installed")
            )
            self._banner_msg.setText(status.message)
            self._banner_remedy.setText(status.remedy)
            self._lbl_version.setText("offline")
            self._btn_send.setEnabled(False)
            self.status_message.emit("AI unavailable — see banner")

    # ── Send / stop ────────────────────────────────────────────────

    def _on_send(self) -> None:
        if self._current_worker is not None:
            return  # already running
        prompt = self._input.toPlainText().strip()
        if not prompt:
            return
        # Re-check status on every send so the user never gets a
        # silent hang when Ollama was killed between requests.
        if self._status is None or not self._status.ok:
            self._refresh_status()
            if self._status is None or not self._status.ok:
                return

        self._input.setReadOnly(True)
        self._btn_send.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._lbl_copy_state.setText("")

        if self._mode == "command":
            self._start_command_request(prompt)
        else:
            self._start_chat_request(prompt)

    def _on_stop(self) -> None:
        worker = self._current_worker
        if worker is not None:
            worker.cancel()
        self._btn_stop.setEnabled(False)

    def _unlock_input(self) -> None:
        self._input.setReadOnly(False)
        self._btn_send.setEnabled(
            bool(self._status and self._status.ok)
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
        worker.chunk.connect(self._on_command_chunk)
        worker.finished.connect(self._on_command_finished)
        worker.failed.connect(self._on_request_failed)
        self._current_worker = worker
        self._current_thread = run_stream_worker(self, worker)

    @pyqtSlot(str)
    def _on_command_chunk(self, piece: str) -> None:
        # We don't re-parse on every chunk — a command response is
        # short and the strict format makes mid-stream previews noisy.
        # Just show an animated "thinking" hint and wait for finished.
        self._cmd_raw_buffer += piece
        self._cmd_explain.setText(
            "thinking…  ("
            f"{len(self._cmd_raw_buffer)} chars received)"
        )

    @pyqtSlot(str)
    def _on_command_finished(self, _full: str) -> None:
        suggestion: CommandSuggestion = parse_command_response(
            self._cmd_raw_buffer or _full
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

    def _on_copy_command(self) -> None:
        cmd = self._cmd_line.text().strip()
        if not cmd:
            return
        QApplication.clipboard().setText(cmd)
        self._lbl_copy_state.setText("copied")
        QTimer.singleShot(1500, lambda: self._lbl_copy_state.setText(""))

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
        # Append the user's message to the log immediately so the
        # exchange feels reactive even before the first streamed token.
        self._append_chat_role("you", prompt)
        self._append_chat_role("assistant", "")
        self._input.clear()

        worker = make_chat_worker(self._service, prompt)
        worker.chunk.connect(self._on_chat_chunk)
        worker.finished.connect(self._on_chat_finished)
        worker.failed.connect(self._on_request_failed)
        self._current_worker = worker
        self._current_thread = run_stream_worker(self, worker)

    @pyqtSlot(str)
    def _on_chat_chunk(self, piece: str) -> None:
        self._chat_stream_buffer += piece
        cursor = self._chat_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(piece)
        self._chat_log.setTextCursor(cursor)
        self._chat_log.ensureCursorVisible()

    @pyqtSlot(str)
    def _on_chat_finished(self, full: str) -> None:
        if full:
            self._service.chat_assistant.record_exchange(
                self._pending_user_msg, full,
            )
        self._append_chat_role(None, "")  # trailing blank line
        self._unlock_input()

    def _on_clear_chat(self) -> None:
        self._on_stop()
        self._chat_log.clear()
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

    @pyqtSlot(str)
    def _on_request_failed(self, message: str) -> None:
        if self._mode == "command":
            self._cmd_explain.setText(f"error: {message}")
            self._cmd_caution.setVisible(False)
            self._btn_copy.setEnabled(False)
            self._btn_insert.setEnabled(False)
        else:
            self._append_chat_role(None, "")
            cursor = self._chat_log.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(f"[error] {message}\n")
            self._chat_log.setTextCursor(cursor)
        self.status_message.emit(f"AI error — {message[:80]}")
        self._unlock_input()
        # A failure means the connection or model is likely broken —
        # re-run the status probe so the banner updates accordingly.
        self._refresh_status()

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
            # Chat log
            f"QTextBrowser#ai_chat_log {{"
            f"  background: {t.bg_base}; color: {t.text};"
            f"  border: 1px solid {t.border_lt}; border-radius: 8px;"
            f"  padding: 10px;"
            f"}}"
            f"QPushButton#ai_clear_chat {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  padding: 4px 12px; font-size: 11px;"
            f"}}"
            f"QPushButton#ai_clear_chat:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"}}"
            # Input + action buttons
            f"QPlainTextEdit#ai_input {{"
            f"  background: {t.bg_input}; color: {t.text};"
            f"  border: 1px solid {t.border_lt}; border-radius: 8px;"
            f"  padding: 8px 10px; font-family: {mono}; font-size: 12px;"
            f"}}"
            f"QPlainTextEdit#ai_input:focus {{"
            f"  border-color: {t.accent};"
            f"}}"
            f"QPushButton#ai_send_btn {{"
            f"  background: {t.accent_bg}; color: {t.accent};"
            f"  border: 1px solid {t.accent}; border-radius: 6px;"
            f"  padding: 8px 20px; min-width: 72px;"
            f"  font-family: {mono}; font-size: 12px; font-weight: 800;"
            f"}}"
            f"QPushButton#ai_send_btn:hover {{ background: {t.bg_hover}; }}"
            f"QPushButton#ai_send_btn:disabled {{"
            f"  color: {t.text_dim}; border-color: {t.border};"
            f"  background: transparent;"
            f"}}"
            f"QPushButton#ai_stop_btn {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 6px 16px; font-size: 11px; font-weight: 700;"
            f"}}"
            f"QPushButton#ai_stop_btn:hover:enabled {{"
            f"  color: {t.red}; border-color: {t.red};"
            f"}}"
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    def on_entered(self) -> None:
        """Called by MainWindow when the user navigates onto this page.

        Re-checks Ollama status so the banner reflects reality if the
        user started the daemon since the last visit. Cheap: health
        check has a 4s cap and runs on the GUI thread.
        """
        self._refresh_status()

    def shutdown(self) -> None:
        """Called by MainWindow on app close — cancel any in-flight
        request so the background thread dies cleanly."""
        self._on_stop()
