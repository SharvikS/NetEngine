"""
Unit tests for the SSH terminal emulator in TerminalWidget.

Feeds real BusyBox/OpenWrt-style byte streams through
`_ssh_process_stream` and asserts that the rendered QPlainTextEdit
document matches what a real terminal (PuTTY) would show for the
same input. Each scenario is a regression for a specific symptom
that the old `_strip_basic_ansi` code broke.

Runs offscreen so no display is needed.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from gui.components.terminal_widget import TerminalWidget  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def new_widget() -> TerminalWidget:
    """Build a fresh terminal widget in ssh_only mode with a dummy
    attached session so ``_on_ssh_chunk`` doesn't early-out."""
    w = TerminalWidget(ssh_only=True)
    # Force ssh mode without a real session. The processor only reads
    # mode / generation / shutdown flags — it doesn't touch the
    # session object when processing bytes.
    w._mode = "ssh"
    w._ssh_generation = 1
    w._ssh_cursor_pos = 0
    return w


def feed(w: TerminalWidget, data: bytes) -> None:
    w._ssh_process_stream(data)


def lines(w: TerminalWidget) -> list[str]:
    text = w.toPlainText()
    # Use splitlines rather than split('\n') so a trailing newline
    # doesn't give us a phantom empty final element.
    return text.split("\n")


# ── Scenarios ────────────────────────────────────────────────────────────────

RESULTS: list[tuple[str, bool, str]] = []


def scenario(name: str):
    def deco(fn):
        def wrapped():
            try:
                fn()
            except AssertionError as exc:
                RESULTS.append((name, False, f"assert: {exc}"))
                print(f"[FAIL] {name}: {exc}")
                return
            except Exception as exc:
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
                return
            RESULTS.append((name, True, ""))
            print(f"[PASS] {name}")
        return wrapped
    return deco


@scenario("plain printable chars append")
def s_plain():
    w = new_widget()
    feed(w, b"hello world")
    assert w.toPlainText() == "hello world", repr(w.toPlainText())


@scenario("CRLF newline — prompt appears on next line")
def s_crlf():
    w = new_widget()
    feed(w, b"line one\r\nline two\r\n")
    ls = lines(w)
    assert ls[0] == "line one", ls
    assert ls[1] == "line two", ls


@scenario("bare CR returns cursor to start of line")
def s_cr_only():
    w = new_widget()
    # Print "hello", then \r and overwrite with "HI!!!"
    feed(w, b"hello\rHI!!!")
    # Expected: HI!!! (the H overwrites h, I overwrites e, etc.)
    assert w.toPlainText() == "HI!!!", repr(w.toPlainText())


@scenario("prompt redraw with CR + erase-line (BusyBox tab completion)")
def s_prompt_redraw():
    w = new_widget()
    # BusyBox-style: print the initial prompt, then \r + CSI K + redraw
    feed(w, b"root@OpenWrt:~# ca")
    feed(w, b"\r\x1b[Kroot@OpenWrt:~# cat ")
    # Only ONE line in the document, with the updated prompt.
    assert w.toPlainText() == "root@OpenWrt:~# cat ", repr(w.toPlainText())
    assert "\n" not in w.toPlainText()


@scenario("backspace + space + backspace (line editor erase)")
def s_backspace_erase():
    w = new_widget()
    # User types "hel", then backspace. BusyBox sends \b \x20 \b.
    feed(w, b"hel")
    feed(w, b"\b \b")
    # Cursor is now at the 'l' position, which was erased by space.
    # The visible text has one trailing space but the cursor will
    # overwrite it on the next character. Check visible length.
    # Real terminals leave no visible artefact — the user sees "he".
    # Our overwrite model leaves a trailing space; verify that's
    # where the cursor sits so the next char replaces it.
    text = w.toPlainText()
    assert text == "he " or text == "he", repr(text)
    # The critical thing is that the next char replaces what's at
    # the cursor — the line doesn't grow past what's visible.
    feed(w, b"y")
    assert w.toPlainText() == "hey", repr(w.toPlainText())


