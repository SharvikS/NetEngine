"""
SCP transfer engine built directly on top of paramiko.Transport.

This module implements the classic OpenSSH SCP (rcp) wire protocol so
Net Engine can run SCP transfers without taking a dependency on the
external ``scp`` package. The protocol speaks via a single channel
opened with ``exec_command("scp -<flags> -- <path>")`` — no SFTP
subsystem is involved, so SCP works even against remotes that disable
SFTP entirely.

Protocol summary
----------------
Sink mode (upload)
    Client runs ``scp -t -- <remote_dir>`` (add ``r`` for recursive).

    After each outgoing control message the client reads a single
    response byte from the remote:

        0x00  OK, continue
        0x01  <text>\\n    warning — still OK to continue
        0x02  <text>\\n    fatal error — abort

    Control messages:

        ``C<mode> <size> <name>\\n``  — begin file
        <size bytes of file data>
        ``\\x00``                      — end-of-file marker
        ``D<mode> 0 <name>\\n``        — enter directory (recursive)
        ``E\\n``                       — leave directory (recursive)

Source mode (download)
    Client runs ``scp -f -- <remote_path>`` (``-r`` for recursive).
    The remote sends the same control messages; the client replies
    with ``\\x00`` after each message to acknowledge.

Thread safety
-------------
A single ScpTransferEngine instance takes a short re-entrant lock
around each top-level transfer so two worker threads cannot interleave
channels on the same paramiko.Transport. All blocking I/O happens on
the calling thread — callers are expected to run transfers from a
worker and forward progress callbacks into the GUI via Qt signals.

Progress callbacks
------------------
Every transfer call accepts an optional ``on_progress(done, total,
label)`` callback. ``done`` is bytes for the current file, ``total``
is the current file size, ``label`` is the short per-file display
name. The callback is invoked at least once per file (immediately
after the file starts) and again on each 64 KiB chunk. Exceptions
raised by the callback are swallowed so a misbehaving GUI cannot
kill the transfer mid-stream.

Cancellation
------------
Each transfer accepts a ``cancel_flag`` — any object with a truthy
``is_set()`` method (including ``threading.Event``). The engine
checks it between chunks; on cancel it closes the channel cleanly
and raises ``ScpCancelled``.
"""

from __future__ import annotations

import os
import shlex
import stat
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:                                          # pragma: no cover
    paramiko = None
    HAS_PARAMIKO = False


# ── Public exceptions ──────────────────────────────────────────────────────

class ScpError(Exception):
    """User-facing transfer failure. The string form is shown in the UI."""


class ScpCancelled(ScpError):
    """Raised when a transfer was cancelled via the cancel_flag."""


# ── Public dataclasses ────────────────────────────────────────────────────

ProgressCallback = Callable[[int, int, str], None]
CancelFlag = object  # duck-typed: anything with .is_set() -> bool


@dataclass(frozen=True)
class ScpResult:
    """Summary returned by every successful top-level transfer."""
    files: int
    directories: int
    bytes_total: int


# ── Engine ────────────────────────────────────────────────────────────────

_CHUNK = 64 * 1024      # 64 KiB — matches OpenSSH's native chunk size
_HEADER_TIMEOUT_S = 30  # max wall time to wait for a single control ack


