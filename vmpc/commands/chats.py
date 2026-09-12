"""``/chats`` and ``/title`` — the saved conversations and their names.

Every chat is written to disk as it happens (see :meth:`vmpc.ui.app.App._persist`),
so these commands never ask whether to save; there is nothing to save, only
something to reopen or rename. That is deliberate. A ``/save`` you have to
remember is a ``/save`` you forget on the one conversation you wanted.

Naming is the model's job by default. ``/title`` exists for the times it guessed
badly, and for the chat you want filed under a word only you would search for.
"""

from __future__ import annotations

import time

from rich.console import Console
from rich.text import Text

from vmpc.chats import (
    ChatRecord,
    delete_chat,
    list_chats,
    load_chat,
    sanitize_title,
    title_for,
)
from vmpc.commands import CommandContext
from vmpc.strings import en_plural, ru_plural, current_language, t
from vmpc.ui.picker import Choice, confirm, select


def _turn_count(turns: int) -> str:
    """``1 turn`` / ``2 turns`` — or the Russian three-way plural."""
    if current_language() == "en":
        return f"{turns} {en_plural(turns, t('chats.turn'))}"
    return f"{turns} {ru_plural(turns, 'ход', 'хода', 'ходов')}"


def run_chats_command(context: CommandContext, args: str) -> bool:
    """``/chats`` — reopen, list or delete. Returns False: nothing here is config."""
    parts = args.split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if action in ("", "open", "resume"):
        _pick(context)
    elif action == "list":
        _show(context)
    elif action in ("remove", "rm", "delete"):
        _remove(context, rest)
    else:
        console: Console = context.console  # type: ignore[assignment]
        console.print(Text(t("chats.unknown", action=action), style="error"))
        console.print(
            Text("  /chats", style="hint").append(
                t("chats.hint"), style="secondary"
            )
        )
    return False


def run_title_command(context: CommandContext, args: str) -> bool:
    """``/title`` — show it, set it, or ask the model again."""
    console: Console = context.console  # type: ignore[assignment]
    app = (context.extras or {}).get("app")
    session = context.session
    wanted = args.strip()

    if wanted.lower() in ("auto", "again", "retry"):
        return _rename_automatically(context, app)

    if not wanted:
        current = getattr(session, "title", "")
        if current:
            console.print(Text("  ✿ ", style="hint").append(current, style="header"))
            console.print(
                Text(t("title.rename_label"), style="hint").append(
                    t("title.rename_desc"), style="secondary"
                ).append(t("title.auto_hint"), style="hint").append(
                    t("title.auto_desc"), style="secondary"
                )
            )
        else:
            console.print(Text(t("title.no_name_yet"), style="secondary"))
            console.print(
                Text(t("title.rename_label"), style="hint").append(
                    t("title.no_name_hint"),
                    style="secondary",
                )
            )
        return False

    cleaned = sanitize_title(wanted)
    if not cleaned:
        console.print(Text(t("title.empty_after_clean"), style="error"))
        return False
    if app is not None and hasattr(app, "set_title"):
        app.set_title(cleaned, announce=False)
    else:
        session.title = cleaned  # type: ignore[union-attr]
    console.print(Text("  ✿ ", style="hint").append(cleaned, style="header"))
    return False


# --------------------------------------------------------------------------
# Reopening
# --------------------------------------------------------------------------


def _pick(context: CommandContext) -> None:
    console: Console = context.console  # type: ignore[assignment]
    records = list_chats()
    app = (context.extras or {}).get("app")
    current = getattr(app, "chat_id", "")

    if not records:
        console.print(Text(t("chats.no_saved"), style="secondary"))
        return

    choices = [
        Choice(
            value=record.id,
            label=record.label,
            description=_turn_count(record.turns)
            + (f" · {record.model}" if record.model else ""),
            badge="current" if record.id == current else _ago(record.updated),
        )
        for record in records
    ]
    chosen = select(t("chats.open_title"), choices)
    if chosen is None or chosen == current:
        return
    _adopt(context, chosen)


def _adopt(context: CommandContext, chat_id: str) -> None:
    console: Console = context.console  # type: ignore[assignment]
    app = (context.extras or {}).get("app")
    try:
        record = load_chat(chat_id)
    except (OSError, ValueError) as exc:
        console.print(Text(t("chats.cannot_open", exc=exc), style="error"))
        return

    if app is None or not hasattr(app, "adopt"):
        # Reachable if a command is driven outside the REPL; say so rather than
        # silently loading a conversation into a session nobody will read.
        console.print(Text(t("chats.needs_app"), style="error"))
        return

    app.adopt(record)
    console.print()
    console.print(Text("  ✿ ", style="hint").append(record.label, style="header"))
    turn_noun = (
        en_plural(record.turns, "turn")
        if current_language() == "en"
        else ru_plural(record.turns, "ход", "хода", "ходов")
    )
    console.print(
        Text(
            t("chats.restored", n=record.turns, turn_noun=turn_noun, tokens=record.total_tokens),
            style="secondary",
        )
    )
    if record.mode:
        # The prompt came back with the chat, so the mode line would otherwise be
        # the only thing on screen still claiming the old one.
        console.print(
            Text(t("chats.prompt_label"), style="secondary").append(f"/{record.mode}", style="hint")
        )
    console.print()


