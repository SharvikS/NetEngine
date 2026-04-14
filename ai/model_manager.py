"""
Central registry for locally installed Ollama models.

``ModelManager`` is the single source of truth the UI layer consults
for:

* the list of ``ModelInfo`` objects that Ollama reports as installed,
* which of those is currently active,
* whether a refresh is in flight,
* notifications (Qt signals) whenever any of the above changes.

The class deliberately does **not** own the ``OllamaClient`` — the
``AIService`` owns the client and injects it via a factory callable
so that a config swap (base URL change, rebuild) rotates the client
everywhere at once without the manager holding a stale reference.

Why a dedicated manager instead of bolting this onto ``AIService``:

* Model discovery is its own unit of work: it has its own worker,
  its own error surface, its own cadence, and its own consumers.
  Mixing it into the status probe would have ballooned that method
  and forced the UI to re-derive "what models are available?" from
  the status payload on every refresh.
* Qt signals give the UI a push-based subscription instead of having
  to poll. Multiple widgets (dropdown, banner, future dashboard
  badge) can listen without each owning their own refresh logic.
* Thread affinity is contained here: every mutation is funneled
  through ``_apply_refresh_result`` which runs on the GUI thread via
  a queued signal connection. The caller never has to think about
  which thread owns the ``_models`` list.

Auto-fallback policy:

  If a refresh reveals that the currently selected model is no
  longer installed (user deleted it with ``ollama rm`` externally),
  the manager auto-picks the first available model, emits
  ``current_model_changed``, and emits ``model_auto_fallback`` so
  the UI can surface a notice. If the refresh returns an empty
  list, the current selection is preserved — an empty Ollama may
  be transient (daemon just started) and blowing away the user's
  choice would create noise.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal, pyqtSlot

from ai.ollama_client import (
    ModelInfo,
    OllamaClient,
    OllamaError,
    OllamaTimeout,
    OllamaUnavailable,
)


class ModelManager(QObject):
    """Qt-aware registry of installed Ollama models.

    Typical lifecycle:

    1. ``AIService`` constructs one of these, passes a factory that
       returns the *current* ``OllamaClient`` (which the service
       rotates on config changes).
    2. UI calls ``refresh_async(parent)`` on entry and on user click.
    3. UI connects to ``models_changed`` / ``current_model_changed``
       / ``refresh_failed`` to render state.
    4. UI calls ``select_model(name)`` on dropdown interaction; the
       service intercepts via its own wrapper to also rebuild the
       client + assistants. The UI does not call ``set_current``
       directly because that would bypass persistence.

    Thread safety:

    * Mutations to ``_models`` / ``_current`` only happen on the GUI
      thread. The refresh worker lives on a background ``QThread``
      and communicates results exclusively through ``result`` /
      ``failed`` signals, which Qt queues onto the manager's home
      thread automatically.
    * ``refresh_async`` guards against overlapping probes by
      short-circuiting when a worker is already in flight and
      returning the existing thread reference.
    """

    #: Full list of currently-known models. Emitted after every
    #: successful refresh, even if the list is identical to the
    #: previous one — the UI does a cheap diff on its end.
    models_changed = pyqtSignal(list)

    #: Fired whenever the active model name changes, including when
    #: the auto-fallback kicks in. Carries the new name.
    current_model_changed = pyqtSignal(str)

    #: Fired when a refresh starts. UI uses this to dim the dropdown
    #: and show a spinner on the refresh button.
    refresh_started = pyqtSignal()

    #: Fired when a refresh finishes in any terminal state (success
    #: or failure). Carries a bool — True if the manager now holds a
    #: non-empty model list. UI uses this to re-enable the dropdown.
    refresh_finished = pyqtSignal(bool)

    #: Fired on refresh failure. Carries a human-readable message.
    #: ``models_changed`` is **not** emitted on failure — the UI
    #: keeps whatever list it had so a flaky daemon doesn't wipe the
    #: dropdown mid-session.
    refresh_failed = pyqtSignal(str)

    #: Fired when the current selection was dropped because it's no
    #: longer installed, and the manager has picked a fallback.
    #: Carries (old_name, new_name).
    model_auto_fallback = pyqtSignal(str, str)

    #: Private bridge signal. Any thread may emit this — Qt will
    #: queue the delivery onto the manager's home thread so the
    #: actual list mutation never races GUI-thread readers. Public
    #: callers use :meth:`push_models` instead of emitting directly.
    _push_models_internal = pyqtSignal(list)

    #: Private bridge signal for failures. Same rationale.
    _push_failure_internal = pyqtSignal(str)

    def __init__(
        self,
        client_factory: Callable[[], OllamaClient],
        initial_model: str = "",
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._client_factory = client_factory
        self._models: list[ModelInfo] = []
        self._current: str = (initial_model or "").strip()
        # Incremented on every ``select_model`` or fallback. The
        # service stamps requests with this value so stale responses
        # from before a model switch can be detected and discarded
        # even if the worker's identity check fails for any reason.
        self._generation: int = 0
        self._refresh_worker: Optional[_RefreshWorker] = None
        self._refresh_thread: Optional[QThread] = None
        # Wire the cross-thread bridge signals with explicit queued
        # connections so pushes from worker threads always land on
        # the manager's home thread before touching ``_models``.
        self._push_models_internal.connect(
            self._apply_refresh_result,
            Qt.ConnectionType.QueuedConnection,
        )
        self._push_failure_internal.connect(
            self._apply_refresh_failure,
            Qt.ConnectionType.QueuedConnection,
        )

    # ── thread-safe push API ──────────────────────────────────────

    def push_models(self, models: list) -> None:
        """Thread-safe entry point for external probes (e.g. the
        AI status probe) to feed their freshly discovered model
        list into the registry without racing GUI-thread readers.

        Safe to call from any thread. Delivery is queued to the
        manager's home thread via :attr:`_push_models_internal`.
        """
        self._push_models_internal.emit(list(models) if models else [])

    def push_failure(self, message: str) -> None:
        """Thread-safe failure notification. Same rationale as
        :meth:`push_models`."""
        self._push_failure_internal.emit(message or "Model refresh failed.")

    # ── read-only accessors ────────────────────────────────────────

    @property
    def current(self) -> str:
        return self._current

    @property
    def available(self) -> list[ModelInfo]:
        """Return a copy of the model list so callers can't mutate
        the manager's internal state by side effect."""
        return list(self._models)

    @property
    def generation(self) -> int:
        """Monotonic token that advances whenever the active model
        changes. Compare to a saved value to detect "my request was
        started under a previous model selection, drop the result"."""
        return self._generation

    def is_refreshing(self) -> bool:
        return self._refresh_worker is not None

    def has_model(self, name: str) -> bool:
        if not name:
            return False
        prefix = name + ":"
        for info in self._models:
            if info.name == name or info.name.startswith(prefix):
                return True
        return False

    def find(self, name: str) -> Optional[ModelInfo]:
        if not name:
            return None
        for info in self._models:
            if info.name == name:
                return info
        return None

    # ── mutation ───────────────────────────────────────────────────

    def set_current(self, name: str) -> bool:
        """Mark *name* as the active model.

        Returns True if the active model actually changed. This is
        the low-level setter — the typical entry point is
        :meth:`select_model`, which also validates that the name is
        in the installed list. Service-level wrappers persist the
        selection after calling this.
        """
        clean = (name or "").strip()
        if clean == self._current:
            return False
        self._current = clean
        self._generation += 1
        self.current_model_changed.emit(clean)
        return True

    def select_model(self, name: str) -> bool:
        """Select *name* if it's installed.

        Returns True on change, False if the name is empty, matches
        the current selection, or is not in the known list. Callers
        that want a relaxed match (pull the first installed tag
        starting with *name*) should use :meth:`resolve_name` first.
        """
        if not name:
            return False
        if not self.has_model(name):
            return False
        return self.set_current(name)

    def resolve_name(self, name: str) -> str:
        """Return the installed tag that best matches *name*.

        Accepts either an exact tag ("llama3.2:3b") or a family
        prefix ("llama3.2"). Returns an empty string when nothing
        matches — callers use that to decide whether to fall back to
        the first available model.
        """
        if not name:
            return ""
        prefix = name + ":"
        for info in self._models:
            if info.name == name:
                return info.name
        for info in self._models:
            if info.name.startswith(prefix):
                return info.name
        return ""

    # ── refresh pipeline ───────────────────────────────────────────

    def refresh_async(
        self,
        parent: QObject,
        *,
        on_result: Optional[Callable[[list[ModelInfo]], None]] = None,
        on_failed: Optional[Callable[[str], None]] = None,
    ) -> Optional[QThread]:
        """Kick off a background model-list refresh.

        Returns the spawned ``QThread`` for the caller to track, or
        ``None`` if a refresh is already in flight (in which case
        the existing worker will still emit the usual signals). The
        optional callbacks are connected one-shot so specific call
        sites can react without having to manage signal disconnects
        themselves — the long-lived UI observers use ``models_changed``
        etc. directly.
        """
        if self._refresh_worker is not None:
            return self._refresh_thread

        worker = _RefreshWorker(self._client_factory)
        thread = QThread(parent)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Terminal-state wiring. ``_apply_refresh_result`` runs on
        # the manager's home thread (the GUI thread) thanks to Qt's
        # automatic queued-connection dispatch across threads.
        worker.result.connect(self._apply_refresh_result)
        worker.failed.connect(self._apply_refresh_failure)
        if on_result is not None:
            worker.result.connect(on_result)
        if on_failed is not None:
            worker.failed.connect(on_failed)
        worker.result.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_refresh_refs)

        self._refresh_worker = worker
        self._refresh_thread = thread
        self.refresh_started.emit()
        thread.start()
        return thread

    @pyqtSlot(list)
    def _apply_refresh_result(self, models: list) -> None:
        """Apply a fresh model list returned by the worker.

        Runs on the GUI thread. Handles three ordered concerns:

        1. Replace the stored list and emit ``models_changed`` so
           subscribers re-render.
        2. If the current selection vanished, auto-fall-back to the
           first entry and emit ``model_auto_fallback`` so the UI
           can surface a notice. An empty list is *not* treated as
           "selection gone" — a transiently-empty daemon shouldn't
           blow away the user's choice.
        3. Emit ``refresh_finished`` with the "has models" bool so
           any pending UI spinners can unlock.
        """
        # Defensive copy. The worker emits its own list; we don't
        # trust downstream consumers not to mutate whatever they get.
        self._models = list(models) if isinstance(models, list) else []
        self.models_changed.emit(list(self._models))

        if self._models and self._current and not self.has_model(self._current):
            old = self._current
            new = self._models[0].name
            self._current = new
            self._generation += 1
            self.current_model_changed.emit(new)
            self.model_auto_fallback.emit(old, new)
        elif self._models and not self._current:
            # Fresh install with no saved selection — adopt the
            # first model so the UI has something to target.
            new = self._models[0].name
            self._current = new
            self._generation += 1
            self.current_model_changed.emit(new)

        self.refresh_finished.emit(bool(self._models))

    @pyqtSlot(str)
    def _apply_refresh_failure(self, message: str) -> None:
        """Translate a refresh failure into UI-friendly signals.

        Keeps the previous model list intact — a transient daemon
        outage shouldn't empty the dropdown.
        """
        self.refresh_failed.emit(message or "Model refresh failed.")
        self.refresh_finished.emit(bool(self._models))

    def _clear_refresh_refs(self) -> None:
        self._refresh_worker = None
        self._refresh_thread = None

    def shutdown(self) -> None:
        """Drop any references that might extend the lifetime of a
        worker beyond app close. Actual thread teardown is handled
        by Qt via the ``deleteLater`` wiring."""
        self._refresh_worker = None
        self._refresh_thread = None


