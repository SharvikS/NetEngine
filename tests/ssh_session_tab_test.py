"""
Integration test for SshSessionTab + TerminalWidget end-to-end.

Stands up an in-process paramiko SSH server, opens an SshSessionTab
against it under the offscreen Qt platform, and verifies:

  * the tab transitions IDLE -> CONNECTING -> CONNECTED
  * the terminal widget is in 'ssh' mode after connect
  * server-emitted bytes appear in the terminal buffer
  * a wrong password drives the tab to STATE_FAILED
  * disconnect_session drops the tab to STATE_CLOSED
  * shutdown is safe to call from any state
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import paramiko
from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication(sys.argv)

from gui.components.ssh_session_tab import (  # noqa: E402
    SshSessionTab,
    STATE_CONNECTED, STATE_FAILED, STATE_CLOSED,
)
from scanner.ssh_client import SSHProfile  # noqa: E402


HOST = "127.0.0.1"


# ── Minimal SSH server reused from ssh_loopback_test ───────────────────────


class _Server(paramiko.ServerInterface):
    def __init__(self, password="testpw"):
        self._password = password
        self.shell_event = threading.Event()

    def check_auth_password(self, username, password):
        if username == "user" and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_pty_request(self, *args, **kw):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_event.set()
        return True

    def check_channel_window_change_request(self, *args, **kw):
        return True


def _run_server(sock, host_key, ready, stop):
    try:
        client_sock, _ = sock.accept()
    except OSError:
        return
    transport = paramiko.Transport(client_sock)
    transport.add_server_key(host_key)
    server = _Server()
    try:
        transport.start_server(server=server)
    except Exception:
        return
    chan = transport.accept(20)
    if chan is None:
        return
    server.shell_event.wait(10)
    ready.set()
    try:
        chan.send(b"server-banner-line\r\n")
    except Exception:
        pass
    while not stop.is_set():
        if chan.recv_ready():
            try:
                data = chan.recv(4096)
            except Exception:
                break
            if not data:
                break
            try:
                chan.send(b"echo:" + data)
            except Exception:
                break
        if chan.closed:
            break
        time.sleep(0.03)
    try:
        chan.close()
    except Exception:
        pass
    try:
        transport.close()
    except Exception:
        pass


def _start_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    ready = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=_run_server, args=(sock, host_key, ready, stop), daemon=True,
    )
    t.start()
    return port, ready, stop, t, sock


# ── Event-loop helpers ─────────────────────────────────────────────────────


def pump(ms: int) -> None:
    """Pump the Qt event loop for ``ms`` milliseconds so cross-thread
    signals delivered via QueuedConnection actually fire."""
    end = time.time() + (ms / 1000.0)
    while time.time() < end:
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.AllEvents, 50
        )
        time.sleep(0.01)


def wait_for(pred, timeout_s: float = 5.0) -> bool:
    """Pump events until ``pred()`` is truthy or ``timeout_s`` elapses."""
    end = time.time() + timeout_s
    while time.time() < end:
        if pred():
            return True
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.AllEvents, 50
        )
        time.sleep(0.02)
    return pred()


# ── Result registration ────────────────────────────────────────────────────


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


# ── Scenarios ──────────────────────────────────────────────────────────────


@scenario("tab connects, terminal goes into SSH mode, server bytes appear")
def s_connect_and_stream():
    port, ready, stop, t, sock = _start_server()
    profile = SSHProfile(
        host=HOST, port=port, user="user", password="testpw",
    )
    tab = SshSessionTab(profile)
    try:
        tab.start_connection()
        ok = wait_for(lambda: tab.state() == STATE_CONNECTED, timeout_s=8.0)
        assert ok, f"tab did not reach CONNECTED; state={tab.state()!r}"
        assert tab.terminal.mode == "ssh", \
            f"terminal mode != ssh; got {tab.terminal.mode!r}"
        # Wait for the server's banner to land in the terminal buffer.
        ok = wait_for(
            lambda: "server-banner-line" in tab.terminal.toPlainText(),
            timeout_s=5.0,
        )
        assert ok, (
            "server banner missing from terminal buffer; "
            f"buffer={tab.terminal.toPlainText()!r}"
        )
        # Send keystrokes via the session and look for the echo.
        tab._session.send(b"hi\n")
        ok = wait_for(
            lambda: "echo:hi" in tab.terminal.toPlainText(),
            timeout_s=5.0,
        )
        assert ok, (
            "echo not visible in terminal; "
            f"buffer={tab.terminal.toPlainText()!r}"
        )
    finally:
        try:
            tab.shutdown()
        except Exception:
            pass
        try:
            tab.deleteLater()
        except Exception:
            pass
        pump(50)
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("wrong password drives tab to STATE_FAILED with friendly error")
def s_wrong_password():
    port, ready, stop, t, sock = _start_server()
    profile = SSHProfile(
        host=HOST, port=port, user="user", password="WRONG",
    )
    tab = SshSessionTab(profile)
    try:
        tab.start_connection()
        ok = wait_for(lambda: tab.state() == STATE_FAILED, timeout_s=8.0)
        assert ok, f"tab did not reach FAILED; state={tab.state()!r}"
        # The terminal should carry a "[connection failed]" line.
        buf = tab.terminal.toPlainText()
        assert "connection failed" in buf.lower(), \
            f"failure message missing; buffer={buf!r}"
    finally:
        try:
            tab.shutdown()
        except Exception:
            pass
        try:
            tab.deleteLater()
        except Exception:
            pass
        pump(50)
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("disconnect after connect cleanly transitions to STATE_CLOSED")
def s_disconnect_clean():
    port, ready, stop, t, sock = _start_server()
    profile = SSHProfile(
        host=HOST, port=port, user="user", password="testpw",
    )
    tab = SshSessionTab(profile)
    try:
        tab.start_connection()
        ok = wait_for(lambda: tab.state() == STATE_CONNECTED, timeout_s=8.0)
        assert ok, f"tab did not reach CONNECTED; state={tab.state()!r}"
        tab.disconnect_session(silent=False)
        ok = wait_for(lambda: tab.state() == STATE_CLOSED, timeout_s=5.0)
        assert ok, f"tab did not reach CLOSED; state={tab.state()!r}"
        assert tab._session is None, "session reference not released"
    finally:
        try:
            tab.shutdown()
        except Exception:
            pass
        try:
            tab.deleteLater()
        except Exception:
            pass
        pump(50)
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("shutdown on a freshly-built tab is a clean no-op")
def s_shutdown_idle():
    profile = SSHProfile(
        host="127.0.0.1", port=22, user="user", password="x",
    )
    tab = SshSessionTab(profile)
    try:
        tab.shutdown()
        # Calling shutdown twice must not raise.
        tab.shutdown()
    finally:
        try:
            tab.deleteLater()
        except Exception:
            pass
        pump(50)


def main() -> int:
    for fn_name, fn in list(globals().items()):
        if fn_name.startswith("s_") and callable(fn):
            fn()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print()
    print("=" * 60)
    print(f"SSH SESSION-TAB INTEGRATION TESTS: {passed} passed, {failed} failed")
    print("=" * 60)
    for name, ok, msg in RESULTS:
        tag = "PASS" if ok else "FAIL"
        line = f"  [{tag}] {name}"
        if msg:
            line += f"  -- {msg}"
        print(line)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