def _rename_automatically(context: CommandContext, app) -> bool:  # noqa: ANN001
    console: Console = context.console  # type: ignore[assignment]
    session = context.session
    messages = list(getattr(session, "messages", []) or [])
    if len(messages) < 2:
        console.print(
            Text(t("chats.nothing_to_name"), style="secondary")
        )
        return False
    provider = context.config.active_provider()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))
        return False

    # Synchronous here, unlike the automatic pass: the user typed a command and
    # is watching, so the wait is expected and a spinner would be more chrome
    # than the operation deserves.
    console.print(Text(t("chats.naming"), style="secondary"))
    title = title_for(provider, messages)
    if not title:
        console.print(Text(t("chats.bad_name_response"), style="error"))
        return False
    if app is not None and hasattr(app, "set_title"):
        app.session.title = ""  # let set_title through; it is a deliberate rename
        app.set_title(title, announce=False)
    else:
        session.title = title  # type: ignore[union-attr]
    console.print(Text("  ✿ ", style="hint").append(title, style="header"))
    return False


# --------------------------------------------------------------------------
# Listing and deleting
# --------------------------------------------------------------------------


def _show(context: CommandContext) -> None:
    console: Console = context.console  # type: ignore[assignment]
    records = list_chats()
    if not records:
        console.print(Text(t("chats.no_saved"), style="secondary"))
        return
    app = (context.extras or {}).get("app")
    current = getattr(app, "chat_id", "")

    console.print()
    width = min(max(len(record.label) for record in records), 48)
    for record in records:
        row = Text("  ")
        row.append("● " if record.id == current else "  ", style="success")
        row.append(
            record.label.ljust(width),
            style="header" if record.id == current else "primary",
        )
        row.append(f"  {_ago(record.updated)}", style="secondary")
        row.append(f"  {record.id}", style="secondary")
        # One chat is one line, like the endpoint and mode lists.
        row.truncate(max(console.width - 1, 20), overflow="ellipsis")
        console.print(row)
    console.print()


def _remove(context: CommandContext, target: str) -> None:
    console: Console = context.console  # type: ignore[assignment]
    records = list_chats()
    if not records:
        console.print(Text(t("chats.no_saved"), style="secondary"))
        return

    record = _resolve(records, target)
    if record is None:
        if target:
            console.print(Text(t("chats.no_match", target=target), style="error"))
            return
        chosen = select(
            t("chats.delete_title"),
            [
                Choice(value=r.id, label=r.label, badge=_ago(r.updated))
                for r in records
            ],
        )
        if chosen is None:
            return
        record = _resolve(records, chosen)
        if record is None:
            return

    if confirm(t("chats.confirm_delete", label=record.label), default=False) is not True:
        return
    if delete_chat(record.id):
        console.print(Text(t("chats.deleted", label=record.label), style="secondary"))
        app = (context.extras or {}).get("app")
        if app is not None and getattr(app, "chat_id", "") == record.id:
            # The conversation stays on screen and in context; only its file is
            # gone. Clearing the id means the next turn writes a new one rather
            # than resurrecting the file the user just deleted.
            app.chat_id = ""
            app.chat_created = 0.0
    else:
        console.print(Text(t("chats.delete_failed"), style="error"))


def _resolve(records: list[ChatRecord], target: str) -> "ChatRecord | None":
    """Find a chat by id, or by a case-insensitive fragment of its name."""
    wanted = target.strip()
    if not wanted:
        return None
    for record in records:
        if record.id == wanted:
            return record
    lowered = wanted.lower()
    matches = [r for r in records if lowered in r.label.lower()]
    return matches[0] if len(matches) == 1 else None


def _ago(when: float) -> str:
    """A short relative time, for a badge column."""
    if not when:
        return ""
    seconds = max(0, int(time.time() - when))
    if seconds < 90:
        return t("chats.ago_just_now")
    minutes = seconds // 60
    if minutes < 60:
        return t("chats.ago_minutes", n=minutes)
    hours = minutes // 60
    if hours < 24:
        return t("chats.ago_hours", n=hours)
    days = hours // 24
    return t("chats.ago_days", n=days) if days < 30 else time.strftime("%d %b", time.localtime(when))
