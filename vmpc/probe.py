"""Finding out which models a key can actually reach.

Plenty of gateways do not implement ``GET /models``, or answer it with a
catalogue that has nothing to do with what the key is entitled to. The only
reliable question is the one the endpoint answers honestly: send a one-token
request for a name and see what comes back.

This enumerates the caller's *own* access — it is a diagnostic, not a way past
anyone's gate. It is still someone else's server, so:

- the candidate list is bounded and known up front, and the caller is told how
  many requests it means before any are sent;
- ``max_tokens`` is 1, so a probe costs about as little as a request can;
- concurrency is capped at :data:`DEFAULT_WORKERS`;
- a 401, a 403 or a 429 ends the sweep immediately. Those answers are about the
  key or the rate limit rather than the model name, so continuing would be
  hammering an endpoint that has already said no.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

import httpx

from vmpc.config import WIRE_ANTHROPIC, ConfigError, Provider

#: How many probes are in flight at once. Four is enough to make a 50-name sweep
#: take seconds rather than a minute, and low enough that no gateway would call
#: it abuse.
DEFAULT_WORKERS = 4

#: Per-probe timeout. A model that has not answered a one-token request in this
#: long is not usefully "available" either way.
PROBE_TIMEOUT = 20.0

KIND_AVAILABLE = "available"
#: The endpoint knows the name and will not serve it — or does not know it.
KIND_REJECTED = "rejected"
#: A 4xx whose message never mentions the model. Reported rather than counted as
#: a miss: it usually means the request shape is wrong, not the name.
KIND_UNCLEAR = "unclear"
#: 5xx, a timeout, a dropped connection.
KIND_ERROR = "error"
#: Auth or rate limit. Ends the sweep.
KIND_FATAL = "fatal"
#: Never sent, because the sweep ended first.
KIND_SKIPPED = "skipped"

#: Names worth trying against an OpenAI-shaped gateway, newest first per family.
#: A starting point, not a catalogue — ``/model probe a,b,c`` takes your own list,
#: and anything already saved on the endpoint is tried too.
OPENAI_CANDIDATES: tuple[str, ...] = (
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
    "o3",
    "o4-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-1",
    "claude-3-7-sonnet",
    "claude-3-5-haiku",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v3",
    "deepseek-r1",
    "qwen-max",
    "qwen-plus",
    "qwen3-235b-a22b",
    "qwen2.5-72b-instruct",
    "llama-4-maverick",
    "llama-4-scout",
    "llama-3.3-70b-instruct",
    "mistral-large-latest",
    "mistral-small-latest",
    "codestral-latest",
    "grok-4",
    "grok-3",
    "kimi-k2",
    "glm-4.6",
    "minimax-m2",
)

#: Anthropic-shaped endpoints only ever serve Anthropic names.
ANTHROPIC_CANDIDATES: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-1",
    "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
    "claude-3-haiku-20240307",
)


@dataclass(frozen=True)
class ProbeResult:
    model: str
    kind: str
    status: int = 0
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.kind == KIND_AVAILABLE

    @property
    def stops(self) -> bool:
        """Whether this answer should end the sweep."""
        return self.kind == KIND_FATAL


def candidates(provider: Provider, extra: Sequence[str] = ()) -> list[str]:
    """The names to try: the built-in list, plus whatever this endpoint knows.

    Saved and current models go first. They are the ones most likely to work, so
    trying them first means a sweep that gets rate-limited halfway still answered
    the question that mattered.
    """
    base = (
        ANTHROPIC_CANDIDATES
        if provider.wire == WIRE_ANTHROPIC
        else OPENAI_CANDIDATES
    )
    ordered: list[str] = []
    for name in (
        list(extra)
        + [provider.model]
        + list(getattr(provider, "models", []) or [])
        + list(base)
    ):
        name = (name or "").strip()
        if name and name not in ordered:
            ordered.append(name)
    return ordered


# --------------------------------------------------------------------------
# One probe
# --------------------------------------------------------------------------


def _body(provider: Provider, model: str) -> dict[str, Any]:
    """The smallest request either wire format accepts."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    if provider.wire != WIRE_ANTHROPIC:
        body["stream"] = False
    return body


