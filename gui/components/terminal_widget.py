"""
Embedded terminal widget.

A `QPlainTextEdit` subclass that runs commands inside a persistent shell.
Three backends are supported:

  * "local"   — fires off subprocess.Popen for each command line typed
                by the user.  Output is streamed back to the widget via
                a worker thread.  Always available, no extra deps.

  * "ssh"     — attaches to an `SSHSession` (paramiko invoke_shell
                channel), echoes the remote bytes verbatim and forwards
                keystrokes back to the channel.  Used by the SSH view.

  * "serial"  — attaches to a `SerialSession` (pyserial-backed COM port).
                Same byte-stream interface as SSH; line endings (CR /
                LF / CRLF) and local echo are configurable per session
                so AT commands and UART consoles work the same way
                they do in PuTTY.

The "ssh" and "serial" modes share the same code path: the widget
treats any session that exposes ``is_open / send / read_loop / resize
/ close`` as a transport. Internally both are referred to as "remote"
mode but the public hooks keep the historical "ssh" name for
backwards compatibility.

Designed to be theme-aware: re-styles itself when ThemeManager emits.
"""

from __future__ import annotations

import codecs
import os
import platform
import re as _re
import subprocess
import threading
import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QKeyEvent, QTextCursor, QFont, QFontDatabase, QFontInfo,
    QTextCharFormat, QColor,
)
from PyQt6.QtWidgets import QPlainTextEdit

from gui.themes import ThemeManager, theme
from scanner.ssh_client import SSHSession
from utils.clipboard import copy_selected_text, copy_text, read_text


_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0


# ── Shell discovery helpers ──────────────────────────────────────────────────