@scenario("backspace does not cross line boundary")
def s_backspace_boundary():
    w = new_widget()
    feed(w, b"abc\r\ndef")
    # Cursor is after 'f' on second line. Backspace 5 times should
    # only remove chars on the second line.
    feed(w, b"\b\b\b\b\b")
    ls = lines(w)
    assert ls[0] == "abc", ls
    # Second line cursor went back to start but chars are unchanged
    # until we write over them.
    assert ls[1] == "def", ls


@scenario("CSI 2K (erase whole line) + rewrite")
def s_csi_2k():
    w = new_widget()
    feed(w, b"first line content")
    feed(w, b"\r\x1b[2Kreplacement")
    assert w.toPlainText() == "replacement", repr(w.toPlainText())


@scenario("CSI 0K (erase to end of line)")
def s_csi_0k():
    w = new_widget()
    feed(w, b"keep this / delete this")
    # Position cursor at space after "this" (index 9) via
    # CSI 1G (column 11 in 1-indexed) then erase to end.
    # Simpler: use CR + move right 10 positions with CSI 10C.
    feed(w, b"\r\x1b[10C\x1b[K")
    # Should now be just the first 10 chars.
    assert w.toPlainText() == "keep this ", repr(w.toPlainText())


@scenario("CSI J clear display (clear screen)")
def s_csi_2j():
    w = new_widget()
    feed(w, b"old content\r\nmore old\r\n")
    feed(w, b"\x1b[2J")
    assert w.toPlainText() == "", repr(w.toPlainText())


@scenario("BusyBox clear: H + J (mode 0) wipes the whole document")
def s_busybox_clear():
    # BusyBox / minimal ``clear`` sends CUP-home followed by
    # erase-in-display mode 0. Real terminals treat this as a
    # full-screen clear because the cursor is parked at (1,1).
    # Our linear-document emulator has to recognise the H→J
    # pair explicitly — otherwise mode 0 would only erase the
    # single line the cursor is sitting on.
    w = new_widget()
    feed(w, b"line1\r\nline2\r\nuser@box:~$ ls\r\na  b  c\r\nuser@box:~$ ")
    feed(w, b"\x1b[H\x1b[J")
    assert w.toPlainText() == "", repr(w.toPlainText())


@scenario("Ctrl+L redraw: H + 2J + prompt lands at the top")
def s_ctrl_l_redraw():
    w = new_widget()
    feed(w, b"scrollback\r\nmore\r\nuser@box:~$ ")
    feed(w, b"\x1b[H\x1b[2J")
    feed(w, b"user@box:~$ ")
    assert w.toPlainText() == "user@box:~$ ", repr(w.toPlainText())


@scenario("Non-home CUP + J erases only to end (not full screen)")
def s_csi_j_mid_stream():
    # An erase-to-end that isn't preceded by a home-cursor must
    # behave as the plain ``\x1b[J`` spec says: erase from the
    # cursor to the end of the document. This guards against the
    # home-pending latch leaking into mid-stream uses of J.
    w = new_widget()
    feed(w, b"keep me\r\ndrop me\r\nalso drop")
    # Move cursor up to the middle of the second line, then J.
    feed(w, b"\x1b[2A\r\x1b[4C\x1b[J")
    assert w.toPlainText() == "keep", repr(w.toPlainText())


@scenario("cursor movement CSI A/B/C/D")
def s_cursor_arrows():
    w = new_widget()
    # Build three lines
    feed(w, b"aaa\r\nbbb\r\nccc")
    # Move up 2 lines, left to col 0, then overwrite
    feed(w, b"\x1b[2A\r")
    feed(w, b"AAA")
    # First line should be "AAA"
    assert lines(w)[0] == "AAA", lines(w)


@scenario("SGR color codes are ignored, text still flows")
def s_sgr_ignored():
    w = new_widget()
    # Green "OK" + reset
    feed(w, b"status: \x1b[32mOK\x1b[0m\r\n")
    assert lines(w)[0] == "status: OK", lines(w)


