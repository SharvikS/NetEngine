"""
Sequential transfer queue + worker for the File Transfer workspace.

The UI submits jobs (UPLOAD_FILE / UPLOAD_TREE / DOWNLOAD_FILE /
DOWNLOAD_TREE) through ``TransferManager.enqueue(...)``. A single
background thread pulls them off the queue one at a time and runs
them against a bound ``ScpTransferEngine``. Every state change
(queued, running, progress, done, failed, cancelled) is published via
Qt signals so the UI can render a live queue table without ever
touching the worker thread.

Design choices
--------------
* **One worker thread, one transfer at a time.** Running two SCP
  transfers simultaneously on the same SSH transport quickly
  saturates the channel and produces a noticeably slower total
  throughput than serialising them — WinSCP uses the same policy
  by default. A sequential queue also means the user can cancel
  the *current* transfer with a single flag, not N flags.

* **Jobs are plain dataclasses.** They are thread-safe precisely
  because they are read-only from the worker's perspective: the
  manager creates a job, pushes it into the queue, and the worker
  pulls it out. Progress state is held separately on the manager
  and published via signals, so the job objects themselves never
  need locks.

* **Cancellation is cooperative.** ``cancel_current()`` sets a flag
  that the SCP engine checks between chunks. In-flight reads on a
  stuck transport will still block — in that case the user can
  disconnect the SSH session, which closes the paramiko channel
  and forces the worker out of its blocking recv().
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from scanner.scp_transfer import (
    ScpTransferEngine, ScpError, ScpCancelled, ScpResult,
)


# ── Public types ──────────────────────────────────────────────────────────

class JobKind(enum.Enum):
    UPLOAD_FILE   = "upload_file"
    UPLOAD_TREE   = "upload_tree"
    DOWNLOAD_FILE = "download_file"
    DOWNLOAD_TREE = "download_tree"


class JobStatus(enum.Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class TransferJob:
    """
    One queued transfer.

    ``source`` and ``destination`` are plain strings; their meaning
    depends on ``kind``:

    * UPLOAD_FILE   — source = local file path,       destination = remote directory
    * UPLOAD_TREE   — source = local directory path,  destination = remote parent dir
    * DOWNLOAD_FILE — source = remote file path,      destination = local directory
    * DOWNLOAD_TREE — source = remote directory path, destination = local parent dir

    ``display_name`` is shown in the queue table so the user can
    distinguish two jobs moving similarly-named files from different
    directories.
    """
    id: int
    kind: JobKind
    source: str
    destination: str
    display_name: str
    size_hint: int = 0            # best-effort, may be 0
    status: JobStatus = JobStatus.QUEUED
    bytes_done: int = 0
    bytes_total: int = 0
    current_file: str = ""
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


# ── Manager ───────────────────────────────────────────────────────────────

class TransferManager(QObject):
    """
    Owns the transfer queue and its worker thread.

    Signals
    -------
    job_enqueued(job_id: int)
        Fired immediately after a job is added to the queue.

    job_started(job_id: int)
        Fired when the worker picks a job up and begins running it.

    job_progress(job_id: int, bytes_done: int, bytes_total: int, current_file: str)
        Fired repeatedly while a transfer is running. The GUI should
        debounce rendering — this signal can fire many times per
        second on fast links.

    job_finished(job_id: int, status: str, message: str)
        Fired exactly once per job when it reaches a terminal state.
        ``status`` is one of ``done`` / ``failed`` / ``cancelled``.

    queue_changed()
        Fired whenever the total job list changes (enqueue, clear,
        or when a job reaches a terminal state). The GUI rebuilds
        the queue table from ``list_jobs()`` on this signal.
    """

    job_enqueued = pyqtSignal(int)
    job_started  = pyqtSignal(int)
    job_progress = pyqtSignal(int, int, int, str)
    job_finished = pyqtSignal(int, str, str)
    queue_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._engine: Optional[ScpTransferEngine] = None
        self._jobs: dict[int, TransferJob] = {}
        self._order: list[int] = []
        self._queue: "Queue[int]" = Queue()
        self._next_id = 1

        self._lock = threading.RLock()
        self._cancel_current = threading.Event()
        self._shutdown = threading.Event()

        self._worker = threading.Thread(
            target=self._worker_main,
            name="netengine-scp-worker",
            daemon=True,
        )
        self._worker.start()

    # ── Public API ──────────────────────────────────────────────────────

    def bind_engine(self, engine: Optional[ScpTransferEngine]) -> None:
        """
        Hand the manager a new SCP engine (or None to unbind).

        The worker reads the engine reference under the lock before
        each job, so a bound engine swap takes effect for the next
        job without restarting the thread.
        """
        with self._lock:
            self._engine = engine

    def enqueue(
        self,
        kind: JobKind,
        source: str,
        destination: str,
        *,
        display_name: str = "",
        size_hint: int = 0,
    ) -> int:
        """Create a TransferJob and append it to the queue. Returns its id."""
        with self._lock:
            jid = self._next_id
            self._next_id += 1
            job = TransferJob(
                id=jid,
                kind=kind,
                source=source,
                destination=destination,
                display_name=display_name or _derive_name(kind, source),
                size_hint=size_hint,
            )
            self._jobs[jid] = job
            self._order.append(jid)
        self._queue.put(jid)
        try:
            self.job_enqueued.emit(jid)
            self.queue_changed.emit()
        except RuntimeError:
            pass
        return jid

    def list_jobs(self) -> list[TransferJob]:
        """Return a snapshot of every job in insertion order."""
        with self._lock:
            return [self._jobs[j] for j in self._order if j in self._jobs]

    def get_job(self, job_id: int) -> Optional[TransferJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_current(self) -> None:
        """Signal the currently-running transfer to stop at the next chunk."""
        self._cancel_current.set()

    def clear_finished(self) -> None:
        """Remove every finished / failed / cancelled entry from the queue."""
        with self._lock:
            remove = [
                jid for jid, job in self._jobs.items()
                if job.status in (
                    JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED,
                )
            ]
            for jid in remove:
                self._jobs.pop(jid, None)
                try:
                    self._order.remove(jid)
                except ValueError:
                    pass
        try:
            self.queue_changed.emit()
        except RuntimeError:
            pass

    def shutdown(self, timeout: float = 2.0) -> None:
        """Tell the worker thread to exit. Idempotent."""
        self._shutdown.set()
        self._cancel_current.set()
        # Poison pill — any number works, the worker just uses it to
        # unblock .get() and then re-checks _shutdown.
        try:
            self._queue.put(-1)
        except Exception:
            pass
        try:
            self._worker.join(timeout=timeout)
        except Exception:
            pass

    # ── Worker ──────────────────────────────────────────────────────────

    def _worker_main(self) -> None:
        while not self._shutdown.is_set():
            try:
                jid = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if self._shutdown.is_set():
                return
            if jid == -1:
                continue

            with self._lock:
                job = self._jobs.get(jid)
                engine = self._engine
            if job is None:
                continue
            if job.status != JobStatus.QUEUED:
                # Already terminal — skipped via clear_finished() race
                # or a duplicate enqueue. Nothing to do.
                continue

            if engine is None:
                self._finish(job, JobStatus.FAILED, "SCP engine not bound")
                continue

            self._cancel_current.clear()
            with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
            try:
                self.job_started.emit(jid)
            except RuntimeError:
                pass

            try:
                self._run_one(engine, job)
            except ScpCancelled as exc:
                self._finish(job, JobStatus.CANCELLED, str(exc) or "Cancelled")
            except ScpError as exc:
                self._finish(job, JobStatus.FAILED, str(exc))
            except Exception as exc:
                self._finish(job, JobStatus.FAILED, f"{exc}")
            else:
                self._finish(
                    job, JobStatus.DONE,
                    f"{job.bytes_done or job.bytes_total} bytes transferred",
                )

    def _run_one(self, engine: ScpTransferEngine, job: TransferJob) -> None:
        """Dispatch a single job to the SCP engine with a progress bridge."""

        def _on_progress(done: int, total: int, label: str) -> None:
            with self._lock:
                job.bytes_done = int(done)
                job.bytes_total = int(total)
                job.current_file = label
            try:
                self.job_progress.emit(job.id, int(done), int(total), label)
            except RuntimeError:
                pass

        cancel = self._cancel_current
        kind = job.kind
        if kind == JobKind.UPLOAD_FILE:
            engine.put_file(
                job.source,
                job.destination,
                on_progress=_on_progress,
                cancel_flag=cancel,
            )
        elif kind == JobKind.UPLOAD_TREE:
            engine.put_tree(
                job.source,
                job.destination,
                on_progress=_on_progress,
                cancel_flag=cancel,
            )
        elif kind == JobKind.DOWNLOAD_FILE:
            engine.get_file(
                job.source,
                job.destination,
                on_progress=_on_progress,
                cancel_flag=cancel,
            )
        elif kind == JobKind.DOWNLOAD_TREE:
            engine.get_tree(
                job.source,
                job.destination,
                on_progress=_on_progress,
                cancel_flag=cancel,
            )
        else:
            raise ScpError(f"Unknown transfer kind: {kind}")

    def _finish(self, job: TransferJob, status: JobStatus, message: str) -> None:
        with self._lock:
            job.status = status
            job.message = message
            job.finished_at = time.time()
        try:
            self.job_finished.emit(job.id, status.value, message)
            self.queue_changed.emit()
        except RuntimeError:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────

def _derive_name(kind: JobKind, source: str) -> str:
    base = source.rstrip("\\/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or source
    arrow = "▲" if kind in (JobKind.UPLOAD_FILE, JobKind.UPLOAD_TREE) else "▼"
    suffix = " (folder)" if kind in (JobKind.UPLOAD_TREE, JobKind.DOWNLOAD_TREE) else ""
    return f"{arrow} {base}{suffix}"
