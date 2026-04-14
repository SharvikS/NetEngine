"""
SSH and SCP/SFTP client wrappers built on paramiko.

These classes are pure-logic; the GUI layer drives them and renders the
output. They emit no Qt signals so they can be unit-tested headlessly.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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

    def read_loop(self, callback: Callable[[bytes], None]) -> None:
        """
        Blocking read loop — call from a worker thread.
        Invokes `callback(data)` whenever new bytes arrive.
        """
        chan = self._channel
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


# ── SCP / SFTP transfer ──────────────────────────────────────────────────────

class SCPTransfer:
    """
    File transfer via paramiko SFTP.
    Direction is upload (local→remote) or download (remote→local).
    Calls `progress_cb(transferred, total)` periodically.
    """

    def __init__(self, profile: SSHProfile):
        if not HAS_PARAMIKO:
            raise RuntimeError(
                "paramiko is not installed — install it with `pip install paramiko`"
            )
        self.profile = profile
        self._client: "Optional[paramiko.SSHClient]" = None
        self._sftp = None
        self._cancel = threading.Event()

    # ------------------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    def _connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.profile.host,
            port=int(self.profile.port or 22),
            username=self.profile.user,
            timeout=8.0,
            look_for_keys=bool(not self.profile.password and not self.profile.key_path),
            allow_agent=bool(not self.profile.password and not self.profile.key_path),
        )
        if self.profile.password:
            kwargs["password"] = self.profile.password
        if self.profile.key_path:
            kwargs["key_filename"] = self.profile.key_path
        client.connect(**kwargs)
        self._client = client
        self._sftp = client.open_sftp()

    def _disconnect(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        except Exception:
            pass
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._sftp = None
        self._client = None

    # ------------------------------------------------------------------------

    def upload(
        self,
        local_path: str,
        remote_path: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        local = Path(local_path)
        if not local.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        try:
            self._connect()
            assert self._sftp is not None

            total = local.stat().st_size
            sent = 0

            with local.open("rb") as src, self._sftp.file(remote_path, "wb") as dst:
                dst.set_pipelined(True)
                while True:
                    if self._cancel.is_set():
                        raise InterruptedError("Transfer cancelled")
                    chunk = src.read(32 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    sent += len(chunk)
                    if progress_cb:
                        progress_cb(sent, total)
            if progress_cb:
                progress_cb(total, total)
        finally:
            self._disconnect()

    def download(
        self,
        remote_path: str,
        local_path: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        try:
            self._connect()
            assert self._sftp is not None

            try:
                attrs = self._sftp.stat(remote_path)
                total = attrs.st_size or 0
            except IOError as exc:
                raise FileNotFoundError(f"Remote file not found: {remote_path}") from exc

            local = Path(local_path)
            local.parent.mkdir(parents=True, exist_ok=True)

            received = 0
            with self._sftp.file(remote_path, "rb") as src, local.open("wb") as dst:
                src.prefetch()
                while True:
                    if self._cancel.is_set():
                        raise InterruptedError("Transfer cancelled")
                    chunk = src.read(32 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        progress_cb(received, total)
            if progress_cb:
                progress_cb(total or received, total or received)
        finally:
            self._disconnect()
