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
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QKeyEvent, QTextCursor, QFontDatabase, QTextCharFormat, QColor,
)
from PyQt6.QtWidgets import QPlainTextEdit

from gui.themes import ThemeManager, theme
from scanner.ssh_client import SSHSession


_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = 0x08000000 if _IS_WINDOWS else 0


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
        try:
            # Quick check: does PATH or absolute path resolve?
            if os.path.isabs(c) and os.path.isfile(c):
                return c
            # Try via subprocess where
            r = subprocess.run(
                ["where", c], capture_output=True, text=True,
                creationflags=_NO_WINDOW,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
        except Exception:
            continue
    return "powershell.exe"  # last-resort fallback


class TerminalWidget(QPlainTextEdit):
    """
    Reusable embedded terminal control.

    Modes:
        local  → REPL: each line typed runs a shell command
        ssh    → bytes are forwarded to/from an SSHSession
    """

    # Internal signals so worker threads can poke the widget safely.
    _local_chunk = pyqtSignal(str)
    _local_done  = pyqtSignal(int)
    _ssh_chunk   = pyqtSignal(bytes)

    # Outward
    session_closed = pyqtSignal()
    session_opened = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.setReadOnly(False)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(32)

        # Prefer Cascadia / Consolas for retro mono look
        for family in ("Cascadia Mono", "Cascadia Code", "Consolas",
                       "JetBrains Mono", "Courier New"):
            try:
                f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
                from PyQt6.QtGui import QFont
                test = QFont(family)
                if test.exactMatch() or family == "Consolas":
                    f = QFont(family, 11)
                    f.setFixedPitch(True)
                    f.setStyleHint(QFont.StyleHint.Monospace)
                    break
            except Exception:
                pass
        f.setPointSize(11)
        self.setFont(f)
        # Slight letter-spacing for readability
        try:
            from PyQt6.QtGui import QFont
            font = self.font()
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
            self.setFont(font)
        except Exception:
            pass

        self._mode = "local"            # local | ssh
        self._busy = False              # local command running
        self._cwd = os.path.expanduser("~")
        self._input_anchor = 0          # doc position where editable input starts

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

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)
        self._apply_theme_colors()

        self._show_local_prompt(banner=True)

    # ── Theme integration ────────────────────────────────────────────────────

    def _on_theme_changed(self, _t):
        self._apply_theme_colors()

    def _apply_theme_colors(self):
        t = theme()
        # Retro terminal palette: dedicated bg/fg + glow border.
        self.setStyleSheet(
            f"QPlainTextEdit#terminal {{"
            f" background-color: {t.term_bg};"
            f" color: {t.term_fg};"
            f" border: 2px solid {t.term_border};"
            f" border-radius: 6px;"
            f" selection-background-color: {t.bg_select};"
            f" selection-color: {t.white};"
            f" padding: 14px 16px;"
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
        """Switch into ssh mode using an already-connected SSHSession."""
        self.detach_ssh()
        self._mode = "ssh"
        self._ssh = session
        self._busy = False

        if banner:
            self._append(banner)

        self._ssh_reader = threading.Thread(
            target=session.read_loop,
            args=(self._on_ssh_bytes_thread,),
            daemon=True,
        )
        self._ssh_reader.start()
        self.session_opened.emit()
        self._move_cursor_to_end()
        self.setFocus()

    def detach_ssh(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
        self._ssh = None
        self._ssh_reader = None
        if self._mode == "ssh":
            self._mode = "local"
            self._append("\n[ssh session closed]\n")
            self._show_local_prompt(banner=False)
            self.session_closed.emit()

    # ── Local shell helpers ─────────────────────────────────────────────────

    def _show_local_prompt(self, banner: bool = False) -> None:
        if banner:
            user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
            kind = "PowerShell" if _IS_WINDOWS else "bash"
            self._append(
                f"╔══════════════════════════════════════════════════════╗\n"
                f"║       NETSCOPE :: embedded {kind:<10} terminal       ║\n"
                f"╚══════════════════════════════════════════════════════╝\n"
                f"   Type a command and press Enter.\n"
                f"   Built-ins: cd <dir>, clear / cls, ^L\n"
                f"   Logged in as {user}@{platform.node()}\n\n"
            )
        prompt = self._build_prompt()
        self._append(prompt)
        self._input_anchor = self._end_position()
        self._move_cursor_to_end()

    def _build_prompt(self) -> str:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        if _IS_WINDOWS:
            # PowerShell-style prompt
            return f"PS {self._cwd}> "
        return f"{user}@{platform.node()}:{self._cwd}$ "

    # ── Public command runner (used by SSH/SCP/scanner views) ────────────────

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
            self._append("[type the application's quit menu to close NetScope]\n")
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

    def _run_external(self, cmd: str) -> None:
        try:
            if _IS_WINDOWS:
                ps = _find_powershell() or "powershell.exe"
                # -NoProfile keeps startup fast; -Command runs the line.
                shell_cmd = [
                    ps, "-NoLogo", "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", cmd,
                ]
            else:
                shell_cmd = ["/bin/sh", "-c", cmd]
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
        self._kill_local_proc()
        self.detach_ssh()


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
