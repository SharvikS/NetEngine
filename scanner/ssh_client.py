"""
SSH client wrappers built on paramiko.

Pure-logic classes — the GUI layer drives them and renders the output.
They emit no Qt signals so they can be unit-tested headlessly.

Lifecycle safety
----------------
The SSHSession is the authoritative boundary between "maybe-connected
paramiko state" and the GUI. Every public method is idempotent and
guards against None / dead / half-closed transport, channel, and
client objects so no caller can crash this layer by double-closing,
sending to a dead channel, or racing with a background reader.

* ``start()`` cleans up any partially-constructed client if the
  connect/invoke_shell pair fails, so failed attempts never leak open
  sockets or file descriptors.
* ``close()`` is idempotent and safe to call from any thread. It sets
  the stop flag first so an in-flight ``read_loop`` unblocks and exits
  before the transport is torn down.
* ``read_loop()`` defends against every recv path paramiko can take
  (closed channel, EOF, socket timeout, transport error) and always
  invokes ``on_close`` exactly once even if the loop body raises.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:                                          # pragma: no cover
    paramiko = None
    HAS_PARAMIKO = False


# ── Logging ─────────────────────────────────────────────────────────────────
#
# All SSH lifecycle events go through ``logger`` so the user (or a
# support engineer chasing a connection issue) can flip a single env
# var and see exactly what happened: connection attempt, auth method
# selected, socket connect, banner exchange, channel creation, every
# disconnect reason.
#
# The logger is silent by default (no handler beyond NullHandler) so
# we never spam stdout in normal operation. Set ``NETENGINE_SSH_DEBUG=1``
# in the environment to enable a console handler at DEBUG level. The
# value is read once at import time; flipping it later requires a
# restart, which keeps the logging state predictable.

logger = logging.getLogger("netengine.ssh")
logger.addHandler(logging.NullHandler())


def _enable_console_logging_if_requested() -> None:
    """
    Attach a console handler to ``logger`` if the user has asked for
    SSH debug output via the ``NETENGINE_SSH_DEBUG`` environment
    variable. Called once at module load. Safe to call repeatedly —
    the handler-add is guarded against duplicates.
    """
    if os.environ.get("NETENGINE_SSH_DEBUG", "").strip() not in ("1", "true", "yes", "on"):
        return
    # Avoid stacking handlers on every import (unlikely but cheap to guard).
    for h in logger.handlers:
        if getattr(h, "_netengine_ssh_console", False):
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  ssh: %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler._netengine_ssh_console = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # paramiko's own logger is noisy at DEBUG but extremely useful when
    # diagnosing handshake / kex / auth failures — chain it on too.
    pk_logger = logging.getLogger("paramiko")
    pk_logger.setLevel(logging.DEBUG)
    pk_logger.addHandler(handler)


_enable_console_logging_if_requested()


# ── Friendly error translation ──────────────────────────────────────────────
#
# paramiko surfaces auth/network errors with messages aimed at
# developers ("Authentication failed.", "[Errno 11001] getaddrinfo
# failed", "timed out"). The GUI prints those as-is which doesn't help
# a user figure out *what to fix*. ``friendly_error`` maps the most
# common exception types to a short, plain-English summary plus a
# hint line so the terminal-banner failure block reads like a real
# SSH client's error message.

def friendly_error(exc: BaseException) -> str:
    """
    Translate an SSH connect/start exception into a one-or-two-line
    user-facing message.

    Always returns a non-empty string. Includes the original error
    text when it adds detail (auth back-ends, transport reason
    strings) so a power user can still diagnose without enabling
    debug logging.
    """
    raw = (str(exc) or type(exc).__name__).strip()

    if HAS_PARAMIKO:
        if isinstance(exc, paramiko.AuthenticationException):
            return (
                "Authentication failed — check the username and password "
                "(or private key path)."
            )
        if isinstance(exc, paramiko.BadHostKeyException):
            return (
                "Host key verification failed — the remote host key has "
                "changed. If this is expected, remove the old key from "
                "your known_hosts file."
            )
        if isinstance(exc, paramiko.ChannelException):
            return f"SSH channel error: {raw}"
        if isinstance(exc, paramiko.SSHException):
            low = raw.lower()
            if "banner" in low:
                return (
                    "No SSH banner from server — the host answered but did "
                    "not speak SSH. Verify the port and that an SSH daemon "
                    "is listening."
                )
            if "no existing session" in low:
                return (
                    "Could not open an SSH session — the remote refused or "
                    "tore down the connection."
                )
            return f"SSH error: {raw}"

    if isinstance(exc, socket.timeout):
        return (
            "Connection timed out — the host did not answer in time. "
            "Check the IP/host, port, and that the device is reachable."
        )
    if isinstance(exc, ConnectionRefusedError):
        return (
            "Connection refused — nothing is listening on this port, or a "
            "firewall is blocking it."
        )
    if isinstance(exc, socket.gaierror):
        return f"Could not resolve host — {raw}"
    if isinstance(exc, OSError):
        # Generic network failures: unreachable, network down, etc.
        return f"Network error: {raw}"
    return raw


# ── Connection profile ───────────────────────────────────────────────────────

@dataclass
class SSHProfile:
    name: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    password: str = ""
    key_path: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "key_path": self.key_path,
            # never persist passwords
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SSHProfile":
        return cls(
            name=d.get("name", ""),
            host=d.get("host", ""),
            port=int(d.get("port", 22) or 22),
            user=d.get("user", ""),
            key_path=d.get("key_path", ""),
        )


# ── Interactive SSH shell session ────────────────────────────────────────────

class SSHSession:
    """
    Wraps a paramiko SSHClient + invoke_shell channel.

    Use `start(profile)` to connect, `send(bytes)` to push input,
    `read_loop(callback)` (called from a background thread) to stream
    output back, and `close()` to tear down.

    Thread model
    ------------
    ``send``/``resize``/``close`` may be called from the GUI thread;
    ``read_loop`` runs on a dedicated worker thread. All public methods
    are safe against the channel being None, already closed, or having
    raised during teardown. A single ``threading.Lock`` serialises the
    small number of transitions that mutate ``_client``/``_channel``
    so close() can race safely with a reader thread exit.
    """

    def __init__(self):
        self._client: "Optional[paramiko.SSHClient]" = None
        self._channel = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._closed = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        chan = self._channel
        if chan is None:
            return False
        try:
            # paramiko.Channel.closed is a plain attribute but some
            # transport failures can leave the channel in a state where
            # touching it raises; treat any error as "not open".
            return not bool(chan.closed)
        except Exception:
            return False

    def start(self, profile: SSHProfile, timeout: float = 8.0) -> None:
        if not HAS_PARAMIKO:
            raise RuntimeError(
                "paramiko is not installed — install it with `pip install paramiko`"
            )
        if not profile.host or not profile.user:
            raise ValueError("Host and user are required.")

        port = int(profile.port or 22)
        if port < 1 or port > 65535:
            raise ValueError(f"Port must be in 1..65535, got {profile.port!r}.")

        # Don't allow reusing a session object for a second connection —
        # the old transport could still be holding sockets.
        with self._lock:
            if self._channel is not None or self._client is not None:
                raise RuntimeError("SSHSession is already started; create a new one.")
            if self._closed:
                raise RuntimeError("SSHSession has been closed; create a new one.")

        # Pick the auth method we'll *try first*, for the log line.
        # paramiko itself walks the methods the server advertises; we
        # just record which credential the user supplied.
        auth_method = (
            "key" if profile.key_path
            else "password" if profile.password
            else "agent/key-discovery"
        )
        logger.info(
            "connect attempt host=%s port=%d user=%s auth=%s timeout=%.1fs",
            profile.host, port, profile.user, auth_method, timeout,
        )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(
            hostname=profile.host,
            port=port,
            username=profile.user,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=bool(not profile.password and not profile.key_path),
            allow_agent=bool(not profile.password and not profile.key_path),
        )
        if profile.password:
            kwargs["password"] = profile.password
        if profile.key_path:
            kwargs["key_filename"] = profile.key_path

        chan = None
        t0 = time.monotonic()
        try:
            client.connect(**kwargs)
            logger.info(
                "transport up host=%s after %.2fs — opening shell channel",
                profile.host, time.monotonic() - t0,
            )
            # ``xterm`` is the safest TERM value for BusyBox/OpenWrt:
            # its terminfo entry is tiny but universally installed,
            # whereas ``xterm-256color`` may be missing from a stock
            # OpenWrt build and makes readline fall back to dumb-mode
            # line editing — which is exactly what makes the prompt
            # look broken.
            #
            # The 100x32 default is a placeholder — the terminal
            # widget calls session.resize() with its actual widget
            # dimensions immediately after attach_ssh, so the real
            # shell never prints its first prompt at this size.
            chan = client.invoke_shell(term="xterm", width=100, height=32)
            chan.settimeout(0.0)   # non-blocking reads
            logger.info("shell channel ready host=%s", profile.host)
        except Exception as exc:
            logger.warning(
                "connect failed host=%s port=%d user=%s after %.2fs — %s: %s",
                profile.host, port, profile.user,
                time.monotonic() - t0,
                type(exc).__name__, exc,
            )
            # Clean up the half-open client so a failed connect does
            # not leak a socket / transport thread.
            try:
                if chan is not None:
                    chan.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
            raise

        with self._lock:
            if self._closed:
                # Someone called close() while we were connecting —
                # honour that and tear the fresh channel back down.
                logger.info(
                    "session closed during start() host=%s — tearing fresh channel down",
                    profile.host,
                )
                try:
                    chan.close()
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
                raise RuntimeError("SSHSession closed during start()")
            self._client = client
            self._channel = chan
            self._stop.clear()

    def send(self, data: bytes | str) -> None:
        chan = self._channel
        if chan is None or self._closed:
            return
        try:
            if bool(chan.closed):
                return
        except Exception:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            chan.send(data)
        except Exception:
            # Channel died between the check and the send — silently
            # swallow; the read loop will observe the close and notify.
            pass

    def resize(self, cols: int, rows: int) -> None:
        chan = self._channel
        if chan is None or self._closed:
            return
        try:
            if bool(chan.closed):
                return
        except Exception:
            return
        try:
            chan.resize_pty(width=max(20, cols), height=max(5, rows))
        except Exception:
            pass

    def read_loop(
        self,
        callback: Callable[[bytes], None],
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Blocking read loop — call from a worker thread.

        Invokes `callback(data)` whenever new bytes arrive. When the
        read loop ends for any reason (remote closed the channel,
        network error, local `close()`), `on_close()` is invoked once
        from the worker thread so the UI can update its state.

        This method never raises — every internal failure is swallowed
        and translated into a clean loop exit + on_close call. The
        caller should treat it as "runs until the session ends".
        """
        chan = self._channel
        reason = "stopped"
        try:
            if chan is None:
                logger.debug("read_loop: no channel attached, exiting")
                return
            logger.debug("read_loop: starting")
            while not self._stop.is_set():
                # Bail immediately if the channel was torn down from
                # another thread. Accessing `.closed` can raise if the
                # transport is mid-teardown, so guard it.
                try:
                    if chan.closed:
                        reason = "channel closed"
                        break
                except Exception:
                    reason = "channel attribute error"
                    break

                try:
                    ready = chan.recv_ready()
                except Exception:
                    reason = "recv_ready error"
                    break

                if ready:
                    try:
                        data = chan.recv(4096)
                    except socket.timeout:
                        # Non-blocking recv race — just loop.
                        continue
                    except Exception as exc:
                        reason = f"recv error ({type(exc).__name__})"
                        break
                    if not data:
                        # EOF on the channel.
                        reason = "remote EOF"
                        break
                    try:
                        callback(data)
                    except Exception:
                        # Never let a downstream callback failure kill
                        # the reader thread or leak a traceback into
                        # the worker.
                        pass
                    continue

                # Nothing ready right now — check for transport-level
                # closure (exit status or explicit close) and sleep
                # briefly. paramiko's exit_status_ready() can raise on
                # a dead transport, so guard it.
                try:
                    exit_ready = chan.exit_status_ready()
                except Exception:
                    reason = "transport torn down"
                    break
                if exit_ready:
                    reason = "remote exit"
                    # Drain anything still buffered.
                    try:
                        while not self._stop.is_set() and chan.recv_ready():
                            data = chan.recv(4096)
                            if not data:
                                break
                            try:
                                callback(data)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    break

                # Use the stop event as the sleep primitive so close()
                # unblocks us immediately instead of waiting out the
                # remainder of the 30ms tick.
                if self._stop.wait(0.03):
                    reason = "stop event"
                    break
        except Exception as exc:
            # Catch-all: anything paramiko throws at us translates to a
            # clean end-of-loop.
            reason = f"unexpected error ({type(exc).__name__})"
        finally:
            logger.info("read_loop ended — reason=%s", reason)
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

    def exec_command(
        self,
        command: str,
        *,
        timeout: float = 20.0,
    ) -> tuple[int, str, str]:
        """
        Run a non-interactive command on the remote host.

        Opens a fresh SSH ``exec`` channel on the current transport,
        runs ``command``, and returns ``(returncode, stdout, stderr)``
        as decoded UTF-8 strings. The interactive shell channel used
        by the Terminal widget is **not** touched — paramiko
        multiplexes multiple channels over the same transport, so
        this runs alongside an open shell without interfering with
        what the user sees in the terminal.

        ``timeout`` is applied at the channel level via paramiko's
        ``exec_command`` kwarg, so a wedged remote shell unblocks
        after the timeout instead of hanging forever. On timeout /
        transport error / channel close mid-read, a ``RuntimeError``
        is raised with a short message — callers should translate
        that into their own domain exception.

        Thread safety
        -------------
        ``paramiko.SSHClient.exec_command`` is safe to call from
        multiple threads — each call opens its own channel. The
        ShellBrowser relies on this so listdir/stat/mkdir/rename
        calls submitted from the Qt worker pool never deadlock on
        each other.
        """
        if not HAS_PARAMIKO:
            raise RuntimeError("paramiko is not installed")
        with self._lock:
            if self._closed:
                raise RuntimeError("SSH session closed")
            client = self._client
        if client is None:
            raise RuntimeError("SSH session not connected")
        try:
            stdin, stdout, stderr = client.exec_command(
                command, timeout=timeout
            )
        except Exception as exc:
            raise RuntimeError(f"exec_command failed: {exc}") from exc
        try:
            stdin.close()
        except Exception:
            pass
        try:
            out_bytes = stdout.read()
            err_bytes = stderr.read()
        except Exception as exc:
            raise RuntimeError(f"exec_command read failed: {exc}") from exc
        try:
            rc = stdout.channel.recv_exit_status()
        except Exception:
            rc = -1
        try:
            stdout.close()
        except Exception:
            pass
        try:
            stderr.close()
        except Exception:
            pass
        return (
            int(rc),
            out_bytes.decode("utf-8", errors="replace"),
            err_bytes.decode("utf-8", errors="replace"),
        )

    def open_sftp(self):
        """
        Open a brand-new SFTP sub-channel on the current SSH transport.

        Returns a live ``paramiko.SFTPClient`` on success or ``None`` if
        the session is not connected / paramiko is unavailable / the
        underlying transport refuses the subsystem request. The caller
        owns the returned client and must close it with ``.close()``
        when done — closing it does NOT affect the parent shell
        session.

        This method is thread-safe: it takes a short lock while it
        reads the paramiko client reference so it cannot race with a
        concurrent ``close()``. paramiko's own ``open_sftp`` call is
        blocking, so callers should not invoke this from the GUI
        thread if the remote is slow — run it from a worker.
        """
        if not HAS_PARAMIKO:
            return None
        with self._lock:
            if self._closed:
                return None
            client = self._client
        if client is None:
            return None
        try:
            return client.open_sftp()
        except Exception:
            return None

    def close(self) -> None:
        """
        Tear the session down. Idempotent and safe from any thread.

        The stop flag is set first so any concurrent ``read_loop`` wakes
        up and exits before we close the underlying channel/client — a
        channel close() while paramiko is mid-recv occasionally crashes
        the transport thread in older paramiko builds. We don't join
        the reader here because close() is called from the GUI thread
        and the reader owns its own daemon thread lifetime.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            chan = self._channel
            client = self._client
            self._channel = None
            self._client = None

        had_chan = chan is not None
        # Release the lock before touching paramiko so a slow transport
        # teardown can't deadlock a concurrent is_open() check from the
        # GUI thread.
        try:
            if chan is not None:
                chan.close()
        except Exception:
            pass
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
        if had_chan:
            logger.info("session closed — channel + client torn down")
