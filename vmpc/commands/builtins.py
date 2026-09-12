"""Small built-in commands: help, status, clear, new, reasoning."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from vmpc.commands import ALL_COMMANDS, CommandContext, CommandRegistry
from vmpc.config import AUTH_LABELS, KEY_TYPE_LABELS, WIRE_LABELS


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
    console.print(Text("commands", style="header"))
    for command in commands:
        row(command.display, command.description)

    if modes:
        console.print()
        console.print(Text("modes", style="header"))
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
            console.print(
                Text(f"  /{group}", style="hint").append(
                    f" unlocks {count} more command{'s' if count != 1 else ''}",
                    style="secondary",
                )
            )

    console.print()
    console.print(Text("  enter", style="hint").append("      send", style="secondary"))
    console.print(
        Text("  alt+enter", style="hint").append("  newline", style="secondary")
    )
    console.print(
        Text("  ctrl+c", style="hint").append(
            "    interrupt a running turn", style="secondary"
        )
    )
    console.print(
        Text("  ctrl+c", style="hint").append(
            "     clear the input, twice to quit", style="secondary"
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
        console.print(Text("  no endpoint configured", style="error"))  # type: ignore[union-attr]
        console.print(Text("  /api add — configure one", style="hint"))  # type: ignore[union-attr]
        return

    rows = [
        ("endpoint", provider.name),
        ("format", WIRE_LABELS.get(provider.wire, provider.wire)),
        ("url", provider.endpoint()),
        ("model", provider.model or "(not set)"),
        ("api-key-type", KEY_TYPE_LABELS.get(provider.key_type, provider.key_type)),
        ("api-key", provider.masked_key()),
        ("auth", AUTH_LABELS.get(provider.auth_scheme, provider.auth_scheme)),
        ("reasoning", "on" if provider.reasoning else "off"),
        # A mode rewrites the system prompt, so it belongs in the same list as
        # the endpoint rather than being something you have to remember.
        ("mode", getattr(session, "mode", "") or "(none)"),
        ("chat", getattr(session, "title", "") or "(unnamed)"),
        # Attached files ride in the system prompt on every turn, so their cost
        # belongs next to the endpoint's rather than only inside /context.
        ("context", _context_summary(session)),
        ("messages", str(len(getattr(session, "messages", [])))),
        ("tokens", str(getattr(session, "total_tokens", 0))),
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
        return "(none)"
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
        context.console.print(Text("  new conversation", style="secondary"))  # type: ignore[union-attr]


def toggle_reasoning(context: CommandContext, args: str) -> bool:
    """Turn the reasoning channel on or off for the active endpoint."""
    config = context.config
    console = context.console
    provider = config.active_provider()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text("  no endpoint configured", style="error"))  # type: ignore[union-attr]
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
    state = "on" if provider.reasoning else "off"
    console.print(  # type: ignore[union-attr]
        Text("  reasoning ", style="secondary").append(
            state, style="success" if provider.reasoning else "secondary"
        )
    )
    return True
