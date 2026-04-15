"""
Track remote files that were opened in a local editor so changes
can be detected and re-uploaded — the WinSCP "remote edit" workflow.

Flow
----
When the user double-clicks a remote file in the File Transfer view:

    1. The file is downloaded to a session temp cache via SCP.
    2. The local temp copy is opened in the user's preferred editor.
    3. The temp path is registered here with ``add()``, stamping its
       original modification time.

A background poller in the GUI calls ``check_for_changes()`` every
few seconds. When the editor saves, the temp file's mtime advances
past the tracked ``ack_mtime`` and the call returns the affected
entries. The GUI surfaces a lightweight notification bar offering
to upload the edits back to the remote host through the normal
transfer queue.

Design notes
------------
* **Pure service class.** No Qt, no SCP, no SFTP — the module takes
  only paths and integers. This keeps it trivial to unit-test and
  safe to reuse from a future automation layer.
* **Thread-safe.** A single ``threading.Lock`` guards the entry
  dict so the GUI's QTimer and any background upload completion
  callback can touch the tracker without stepping on each other.
* **Ack / upload semantics.** Every entry carries three mtimes:
    - ``original_mtime``      — value at registration time
    - ``last_uploaded_mtime`` — value of the most recent successful
                                re-upload (or ``original_mtime`` if
                                no upload has happened yet)
    - ``ack_mtime``           — suppression floor; no prompt is
                                raised until the file's mtime
                                advances past this
  After the user confirms an upload, ``mark_uploaded`` bumps both
  ``last_uploaded_mtime`` and ``ack_mtime`` to the post-upload
  mtime so a subsequent save triggers a fresh prompt.
* **Session scoping.** Each entry carries the owning SSH session
  id so ``remove_session`` can purge everything associated with a
  disconnected or destroyed session in one call. This prevents
  stale entries from lingering across reconnects.

Architecture hook for future sync
---------------------------------
The tracker's data model (``temp_path`` → remote target + mtime)
is the same shape a directory-synchronization engine would need.
A future sync layer can iterate tracker entries alongside a
local↔remote directory diff without changing this module.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrackedEdit:
    """One file being watched for editor changes."""
    temp_path: str           # local temp-cache path handed to the editor
    remote_path: str         # where the file came from on the remote host
    session_id: int          # owning SSH session (id(SshSessionTab))
    original_mtime: float    # mtime at the moment of registration
    last_uploaded_mtime: float  # mtime as of last successful reupload
    ack_mtime: float         # suppression floor for change detection
    basename: str = ""       # short label for UI messages


class RemoteEditTracker:
    """Thread-safe registry of opened-for-edit remote files."""

    def __init__(self) -> None:
        self._entries: dict[str, TrackedEdit] = {}
        self._lock = threading.Lock()

    # ── Lifecycle of individual entries ─────────────────────────────────

    def add(
        self,
        temp_path: str,
        remote_path: str,
        session_id: int,
    ) -> TrackedEdit:
        """
        Begin tracking a temp file. The mtime recorded here is the
        suppression floor — we will only surface a "file changed"
        notification once the editor pushes the mtime past this
        value. Returns the newly-registered entry so the caller can
        stash whatever it needs.
        """
        try:
            mt = float(os.path.getmtime(temp_path))
        except OSError:
            mt = 0.0
        entry = TrackedEdit(
            temp_path=temp_path,
            remote_path=remote_path,
            session_id=int(session_id),
            original_mtime=mt,
            last_uploaded_mtime=mt,
            ack_mtime=mt,
            basename=os.path.basename(temp_path),
        )
        with self._lock:
            self._entries[temp_path] = entry
        return entry

    def remove(self, temp_path: str) -> None:
        """Drop a single entry. Idempotent."""
        with self._lock:
            self._entries.pop(temp_path, None)

    def remove_session(self, session_id: int) -> int:
        """
        Drop every entry associated with a given SSH session id.

        Called when a session tab closes / the session transitions
        out of the CONNECTED state. Returns the number of entries
        that were removed, for logging.
        """
        with self._lock:
            doomed = [
                tp for tp, e in self._entries.items()
                if e.session_id == session_id
            ]
            for tp in doomed:
                del self._entries[tp]
            return len(doomed)

    def clear(self) -> None:
        """Drop everything — called on view shutdown."""
        with self._lock:
            self._entries.clear()

    # ── Observation ─────────────────────────────────────────────────────

    def check_for_changes(self) -> list[TrackedEdit]:
        """
        Scan every tracked entry and return those whose current
        on-disk mtime has advanced past their ``ack_mtime``. Entries
        whose temp file has been deleted from disk are silently
        purged — the editor or OS cleanup has invalidated them.

        Never raises. Callers can drive this from a QTimer and trust
        it to be cheap: ``os.path.getmtime`` is a single stat() call.
        """
        changed: list[TrackedEdit] = []
        with self._lock:
            stale: list[str] = []
            for tp, entry in self._entries.items():
                try:
                    current = float(os.path.getmtime(tp))
                except OSError:
                    stale.append(tp)
                    continue
                if current > entry.ack_mtime:
                    changed.append(entry)
            for tp in stale:
                del self._entries[tp]
        return changed

    def all_entries(self) -> list[TrackedEdit]:
        """Snapshot of every tracked entry, for UI status readouts."""
        with self._lock:
            return list(self._entries.values())

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, temp_path: str) -> Optional[TrackedEdit]:
        with self._lock:
            return self._entries.get(temp_path)

    # ── Ack / upload bookkeeping ────────────────────────────────────────

    def mark_uploaded(self, temp_path: str) -> Optional[TrackedEdit]:
        """
        Called after a successful re-upload. Bumps both the
        last_uploaded and ack mtimes to the file's current mtime so
        the next editor save raises a fresh prompt.
        """
        with self._lock:
            entry = self._entries.get(temp_path)
            if entry is None:
                return None
            try:
                mt = float(os.path.getmtime(temp_path))
            except OSError:
                mt = entry.ack_mtime
            entry.last_uploaded_mtime = mt
            entry.ack_mtime = mt
            return entry

    def acknowledge(self, temp_path: str) -> Optional[TrackedEdit]:
        """
        Mark the current on-disk state as "seen but not uploaded".

        Called when the user dismisses the reupload prompt for a
        particular file. Without this step the prompt would re-fire
        on the next poll because the mtime has not advanced.
        """
        with self._lock:
            entry = self._entries.get(temp_path)
            if entry is None:
                return None
            try:
                entry.ack_mtime = float(os.path.getmtime(temp_path))
            except OSError:
                pass
            return entry
