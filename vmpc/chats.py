"""Saved conversations, and the model-written names they are filed under.

One JSON file per chat under ``~/.vmpc/chats/``. A directory of small files
rather than one index: appending a chat cannot corrupt the others, a half-written
file loses one conversation instead of all of them, and listing is a directory
scan, which is fast enough for the few hundred chats a person accumulates.

The name is asked of the model itself after the first exchange — a conversation
knows what it is about, and ``2026-08-09T14:23.json`` does not. Two rules keep
that from being a hazard:

- The file name is always an id *we* generated. The model's text is data stored
  inside the JSON, never a path component. See :func:`new_id` and :func:`_safe_id`.
- The reply is clamped to one short line by :func:`sanitize_title` before it is
  shown anywhere, because "reply with a title only" is a request, not a
  guarantee.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from vmpc.config import default_config_path
from vmpc.session import DEFAULT_SYSTEM_PROMPT, Session

#: Longest title we keep. Long enough for a sentence fragment in Cyrillic, short
#: enough to sit in a picker row and a terminal tab without truncation.
TITLE_LIMIT = 60

#: How much of the opening exchange the namer is shown. The first paragraph of
#: each side is what a title comes from; sending the whole reply would pay for
#: tokens that do not change the answer.
_NAMER_EXCERPT = 1200

#: Ids we generate, and the only shape we will read back.
_ID_PATTERN = re.compile(r"\A[0-9]{8}-[0-9]{6}(-[0-9]+)?\Z")


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@dataclass
class ChatRecord:
    """One saved conversation.

    ``system`` is stored alongside the messages so a resumed chat runs under the
    prompt it was held under. Reconstructing it from the current config instead
    would silently rewrite history whenever a mode was edited.
    """

    id: str
    title: str = ""
    created: float = 0.0
    updated: float = 0.0
    provider: str = ""
    model: str = ""
    mode: str = ""
    system: str = DEFAULT_SYSTEM_PROMPT
    messages: list[dict[str, str]] = field(default_factory=list)
    total_tokens: int = 0
    #: Paths attached with ``/context``, not their contents. A chat is a record
    #: of the conversation, and the files are on disk already — storing the text
    #: would make every save write a copy of the tree and every resume show a
    #: snapshot from whenever the folder was named.
    context: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What to show in a list: the title, or a stand-in derived from it."""
        return self.title or fallback_title(self.messages) or self.id

    @property
    def turns(self) -> int:
        return sum(1 for message in self.messages if message.get("role") == "user")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "system": self.system,
            "messages": self.messages,
            "total_tokens": self.total_tokens,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChatRecord":
        messages = [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in raw.get("messages", [])
            if isinstance(item, dict) and item.get("role") and "content" in item
        ]
        return cls(
            id=str(raw.get("id", "")),
            # Sanitized on the way in as well as on the way out: the file may
            # have been edited by hand, and a title with a newline in it would
            # break every list that prints one chat per line.
            title=sanitize_title(str(raw.get("title", ""))),
            created=float(raw.get("created", 0.0) or 0.0),
            updated=float(raw.get("updated", 0.0) or 0.0),
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            mode=str(raw.get("mode", "")),
            system=str(raw.get("system", "") or DEFAULT_SYSTEM_PROMPT),
            messages=messages,
            total_tokens=int(raw.get("total_tokens", 0) or 0),
            # Only strings, and only non-empty ones: this list is handed to the
            # filesystem, and a null or a nested object in a hand-edited file
            # would blow up the first walk rather than the parse.
            context=[
                str(item)
                for item in (raw.get("context") or [])
                if isinstance(item, str) and item.strip()
            ],
        )

    # -- session bridge ----------------------------------------------------

    @classmethod
    def from_session(
        cls,
        session: Session,
        chat_id: str,
        provider_name: str = "",
        model: str = "",
        title: str = "",
        created: float = 0.0,
    ) -> "ChatRecord":
        now = time.time()
        return cls(
            id=chat_id,
            title=sanitize_title(title or getattr(session, "title", "")),
            created=created or now,
            updated=now,
            provider=provider_name,
            model=model,
            mode=getattr(session, "mode", ""),
            system=session.system,
            messages=[dict(message) for message in session.messages],
            total_tokens=session.total_tokens,
            context=[str(path) for path in getattr(session, "context_paths", [])],
        )

    def into_session(self, session: Session) -> Session:
        """Load this chat into ``session``, replacing whatever it held."""
        session.messages[:] = [dict(message) for message in self.messages]
        session.total_tokens = self.total_tokens
        session.system = self.system
        session.mode = self.mode
        session.title = self.title
        session.context_paths[:] = list(self.context)
        # Cleared rather than rebuilt: reading the folders is the caller's call,
        # since a resume from a machine that no longer has them should report
        # that once, not fail inside a dataclass.
        session.context_text = ""
        return session


