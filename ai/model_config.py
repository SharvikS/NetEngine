"""
Local-only AI configuration.

Persisted to ``~/.netscope/settings.json`` via the existing
``utils.settings`` module under the ``ai`` key. Defaults target a
small instruct model so the integration works out of the box as long
as the user has pulled the model with::

    ollama pull llama3.2:3b

No network endpoint other than localhost is ever used. The caller
can swap in a different model at runtime by editing the AI settings
panel or by writing directly to settings.json.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, fields

from utils import settings


_SETTINGS_KEY = "ai"


@dataclass
class AIConfig:
    #: Master on/off. When False the AI panel is still reachable but
    #: renders a disabled banner and refuses to issue requests.
    enabled: bool = True

    #: Where to reach the local Ollama daemon. Only localhost-style
    #: URLs are expected — remote hosts would violate the "fully local"
    #: contract of this integration and should not be configured here.
    base_url: str = "http://localhost:11434"

    #: Primary chat/instruct model. Acts as the *initial* default
    #: when no other selection has been persisted. The assistant view
    #: dropdown overwrites this at runtime via
    #: ``AIService.select_model`` and the result is persisted under
    #: :attr:`last_model`, so on the next run we resume whatever the
    #: user actually picked — not this hard-coded value. Kept
    #: non-empty so a fresh install still has a target to probe
    #: against before the first refresh completes.
    model: str = "llama3.2:3b"

    #: Last model the user explicitly selected from the UI. Empty
    #: means "no explicit choice yet — fall back to :attr:`model`".
    #: Persisted separately so that ``model`` can be treated as the
    #: default-default and ``last_model`` as the user preference.
    last_model: str = ""

    #: Optional second model dedicated to command suggestions. Empty
    #: means "reuse ``model``". Leaving the door open for a future
    #: specialised code/command model (e.g. a 1-2B Coder variant)
    #: without having to refactor the assistant layer.
    command_model: str = ""

    #: Per-request timeout in seconds. Covers the full round trip
    #: including streaming — Ollama can take a while on first token
    #: when the model is cold, so don't set this too low.
    timeout: int = 60

    #: Sampling temperature. Low by default because the primary use
    #: case is correctness-sensitive help, not creative writing.
    temperature: float = 0.3

    #: Upper bound on generated tokens. 512 is plenty for an in-app
    #: help answer or a command suggestion.
    max_tokens: int = 512

    #: Optional free-form text appended to every system prompt. Lets
    #: the user inject project-specific guidance (e.g. "I use WSL
    #: Ubuntu as my default shell") without editing code.
    system_hint: str = ""

    def effective_command_model(self) -> str:
        return self.command_model or self.model

    def effective_model(self) -> str:
        """Return the model we should actually target.

        Prefers the last explicitly-selected model so a reboot
        respects the user's choice. Falls back to :attr:`model`
        (which itself is just the dataclass default on a brand-new
        install) when nothing has been picked yet.
        """
        return (self.last_model or self.model or "").strip()


def load_config() -> AIConfig:
    """Read the persisted config, falling back to dataclass defaults
    for any missing key. Unknown keys in the stored JSON are ignored
    so downgrading the app never crashes on an old settings file."""
    raw = settings.get(_SETTINGS_KEY, {}) or {}
    cfg = AIConfig()
    if not isinstance(raw, dict):
        return cfg
    valid_names = {f.name for f in fields(cfg)}
    for key, value in raw.items():
        if key in valid_names:
            try:
                setattr(cfg, key, value)
            except Exception:
                # Don't let a single bad field block the whole load.
                pass
    return cfg


def save_config(cfg: AIConfig) -> None:
    settings.set_value(_SETTINGS_KEY, asdict(cfg))