class ScpTransferEngine:
    """
    SCP transfer engine bound to a live ``scanner.ssh_client.SSHSession``.

    The engine is lightweight: it never holds any state between
    transfers, so a single instance can be reused for an entire
    SSH session. All public methods acquire a short lock so they
    can be called from a worker thread without corrupting the
    underlying paramiko transport.
    """

    def __init__(self, ssh_session) -> None:
        self._session = ssh_session
        self._lock = threading.RLock()

    # ── Upload ──────────────────────────────────────────────────────────

    def put_file(
        self,
        local_path: str,
        remote_dir: str,
        *,
        remote_name: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel_flag: Optional[CancelFlag] = None,
    ) -> ScpResult:
        """
        Upload ``local_path`` into ``remote_dir`` on the remote host.

        The remote filename defaults to the local basename; pass
        ``remote_name`` to rename on the fly. The target directory
        must already exist — SCP does not create missing parents.
        """
        if not os.path.isfile(local_path):
            raise ScpError(f"Local file not found: {local_path}")
        size = os.path.getsize(local_path)
        mode = _safe_mode(local_path, default=0o644)
        name = remote_name or os.path.basename(local_path)

        with self._lock:
            chan = self._open_channel(
                f"scp -t -- {_quote(remote_dir or '.')}"
            )
            try:
                self._expect_ack(chan)
                self._send_file(
                    chan, local_path, size, mode, name,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                return ScpResult(files=1, directories=0, bytes_total=size)
            finally:
                _close_channel(chan)

    def put_tree(
        self,
        local_dir: str,
        remote_parent: str,
        *,
        remote_name: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel_flag: Optional[CancelFlag] = None,
    ) -> ScpResult:
        """
        Recursively upload ``local_dir`` into ``remote_parent`` on the
        remote host. The leaf directory is created under
        ``remote_parent``; pass ``remote_name`` to rename it on the fly.
        """
        if not os.path.isdir(local_dir):
            raise ScpError(f"Local directory not found: {local_dir}")
        name = remote_name or os.path.basename(local_dir.rstrip("\\/"))
        if not name:
            raise ScpError(f"Cannot derive a directory name from {local_dir!r}")

        with self._lock:
            chan = self._open_channel(
                f"scp -rt -- {_quote(remote_parent or '.')}"
            )
            try:
                self._expect_ack(chan)
                files, dirs, total = self._send_directory(
                    chan, local_dir, name,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                return ScpResult(files=files, directories=dirs, bytes_total=total)
            finally:
                _close_channel(chan)

    # ── Download ────────────────────────────────────────────────────────

    def get_file(
        self,
        remote_path: str,
        local_dir: str,
        *,
        local_name: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel_flag: Optional[CancelFlag] = None,
    ) -> ScpResult:
        """
        Download a single remote file into ``local_dir``.

        Overwrites any existing local file with the same name.
        """
        if not remote_path:
            raise ScpError("Remote path is required")
        os.makedirs(local_dir, exist_ok=True)

        with self._lock:
            chan = self._open_channel(
                f"scp -f -- {_quote(remote_path)}"
            )
            try:
                # Signal readiness so the remote starts streaming.
                chan.sendall(b"\x00")
                files, dirs, total = self._receive_stream(
                    chan,
                    root_dir=local_dir,
                    rename_first_to=local_name,
                    allow_dirs=False,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                if files == 0:
                    raise ScpError(f"Remote did not return a file: {remote_path}")
                return ScpResult(files=files, directories=dirs, bytes_total=total)
            finally:
                _close_channel(chan)

    def get_tree(
        self,
        remote_path: str,
        local_parent: str,
        *,
        local_name: Optional[str] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel_flag: Optional[CancelFlag] = None,
    ) -> ScpResult:
        """
        Recursively download a remote directory tree into ``local_parent``.

        The leaf directory is created under ``local_parent``; pass
        ``local_name`` to rename it on the fly.
        """
        if not remote_path:
            raise ScpError("Remote path is required")
        os.makedirs(local_parent, exist_ok=True)

        with self._lock:
            chan = self._open_channel(
                f"scp -rf -- {_quote(remote_path)}"
            )
            try:
                chan.sendall(b"\x00")
                files, dirs, total = self._receive_stream(
                    chan,
                    root_dir=local_parent,
                    rename_first_to=local_name,
                    allow_dirs=True,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                return ScpResult(files=files, directories=dirs, bytes_total=total)
            finally:
                _close_channel(chan)

    # ── Internal: channel setup ────────────────────────────────────────

    def _open_channel(self, command: str):
        """Open a fresh paramiko channel running ``command`` on the remote."""
        if not HAS_PARAMIKO:
            raise ScpError("paramiko is not installed")
        session = self._session
        if session is None or not getattr(session, "is_open", False):
            raise ScpError("SSH session is not connected")
        client = getattr(session, "_client", None)
        if client is None:
            raise ScpError("SSH session is not connected")
        try:
            transport = client.get_transport()
        except Exception as exc:
            raise ScpError(f"SSH transport unavailable: {exc}") from exc
        if transport is None or not transport.is_active():
            raise ScpError("SSH transport is not active")
        try:
            chan = transport.open_session()
        except Exception as exc:
            raise ScpError(f"Could not open SSH channel: {exc}") from exc
        try:
            chan.settimeout(_HEADER_TIMEOUT_S)
            chan.exec_command(command)
        except Exception as exc:
            _close_channel(chan)
            raise ScpError(f"scp exec failed: {exc}") from exc
        return chan

    # ── Internal: upload (sink side) ────────────────────────────────────

    def _send_file(
        self,
        chan,
        local_path: str,
        size: int,
        mode: int,
        name: str,
        *,
        on_progress: Optional[ProgressCallback],
        cancel_flag: Optional[CancelFlag],
    ) -> None:
        header = f"C{mode & 0o7777:04o} {size} {name}\n".encode("utf-8")
        chan.sendall(header)
        self._expect_ack(chan)

        sent = 0
        try:
            fh = open(local_path, "rb")
        except OSError as exc:
            raise ScpError(f"{local_path}: {exc}") from exc
        try:
            _invoke_progress(on_progress, 0, size, name)
            while True:
                if _is_cancelled(cancel_flag):
                    raise ScpCancelled(f"Transfer cancelled: {name}")
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                try:
                    chan.sendall(chunk)
                except Exception as exc:
                    raise ScpError(f"Channel write failed: {exc}") from exc
                sent += len(chunk)
                _invoke_progress(on_progress, sent, size, name)
        finally:
            try:
                fh.close()
            except Exception:
                pass

        # Final 0x00 marks the end of the file body.
        chan.sendall(b"\x00")
        self._expect_ack(chan)

    def _send_directory(
        self,
        chan,
        local_dir: str,
        remote_name: str,
        *,
        on_progress: Optional[ProgressCallback],
        cancel_flag: Optional[CancelFlag],
    ) -> tuple[int, int, int]:
        """
        Recursive upload inside an already-opened sink-mode channel.

        Returns (file_count, dir_count, byte_total).
        """
        dir_mode = _safe_mode(local_dir, default=0o755)
        chan.sendall(
            f"D{dir_mode & 0o7777:04o} 0 {remote_name}\n".encode("utf-8")
        )
        self._expect_ack(chan)

        files = 0
        dirs = 1
        total = 0
        try:
            entries = sorted(os.listdir(local_dir))
        except OSError as exc:
            raise ScpError(f"{local_dir}: {exc}") from exc

        for entry in entries:
            if _is_cancelled(cancel_flag):
                raise ScpCancelled("Transfer cancelled")
            path = os.path.join(local_dir, entry)
            try:
                st = os.stat(path)
            except OSError:
                # Skip unreadable entries rather than aborting the
                # whole tree — SCP itself does the same.
                continue
            if stat.S_ISDIR(st.st_mode):
                sub_f, sub_d, sub_t = self._send_directory(
                    chan, path, entry,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                files += sub_f
                dirs += sub_d
                total += sub_t
            elif stat.S_ISREG(st.st_mode):
                self._send_file(
                    chan, path, st.st_size, st.st_mode, entry,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                files += 1
                total += int(st.st_size)
            # Other kinds (symlinks, fifos, devices) are skipped.

        chan.sendall(b"E\n")
        self._expect_ack(chan)
        return files, dirs, total

    # ── Internal: download (source side) ───────────────────────────────

    def _receive_stream(
        self,
        chan,
        *,
        root_dir: str,
        rename_first_to: Optional[str],
        allow_dirs: bool,
        on_progress: Optional[ProgressCallback],
        cancel_flag: Optional[CancelFlag],
    ) -> tuple[int, int, int]:
        """
        Loop reading SCP control messages from the remote and
        materialising them under ``root_dir``.

        ``rename_first_to`` optionally overrides the name of the
        first top-level entry the remote sends — used by the single-
        file get_file() to honour a user-chosen destination name.
        """
        # Stack of directories we are "inside" on the local side; the
        # current target dir is stack[-1].
        stack: list[str] = [root_dir]
        files = 0
        dirs = 0
        total = 0
        first = True

        while True:
            if _is_cancelled(cancel_flag):
                raise ScpCancelled("Transfer cancelled")

            line = self._read_control_line(chan)
            if line is None:
                # Remote closed the channel cleanly.
                break

            kind = line[:1]

            if kind == "T":
                # Timestamp prefix: "T<mtime> 0 <atime> 0". We don't
                # preserve timestamps for now; just ack and continue.
                chan.sendall(b"\x00")
                continue

            if kind == "C":
                mode, size, name = _parse_entry_header(line)
                if first and rename_first_to:
                    name = rename_first_to
                first = False
                local_path = os.path.join(stack[-1], name)
                chan.sendall(b"\x00")
                _invoke_progress(on_progress, 0, size, name)
                self._receive_file_body(
                    chan, local_path, size,
                    name=name,
                    on_progress=on_progress,
                    cancel_flag=cancel_flag,
                )
                self._expect_ack(chan)
                files += 1
                total += size
                continue

            if kind == "D":
                if not allow_dirs:
                    raise ScpError(
                        "Remote path is a directory — use the folder "
                        "variant to download it"
                    )
                mode, _size, name = _parse_entry_header(line)
                if first and rename_first_to:
                    name = rename_first_to
                first = False
                sub = os.path.join(stack[-1], name)
                try:
                    os.makedirs(sub, exist_ok=True)
                except OSError as exc:
                    raise ScpError(f"{sub}: {exc}") from exc
                stack.append(sub)
                dirs += 1
                chan.sendall(b"\x00")
                continue

            if kind == "E":
                if len(stack) > 1:
                    stack.pop()
                chan.sendall(b"\x00")
                continue

            # Error / warning messages from the remote: 0x01 / 0x02
            # byte prefixes. Our _read_control_line already translates
            # those to ScpError, so reaching here means an unexpected
            # control byte.
            raise ScpError(f"Unexpected SCP control message: {line!r}")

        return files, dirs, total

    def _receive_file_body(
        self,
        chan,
        local_path: str,
        size: int,
        *,
        name: str,
        on_progress: Optional[ProgressCallback],
        cancel_flag: Optional[CancelFlag],
    ) -> None:
        """Stream ``size`` bytes from the channel into ``local_path``."""
        parent = os.path.dirname(local_path) or "."
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ScpError(f"{parent}: {exc}") from exc

        try:
            fh = open(local_path, "wb")
        except OSError as exc:
            raise ScpError(f"{local_path}: {exc}") from exc

        received = 0
        try:
            while received < size:
                if _is_cancelled(cancel_flag):
                    raise ScpCancelled(f"Transfer cancelled: {name}")
                remaining = size - received
                want = remaining if remaining < _CHUNK else _CHUNK
                try:
                    chunk = chan.recv(want)
                except Exception as exc:
                    raise ScpError(f"Channel read failed: {exc}") from exc
                if not chunk:
                    raise ScpError(
                        f"Remote closed channel mid-file: {name} "
                        f"({received}/{size} bytes)"
                    )
                try:
                    fh.write(chunk)
                except OSError as exc:
                    raise ScpError(f"{local_path}: {exc}") from exc
                received += len(chunk)
                _invoke_progress(on_progress, received, size, name)
        finally:
            try:
                fh.close()
            except Exception:
                pass

        # After the file body the remote sends a single 0x00 byte to
        # signal end-of-file; our caller does the _expect_ack().

    # ── Internal: control-channel primitives ───────────────────────────

    def _expect_ack(self, chan) -> None:
        """
        Read one acknowledgement byte. Raises ScpError on warning /
        error responses so the caller sees a clean failure instead of
        a wedged channel.
        """
        try:
            byte = chan.recv(1)
        except Exception as exc:
            raise ScpError(f"SCP ack read failed: {exc}") from exc
        if not byte:
            raise ScpError("SCP: remote closed channel before acknowledgement")
        code = byte[0]
        if code == 0:
            return
        if code in (1, 2):
            message = _read_line(chan).decode("utf-8", errors="replace").strip()
            if not message:
                message = "remote SCP error"
            # 0x01 is a "warning" but in practice OpenSSH treats it as
            # fatal for the current operation — so do we.
            raise ScpError(f"SCP remote: {message}")
        raise ScpError(f"SCP: unexpected ack byte 0x{code:02x}")

    def _read_control_line(self, chan) -> Optional[str]:
        """
        Read one control message starting with its kind byte. Returns
        None on clean EOF, or the decoded line without the trailing
        newline on success. 0x01/0x02 warn/error bytes raise ScpError.
        """
        try:
            first = chan.recv(1)
        except Exception as exc:
            raise ScpError(f"SCP read failed: {exc}") from exc
        if not first:
            return None
        code = first[0]
        if code in (1, 2):
            message = _read_line(chan).decode("utf-8", errors="replace").strip()
            raise ScpError(f"SCP remote: {message or 'error'}")
        rest = _read_line(chan)
        return chr(code) + rest.decode("utf-8", errors="replace").rstrip("\n")


# ── Module-level helpers ──────────────────────────────────────────────────

def _read_line(chan) -> bytes:
    """Read bytes from ``chan`` up to and including the next \\n (stripped)."""
    out = bytearray()
    while True:
        try:
            b = chan.recv(1)
        except Exception:
            break
        if not b:
            break
        if b == b"\n":
            break
        out.extend(b)
    return bytes(out)


def _parse_entry_header(line: str) -> tuple[int, int, str]:
    """
    Parse a ``C<mode> <size> <name>`` or ``D<mode> 0 <name>`` header.

    The first character (kind) has already been consumed by the reader;
    ``line`` still carries it so we can strip it here.
    """
    body = line[1:]  # drop leading 'C' / 'D'
    parts = body.split(" ", 2)
    if len(parts) != 3:
        raise ScpError(f"Malformed SCP header: {line!r}")
    mode_s, size_s, name = parts
    try:
        mode = int(mode_s, 8)
    except ValueError as exc:
        raise ScpError(f"Bad mode in SCP header: {line!r}") from exc
    try:
        size = int(size_s)
    except ValueError as exc:
        raise ScpError(f"Bad size in SCP header: {line!r}") from exc
    return mode, size, name


def _safe_mode(path: str, *, default: int) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return default


def _quote(path: str) -> str:
    """
    Shell-quote a path for embedding inside an exec_command string.

    Uses POSIX single-quote quoting so special characters in the path
    can never be interpreted by the remote's shell.
    """
    # shlex.quote handles the POSIX case perfectly; the remote is a
    # POSIX host or is close enough that the same quoting applies.
    return shlex.quote(path)


def _close_channel(chan) -> None:
    if chan is None:
        return
    try:
        chan.shutdown_write()
    except Exception:
        pass
    try:
        chan.close()
    except Exception:
        pass


def _is_cancelled(flag: Optional[CancelFlag]) -> bool:
    if flag is None:
        return False
    try:
        return bool(flag.is_set())
    except Exception:
        return False


def _invoke_progress(
    cb: Optional[ProgressCallback],
    done: int,
    total: int,
    label: str,
) -> None:
    if cb is None:
        return
    try:
        cb(int(done), int(total), str(label))
    except Exception:
        pass
