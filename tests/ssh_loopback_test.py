"""
Loopback SSH integration test.

Stands up a paramiko SSH server on 127.0.0.1, drives the application's
SSHSession against it, and verifies:

  * connect succeeds with password auth
  * invoke_shell produces a usable channel
  * read_loop streams the bytes the server sends
  * send() pushes user input into the channel
  * close() tears the session down cleanly
  * a wrong password produces a clean AuthenticationException
  * an unreachable host fails fast within the configured timeout

No external SSH daemon is required. Runs entirely in-process.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import paramiko

from scanner.ssh_client import SSHProfile, SSHSession


HOST = "127.0.0.1"


# ── Tiny in-process SSH server ──────────────────────────────────────────────


class _Server(paramiko.ServerInterface):
    def __init__(self, password="testpw"):
        self._password = password
        self.shell_event = threading.Event()
        self.last_command = None

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

    def check_channel_exec_request(self, channel, command):
        self.last_command = command
        # Echo the command back, then close.
        try:
            channel.send(b"OUT:" + command + b"\n")
            channel.send_exit_status(0)
        finally:
            channel.close()
        return True


def _run_server(sock, host_key, password, ready, channel_holder, stop):
    try:
        client_sock, _ = sock.accept()
    except OSError:
        return
    transport = paramiko.Transport(client_sock)
    transport.add_server_key(host_key)
    server = _Server(password=password)
    try:
        transport.start_server(server=server)
    except Exception:
        return
    # Accept a single channel.
    chan = transport.accept(20)
    if chan is None:
        return
    channel_holder.append(chan)
    server.shell_event.wait(10)
    ready.set()
    # Write a banner that the client should pick up.
    try:
        chan.send(b"hello-from-server\r\n")
    except Exception:
        pass
    # Wait for a message from the client, echo it back.
    while not stop.is_set():
        if chan.recv_ready():
            data = chan.recv(4096)
            if not data:
                break
            try:
                chan.send(b"echo:" + data)
            except Exception:
                break
        if chan.closed:
            break
        time.sleep(0.02)
    try:
        chan.close()
    except Exception:
        pass
    try:
        transport.close()
    except Exception:
        pass


def _start_server():
    """Spin up a one-shot SSH server. Returns (port, stop_event, server_thread)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    host_key = paramiko.RSAKey.generate(2048)
    ready = threading.Event()
    stop = threading.Event()
    channel_holder = []
    t = threading.Thread(
        target=_run_server,
        args=(sock, host_key, "testpw", ready, channel_holder, stop),
        daemon=True,
    )
    t.start()
    return port, ready, stop, t, sock


# ── Result registration ──────────────────────────────────────────────────────


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


# ── Scenarios ────────────────────────────────────────────────────────────────