def _which(exe: str) -> Optional[str]:
    """Resolve an executable on the current PATH (Windows or POSIX)."""
    try:
        if os.path.isabs(exe) and os.path.isfile(exe):
            return exe
        finder = "where" if _IS_WINDOWS else "which"
        r = subprocess.run(
            [finder, exe], capture_output=True, text=True,
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


def _find_powershell() -> Optional[str]:
    """
    Locate PowerShell on Windows. Prefer pwsh.exe (Core) if installed,
    otherwise fall back to Windows PowerShell.
    """
    if not _IS_WINDOWS:
        return None
    candidates = [
        os.environ.get("POWERSHELL"),
        "pwsh.exe",
        "powershell.exe",
        os.path.expandvars(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
        ),
    ]
    for c in candidates:
        if not c:
            continue
        resolved = _which(c)
        if resolved:
            return resolved
    return "powershell.exe"  # last-resort fallback


def _find_cmd() -> Optional[str]:
    if not _IS_WINDOWS:
        return None
    return (
        _which("cmd.exe")
        or os.path.expandvars(r"%SystemRoot%\System32\cmd.exe")
    )


def _find_wsl() -> Optional[str]:
    """Locate wsl.exe on Windows; returns None if WSL is not present."""
    if not _IS_WINDOWS:
        return None
    return _which("wsl.exe")


# Public registry of available shell backends. Each entry maps a label
# to a callable that builds a `subprocess.Popen` argv for a single
# command line. Keys map to UI selector entries.
SHELL_BACKENDS: dict[str, dict] = {
    "PowerShell": {
        "find": _find_powershell,
        "available_on": ("Windows",),
        "build": lambda exe, cmd: [
            exe, "-NoLogo", "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-Command", cmd,
        ],
        "prompt": lambda cwd: f"PS {cwd}> ",
    },
    "CMD": {
        "find": _find_cmd,
        "available_on": ("Windows",),
        "build": lambda exe, cmd: [exe, "/d", "/c", cmd],
        "prompt": lambda cwd: f"{cwd}> ",
    },
    "WSL": {
        "find": _find_wsl,
        "available_on": ("Windows",),
        "build": lambda exe, cmd: [exe, "--", "bash", "-lc", cmd],
        "prompt": lambda cwd: f"wsl:{cwd}$ ",
    },
    "Bash": {
        "find": lambda: _which("bash") or "/bin/sh",
        "available_on": ("Linux", "Darwin"),
        "build": lambda exe, cmd: [exe, "-c", cmd],
        "prompt": lambda cwd: f"{cwd}$ ",
    },
}


def available_shell_names() -> list[str]:
    """Return shell labels usable on the current OS."""
    sys_name = platform.system()
    return [
        name
        for name, meta in SHELL_BACKENDS.items()
        if sys_name in meta["available_on"]
    ]


def shell_is_installed(name: str) -> bool:
    """True if the requested shell can be located on this machine."""
    meta = SHELL_BACKENDS.get(name)
    if not meta:
        return False
    if platform.system() not in meta["available_on"]:
        return False
    try:
        return bool(meta["find"]())
    except Exception:
        return False


def default_shell_name() -> str:
    """Pick a sensible default shell for the current OS."""
    if _IS_WINDOWS:
        return "PowerShell"
    return "Bash"


class TerminalWidget(QPlainTextEdit):
    """
    Reusable embedded terminal control.

    Modes:
        local  → REPL: each line typed runs a shell command
        ssh    → bytes are forwarded to/from an SSHSession
        idle   → no session owns the buffer; keys are swallowed.
                 Only used when the widget is constructed with
                 `ssh_only=True`, i.e. as the terminal inside an
                 SshSessionTab.

    Construction modes:
        ssh_only=False (default)
            Standalone local terminal with a welcome banner and a
            living local shell. On SSH detach (non-silent), falls back
            to the local shell.

        ssh_only=True
            A session-owned terminal. No welcome banner, no local
            fallback — when no SSH session is attached the widget sits
            in idle mode and rejects keystrokes. Used by SshSessionTab.
    """

    # Internal signals so worker threads can poke the widget safely.
    # The SSH signals carry a generation tag so late events from a
    # previously-attached session can be dropped on the GUI thread
    # without corrupting a freshly-attached session's buffer.
    _local_chunk    = pyqtSignal(str)
    _local_done     = pyqtSignal(int)
    _ssh_chunk      = pyqtSignal(int, bytes)
    _ssh_closed_sig = pyqtSignal(int)

    # Outward
    session_closed = pyqtSignal()
    session_opened = pyqtSignal()

    def __init__(self, parent=None, *, ssh_only: bool = False):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(32)
        self._ssh_only = ssh_only
        # A 2px caret reads as a proper terminal cursor and pairs well
        # with the tightened cursor flash time set by motion.install_global.
        self.setCursorWidth(2)

        # ── Font setup ───────────────────────────────────────────────────
        # The QSS rule in `_apply_theme_colors` is the authoritative
        # source of font-family and font-size for this widget — it has
        # to be, because the global QWidget stylesheet sets a
        # proportional font and Qt's `setFont()` doesn't win against
        # a QSS rule on a parent class selector. The QFont we build
        # here drives the *other* properties Qt reads at render time:
        # line height, hinting, kerning, and the fixedPitch hint that
        # influences fallback selection.
        #
        # We walk the same family preference list as the QSS rule and
        # pick the first one that the OS confirms is actually fixed
        # pitch via QFontInfo. That way the in-code font line height
        # matches what the QSS-resolved font will end up using.
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        for family in (
            "Cascadia Mono", "Cascadia Code", "Consolas",
            "JetBrains Mono", "Fira Code", "Courier New",
        ):
            candidate = QFont(family, 11)
            candidate.setStyleHint(QFont.StyleHint.Monospace)
            candidate.setFixedPitch(True)
            if QFontInfo(candidate).fixedPitch():
                f = candidate
                break

        f.setPointSize(11)
        f.setFixedPitch(True)
        f.setStyleHint(QFont.StyleHint.Monospace)
        # Full hinting snaps glyphs onto integer pixel boundaries —
        # crucial for ASCII art on HiDPI / fractional-scale displays.
        try:
            f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        except Exception:
            pass
        # Lock spacing to 100% (exactly the font's natural advance).
        # An earlier version added 0.5px absolute spacing here, which
        # accumulated left-to-right and broke ASCII art adjacency.
        try:
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100.0)
        except Exception:
            pass
        self.setFont(f)

        # Mode: "local" | "ssh" | "idle"
        # "idle" is only used by ssh_only terminals when no SSH session
        # is currently attached — keystrokes are swallowed and the
        # buffer is read-only.
        self._mode = "idle" if ssh_only else "local"
        self._busy = False              # local command running
        self._cwd = os.path.expanduser("~")
        self._input_anchor = 0          # doc position where editable input starts

        # Monotonic timestamp of the last banner render. Used by
        # `refresh_intro()` to debounce repeated calls when the user
        # rapidly bounces in and out of the terminal page.
        self._last_banner_at: float = 0.0

        # Active local shell backend (PowerShell / CMD / WSL / Bash).
        self._shell_name: str = default_shell_name()

        self._history: list[str] = []
        self._history_idx = 0
        self._partial_input = ""

        self._proc: Optional[subprocess.Popen] = None
        self._proc_reader: Optional[threading.Thread] = None

        self._ssh: Optional[SSHSession] = None
        self._ssh_reader: Optional[threading.Thread] = None
        # Per-session I/O configuration. Defaults match historical SSH
        # behavior: Enter sends bare CR (PTY shells turn that into a
        # newline themselves) and the remote does its own echoing.
        # Serial sessions override these via attach_ssh kwargs because
        # AT-command devices and bare UART consoles often want CRLF
        # and don't echo back at all.
        #   _line_ending: bytes sent on the Enter key. One of b"\r",
        #                 b"\n", or b"\r\n".
        #   _local_echo:  if True, every byte we send to the session is
        #                 also fed into the display so the user sees
        #                 their input (PuTTY's "Local echo: Force on"
        #                 mode).
        #   _backend_kind: "ssh" or "serial" — picks Backspace byte
        #                  (DEL 0x7f for SSH/xterm, BS 0x08 for serial)
        #                  and selects display-erase behavior under
        #                  local echo.
        self._line_ending: bytes = b"\r"
        self._local_echo: bool = False
        self._backend_kind: str = "ssh"
        # Monotonic generation tag for the currently-attached SSH
        # session. Bumped on every attach + detach so stale worker
        # signals from an old session can be filtered out on the GUI
        # thread. Guards against reconnect races where an old reader
        # thread finishes draining after a new session is attached.
        self._ssh_generation: int = 0
        # Set once the widget begins shutdown so late cross-thread
        # signals are dropped instead of touching a dying QObject.
        self._shutting_down: bool = False

        # ── SSH terminal emulation state ─────────────────────────────
        # Document position where the next SSH output byte should
        # land. Tracked separately from the visible QTextCursor so
        # that user mouse-selection doesn't move the terminal's
        # write head.
        self._ssh_cursor_pos: int = 0
        # Incremental UTF-8 decoder — handles multi-byte characters
        # that straddle TCP chunk boundaries without emitting
        # replacement characters.
        self._ssh_utf8_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        # Buffer for an incomplete escape sequence carried over from
        # the previous chunk. A chunk boundary can easily fall in the
        # middle of something like ``\x1b[K``.
        self._ssh_pending: str = ""
        # Latched after a CUP-home (``\x1b[H`` / ``\x1b[1;1H``) so
        # that a following erase-in-display can be recognised as a
        # *whole-screen* clear even when the shell sends the BusyBox
        # style ``\x1b[H\x1b[J`` pair (erase-from-cursor-to-end).
        # Without this latch, ``\x1b[J`` mode 0 would only erase the
        # single line the cursor is sitting on, which is why
        # BusyBox / minimal ``clear`` and readline's ``Ctrl+L`` on
        # some Linux shells appeared to do nothing. The flag is
        # cleared again by any printable output, control byte, or
        # non-cursor CSI command — only the immediate H→J pair
        # triggers the whole-screen interpretation.
        self._ssh_home_pending: bool = False
        # Debounced PTY resize. Dragging the window fires resizeEvent
        # on every pixel; we coalesce those into a single
        # chan.resize_pty call after the user stops dragging.
        self._ssh_resize_timer = QTimer(self)
        self._ssh_resize_timer.setSingleShot(True)
        self._ssh_resize_timer.setInterval(100)
        self._ssh_resize_timer.timeout.connect(self._ssh_send_resize)

        self._local_chunk.connect(self._on_local_chunk)
        self._local_done.connect(self._on_local_done)
        self._ssh_chunk.connect(self._on_ssh_chunk)
        self._ssh_closed_sig.connect(self._on_ssh_closed_remote)

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme_colors()

        # Only the standalone local terminal shows a welcome banner.
        # ssh_only terminals start blank — SshSessionTab drives the
        # content via [connecting…]/[connected…] messages.
        if not ssh_only:
            self._show_local_prompt(banner=True)

        self._sync_read_only()

    # ── Theme integration ────────────────────────────────────────────────────

    def _on_theme_changed(self, _t):
        self._apply_theme_colors()

    def _apply_theme_colors(self):
        t = theme()
        # Retro terminal palette: dedicated bg/fg + glow border.
        #
        # IMPORTANT: font-family + font-size MUST be set in this
        # per-instance stylesheet rule. The global theme stylesheet
        # has a `QWidget { font-family: 'Segoe UI'; font-size: 13px; }`
        # rule that cascades into every widget — including
        # QPlainTextEdit. Qt's `setFont()` does NOT win against a QSS
        # font rule applied via a parent class selector, so without
        # this override the terminal would render in Segoe UI (a
        # proportional font!) and any aligned output (banner, ASCII
        # art, columnar command output) would visibly shear apart.
        #
        # Using the object-name selector (`#terminal`) gives this rule
        # higher specificity than the `QWidget` cascade, so the font
        # locks to the first available true monospace family. The
        # listed families are all guaranteed monospace and ship with
        # default Windows / macOS / Linux installations.
        self.setStyleSheet(
            f"QPlainTextEdit#terminal {{"
            f" background-color: {t.term_bg};"
            f" color: {t.term_fg};"
            f" border: 2px solid {t.term_border};"
            f" border-radius: 6px;"
            f" selection-background-color: {t.bg_select};"
            f" selection-color: {t.white};"
            f" padding: 14px 16px;"
            f" font-family: 'Cascadia Mono', 'Cascadia Code',"
            f"   'Consolas', 'JetBrains Mono', 'Fira Code',"
            f"   'Courier New', monospace;"
            f" font-size: 11pt;"
            f" font-weight: 500;"
            f"}}"
        )

    # ── Mode switching ───────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_busy(self) -> bool:
        return self._busy or self._mode == "ssh"

    def attach_ssh(
        self,
        session,
        banner: str = "",
        *,
        line_ending: str = "cr",
        local_echo: bool = False,
        backend_kind: str = "ssh",
    ) -> None:
        """
        Switch into remote mode using an already-connected session.

        ``session`` may be an :class:`SSHSession` or any duck-typed
        object that exposes ``is_open / send / read_loop / resize /
        close`` — :class:`scanner.serial_client.SerialSession` qualifies
        and is used by the Serial/UART tab.

        Parameters
        ----------
        line_ending : "cr" | "lf" | "crlf"
            What the Enter key sends on this session.  SSH PTYs expect
            bare ``\\r`` (the default); AT-command devices and most
            UART consoles want ``\\r\\n``.
        local_echo : bool
            If True, every byte we send is also written to the display
            so the user can see their input.  Required for serial
            devices that don't echo back.
        backend_kind : "ssh" | "serial"
            Selects Backspace semantics (xterm DEL 0x7f for SSH, BS
            0x08 for serial) and a couple of secondary defaults.

        Any previously-attached session is torn down silently (no
        "[session closed]" line, no local prompt) so that a clean
        reconnect over a live session only writes the new banner.

        Each attach bumps the internal generation counter so any
        in-flight signals from a previously-attached session's reader
        thread are ignored when they eventually reach the GUI thread.
        """
        if self._shutting_down or session is None:
            return
        self.detach_ssh(silent=True)
        self._ssh_generation += 1
        generation = self._ssh_generation
        self._mode = "ssh"
        self._ssh = session
        self._busy = False
        self._line_ending = {
            "cr":   b"\r",
            "lf":   b"\n",
            "crlf": b"\r\n",
        }.get((line_ending or "cr").lower(), b"\r")
        self._local_echo = bool(local_echo)
        self._backend_kind = (backend_kind or "ssh").lower()

        # Reset the terminal-emulation state for this fresh session.
        self._ssh_pending = ""
        self._ssh_home_pending = False
        self._ssh_utf8_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )

        if banner:
            self._append(banner)
        # Our terminal cursor starts immediately after whatever the
        # caller-provided banner wrote. From here on, remote PTY
        # output drives all cursor movement.
        self._ssh_cursor_pos = self._end_position()

        # Tell the remote PTY how big the widget actually is right
        # now. The SSHSession was opened with a default 120x32 (see
        # scanner/ssh_client.py) but that's almost certainly not
        # what the widget shows — renegotiate before the shell has
        # a chance to produce its first prompt.
        try:
            cols, rows = self._compute_terminal_size()
            session.resize(cols, rows)
        except Exception:
            pass

        self._ssh_reader = threading.Thread(
            target=self._run_reader,
            args=(session, generation),
            daemon=True,
        )
        self._ssh_reader.start()
        self._sync_read_only()
        self.session_opened.emit()
        self._move_cursor_to_end()
        self.setFocus()

    # Backend-agnostic alias matching the BaseTerminalSession naming
    # convention. Internally this is the same code path as ``attach_ssh``
    # because the widget already treats any ``is_open / send / read_loop /
    # resize / close`` object as a transport — the only thing tied to the
    # transport is the line-ending byte and the Backspace mapping, both
    # configured per-call. New code should prefer ``attach_session``.
    def attach_session(
        self,
        session,
        *,
        banner: str = "",
        line_ending: str = "cr",
        local_echo: bool = False,
        backend_kind: str = "ssh",
    ) -> None:
        self.attach_ssh(
            session,
            banner=banner,
            line_ending=line_ending,
            local_echo=local_echo,
            backend_kind=backend_kind,
        )

    def detach_session(self, *, silent: bool = False) -> None:
        """Backend-agnostic alias for ``detach_ssh``."""
        self.detach_ssh(silent=silent)

    def _run_reader(self, session: SSHSession, generation: int) -> None:
        """
        Worker-thread entry point for the SSH read loop.

        Binds the generation tag into the data/close callbacks so the
        GUI thread can cheaply discard any late signals from this
        reader if the widget has since been reattached or torn down.
        """
        def on_bytes(data: bytes) -> None:
            # Don't bother emitting if the widget has been reattached
            # or is shutting down — saves a pointless signal dispatch.
            if self._shutting_down or generation != self._ssh_generation:
                return
            try:
                self._ssh_chunk.emit(generation, data)
            except Exception:
                pass

        def on_close() -> None:
            if self._shutting_down:
                return
            try:
                self._ssh_closed_sig.emit(generation)
            except Exception:
                pass

        try:
            session.read_loop(on_bytes, on_close)
        except Exception:
            # read_loop never raises but defend against a paramiko
            # regression — the reader thread must never crash out.
            try:
                if not self._shutting_down:
                    self._ssh_closed_sig.emit(generation)
            except Exception:
                pass

    def detach_ssh(self, *, silent: bool = False) -> None:
        """
        Tear down the active SSH session.

        Parameters
        ----------
        silent : bool, default False
            Controls the post-detach behaviour:

            * ``silent=True`` — close the channel, drop state, restore
              the idle/local mode quietly. **No text is written to the
              buffer, no local prompt is re-shown, and
              ``session_closed`` is NOT emitted.** Use this path for
              reconnect and pre-connect reset flows where the caller
              is about to write its own "[connecting…]" message.

            * ``silent=False`` — announce the closure with a single
              ``[ssh session closed]`` line. For a standalone (non
              ssh_only) terminal this also returns the widget to local
              mode with a fresh prompt so the user can keep using it
              as a local shell. ``session_closed`` IS emitted.
        """
        # 1. Close the underlying channel (idempotent). Bumping the
        # generation here means any signals still in flight from the
        # reader thread will be dropped when they reach the GUI
        # thread — even in the brief window before close() has
        # actually unblocked the reader.
        self._ssh_generation += 1
        session = self._ssh
        self._ssh = None
        self._ssh_reader = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

        # Reset terminal-emulation state so a future attach starts
        # from a clean slate (no stale decoder bytes, no leftover
        # escape sequence fragment, no stale cursor anchor).
        self._ssh_pending = ""
        self._ssh_home_pending = False
        self._ssh_cursor_pos = 0
        self._ssh_utf8_decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        try:
            self._ssh_resize_timer.stop()
        except Exception:
            pass

        was_in_ssh_mode = (self._mode == "ssh")

        if silent:
            # No output. Restore the resting mode for this widget.
            if was_in_ssh_mode:
                self._mode = "idle" if self._ssh_only else "local"
                self._sync_read_only()
            # Never emits session_closed — the caller owns the state
            # machine and will drive the next transition itself.
            return

        # Non-silent path — only meaningful if we were actually in an
        # SSH session. Detaching an idle terminal is a no-op.
        if not was_in_ssh_mode:
            return

        self._append("\n[ssh session closed]\n")

        if self._ssh_only:
            # The tab owner decides what comes next (reconnect, close,
            # etc.); just sit in idle and reject keystrokes.
            self._mode = "idle"
        else:
            # Standalone local terminal: reclaim the buffer for the
            # local shell so the user can keep working.
            self._mode = "local"
            self._show_local_prompt(banner=False)

        self._sync_read_only()
        self.session_closed.emit()

    # ── Per-session I/O config (line ending, local echo) ─────────────────────

    def set_line_ending(self, mode: str) -> None:
        """
        Set what the Enter key sends on the current remote session.

        ``mode`` is one of "cr", "lf", "crlf" (case-insensitive). Any
        other value falls back to "cr". Safe to call from the GUI
        thread at any time — it doesn't touch the underlying session.
        """
        self._line_ending = {
            "cr":   b"\r",
            "lf":   b"\n",
            "crlf": b"\r\n",
        }.get((mode or "cr").lower(), b"\r")

    def line_ending(self) -> str:
        """Return the current line-ending mode as 'cr' / 'lf' / 'crlf'."""
        return {
            b"\r":   "cr",
            b"\n":   "lf",
            b"\r\n": "crlf",
        }.get(self._line_ending, "cr")

    def set_local_echo(self, on: bool) -> None:
        """
        Enable / disable local echo of typed bytes back into the
        display buffer. Required for serial devices that don't echo.
        """
        self._local_echo = bool(on)

    def local_echo(self) -> bool:
        return bool(self._local_echo)

    def _local_echo_bytes(self, data: bytes) -> None:
        """
        Feed locally-typed bytes back through the remote-display
        pipeline so the user sees them. Routed through the same
        ``_ssh_chunk`` signal the read loop uses, so cursor tracking
        and ANSI parsing stay consistent.
        """
        if not data or self._mode != "ssh":
            return
        try:
            self._ssh_chunk.emit(self._ssh_generation, data)
        except Exception:
            pass

    # ── Read-only policy ─────────────────────────────────────────────────────

    def _sync_read_only(self) -> None:
        """Keep the widget's read-only flag in sync with the current mode."""
        # Idle = no session owns the buffer → user cannot type.
        # Local or SSH = user can interact.
        self.setReadOnly(self._mode == "idle")

    # ── Remote-side closure detection ────────────────────────────────────────

    @pyqtSlot(int)
    def _on_ssh_closed_remote(self, generation: int) -> None:
        """
        GUI-thread handler for spontaneous SSH closure (remote logout,
        network drop). Only fires if we're still in SSH mode and the
        notification belongs to the currently-attached session — a
        late close notification from a previously-replaced reader must
        not write a spurious "[ssh session closed]" line into a fresh
        session's buffer.
        """
        if self._shutting_down:
            return
        if generation != self._ssh_generation:
            return
        if self._mode != "ssh":
            return
        # Use the non-silent path so the user sees exactly one
        # "[ssh session closed]" line and the tab's state machine
        # receives session_closed.
        self.detach_ssh(silent=False)

    # ── Local shell helpers ─────────────────────────────────────────────────

    def set_shell(self, name: str) -> bool:
        """
        Switch the local-mode shell backend.
        Returns True on success. If the requested shell is not installed
        on the current machine the call is rejected and a message is
        printed to the terminal area.
        """
        if name == self._shell_name:
            return True
        if name not in SHELL_BACKENDS:
            self._append(f"\n[unknown shell: {name}]\n")
            return False
        if not shell_is_installed(name):
            self._append(
                f"\n[{name} is not available on this machine — leaving "
                f"the active shell as {self._shell_name}]\n"
            )
            self._show_local_prompt(banner=False)
            return False

        # A shell switch is a meaningful terminal-context change, so
        # we refresh the welcome banner with the new shell name. The
        # `force=True` flag bypasses the page-switch debounce because
        # this is a deliberate user action, not a passive re-entry.
        self._shell_name = name
        if self._mode == "local" and not self._busy:
            refreshed = self.refresh_intro(force=True)
            if not refreshed:
                # Refresh declined (pending input, etc) — fall back to
                # a quiet inline notice so the user still sees the
                # change took effect.
                self._append(f"\n[switched to {name}]\n")
                self._show_local_prompt(banner=False)
        return True

    def shell_name(self) -> str:
        return self._shell_name

    def _show_local_prompt(self, banner: bool = False) -> None:
        if banner:
            # `banner=True` is passed from __init__ for the standalone
            # (non ssh_only) terminal and from `refresh_intro()` when
            # the user re-enters the terminal page after a meaningful
            # gap. Every other call path (cd, clear, shell switch,
            # SSH detach) passes banner=False so the welcome art is
            # never duplicated mid-session and never spams reconnect
            # loops.
            from gui.terminal_banner import build_welcome_banner
            self._append(build_welcome_banner(self._shell_name))
            self._last_banner_at = time.monotonic()
        prompt = self._build_prompt()
        self._append(prompt)
        self._input_anchor = self._end_position()
        self._move_cursor_to_end()

    # ── Lifecycle: re-show banner on context entry ──────────────────────────

    #: Minimum interval (seconds) between two consecutive banner renders
    #: triggered by `refresh_intro()`. Stops rapid page-switch toggling
    #: from spamming the buffer.
    REFRESH_DEBOUNCE_SECS = 8.0

    def refresh_intro(self, *, force: bool = False) -> bool:
        """
        Reset the local-mode terminal to a fresh welcome state.

        Called by `TerminalView.on_entered()` when the user navigates
        back to the terminal page after enough time has passed for the
        intro to feel meaningful again. The behaviour is:

        * No-op when the terminal is **busy** (running a command,
          attached to an SSH session, or in idle ssh_only mode) —
          we never tear down live interaction.
        * No-op when the terminal has **pending input** the user has
          typed but not submitted — we never throw away their work.
        * No-op when the most recent banner is younger than
          ``REFRESH_DEBOUNCE_SECS`` unless ``force=True``.
        * Otherwise: clear the buffer, render the welcome banner, and
          show a fresh prompt.

        Returns ``True`` if the buffer was actually refreshed.
        """
        if self._mode != "local" or self._busy:
            return False
        if self._current_input().strip():
            return False
        if not force:
            now = time.monotonic()
            if now - self._last_banner_at < self.REFRESH_DEBOUNCE_SECS:
                return False
        self.clear()
        self._show_local_prompt(banner=True)
        return True

    def _build_prompt(self) -> str:
        meta = SHELL_BACKENDS.get(self._shell_name)
        if meta and "prompt" in meta:
            try:
                return meta["prompt"](self._cwd)
            except Exception:
                pass
        # Fallback
        if _IS_WINDOWS:
            return f"PS {self._cwd}> "
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        return f"{user}@{platform.node()}:{self._cwd}$ "

    # ── Public command runner (used by SSH and scanner views) ────────────────

    def run_local_command(self, command: str) -> None:
        """
        Programmatically run a command as if the user typed it.
        Only works in local mode.
        """
        if self._mode != "local" or self._busy:
            return
        # Drop any partial input the user may have typed.
        self._replace_current_input(command)
        self._submit_local_input()

    # ── Submit current input ────────────────────────────────────────────────

    def _current_input(self) -> str:
        text = self.toPlainText()
        return text[self._input_anchor:]

    def _replace_current_input(self, new_text: str) -> None:
        cursor = self.textCursor()
        cursor.setPosition(self._input_anchor)
        cursor.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()
        cursor.insertText(new_text)
        self.setTextCursor(cursor)

    def _submit_local_input(self) -> None:
        cmd = self._current_input().rstrip("\r\n")
        # newline visible
        self._append("\n")

        if not cmd.strip():
            self._show_local_prompt(banner=False)
            return

        self._history.append(cmd)
        self._history_idx = len(self._history)

        # Built-ins
        cmd_stripped = cmd.strip()
        if cmd_stripped in ("exit", "quit"):
            self._append("[type the application's quit menu to close Net Engine]\n")
            self._show_local_prompt(banner=False)
            return

        if cmd_stripped == "clear" or cmd_stripped == "cls":
            self.clear()
            self._show_local_prompt(banner=False)
            return

        if cmd_stripped.startswith("cd"):
            target = cmd_stripped[2:].strip().strip('"').strip("'")
            self._do_cd(target)
            self._show_local_prompt(banner=False)
            return

        # External command
        self._busy = True
        self._proc_reader = threading.Thread(
            target=self._run_external,
            args=(cmd,),
            daemon=True,
        )
        self._proc_reader.start()

    def _do_cd(self, target: str) -> None:
        if not target or target == "~":
            target = os.path.expanduser("~")
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(self._cwd, target))
        if os.path.isdir(target):
            try:
                self._cwd = os.path.realpath(target)
            except Exception:
                self._append(f"cd: cannot resolve: {target}\n")
        else:
            self._append(f"cd: not a directory: {target}\n")

    def _resolve_shell_argv(self, cmd: str) -> Optional[list[str]]:
        """
        Build the argv to spawn `cmd` under the active shell backend.
        Falls back to PowerShell/sh if the configured backend is missing.
        """
        meta = SHELL_BACKENDS.get(self._shell_name)
        if not meta:
            return None
        try:
            exe = meta["find"]()
        except Exception:
            exe = None
        if not exe:
            return None
        try:
            return meta["build"](exe, cmd)
        except Exception:
            return None

    def _run_external(self, cmd: str) -> None:
        try:
            shell_cmd = self._resolve_shell_argv(cmd)
            if shell_cmd is None:
                # Sensible cross-platform fallback
                if _IS_WINDOWS:
                    fallback = _find_powershell() or "powershell.exe"
                    shell_cmd = [
                        fallback, "-NoLogo", "-NoProfile",
                        "-ExecutionPolicy", "Bypass",
                        "-Command", cmd,
                    ]
                else:
                    shell_cmd = ["/bin/sh", "-c", cmd]
                self._local_chunk.emit(
                    f"[{self._shell_name} not found — falling back]\n"
                )

            proc = subprocess.Popen(
                shell_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=self._cwd,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )
            self._proc = proc
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if line == "":
                    break
                self._local_chunk.emit(line)
            proc.stdout.close()
            rc = proc.wait()
            self._local_done.emit(rc)
        except FileNotFoundError as exc:
            self._local_chunk.emit(f"command not found: {exc}\n")
            self._local_done.emit(127)
        except Exception as exc:
            self._local_chunk.emit(f"error: {exc}\n")
            self._local_done.emit(1)
        finally:
            self._proc = None

    @pyqtSlot(str)
    def _on_local_chunk(self, text: str) -> None:
        self._append(text)

    @pyqtSlot(int)
    def _on_local_done(self, rc: int) -> None:
        self._busy = False
        if rc != 0:
            t = theme()
            self._append_colored(f"\n[exit code {rc}]\n", t.text_dim)
        else:
            if not self.toPlainText().endswith("\n"):
                self._append("\n")
        self._show_local_prompt(banner=False)

    # ── SSH terminal-emulation output path ──────────────────────────────────
    #
    # This is the path that turns raw bytes coming off the paramiko
    # Channel into on-screen characters. Previously the widget just
    # called ``_append(data)`` and stripped every CSI sequence, which
    # broke every interactive shell feature that depends on in-place
    # prompt redraws (tab completion, backspace, history recall,
    # readline line editing, BusyBox ash). The replacement below is
    # a minimal but correct terminal stream interpreter:
    #
    #   * ``\r``  — cursor to start of current block
    #   * ``\n``  — cursor to start of next block, creating one if needed
    #   * ``\b``  — cursor left one (bounded by block start)
    #   * ``\t``  — advance cursor to next 8-column stop
    #   * ``\x07`` — bell, silently ignored
    #   * printable — inserted in OVERWRITE mode (replaces char under cursor)
    #   * CSI K — erase-in-line   (0/1/2)
    #   * CSI J — erase-in-display (0/1/2)
    #   * CSI A/B/C/D — cursor up/down/right/left
    #   * CSI G  — cursor horizontal absolute (column)
    #   * CSI @  — insert blanks
    #   * CSI P  — delete characters
    #   * CSI H/f — cursor position (best-effort on a linear doc)
    #   * CSI m — SGR (ignored — text renders in the default colour)
    #   * OSC ``\x1b]...\x07`` or ``\x1b]...\x1b\\`` — terminal title etc.,
    #     parsed and discarded
    #
    # Partial escape sequences that straddle TCP chunk boundaries are
    # buffered in ``_ssh_pending`` and prepended to the next chunk.

    @pyqtSlot(int, bytes)
    def _on_ssh_chunk(self, generation: int, data: bytes) -> None:
        # Drop late bytes from a session that has already been
        # replaced or detached — otherwise output from an old
        # connection can bleed into a freshly-attached session's
        # buffer during a fast reconnect.
        if self._shutting_down:
            return
        if generation != self._ssh_generation:
            return
        if self._mode != "ssh":
            return
        self._ssh_process_stream(data)

    def _ssh_process_stream(self, data: bytes) -> None:
        """
        Feed a chunk of raw PTY output through the terminal state
        machine. Walks the decoded string byte-by-byte, executing
        terminal actions against a local QTextCursor anchored at
        ``_ssh_cursor_pos``, then commits the cursor's final position
        back to ``_ssh_cursor_pos`` and optionally to the visible
        text cursor (only if the user isn't mid-selection).
        """
        try:
            new_text = self._ssh_utf8_decoder.decode(data)
        except Exception:
            try:
                new_text = data.decode("utf-8", errors="replace")
            except Exception:
                return
        if self._ssh_pending:
            text = self._ssh_pending + new_text
            self._ssh_pending = ""
        else:
            text = new_text
        if not text:
            return

        try:
            end_pos = self._end_position()
            start_pos = min(self._ssh_cursor_pos, end_pos)
            # Save user's current selection state before we mutate
            # the document — if they're actively highlighting text
            # for a copy, we won't disturb their selection.
            user_cur = self.textCursor()
            user_had_selection = user_cur.hasSelection()

            cursor = self.textCursor()
            cursor.setPosition(start_pos)

            i = 0
            n = len(text)

            while i < n:
                ch = text[i]
                oc = ord(ch)

                # ── Escape sequences ────────────────────────────
                if ch == "\x1b":
                    if i + 1 >= n:
                        # Incomplete — buffer and wait for more
                        self._ssh_pending = text[i:]
                        break
                    nxt = text[i + 1]
                    if nxt == "[":
                        # CSI: read params until a final byte in 0x40-0x7e
                        j = i + 2
                        while j < n:
                            cj = ord(text[j])
                            if 0x40 <= cj <= 0x7e:
                                break
                            j += 1
                        if j >= n:
                            self._ssh_pending = text[i:]
                            break
                        params_str = text[i + 2:j]
                        final_ch = text[j]
                        self._ssh_handle_csi(cursor, params_str, final_ch)
                        i = j + 1
                        continue
                    if nxt == "]":
                        # OSC: read until BEL or ST (ESC \)
                        j = i + 2
                        terminated = False
                        while j < n:
                            if text[j] == "\x07":
                                terminated = True
                                j += 1
                                break
                            if (text[j] == "\x1b"
                                    and j + 1 < n
                                    and text[j + 1] == "\\"):
                                terminated = True
                                j += 2
                                break
                            j += 1
                        if not terminated:
                            self._ssh_pending = text[i:]
                            break
                        # OSC payload is typically a window title.
                        # We don't render it anywhere; just drop it.
                        i = j
                        continue
                    if nxt in "()*+":
                        # Character set designation (ESC ( B selects
                        # ASCII). Payload is exactly one byte.
                        if i + 2 >= n:
                            self._ssh_pending = text[i:]
                            break
                        self._ssh_home_pending = False
                        i += 3
                        continue
                    if nxt in "=>":
                        # Keypad application / numeric mode — ignore
                        self._ssh_home_pending = False
                        i += 2
                        continue
                    if nxt == "M":
                        # Reverse Index — move cursor up one block
                        if cursor.block().previous().isValid():
                            cursor.movePosition(
                                QTextCursor.MoveOperation.PreviousBlock
                            )
                        self._ssh_home_pending = False
                        i += 2
                        continue
                    if nxt in "78":
                        # DECSC / DECRC — save/restore cursor.
                        # We don't implement it; drop.
                        self._ssh_home_pending = False
                        i += 2
                        continue
                    # Unknown escape — consume the intro byte only.
                    self._ssh_home_pending = False
                    i += 2
                    continue

                # ── Control characters ──────────────────────────
                if ch == "\r":
                    cursor.movePosition(
                        QTextCursor.MoveOperation.StartOfBlock
                    )
                    self._ssh_home_pending = False
                    i += 1
                    continue

                if ch == "\n":
                    # Line feed — move to start of next block, creating
                    # one if we're on the last block.
                    block = cursor.block()
                    nxt_block = block.next()
                    if nxt_block.isValid():
                        cursor.setPosition(nxt_block.position())
                    else:
                        cursor.movePosition(
                            QTextCursor.MoveOperation.End
                        )
                        cursor.insertText("\n")
                    self._ssh_home_pending = False
                    i += 1
                    continue

                if ch == "\b":
                    if not cursor.atBlockStart():
                        cursor.movePosition(
                            QTextCursor.MoveOperation.Left
                        )
                    self._ssh_home_pending = False
                    i += 1
                    continue

                if ch == "\x07":
                    # BEL — silently ignored (no audible bell, no visual).
                    # Does not invalidate the home-pending latch because
                    # a bell between H and J shouldn't change the
                    # intent of the clear pair.
                    i += 1
                    continue

                if ch == "\t":
                    col = cursor.positionInBlock()
                    spaces = 8 - (col % 8)
                    self._ssh_insert_overwrite(cursor, " " * spaces)
                    self._ssh_home_pending = False
                    i += 1
                    continue

                if oc < 0x20 or oc == 0x7f:
                    # Any other C0 control or DEL — skip
                    i += 1
                    continue

                # ── Printable run ───────────────────────────────
                # Collect a contiguous run of printable characters
                # and insert them in one call for performance. Stops
                # at the next control byte or escape.
                run_start = i
                while i < n:
                    rc = text[i]
                    rc_ord = ord(rc)
                    if rc == "\x1b" or rc_ord < 0x20 or rc_ord == 0x7f:
                        break
                    i += 1
                run = text[run_start:i]
                if run:
                    self._ssh_home_pending = False
                    self._ssh_insert_overwrite(cursor, run)

            # Save the new terminal cursor position so the next chunk
            # starts where this one left off. Commit it to the visible
            # cursor too, but only if the user isn't in the middle of
            # a selection — we don't want remote output to blow away
            # a selection the user is making to copy text.
            self._ssh_cursor_pos = cursor.position()
            if not user_had_selection:
                self.setTextCursor(cursor)
                self.ensureCursorVisible()
        except RuntimeError:
            # Widget is being torn down — drop the chunk.
            return

    def _ssh_insert_overwrite(self, cursor: QTextCursor, text: str) -> None:
        """
        Insert ``text`` at the cursor in OVERWRITE mode.

        If the cursor is already at the end of its block we just
        insert (extending the line). Otherwise each character
        replaces the character under the cursor — this is what makes
        in-place prompt redraws work correctly. Without overwrite
        mode, every `\\r` + redraw in the shell would stack a second
        copy of the prompt on top of the first.
        """
        if not text:
            return
        # Fast path — appending at the block end, no overwrite needed.
        if cursor.atBlockEnd():
            cursor.insertText(text)
            return
        for ch in text:
            if cursor.atBlockEnd():
                cursor.insertText(ch)
                continue
            cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
                1,
            )
            cursor.removeSelectedText()
            cursor.insertText(ch)

    def _ssh_handle_csi(self, cursor: QTextCursor,
                        params_str: str, final_ch: str) -> None:
        """Dispatch a CSI sequence to the appropriate cursor op."""
        # Drop DEC private mode markers (``?``) — we don't support
        # the modes anyway, but the numbers come in the same form.
        if params_str.startswith("?"):
            params_str = params_str[1:]

        # Snapshot the home-pending latch. ``H`` re-arms it; ``J``
        # consumes it; every other CSI final byte silently drops it.
        home_was_pending = self._ssh_home_pending
        if final_ch not in ("H", "f", "J"):
            self._ssh_home_pending = False

        params: list[int] = []
        for p in params_str.split(";"):
            if p == "":
                params.append(0)
                continue
            try:
                params.append(int(p))
            except ValueError:
                pass

        def _count(idx: int = 0) -> int:
            if idx < len(params) and params[idx] > 0:
                return params[idx]
            return 1

        if final_ch == "K":
            mode = params[0] if params else 0
            if mode == 0:
                cursor.movePosition(
                    QTextCursor.MoveOperation.EndOfBlock,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
            elif mode == 1:
                pos = cursor.position()
                anchor_cur = QTextCursor(cursor)
                anchor_cur.movePosition(
                    QTextCursor.MoveOperation.StartOfBlock
                )
                length = pos - anchor_cur.position()
                cursor.movePosition(
                    QTextCursor.MoveOperation.StartOfBlock,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
                if length > 0:
                    cursor.insertText(" " * length)
            elif mode == 2:
                cursor.movePosition(
                    QTextCursor.MoveOperation.StartOfBlock
                )
                cursor.movePosition(
                    QTextCursor.MoveOperation.EndOfBlock,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
            return

        if final_ch == "J":
            mode = params[0] if params else 0
            # BusyBox / readline / minimal ``clear`` implementations
            # send ``\x1b[H\x1b[J`` as the entire clear-screen
            # sequence (mode 0 = "erase from cursor to end of
            # display"). In a real fixed-grid terminal this wipes
            # the whole visible area because ``\x1b[H`` has just
            # parked the cursor at the top-left. Our linear
            # document has no fixed grid, so mode 0 on its own
            # would only erase from the cursor down — which in the
            # post-home state means "the empty line under the
            # prompt" and looks like clear does nothing.
            #
            # Promote mode 0 to a full clear iff the immediately-
            # preceding command was a CUP-home. That keeps mid-
            # screen usages of ``\x1b[J`` (output streaming, less,
            # etc.) behaving exactly as before.
            if mode == 0 and home_was_pending:
                mode = 2
            # Any erase-in-display consumes the home-pending latch.
            self._ssh_home_pending = False
            if mode == 0:
                cursor.movePosition(
                    QTextCursor.MoveOperation.End,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
            elif mode == 1:
                cursor.movePosition(
                    QTextCursor.MoveOperation.Start,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.removeSelectedText()
            elif mode in (2, 3):
                # Clear screen / clear scrollback — blow the whole
                # document away and re-seat the caller's cursor at
                # the start of the now-empty document. The next
                # printable run in this same chunk (typically the
                # prompt that the shell emits right after ``clear``)
                # therefore lands at the top of the buffer instead
                # of tailing stale position data.
                self.clear()
                cursor.setPosition(0)
                self._ssh_cursor_pos = 0
            return

        if final_ch == "A":
            for _ in range(_count()):
                if cursor.block().previous().isValid():
                    cursor.movePosition(
                        QTextCursor.MoveOperation.PreviousBlock
                    )
            return

        if final_ch == "B":
            for _ in range(_count()):
                if cursor.block().next().isValid():
                    cursor.movePosition(
                        QTextCursor.MoveOperation.NextBlock
                    )
                else:
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertText("\n")
            return

        if final_ch == "C":
            for _ in range(_count()):
                if not cursor.atBlockEnd():
                    cursor.movePosition(
                        QTextCursor.MoveOperation.Right
                    )
            return

        if final_ch == "D":
            for _ in range(_count()):
                if not cursor.atBlockStart():
                    cursor.movePosition(
                        QTextCursor.MoveOperation.Left
                    )
            return

        if final_ch == "G":
            # CHA — cursor to column N (1-indexed) in current line.
            col = (params[0] - 1) if params and params[0] > 0 else 0
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            for _ in range(col):
                if cursor.atBlockEnd():
                    break
                cursor.movePosition(QTextCursor.MoveOperation.Right)
            return

        if final_ch in ("H", "f"):
            # CUP — row;col. A linear text document has no fixed
            # grid, so "home" (no params or 1;1) is the only case
            # we can do usefully: jump to the start of the last
            # line. The home-pending latch is set so a following
            # ``\x1b[J`` (erase-to-end) is recognised as a whole-
            # screen clear rather than an erase of the single line
            # the cursor is sitting on — see the CSI J handler for
            # the full rationale.
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            row = params[0] if len(params) >= 1 and params[0] > 0 else 1
            col = params[1] if len(params) >= 2 and params[1] > 0 else 1
            self._ssh_home_pending = (row == 1 and col == 1)
            return

        if final_ch == "P":
            # DCH — delete N characters forward from cursor.
            n_del = _count()
            cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
                n_del,
            )
            cursor.removeSelectedText()
            return

        if final_ch == "@":
            # ICH — insert N blank characters at cursor, shifting
            # the rest right.
            n_ins = _count()
            pos = cursor.position()
            cursor.insertText(" " * n_ins)
            cursor.setPosition(pos)
            return

        # m (SGR color/style), l/h (modes), r (scroll region),
        # s/u (save/restore cursor), c (device attributes),
        # n (device status), t (window ops): all silently ignored.

    # ── PTY size negotiation ─────────────────────────────────────────────────

    def _compute_terminal_size(self) -> tuple[int, int]:
        """
        Compute the terminal's logical dimensions in character cells
        from the widget's current font metrics + viewport geometry.
        Used both for the initial invoke_shell and for every
        resize_pty sent when the window changes size.
        """
        try:
            fm = self.fontMetrics()
            cw = fm.horizontalAdvance("M")
            if cw <= 0:
                cw = max(6, fm.averageCharWidth())
            lh = fm.lineSpacing()
            if lh <= 0:
                lh = fm.height()
            vp = self.viewport()
            vw = vp.width()
            vh = vp.height()
            cols = max(20, min(500, vw // max(1, cw)))
            rows = max(5, min(200, vh // max(1, lh)))
            return int(cols), int(rows)
        except Exception:
            return 80, 24

    def _ssh_send_resize(self) -> None:
        """Debounced PTY resize — called by ``_ssh_resize_timer``."""
        if self._shutting_down or self._mode != "ssh":
            return
        session = self._ssh
        if session is None:
            return
        try:
            cols, rows = self._compute_terminal_size()
            session.resize(cols, rows)
        except Exception:
            return

    # ── Display helpers ─────────────────────────────────────────────────────

    def _append(self, text: str) -> None:
        if self._shutting_down or not text:
            return
        try:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self.setTextCursor(cursor)
            self._move_cursor_to_end()
        except RuntimeError:
            # Qt object has been deleted while a queued signal was
            # still in flight. Swallow — the widget is going away.
            pass

    def _append_colored(self, text: str, color: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(text, fmt)
        cursor.setCharFormat(QTextCharFormat())
        self.setTextCursor(cursor)
        self._move_cursor_to_end()

    def _end_position(self) -> int:
        return len(self.toPlainText())

    def _move_cursor_to_end(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # ── Key handling ─────────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._shutting_down:
            return
        # ── SSH mode: forward keys directly ──────────────────────────────────
        if self._mode == "ssh":
            session = self._ssh
            if session is not None and session.is_open:
                # Copy / paste shortcuts are intercepted *before* the
                # key reaches the remote. Ctrl+Shift+C, Ctrl+Insert
                # copy the current selection; Ctrl+Shift+V and
                # Shift+Insert paste clipboard text back into the
                # remote shell. Ctrl+C with an active selection
                # copies (and clears the selection) so that a
                # following bare Ctrl+C still reaches the remote as
                # SIGINT — remote Ctrl+C semantics stay intact.
                if self._handle_ssh_copy_paste_shortcut(event):
                    return
                self._handle_ssh_key(event)
                return
            # In SSH mode but the session is already gone — swallow
            # the keystroke until the GUI thread finishes transitioning
            # out of SSH mode. Avoids writing characters into the
            # buffer as if it were a local shell.
            return

        # ── Idle mode: swallow everything so the buffer stays clean ─────────
        # Only copy shortcuts are allowed through so the user can still
        # lift text out of the terminal with Ctrl+C / Ctrl+Shift+C /
        # Ctrl+Insert. We route through the clipboard helper so the
        # Unicode paragraph separator Qt emits for selections becomes
        # plain \n on the system clipboard.
        if self._mode == "idle":
            mods = event.modifiers()
            key = event.key()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            if ctrl and key in (Qt.Key.Key_C, Qt.Key.Key_Insert):
                copy_selected_text(self)
            # Otherwise: eat the event.
            return

        # ── Local mode ───────────────────────────────────────────────────────
        if self._busy:
            # Allow Ctrl+C to kill running command.
            if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._kill_local_proc()
                return
            return  # ignore everything else while busy

        key = event.key()
        mods = event.modifiers()
        cursor = self.textCursor()

        # Block edits before the input anchor.
        if cursor.position() < self._input_anchor:
            self._move_cursor_to_end()
            cursor = self.textCursor()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._submit_local_input()
            return

        if key == Qt.Key.Key_Backspace:
            if cursor.position() <= self._input_anchor:
                return
            super().keyPressEvent(event)
            return

        if key == Qt.Key.Key_Left:
            if cursor.position() <= self._input_anchor:
                return
            super().keyPressEvent(event)
            return

        if key == Qt.Key.Key_Home:
            new_cursor = self.textCursor()
            new_cursor.setPosition(
                self._input_anchor,
                QTextCursor.MoveMode.KeepAnchor if mods & Qt.KeyboardModifier.ShiftModifier
                else QTextCursor.MoveMode.MoveAnchor,
            )
            self.setTextCursor(new_cursor)
            return

        if key == Qt.Key.Key_Up:
            self._history_prev()
            return

        if key == Qt.Key.Key_Down:
            self._history_next()
            return

        if key == Qt.Key.Key_C and mods & Qt.KeyboardModifier.ControlModifier:
            # Copy if there's a selection; otherwise abort current input.
            # Selections copy via the clipboard helper so multi-line
            # selections land on the clipboard with real newlines
            # instead of Qt's U+2029 paragraph separators.
            if cursor.hasSelection():
                copy_selected_text(self)
                return
            self._append("^C\n")
            self._show_local_prompt(banner=False)
            return

        if (key == Qt.Key.Key_Insert
                and mods & Qt.KeyboardModifier.ControlModifier):
            copy_selected_text(self)
            return

        if key == Qt.Key.Key_L and mods & Qt.KeyboardModifier.ControlModifier:
            self.clear()
            self._show_local_prompt(banner=False)
            return

        super().keyPressEvent(event)

    def _handle_ssh_copy_paste_shortcut(self, event: QKeyEvent) -> bool:
        """
        Handle clipboard shortcuts in SSH mode.

        Returns ``True`` if the event was consumed (copy / paste) and
        must NOT be forwarded to the remote shell; ``False`` if the
        caller should continue with normal remote-key dispatch.

        Shortcut map — chosen to match PuTTY / GNOME Terminal / xterm
        conventions so users coming from those tools don't have to
        re-learn anything:

        * ``Ctrl+Shift+C`` / ``Ctrl+Insert`` — copy selection.
        * ``Ctrl+Shift+V`` / ``Shift+Insert`` — paste.
        * ``Ctrl+C`` with an active selection — copy and clear the
          selection. The next bare ``Ctrl+C`` therefore reaches the
          remote shell as SIGINT, preserving interrupt semantics.
        """
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if ctrl and shift and key == Qt.Key.Key_C:
            copy_selected_text(self)
            return True
        if ctrl and not shift and key == Qt.Key.Key_Insert:
            copy_selected_text(self)
            return True
        if ctrl and shift and key == Qt.Key.Key_V:
            self._paste_to_ssh()
            return True
        if shift and not ctrl and key == Qt.Key.Key_Insert:
            self._paste_to_ssh()
            return True

        # Ctrl+C with selection → copy. No selection → fall through
        # so ``_handle_ssh_key`` sends \x03 (SIGINT) to the remote.
        if ctrl and not shift and key == Qt.Key.Key_C:
            cur = self.textCursor()
            if cur.hasSelection():
                copy_selected_text(self)
                cur.clearSelection()
                self.setTextCursor(cur)
                return True

        return False

    def _paste_to_ssh(self) -> None:
        """
        Send the current system clipboard contents to the remote
        session as if the user had typed the text.

        Newlines are normalised to the session's configured line
        ending. For SSH (default ``cr``) that matches historical
        behavior — Windows editors paste their CRLF lines into a PTY
        as bare CR. For Serial sessions, newlines are emitted as the
        configured CR / LF / CRLF so a multi-line AT-command paste
        arrives at the device the same way each typed Enter would.

        If the clipboard is empty or unreadable this is a silent no-op.
        """
        if self._shutting_down or self._mode != "ssh":
            return
        session = self._ssh
        if session is None or not session.is_open:
            return
        text = read_text()
        if not text:
            return
        # Collapse Windows + classic-Mac line breaks to a single
        # internal LF first, then re-emit as the configured ending.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        ending = self._line_ending.decode("ascii", errors="replace")
        # Historical SSH default of bare \r matches the old behavior;
        # in that case feed the shell each line terminated with \n
        # rather than rebuilding into LF (the PTY's onlcr maps it).
        if ending == "\r":
            payload = text  # PTY converts each \n into the prompt's newline
        else:
            payload = text.replace("\n", ending)
        data = payload.encode("utf-8", errors="replace")
        try:
            session.send(data)
        except Exception:
            return
        if self._local_echo:
            # Echo the pasted block back so the user sees it. We use
            # CRLF as the visual line break regardless of what we
            # actually sent on the wire.
            visible = text.replace("\n", "\r\n")
            self._local_echo_bytes(visible.encode("utf-8", errors="replace"))

    def _handle_ssh_key(self, event: QKeyEvent) -> None:
        session = self._ssh
        if session is None or not session.is_open:
            return

        key = event.key()
        text = event.text()
        mods = event.modifiers()

        # Special key mappings → escape sequences understood by xterm shells.
        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            # The line-ending is per-session: SSH PTYs default to bare
            # CR (the PTY's icrnl maps it to a newline), serial AT
            # devices typically need CR-LF or LF.
            session.send(self._line_ending)
            if self._local_echo:
                # Visually advance to a fresh line regardless of the
                # ending we sent on the wire.
                self._local_echo_bytes(b"\r\n")
            return
        if key == Qt.Key.Key_Backspace:
            # Ctrl+Backspace → ^W (delete previous word), matching
            # bash / readline. Plain Backspace sends DEL (0x7f) for
            # SSH/xterm shells (where readline expects DEL) and BS
            # (0x08) for raw serial devices, which is the convention
            # most embedded UART firmware uses for line editing.
            if mods & Qt.KeyboardModifier.ControlModifier:
                session.send(b"\x17")
            elif self._backend_kind == "serial":
                session.send(b"\x08")
                if self._local_echo:
                    # Erase one cell visually: BS, space, BS.
                    self._local_echo_bytes(b"\b \b")
            else:
                session.send(b"\x7f")
                # SSH normally relies on remote echo (readline draws
                # the erase). When local echo is forced on, mirror it.
                if self._local_echo:
                    self._local_echo_bytes(b"\b \b")
            return
        if key == Qt.Key.Key_Tab:
            session.send(b"\t")
            return
        if key == Qt.Key.Key_Backtab:
            # Shift+Tab — standard reverse-tab CSI.
            session.send(b"\x1b[Z")
            return
        if key == Qt.Key.Key_Up:
            session.send(b"\x1b[A")
            return
        if key == Qt.Key.Key_Down:
            session.send(b"\x1b[B")
            return
        if key == Qt.Key.Key_Right:
            session.send(b"\x1b[C")
            return
        if key == Qt.Key.Key_Left:
            session.send(b"\x1b[D")
            return
        if key == Qt.Key.Key_Home:
            session.send(b"\x1b[H")
            return
        if key == Qt.Key.Key_End:
            session.send(b"\x1b[F")
            return
        if key == Qt.Key.Key_PageUp:
            session.send(b"\x1b[5~")
            return
        if key == Qt.Key.Key_PageDown:
            session.send(b"\x1b[6~")
            return
        if key == Qt.Key.Key_Insert:
            session.send(b"\x1b[2~")
            return
        if key == Qt.Key.Key_Delete:
            session.send(b"\x1b[3~")
            return
        if key == Qt.Key.Key_Escape:
            session.send(b"\x1b")
            return
        # Function keys — xterm sequences, the lingua franca of
        # remote shells and TUIs (htop, less, vim, midnight commander).
        if key == Qt.Key.Key_F1:
            session.send(b"\x1bOP")
            return
        if key == Qt.Key.Key_F2:
            session.send(b"\x1bOQ")
            return
        if key == Qt.Key.Key_F3:
            session.send(b"\x1bOR")
            return
        if key == Qt.Key.Key_F4:
            session.send(b"\x1bOS")
            return
        if key == Qt.Key.Key_F5:
            session.send(b"\x1b[15~")
            return
        if key == Qt.Key.Key_F6:
            session.send(b"\x1b[17~")
            return
        if key == Qt.Key.Key_F7:
            session.send(b"\x1b[18~")
            return
        if key == Qt.Key.Key_F8:
            session.send(b"\x1b[19~")
            return
        if key == Qt.Key.Key_F9:
            session.send(b"\x1b[20~")
            return
        if key == Qt.Key.Key_F10:
            session.send(b"\x1b[21~")
            return
        if key == Qt.Key.Key_F11:
            session.send(b"\x1b[23~")
            return
        if key == Qt.Key.Key_F12:
            session.send(b"\x1b[24~")
            return

        # Ctrl-letter → control character.
        # Ctrl+L lands here and is sent as 0x0C, which readline /
        # bash interpret as "clear screen and redraw prompt". On
        # shells without readline (BusyBox ash) the byte is a no-op —
        # that is accurate remote-terminal behavior, not a client bug.
        if (mods & Qt.KeyboardModifier.ControlModifier
                and Qt.Key.Key_A <= key <= Qt.Key.Key_Z):
            ctrl = bytes([key - Qt.Key.Key_A + 1])
            session.send(ctrl)
            return

        # Alt+<printable ASCII> → ESC + char. readline interprets
        # this as "meta" for word-level cursor moves (Alt+b, Alt+f,
        # Alt+d, …). Only triggered for a single ASCII character to
        # keep Windows Alt+F4 / Alt+Tab / menu mnemonics untouched.
        if (mods & Qt.KeyboardModifier.AltModifier
                and not (mods & Qt.KeyboardModifier.ControlModifier)
                and text and len(text) == 1 and 0x20 <= ord(text) < 0x7f):
            session.send(b"\x1b" + text.encode("ascii", errors="replace"))
            # Alt-meta is invisible by convention — don't echo even
            # when local_echo is on; the ESC byte would garble the
            # display.
            return

        if text:
            data = text.encode("utf-8", errors="replace")
            session.send(data)
            if self._local_echo:
                self._local_echo_bytes(data)

    # ── Mouse: keep selection but force cursor to end after click ────────────

    def mousePressEvent(self, event):
        # X11 / xterm / PuTTY-style middle-click paste: the clipboard
        # contents are fed to the remote shell as keyboard input.
        # Handled *before* super() so Qt doesn't try to interpret the
        # middle button as a cursor-move click. We only paste in SSH
        # mode — in local mode the widget has its own input anchor
        # model and pasting into the middle of the prompt would be
        # confusing.
        if (event.button() == Qt.MouseButton.MiddleButton
                and self._mode == "ssh"):
            self._paste_to_ssh()
            event.accept()
            return

        super().mousePressEvent(event)
        # Don't trap selection — only refocus cursor when no selection
        # is being made and the user clicked above the input anchor.
        if not self.textCursor().hasSelection() and self._mode == "local":
            cursor = self.textCursor()
            if cursor.position() < self._input_anchor:
                self._move_cursor_to_end()

    def mouseReleaseEvent(self, event):
        """
        PuTTY-style auto-copy on selection release.

        When the user finishes a left-click drag that produced a
        non-empty selection, the selected text is pushed to the system
        clipboard immediately — no context menu click or keyboard
        shortcut required. The visible selection is preserved so the
        user keeps their visual confirmation of what was copied.
        """
        super().mouseReleaseEvent(event)
        if self._shutting_down:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        try:
            cursor = self.textCursor()
        except RuntimeError:
            return
        if not cursor.hasSelection():
            return
        # copy_selected_text is a no-op if the clipboard is
        # temporarily unavailable, so this stays silent on failure.
        copy_selected_text(self)

    # ── History ─────────────────────────────────────────────────────────────

    def _history_prev(self) -> None:
        if not self._history:
            return
        if self._history_idx == len(self._history):
            self._partial_input = self._current_input()
        self._history_idx = max(0, self._history_idx - 1)
        self._replace_current_input(self._history[self._history_idx])

    def _history_next(self) -> None:
        if not self._history:
            return
        if self._history_idx >= len(self._history):
            return
        self._history_idx += 1
        if self._history_idx == len(self._history):
            self._replace_current_input(self._partial_input)
        else:
            self._replace_current_input(self._history[self._history_idx])

    # ── Termination ─────────────────────────────────────────────────────────

    def _kill_local_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def shutdown(self) -> None:
        """
        Destructive cleanup — called when the owning view/tab is about
        to be destroyed. Always detaches silently so we never write to
        a buffer that is about to be thrown away.

        Sets ``_shutting_down`` first so any cross-thread signals
        still in flight from the SSH reader or local command runner
        are dropped on arrival instead of touching a half-destroyed
        QObject.
        """
        self._shutting_down = True
        try:
            self._ssh_resize_timer.stop()
        except Exception:
            pass
        try:
            self._kill_local_proc()
        except Exception:
            pass
        try:
            self.detach_ssh(silent=True)
        except Exception:
            pass

    # ── Resize → remote PTY ─────────────────────────────────────────────────

    def resizeEvent(self, event):
        """
        Whenever the widget is resized, schedule a debounced
        ``chan.resize_pty`` call so the remote shell's idea of
        ``$COLUMNS`` / ``$LINES`` stays in sync with how much room
        the widget actually has. Without this, BusyBox's line editor
        wraps at whatever the original 120 columns said regardless
        of how narrow or wide the window has become.
        """
        super().resizeEvent(event)
        if self._shutting_down:
            return
        if self._mode == "ssh" and self._ssh is not None:
            try:
                self._ssh_resize_timer.start()
            except RuntimeError:
                pass


# The old ``_strip_basic_ansi`` helper used to live here; it was
# the root cause of every broken BusyBox / OpenWrt interaction in
# the SSH terminal. It stripped every CSI escape sequence (losing
# prompt redraws, tab-completion updates, and line-editing
# corrections) and rewrote every ``\r`` as ``\n`` (which turned
# every in-place shell redraw into a duplicated prompt on a new
# line). It has been replaced by the real terminal stream
# interpreter in ``_ssh_process_stream``.
