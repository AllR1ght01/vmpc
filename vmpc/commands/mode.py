"""``/mode`` — pick a prompt mode, or write one.

A mode is two things at once: a system prompt and a set of command groups. That
pairing is the point. ``/dev`` is not just a different prompt, it is a different
set of tools, and separating the two would mean remembering to turn both on.

Switching is deliberately *not* a fresh conversation. The new prompt applies from
the next turn onward, so you can ask something, decide it needs a reviewer's eye,
and switch without losing the thread. The cost is that the transcript then has
two prompts behind it, which is why a switch prints what it changed and why the
banner keeps the mode on screen.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from vmpc.commands import CommandContext, CommandRegistry
from vmpc.modes import BUILTIN_MODES, DEV_GROUP, Mode, find_mode, validate_name
from vmpc.strings import t
from vmpc.ui.picker import confirm, prompt_multiline, prompt_text


def run_mode_command(context: CommandContext, args: str) -> bool:
    """Returns True when the config changed."""
    parts = args.split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if action in ("", "list"):
        _show_modes(context)
        return False
    if action in ("off", "none", "default"):
        return _clear_mode(context)
    if action == "add":
        return _add_mode(context, rest)
    if action == "edit":
        return _edit_mode(context, rest)
    if action in ("remove", "rm", "delete"):
        return _remove_mode(context, rest)
    if action == "show":
        _show_one(context, rest)
        return False

    # Anything else is a mode name, so `/mode dev` works as well as bare `/dev`.
    return activate_mode(context, action)


def _registry(context: CommandContext) -> CommandRegistry:
    return context.registry or CommandRegistry(context.config)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def _show_modes(context: CommandContext) -> None:
    console: Console = context.console  # type: ignore[assignment]
    registry = _registry(context)
    active = registry.active_mode()
    modes = registry.modes()

    console.print()
    builtin = {mode.name for mode in BUILTIN_MODES()}
    width = max(len(mode.name) for mode in modes)
    for mode in modes:
        current = active is not None and active.name == mode.name
        row = Text("  ")
        row.append("● " if current else "  ", style="success")
        row.append(mode.name.ljust(width), style="header" if current else "hint")
        row.append(f"  {mode.description}", style="secondary")
        if mode.name not in builtin:
            row.append(t("mode.custom_suffix"), style="secondary")
        # One mode is one line, for the same reason the endpoint list is.
        row.truncate(max(console.width - 1, 20), overflow="ellipsis")
        console.print(row)
    console.print()
    console.print(
        Text("  /mode add", style="hint").append(
            t("mode.menu_hint"),
            style="secondary",
        )
    )
    console.print()


def _show_one(context: CommandContext, name: str) -> None:
    console: Console = context.console  # type: ignore[assignment]
    registry = _registry(context)
    mode = find_mode(name, registry.custom_modes()) if name else registry.active_mode()
    if mode is None:
        console.print(Text(t("mode.no_mode_named", name=name), style="error"))
        return
    console.print()
    console.print(
        Text(f"  /{mode.name}", style="header").append(
            f"  {mode.description}", style="secondary"
        )
    )
    if mode.unlocks:
        console.print(
            Text(t("mode.unlocks_label"), style="secondary").append(
                ", ".join(mode.unlocks), style="hint"
            )
        )
    console.print()
    for line in (mode.prompt or t("mode.no_prompt_placeholder")).splitlines():
        console.print(Text("  │ ", style="secondary").append(line, style="primary"))
    console.print()


# --------------------------------------------------------------------------
# Switching
# --------------------------------------------------------------------------


def activate_mode(context: CommandContext, name: str) -> bool:
    """Switch to ``name``, or turn it off if it is already active."""
    console: Console = context.console  # type: ignore[assignment]
    registry = _registry(context)
    mode = find_mode(name, registry.custom_modes())
    if mode is None:
        console.print(Text(t("mode.no_mode_named", name=name), style="error"))
        console.print(
            Text("  /mode", style="hint").append(t("mode.list_hint"), style="secondary")
        )
        return False

    active = registry.active_mode()
    if active is not None and active.name == mode.name:
        return _clear_mode(context)

    config = context.config
    config.mode = mode.name  # type: ignore[union-attr]
    config.save()  # type: ignore[union-attr]
    _apply_to_session(context, mode)

    console.print()
    console.print(
        Text("  ● ", style="success")
        .append(f"/{mode.name}", style="header")
        .append(f"  {mode.description}", style="secondary")
    )
    if mode.unlocks:
        # The commands are the visible half of the switch; say where to see them.
        console.print(
            Text(t("mode.unlocked_hint"), style="secondary").append(
                "/help", style="hint"
            )
        )
    console.print()
    return True


def _clear_mode(context: CommandContext) -> bool:
    console: Console = context.console  # type: ignore[assignment]
    config = context.config
    if not getattr(config, "mode", ""):
        console.print(Text(t("mode.already_default"), style="secondary"))
        return False
    config.mode = ""  # type: ignore[union-attr]
    config.save()  # type: ignore[union-attr]
    _apply_to_session(context, None)
    console.print(
        Text(t("mode.off"), style="secondary").append(
            t("mode.off_suffix"), style="secondary"
        )
    )
    return True


def _apply_to_session(context: CommandContext, mode: "Mode | None") -> None:
    session = context.session
    if hasattr(session, "set_mode"):
        session.set_mode(mode)


# --------------------------------------------------------------------------
# Writing one
# --------------------------------------------------------------------------


def _add_mode(context: CommandContext, name: str) -> bool:
    console: Console = context.console  # type: ignore[assignment]
    registry = _registry(context)

    taken = set(registry.reserved_names()) | {m.name for m in registry.modes()}

    def check(value: str) -> "str | None":
        return validate_name(value, frozenset(taken)) or None

    if name:
        problem = check(name)
        if problem:
            console.print(Text(f"  {problem}", style="error"))
            return False
    else:
        name = prompt_text(
            t("mode.field_name"),
            placeholder=t("mode.field_name_placeholder"),
            validate=check,
        ) or ""
    name = name.strip().lower()
    if not name:
        return False

    description = prompt_text(
        t("mode.field_description"), placeholder=t("mode.field_description_placeholder")
    )
    if description is None:
        return False

    prompt = prompt_multiline(
        t("mode.field_prompt"), placeholder=t("mode.field_prompt_placeholder")
    )
    if not prompt:
        console.print(Text(t("mode.cancelled_no_prompt"), style="secondary"))
        return False

    unlock = confirm(t("mode.confirm_unlock_dev"), default=False)
    if unlock is None:
        return False

    context.config.modes.append(  # type: ignore[union-attr]
        Mode(
            name=name,
            description=description or t("mode.default_description"),
            prompt=prompt,
            unlocks=(DEV_GROUP,) if unlock else (),
        )
    )
    context.config.save()  # type: ignore[union-attr]
    console.print(
        Text("  ✔ ", style="success")
        .append(f"/{name}", style="header")
        .append(t("mode.saved"), style="secondary")
    )
    return True


def _edit_mode(context: CommandContext, name: str) -> bool:
    console: Console = context.console  # type: ignore[assignment]
    config = context.config
    wanted = name.strip().lower()
    match = next(
        (m for m in getattr(config, "modes", []) or [] if m.name == wanted), None
    )
    if match is None:
        if find_mode(wanted, ()) is not None:
            # Built-ins ship with the code. Copying one to disk to edit it would
            # freeze that copy, and it would stop tracking later improvements.
            console.print(
                Text(t("mode.built_in", name=wanted), style="error")
                .append("/mode add", style="hint")
                .append(t("mode.write_own_hint"), style="secondary")
            )
        else:
            console.print(Text(t("mode.no_custom_named", name=name), style="error"))
        return False

    description = prompt_text(t("mode.field_description"), default=match.description)
    if description is None:
        return False
    prompt = prompt_multiline(t("mode.field_prompt"), default=match.prompt)
    if not prompt:
        console.print(Text(t("mode.cancelled_empty_prompt"), style="secondary"))
        return False
    unlock = confirm(
        t("mode.confirm_unlock_dev"),
        default=DEV_GROUP in match.unlocks,
    )
    if unlock is None:
        return False

    match.description = description
    match.prompt = prompt
    match.unlocks = (DEV_GROUP,) if unlock else ()
    config.save()  # type: ignore[union-attr]
    # The session is holding the old prompt if this is the mode in effect.
    if getattr(config, "mode", "") == match.name:
        _apply_to_session(context, match)
    console.print(Text(t("mode.updated", name=match.name), style="success"))
    return True


def _remove_mode(context: CommandContext, name: str) -> bool:
    console: Console = context.console  # type: ignore[assignment]
    config = context.config
    wanted = name.strip().lower()
    match = next(
        (m for m in getattr(config, "modes", []) or [] if m.name == wanted), None
    )
    if match is None:
        console.print(Text(t("mode.no_custom_named", name=name), style="error"))
        return False
    config.modes.remove(match)  # type: ignore[union-attr]
    if getattr(config, "mode", "") == match.name:
        # Leaving the name behind would point at nothing: the next run would
        # quietly fall back to the default prompt with the banner still naming
        # a mode that no longer exists.
        config.mode = ""  # type: ignore[union-attr]
        _apply_to_session(context, None)
    config.save()  # type: ignore[union-attr]
    console.print(Text(t("mode.removed", name=match.name), style="secondary"))
    return True
