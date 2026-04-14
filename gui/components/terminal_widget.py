"""
Embedded terminal widget.

A `QPlainTextEdit` subclass that runs commands inside a persistent shell.
Two backends are supported:

  * "local"  — fires off subprocess.Popen for each command line typed by
               the user.  Output is streamed back to the widget via a
               worker thread.  Always available, no extra dependencies.

  * "ssh"    — attaches to an `SSHSession` (paramiko invoke_shell channel),
               echoes the remote bytes verbatim and forwards keystrokes
               back to the channel.  Used by the SSH view.

Designed to be theme-aware: re-styles itself when ThemeManager emits.
"""

from __future__ import annotations

import os
import platform
import re as _re
import subprocess
import threading
import time
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QKeyEvent, QTextCursor, QFont, QFontDatabase, QFontInfo,
    QTextCharFormat, QColor,
)
from PyQt6.QtWidgets import QPlainTextEdit

from gui.themes import ThemeManager, theme
from scanner.ssh_client import SSHSession


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
    _local_chunk    = pyqtSignal(str)
    _local_done     = pyqtSignal(int)
    _ssh_chunk      = pyqtSignal(bytes)
    _ssh_closed_sig = pyqtSignal()

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

    def attach_ssh(self, session: SSHSession, banner: str = "") -> None:
        """
        Switch into SSH mode using an already-connected SSHSession.

        Any previously-attached session is torn down silently (no
        "[ssh session closed]" line, no local prompt) so that a clean
        reconnect over a live session only writes the new banner.
        """
        self.detach_ssh(silent=True)
        self._mode = "ssh"
        self._ssh = session
        self._busy = False

        if banner:
            self._append(banner)

        self._ssh_reader = threading.Thread(
            target=session.read_loop,
            args=(self._on_ssh_bytes_thread, self._on_ssh_closed_thread),
            daemon=True,
        )
        self._ssh_reader.start()
        self._sync_read_only()
        self.session_opened.emit()
        self._move_cursor_to_end()
        self.setFocus()

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
        # 1. Close the underlying channel (idempotent).
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
        self._ssh = None
        self._ssh_reader = None

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

    # ── Read-only policy ─────────────────────────────────────────────────────

    def _sync_read_only(self) -> None:
        """Keep the widget's read-only flag in sync with the current mode."""
        # Idle = no session owns the buffer → user cannot type.
        # Local or SSH = user can interact.
        self.setReadOnly(self._mode == "idle")

    # ── Remote-side closure detection ────────────────────────────────────────

    def _on_ssh_closed_thread(self) -> None:
        """
        Called from the SSH read-loop worker thread when the channel
        ends for any reason. Bounces onto the GUI thread via a signal.
        """
        self._ssh_closed_sig.emit()

    @pyqtSlot()
    def _on_ssh_closed_remote(self) -> None:
        """
        GUI-thread handler for spontaneous SSH closure (remote logout,
        network drop). Only fires if we're still in SSH mode — if the
        user explicitly disconnected, `detach_ssh(silent=False)` will
        already have transitioned us out.
        """
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

    # ── SSH helpers ─────────────────────────────────────────────────────────

    def _on_ssh_bytes_thread(self, data: bytes) -> None:
        # Crosses thread boundary safely via signal.
        self._ssh_chunk.emit(data)

    @pyqtSlot(bytes)
    def _on_ssh_chunk(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data)
        # Strip the most common ANSI escapes for readability.
        text = _strip_basic_ansi(text)
        self._append(text)
        self._move_cursor_to_end()

    # ── Display helpers ─────────────────────────────────────────────────────

    def _append(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self._move_cursor_to_end()

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
        # ── SSH mode: forward keys directly ──────────────────────────────────
        if self._mode == "ssh" and self._ssh is not None and self._ssh.is_open:
            self._handle_ssh_key(event)
            return

        # ── Idle mode: swallow everything so the buffer stays clean ─────────
        # Only copy shortcuts are allowed through so the user can still
        # lift text out of the terminal with Ctrl+C / Ctrl+Insert.
        if self._mode == "idle":
            mods = event.modifiers()
            key = event.key()
            if (mods & Qt.KeyboardModifier.ControlModifier
                    and key in (Qt.Key.Key_C, Qt.Key.Key_Insert)):
                super().keyPressEvent(event)
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
            # Copy if there's a selection; otherwise clear current input.
            if cursor.hasSelection():
                super().keyPressEvent(event)
                return
            self._append("^C\n")
            self._show_local_prompt(banner=False)
            return

        if key == Qt.Key.Key_L and mods & Qt.KeyboardModifier.ControlModifier:
            self.clear()
            self._show_local_prompt(banner=False)
            return

        super().keyPressEvent(event)

    def _handle_ssh_key(self, event: QKeyEvent) -> None:
        if self._ssh is None or not self._ssh.is_open:
            return

        key = event.key()
        text = event.text()
        mods = event.modifiers()

        # Special key mappings → escape sequences understood by xterm shells.
        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            self._ssh.send(b"\r")
            return
        if key == Qt.Key.Key_Backspace:
            self._ssh.send(b"\x7f")
            return
        if key == Qt.Key.Key_Tab:
            self._ssh.send(b"\t")
            return
        if key == Qt.Key.Key_Up:
            self._ssh.send(b"\x1b[A")
            return
        if key == Qt.Key.Key_Down:
            self._ssh.send(b"\x1b[B")
            return
        if key == Qt.Key.Key_Right:
            self._ssh.send(b"\x1b[C")
            return
        if key == Qt.Key.Key_Left:
            self._ssh.send(b"\x1b[D")
            return
        if key == Qt.Key.Key_Home:
            self._ssh.send(b"\x1b[H")
            return
        if key == Qt.Key.Key_End:
            self._ssh.send(b"\x1b[F")
            return
        if key == Qt.Key.Key_Delete:
            self._ssh.send(b"\x1b[3~")
            return
        if key == Qt.Key.Key_Escape:
            self._ssh.send(b"\x1b")
            return

        # Ctrl-letter → control character
        if mods & Qt.KeyboardModifier.ControlModifier and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            ctrl = bytes([key - Qt.Key.Key_A + 1])
            self._ssh.send(ctrl)
            return

        if text:
            self._ssh.send(text.encode("utf-8", errors="replace"))

    # ── Mouse: keep selection but force cursor to end after click ────────────

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        # Don't trap selection — only refocus cursor when no selection
        # is being made and the user clicked above the input anchor.
        if not self.textCursor().hasSelection() and self._mode == "local":
            cursor = self.textCursor()
            if cursor.position() < self._input_anchor:
                self._move_cursor_to_end()

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
        """
        self._kill_local_proc()
        self.detach_ssh(silent=True)


# ── Tiny ANSI helper ─────────────────────────────────────────────────────────

_ANSI_RE = _re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_basic_ansi(text: str) -> str:
    """
    Strip the most common ANSI CSI sequences and translate \r without
    full terminal emulation. Good enough for most SSH session output.
    """
    text = _ANSI_RE.sub("", text)
    # Translate carriage returns to newlines if not followed by \n
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\r":
            if i + 1 < len(text) and text[i + 1] == "\n":
                out.append("\n")
                i += 2
                continue
            out.append("\n")
        else:
            out.append(c)
        i += 1
    return "".join(out)