@scenario("connect with password and stream output")
def s_connect_stream():
    port, ready, stop, t, sock = _start_server()
    received = bytearray()
    closed = threading.Event()
    sess = SSHSession()
    try:
        profile = SSHProfile(
            host=HOST, port=port, user="user", password="testpw",
        )
        sess.start(profile, timeout=5.0)
        assert sess.is_open, "session reports not open after start()"

        def on_bytes(data: bytes) -> None:
            received.extend(data)

        def on_close() -> None:
            closed.set()

        reader = threading.Thread(
            target=sess.read_loop,
            args=(on_bytes, on_close),
            daemon=True,
        )
        reader.start()

        # Wait for the server's banner.
        deadline = time.time() + 5
        while time.time() < deadline:
            if b"hello-from-server" in bytes(received):
                break
            time.sleep(0.05)
        assert b"hello-from-server" in bytes(received), \
            f"banner not received; got {bytes(received)!r}"

        # Send and verify echo.
        sess.send(b"ping\n")
        deadline = time.time() + 5
        while time.time() < deadline:
            if b"echo:ping\n" in bytes(received):
                break
            time.sleep(0.05)
        assert b"echo:ping\n" in bytes(received), \
            f"echo not received; got {bytes(received)!r}"
    finally:
        sess.close()
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("wrong password raises AuthenticationException promptly")
def s_wrong_password():
    port, ready, stop, t, sock = _start_server()
    sess = SSHSession()
    try:
        profile = SSHProfile(
            host=HOST, port=port, user="user", password="WRONG",
        )
        raised = None
        try:
            sess.start(profile, timeout=5.0)
        except Exception as exc:
            raised = exc
        assert raised is not None, "wrong password did not raise"
        # Should be a paramiko AuthenticationException or a generic one
        # whose message mentions auth.
        msg = str(raised).lower()
        assert (
            isinstance(raised, paramiko.AuthenticationException)
            or "auth" in msg
            or "denied" in msg
        ), f"unexpected exception: {type(raised).__name__}: {raised}"
        # Session must not appear to be open after a failed start.
        assert not sess.is_open, "session.is_open should be False after failed connect"
    finally:
        sess.close()
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("unreachable host fails fast within timeout")
def s_unreachable():
    # 192.0.2.1 is TEST-NET-1 — reserved, guaranteed unreachable.
    sess = SSHSession()
    try:
        profile = SSHProfile(
            host="192.0.2.1", port=22, user="user", password="x",
        )
        t0 = time.time()
        raised = None
        try:
            sess.start(profile, timeout=2.0)
        except Exception as exc:
            raised = exc
        elapsed = time.time() - t0
        assert raised is not None, "unreachable host did not raise"
        # Allow some slack: paramiko's banner_timeout/auth_timeout chain
        # can extend beyond the bare connect timeout.
        assert elapsed < 8.0, f"failed too slowly: {elapsed:.1f}s"
        assert not sess.is_open
    finally:
        sess.close()


@scenario("close is idempotent and unblocks reader")
def s_close_idempotent():
    port, ready, stop, t, sock = _start_server()
    closed = threading.Event()
    sess = SSHSession()
    try:
        profile = SSHProfile(
            host=HOST, port=port, user="user", password="testpw",
        )
        sess.start(profile, timeout=5.0)
        reader = threading.Thread(
            target=sess.read_loop,
            args=(lambda d: None, closed.set),
            daemon=True,
        )
        reader.start()
        # Give the reader a beat to start.
        time.sleep(0.1)
        sess.close()
        sess.close()  # second call is the idempotency check
        # Reader should exit promptly after close().
        assert closed.wait(2.0), "on_close not invoked after session.close()"
        assert not sess.is_open
    finally:
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


@scenario("send/recv bytes round-trip cleanly")
def s_round_trip():
    port, ready, stop, t, sock = _start_server()
    received = bytearray()
    sess = SSHSession()
    try:
        profile = SSHProfile(
            host=HOST, port=port, user="user", password="testpw",
        )
        sess.start(profile, timeout=5.0)
        reader = threading.Thread(
            target=sess.read_loop,
            args=(received.extend, lambda: None),
            daemon=True,
        )
        reader.start()
        # Send each word and wait for its echo before sending the
        # next one — otherwise the test server's single recv() can
        # bundle multiple sends into one buffer and only the first
        # one would carry the "echo:" prefix.
        for word in (b"alpha\n", b"bravo\n", b"charlie\n"):
            sess.send(word)
            expected = b"echo:" + word
            deadline = time.time() + 5
            while time.time() < deadline:
                if expected in bytes(received):
                    break
                time.sleep(0.05)
            assert expected in bytes(received), \
                f"missing {expected!r} in {bytes(received)!r}"
    finally:
        sess.close()
        stop.set()
        try:
            sock.close()
        except Exception:
            pass
        t.join(timeout=2)


def main() -> int:
    for fn_name, fn in list(globals().items()):
        if fn_name.startswith("s_") and callable(fn):
            fn()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print()
    print("=" * 60)
    print(f"SSH LOOPBACK TESTS: {passed} passed, {failed} failed")
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
