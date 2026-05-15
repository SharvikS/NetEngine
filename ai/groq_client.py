"""
HTTP client for the Groq cloud inference API.

Mirrors OllamaClient's public interface so AIService can swap between
local (Ollama) and cloud (Groq) backends without touching the assistant
or threading layers.

Authentication:  Authorization: Bearer <api_key>
Base URL:        https://api.groq.com/openai/v1
Endpoints used:
    GET  /models            — list available models / validate key
    POST /chat/completions  — chat completion (sync or streaming)

Streaming uses Server-Sent Events:
    data: {"choices":[{"delta":{"content":"..."}}]}\n\n
    data: [DONE]\n\n
"""

from __future__ import annotations

import json
from typing import Callable, Iterator, Optional

import requests

from ai.ollama_client import ModelInfo


# ── Error types ────────────────────────────────────────────────────────────


class GroqError(RuntimeError):
    """Generic failure talking to Groq."""


class GroqUnavailable(GroqError):
    """Groq API is not reachable (network error, DNS failure, etc.)."""


class GroqAuthError(GroqError):
    """Invalid or missing API key — treat as configuration error, not network."""


class GroqTimeout(GroqError):
    """Groq accepted the request but did not respond in time."""


class GroqRateLimit(GroqError):
    """Free-tier rate limit hit; caller may surface a retry hint."""


# ── Constants ──────────────────────────────────────────────────────────────

_BASE_URL = "https://api.groq.com/openai/v1"
_CONNECT_TIMEOUT = 5.0
_HEALTH_READ_TIMEOUT = 10.0

# Fallback model list shown before the first successful /models call.
_FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


# ── Client ─────────────────────────────────────────────────────────────────


