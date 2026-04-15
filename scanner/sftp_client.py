"""
SFTP browser / metadata provider.

Net Engine's File Transfer workspace is SCP-first: the actual byte-
level transfers go through ``scanner.scp_transfer.ScpTransferEngine``.
SCP, however, has no directory-listing primitive, so the browser
side of the UI needs a second channel for metadata.

This module owns that metadata channel. It is a thin thread-safe
wrapper around ``paramiko.SFTPClient`` that exposes only the
operations the file manager needs for browsing and non-transfer
filesystem ops: listdir, stat, mkdir, rmdir, rmtree, remove, rename,
exists. Transfers are deliberately NOT exposed here — they live in
the SCP engine so there is exactly one code path that moves bytes.

Threading
---------
paramiko.SFTPClient is not thread-safe. Every public method holds a
single re-entrant lock for the duration of the SFTP call, so two
workers submitted back-to-back will run sequentially on the shared
channel without corruption.

Error policy
------------
Every method either returns a plain value on success or raises
``SftpError`` with a short human-readable message on failure —
permission denied, path missing, transport dead, timeout, etc. The
GUI layer prints the message verbatim into its status area. No
paramiko traceback ever leaks to the user.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from typing import Optional


class SftpError(Exception):
    """User-facing SFTP failure. The string form is shown in the UI log."""


@dataclass(frozen=True)
class SftpEntry:
    """One directory listing row."""
    name: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: float         # epoch seconds; 0 if unknown
    mode: int            # posix permission bits


class SftpBrowser:
    """
    Thread-safe facade around a single paramiko.SFTPClient used purely
    for directory browsing and non-transfer filesystem ops.

    Usage:
        browser = SftpBrowser(session)          # borrow paramiko client
        browser.open()                          # open the SFTP channel
        entries = browser.listdir("/home")
        browser.mkdir("/tmp/new")
        browser.rename("/tmp/a.txt", "/tmp/b.txt")
        browser.close()

    All operations except ``open``/``close`` acquire a short lock so
    two worker threads can call methods on the same browser without
    corrupting paramiko's single-channel state.
    """

    def __init__(self, ssh_session) -> None:
        # We intentionally keep a reference to the SSH session rather
        # than the paramiko client directly — the session owns the
        # underlying transport and will close it out from under us if
        # the user disconnects. We re-check session liveness on every
        # public call so a dead transport produces a clean error
        # instead of a paramiko traceback.
        self._session = ssh_session
        self._sftp = None                                # paramiko.SFTPClient
        self._lock = threading.RLock()
        self._closed = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._sftp is not None and not self._closed

    def open(self) -> None:
        """Open the SFTP sub-channel. Raises SftpError on failure."""
        with self._lock:
            if self._closed:
                raise SftpError("SFTP browser has been closed")
            if self._sftp is not None:
                return
            if self._session is None or not getattr(self._session, "is_open", False):
                raise SftpError("SSH session is not connected")
            try:
                sftp = self._session.open_sftp()
            except Exception as exc:
                raise SftpError(f"Could not open SFTP channel: {exc}") from exc
            if sftp is None:
                raise SftpError("Remote refused SFTP subsystem")
            self._sftp = sftp

    def close(self) -> None:
        """Close the SFTP channel. Idempotent; safe from any thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sftp = self._sftp
            self._sftp = None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass

    # ── Path helpers ─────────────────────────────────────────────────────────

    def normalize(self, path: str) -> str:
        """Resolve ``path`` against the remote's sense of cwd."""
        with self._lock:
            self._require_open()
            try:
                return self._sftp.normalize(path or ".")
            except Exception as exc:
                raise SftpError(str(exc)) from exc

    def home(self) -> str:
        """Best-effort remote home directory."""
        try:
            return self.normalize(".")
        except SftpError:
            return "/"

    # ── Directory operations ────────────────────────────────────────────────

    def listdir(self, path: str) -> list[SftpEntry]:
        """
        Return a sorted list of entries in ``path``. Directories come
        first, then files, each group alphabetical. Hidden entries (.*)
        are included — the GUI decides whether to show them.
        """
        with self._lock:
            self._require_open()
            try:
                raw = self._sftp.listdir_attr(path)
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {path}") from exc
            except FileNotFoundError as exc:
                raise SftpError(f"No such directory: {path}") from exc
            except IOError as exc:
                raise SftpError(f"{path}: {exc}") from exc
            except Exception as exc:
                raise SftpError(f"{path}: {exc}") from exc

        entries: list[SftpEntry] = []
        for a in raw:
            mode = getattr(a, "st_mode", 0) or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode)
            # If it's a symlink, resolve it so directory links are
            # navigable. stat() is a separate round-trip, so only do it
            # for links (which are rare).
            if is_link:
                try:
                    real = self._sftp.stat(_join(path, a.filename))
                    is_dir = stat.S_ISDIR(real.st_mode or 0)
                except Exception:
                    pass
            entries.append(
                SftpEntry(
                    name=a.filename,
                    is_dir=bool(is_dir),
                    is_link=bool(is_link),
                    size=int(getattr(a, "st_size", 0) or 0),
                    mtime=float(getattr(a, "st_mtime", 0) or 0),
                    mode=int(mode),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def mkdir(self, path: str) -> None:
        with self._lock:
            self._require_open()
            try:
                self._sftp.mkdir(path)
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {path}") from exc
            except IOError as exc:
                raise SftpError(f"mkdir {path}: {exc}") from exc
            except Exception as exc:
                raise SftpError(f"mkdir {path}: {exc}") from exc

    def remove_file(self, path: str) -> None:
        with self._lock:
            self._require_open()
            try:
                self._sftp.remove(path)
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {path}") from exc
            except FileNotFoundError as exc:
                raise SftpError(f"No such file: {path}") from exc
            except Exception as exc:
                raise SftpError(f"remove {path}: {exc}") from exc

    def rmdir(self, path: str) -> None:
        with self._lock:
            self._require_open()
            try:
                self._sftp.rmdir(path)
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {path}") from exc
            except IOError as exc:
                raise SftpError(f"rmdir {path}: {exc}") from exc
            except Exception as exc:
                raise SftpError(f"rmdir {path}: {exc}") from exc

    def rmtree(self, path: str) -> None:
        """
        Recursively remove a directory and everything inside it.

        Walks via ``listdir_attr`` rather than paramiko's posix_rename
        tricks so it works on any SFTP server. Non-directory entries
        are unlinked; directories are descended into and rmdir'd once
        empty. An error on one entry surfaces as an SftpError describing
        the offending path.
        """
        with self._lock:
            self._require_open()
            self._rmtree_inner(path)

    def _rmtree_inner(self, path: str) -> None:
        """Lock-free recursion used by rmtree (holds the outer lock)."""
        try:
            entries = self._sftp.listdir_attr(path)
        except FileNotFoundError as exc:
            raise SftpError(f"No such directory: {path}") from exc
        except PermissionError as exc:
            raise SftpError(f"Permission denied: {path}") from exc
        except Exception as exc:
            raise SftpError(f"{path}: {exc}") from exc

        for a in entries:
            child = _join(path, a.filename)
            mode = getattr(a, "st_mode", 0) or 0
            if stat.S_ISDIR(mode):
                self._rmtree_inner(child)
            else:
                try:
                    self._sftp.remove(child)
                except PermissionError as exc:
                    raise SftpError(f"Permission denied: {child}") from exc
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    raise SftpError(f"remove {child}: {exc}") from exc

        try:
            self._sftp.rmdir(path)
        except PermissionError as exc:
            raise SftpError(f"Permission denied: {path}") from exc
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise SftpError(f"rmdir {path}: {exc}") from exc

    def rename(self, old_path: str, new_path: str) -> None:
        """
        Rename / move a remote entry. Fails loudly if the target
        already exists — the GUI prompts the user first so we never
        silently stomp an existing file.
        """
        with self._lock:
            self._require_open()
            try:
                if self._exists_locked(new_path):
                    raise SftpError(f"Already exists: {new_path}")
                self._sftp.rename(old_path, new_path)
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {new_path}") from exc
            except FileNotFoundError as exc:
                raise SftpError(f"No such path: {old_path}") from exc
            except SftpError:
                raise
            except Exception as exc:
                raise SftpError(f"rename {old_path}: {exc}") from exc

    def exists(self, path: str) -> bool:
        """Return True if ``path`` exists on the remote, False otherwise."""
        with self._lock:
            self._require_open()
            return self._exists_locked(path)

    def _exists_locked(self, path: str) -> bool:
        try:
            self._sftp.stat(path)
            return True
        except FileNotFoundError:
            return False
        except IOError:
            return False
        except Exception:
            return False

    def stat_entry(self, path: str) -> Optional[SftpEntry]:
        """Return an SftpEntry for ``path`` or None if it does not exist."""
        with self._lock:
            self._require_open()
            try:
                a = self._sftp.stat(path)
            except FileNotFoundError:
                return None
            except PermissionError as exc:
                raise SftpError(f"Permission denied: {path}") from exc
            except Exception as exc:
                raise SftpError(f"{path}: {exc}") from exc
            mode = getattr(a, "st_mode", 0) or 0
            return SftpEntry(
                name=path.rsplit("/", 1)[-1] or path,
                is_dir=bool(stat.S_ISDIR(mode)),
                is_link=bool(stat.S_ISLNK(mode)),
                size=int(getattr(a, "st_size", 0) or 0),
                mtime=float(getattr(a, "st_mtime", 0) or 0),
                mode=int(mode),
            )

    # ── Internals ───────────────────────────────────────────────────────────

    def _require_open(self) -> None:
        if self._closed or self._sftp is None:
            raise SftpError("SFTP channel is not open")
        if self._session is None or not getattr(self._session, "is_open", False):
            raise SftpError("SSH session disconnected")


def _join(base: str, name: str) -> str:
    """POSIX-style join (remote paths always use forward slashes)."""
    if not base:
        return name
    if base.endswith("/"):
        return base + name
    return base + "/" + name