def _headers(provider: Provider) -> dict[str, str]:
    """The real request headers, minus the streaming Accept.

    Built by the transport's own builder rather than by hand: a probe that
    authenticated differently from a turn would answer a different question than
    the one asked.
    """
    from vmpc.api.client import _headers as real_headers

    headers = real_headers(provider)
    headers["accept"] = "application/json"
    return headers


def _message(payload: Any) -> str:
    """Pull the human-readable complaint out of an error body."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "type", "code"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(error, str) and error:
        return error
    for key in ("message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


#: Words that make a 4xx about the model name rather than about the request.
_ABOUT_THE_MODEL = (
    "model",
    "engine",
    "deployment",
    "not found",
    "does not exist",
    "unsupported",
    "no such",
    "unavailable",
    "нет",
    "модел",
)


def _classify(response: httpx.Response, model: str) -> ProbeResult:
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = _message(payload) or (response.text or "").strip()[:120]

    if status < 300:
        # Some gateways answer 200 and put the refusal in the body, with no
        # content anywhere in it.
        empty = isinstance(payload, dict) and not (
            payload.get("choices") or payload.get("content")
        )
        if empty and _message(payload):
            return ProbeResult(model, KIND_REJECTED, status, detail)
        return ProbeResult(model, KIND_AVAILABLE, status)
    if status in (401, 403):
        return ProbeResult(model, KIND_FATAL, status, detail or "the key was rejected")
    if status == 429:
        return ProbeResult(model, KIND_FATAL, status, detail or "rate limited")
    if status >= 500:
        return ProbeResult(model, KIND_ERROR, status, detail)
    lowered = detail.lower()
    if any(word in lowered for word in _ABOUT_THE_MODEL):
        return ProbeResult(model, KIND_REJECTED, status, detail)
    # A 400 that complains about something else — max_tokens, a missing field —
    # says nothing about the name, and calling it a miss would be a lie.
    return ProbeResult(model, KIND_UNCLEAR, status, detail)


def probe_one(
    provider: Provider,
    model: str,
    client: Optional[httpx.Client] = None,
    cancel: Optional[threading.Event] = None,
) -> ProbeResult:
    """Send one minimal request. Never raises."""
    if cancel is not None and cancel.is_set():
        return ProbeResult(model, KIND_SKIPPED)
    try:
        headers = _headers(provider)
    except ConfigError as exc:
        return ProbeResult(model, KIND_FATAL, 0, str(exc))

    url = provider.endpoint()
    body = _body(provider, model)
    owned = client is None
    handle = client or httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=True)
    try:
        response = handle.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        return ProbeResult(model, KIND_ERROR, 0, "timed out")
    except httpx.RequestError as exc:
        return ProbeResult(model, KIND_ERROR, 0, str(exc)[:120])
    finally:
        if owned:
            handle.close()
    return _classify(response, model)


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def probe(
    provider: Provider,
    models: Sequence[str],
    workers: int = DEFAULT_WORKERS,
    cancel: Optional[threading.Event] = None,
) -> Iterator[ProbeResult]:
    """Probe each name, yielding results in the order they were asked for.

    In order rather than as they complete: the caller prints these into
    scrollback, and a list that jumps around is harder to read than one that
    arrives a beat later. The sweep still runs concurrently — only the yielding
    is sequential.

    A fatal answer stops it. Everything queued behind that point comes back as
    :data:`KIND_SKIPPED` rather than being silently dropped, so the caller can
    say how much of the list was actually covered.
    """
    names = [name for name in models if name]
    if not names:
        return
    stop = cancel if cancel is not None else threading.Event()
    limit = max(1, min(int(workers), 8))

    with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=limit) as pool:
            futures = [
                pool.submit(probe_one, provider, name, client, stop) for name in names
            ]
            for future in futures:
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - a probe owes nothing
                    yield ProbeResult("?", KIND_ERROR, 0, str(exc)[:120])
                    continue
                yield result
                if result.stops:
                    # Set before draining the rest: probes not yet started see
                    # it and return immediately instead of adding to the load.
                    stop.set()


__all__ = [
    "ANTHROPIC_CANDIDATES",
    "DEFAULT_WORKERS",
    "KIND_AVAILABLE",
    "KIND_ERROR",
    "KIND_FATAL",
    "KIND_REJECTED",
    "KIND_SKIPPED",
    "KIND_UNCLEAR",
    "OPENAI_CANDIDATES",
    "ProbeResult",
    "candidates",
    "probe",
    "probe_one",
]
