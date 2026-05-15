"""
High-level AI service — the single object the UI layer talks to.

Responsibilities:

* Own one ``OllamaClient`` configured from the persisted ``AIConfig``.
* Expose ``command_assistant`` and ``chat_assistant`` as cached fields.
* Provide a **non-blocking** health probe. The UI never calls the raw
  blocking ``_probe_status_blocking`` directly — it uses
  ``probe_status_async`` which runs the check on a QThread and
  delivers the result via a signal. This is the single biggest
  reliability fix in this module: the old synchronous ``status()``
  call used to freeze the GUI for up to ~8 s whenever Ollama was slow
  or unreachable (two sequential 4 s health calls), which looked
  indistinguishable from a crash.
* Cache the most recent ``AIStatus`` with a short TTL so repeated
  reads (page switches, re-entry checks) don't re-probe the network.
* Expose a stream worker that owns its cancellation state and can be
  driven from the GUI thread without cross-thread hazards.

This is the first file in the ``ai`` package that imports Qt. Keeping
Qt out of the lower modules means all the "business logic" (client,
assistants, prompts, config) can be tested or reused without a
QApplication instance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from ai.chat_assistant import ChatAssistant
from ai.command_assistant import CommandAssistant
from ai.model_config import AIConfig, load_config, save_config
from ai.model_manager import ModelManager
from ai.ollama_client import (
    ModelInfo,
    OllamaClient,
    OllamaError,
    OllamaModelMissing,
    OllamaTimeout,
    OllamaUnavailable,
)


# ── Status value object ────────────────────────────────────────────────────


@dataclass
class AIStatus:
    """Snapshot of AI subsystem health. Never raises; callers render
    ``ok``, ``message`` and ``remedy`` directly into the UI banner.

    ``state`` is a short machine-readable tag so the UI can pick the
    right banner title without string-matching the message:

        "ok"          — reachable and model installed/available
        "disabled"    — user turned AI off in settings
        "checking"    — a probe is currently running; nothing decided
        "unreachable" — daemon/API not running / connection refused
        "no_key"      — Groq provider selected but API key missing/invalid
        "no_model"    — daemon up but configured model missing
        "timeout"     — daemon took too long to answer
        "error"       — anything else
    """

    ok: bool
    reachable: bool
    model_installed: bool
    state: str = "error"
    version: str = ""
    message: str = ""
    remedy: str = ""

    @classmethod
    def disabled(cls) -> "AIStatus":
        return cls(
            ok=False, reachable=False, model_installed=False,
            state="disabled",
            message="Local AI is disabled in settings.",
            remedy="Re-enable it from the AI panel.",
        )

    @classmethod
    def checking(cls) -> "AIStatus":
        return cls(
            ok=False, reachable=False, model_installed=False,
            state="checking",
            message="Checking local AI…",
            remedy="",
        )


# ── Service façade ─────────────────────────────────────────────────────────


#: Cached-status TTL. If the UI asks for status within this many
#: seconds of the last successful probe it gets the cached value
#: instead of triggering another network round-trip. Short enough
#: that a newly-started daemon is detected on the next interaction,
#: long enough that rapid page switches don't spam /api/version.
_STATUS_CACHE_TTL = 5.0


class AIService:
    """Single entry-point the UI uses for everything AI-related.

    Holds cached assistant instances so repeated requests don't
    thrash. ``update_config`` swaps the underlying client + assistants
    atomically, so changing models at runtime is safe.
    """

    def __init__(self, config: Optional[AIConfig] = None):
        try:
            self._config = config or load_config()
        except Exception:
            # A corrupt settings file must never prevent the rest of
            # the app from starting. Fall back to defaults silently.
            self._config = AIConfig()
        # Resolve "last explicit choice vs. static default" up front
        # so the assistants always target the persisted model, not
        # the out-of-the-box fallback.
        effective = self._config.effective_model()
        if effective and effective != self._config.model:
            self._config = replace(self._config, model=effective)
        self._client, self._cmd, self._chat = self._build(self._config)
        self._last_status: Optional[AIStatus] = None
        self._last_status_ts: float = 0.0
        # Python-side strong refs for the in-flight probe worker /
        # thread. Needed because ``probe_status_async`` returns before
        # the background work completes, and without holding these
        # references the Python wrappers would be GCd the moment the
        # function exits — taking the C++ QObjects with them and
        # dropping the ``result`` signal on the floor. Cleared when
        # the worker's ``result`` signal fires.
        self._probe_worker: Optional[_ProbeWorker] = None
        self._probe_thread: Optional[QThread] = None
        # Centralized model registry. The factory closure returns the
        # *current* client, not the instance at construction time, so
        # a later ``update_config`` rotation is transparently picked
        # up by refresh workers.
        self._model_manager = ModelManager(
            client_factory=lambda: self._client,
            initial_model=self._config.model,
        )
        # Keep ``config.model`` in lockstep when the manager
        # auto-falls-back. We only listen to the fallback signal
        # (not the generic ``current_model_changed``) to avoid a
        # feedback loop with user-initiated ``select_model``
        # which already rebuilds the assistants itself.
        self._model_manager.model_auto_fallback.connect(
            self._on_manager_auto_fallback
        )

    # ── construction helpers ───────────────────────────────────────

    @staticmethod
    def _build(cfg: AIConfig):
        if cfg.provider == "groq":
            from ai.groq_client import GroqClient
            client = GroqClient(api_key=cfg.groq_api_key, timeout=cfg.timeout)
        else:
            client = OllamaClient(base_url=cfg.base_url, timeout=cfg.timeout)
        cmd = CommandAssistant(client, cfg)
        chat = ChatAssistant(client, cfg)
        return client, cmd, chat

    # ── public access ──────────────────────────────────────────────

    @property
    def config(self) -> AIConfig:
        return self._config

    @property
    def client(self) -> OllamaClient:
        return self._client

    @property
    def command_assistant(self) -> CommandAssistant:
        return self._cmd

    @property
    def chat_assistant(self) -> ChatAssistant:
        return self._chat

    @property
    def model_manager(self) -> ModelManager:
        """The central registry of locally installed models. UI
        widgets subscribe to its Qt signals to stay in sync without
        polling."""
        return self._model_manager

    def select_model(self, name: str) -> bool:
        """Set the active Ollama model at runtime.

        This is the high-level entry point the UI calls when the
        user picks a row from the dropdown. It:

        1. Accepts either an exact tag or a family prefix — the
           manager's ``resolve_name`` forgives minor user typos.
        2. **Rejects** names that aren't installed, returning False
           so the UI can snap the dropdown back to the previous
           selection and show a "not installed" notice. This is a
           hard rule: we never let a non-existent model slip into
           ``config.model`` because the next request would then
           fail with a cryptic 404 from Ollama instead of a clean
           "pick another model" message.
        3. Rebuilds the client + assistants by reusing
           :meth:`update_config`, which also closes the previous
           HTTP session. That rotation is what kills any in-flight
           inference on the old model — streaming reads on the old
           session get ``ConnectionError`` which translates into a
           terminal signal on the worker, so the UI unlocks cleanly
           instead of silently mixing tokens from two different
           models.
        4. Persists the choice as ``last_model`` so the next app
           launch resumes on the selected model rather than
           reverting to the dataclass default.
        5. Invalidates the cached status so the very next probe
           re-checks "is this model installed?" against Ollama.
        """
        target = self._model_manager.resolve_name(name)
        if not target:
            # Name not installed. Refuse — the UI's stale-safe
            # layer (``_reselect_current_in_combo``) will revert
            # the dropdown to the last valid choice.
            return False
        if target == self._config.model:
            # Idempotent click; still make sure the manager's
            # ``current`` matches in case it drifted.
            self._model_manager.set_current(target)
            return False

        new_cfg = replace(self._config, model=target, last_model=target)
        self.update_config(new_cfg)
        self._model_manager.set_current(target)
        return True

    def _on_manager_auto_fallback(self, _old: str, new: str) -> None:
        """Slot: manager dropped the current model and picked a
        fallback because the user deleted the selection externally
        (``ollama rm``). Mirror the change into ``config.model``
        and rebuild the assistants so the next request targets the
        new model. Without this sync, ``config.model`` would stay
        stale and every subsequent call would 404 with the old
        name. No loop: ``update_config`` never re-emits
        ``current_model_changed``.
        """
        if not new or new == self._config.model:
            return
        try:
            new_cfg = replace(self._config, model=new, last_model=new)
            self.update_config(new_cfg)
        except Exception:
            # Never let a sync failure destabilize the UI. The
            # banner will show "no_model" on the next probe and
            # the user can pick again.
            pass

    def refresh_models_async(
        self,
        parent: QObject,
        *,
        on_result: Optional[Callable[[list], None]] = None,
        on_failed: Optional[Callable[[str], None]] = None,
    ) -> Optional[QThread]:
        """Kick off an off-thread refresh of the model registry.

        Thin passthrough so UI code doesn't have to reach through
        ``service.model_manager`` just for this one call. Returns
        whatever the manager returns (the spawned thread or None
        when a refresh is already in flight).
        """
        return self._model_manager.refresh_async(
            parent,
            on_result=on_result,
            on_failed=on_failed,
        )

    def update_config(self, cfg: AIConfig) -> None:
        """Replace the live config and rebuild the client + assistants.

        Persists to disk via ``save_config`` so the new values survive
        an app restart. Previous chat history is dropped because it
        was associated with the old model and may no longer fit the
        new one's context window. The cached status is invalidated so
        the next probe re-checks against the new endpoint / model.
        """
        old_client = self._client
        self._config = cfg
        try:
            save_config(cfg)
        except Exception:
            # Persistence failure should never prevent a runtime
            # config swap — the user will just lose the change on
            # next restart, which is better than a crash.
            pass
        self._client, self._cmd, self._chat = self._build(cfg)
        self._last_status = None
        self._last_status_ts = 0.0
        try:
            old_client.close()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Drop the HTTP session. Call from MainWindow.closeEvent.

        Closing the session force-unblocks any streaming read that may
        still be in progress on a worker thread, so the QThread can
        exit ``run()`` cleanly instead of being destroyed by Qt while
        still executing.
        """
        try:
            self._client.close()
        except Exception:
            pass

    # ── status: cached + blocking + async ──────────────────────────

    def cached_status(self) -> Optional[AIStatus]:
        """Return the most recent probed status, or ``None`` if we
        haven't probed yet."""
        return self._last_status

    def status_is_fresh(self) -> bool:
        """True if the cached status is within the TTL window."""
        if self._last_status is None:
            return False
        return (time.monotonic() - self._last_status_ts) < _STATUS_CACHE_TTL

    def status(self) -> AIStatus:
        """Return cached status when fresh, otherwise probe blocking.

        Kept for programmatic callers and tests. **The UI must not
        call this directly** — it should use ``probe_status_async``
        so the GUI thread never stalls on a slow health check.
        """
        if self.status_is_fresh() and self._last_status is not None:
            return self._last_status
        status = self._probe_status_blocking()
        self._last_status = status
        self._last_status_ts = time.monotonic()
        return status

    def _probe_status_blocking(self) -> AIStatus:
        """Probe the active AI backend. Never raises.

        Runs on whatever thread calls it — the UI wraps this in a
        QThread via ``probe_status_async`` so it can't stall the
        event loop.
        """
        if not self._config.enabled:
            return AIStatus.disabled()

        if self._config.provider == "groq":
            return self._probe_groq_blocking()
        return self._probe_ollama_blocking()

    def _probe_groq_blocking(self) -> AIStatus:
        """Probe the Groq cloud backend. Never raises."""
        from ai.groq_client import (
            GroqAuthError, GroqError, GroqRateLimit,
            GroqTimeout, GroqUnavailable,
        )
        try:
            version = self._client.ping()
        except GroqAuthError as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="no_key",
                message=str(exc),
                remedy=(
                    "Get a free API key at console.groq.com, then enter "
                    "it in Settings → AI Assistant."
                ),
            )
        except GroqUnavailable as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="unreachable",
                message=str(exc),
                remedy="Check your internet connection.",
            )
        except GroqTimeout as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="timeout",
                message=str(exc),
                remedy="Check your internet connection and try again.",
            )
        except (GroqRateLimit, GroqError) as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="error",
                message=str(exc),
                remedy="Check your API key and internet connection.",
            )
        except Exception as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="error",
                message=f"Unexpected Groq probe error: {exc}",
                remedy="Restart the app.",
            )

        try:
            models = self._client.list_model_info()
        except Exception:
            models = []

        try:
            self._model_manager.push_models(list(models))
        except Exception:
            pass

        if models:
            installed = any(m.name == self._config.model for m in models)
        else:
            installed = True  # Can't verify without a model list; be optimistic.

        if not installed:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="no_model",
                version=version,
                message=f"Model '{self._config.model}' not found on Groq.",
                remedy="Pick a model from the dropdown on the Assistant page.",
            )

        return AIStatus(
            ok=True, reachable=True, model_installed=True,
            state="ok",
            version=version,
            message=f"Groq Cloud  ·  model {self._config.model}",
        )

    def _probe_ollama_blocking(self) -> AIStatus:
        """Probe the local Ollama daemon. Never raises."""
        # Step 1: is Ollama even up?
        try:
            version = self._client.ping()
        except OllamaUnavailable as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="unreachable",
                message=str(exc),
                remedy=(
                    "Install Ollama from https://ollama.com and start it "
                    "(Ollama Desktop on Windows/macOS, or `ollama serve` "
                    "from a terminal on Linux)."
                ),
            )
        except OllamaTimeout as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="timeout",
                message=str(exc),
                remedy="Check that Ollama isn't stuck loading a model.",
            )
        except OllamaError as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="error",
                message=str(exc),
                remedy="Restart the Ollama service and try again.",
            )
        except Exception as exc:  # pragma: no cover — last-resort catch
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="error",
                message=f"Unexpected AI probe error: {exc}",
                remedy="Restart the app or the Ollama service.",
            )

        # Step 2: fetch the full model list once, push it to the
        # registry, and derive "is the configured model installed?"
        # locally. This collapses two API calls into one and keeps
        # the UI dropdown in lockstep with the probe.
        try:
            models = self._client.list_model_info()
        except OllamaUnavailable as exc:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="unreachable",
                version=version,
                message=str(exc),
                remedy="Restart the Ollama service.",
            )
        except OllamaTimeout as exc:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="timeout",
                version=version,
                message=str(exc),
                remedy="Ollama is responding slowly — try again in a moment.",
            )
        except OllamaError as exc:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="error",
                version=version, message=str(exc),
            )
        except Exception as exc:  # pragma: no cover — last-resort catch
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="error",
                version=version,
                message=f"Unexpected AI probe error: {exc}",
            )

        try:
            self._model_manager.push_models(list(models))
        except Exception:
            pass

        installed = any(
            m.name == self._config.model
            or m.name.startswith(self._config.model + ":")
            for m in models
        )
        if not installed:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                state="no_model",
                version=version,
                message=(
                    f"Model '{self._config.model}' is not installed "
                    f"on this machine."
                ),
                remedy=f"Pull it with: ollama pull {self._config.model}",
            )

        return AIStatus(
            ok=True, reachable=True, model_installed=True,
            state="ok",
            version=version,
            message=f"Ollama {version}  ·  model {self._config.model}",
        )

    def probe_status_async(
        self,
        parent: QObject,
        on_result: Callable[[AIStatus], None],
        *,
        force: bool = False,
    ) -> Optional[QThread]:
        """Kick off a background status probe.

        If a fresh cached status exists and ``force`` is False, the
        callback is invoked synchronously with the cached value and no
        thread is started — ``None`` is returned.

        Otherwise a ``_ProbeWorker`` is moved onto its own QThread.
        The worker's ``result`` signal is connected directly to the
        caller's ``on_result`` slot so PyQt auto-dispatches using the
        callback's owning QObject thread affinity. **``on_result``
        must be a bound method of a QObject living in the GUI thread**
        for the callback to run on the GUI thread — if you pass a
        plain function or closure Qt will execute it on the worker
        thread, which is almost certainly not what you want.

        The cache-update slot is connected separately with no thread
        marshalling because it only touches internal service state.
        The returned thread is owned by *parent* so Qt tears it down
        when the parent is destroyed.
        """
        if not force and self.status_is_fresh() and self._last_status is not None:
            try:
                on_result(self._last_status)
            except Exception:
                pass
            return None

        # Refuse to stack probes. The UI also gates this, but enforcing
        # it here means a misbehaving caller can't corrupt _probe_worker.
        if self._probe_worker is not None:
            return self._probe_thread

        worker = _ProbeWorker(self)
        thread = QThread(parent)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Cache update — runs on whatever thread emits (the worker
        # thread here). Only touches plain Python fields so it's safe.
        worker.result.connect(self._record_status)
        # UI callback — PyQt inspects the bound method's owning
        # QObject and uses a queued connection if it lives on a
        # different thread, so this lands on the GUI thread even
        # though the signal is emitted from the worker thread.
        worker.result.connect(on_result)
        worker.result.connect(self._clear_probe_refs)
        worker.result.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Store Python refs BEFORE start so the C++ objects can't be
        # GCd out from under us by a context switch.
        self._probe_worker = worker
        self._probe_thread = thread
        thread.start()
        return thread

    def _clear_probe_refs(self, _status: AIStatus) -> None:
        """Drop the Python refs to the probe worker/thread.

        Called from the probe worker's own thread via a direct
        connection. Safe because it only assigns attributes on the
        service, and the worker/thread C++ objects are kept alive by
        their ``deleteLater`` wiring until ``thread.finished`` fires.
        """
        self._probe_worker = None
        self._probe_thread = None

    def _record_status(self, status: AIStatus) -> None:
        """Update the cached status + timestamp.

        Called from the probe worker's thread via a direct signal
        connection. Only touches plain Python fields; Python's GIL
        makes the reference assignment safe for the GUI thread to
        read without locking.
        """
        self._last_status = status
        self._last_status_ts = time.monotonic()


# ── Probe worker ───────────────────────────────────────────────────────────


class _ProbeWorker(QObject):
    """Runs ``AIService._probe_status_blocking`` off the GUI thread."""

    result = pyqtSignal(object)  # AIStatus

    def __init__(self, service: AIService):
        super().__init__()
        self._service = service

    @pyqtSlot()
    def run(self) -> None:
        try:
            status = self._service._probe_status_blocking()
        except Exception as exc:  # pragma: no cover — already caught inside
            status = AIStatus(
                ok=False, reachable=False, model_installed=False,
                state="error",
                message=f"Unexpected AI probe error: {exc}",
                remedy="Restart the app or Ollama.",
            )
        self.result.emit(status)


# ── Qt worker / threading plumbing ─────────────────────────────────────────


class StreamWorker(QObject):
    """Generic QObject worker around a ``(cancel_check) -> Iterator[str]`` producer.

    Moved onto its own QThread by ``run_stream_worker``. Emits:

        chunk(str)     - each delta from the model as it streams in
        finished(str)  - full accumulated text on successful completion
        cancelled(str) - partial text collected before Stop was pressed
        failed(str)    - human-readable error (typed exceptions are
                         already translated to nice messages)

    Exactly one of ``finished`` / ``cancelled`` / ``failed`` fires per
    run, which gives the UI a single "unlock the Send button" hook per
    terminal signal. Splitting cancelled out from finished means the
    UI can skip recording a partial exchange into chat history.

    ``cancel()`` can be called from the GUI thread to stop streaming
    on the next chunk boundary — the producer is passed a
    ``lambda: self._cancelled`` so the client-level stream loop can
    also bail out at the HTTP boundary rather than waiting for the
    next token.
    """

    chunk = pyqtSignal(str)
    finished = pyqtSignal(str)
    cancelled = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        producer_factory: Callable[[Callable[[], bool]], Iterator[str]],
    ):
        super().__init__()
        self._producer_factory = producer_factory
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    @pyqtSlot()
    def run(self) -> None:
        gen: Optional[Iterator[str]] = None
        buf: list[str] = []
        try:
            gen = self._producer_factory(self.is_cancelled)
            for piece in gen:
                if self._cancelled:
                    break
                if piece:
                    buf.append(piece)
                    self.chunk.emit(piece)
            if self._cancelled:
                self.cancelled.emit("".join(buf))
            else:
                self.finished.emit("".join(buf))
        except OllamaUnavailable as exc:
            self.failed.emit(f"Ollama unavailable: {exc}")
        except OllamaTimeout as exc:
            self.failed.emit(f"Ollama timeout: {exc}")
        except OllamaModelMissing as exc:
            self.failed.emit(str(exc))
        except OllamaError as exc:
            self.failed.emit(str(exc))
        except RuntimeError as exc:
            # Catches GroqError, GroqUnavailable, GroqAuthError, etc.
            # All AI client errors inherit RuntimeError.
            self.failed.emit(str(exc))
        except Exception as exc:  # defensive; unknown failure mode
            self.failed.emit(f"Unexpected AI error: {exc}")
        finally:
            # Explicitly close the generator so the underlying HTTP
            # response is released immediately even if we bailed out
            # mid-iteration. Relying on gc would delay socket cleanup.
            if gen is not None:
                try:
                    close = getattr(gen, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass


def run_stream_worker(
    parent: QObject,
    worker: StreamWorker,
) -> QThread:
    """Move *worker* onto a fresh QThread, wire lifecycle signals, start.

    The caller should connect ``chunk`` / ``finished`` / ``cancelled``
    / ``failed`` to its UI slots **before** calling this function,
    otherwise those signals may fire before the connections are wired
    up.

    The returned QThread is parented to *parent* so Qt cleans it up
    when the parent is destroyed — no manual bookkeeping needed.
    """
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.cancelled.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


def make_command_worker(
    service: AIService,
    user_request: str,
) -> StreamWorker:
    return StreamWorker(
        lambda cancel: service.command_assistant.suggest_stream(
            user_request, cancel_check=cancel,
        )
    )


def make_chat_worker(
    service: AIService,
    user_message: str,
) -> StreamWorker:
    return StreamWorker(
        lambda cancel: service.chat_assistant.ask_stream(
            user_message, cancel_check=cancel,
        )
    )