# --------------------------------------------------------------------------
# Ids and paths
# --------------------------------------------------------------------------


def chats_dir() -> Path:
    """``<config dir>/chats``, so ``$VMPC_HOME`` moves the chats with the keys."""
    return default_config_path().parent / "chats"


def new_id(now: Optional[float] = None, directory: Optional[Path] = None) -> str:
    """A fresh chat id: a local timestamp, with a counter only if it collided.

    Seconds resolution reads well in a directory listing and is unique in
    practice, since a chat is created when someone starts typing. The counter is
    there for the case it is not — two runs started in the same second — because
    silently overwriting the other one's file would be the worse failure.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now else time.time()))
    root = directory or chats_dir()
    if not (root / f"{stamp}.json").exists():
        return stamp
    suffix = 2
    while (root / f"{stamp}-{suffix}.json").exists():
        suffix += 1
    return f"{stamp}-{suffix}"


def _safe_id(chat_id: str) -> str:
    """Return ``chat_id`` if it is one of ours, else raise.

    Every path in this module is built from an id, and an id reaches us from a
    file name, a ``--continue`` argument or a JSON field — none of which we
    wrote. Matching the generated shape rather than blacklisting ``..`` and
    separators means a new attack spelling is rejected by default.
    """
    if not _ID_PATTERN.match(chat_id or ""):
        raise ValueError(f"not a chat id: {chat_id!r}")
    return chat_id


def chat_path(chat_id: str) -> Path:
    return chats_dir() / f"{_safe_id(chat_id)}.json"


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def save_chat(record: ChatRecord) -> Path:
    """Write one chat, atomically. Returns the path written."""
    target = chat_path(record.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    record.updated = time.time()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, target)
    return target


def load_chat(chat_id: str) -> ChatRecord:
    raw = json.loads(chat_path(chat_id).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"chat {chat_id} is not a JSON object")
    record = ChatRecord.from_dict(raw)
    # Trust the file name over the field: the name is what we looked up, and a
    # mismatched id inside would make the next save write to a different file.
    record.id = chat_id
    return record


def list_chats(limit: int = 50) -> list[ChatRecord]:
    """Saved chats, most recently touched first.

    A file that will not parse is skipped rather than raised: one bad chat
    should not make the list unopenable, and the user has no way to fix it from
    inside a traceback.
    """
    root = chats_dir()
    if not root.is_dir():
        return []
    records: list[ChatRecord] = []
    for path in root.glob("*.json"):
        try:
            records.append(load_chat(path.stem))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda record: record.updated, reverse=True)
    return records[:limit] if limit else records


def latest_chat() -> Optional[ChatRecord]:
    records = list_chats(limit=1)
    return records[0] if records else None


def delete_chat(chat_id: str) -> bool:
    try:
        chat_path(chat_id).unlink()
    except (ValueError, OSError):
        return False
    return True


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

NAMER_SYSTEM = (
    "You name conversations. Read the exchange and reply with a title of two to "
    "five words describing what it is about. Write it in the same language the "
    "user used. Reply with the title alone: no quotes, no markdown, no trailing "
    "punctuation, no explanation, no preamble."
)

#: Stripped from the ends of the reply. Models like to quote a title even when
#: told not to, and the quotes are not part of the name.
_WRAPPERS = "\"'`«»“”‘’*_ \t.:;!?—–-"

#: A label the reply sometimes puts in front of the actual title.
_LABEL = re.compile(
    r"\A(title|name|chat|conversation|название|заголовок|имя)\s*[:—-]\s*(?P<rest>.+)\Z",
    re.IGNORECASE,
)


def _candidate_lines(raw: str) -> list[str]:
    """The lines of a reply that could plausibly be the title.

    Dropping lead-ins matters more than it sounds. A namer that answers
    ``Sure! Here is a title:\\n\\n**Rose theme tweaks**`` has given a perfectly
    good name, and taking the first non-empty line would file the chat under
    "Sure! Here is a title" — a wrong name that looks like a bug in the feature
    rather than in the reply.
    """
    lines: list[str] = []
    for line in raw.replace("\r", "").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        # A line ending in a colon introduces what comes next; it is never the
        # name itself. Checked before wrappers are stripped, since the colon is
        # one of them.
        if stripped.rstrip("*_`\"'") .endswith(":"):
            continue
        lines.append(stripped)
    return lines


def sanitize_title(raw: str) -> str:
    """Reduce a model reply to one short plain line, or "" if nothing is left.

    Written as a clamp rather than a validator on purpose. The reply is usually
    the title and occasionally the title wrapped in politeness, and the useful
    part is recoverable in both cases; rejecting the whole thing because it had
    a preamble would throw away a good name over formatting.
    """
    if not raw:
        return ""
    candidates = _candidate_lines(raw)
    if not candidates:
        return ""
    text = re.sub(r"\s+", " ", candidates[0]).strip(_WRAPPERS)
    # Markdown a namer sometimes adds around a heading-shaped answer.
    text = text.lstrip("#").strip(_WRAPPERS)
    labelled = _LABEL.match(text)
    if labelled:
        text = labelled.group("rest").strip(_WRAPPERS)
    if not text:
        return ""
    if len(text) > TITLE_LIMIT:
        # Cut on a word boundary when there is one close to the limit, so a
        # clamped title reads as a phrase rather than as a truncation.
        cut = text[:TITLE_LIMIT]
        space = cut.rfind(" ")
        text = (cut[:space] if space > TITLE_LIMIT * 0.6 else cut).rstrip(_WRAPPERS)
    return text


def fallback_title(messages: list[dict[str, str]]) -> str:
    """A name from the first user message, for when the namer is unavailable.

    Better than the id and worse than the model's answer, which is exactly what
    a fallback should be: an unnamed chat is still findable by what was asked.
    """
    first = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"), ""
    )
    return sanitize_title(first)


def _excerpt(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """The opening exchange, trimmed, as the namer's input."""
    excerpt: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role", "")
        if role not in ("user", "assistant"):
            continue
        excerpt.append(
            {"role": role, "content": (message.get("content") or "")[:_NAMER_EXCERPT]}
        )
        if len(excerpt) == 2:
            break
    return excerpt


