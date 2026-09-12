"""HTTP transport for both wire formats.

Exposes one generator, :func:`stream_chat`, that yields :class:`StreamEvent`
regardless of which shape the endpoint speaks. Everything vendor-specific —
request body, SSE framing, where reasoning hides — is confined to this module.
"""

from __future__ import annotations

import json
import os
import platform
from typing import Any, Iterator, Optional, Sequence

import httpx

from vmpc.api.events import ApiError, EventKind, StreamEvent, Usage, hint_for_status
from vmpc.config import (
    WIRE_ANTHROPIC,
    WIRE_OPENAI,
    ConfigError,
    Provider,
)


def stream_chat(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str = "",
    cancel: Optional[Any] = None,
) -> Iterator[StreamEvent]:
    """Stream one completion.

    ``messages`` is a list of ``{"role": ..., "content": ...}`` in OpenAI's
    vocabulary; the Anthropic path translates as needed. ``cancel`` is any
    object with ``is_set()`` (a :class:`threading.Event`) — checked between SSE
    frames so an interrupt takes effect without waiting for the response to end.
    """
    problems = provider.validate()
    if problems:
        raise ApiError(
            f"provider {provider.name!r} is not usable: {problems[0]}",
            hint="run /api to fix it",
        )

    try:
        headers = _headers(provider)
    except ConfigError as exc:
        raise ApiError(str(exc), hint="run /api to fix the credential") from exc

    # Prefer the vendor SDK when it is installed. Some gateways only accept
    # requests made by a recognised SDK and reject a hand-rolled one with 401
    # regardless of the key; going through the real library is the honest way
    # to satisfy that, and it also tracks vendor wire changes for free.
    if provider.use_sdk:
        from vmpc.api import sdk

        if sdk.available_for(provider.wire):
            yield from sdk.stream_via_sdk(
                provider,
                messages,
                system=system,
                cancel=cancel,
                api_key=provider.resolve_key(),
            )
            return

    if provider.wire == WIRE_ANTHROPIC:
        body = _anthropic_body(provider, messages, system)
        parse = _parse_anthropic
    else:
        body = _openai_body(provider, messages, system)
        parse = _parse_openai

    url = provider.endpoint()
    timeout = httpx.Timeout(provider.timeout, connect=20.0)

    if provider.wire == WIRE_ANTHROPIC:
        parse_final = _parse_anthropic_final
    else:
        parse_final = _parse_openai_final

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _http_error(response, provider)

                # Not every endpoint honours `stream: true`; plenty of gateways
                # answer with one plain JSON object. Those lines carry no `data:`
                # prefix, so they are buffered until the first real SSE frame
                # proves the response is a stream. Once it is, buffering stops —
                # a long answer is never held in memory.
                buffered: list[str] = []
                saw_sse = False
                produced = False

                for line in response.iter_lines():
                    if cancel is not None and cancel.is_set():
                        return
                    _debug_log(provider, line)
                    event = _sse_payload(line)
                    if event is None:
                        if not saw_sse:
                            buffered.append(line)
                        continue
                    saw_sse = True
                    buffered.clear()
                    if event == "[DONE]":
                        yield StreamEvent(EventKind.DONE)
                        return
                    try:
                        data = json.loads(event)
                    except json.JSONDecodeError:
                        continue
                    for parsed in parse(data):
                        produced = True
                        yield parsed

                if saw_sse:
                    if not produced:
                        raise _unreadable(url, "the stream carried no content")
                    return

                # Non-streaming reply: parse the whole body as one response.
                raw = "\n".join(buffered).strip()
                if not raw:
                    raise _unreadable(url, "the endpoint returned an empty body")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raise _unreadable(
                        url, f"the endpoint returned no SSE and no JSON: {raw[:200]}"
                    ) from None
                if isinstance(data, dict):
                    _raise_body_error(data, url)
                for parsed in parse_final(data if isinstance(data, dict) else {}):
                    produced = True
                    yield parsed
                if not produced:
                    raise _unreadable(
                        url, f"no content in the reply: {raw[:200]}"
                    )
    except httpx.TimeoutException as exc:
        raise ApiError(
            f"request timed out after {provider.timeout:.0f}s",
            hint="raise the timeout, or check that the endpoint is reachable",
            endpoint=url,
        ) from exc
    except httpx.RequestError as exc:
        raise ApiError(
            f"cannot reach {url}: {exc}",
            hint="check the base URL and your network",
            endpoint=url,
        ) from exc


