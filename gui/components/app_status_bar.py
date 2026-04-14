"""
Application status bar — a structured, context-aware bottom bar.

Layout (left → right):

    ┌───────────────────────────────────────────────────────────────────┐
    │ [•] Ready               SSH · 2 sessions   ●   CPU 6%  MEM 41%  │
    │                         ────── primary ────┘                     │
    │    PRIMARY              CONTEXT METRICS       SYSTEM / SESSION   │
    └───────────────────────────────────────────────────────────────────┘

  * **Primary zone** (left) — an animated StatusDot + the active activity
    message (idle, scanning…, connecting, errors…). Supports transient
    messages that auto-revert to the persistent text after a timeout.

  * **Context zone** (middle) — contextual information that reflects the
    currently active workspace page (Scanner, SSH, Terminal, …). The
    MainWindow re-populates it on page switch.

  * **System zone** (right) — persistent indicators: current mode label,
    CPU / MEM (when psutil is available), current time, theme name.

The bar exposes a small imperative API so any view can push updates:

    set_activity(text, level)          — primary message + dot color
    push_transient(text, level, ms)    — temporary primary message
    set_mode(name)                     — right-side mode label
    set_context(segments)              — middle-zone contextual items
    set_scan_metrics(alive, total, el) — scanner helper
    set_ssh_metrics(active, total, host) — ssh helper

All widgets re-style on theme change via `_restyle`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QStatusBar, QWidget, QHBoxLayout, QLabel, QSizePolicy,
)

from gui.themes import theme, ThemeManager
from gui.components.live_widgets import StatusDot

try:
    import psutil                          # type: ignore
    _HAS_PSUTIL = True
except ImportError:                        # pragma: no cover
    psutil = None                          # type: ignore
    _HAS_PSUTIL = False


# ── Level → color map ────────────────────────────────────────────────────────

LEVEL_IDLE = "idle"
LEVEL_BUSY = "busy"
LEVEL_OK   = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


def _level_color(level: str):
    t = theme()
    return {
        LEVEL_IDLE:  t.text_dim,
        LEVEL_BUSY:  t.amber,
        LEVEL_OK:    t.green,
        LEVEL_WARN:  t.amber,
        LEVEL_ERROR: t.red,
    }.get(level, t.text_dim)


# ── Small helpers ─────────────────────────────────────────────────────────────

def _sep(parent: QWidget | None = None) -> QLabel:
    lbl = QLabel("·", parent)
    lbl.setObjectName("status_sep")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setFixedWidth(16)
    return lbl


# ── Status bar component ──────────────────────────────────────────────────────

class AppStatusBar(QStatusBar):
    """
    Production-grade status bar with three structured zones.

    The MainWindow creates exactly one of these and wires views into
    its imperative API. Individual views never touch labels directly;
    they emit their own signals and the MainWindow routes them here.
    """

    #: Default persistent message shown when no transient activity is
    #: running.
    DEFAULT_PRIMARY = "Ready"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self.setObjectName("app_status_bar")

        # Persistent vs transient primary message tracking
        self._persistent_text = self.DEFAULT_PRIMARY
        self._persistent_level = LEVEL_IDLE
        self._transient_active = False
        self._current_text = self.DEFAULT_PRIMARY
        self._current_level = LEVEL_IDLE

        # Transient timer — reverts primary text after N ms.
        self._transient_timer = QTimer(self)
        self._transient_timer.setSingleShot(True)
        self._transient_timer.timeout.connect(self._clear_transient)

        # Clock timer
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(10_000)
        self._clock_timer.timeout.connect(self._tick_clock)

        # Health timer (CPU / MEM)
        if _HAS_PSUTIL:
            self._health_timer = QTimer(self)
            self._health_timer.setInterval(2000)
            self._health_timer.timeout.connect(self._tick_health)
        else:
            self._health_timer = None

        self._build_ui()

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._restyle(theme())

        self._tick_clock()
        if self._health_timer is not None:
            self._health_timer.start()
            self._tick_health()
        self._clock_timer.start()

    # ── Build ───────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Primary zone (left) ───────────────────────────────────────
        primary = QWidget()
        primary.setObjectName("status_primary")
        pl = QHBoxLayout(primary)
        pl.setContentsMargins(10, 0, 10, 0)
        pl.setSpacing(8)

        self._dot = StatusDot(size=8)
        self._dot.set_color(theme().text_dim)
        pl.addWidget(self._dot)

        self._lbl_primary = QLabel(self.DEFAULT_PRIMARY)
        self._lbl_primary.setObjectName("status_primary_text")
        self._lbl_primary.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        # Elide behavior — guarantee stable layout on long messages.
        self._lbl_primary.setMinimumWidth(120)
        pl.addWidget(self._lbl_primary, 1)

        self.addWidget(primary, 1)

        # ── Context zone (middle) ─────────────────────────────────────
        # Hosts a stable set of small segment labels that the active
        # page repopulates via set_context(). We pre-create up to 4
        # segments with their own separators so layout never jumps.
        self._ctx_segments: list[QLabel] = []
        self._ctx_separators: list[QLabel] = []
        for _ in range(4):
            sep = _sep()
            lbl = QLabel("")
            lbl.setObjectName("status_ctx")
            lbl.setVisible(False)
            sep.setVisible(False)
            self._ctx_separators.append(sep)
            self._ctx_segments.append(lbl)
            self.addPermanentWidget(sep)
            self.addPermanentWidget(lbl)

        # Fixed separator to the system zone
        self._sep_system = _sep()
        self.addPermanentWidget(self._sep_system)

        # ── System zone (right) ───────────────────────────────────────
        self._lbl_mode = QLabel("")
        self._lbl_mode.setObjectName("status_mode")
        self.addPermanentWidget(self._lbl_mode)

        self._sep_health = _sep()
        self.addPermanentWidget(self._sep_health)

        self._lbl_health = QLabel("")
        self._lbl_health.setObjectName("status_health")
        self.addPermanentWidget(self._lbl_health)

        self._sep_clock = _sep()
        self.addPermanentWidget(self._sep_clock)

        self._lbl_clock = QLabel("")
        self._lbl_clock.setObjectName("status_clock")
        self.addPermanentWidget(self._lbl_clock)

        self._sep_theme = _sep()
        self.addPermanentWidget(self._sep_theme)

        self._lbl_theme = QLabel("")
        self._lbl_theme.setObjectName("status_theme")
        self.addPermanentWidget(self._lbl_theme)

    # ── Public API — primary activity ───────────────────────────────────────

    def set_activity(self, text: str, level: str = LEVEL_IDLE) -> None:
        """
        Persistent primary message and dot level.

        Transient messages (from push_transient) temporarily override
        this; once they expire the bar reverts to the last call to
        set_activity.
        """
        text = (text or "").strip()
        if not text:
            text = self.DEFAULT_PRIMARY
            level = LEVEL_IDLE
        self._persistent_text = text
        self._persistent_level = level
        if not self._transient_active:
            self._apply_primary(text, level)

    def push_transient(
        self,
        text: str,
        level: str = LEVEL_OK,
        timeout_ms: int = 4000,
    ) -> None:
        """
        Temporarily show `text` in the primary slot. After
        `timeout_ms` ms the bar reverts to the current persistent
        activity.
        """
        text = (text or "").strip()
        if not text:
            return
        self._transient_active = True
        self._apply_primary(text, level)
        self._transient_timer.start(max(500, timeout_ms))

    def _clear_transient(self) -> None:
        self._transient_active = False
        self._apply_primary(self._persistent_text, self._persistent_level)

    def _apply_primary(self, text: str, level: str) -> None:
        t = theme()
        color = _level_color(level)
        self._current_text = text
        self._current_level = level
        self._lbl_primary.setText(text)
        mono_family = (
            "'JetBrains Mono', 'Cascadia Mono', 'Cascadia Code',"
            " 'Fira Code', 'Consolas', monospace"
        )
        self._lbl_primary.setStyleSheet(
            f"color: {t.text}; font-family: {mono_family};"
            f" font-size: 12px; font-weight: 700; letter-spacing: 0.5px;"
            f" background: transparent;"
        )
        if level == LEVEL_BUSY:
            self._dot.set_active(True, color=color)
        elif level in (LEVEL_OK, LEVEL_WARN, LEVEL_ERROR):
            self._dot.set_active(False)
            self._dot.set_color(color)
        else:
            self._dot.set_active(False)
            self._dot.set_color(t.text_dim)

    # ── Public API — context zone ───────────────────────────────────────────

    def set_context(self, segments: Iterable[str]) -> None:
        """
        Replace the middle context zone with up to 4 short strings.
        Pass an empty iterable to clear it entirely.
        """
        seg_list = [s for s in (segments or []) if s]
        for i, (sep, lbl) in enumerate(zip(self._ctx_separators, self._ctx_segments)):
            if i < len(seg_list):
                lbl.setText(seg_list[i])
                lbl.setVisible(True)
                sep.setVisible(i > 0)
            else:
                lbl.setText("")
                lbl.setVisible(False)
                sep.setVisible(False)
        # The leading separator is only shown between segments, not
        # before the first one (that's handled by the zone gap).

    def clear_context(self) -> None:
        self.set_context([])

    # ── Public API — system zone ────────────────────────────────────────────

    def set_mode(self, name: str) -> None:
        """Right-side mode label — e.g., 'SCANNER', 'SSH', 'TERMINAL'."""
        name = (name or "").strip().upper()
        self._lbl_mode.setText(name)
        self._lbl_mode.setVisible(bool(name))
        self._sep_system.setVisible(bool(name))

    def set_theme_label(self, name: str) -> None:
        self._lbl_theme.setText((name or "").upper())

    # ── Convenience helpers for specific views ──────────────────────────────

    def set_scan_metrics(
        self,
        alive: Optional[int] = None,
        total: Optional[int] = None,
        elapsed: Optional[str] = None,
    ) -> None:
        """Context segments for the Scanner page."""
        parts: list[str] = []
        if total is not None:
            if total > 0:
                alive_s = alive if alive is not None else 0
                parts.append(f"{alive_s}/{total} alive")
            else:
                parts.append("no scan yet")
        if elapsed:
            parts.append(f"⏱ {elapsed}")
        self.set_context(parts)

    def set_ssh_metrics(
        self,
        active_tabs: int,
        current_host: Optional[str] = None,
        state: Optional[str] = None,
    ) -> None:
        """Context segments for the SSH Sessions page."""
        parts: list[str] = []
        if active_tabs <= 0:
            parts.append("no sessions")
        else:
            word = "session" if active_tabs == 1 else "sessions"
            parts.append(f"{active_tabs} {word}")
        if current_host:
            parts.append(current_host)
        if state:
            parts.append(state)
        self.set_context(parts)

    def set_terminal_metrics(self, shell: Optional[str], running: bool) -> None:
        parts: list[str] = []
        if shell:
            parts.append(f"shell: {shell}")
        parts.append("running" if running else "ready")
        self.set_context(parts)

    # ── Timers ──────────────────────────────────────────────────────────────

    def _tick_health(self) -> None:
        if not _HAS_PSUTIL:
            self._lbl_health.setText("")
            return
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            self._lbl_health.setText(f"CPU {cpu:>4.0f}%   MEM {mem:>4.0f}%")
        except Exception:
            self._lbl_health.setText("")

    def _tick_clock(self) -> None:
        self._lbl_clock.setText(datetime.now().strftime("%H:%M"))

    # ── Theme ───────────────────────────────────────────────────────────────

    def _on_theme_changed(self, _t) -> None:
        self._restyle(theme())
        # Re-apply the current primary message with the new palette.
        self._apply_primary(self._current_text, self._current_level)

    def _restyle(self, t) -> None:
        # The status bar itself is styled via the global QSS
        # (`QStatusBar`) but the segment labels need matching mono
        # typography so the metrics, mode, clock and theme name read
        # as one engineered band of text.
        accent2 = t.accent2 or t.accent
        mono_family = (
            "'JetBrains Mono', 'Cascadia Mono', 'Cascadia Code',"
            " 'Fira Code', 'Consolas', monospace"
        )
        base = (
            f"color: {t.text_dim}; font-family: {mono_family};"
            f" font-size: 11px; letter-spacing: 0.6px;"
            f" background: transparent; padding: 0 6px;"
        )
        mono = (
            f"color: {t.text_dim}; font-size: 11px;"
            f" font-family: {mono_family}; letter-spacing: 0.6px;"
            f" background: transparent; padding: 0 6px;"
        )
        for lbl in self._ctx_segments:
            lbl.setStyleSheet(base)
        for sep in (
            *self._ctx_separators,
            self._sep_system,
            self._sep_health,
            self._sep_clock,
            self._sep_theme,
        ):
            sep.setStyleSheet(
                f"color: {t.text_dim}; font-size: 12px;"
                f" background: transparent;"
            )
        self._lbl_health.setStyleSheet(mono)
        self._lbl_clock.setStyleSheet(mono)
        # Mode is the page identifier — stays on the primary accent so
        # it anchors the status bar like a section flag.
        self._lbl_mode.setStyleSheet(
            f"color: {t.accent}; font-size: 11px; font-weight: 900;"
            f" letter-spacing: 2.0px; font-family: {mono_family};"
            f" background: transparent; padding: 0 6px;"
            f" text-transform: uppercase;"
        )
        # Theme name uses the secondary accent so the two right-side
        # markers read as a deliberate two-tone pair.
        self._lbl_theme.setStyleSheet(
            f"color: {accent2}; font-size: 11px; font-weight: 800;"
            f" letter-spacing: 1.6px; font-family: {mono_family};"
            f" background: transparent; padding: 0 6px;"
            f" text-transform: uppercase;"
        )
        self._lbl_primary.setStyleSheet(
            f"color: {t.text}; font-family: {mono_family};"
            f" font-size: 12px; font-weight: 700; letter-spacing: 0.5px;"
            f" background: transparent;"
        )
