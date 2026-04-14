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
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

import requests


# ── Model metadata ────────────────────────────────────────────────────────


@dataclass
class ModelInfo:
    """Structured description of one installed Ollama model.

    Populated from ``/api/tags``. Only ``name`` is guaranteed; every
    other field may be empty / zero when Ollama returns a sparse
    response (older daemons, imported GGUFs without sidecar metadata).
    The UI must tolerate missing fields — formatters below fall back
    to sensible placeholders.
    """

    name: str
    size_bytes: int = 0
    digest: str = ""
    modified_at: str = ""
    family: str = ""
    families: list[str] = field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""

    @property
    def size_human(self) -> str:
        """Render ``size_bytes`` as a short human string (e.g. "3.2 GB").

        Returns an empty string when the size is unknown so the UI can
        hide the label instead of showing ``0 B``.
        """
        n = int(self.size_bytes or 0)
        if n <= 0:
            return ""
        units = ("B", "KB", "MB", "GB", "TB")
        val = float(n)
        idx = 0
        while val >= 1024.0 and idx < len(units) - 1:
            val /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(val)} {units[idx]}"
        return f"{val:.1f} {units[idx]}"

    @property
    def category(self) -> str:
        """Loose category for grouping. Purely cosmetic — used to sort
        the dropdown so code-focused models cluster together.

        Matching is substring-based against the tag string, because
        Ollama's metadata doesn't carry a reliable "purpose" field.
        """
        lname = (self.name or "").lower()
        for needle in ("coder", "code", "starcoder", "codellama", "sqlcoder"):
            if needle in lname:
                return "code"
        for needle in ("embed", "bge", "minilm", "nomic"):
            if needle in lname:
                return "embedding"
        for needle in ("vision", "llava", "moondream", "bakllava"):
            if needle in lname:
                return "vision"
        return "chat"

    def display_label(self) -> str:
        """Short label for combo-box rows. Includes size when known."""
        sz = self.size_human
        if sz:
            return f"{self.name}  ·  {sz}"
        return self.name

    @property
    def is_heavy(self) -> bool:
        """True if this model is likely to be slow on modest hardware.

        Threshold is 8 GB on disk — an ad-hoc cutoff but close enough
        to what most laptops can run comfortably without paging.
        """
        return int(self.size_bytes or 0) >= 8 * (1024 ** 3)


def _parse_model_info(raw: dict) -> Optional[ModelInfo]:
    """Best-effort decode of one /api/tags entry.

    Returns ``None`` only when the entry has no usable name — the UI
    can't do anything with a nameless model.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None

    size = raw.get("size")
    size_bytes = int(size) if isinstance(size, (int, float)) else 0

    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    family = ""
    families: list[str] = []
    param_size = ""
    quant = ""
    if details:
        if isinstance(details.get("family"), str):
            family = details["family"]
        if isinstance(details.get("families"), list):
            families = [f for f in details["families"] if isinstance(f, str)]
        if isinstance(details.get("parameter_size"), str):
            param_size = details["parameter_size"]
        if isinstance(details.get("quantization_level"), str):
            quant = details["quantization_level"]

    return ModelInfo(
        name=name,
        size_bytes=size_bytes,
        digest=str(raw.get("digest") or ""),
        modified_at=str(raw.get("modified_at") or ""),
        family=family,
        families=families,
        parameter_size=param_size,
        quantization=quant,
    )


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

    def list_model_info(self) -> list[ModelInfo]:
        """Return rich metadata for every locally installed model.

        This is the authoritative listing call — :meth:`list_models`
        is a thin wrapper that returns just the names. Malformed
        entries are skipped rather than aborting the whole call, so
        one bad line in ``/api/tags`` never hides the good ones.
        """
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
        out: list[ModelInfo] = []
        for m in models:
            info = _parse_model_info(m)
            if info is not None:
                out.append(info)
        return out

    def list_models(self) -> list[str]:
        """Return the list of locally installed model tag names.

        Convenience wrapper around :meth:`list_model_info` for callers
        that only care about names (health checks, ``has_model``).
        """
        return [info.name for info in self.list_model_info()]

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
