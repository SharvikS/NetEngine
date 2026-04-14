"""
High-level AI service — the single object the UI layer talks to.

Responsibilities:

* Own one ``OllamaClient`` configured from the persisted ``AIConfig``.
* Expose ``command_assistant`` and ``chat_assistant`` as cached fields.
* Provide a non-raising ``status()`` that returns a structured
  ``AIStatus`` (reachable / model installed / remedy text) so the UI
  can render a helpful banner instead of swallowing exceptions.
* Expose QThread-ready workers so the UI never blocks on inference.

This is the first file in the ``ai`` package that imports Qt. Keeping
Qt out of the lower modules means all the "business logic" (client,
assistants, prompts, config) can be tested or reused without a
QApplication instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from ai.chat_assistant import ChatAssistant
from ai.command_assistant import CommandAssistant
from ai.model_config import AIConfig, load_config, save_config
from ai.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaModelMissing,
    OllamaUnavailable,
)


# ── Status value object ────────────────────────────────────────────────────


@dataclass
class AIStatus:
    """Snapshot of AI subsystem health. Never raises; callers render
    ``ok``, ``message`` and ``remedy`` directly into the UI banner."""

    ok: bool
    reachable: bool
    model_installed: bool
    version: str = ""
    message: str = ""
    remedy: str = ""

    @classmethod
    def disabled(cls) -> "AIStatus":
        return cls(
            ok=False, reachable=False, model_installed=False,
            message="Local AI is disabled in settings.",
            remedy="Re-enable it from the AI panel.",
        )


# ── Service façade ─────────────────────────────────────────────────────────


class AIService:
    """Single entry-point the UI uses for everything AI-related.

    Holds cached assistant instances so repeated requests don't
    thrash. ``update_config`` swaps the underlying client + assistants
    atomically, so changing models at runtime is safe.
    """

    def __init__(self, config: Optional[AIConfig] = None):
        self._config = config or load_config()
        self._client, self._cmd, self._chat = self._build(self._config)

    # ── construction helpers ───────────────────────────────────────

    @staticmethod
    def _build(cfg: AIConfig):
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

    def update_config(self, cfg: AIConfig) -> None:
        """Replace the live config and rebuild the client + assistants.

        Persists to disk via ``save_config`` so the new values survive
        an app restart. Previous chat history is dropped because it
        was associated with the old model and may no longer fit the
        new one's context window.
        """
        self._config = cfg
        save_config(cfg)
        self._client, self._cmd, self._chat = self._build(cfg)

    # ── connectivity / health ──────────────────────────────────────

    def status(self) -> AIStatus:
        """Check Ollama + model health. Never raises.

        Returns an ``AIStatus`` whose ``ok`` is True only when both
        the daemon is reachable and the configured model is installed.
        In every other case ``message`` / ``remedy`` carry
        human-readable guidance suitable for the UI banner.
        """
        if not self._config.enabled:
            return AIStatus.disabled()

        # Step 1: is Ollama even up?
        try:
            version = self._client.ping()
        except OllamaUnavailable as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                message=str(exc),
                remedy=(
                    "Install Ollama from https://ollama.com and start it "
                    "(Ollama Desktop on Windows/macOS, or `ollama serve` "
                    "from a terminal on Linux)."
                ),
            )
        except OllamaError as exc:
            return AIStatus(
                ok=False, reachable=False, model_installed=False,
                message=str(exc),
                remedy="Restart the Ollama service and try again.",
            )

        # Step 2: is the configured model installed?
        try:
            installed = self._client.has_model(self._config.model)
        except OllamaUnavailable as exc:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                version=version,
                message=str(exc),
                remedy="Restart the Ollama service.",
            )
        except OllamaError as exc:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                version=version, message=str(exc),
            )

        if not installed:
            return AIStatus(
                ok=False, reachable=True, model_installed=False,
                version=version,
                message=(
                    f"Model '{self._config.model}' is not installed "
                    f"on this machine."
                ),
                remedy=f"Pull it with: ollama pull {self._config.model}",
            )

        return AIStatus(
            ok=True, reachable=True, model_installed=True,
            version=version,
            message=f"Ollama {version}  ·  model {self._config.model}",
        )


# ── Qt worker / threading plumbing ─────────────────────────────────────────


class StreamWorker(QObject):
    """Generic QObject worker around a ``() -> Iterator[str]`` producer.

    Moved onto its own QThread by ``run_stream_worker``. Emits:

        chunk(str)     - each delta from the model as it streams in
        finished(str)  - full accumulated text on successful completion
        failed(str)    - human-readable error (typed exceptions are
                         already translated to nice messages)

    ``cancel()`` can be called from the GUI thread to stop streaming
    on the next chunk boundary. A cancelled worker still emits
    ``finished`` (with whatever text was collected so far) so the UI's
    single "I'm done, unlock the Send button" code path works.
    """

    chunk = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, producer_factory: Callable[[], Iterator[str]]):
        super().__init__()
        self._producer_factory = producer_factory
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @pyqtSlot()
    def run(self) -> None:
        try:
            buf: list[str] = []
            for piece in self._producer_factory():
                if self._cancelled:
                    break
                if piece:
                    buf.append(piece)
                    self.chunk.emit(piece)
            self.finished.emit("".join(buf))
        except OllamaUnavailable as exc:
            self.failed.emit(f"Ollama unavailable: {exc}")
        except OllamaModelMissing as exc:
            self.failed.emit(str(exc))
        except OllamaError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # defensive; unknown failure mode
            self.failed.emit(f"Unexpected AI error: {exc}")


def run_stream_worker(
    parent: QObject,
    worker: StreamWorker,
) -> QThread:
    """Move *worker* onto a fresh QThread, wire lifecycle signals, start.

    The caller should connect ``chunk`` / ``finished`` / ``failed``
    to its UI slots **before** calling this function, otherwise those
    signals may fire before the connections are wired up.

    The returned QThread is parented to *parent* so Qt cleans it up
    when the parent is destroyed — no manual bookkeeping needed.
    """
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
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
        lambda: service.command_assistant.suggest_stream(user_request)
    )


def make_chat_worker(
    service: AIService,
    user_message: str,
) -> StreamWorker:
    return StreamWorker(
        lambda: service.chat_assistant.ask_stream(user_message)
    )