def stream_agent_chat(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str = "",
    cancel: Optional[Any] = None,
) -> Iterator[StreamEvent]:
    """Chat with local file function tools on OpenAI-compatible endpoints.

    A tool turn is intentionally non-streamed: a complete JSON argument object
    is validated before it is allowed to read or write disk. The final answer
    is emitted through the normal event vocabulary. Older gateways that reject
    the standard ``tools`` key keep the ordinary streaming chat path.
    """
    if provider.wire == WIRE_ANTHROPIC:
        try:
            yield from _anthropic_agent_turn(provider, messages, system, cancel)
        except ApiError as exc:
            if exc.status not in (400, 404, 422):
                raise
            yield from stream_chat(provider, messages, system=system, cancel=cancel)
        return
    if provider.wire != WIRE_OPENAI:
        yield from stream_chat(provider, messages, system=system, cancel=cancel)
        return
    try:
        yield from _openai_agent_turn(provider, messages, system, cancel)
    except ApiError as exc:
        if exc.status not in (400, 404, 422):
            raise
        yield from stream_chat(provider, messages, system=system, cancel=cancel)


def _openai_agent_turn(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str,
    cancel: Optional[Any],
) -> Iterator[StreamEvent]:
    from vmpc.file_tools import FileToolError, execute_tool, tool_specs

    problems = provider.validate()
    if problems:
        raise ApiError(f"provider {provider.name!r} is not usable: {problems[0]}")
    try:
        headers = _headers(provider)
        headers["accept"] = "application/json"
    except ConfigError as exc:
        raise ApiError(str(exc), hint="run /api to fix the credential") from exc

    tool_instruction = (
        "You can use local filesystem tools. When the user asks to inspect, find, "
        "search, read or change files, call the appropriate tool instead of guessing. "
        "Only operate on a path the user named or confirmed."
    )
    transcript: list[dict[str, Any]] = []
    transcript.append({
        "role": "system",
        "content": f"{system}\n\n{tool_instruction}" if system else tool_instruction,
    })
    transcript.extend(
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message.get("role") and "content" in message
    )
    url = provider.endpoint()
    timeout = httpx.Timeout(provider.timeout, connect=20.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for _turn in range(200):
                if cancel is not None and cancel.is_set():
                    return
                body: dict[str, Any] = {
                    "model": provider.model,
                    "messages": transcript,
                    "stream": False,
                    "max_tokens": provider.max_tokens,
                    "tools": tool_specs(),
                    "tool_choice": "auto",
                }
                if provider.reasoning:
                    body["reasoning_effort"] = "medium"
                response = client.post(url, headers=headers, json=body)
                if response.status_code >= 400:
                    raise _http_error(response, provider)
                try:
                    payload = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise _unreadable(url, "the tool reply was not JSON") from exc
                if not isinstance(payload, dict):
                    raise _unreadable(url, "the tool reply had an invalid shape")
                _raise_body_error(payload, url)
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    yield StreamEvent(EventKind.USAGE, usage=Usage(
                        input_tokens=int(usage.get("prompt_tokens") or 0),
                        output_tokens=int(usage.get("completion_tokens") or 0),
                    ))
                choices = payload.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    raise _unreadable(url, "the reply had no choices")
                message = choices[0].get("message")
                if not isinstance(message, dict):
                    raise _unreadable(url, "the reply had no message")
                calls = message.get("tool_calls") or []
                if not calls:
                    content = message.get("content")
                    if isinstance(content, list):
                        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                    if isinstance(content, str) and content:
                        yield StreamEvent.answer(content)
                    yield StreamEvent(EventKind.DONE)
                    return

                transcript.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                })
                for index, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") if isinstance(function, dict) else ""
                    raw_args = function.get("arguments") if isinstance(function, dict) else "{}"
                    try:
                        arguments = (
                            raw_args
                            if isinstance(raw_args, dict)
                            else json.loads(raw_args or "{}")
                        )
                        if not isinstance(arguments, dict):
                            raise FileToolError("tool arguments must be an object")
                        result: dict[str, Any] = execute_tool(str(name), arguments)
                    except (json.JSONDecodeError, FileToolError) as exc:
                        result = {"error": str(exc)}
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or f"call_{index}"),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            raise ApiError("the model exceeded the 200-step file-tool limit", endpoint=url)
    except httpx.TimeoutException as exc:
        raise ApiError(f"request timed out after {provider.timeout:.0f}s", endpoint=url) from exc
    except httpx.RequestError as exc:
        raise ApiError(f"cannot reach {url}: {exc}", endpoint=url) from exc


