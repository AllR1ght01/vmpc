"""Conversation state for one run.

Deliberately thin: a message list, a token tally, and the system prompt. There is
no summarization or context-window management here yet — when that arrives it
belongs behind this class's interface, not spread through the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SYSTEM_PROMPT = (
    "You are vmpc, a terminal assistant. Answer in the user's language. "
    "Prefer short, concrete answers. Use markdown for structure and fenced code "
    "blocks with a language tag for code."
)


@dataclass
class Session:
    system: str = DEFAULT_SYSTEM_PROMPT
    messages: list[dict[str, str]] = field(default_factory=list)
    total_tokens: int = 0
    #: Name of the active mode, for display. The prompt it contributed is
    #: already folded into :attr:`system`; this is what ``/status`` and the
    #: banner show, so a rewritten prompt is never invisible.
    mode: str = ""
    #: What this conversation is about, written by the model after the first
    #: exchange. Conversation content rather than storage bookkeeping, so it
    #: lives here and is cleared by :meth:`reset` — the chat id, which names a
    #: file, is the caller's business.
    title: str = ""
    #: Folders and files attached with ``/context``. Only the paths are state;
    #: the text below is a cache, re-read from disk whenever the session is
    #: resumed, so an attachment cannot go stale and a chat file cannot grow to
    #: hold a copy of someone's repository.
    context_paths: list[str] = field(default_factory=list)
    context_text: str = ""

    def full_system(self) -> str:
        """The system prompt as it goes on the wire, attachments included.

        Kept out of :attr:`system` on purpose. ``system`` is the prompt the user
        chose and is what a chat stores; the attachment block is derived from
        files that may have changed since, and folding the two together would
        persist a snapshot of the tree into every saved conversation.
        """
        if not self.context_text:
            return self.system
        return f"{self.system}\n\n{self.context_text}"

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str, model: str = "") -> None:
        message = {"role": "assistant", "content": content}
        # Display-only metadata survives in the saved chat, but is stripped by
        # the transport before the message is sent to an endpoint.
        if model.strip():
            message["model"] = model.strip()
        self.messages.append(message)

    def drop_last_user(self) -> None:
        """Undo the last user turn, for an interrupted or failed request.

        Without this an interrupted turn would leave a user message with no
        assistant reply, and every later request would resend it.
        """
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def set_mode(self, mode: object) -> None:
        """Adopt a :class:`~vmpc.modes.Mode`, or ``None`` for the default.

        The mode's text is *appended* to the default prompt rather than
        replacing it. Someone writing a two-line mode should not have to restate
        that answers use markdown and follow the user's language — and a mode
        that silently dropped those would look like a renderer bug rather than a
        prompt change.
        """
        prompt = str(getattr(mode, "prompt", "") or "").strip()
        self.mode = str(getattr(mode, "name", "") or "")
        self.system = (
            f"{DEFAULT_SYSTEM_PROMPT}\n\n{prompt}" if prompt else DEFAULT_SYSTEM_PROMPT
        )

    def reset(self) -> None:
        """Clear the conversation, keeping the mode and the attached folders.

        A mode is a preference, not conversation content: ``/new`` means "forget
        what we said", not "go back to the default personality". An attached
        folder is the same kind of thing — it is what the session is *about*, and
        the usual reason to start fresh is that the context filled up while
        working on that same tree. The title is conversation content, so it goes.
        """
        self.messages.clear()
        self.total_tokens = 0
        self.title = ""
