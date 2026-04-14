"""
SSH client wrappers built on paramiko.

Pure-logic classes — the GUI layer drives them and renders the output.
They emit no Qt signals so they can be unit-tested headlessly.
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
    """

    def __init__(self):
        self._client: "Optional[paramiko.SSHClient]" = None
        self._channel = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._channel is not None and not self._channel.closed

    def start(self, profile: SSHProfile, timeout: float = 8.0) -> None:
        if not HAS_PARAMIKO:
            raise RuntimeError(
                "paramiko is not installed — install it with `pip install paramiko`"
            )
        if not profile.host or not profile.user:
            raise ValueError("Host and user are required.")

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

        client.connect(**kwargs)

        chan = client.invoke_shell(term="xterm", width=120, height=32)
        chan.settimeout(0.0)   # non-blocking reads

        self._client = client
        self._channel = chan
        self._stop.clear()

    def send(self, data: bytes | str) -> None:
        if not self.is_open:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            self._channel.send(data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if self.is_open:
            try:
                self._channel.resize_pty(width=max(20, cols), height=max(5, rows))
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
        """
        chan = self._channel
        try:
            if chan is None:
                return
            while not self._stop.is_set():
                try:
                    if chan.recv_ready():
                        data = chan.recv(4096)
                        if not data:
                            break
                        callback(data)
                    elif chan.closed or chan.exit_status_ready():
                        # drain anything still buffered
                        try:
                            while chan.recv_ready():
                                data = chan.recv(4096)
                                if data:
                                    callback(data)
                        except Exception:
                            pass
                        break
                    else:
                        time.sleep(0.03)
                except socket.timeout:
                    time.sleep(0.03)
                except Exception:
                    break
        finally:
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            if self._channel is not None:
                self._channel.close()
        except Exception:
            pass
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._channel = None
        self._client = None
