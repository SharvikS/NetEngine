"""
Raw HTTP client for a locally running Ollama daemon.

This module has **no Qt imports** and **no app-level state**. It
exists so that the assistants, the service façade, and any future
non-UI consumer (tests, CLI helpers) can all share one well-behaved
client.

Endpoints used (see https://github.com/ollama/ollama/blob/main/docs/api.md):

    GET  /api/version   - is the daemon up?
    GET  /api/tags      - list installed models
    POST /api/chat      - chat completion (sync or streaming)

Error contract:

    OllamaUnavailable   - daemon is not reachable at all (connection
                          refused, timeout on the health check, DNS
                          failure, etc.). Treat as "Ollama is not
                          running — show remedy".
    OllamaModelMissing  - daemon is fine but the requested model is
                          not pulled locally. Treat as "tell the user
                          to run ``ollama pull <model>``".
    OllamaError         - everything else (non-2xx, bad JSON, server
                          error in the middle of streaming). Generic.

The daemon is assumed to live on ``localhost``. Nothing in this file
reaches the public internet.
"""

from __future__ import annotations

import json
from typing import Iterator

import requests


class OllamaError(RuntimeError):
    """Generic failure talking to Ollama."""


class OllamaUnavailable(OllamaError):
    """Ollama daemon is not reachable at all."""


class OllamaModelMissing(OllamaError):
    """The requested model is not pulled locally."""


# Short default for health checks so a missing daemon doesn't stall
# the UI for a full minute. The streaming chat path uses the caller's
# longer timeout instead.
_HEALTH_TIMEOUT = 4


class OllamaClient:
    def __init__(self, base_url: str, timeout: int = 60):
        self._base = base_url.rstrip("/")
        self._timeout = max(1, int(timeout))

    # ── Health checks ───────────────────────────────────────────────

    def ping(self) -> str:
        """Return the running Ollama version string. Raises on failure."""
        try:
            r = requests.get(
                f"{self._base}/api/version", timeout=_HEALTH_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                f"Ollama is not reachable at {self._base}. "
                "Is the daemon running?"
            ) from exc
        except requests.Timeout as exc:
            raise OllamaUnavailable(
                f"Timed out contacting Ollama at {self._base}."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaUnavailable(str(exc)) from exc

        if r.status_code != 200:
            raise OllamaError(
                f"Ollama responded {r.status_code} on /api/version"
            )
        try:
            return str(r.json().get("version", "?"))
        except ValueError:
            return "?"

    def list_models(self) -> list[str]:
        """Return the list of locally installed model tags."""
        try:
            r = requests.get(
                f"{self._base}/api/tags", timeout=_HEALTH_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                f"Ollama is not reachable at {self._base}. "
                "Is the daemon running?"
            ) from exc
        except requests.Timeout as exc:
            raise OllamaUnavailable(
                f"Timed out contacting Ollama at {self._base}."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaUnavailable(str(exc)) from exc

        if r.status_code != 200:
            raise OllamaError(
                f"Ollama responded {r.status_code} on /api/tags"
            )
        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError("Malformed JSON from /api/tags") from exc

        return [m.get("name", "") for m in (data.get("models") or [])]

    def has_model(self, name: str) -> bool:
        """Is *name* installed? Forgiving of missing tag suffixes.

        Ollama tags look like ``llama3.2:3b``. If the caller asks for
        ``llama3.2`` we accept any installed tag that starts with that
        family name, so users don't have to memorise exact tag strings.
        """
        if not name:
            return False
        installed = self.list_models()
        if name in installed:
            return True
        prefix = name + ":"
        return any(m == name or m.startswith(prefix) for m in installed)

    # ── Chat inference ──────────────────────────────────────────────

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """One-shot chat call. Returns the full assistant text."""
        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, stream=False,
        )
        try:
            r = requests.post(
                f"{self._base}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                "Lost connection to Ollama mid-request."
            ) from exc
        except requests.Timeout as exc:
            raise OllamaError(
                f"Ollama did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(str(exc)) from exc

        self._check_response_status(r, model)

        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError("Malformed JSON in /api/chat response") from exc
        return (data.get("message") or {}).get("content", "") or ""

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> Iterator[str]:
        """Streaming chat call. Yields text chunks as they arrive.

        Ollama's streaming protocol is newline-delimited JSON: each
        line is an object with at least a ``message.content`` and a
        ``done`` flag. We yield only the delta text; the caller is
        responsible for accumulating if it needs the full string.
        """
        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, stream=True,
        )
        try:
            r = requests.post(
                f"{self._base}/api/chat",
                json=payload,
                timeout=self._timeout,
                stream=True,
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                "Lost connection to Ollama mid-request."
            ) from exc
        except requests.Timeout as exc:
            raise OllamaError(
                f"Ollama did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(str(exc)) from exc

        self._check_response_status(r, model, close_on_fail=True)

        try:
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    # Ollama occasionally emits a heartbeat-ish line.
                    # Skip anything we can't decode instead of dying.
                    continue
                if chunk.get("error"):
                    raise OllamaError(str(chunk["error"]))
                msg = chunk.get("message") or {}
                content = msg.get("content", "")
                if content:
                    yield content
                if chunk.get("done"):
                    break
        finally:
            try:
                r.close()
            except Exception:
                pass

    # ── internals ───────────────────────────────────────────────────

    @staticmethod
    def _build_payload(
        model: str,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict:
        return {
            "model": model,
            "messages": messages,
            "stream": bool(stream),
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }

    @staticmethod
    def _check_response_status(
        r: "requests.Response",
        model: str,
        *,
        close_on_fail: bool = False,
    ) -> None:
        """Translate HTTP status into our typed exceptions.

        404 on /api/chat means the model isn't installed — surface
        that as ``OllamaModelMissing`` so the UI can show the pull
        command. Everything else becomes a generic ``OllamaError``
        with a short excerpt of the server's response body.
        """
        if r.status_code == 200:
            return
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        if close_on_fail:
            try:
                r.close()
            except Exception:
                pass
        if r.status_code == 404:
            raise OllamaModelMissing(
                f"Model '{model}' is not installed. "
                f"Pull it with: ollama pull {model}"
            )
        raise OllamaError(f"Ollama responded {r.status_code}: {body}")