class GroqClient:
    """Thin HTTP client for Groq's OpenAI-compatible chat API."""

    def __init__(self, api_key: str, timeout: int = 60):
        self._api_key = (api_key or "").strip()
        self._timeout = max(5, int(timeout))
        self._session: Optional[requests.Session] = None

    # ── session lifecycle ───────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update({
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            })
            self._session = s
        return self._session

    def close(self) -> None:
        """Drop the underlying HTTP session, unblocking any in-flight stream."""
        s = self._session
        self._session = None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # ── Health / model listing ──────────────────────────────────────────

    def ping(self) -> str:
        """Validate the API key via GET /models. Returns 'Groq Cloud' on success."""
        if not self._api_key:
            raise GroqAuthError(
                "No Groq API key configured. "
                "Get a free key at console.groq.com."
            )
        try:
            r = self._get_session().get(
                f"{_BASE_URL}/models",
                timeout=(_CONNECT_TIMEOUT, _HEALTH_READ_TIMEOUT),
            )
        except requests.ConnectionError as exc:
            raise GroqUnavailable(
                "Cannot reach Groq API. Check your internet connection."
            ) from exc
        except requests.Timeout as exc:
            raise GroqTimeout("Timed out contacting Groq API.") from exc
        except requests.RequestException as exc:
            raise GroqUnavailable(str(exc) or "Network error") from exc

        if r.status_code == 401:
            raise GroqAuthError(
                "Invalid Groq API key. Check your key at console.groq.com."
            )
        if r.status_code == 429:
            raise GroqRateLimit("Groq rate limit hit. Try again in a moment.")
        if r.status_code != 200:
            raise GroqError(f"Groq responded {r.status_code} on /models")
        return "Groq Cloud"

    def list_model_info(self) -> list[ModelInfo]:
        """Return available Groq chat models as ModelInfo objects.

        Falls back to a hardcoded list when the API call fails or returns
        no chat models, so the dropdown is never empty.
        """
        if not self._api_key:
            raise GroqAuthError(
                "No Groq API key configured. "
                "Get a free key at console.groq.com."
            )
        try:
            r = self._get_session().get(
                f"{_BASE_URL}/models",
                timeout=(_CONNECT_TIMEOUT, _HEALTH_READ_TIMEOUT),
            )
        except requests.ConnectionError as exc:
            raise GroqUnavailable("Cannot reach Groq API.") from exc
        except requests.Timeout as exc:
            raise GroqTimeout("Timed out listing Groq models.") from exc
        except requests.RequestException as exc:
            raise GroqUnavailable(str(exc)) from exc

        if r.status_code == 401:
            raise GroqAuthError("Invalid Groq API key.")
        if r.status_code != 200:
            return [ModelInfo(name=n) for n in _FALLBACK_MODELS]

        try:
            data = r.json()
        except (ValueError, TypeError):
            return [ModelInfo(name=n) for n in _FALLBACK_MODELS]

        models: list[ModelInfo] = []
        if isinstance(data, dict):
            for entry in (data.get("data") or []):
                if not isinstance(entry, dict):
                    continue
                model_id = entry.get("id") or ""
                if not isinstance(model_id, str) or not model_id:
                    continue
                # Skip non-chat models (embeddings, audio, guard, vision).
                if any(x in model_id for x in ("embed", "whisper", "guard", "vision")):
                    continue
                # Only include models marked active when that field is present.
                if entry.get("active") is False:
                    continue
                models.append(ModelInfo(name=model_id))

        return models if models else [ModelInfo(name=n) for n in _FALLBACK_MODELS]

    def list_models(self) -> list[str]:
        return [info.name for info in self.list_model_info()]

    def has_model(self, name: str) -> bool:
        if not name:
            return False
        return name in self.list_models()

    # ── Chat inference ──────────────────────────────────────────────────

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """One-shot chat call. Returns the full assistant text."""
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        try:
            r = self._get_session().post(
                f"{_BASE_URL}/chat/completions",
                json=payload,
                timeout=(_CONNECT_TIMEOUT, self._timeout),
            )
        except requests.ConnectionError as exc:
            raise GroqUnavailable("Lost connection to Groq.") from exc
        except requests.Timeout as exc:
            raise GroqTimeout(
                f"Groq did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise GroqError(str(exc) or "Network error") from exc

        self._check_status(r, model)

        try:
            data = r.json()
        except (ValueError, TypeError) as exc:
            raise GroqError("Malformed JSON in Groq response") from exc

        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return ""

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """Streaming chat via SSE. Yields text chunks as they arrive.

        Groq emits newline-delimited SSE frames:
            data: {"choices":[{"delta":{"content":"..."},"finish_reason":null}]}
            data: [DONE]
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": True,
        }
        try:
            r = self._get_session().post(
                f"{_BASE_URL}/chat/completions",
                json=payload,
                timeout=(_CONNECT_TIMEOUT, self._timeout),
                stream=True,
            )
        except requests.ConnectionError as exc:
            raise GroqUnavailable("Lost connection to Groq.") from exc
        except requests.Timeout as exc:
            raise GroqTimeout(
                f"Groq did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise GroqError(str(exc) or "Network error") from exc

        self._check_status(r, model, close_on_fail=True)

        try:
            for raw in r.iter_lines(decode_unicode=True):
                if cancel_check is not None and cancel_check():
                    return
                if not raw:
                    continue
                if not raw.startswith("data: "):
                    continue
                payload_str = raw[6:]
                if payload_str.strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(payload_str)
                except (ValueError, TypeError):
                    continue
                if not isinstance(chunk, dict):
                    continue
                choices = chunk.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                first = choices[0]
                if not isinstance(first, dict):
                    continue
                delta = first.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
        except requests.ConnectionError as exc:
            raise GroqUnavailable("Lost connection to Groq mid-stream.") from exc
        except requests.Timeout as exc:
            raise GroqTimeout(
                f"Groq stalled mid-stream (>{self._timeout}s without a chunk)."
            ) from exc
        except requests.RequestException as exc:
            raise GroqError(str(exc) or "Network error mid-stream") from exc
        finally:
            try:
                r.close()
            except Exception:
                pass

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _check_status(
        r: "requests.Response",
        model: str,
        *,
        close_on_fail: bool = False,
    ) -> None:
        if r.status_code == 200:
            return
        body = ""
        try:
            body = r.text[:300]
        except Exception:
            pass
        if close_on_fail:
            try:
                r.close()
            except Exception:
                pass
        if r.status_code == 401:
            raise GroqAuthError(
                "Invalid Groq API key. Check your key at console.groq.com."
            )
        if r.status_code == 429:
            raise GroqRateLimit("Groq rate limit hit. Try again in a moment.")
        if r.status_code == 404:
            raise GroqError(f"Model '{model}' not found on Groq.")
        raise GroqError(f"Groq responded {r.status_code}: {body}")
