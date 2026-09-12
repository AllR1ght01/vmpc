"""Small built-in commands: help, status, clear, new, reasoning, lang."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from vmpc.commands import ALL_COMMANDS, CommandContext, CommandRegistry
from vmpc.config import AUTH_LABELS, KEY_TYPE_LABELS, WIRE_LABELS
from vmpc.strings import (
    LANGUAGE_NAMES,
    current_language,
    en_plural,
    ru_plural,
    set_ui_language,
    t,
)


def show_help(context: CommandContext) -> None:
    console: Console = context.console  # type: ignore[assignment]
    registry = context.registry or CommandRegistry()
    active = registry.active_mode()
    mode_names = {mode.name for mode in registry.modes()}

    commands = [c for c in registry.visible() if c.name not in mode_names]
    modes = [c for c in registry.visible() if c.name in mode_names]

    # One width across both sections so the descriptions line up as a single
    # column, even though the sections are printed apart.
    width = max(len(command.display) for command in registry.visible())

    def row(label: str, description: str, label_style: str = "hint") -> None:
        line = Text("  ")
        line.append(label.ljust(width), style=label_style)
        line.append(f"  {description}", style="secondary")
        console.print(line)

    console.print()
    console.print(Text(t("help.commands_header"), style="header"))
    for command in commands:
        row(command.display, command.description)

    if modes:
        console.print()
        console.print(Text(t("help.modes_header"), style="header"))
        for command in modes:
            current = active is not None and command.name == active.name
            row(command.display, command.description, "success" if current else "hint")

    # Point at what is not on screen. A command group nobody knows exists is
    # worth exactly as much as one that does not.
    groups = registry.groups()
    hidden = [c for c in ALL_COMMANDS if c.group and c.group not in groups]
    if hidden:
        by_group: dict[str, int] = {}
        for command in hidden:
            by_group[command.group] = by_group.get(command.group, 0) + 1
        console.print()
        for group, count in by_group.items():
            noun = (
                en_plural(count, "command")
                if current_language() == "en"
                else ru_plural(count, "команду", "команды", "команд")
            )
            console.print(
                Text(f"  /{group}", style="hint").append(
                    t("help.group_unlocks", n=count, noun=noun),
                    style="secondary",
                )
            )

    console.print()
    console.print(
        Text(t("help.key_enter"), style="hint").append(
            t("help.key_enter_desc"), style="secondary"
        )
    )
    console.print(
        Text(t("help.key_alt_enter"), style="hint").append(
            t("help.key_alt_enter_desc"), style="secondary"
        )
    )
    console.print(
        Text(t("help.key_esc"), style="hint").append(
            t("help.key_esc_desc"), style="secondary"
        )
    )
    console.print(
        Text(t("help.key_ctrl_c"), style="hint").append(
            t("help.key_ctrl_c_desc"), style="secondary"
        )
    )
    console.print()


def show_status(context: CommandContext) -> None:
    console = context.console  # type: ignore[assignment]
    config = context.config
    session = context.session

    provider = config.active_provider()  # type: ignore[union-attr]
    console.print()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))  # type: ignore[union-attr]
        console.print(Text(t("app.api_add_hint"), style="hint"))  # type: ignore[union-attr]
        return

    rows = [
        (t("status.label.endpoint"), provider.name),
        (t("status.label.format"), WIRE_LABELS.get(provider.wire, provider.wire)),
        (t("status.label.url"), provider.endpoint()),
        (t("status.label.model"), provider.model or t("status.not_set")),
        (
            t("status.label.api_key_type"),
            KEY_TYPE_LABELS.get(provider.key_type, provider.key_type),
        ),
        (t("status.label.api_key"), provider.masked_key()),
        (t("status.label.auth"), AUTH_LABELS.get(provider.auth_scheme, provider.auth_scheme)),
        (t("status.label.reasoning"), t("status.on") if provider.reasoning else t("status.off")),
        # A mode rewrites the system prompt, so it belongs in the same list as
        # the endpoint rather than being something you have to remember.
        (t("status.label.mode"), getattr(session, "mode", "") or t("status.none")),
        (t("status.label.language"), LANGUAGE_NAMES.get(current_language(), "English")),
        (t("status.label.chat"), getattr(session, "title", "") or t("status.unnamed")),
        # Attached files ride in the system prompt on every turn, so their cost
        # belongs next to the endpoint's rather than only inside /context.
        (t("status.label.context"), _context_summary(session)),
        (t("status.label.messages"), str(len(getattr(session, "messages", [])))),
        (t("status.label.tokens"), str(getattr(session, "total_tokens", 0))),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(  # type: ignore[union-attr]
            Text(f"  {label.ljust(width)}  ", style="secondary").append(
                value, style="primary"
            )
        )
    console.print()  # type: ignore[union-attr]


def _context_summary(session: object) -> str:
    """One cell for /status: how much attached-file text every turn carries."""
    paths = list(getattr(session, "context_paths", []) or [])
    if not paths:
        return t("status.context_none")
    # Four characters to a token, the same rough conversion /context prints.
    tokens = len(str(getattr(session, "context_text", "") or "")) // 4
    where = paths[0] if len(paths) == 1 else f"{len(paths)} paths"
    return f"{where}  ~{tokens // 1000}k tokens" if tokens >= 1000 else f"{where}  ~{tokens} tokens"


def clear_screen(context: CommandContext) -> None:
    console = context.console
    console.clear()  # type: ignore[union-attr]
    reset_conversation(context, announce=False)


def reset_conversation(context: CommandContext, announce: bool = True) -> None:
    session = context.session
    if hasattr(session, "reset"):
        session.reset()
    # Release the chat id too, so the next turn opens a new file. Keeping it
    # would make /new append to the conversation it was told to forget: the
    # messages would be gone from context but the file would still grow.
    app = (context.extras or {}).get("app")
    if app is not None and hasattr(app, "forget_chat"):
        app.forget_chat()
    if announce:
        context.console.print(Text(t("new.conversation"), style="secondary"))  # type: ignore[union-attr]


def toggle_reasoning(context: CommandContext, args: str) -> bool:
    """Turn the reasoning channel on or off for the active endpoint."""
    config = context.config
    console = context.console
    provider = config.active_provider()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))  # type: ignore[union-attr]
        return False

    wanted = args.strip().lower()
    if wanted in ("on", "true", "yes", "1"):
        provider.reasoning = True
    elif wanted in ("off", "false", "no", "0"):
        provider.reasoning = False
    else:
        provider.reasoning = not provider.reasoning

    config.upsert(provider)  # type: ignore[union-attr]
    config.save()  # type: ignore[union-attr]
    state = t("status.on") if provider.reasoning else t("status.off")
    console.print(  # type: ignore[union-attr]
        Text(f"  {t('status.label.reasoning')} ", style="secondary").append(
            state, style="success" if provider.reasoning else "secondary"
        )
    )
    return True


#: What a person can type after ``/lang``: the two interface languages vmpc
#: actually has, plus ``auto`` (and a couple of synonyms) to fall back to the
#: default. Russian spellings are accepted too, since switching *to* Russian
#: by typing an English word is a small annoyance this costs nothing to
#: remove.
LANGUAGE_ALIASES = {
    "en": "en", "eng": "en", "english": "en", "англ": "en", "английский": "en",
    "ru": "ru", "rus": "ru", "russian": "ru", "рус": "ru", "русский": "ru",
    "auto": "en", "off": "en", "default": "en", "авто": "en",
}


def set_language(context: CommandContext, args: str) -> bool:
    """Switch vmpc's own interface language — not what the model replies in.

    Persisted on ``config.ui`` rather than only applied for this run — the
    same reasoning as ``mode``: picking Russian once should still hold on
    tomorrow's launch, not just for the rest of tonight's process.
    """
    config = context.config
    console = context.console

    wanted = args.strip().lower()
    if not wanted:
        # Bare toggle: flip between the two languages vmpc actually ships.
        new = "en" if current_language() == "ru" else "ru"
    else:
        new = LANGUAGE_ALIASES.get(wanted)
        if new is None:
            console.print(  # type: ignore[union-attr]
                Text(t("lang.unknown", wanted=wanted), style="error")
            )
            return False

    set_ui_language(new)
    config.ui["language"] = new  # type: ignore[union-attr]
    config.save()  # type: ignore[union-attr]

    console.print(  # type: ignore[union-attr]
        Text(t("lang.set"), style="secondary").append(
            LANGUAGE_NAMES.get(new, new), style="success"
        )
    )
    return True
