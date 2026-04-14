"""
Command-assistant service.

Takes a natural-language request ("how do I list open ports?"), asks
the local Ollama model for exactly one command in a strict labeled
format, and parses the result into a ``CommandSuggestion`` with three
fields the UI can render independently:

    command     - single-line shell command (no backticks)
    explanation - short description of what it does
    caution     - short warning if any, else empty

The parser is deliberately lenient: if a small model wanders off-format
(extra blank lines, stray backticks, lowercase labels) we still pull
out what we can. The raw response is always preserved on the object so
the UI can fall back to showing it verbatim when parsing is clearly
insufficient.

Safety: this module **never** executes commands. It only produces
text for the user to review and copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from ai.model_config import AIConfig
from ai.ollama_client import OllamaClient
from ai.prompts import command_system


@dataclass
class CommandSuggestion:
    command: str = ""
    explanation: str = ""
    caution: str = ""
    raw: str = ""  # original model output, for fallback display / debugging

    @property
    def has_command(self) -> bool:
        """True if the suggestion has a non-empty, non-sentinel command."""
        c = (self.command or "").strip().strip("`").strip()
        return bool(c) and c.lower() not in ("(none)", "none")


# Match "LABEL: value" lines. Labels are case-insensitive because
# small models sometimes forget to uppercase.
_LABEL_RE = re.compile(
    r"^\s*(COMMAND|EXPLAIN|CAUTION)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def parse_command_response(text: str) -> CommandSuggestion:
    """Parse the labeled three-line format produced by COMMAND_SYSTEM.

    Continuation lines (unlabeled lines that follow an EXPLAIN label)
    are folded into the explanation — this is the single most common
    "wandered off-format" case and salvaging it produces noticeably
    better UX than refusing to parse.
    """
    command = ""
    explain_parts: list[str] = []
    caution = ""
    current: str | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        m = _LABEL_RE.match(line)
        if m:
            label = m.group(1).upper()
            value = m.group(2).strip()
            if label == "COMMAND":
                # Strip any accidentally-emitted code fences / backticks.
                command = value.strip("`").strip()
                current = "command"
            elif label == "EXPLAIN":
                explain_parts = [value] if value else []
                current = "explain"
            elif label == "CAUTION":
                caution = value
                current = "caution"
            continue
        # Continuation for the current section.
        stripped = line.strip()
        if current == "explain" and stripped:
            explain_parts.append(stripped)

    explanation = " ".join(explain_parts).strip()
    if caution.strip().lower() in ("", "none", "n/a", "na"):
        caution = ""
    return CommandSuggestion(
        command=command,
        explanation=explanation,
        caution=caution,
        raw=text or "",
    )


class CommandAssistant:
    """Thin service wrapper around an ``OllamaClient``.

    Stateless (beyond the injected client + config) so it's safe to
    call from background threads — each request builds its own
    message list and returns immediately.
    """

    def __init__(self, client: OllamaClient, config: AIConfig):
        self._client = client
        self._config = config

    def _messages(self, user_request: str) -> list[dict]:
        return [
            {
                "role": "system",
                "content": command_system(self._config.system_hint),
            },
            {
                "role": "user",
                "content": (user_request or "").strip(),
            },
        ]

    def _temperature(self) -> float:
        # Cap temperature for command work — we want the model to pick
        # the obvious right answer, not be creative.
        return min(0.3, float(self._config.temperature))

    def _max_tokens(self) -> int:
        # A command suggestion is never long; keep the budget small so
        # a misbehaving model can't hang the UI for thousands of tokens.
        return min(256, int(self._config.max_tokens))

    def suggest(self, user_request: str) -> CommandSuggestion:
        """Synchronous one-shot suggestion. Raises ``OllamaError`` on failure."""
        text = self._client.chat(
            self._config.effective_command_model(),
            self._messages(user_request),
            temperature=self._temperature(),
            max_tokens=self._max_tokens(),
        )
        return parse_command_response(text)

    def suggest_stream(
        self,
        user_request: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """Streaming variant — yields raw chunks.

        The caller is responsible for accumulating the full text and
        then running it through ``parse_command_response`` to build
        the final ``CommandSuggestion``. Done this way because a
        command response is short enough that incremental parsing
        (re-parsing every chunk) is wasteful.
        """
        yield from self._client.chat_stream(
            self._config.effective_command_model(),
            self._messages(user_request),
            temperature=self._temperature(),
            max_tokens=self._max_tokens(),
            cancel_check=cancel_check,
        )
