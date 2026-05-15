"""
AI Assistant page — unified chat with automatic command detection.

Layout::

    +---------------------------------------------------------------+
    |  Model: [dropdown] [↻]   Ollama 0.x · model llama3.2 [Retry] |
    +---------------------------------------------------------------+
    |  [status banner — only when AI is unavailable]                |
    +---------------------------------------------------------------+
    |  [history sidebar] |  [chat history toggle]  [+ New chat]     |
    |                    |                                           |
    |                    |  <chat bubbles — welcome screen or msgs>  |
    |                    |                                           |
    +---------------------------------------------------------------+
    |  input textarea                                   [Send] [Stop]|
    +---------------------------------------------------------------+

Command detection: the AI is prompted to respond with a structured
COMMAND:/EXPLAIN:/CAUTION: block when the user asks for a shell
command. The view detects this format on completion and renders an
inline command card with Copy and Insert-into-Terminal buttons instead
of plain markdown — without requiring a separate Command mode.

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

import html as _html_lib
import re as _re
from typing import Optional

import time as _time

from PyQt6.QtCore import (
    Qt, QSize, QThread, QTimer,
    QPropertyAnimation, QEasingCurve,
    pyqtSignal, pyqtSlot, pyqtProperty,
)
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
    QTextBrowser, QFrame, QStackedWidget, QSizePolicy, QApplication,
    QLineEdit, QComboBox, QScrollArea, QGridLayout,
    QListWidget, QListWidgetItem, QMenu,
)

from ai.chat_history import ChatHistoryManager, ChatSession, ChatMessage, auto_title, relative_time
from ai.ai_service import (
    AIService,
    AIStatus,
    StreamWorker,
    make_chat_worker,
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


# ── Markdown renderer ──────────────────────────────────────────────────────


def _format_inline(raw: str) -> str:
    """Convert inline markdown in *raw* to an HTML-safe string."""
    # Extract code spans so their content is not further processed.
    spans: list[str] = []

    def _pull(m: _re.Match) -> str:
        spans.append(_html_lib.escape(m.group(1)))
        return f"\x02{len(spans) - 1}\x03"

    s = _re.sub(r"`([^`\n]+)`", _pull, raw)
    s = _html_lib.escape(s)
    # Bold+italic ***
    s = _re.sub(r"\*{3}(.+?)\*{3}", r"<b><em>\1</em></b>", s)
    # Bold **  or __
    s = _re.sub(r"\*{2}(.+?)\*{2}", r"<b>\1</b>", s)
    s = _re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    # Italic *  (not adjacent to another *)
    s = _re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", s)
    # Italic _  (not adjacent to word char)
    s = _re.sub(r"(?<![_\w])_([^_\n]+)_(?![_\w])", r"<em>\1</em>", s)
    # Restore code spans
    _CODE_STYLE = (
        "font-family:'Consolas','Courier New',monospace;"
        "background:rgba(128,128,128,0.15);"
        "padding:1px 5px;border-radius:3px;font-size:0.92em;"
    )
    for i, code in enumerate(spans):
        s = s.replace(
            f"\x02{i}\x03",
            f'<code style="{_CODE_STYLE}">{code}</code>',
        )
    return s


def _md_to_html(text: str) -> str:
    """Convert a markdown string to Qt-compatible HTML with UTF-8 charset."""
    if not text:
        return ""

    _FENCE_STYLE = (
        "background:rgba(0,0,0,0.12);border-radius:6px;"
        "padding:12px 14px;margin:8px 0;"
        "font-family:'Consolas','Courier New',monospace;font-size:12px;"
        "white-space:pre-wrap;overflow-wrap:break-word;"
    )
    _HR_STYLE = "border:none;border-top:1px solid rgba(128,128,128,0.3);margin:12px 0;"
    _UL_STYLE = "margin:4px 0;padding-left:20px;"
    _OL_STYLE = "margin:4px 0;padding-left:20px;"
    _LI_STYLE = "margin:2px 0;"
    _H_SIZE = {1: "18px", 2: "15px", 3: "14px", 4: "13px", 5: "12px", 6: "12px"}
    _H_WEIGHT = {1: "800", 2: "700", 3: "700", 4: "600", 5: "600", 6: "600"}
    _H_MARGIN = {1: "14px 0 4px 0", 2: "12px 0 4px 0", 3: "10px 0 2px 0"}

    lines = text.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            escaped = _html_lib.escape("\n".join(body))
            out.append(f'<pre style="{_FENCE_STYLE}">{escaped}</pre>')
            continue

        # Horizontal rule
        if _re.fullmatch(r"[-*_]{3,}\s*", stripped):
            out.append(f'<hr style="{_HR_STYLE}">')
            i += 1
            continue

        # ATX heading
        m = _re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            lvl = min(len(m.group(1)), 6)
            body_html = _format_inline(m.group(2))
            margin = _H_MARGIN.get(lvl, "10px 0 2px 0")
            out.append(
                f'<p style="font-size:{_H_SIZE[lvl]};'
                f'font-weight:{_H_WEIGHT[lvl]};margin:{margin};">'
                f"{body_html}</p>"
            )
            i += 1
            continue

        # Bullet list
        if _re.match(r"^[-*+]\s+", line):
            items: list[str] = []
            while i < len(lines) and _re.match(r"^[-*+]\s+", lines[i]):
                items.append(_format_inline(lines[i][2:].lstrip()))
                i += 1
            lis = "".join(f'<li style="{_LI_STYLE}">{it}</li>' for it in items)
            out.append(f'<ul style="{_UL_STYLE}">{lis}</ul>')
            continue

        # Numbered list
        if _re.match(r"^\d+[.)]\s+", line):
            items = []
            while i < len(lines) and _re.match(r"^\d+[.)]\s+", lines[i]):
                items.append(_format_inline(_re.sub(r"^\d+[.)]\s+", "", lines[i])))
                i += 1
            lis = "".join(f'<li style="{_LI_STYLE}">{it}</li>' for it in items)
            out.append(f'<ol style="{_OL_STYLE}">{lis}</ol>')
            continue

        # Blank line
        if not stripped:
            out.append("<br>")
            i += 1
            continue

        # Paragraph — accumulate consecutive non-special lines
        para: list[str] = []
        while i < len(lines):
            l = lines[i]
            st = l.strip()
            if (
                not st
                or st.startswith("```")
                or _re.fullmatch(r"[-*_]{3,}\s*", st)
                or _re.match(r"^#{1,6}\s", l)
                or _re.match(r"^[-*+]\s+", l)
                or _re.match(r"^\d+[.)]\s+", l)
            ):
                break
            para.append(_format_inline(l))
            i += 1
        if para:
            out.append(
                '<p style="margin:4px 0;line-height:1.6;">'
                + "<br>".join(para)
                + "</p>"
            )

    body = "".join(out)
    # Wrap in a proper HTML document with UTF-8 charset so Qt's renderer
    # correctly handles em dashes, curly quotes, and other non-ASCII chars.
    return (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8"/>'
        '</head><body style="margin:0;padding:0;">'
        + body
        + '</body></html>'
    )


# ── AI bubble text widget ──────────────────────────────────────────────────


class _AIBubbleText(QTextBrowser):
    """Auto-sizing rich-text display for AI chat bubbles.

    Streams plain text during inference, then renders markdown HTML when the
    response is complete. Height tracks document content so the bubble grows
    naturally inside the scroll area without internal scrollbars.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._plain_buf: str = ""
        self.document().documentLayout().documentSizeChanged.connect(
            lambda _: self.updateGeometry()
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        w = self.viewport().width()
        if w > 0:
            self.document().setTextWidth(w)
        h = int(self.document().size().height()) + 4
        return QSize(max(w, 100), max(h, 20))

    def minimumSizeHint(self) -> QSize:
        return QSize(100, 20)

    def set_plain_stream(self, text: str) -> None:
        """Display raw text during streaming (no markdown processing)."""
        self._plain_buf = text
        self.setPlainText(text)

    def set_rendered(self, text: str) -> None:
        """Render *text* as markdown HTML (called when streaming is done)."""
        self._plain_buf = text
        self.setHtml(_md_to_html(text))
        self.updateGeometry()

    def plain_text(self) -> str:
        """Return the original markdown source (used for copy / error append)."""
        return self._plain_buf


# ── Auto-growing prompt input ──────────────────────────────────────────────


class _GrowingInput(QPlainTextEdit):
    """Prompt input that smoothly expands from 1 line up to 8 lines.

    Height is animated via a ``QPropertyAnimation`` on a custom Qt property
    so the resize is frame-interpolated and glitch-free. A vertical scrollbar
    appears automatically once the content exceeds the 8-line cap.
    """

    _MIN_LINES = 1
    _MAX_LINES = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._resize_pending = False

        # Animation drives both min/max height together through the
        # 'animHeight' Qt property defined below.
        self._anim = QPropertyAnimation(self, b"animHeight")
        self._anim.setDuration(130)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.document().contentsChanged.connect(self._schedule_resize)

    # ── Animatable Qt property ─────────────────────────────────────

    def _get_anim_h(self) -> int:
        return self.minimumHeight()

    def _set_anim_h(self, h: int) -> None:
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)

    animHeight = pyqtProperty(int, fget=_get_anim_h, fset=_set_anim_h)

    # ── Resize logic ───────────────────────────────────────────────

    def _schedule_resize(self) -> None:
        """Defer the resize to the next event-loop tick so the document
        layout has already settled before we measure it."""
        if not self._resize_pending:
            self._resize_pending = True
            QTimer.singleShot(0, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_pending = False
        vw = self.viewport().width()
        if vw <= 0:
            return

        doc = self.document()
        doc.setTextWidth(vw)
        content_h = int(doc.size().height())

        fm = self.fontMetrics()
        line_h = fm.lineSpacing() or 18
        # Vertical padding = frame borders + content margins + a little breathing room
        pad = (self.frameWidth() * 2
               + self.contentsMargins().top()
               + self.contentsMargins().bottom()
               + 10)

        min_h = line_h * self._MIN_LINES + pad
        max_h = line_h * self._MAX_LINES + pad
        target = max(min_h, min(max_h, content_h + pad))

        # Show the scrollbar only when content overflows the 8-line cap
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if content_h + pad > max_h
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        cur = self.minimumHeight()
        if cur <= 0:
            # First paint — set directly, no animation needed
            self._set_anim_h(target)
            return
        if abs(target - cur) < 2:
            return  # avoid micro-jitter on every keystroke

        self._anim.stop()
        self._anim.setStartValue(cur)
        self._anim.setEndValue(target)
        self._anim.start()

    # ── Qt event hooks ─────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Measure after the widget is actually laid out
        QTimer.singleShot(10, self._do_resize)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Viewport width changed — content may reflow to more/fewer lines
        self._schedule_resize()


# ── Inline command card ────────────────────────────────────────────────────


class _CommandCard(QFrame):
    """Inline command suggestion card rendered inside an AI chat bubble.

    Displays the suggested command, explanation, optional caution, and
    Copy / Insert-into-Terminal action buttons.  The card never
    executes anything — Insert only pre-fills the terminal input; the
    user must press Enter themselves.
    """

    insert_to_terminal = pyqtSignal(str)

    def __init__(self, suggestion, parent=None):
        super().__init__(parent)
        self.setObjectName("ai_cmd_card")
        self._command = (suggestion.command or "").strip()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        section_lbl = QLabel("SUGGESTED COMMAND")
        section_lbl.setObjectName("ai_cmd_card_label")
        lay.addWidget(section_lbl)

        self._cmd_display = QLineEdit(self._command)
        self._cmd_display.setReadOnly(True)
        self._cmd_display.setObjectName("ai_cmd_card_line")
        self._cmd_display.setFont(_mono_font())
        lay.addWidget(self._cmd_display)

        if suggestion.explanation:
            explain_lbl = QLabel(suggestion.explanation)
            explain_lbl.setObjectName("ai_cmd_card_explain")
            explain_lbl.setWordWrap(True)
            explain_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(explain_lbl)

        if suggestion.caution:
            caution_lbl = QLabel(f"⚠  {suggestion.caution}")
            caution_lbl.setObjectName("ai_cmd_card_caution")
            caution_lbl.setWordWrap(True)
            lay.addWidget(caution_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.setContentsMargins(0, 4, 0, 0)

        self._btn_copy = QPushButton("⎘ Copy")
        self._btn_copy.setObjectName("ai_cmd_card_action")
        self._btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_copy.clicked.connect(self._on_copy)
        btn_row.addWidget(self._btn_copy)

        if self._command:
            btn_insert = QPushButton("Insert into Terminal")
            btn_insert.setObjectName("ai_cmd_card_action")
            btn_insert.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_insert.setToolTip(
                "Pre-fill the Terminal tab's input with this command.\n"
                "You must press Enter to run it."
            )
            btn_insert.clicked.connect(
                lambda: self.insert_to_terminal.emit(self._command))
            btn_row.addWidget(btn_insert)

        btn_row.addStretch(1)
        lay.addLayout(btn_row)

    def _on_copy(self) -> None:
        if not self._command:
            return
        ok = copy_text(self._command)
        try:
            self._btn_copy.setText("✓ Copied" if ok else "✗ Failed")
        except RuntimeError:
            return
        QTimer.singleShot(1500, self._reset_copy_btn)

    def _reset_copy_btn(self) -> None:
        try:
            self._btn_copy.setText("⎘ Copy")
        except RuntimeError:
            pass


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

        # Per-run state — cleared on each new request.
        self._chat_stream_buffer = ""
        self._pending_user_msg = ""
        # Reference to the _AIBubbleText and its parent layout/copy-btn
        # currently being streamed into. Cleared when the response ends.
        self._current_ai_widget: Optional[_AIBubbleText] = None
        self._current_bubble_layout: Optional[QVBoxLayout] = None
        self._current_copy_btn: Optional[QPushButton] = None

        # Chat history persistence
        self._history_manager = ChatHistoryManager()
        self._current_session: ChatSession = ChatSession.new()
        self._history_sidebar_visible = True

        # If the user hits Send before we've ever probed (or while a
        # probe is in flight), we stash the prompt here and send it
        # once the probe returns ok.
        self._pending_send: str = ""

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

        # ── Top bar: model selector + version label ────────────────
        top = QHBoxLayout()
        top.setSpacing(8)
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

        # ── Chat panel (the only content panel) ───────────────────
        root.addWidget(self._build_chat_panel(), stretch=1)

        # ── Bottom input area (unified chat-style bar) ─────────────
        input_frame = QFrame()
        input_frame.setObjectName("ai_input_frame")
        input_frame_lay = QHBoxLayout(input_frame)
        input_frame_lay.setContentsMargins(12, 8, 8, 8)
        input_frame_lay.setSpacing(8)

        self._input = _GrowingInput()
        self._input.setObjectName("ai_input")
        self._input.setPlaceholderText("Send a message…  (Ctrl+Enter)")
        self._input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        input_frame_lay.addWidget(self._input, stretch=1)

        # Button column: stretch pushes send/stop to the bottom so they
        # sit at the input baseline when the textarea is tall.
        btns_wrap = QWidget()
        btns_wrap.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        btns = QVBoxLayout(btns_wrap)
        btns.setSpacing(4)
        btns.setContentsMargins(0, 0, 0, 0)
        btns.addStretch(1)

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

        input_frame_lay.addWidget(btns_wrap)
        root.addWidget(input_frame)

        # Ctrl+Enter / Cmd+Enter sends from the input box.
        send_sc = QShortcut(QKeySequence("Ctrl+Return"), self._input)
        send_sc.activated.connect(self._on_send)
        send_sc2 = QShortcut(QKeySequence("Ctrl+Enter"), self._input)
        send_sc2.activated.connect(self._on_send)

    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        outer = QHBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── History sidebar ────────────────────────────────────────
        self._history_sidebar = self._build_history_sidebar()
        outer.addWidget(self._history_sidebar)

        # ── Main chat area ─────────────────────────────────────────
        chat_main = QWidget()
        lay = QVBoxLayout(chat_main)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Top bar: history toggle + new chat button
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 8)

        self._btn_toggle_history = QPushButton("☰")
        self._btn_toggle_history.setObjectName("ai_toggle_history")
        self._btn_toggle_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_toggle_history.setFixedWidth(32)
        self._btn_toggle_history.setToolTip("Toggle chat history")
        self._btn_toggle_history.clicked.connect(self._toggle_history_sidebar)
        top.addWidget(self._btn_toggle_history)

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
        self._chat_messages_layout.addStretch(1)

        self._chat_scroll.setWidget(self._chat_messages_widget)
        self._chat_panel_stack.addWidget(self._chat_scroll)

        lay.addWidget(self._chat_panel_stack, stretch=1)
        outer.addWidget(chat_main, stretch=1)
        return panel

    def _build_history_sidebar(self) -> QFrame:
        """Build the collapsible chat history sidebar."""
        sidebar = QFrame()
        sidebar.setObjectName("ai_history_sidebar")
        sidebar.setFixedWidth(220)
        sidebar.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QFrame()
        header.setObjectName("ai_hist_header")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 8, 8, 8)
        h_lay.setSpacing(4)

        title_lbl = QLabel("Chats")
        title_lbl.setObjectName("ai_hist_header_title")
        h_lay.addWidget(title_lbl)
        h_lay.addStretch(1)

        lay.addWidget(header)

        # Search / list
        self._history_list = QListWidget()
        self._history_list.setObjectName("ai_hist_list")
        self._history_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._history_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_list.itemClicked.connect(self._on_history_item_clicked)
        self._history_list.customContextMenuRequested.connect(
            self._on_history_context_menu)
        lay.addWidget(self._history_list, stretch=1)

        # Seed the list
        self._refresh_history_list()
        return sidebar

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
        row_lay.setSpacing(0)

        bubble = QFrame()
        bubble.setObjectName("ai_user_bubble")
        bubble.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        b_lay = QVBoxLayout(bubble)
        b_lay.setContentsMargins(16, 12, 16, 12)
        b_lay.setSpacing(0)

        lbl = QLabel(text)
        lbl.setObjectName("ai_bubble_text_user")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        b_lay.addWidget(lbl)

        # Spacer:bubble = 1:4  →  bubble gets 80 % of row width, right-aligned
        row_lay.addStretch(1)
        row_lay.addWidget(bubble, stretch=4)

        self._chat_messages_layout.addWidget(row)
        self._scroll_chat_to_bottom()

    def _add_ai_bubble(self, static_text: Optional[str] = None) -> None:
        """Add a left-aligned AI response bubble.

        When *static_text* is provided the bubble is rendered immediately
        (history replay). If the text is a command response it shows a
        command card instead of markdown. When None the bubble starts
        empty and the streaming references are set for later attachment.
        """
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(8, 0, 8, 0)
        row_lay.setSpacing(0)

        bubble = QFrame()
        bubble.setObjectName("ai_ai_bubble")
        bubble.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        b_lay = QVBoxLayout(bubble)
        b_lay.setContentsMargins(16, 12, 16, 12)
        b_lay.setSpacing(6)

        # Header row: role label left, copy button right
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        role_lbl = QLabel("AI")
        role_lbl.setObjectName("ai_bubble_role")
        header_row.addWidget(role_lbl)
        header_row.addStretch(1)

        copy_btn = QPushButton("⎘ Copy")
        copy_btn.setObjectName("ai_bubble_copy_btn")
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        header_row.addWidget(copy_btn)

        b_lay.addLayout(header_row)

        text_widget = _AIBubbleText()
        text_widget.setObjectName("ai_bubble_text_browser")
        b_lay.addWidget(text_widget)

        copy_btn.clicked.connect(
            lambda: self._on_copy_ai_response(copy_btn, text_widget))

        # Bubble:spacer = 4:1  →  bubble gets 80 % of row width, left-aligned
        row_lay.addWidget(bubble, stretch=4)
        row_lay.addStretch(1)

        self._chat_messages_layout.addWidget(row)

        if static_text is not None:
            # History replay — detect command vs. plain markdown
            suggestion = parse_command_response(static_text)
            if suggestion.has_command:
                text_widget.setVisible(False)
                copy_btn.setVisible(False)
                card = _CommandCard(suggestion)
                card.insert_to_terminal.connect(self.insert_to_terminal)
                b_lay.addWidget(card)
            else:
                text_widget.set_rendered(static_text)
        else:
            # Live streaming — record all three refs for later use
            self._current_ai_widget = text_widget
            self._current_bubble_layout = b_lay
            self._current_copy_btn = copy_btn

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

        pending_prompt = self._pending_send
        if pending_prompt:
            self._pending_send = ""
            if status.ok:
                self._start_inference(pending_prompt)
            else:
                try:
                    self._input.setReadOnly(False)
                    self._btn_send.setEnabled(False)
                except RuntimeError:
                    return

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
        self._pending_send = prompt
        self._input.setReadOnly(True)
        self._btn_send.setEnabled(False)
        self._apply_status(AIStatus.checking())
        self._kick_probe(force=True)

    def _start_inference(self, prompt: str) -> None:
        """Start a chat inference request."""
        self._input.setReadOnly(True)
        self._btn_send.setEnabled(False)
        self._btn_stop.setEnabled(True)
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
            if self._current_ai_widget is not None:
                self._current_ai_widget.set_plain_stream(self._chat_stream_buffer)
            self._scroll_chat_to_bottom()
        except RuntimeError:
            return

    def _on_chat_finished(self, worker: StreamWorker, full: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        final = full or self._chat_stream_buffer
        if final:
            try:
                self._service.chat_assistant.record_exchange(
                    self._pending_user_msg, final,
                )
            except Exception:
                pass
            self._append_to_session(self._pending_user_msg, final)
        try:
            if self._current_ai_widget is not None:
                suggestion = parse_command_response(final)
                if suggestion.has_command:
                    # Hide the raw streamed text; attach a command card instead
                    self._current_ai_widget.setVisible(False)
                    if self._current_copy_btn is not None:
                        self._current_copy_btn.setVisible(False)
                    if self._current_bubble_layout is not None:
                        card = _CommandCard(suggestion)
                        card.insert_to_terminal.connect(self.insert_to_terminal)
                        self._current_bubble_layout.addWidget(card)
                else:
                    self._current_ai_widget.set_rendered(final)
            self._current_ai_widget = None
            self._current_bubble_layout = None
            self._current_copy_btn = None
            self._scroll_chat_to_bottom()
            self._unlock_input()
        except RuntimeError:
            return

    def _on_chat_cancelled(self, worker: StreamWorker, _partial: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            if self._current_ai_widget is not None:
                partial = self._current_ai_widget.plain_text()
                self._current_ai_widget.set_rendered(
                    (partial + "\n\n*[cancelled]*").strip()
                )
                self._current_ai_widget = None
            self._current_bubble_layout = None
            self._current_copy_btn = None
            self._unlock_input()
        except RuntimeError:
            return

    # ── Chat history management ────────────────────────────────────

    def _append_to_session(self, user_msg: str, ai_msg: str) -> None:
        """Add a completed exchange to the current session and save."""
        now = _time.time()
        self._current_session.messages.append(
            ChatMessage("user", user_msg.strip(), now))
        self._current_session.messages.append(
            ChatMessage("assistant", ai_msg.strip(), now))
        self._current_session.updated_at = now
        # Auto-generate a title from the very first user message
        if len(self._current_session.messages) == 2:
            self._current_session.title = auto_title(user_msg)
        self._history_manager.save_session(self._current_session)
        self._refresh_history_list()

    def _refresh_history_list(self) -> None:
        """Repopulate the history sidebar from saved sessions."""
        if self._shutting_down:
            return
        try:
            self._history_list.clear()
            sessions = self._history_manager.list_sessions()
            for session in sessions:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, session.id)
                item.setSizeHint(QSize(200, 52))
                self._history_list.addItem(item)
                self._history_list.setItemWidget(item, self._make_hist_widget(session))
        except RuntimeError:
            return

    def _make_hist_widget(self, session: ChatSession) -> QWidget:
        """Build the two-line widget shown inside each history list item."""
        w = QWidget()
        w.setObjectName("ai_hist_item")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 6, 10, 4)
        lay.setSpacing(1)

        title = session.title or "Chat"
        if len(title) > 30:
            title = title[:28] + "…"
        t = QLabel(title)
        t.setObjectName("ai_hist_item_title")
        lay.addWidget(t)

        n = session.message_count
        count_str = f"{n} message{'s' if n != 1 else ''}"
        sub = QLabel(f"{relative_time(session.updated_at)}  ·  {count_str}")
        sub.setObjectName("ai_hist_item_time")
        lay.addWidget(sub)

        return w

    def _on_history_item_clicked(self, item: QListWidgetItem) -> None:
        """Load a past session when the user clicks it in the list."""
        if self._shutting_down:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return
        # Don't reload the session we're already in
        if session_id == self._current_session.id:
            return
        self._load_session(session_id)

    def _on_history_context_menu(self, pos) -> None:
        """Right-click: offer Delete for a history item."""
        item = self._history_list.itemAt(pos)
        if not item:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        action = menu.exec(self._history_list.mapToGlobal(pos))
        if action is delete_action:
            self._history_manager.delete_session(session_id)
            # If we deleted the current session, start fresh
            if session_id == self._current_session.id:
                self._on_clear_chat()
            else:
                self._refresh_history_list()

    def _load_session(self, session_id: str) -> None:
        """Save current session, then load and display a past one."""
        session = self._history_manager.load_session(session_id)
        if not session:
            return
        # Stop any ongoing inference
        self._on_stop()
        # Persist current session (non-empty only)
        self._history_manager.save_session(self._current_session)
        # Switch to the loaded session
        self._current_session = session
        # Restore in-memory assistant history
        msgs = [{"role": m.role, "content": m.content}
                for m in session.messages]
        try:
            self._service.chat_assistant.load_from_messages(msgs)
        except Exception:
            pass
        # Rebuild the visual message list
        self._render_session_history(session)
        self._refresh_history_list()

    def _render_session_history(self, session: ChatSession) -> None:
        """Clear the bubble list and replay a session's messages."""
        self._current_ai_widget = None
        while self._chat_messages_layout.count():
            item = self._chat_messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_messages_layout.addStretch(1)

        for msg in session.messages:
            if msg.role == "user":
                self._add_user_bubble(msg.content)
            elif msg.role == "assistant":
                self._add_ai_bubble(static_text=msg.content)

        if session.messages:
            self._chat_panel_stack.setCurrentIndex(1)
        else:
            self._chat_panel_stack.setCurrentIndex(0)
        self._scroll_chat_to_bottom()

    def _toggle_history_sidebar(self) -> None:
        """Show or hide the history sidebar."""
        self._history_sidebar_visible = not self._history_sidebar_visible
        self._history_sidebar.setVisible(self._history_sidebar_visible)

    def _on_copy_ai_response(
        self, btn: QPushButton, widget: _AIBubbleText
    ) -> None:
        if self._shutting_down:
            return
        text = widget.toPlainText().strip()
        if not text:
            return
        ok = copy_text(text)
        try:
            btn.setText("✓ Copied" if ok else "✗ Failed")
        except RuntimeError:
            return
        QTimer.singleShot(1500, lambda: self._reset_copy_btn(btn))

    def _reset_copy_btn(self, btn: QPushButton) -> None:
        if self._shutting_down:
            return
        try:
            btn.setText("⎘ Copy")
        except RuntimeError:
            return

    def _on_clear_chat(self) -> None:
        self._on_stop()
        # Save any in-progress session before wiping
        self._history_manager.save_session(self._current_session)
        while self._chat_messages_layout.count():
            item = self._chat_messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_messages_layout.addStretch(1)
        self._current_ai_widget = None
        self._chat_panel_stack.setCurrentIndex(0)
        self._service.chat_assistant.clear()
        # Start a fresh session
        self._current_session = ChatSession.new()
        self._refresh_history_list()

    # ── Shared failure handler ─────────────────────────────────────

    def _on_request_failed(self, worker: StreamWorker, message: str) -> None:
        if self._shutting_down or worker is not self._current_worker:
            return
        try:
            if self._current_ai_widget is not None:
                partial = self._current_ai_widget.plain_text()
                self._current_ai_widget.set_rendered(
                    (partial + f"\n\n*[error: {message}]*").strip()
                )
                self._current_ai_widget = None
            else:
                self._add_ai_bubble()
                if self._current_ai_widget is not None:
                    self._current_ai_widget.set_rendered(
                        f"*[error: {message}]*"
                    )
                    self._current_ai_widget = None
            self._current_bubble_layout = None
            self._current_copy_btn = None
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
            # Inline command card
            f"QFrame#ai_cmd_card {{"
            f"  background: {t.bg_base};"
            f"  border: 1px solid {t.accent_dim}; border-radius: 8px;"
            f"  margin-top: 4px;"
            f"}}"
            f"QLabel#ai_cmd_card_label {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 10px;"
            f"  font-weight: 800; letter-spacing: 1px;"
            f"}}"
            f"QLineEdit#ai_cmd_card_line {{"
            f"  background: {t.bg_raised}; color: {t.accent};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 6px 10px; font-size: 12px;"
            f"}}"
            f"QLabel#ai_cmd_card_explain {{"
            f"  color: {t.text}; font-size: 12px;"
            f"}}"
            f"QLabel#ai_cmd_card_caution {{"
            f"  color: {t.red}; font-size: 12px; font-weight: 700;"
            f"}}"
            f"QPushButton#ai_cmd_card_action {{"
            f"  background: {t.bg_raised}; color: {t.text};"
            f"  border: 1px solid {t.border_lt}; border-radius: 6px;"
            f"  padding: 5px 14px; font-size: 11px; font-weight: 700;"
            f"}}"
            f"QPushButton#ai_cmd_card_action:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"  background: {t.accent_bg};"
            f"}}"
            # History sidebar
            f"QFrame#ai_history_sidebar {{"
            f"  background: {t.bg_raised};"
            f"  border: none; border-right: 1px solid {t.border_lt};"
            f"}}"
            f"QFrame#ai_hist_header {{"
            f"  background: {t.bg_raised};"
            f"  border-bottom: 1px solid {t.border_lt};"
            f"}}"
            f"QLabel#ai_hist_header_title {{"
            f"  color: {t.text_dim}; font-family: {mono}; font-size: 10px;"
            f"  font-weight: 800; letter-spacing: 1px;"
            f"}}"
            f"QListWidget#ai_hist_list {{"
            f"  background: {t.bg_raised}; border: none;"
            f"  outline: none;"
            f"}}"
            f"QListWidget#ai_hist_list::item {{"
            f"  background: transparent; border-bottom: 1px solid {t.border};"
            f"  padding: 0px;"
            f"}}"
            f"QListWidget#ai_hist_list::item:selected {{"
            f"  background: {t.accent_bg}; border-bottom: 1px solid {t.border};"
            f"}}"
            f"QListWidget#ai_hist_list::item:hover:!selected {{"
            f"  background: {t.bg_base};"
            f"}}"
            f"QWidget#ai_hist_item {{ background: transparent; }}"
            f"QLabel#ai_hist_item_title {{"
            f"  color: {t.text}; font-size: 11px; font-weight: 600;"
            f"}}"
            f"QLabel#ai_hist_item_time {{"
            f"  color: {t.text_dim}; font-size: 10px;"
            f"}}"
            # Toggle sidebar button
            f"QPushButton#ai_toggle_history {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  font-size: 13px; padding: 2px 0px;"
            f"}}"
            f"QPushButton#ai_toggle_history:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
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
            f"QPushButton#ai_bubble_copy_btn {{"
            f"  background: transparent; color: {t.text_dim};"
            f"  border: 1px solid {t.border_lt}; border-radius: 4px;"
            f"  padding: 1px 8px; font-size: 10px; font-weight: 600;"
            f"}}"
            f"QPushButton#ai_bubble_copy_btn:hover {{"
            f"  color: {t.accent}; border-color: {t.accent_dim};"
            f"  background: {t.accent_bg};"
            f"}}"
            f"QTextBrowser#ai_bubble_text_browser {{"
            f"  background: transparent; color: {t.text};"
            f"  border: none; font-size: 13px;"
            f"  font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;"
            f"  selection-background-color: {t.accent_bg};"
            f"  selection-color: {t.accent};"
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
            # Slim, always-visible scrollbar inside the prompt input
            f"QPlainTextEdit#ai_input QScrollBar:vertical {{"
            f"  width: 6px; background: transparent; border: none;"
            f"  margin: 3px 2px 3px 0px;"
            f"}}"
            f"QPlainTextEdit#ai_input QScrollBar::handle:vertical {{"
            f"  background: {t.border_lt}; border-radius: 3px;"
            f"  min-height: 20px;"
            f"}}"
            f"QPlainTextEdit#ai_input QScrollBar::handle:vertical:hover {{"
            f"  background: {t.text_dim};"
            f"}}"
            f"QPlainTextEdit#ai_input QScrollBar::add-line:vertical,"
            f"QPlainTextEdit#ai_input QScrollBar::sub-line:vertical {{"
            f"  height: 0px;"
            f"}}"
            f"QPlainTextEdit#ai_input QScrollBar::add-page:vertical,"
            f"QPlainTextEdit#ai_input QScrollBar::sub-page:vertical {{"
            f"  background: none;"
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