def _anthropic_agent_turn(
    provider: Provider,
    messages: Sequence[dict[str, str]],
    system: str,
    cancel: Optional[Any],
) -> Iterator[StreamEvent]:
    """Run Anthropic's native non-streaming tool loop for local file tools."""
    from vmpc.file_tools import FileToolError, anthropic_tool_specs, execute_tool

    problems = provider.validate()
    if problems:
        raise ApiError(f"provider {provider.name!r} is not usable: {problems[0]}")
    try:
        headers = _headers(provider)
        headers["accept"] = "application/json"
    except ConfigError as exc:
        raise ApiError(str(exc), hint="run /api to fix the credential") from exc

    tool_instruction = (
        "You can use local filesystem tools. When the user asks to inspect, find, "
        "search, read or change files, call the appropriate tool instead of guessing. "
        "Only operate on a path the user named or confirmed."
    )
    transcript: list[dict[str, Any]] = [dict(message) for message in messages]
    url = provider.endpoint()
    timeout = httpx.Timeout(provider.timeout, connect=20.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for _turn in range(200):
                if cancel is not None and cancel.is_set():
                    return
                body: dict[str, Any] = {
                    "model": provider.model,
                    "messages": transcript,
                    "max_tokens": provider.max_tokens,
                    "tools": anthropic_tool_specs(),
                }
                if system:
                    body["system"] = f"{system}\n\n{tool_instruction}"
                else:
                    body["system"] = tool_instruction
                response = client.post(url, headers=headers, json=body)
                if response.status_code >= 400:
                    raise _http_error(response, provider)
                try:
                    payload = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise _unreadable(url, "the tool reply was not JSON") from exc
                if not isinstance(payload, dict):
                    raise _unreadable(url, "the tool reply had an invalid shape")
                _raise_body_error(payload, url)
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    yield StreamEvent(EventKind.USAGE, usage=Usage(
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                    ))
                content = payload.get("content")
                if not isinstance(content, list):
                    raise _unreadable(url, "the tool reply had no content blocks")
                tool_uses = [
                    block for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ]
                text_parts = [
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if text_parts:
                    yield StreamEvent.answer("".join(text_parts))
                if not tool_uses:
                    yield StreamEvent(EventKind.DONE, stop_reason=str(payload.get("stop_reason") or ""))
                    return

                transcript.append({"role": "assistant", "content": content})
                results: list[dict[str, Any]] = []
                for block in tool_uses:
                    name = str(block.get("name") or "")
                    arguments = block.get("input") or {}
                    try:
                        if not isinstance(arguments, dict):
                            raise FileToolError("tool arguments must be an object")
                        result: dict[str, Any] = execute_tool(name, arguments)
                    except (FileToolError, TypeError, ValueError) as exc:
                        result = {"error": str(exc)}
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": str(block.get("id") or ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                transcript.append({"role": "user", "content": results})
            raise ApiError("the model exceeded the 200-step file-tool limit", endpoint=url)
    except httpx.TimeoutException as exc:
        raise ApiError(f"request timed out after {provider.timeout:.0f}s", endpoint=url) from exc
    except httpx.RequestError as exc:
        raise ApiError(f"cannot reach {url}: {exc}", endpoint=url) from exc


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def _headers(provider: Provider) -> dict[str, str]:
    from vmpc import __version__

    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    headers.update(_codex_identity_headers(__version__))
    headers.update(provider.auth_headers())
    if provider.wire == WIRE_ANTHROPIC:
        headers["anthropic-version"] = provider.anthropic_version
    headers.update(provider.extra_headers)
    return headers


def _codex_identity_headers(version: str = "") -> dict[str, str]:
    """Return the Codex-compatible client identity used on every API route."""
    from vmpc import __version__

    codex_originator = os.environ.get("VMPC_CODEX_ORIGINATOR", "codex_cli_rs").strip()
    if not codex_originator:
        codex_originator = "codex_cli_rs"
    codex_version = os.environ.get("VMPC_CODEX_VERSION", "0.147.0").strip() or "0.147.0"
    os_name = platform.system() or "unknown"
    os_version = platform.release() or "unknown"
    architecture = platform.machine() or "unknown"
    implementation = version or __version__
    return {
        "originator": codex_originator,
        "user-agent": (
            f"vmpc/{implementation} {codex_originator}/{codex_version} "
            f"({os_name} {os_version}; {architecture})"
        ),
    }


def _openai_body(
    provider: Provider, messages: Sequence[dict[str, str]], system: str
) -> dict[str, Any]:
    payload: list[dict[str, str]] = []
    if system:
        payload.append({"role": "system", "content": system})
    # Message metadata such as the model label is local UI state, not part of
    # the OpenAI message schema.
    payload.extend(
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message.get("role") and "content" in message
    )
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": payload,
        "stream": True,
        "max_tokens": provider.max_tokens,
    }
    if provider.reasoning:
        # Not universal, but harmless where unsupported: most OpenAI-shaped
        # gateways ignore unknown keys, and the ones that implement reasoning
        # read one of these.
        body["reasoning_effort"] = "medium"
    return body


def _anthropic_body(
    provider: Provider, messages: Sequence[dict[str, str]], system: str
) -> dict[str, Any]:
    # Anthropic takes the system prompt as a top-level field, not a message.
    payload = [
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message.get("role") in ("user", "assistant")
    ]
    body: dict[str, Any] = {
        "model": provider.model,
        "messages": payload,
        "stream": True,
        "max_tokens": provider.max_tokens,
    }
    if system:
        body["system"] = system
    if provider.reasoning:
        budget = max(1024, min(provider.max_tokens - 1, 4096))
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return body


def _http_error(response: httpx.Response, provider: Provider) -> ApiError:
    status = response.status_code
    message = f"{provider.name} rejected the request"
    try:
        detail = response.json()
        if isinstance(detail, dict):
            error = detail.get("error")
            if isinstance(error, dict) and error.get("message"):
                message = str(error["message"])
            elif isinstance(error, str):
                message = error
            elif detail.get("message"):
                message = str(detail["message"])
    except (json.JSONDecodeError, ValueError):
        body = (response.text or "").strip()
        if body:
            message = body[:300]
    return ApiError(
        message,
        status=status,
        hint=hint_for_status(status, provider.wire),
        endpoint=str(response.request.url),
    )


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------


def _sse_payload(line: str) -> Optional[str]:
    """Return the ``data:`` payload of an SSE line, or None for framing lines."""
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        # ``event:`` lines are ignored: both formats repeat the type inside the
        # JSON payload, so the framing line carries nothing extra.
        return None
    return line[5:].strip()


def _unreadable(url: str, detail: str) -> ApiError:
    """A 200 that yielded nothing. Silence here reads as a broken app."""
    return ApiError(
        f"the endpoint accepted the request but sent nothing usable — {detail}",
        hint="check the model name and the API format; VMPC_DEBUG=1 logs the raw reply",
        endpoint=url,
    )


def _raise_body_error(data: dict[str, Any], url: str) -> None:
    """Some gateways report errors with HTTP 200 and an ``error`` field."""
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        raise ApiError(
            str(error["message"]),
            hint=str(error.get("type") or "reported by the endpoint with HTTP 200"),
            endpoint=url,
        )
    if isinstance(error, str) and error:
        raise ApiError(error, endpoint=url)


def _debug_log(provider: Provider, line: str) -> None:
    """Echo raw wire lines to stderr when VMPC_DEBUG is set.

    Diagnosing an endpoint that answers 200 with an unexpected shape is
    guesswork without this, and a live endpoint is the one thing the tests
    cannot stand in for.
    """
    import os
    import sys

    if not os.environ.get("VMPC_DEBUG"):
        return
    sys.stderr.write(f"[vmpc {provider.wire}] {line}\n")
    sys.stderr.flush()


def _parse_openai_final(data: dict[str, Any]) -> Iterator[StreamEvent]:
    """Parse a complete (non-streamed) OpenAI-shaped response."""
    usage = data.get("usage")
    if isinstance(usage, dict):
        yield StreamEvent(
            EventKind.USAGE,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        # `message` is the non-streaming spelling of `delta`.
        message = choice.get("message")
        if isinstance(message, dict):
            for key in ("reasoning_content", "reasoning", "thinking"):
                chunk = message.get(key)
                if isinstance(chunk, str) and chunk:
                    yield StreamEvent.reasoning(chunk)
                    break
            content = message.get("content")
            if isinstance(content, str) and content:
                yield StreamEvent.answer(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        yield StreamEvent.answer(part["text"])
        text = choice.get("text")
        if isinstance(text, str) and text:
            yield StreamEvent.answer(text)
        reason = choice.get("finish_reason")
        if reason:
            yield StreamEvent(EventKind.DONE, stop_reason=str(reason))


def _parse_anthropic_final(data: dict[str, Any]) -> Iterator[StreamEvent]:
    """Parse a complete (non-streamed) Anthropic-shaped response."""
    usage = data.get("usage")
    if isinstance(usage, dict):
        yield StreamEvent(
            EventKind.USAGE,
            usage=Usage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
        )
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            yield StreamEvent.reasoning(block["thinking"])
        elif isinstance(block.get("text"), str) and block["text"]:
            yield StreamEvent.answer(block["text"])
    if data.get("stop_reason"):
        yield StreamEvent(EventKind.DONE, stop_reason=str(data["stop_reason"]))


def _parse_openai(data: dict[str, Any]) -> Iterator[StreamEvent]:
    # An error can arrive inside a frame of an otherwise-200 stream.
    _raise_body_error(data, "")

    usage = data.get("usage")
    if isinstance(usage, dict):
        yield StreamEvent(
            EventKind.USAGE,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )

    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            # Reasoning has no standard key yet; these are the three spellings
            # in the wild (DeepSeek, OpenRouter, vLLM).
            for key in ("reasoning_content", "reasoning", "thinking"):
                chunk = delta.get(key)
                if isinstance(chunk, str) and chunk:
                    yield StreamEvent.reasoning(chunk)
                    break
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield StreamEvent.answer(content)
            elif isinstance(content, list):
                # Some gateways send content as typed parts.
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        yield StreamEvent.answer(part["text"])
        reason = choice.get("finish_reason")
        if reason:
            yield StreamEvent(EventKind.DONE, stop_reason=str(reason))


def _parse_anthropic(data: dict[str, Any]) -> Iterator[StreamEvent]:
    event_type = data.get("type")

    if event_type == "content_block_delta":
        delta = data.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "thinking_delta":
            chunk = delta.get("thinking")
            if isinstance(chunk, str) and chunk:
                yield StreamEvent.reasoning(chunk)
        elif delta_type == "text_delta":
            chunk = delta.get("text")
            if isinstance(chunk, str) and chunk:
                yield StreamEvent.answer(chunk)
        return

    if event_type == "message_start":
        message = data.get("message") or {}
        usage = message.get("usage") or {}
        if usage:
            yield StreamEvent(
                EventKind.USAGE,
                usage=Usage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                ),
            )
        return

    if event_type == "message_delta":
        usage = data.get("usage") or {}
        if usage:
            yield StreamEvent(
                EventKind.USAGE,
                usage=Usage(output_tokens=int(usage.get("output_tokens") or 0)),
            )
        stop = (data.get("delta") or {}).get("stop_reason")
        if stop:
            yield StreamEvent(EventKind.DONE, stop_reason=str(stop))
        return

    if event_type == "message_stop":
        yield StreamEvent(EventKind.DONE)
        return

    if event_type == "error":
        error = data.get("error") or {}
        raise ApiError(
            str(error.get("message") or "the endpoint reported an error"),
            hint=str(error.get("type") or ""),
        )
