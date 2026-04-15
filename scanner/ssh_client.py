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

import socket
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

        # Don't allow reusing a session object for a second connection —
        # the old transport could still be holding sockets.
        with self._lock:
            if self._channel is not None or self._client is not None:
                raise RuntimeError("SSHSession is already started; create a new one.")
            if self._closed:
                raise RuntimeError("SSHSession has been closed; create a new one.")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(
            hostname=profile.host,
            port=int(profile.port or 22),
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
        try:
            client.connect(**kwargs)
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
        except Exception:
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
        try:
            if chan is None:
                return
            while not self._stop.is_set():
                # Bail immediately if the channel was torn down from
                # another thread. Accessing `.closed` can raise if the
                # transport is mid-teardown, so guard it.
                try:
                    if chan.closed:
                        break
                except Exception:
                    break

                try:
                    ready = chan.recv_ready()
                except Exception:
                    break

                if ready:
                    try:
                        data = chan.recv(4096)
                    except socket.timeout:
                        # Non-blocking recv race — just loop.
                        continue
                    except Exception:
                        break
                    if not data:
                        # EOF on the channel.
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
                    break
                if exit_ready:
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
                    break
        except Exception:
            # Catch-all: anything paramiko throws at us translates to a
            # clean end-of-loop.
            pass
        finally:
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

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
