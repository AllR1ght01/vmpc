"""Modes: a named system prompt, optionally carrying extra commands.

A mode is the answer to two things at once. It swaps the system prompt, so
``/dev`` makes the model answer like a pair programmer rather than a general
assistant; and it can *unlock a command group*, so ``/dev`` also puts the
developer tools in the popup that were not there a moment ago.

Keeping both on one object is deliberate. A mode that changed the prompt but not
the toolset would need a second concept to explain why ``/wire`` exists, and a
mode that only revealed commands would be a menu, not a mode.

The prompt is **appended** to :data:`vmpc.session.DEFAULT_SYSTEM_PROMPT`, never
substituted for it. A user writing "be terse" should not have to restate that
answers use markdown and follow the user's language — and a mode that silently
dropped those would look like a bug in the renderer, not a prompt change.

Built-in modes live here; the user's own live in the config file and are loaded
alongside. Both kinds get a slash command named after them, so ``/dev`` and a
hand-written ``/sql`` are typed the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: A mode name becomes a slash command, so it has to be typeable as one word and
#: has to survive being compared against the built-in command names.
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

#: The command group the developer tools are tagged with. Named here rather than
#: spelled as a literal in two files, because a typo would silently hide them.
DEV_GROUP = "dev"


@dataclass
class Mode:
    """A named system prompt, plus whatever it unlocks."""

    name: str
    description: str
    #: Appended to the base system prompt. Empty means "base prompt only", which
    #: is how a mode can exist purely to unlock commands.
    prompt: str = ""
    #: Command groups this mode reveals. A tuple because it is compared against
    #: and never appended to.
    unlocks: tuple[str, ...] = ()
    #: Shipped with vmpc rather than written by the user. Built-ins cannot be
    #: removed, only shadowed by a custom mode of the same name.
    builtin: bool = False

    @property
    def display(self) -> str:
        return f"/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "unlocks": list(self.unlocks),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mode":
        unlocks = data.get("unlocks")
        return cls(
            name=str(data.get("name", "")).strip().lower(),
            description=str(data.get("description", "")),
            prompt=str(data.get("prompt", "")),
            # Read from disk rather than forced empty: a user who hand-edits the
            # config to unlock `dev` from their own mode meant to do that.
            unlocks=tuple(str(g) for g in unlocks) if isinstance(unlocks, list) else (),
            builtin=False,
        )


# --------------------------------------------------------------------------
# Built-ins
# --------------------------------------------------------------------------

_DEV_PROMPT = """You are in developer mode. The user is a programmer working in a terminal.

- Lead with the answer or the code. No preamble, no "great question", no summary
  of what you are about to say.
- Code goes in fenced blocks with a language tag. Reference files as path:line.
- When something is uncertain, say which part and what would settle it, rather
  than hedging the whole answer.
- Prefer the smallest change that works, and say what it trades away."""

_SHORT_PROMPT = """Answer in at most one short paragraph, or a list of at most five
lines. If the honest answer does not fit, give the conclusion and offer to expand."""

_EXPLAIN_PROMPT = """Explain as if to a competent engineer new to this specific area.
Define a term the first time it appears, give one concrete example before the
general rule, and name the thing that most people get wrong about it."""

#: DO NOT ALPHA-SORT — this is the order of the `/mode` picker.
BUILTIN_MODES: tuple[Mode, ...] = (
    Mode(
        "dev",
        "developer mode — terse answers, and the debugging commands",
        prompt=_DEV_PROMPT,
        unlocks=(DEV_GROUP,),
        builtin=True,
    ),
    Mode(
        "short",
        "one paragraph, maximum",
        prompt=_SHORT_PROMPT,
        builtin=True,
    ),
    Mode(
        "explain",
        "teach the topic, with an example before the rule",
        prompt=_EXPLAIN_PROMPT,
        builtin=True,
    ),
)


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------


def all_modes(custom: "list[Mode] | tuple[Mode, ...]" = ()) -> tuple[Mode, ...]:
    """Built-ins first, then the user's, with same-name customs winning.

    Shadowing rather than rejecting means a user who dislikes the built-in
    ``dev`` prompt can write their own ``dev`` and keep the command name they
    have in their fingers — and still keep the command group, if they say so.
    """
    by_name: dict[str, Mode] = {mode.name: mode for mode in BUILTIN_MODES}
    for mode in custom:
        by_name[mode.name] = mode
    return tuple(by_name.values())


def find_mode(name: str, custom: "list[Mode] | tuple[Mode, ...]" = ()) -> "Mode | None":
    wanted = name.strip().lower().lstrip("/")
    for mode in all_modes(custom):
        if mode.name == wanted:
            return mode
    return None


def unlocked_groups(mode: "Mode | None") -> frozenset[str]:
    """Command groups visible right now. No mode means base commands only."""
    return frozenset(mode.unlocks) if mode is not None else frozenset()


def validate_name(name: str, taken: "frozenset[str] | set[str]" = frozenset()) -> str:
    """Return a human-readable problem with ``name``, or an empty string.

    ``taken`` is the set of words already claimed by commands and modes. A mode
    that shadowed ``/help`` would be unreachable *and* would take ``/help`` with
    it, so the collision is refused at entry rather than resolved at dispatch.
    """
    cleaned = name.strip().lower()
    if not cleaned:
        return "name is empty"
    if not _NAME.match(cleaned):
        return "use lowercase letters, digits, - and _; start with a letter"
    if cleaned in taken:
        return f"/{cleaned} is already a command"
    return ""


