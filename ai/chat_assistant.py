"""
Chat / help assistant service.

Holds a bounded conversation history and streams responses from the
local Ollama model. Bounded so small local models don't get crushed
by an ever-growing context window after a long help session.

Separated from the command assistant on purpose:

* different system prompt (explanation vs. strict command format)
* different history model (chat keeps context; command is one-shot)
* different temperature budget (chat can be slightly higher)

Future dashboard / scan-result summarization will slot in alongside
this class, reusing ``OllamaClient`` and ``prompts`` without touching
the command path.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional

from ai.model_config import AIConfig
from ai.ollama_client import OllamaClient
from ai.prompts import chat_system


class ChatAssistant:
    #: Max number of stored messages (user + assistant combined).
    #: 20 == 10 exchanges, plenty for an in-app help chat and cheap
    #: enough for a 3B model to handle comfortably.
    MAX_HISTORY_MESSAGES = 20

    def __init__(self, client: OllamaClient, config: AIConfig):
        self._client = client
        self._config = config
        self._history: list[dict] = []

    # ── History management ─────────────────────────────────────────

    def clear(self) -> None:
        self._history.clear()

    def history(self) -> list[dict]:
        """Return a shallow copy of the current history (for tests / UI)."""
        return list(self._history)

    def _trim_history(self) -> None:
        overflow = len(self._history) - self.MAX_HISTORY_MESSAGES
        if overflow > 0:
            # Drop the oldest messages in pairs so we never split a
            # user/assistant exchange across the boundary.
            drop = overflow + (overflow % 2)
            self._history = self._history[drop:]

    def record_exchange(
        self,
        user_message: str,
        assistant_reply: str,
    ) -> None:
        """Commit a finished exchange to history.

        Split out from ``ask_stream`` so a cancelled, failed, or
        empty response does **not** pollute the conversation context.
        The UI calls this only after a successful full response.
        """
        um = (user_message or "").strip()
        ar = (assistant_reply or "").strip()
        if not um or not ar:
            return
        self._history.append({"role": "user", "content": um})
        self._history.append({"role": "assistant", "content": ar})
        self._trim_history()

    # ── Inference ──────────────────────────────────────────────────

    def _build_messages(self, user_message: str) -> list[dict]:
        msgs: list[dict] = [
            {
                "role": "system",
                "content": chat_system(self._config.system_hint),
            },
        ]
        msgs.extend(self._history)
        msgs.append(
            {"role": "user", "content": (user_message or "").strip()},
        )
        return msgs

    def ask_stream(
        self,
        user_message: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """Streaming chat. Yields text chunks.

        Does **not** touch history — call ``record_exchange`` on
        successful completion (typically from the UI worker's
        ``finished`` handler). If ``cancel_check`` is provided it is
        forwarded to the HTTP client so the stream can exit cleanly
        when the user hits Stop or closes the app.
        """
        yield from self._client.chat_stream(
            self._config.model,
            self._build_messages(user_message),
            temperature=float(self._config.temperature),
            max_tokens=int(self._config.max_tokens),
            cancel_check=cancel_check,
        )

    def ask(self, user_message: str) -> str:
        """Non-streaming variant. Useful for tests and simple callers."""
        return self._client.chat(
            self._config.model,
            self._build_messages(user_message),
            temperature=float(self._config.temperature),
            max_tokens=int(self._config.max_tokens),
        )