# ── Refresh worker ────────────────────────────────────────────────────────


class _RefreshWorker(QObject):
    """Runs ``OllamaClient.list_model_info`` off the GUI thread.

    Emits exactly one of ``result`` / ``failed`` per run. The
    ``client_factory`` callable is invoked inside :meth:`run` — not
    stored as a bound client — so that if ``AIService.update_config``
    rebuilds the client between the worker being scheduled and
    actually running, the refresh targets the fresh client rather
    than a stale one.
    """

    result = pyqtSignal(list)  # list[ModelInfo]
    failed = pyqtSignal(str)

    def __init__(self, client_factory: Callable[[], OllamaClient]):
        super().__init__()
        self._client_factory = client_factory

    @pyqtSlot()
    def run(self) -> None:
        try:
            client = self._client_factory()
            models = client.list_model_info()
        except OllamaUnavailable as exc:
            self.failed.emit(f"Ollama unavailable: {exc}")
            return
        except OllamaTimeout as exc:
            self.failed.emit(f"Ollama timed out: {exc}")
            return
        except OllamaError as exc:
            self.failed.emit(str(exc) or "Ollama error during model refresh.")
            return
        except Exception as exc:  # pragma: no cover — last-resort catch
            self.failed.emit(f"Unexpected model refresh error: {exc}")
            return
        self.result.emit(list(models))