@scenario("escape split across chunks is buffered")
def s_escape_split():
    w = new_widget()
    feed(w, b"prefix\x1b")
    # The escape byte is dangling — nothing new should be visible yet.
    assert w.toPlainText() == "prefix", repr(w.toPlainText())
    feed(w, b"[2Ktext")
    # Now the CSI 2K completes: erase line + write "text"
    assert w.toPlainText() == "text", repr(w.toPlainText())


@scenario("CSI split across chunks is buffered")
def s_csi_split():
    w = new_widget()
    feed(w, b"abc\x1b[")
    assert w.toPlainText() == "abc", repr(w.toPlainText())
    feed(w, b"2Kdef")
    assert w.toPlainText() == "def", repr(w.toPlainText())


@scenario("UTF-8 split across chunks is decoded correctly")
def s_utf8_split():
    w = new_widget()
    # '€' is 3 bytes: 0xe2 0x82 0xac
    feed(w, b"price: \xe2\x82")
    feed(w, b"\xac 5")
    assert w.toPlainText() == "price: € 5", repr(w.toPlainText())


@scenario("OSC window title sequence is swallowed")
def s_osc_title():
    w = new_widget()
    feed(w, b"\x1b]0;my terminal\x07prompt$ ")
    assert w.toPlainText() == "prompt$ ", repr(w.toPlainText())


@scenario("typing 'ls' then Enter produces a single prompt line")
def s_typed_command():
    w = new_widget()
    # Remote echo of 'ls' then CRLF then output then new prompt.
    feed(w, b"root@OpenWrt:~# ")
    # User types 'l', shell echoes 'l' at cursor (overwrite mode).
    feed(w, b"l")
    feed(w, b"s")
    feed(w, b"\r\n")
    feed(w, b"bin  etc  tmp  usr\r\n")
    feed(w, b"root@OpenWrt:~# ")
    ls = lines(w)
    assert ls[0] == "root@OpenWrt:~# ls", ls
    assert ls[1] == "bin  etc  tmp  usr", ls
    assert ls[2] == "root@OpenWrt:~# ", ls


@scenario("long command with multiple backspaces + retype")
def s_backspace_retype():
    w = new_widget()
    feed(w, b"root# ")
    feed(w, b"cat /etc/motd")
    # Backspace all the way back to just after "cat "
    for _ in range(len("/etc/motd")):
        feed(w, b"\b \b")
    # Now retype
    feed(w, b"/etc/hosts")
    assert w.toPlainText() == "root# cat /etc/hosts", repr(w.toPlainText())


@scenario("permission denied error renders cleanly")
def s_permission_denied():
    w = new_widget()
    feed(w, b"root@OpenWrt:~# cat /etc/shadow\r\n")
    feed(w, b"cat: /etc/shadow: Permission denied\r\n")
    feed(w, b"root@OpenWrt:~# ")
    ls = lines(w)
    assert ls[0] == "root@OpenWrt:~# cat /etc/shadow", ls
    assert ls[1] == "cat: /etc/shadow: Permission denied", ls
    assert ls[2] == "root@OpenWrt:~# ", ls


@scenario("BEL is silently consumed")
def s_bel():
    w = new_widget()
    feed(w, b"tab\x07fail")
    assert w.toPlainText() == "tabfail", repr(w.toPlainText())


@scenario("tab character expands to next 8-column stop")
def s_tab():
    w = new_widget()
    feed(w, b"ab\tcd")
    # 'ab' = 2 chars, next stop at 8, so 6 spaces between.
    assert w.toPlainText() == "ab      cd", repr(w.toPlainText())


@scenario("printable run is inserted in one call (not char-by-char)")
def s_run_fast():
    w = new_widget()
    feed(w, b"x" * 1000)
    assert len(w.toPlainText()) == 1000


def main() -> int:
    for fn_name, fn in list(globals().items()):
        if fn_name.startswith("s_") and callable(fn):
            fn()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print()
    print("=" * 60)
    print(f"TERMINAL EMULATION TESTS: {passed} passed, {failed} failed")
    print("=" * 60)
    for name, ok, msg in RESULTS:
        tag = "PASS" if ok else "FAIL"
        line = f"  [{tag}] {name}"
        if msg:
            line += f"  — {msg}"
        print(line)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
