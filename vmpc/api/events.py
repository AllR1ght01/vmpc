"""Wire-independent events yielded by the transport.

Both supported wire formats are normalized into this small vocabulary so the
renderer never branches on which provider produced a delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class EventKind(Enum):
    #: A chunk of the model's reasoning channel.
    REASONING = "reasoning"
    #: A chunk of the answer channel.
    ANSWER = "answer"
    #: Token accounting, usually near the end of the response.
    USAGE = "usage"
    #: The response completed normally.
    DONE = "done"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class StreamEvent:
    kind: EventKind
    text: str = ""
    usage: Optional[Usage] = None
    #: Set on DONE when the provider reported why it stopped.
    stop_reason: str = ""

    @classmethod
    def reasoning(cls, text: str) -> "StreamEvent":
        return cls(EventKind.REASONING, text=text)

    @classmethod
    def answer(cls, text: str) -> "StreamEvent":
        return cls(EventKind.ANSWER, text=text)


class ApiError(Exception):
    """A request failed, with enough context to be actionable in the UI.

    ``status`` is the HTTP status when there was one. ``hint`` carries the
    "here is what to check" line, kept separate so the UI can style it as
    secondary text.
    """

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        hint: str = "",
        endpoint: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint
        self.endpoint = endpoint

    def __str__(self) -> str:  # pragma: no cover - display only
        prefix = f"HTTP {self.status}: " if self.status else ""
        return f"{prefix}{self.message}"


def hint_for_status(status: int, wire: str) -> str:
    """Map a status code to the check that usually resolves it."""
    if status in (401, 403):
        return "check the API key and the auth scheme (/api → edit)"
    if status == 404:
        if wire == "anthropic":
            return "base URL should not include /v1 — the client appends /v1/messages"
        return "base URL should end with /v1 for OpenAI-shaped endpoints"
    if status == 400:
        return "the model name is probably not valid for this endpoint"
    if status == 429:
        return "rate limited — wait, or switch provider with /api"
    if status >= 500:
        return "the endpoint failed; retry, or check its status page"
    return ""