def suggest_title(provider: Any, messages: list[dict[str, str]]) -> str:
    """Ask the endpoint to name this conversation. "" if it cannot.

    Never raises. This runs for a cosmetic label, so a namer that 401s, times
    out or returns prose must not be able to take down the turn that triggered
    it — the caller falls back to :func:`fallback_title`.
    """
    excerpt = _excerpt(messages)
    if not excerpt:
        return ""
    try:
        from vmpc.api.client import stream_chat

        # Reasoning off and a small cap: this is a five-word answer, and paying
        # a thinking budget for it would make naming cost more than the turn.
        namer = provider.copy(reasoning=False, max_tokens=32)
        collected: list[str] = []
        for event in stream_chat(namer, excerpt, system=NAMER_SYSTEM):
            if event.text:
                collected.append(event.text)
        return sanitize_title("".join(collected))
    except Exception:  # noqa: BLE001 - a cosmetic feature owes the turn nothing
        return ""


def title_for(provider: Any, messages: list[dict[str, str]]) -> str:
    """The model's name for this chat, or the first-message fallback."""
    return suggest_title(provider, messages) or fallback_title(messages)


__all__ = [
    "TITLE_LIMIT",
    "ChatRecord",
    "chat_path",
    "chats_dir",
    "delete_chat",
    "fallback_title",
    "latest_chat",
    "list_chats",
    "load_chat",
    "new_id",
    "sanitize_title",
    "save_chat",
    "suggest_title",
    "title_for",
]
