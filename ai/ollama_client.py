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
    OllamaTimeout       - daemon accepted the request but never
                          produced a response in the allowed window.
    OllamaError         - everything else (non-2xx, bad JSON, server
                          error in the middle of streaming). Generic.

Reliability notes:

* One ``requests.Session`` is reused for the lifetime of the client so
  repeated calls don't pay the TCP + keep-alive handshake every time.
* All network calls use a two-part timeout ``(connect, read)``. The
  connect timeout is small (2 s) because anything talking to
  ``localhost`` either succeeds in milliseconds or is not listening at
  all. The read timeout stays large for chat because first-token
  latency on a cold model can be long.
* Every network exception is translated into one of the typed errors
  above — the UI never has to catch raw ``requests`` exceptions.
* ``close()`` drops the underlying session, which also force-unblocks
  any streaming call currently reading from the socket. This is the
  only reliable way to interrupt a slow inference from another thread.
"""

from __future__ import annotations

import json
from typing import Callable, Iterator, Optional

import requests


class OllamaError(RuntimeError):
    """Generic failure talking to Ollama."""


class OllamaUnavailable(OllamaError):
    """Ollama daemon is not reachable at all."""


class OllamaModelMissing(OllamaError):
    """The requested model is not pulled locally."""


class OllamaTimeout(OllamaError):
    """Ollama accepted the request but did not finish in time."""


# localhost either answers in milliseconds or not at all — a generous
# connect timeout is pointless and just makes the UI feel dead when the
# daemon is actually down.
_CONNECT_TIMEOUT = 2.0
#: Health-check read budget. Short by design: if ``/api/version`` or
#: ``/api/tags`` can't answer in a couple of seconds the daemon is
#: effectively unavailable for UI purposes even if it's technically up.
_HEALTH_READ_TIMEOUT = 3.0


class OllamaClient:
    def __init__(self, base_url: str, timeout: int = 60):
        self._base = (base_url or "").rstrip("/") or "http://localhost:11434"
        # Floor the chat timeout at 5 s — anything shorter virtually
        # guarantees a timeout on the first request against a cold model.
        self._timeout = max(5, int(timeout))
        self._session: Optional[requests.Session] = None

    # ── session lifecycle ───────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        s = self._session
        if s is None:
            s = requests.Session()
            self._session = s
        return s

    def close(self) -> None:
        """Drop the underlying HTTP session.

        Safe to call from any thread. Closing the session unblocks any
        in-flight streaming read on another thread with a
        ``ConnectionError``, which the stream loop translates into
        ``OllamaUnavailable``. This is the only reliable way to stop a
        slow inference during app shutdown.
        """
        s = self._session
        self._session = None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # ── Health checks ───────────────────────────────────────────────

    def _health_get(self, path: str) -> "requests.Response":
        try:
            return self._get_session().get(
                f"{self._base}{path}",
                timeout=(_CONNECT_TIMEOUT, _HEALTH_READ_TIMEOUT),
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
            raise OllamaUnavailable(str(exc) or "Network error") from exc
        except Exception as exc:  # pragma: no cover — last-resort net catch
            raise OllamaUnavailable(str(exc) or "Unknown network error") from exc

    def ping(self) -> str:
        """Return the running Ollama version string. Raises on failure."""
        r = self._health_get("/api/version")
        if r.status_code != 200:
            raise OllamaError(
                f"Ollama responded {r.status_code} on /api/version"
            )
        try:
            data = r.json() or {}
            if not isinstance(data, dict):
                return "?"
            return str(data.get("version", "?"))
        except (ValueError, TypeError):
            return "?"

    def list_models(self) -> list[str]:
        """Return the list of locally installed model tags."""
        r = self._health_get("/api/tags")
        if r.status_code != 200:
            raise OllamaError(
                f"Ollama responded {r.status_code} on /api/tags"
            )
        try:
            data = r.json()
        except (ValueError, TypeError) as exc:
            raise OllamaError("Malformed JSON from /api/tags") from exc
        if not isinstance(data, dict):
            return []
        models = data.get("models") or []
        if not isinstance(models, list):
            return []
        out: list[str] = []
        for m in models:
            if isinstance(m, dict):
                name = m.get("name")
                if isinstance(name, str) and name:
                    out.append(name)
        return out

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
            r = self._get_session().post(
                f"{self._base}/api/chat",
                json=payload,
                timeout=(_CONNECT_TIMEOUT, self._timeout),
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                "Lost connection to Ollama mid-request."
            ) from exc
        except requests.Timeout as exc:
            raise OllamaTimeout(
                f"Ollama did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(str(exc) or "Network error") from exc
        except Exception as exc:  # pragma: no cover — defensive
            raise OllamaError(str(exc) or "Unknown network error") from exc

        self._check_response_status(r, model)

        try:
            data = r.json()
        except (ValueError, TypeError) as exc:
            raise OllamaError("Malformed JSON in /api/chat response") from exc
        if not isinstance(data, dict):
            return ""
        msg = data.get("message")
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        return content if isinstance(content, str) else ""

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """Streaming chat call. Yields text chunks as they arrive.

        Ollama's streaming protocol is newline-delimited JSON: each
        line is an object with at least a ``message.content`` and a
        ``done`` flag. We yield only the delta text; the caller is
        responsible for accumulating if it needs the full string.

        If ``cancel_check`` is supplied it is polled between chunks;
        when it returns True the generator exits cleanly and the
        underlying HTTP response is closed. This gives worker threads
        a fast-path bailout without having to wait for the next token.
        """
        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, stream=True,
        )
        try:
            r = self._get_session().post(
                f"{self._base}/api/chat",
                json=payload,
                timeout=(_CONNECT_TIMEOUT, self._timeout),
                stream=True,
            )
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                "Lost connection to Ollama mid-request."
            ) from exc
        except requests.Timeout as exc:
            raise OllamaTimeout(
                f"Ollama did not respond within {self._timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(str(exc) or "Network error") from exc
        except Exception as exc:  # pragma: no cover — defensive
            raise OllamaError(str(exc) or "Unknown network error") from exc

        self._check_response_status(r, model, close_on_fail=True)

        try:
            for raw in r.iter_lines(decode_unicode=True):
                if cancel_check is not None and cancel_check():
                    return
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except (ValueError, TypeError):
                    # Ollama occasionally emits a heartbeat-ish line.
                    # Skip anything we can't decode instead of dying.
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise OllamaError(str(chunk["error"]))
                msg = chunk.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content:
                        yield content
                if chunk.get("done"):
                    return
        except requests.ConnectionError as exc:
            raise OllamaUnavailable(
                "Lost connection to Ollama mid-stream."
            ) from exc
        except requests.Timeout as exc:
            raise OllamaTimeout(
                f"Ollama stalled mid-stream (>{self._timeout}s without a chunk)."
            ) from exc
        except requests.RequestException as exc:
            raise OllamaError(str(exc) or "Network error mid-stream") from exc
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
