"""Transport via the official vendor SDKs, when they are installed.

Some gateways only accept requests made by a recognised SDK and answer anything
else with ``401 unauthorized client detected``. The honest fix is not to forge
headers but to genuinely make the request through the SDK: then the client
really *is* the library it claims to be.

Both SDKs stay optional. :func:`available_for` reports whether the one a
provider needs is importable, and :mod:`vmpc.api.client` falls back to its own
httpx transport when it is not — so the three-dependency baseline still runs.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence

from vmpc.api.events import ApiError, EventKind, StreamEvent, Usage
from vmpc.config import WIRE_ANTHROPIC, Provider

#: Import name per wire format.
_PACKAGE = {WIRE_ANTHROPIC: "anthropic"}
_DEFAULT_PACKAGE = "openai"


def package_for(wire: str) -> str:
    return _PACKAGE.get(wire, _DEFAULT_PACKAGE)


def available_for(wire: str) -> bool:
    """Whether the SDK this wire format would use can be imported."""
    import importlib.util

    return importlib.util.find_spec(package_for(wire)) is not None


def stream_via_sdk(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str = "",
    cancel: Optional[Any] = None,
    api_key: str = "",
) -> Iterator[StreamEvent]:
    """Stream one completion through the vendor SDK.

    Raises :class:`~vmpc.api.events.ApiError` on failure, same as the httpx
    path, so callers do not care which transport ran.
    """
    if provider.wire == WIRE_ANTHROPIC:
        yield from _anthropic(provider, messages, system, cancel, api_key)
    else:
        yield from _openai(provider, messages, system, cancel, api_key)


def _base_url(provider: Provider) -> str:
    """The SDKs append their own path suffix, so hand them the bare base."""
    return provider.base_url.rstrip("/")


def _anthropic(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str,
    cancel: Optional[Any],
    api_key: str,
) -> Iterator[StreamEvent]:
    from anthropic import Anthropic
    from vmpc.api.client import _codex_identity_headers

    client = Anthropic(
        api_key=api_key or "missing",
        base_url=_base_url(provider),
        timeout=provider.timeout,
        default_headers={**_codex_identity_headers(), **provider.extra_headers},
    )

    payload = [
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message.get("role") in ("user", "assistant")
    ]
    kwargs: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": provider.max_tokens,
        "messages": payload,
    }
    if system:
        kwargs["system"] = system
    if provider.reasoning:
        budget = max(1024, min(provider.max_tokens - 1, 4096))
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

    try:
        with client.messages.stream(**kwargs) as stream:
            for event in stream:
                if cancel is not None and cancel.is_set():
                    return
                yield from _from_anthropic_event(event)
    except Exception as exc:  # noqa: BLE001 - normalised below
        raise _as_api_error(exc, provider) from exc


def _from_anthropic_event(event: Any) -> Iterator[StreamEvent]:
    kind = getattr(event, "type", "")
    if kind == "content_block_delta":
        delta = getattr(event, "delta", None)
        text = getattr(delta, "text", None)
        if isinstance(text, str) and text:
            yield StreamEvent.answer(text)
            return
        thinking = getattr(delta, "thinking", None)
        if isinstance(thinking, str) and thinking:
            yield StreamEvent.reasoning(thinking)
        return
    if kind == "message_start":
        usage = getattr(getattr(event, "message", None), "usage", None)
        if usage is not None:
            yield StreamEvent(
                EventKind.USAGE,
                usage=Usage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                ),
            )
        return
    if kind == "message_delta":
        usage = getattr(event, "usage", None)
        if usage is not None:
            yield StreamEvent(
                EventKind.USAGE,
                usage=Usage(output_tokens=int(getattr(usage, "output_tokens", 0) or 0)),
            )
        stop = getattr(getattr(event, "delta", None), "stop_reason", None)
        if stop:
            yield StreamEvent(EventKind.DONE, stop_reason=str(stop))
        return
    if kind == "message_stop":
        yield StreamEvent(EventKind.DONE)


def _openai(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str,
    cancel: Optional[Any],
    api_key: str,
) -> Iterator[StreamEvent]:
    from openai import OpenAI
    from vmpc.api.client import _codex_identity_headers

    client = OpenAI(
        api_key=api_key or "missing",
        base_url=_base_url(provider),
        timeout=provider.timeout,
        default_headers={**_codex_identity_headers(), **provider.extra_headers},
    )

    payload: list[dict[str, str]] = []
    if system:
        payload.append({"role": "system", "content": system})
    payload.extend(
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message.get("role") and "content" in message
    )

    try:
        stream = client.chat.completions.create(
            model=provider.model,
            messages=payload,  # type: ignore[arg-type]
            max_tokens=provider.max_tokens,
            stream=True,
        )
        for chunk in stream:
            if cancel is not None and cancel.is_set():
                return
            yield from _from_openai_chunk(chunk)
    except Exception as exc:  # noqa: BLE001 - normalised below
        raise _as_api_error(exc, provider) from exc


def _from_openai_chunk(chunk: Any) -> Iterator[StreamEvent]:
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        yield StreamEvent(
            EventKind.USAGE,
            usage=Usage(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
        )
    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is not None:
            for key in ("reasoning_content", "reasoning", "thinking"):
                text = getattr(delta, key, None)
                if isinstance(text, str) and text:
                    yield StreamEvent.reasoning(text)
                    break
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                yield StreamEvent.answer(content)
        reason = getattr(choice, "finish_reason", None)
        if reason:
            yield StreamEvent(EventKind.DONE, stop_reason=str(reason))


def _as_api_error(exc: Exception, provider: Provider) -> ApiError:
    """Normalise an SDK exception into our own error type."""
    if isinstance(exc, ApiError):
        return exc

    from vmpc.api.events import hint_for_status

    status = getattr(exc, "status_code", 0) or 0
    message = str(exc)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
        elif isinstance(error, str):
            message = error
    return ApiError(
        message,
        status=int(status),
        hint=hint_for_status(int(status), provider.wire) if status else "",
        endpoint=provider.endpoint(),
    )
