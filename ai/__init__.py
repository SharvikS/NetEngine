"""
Local AI integration for Net Engine.

Everything in this package talks to a locally running Ollama instance
over HTTP at ``http://localhost:11434``. There is no cloud endpoint,
no hosted API, no SaaS dependency — if Ollama is not installed or not
running, the rest of the application continues to work and the AI
panel renders a clear banner explaining how to fix it.

Public surface:

    AIConfig, load_config, save_config   - persistent, local-only config
    OllamaClient + error types           - raw HTTP client
    CommandAssistant, CommandSuggestion  - one-shot command suggestion
    ChatAssistant                        - conversational help
    AIService, AIStatus                  - high-level façade used by the UI

Qt wiring (workers, threads) lives in ``ai.ai_service``. Everything
below that file is Qt-free and reusable from non-UI contexts.
"""

from ai.model_config import AIConfig, load_config, save_config
from ai.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaUnavailable,
    OllamaModelMissing,
)
from ai.command_assistant import CommandAssistant, CommandSuggestion
from ai.chat_assistant import ChatAssistant
from ai.ai_service import AIService, AIStatus

__all__ = [
    "AIConfig", "load_config", "save_config",
    "OllamaClient", "OllamaError", "OllamaUnavailable", "OllamaModelMissing",
    "CommandAssistant", "CommandSuggestion",
    "ChatAssistant",
    "AIService", "AIStatus",
]
