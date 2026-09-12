"""Slash command registry.

Commands are data, not a dispatch chain. One table drives the completion popup,
``/help``, and dispatch, so adding a command cannot leave one of the three out of
sync. The ordering convention is borrowed from the Codex TUI: the list is *not*
alphabetical, because presentation order is the order of the popup and frequently
used commands belong at the top.

Which commands exist depends on the active mode, so the table is resolved through
a :class:`CommandRegistry` rather than read as a module global. The registry
holds the live :class:`~vmpc.config.Config` instead of a snapshot of it — a mode
added by ``/mode add`` has to appear in the popup on the very next keystroke, and
a copy taken at startup could not do that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Optional

from vmpc.modes import DEV_GROUP, Mode, all_modes, find_mode, unlocked_groups
from vmpc.strings import t

#: Signature of a command handler: (context, inline args) -> keep running.
Handler = Callable[["CommandContext", str], bool]


@dataclass
class SlashCommand:
    name: str
    #: A key into :mod:`vmpc.strings` (``t(f"cmd.{desc_key}")``), not the text
    #: itself — ``description`` below resolves it lazily on every read, so a
    #: ``/lang`` switch changes what ``/help`` shows on its very next call
    #: without needing the command table rebuilt.
    desc_key: str
    #: Whether text after the command name is meaningful, e.g. ``/api use foo``.
    supports_inline_args: bool = False
    #: Whether the command can run while a turn is streaming.
    available_during_task: bool = False
    aliases: tuple[str, ...] = ()
    handler: Optional[Handler] = None
    #: Empty means always available. Anything else is a group a mode has to
    #: unlock before this command is listed or can be dispatched.
    group: str = ""
    #: Set only by :meth:`CommandRegistry.mode_commands`, which builds a
    #: command per mode and already has the (possibly user-written, therefore
    #: untranslated) text in hand — no key to look up.
    literal_description: Optional[str] = None

    @property
    def display(self) -> str:
        return f"/{self.name}"

    @property
    def description(self) -> str:
        if self.literal_description is not None:
            return self.literal_description
        return t(f"cmd.{self.desc_key}")


@dataclass
class CommandContext:
    """What a handler is allowed to touch."""

    console: object
    config: object
    session: object
    #: The registry that dispatched this command, so a handler can list its
    #: siblings (``/help``) or add to them (``/mode add``).
    registry: Optional["CommandRegistry"] = None
    #: Set by a handler to end the REPL.
    should_exit: bool = False
    #: Set by a handler when the config was mutated and the header needs redraw.
    config_changed: bool = False
    extras: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _handle_api(context: CommandContext, args: str) -> bool:
    from vmpc.commands.api import run_api_command

    changed = run_api_command(context.console, context.config, args)  # type: ignore[arg-type]
    context.config_changed = context.config_changed or changed
    return True


def _handle_model(context: CommandContext, args: str) -> bool:
    from vmpc.commands.model import run_model_command

    changed = run_model_command(context.console, context.config, args)  # type: ignore[arg-type]
    context.config_changed = context.config_changed or changed
    return True


def _handle_help(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import show_help

    show_help(context)
    return True


def _handle_clear(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import clear_screen

    clear_screen(context)
    return True


def _handle_status(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import show_status

    show_status(context)
    return True


def _handle_new(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import reset_conversation

    reset_conversation(context)
    return True


def _handle_reasoning(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import toggle_reasoning

    context.config_changed = toggle_reasoning(context, args) or context.config_changed
    return True


def _handle_mode(context: CommandContext, args: str) -> bool:
    from vmpc.commands.mode import run_mode_command

    context.config_changed = run_mode_command(context, args) or context.config_changed
    return True


def _handle_lang(context: CommandContext, args: str) -> bool:
    from vmpc.commands.builtins import set_language

    context.config_changed = set_language(context, args) or context.config_changed
    return True


def _handle_mode_switch(name: str, context: CommandContext, args: str) -> bool:
    """Handler bound to one mode's own slash command, e.g. ``/dev``."""
    from vmpc.commands.mode import activate_mode

    context.config_changed = activate_mode(context, name) or context.config_changed
    return True


def _handle_quit(context: CommandContext, args: str) -> bool:
    context.should_exit = True
    return False


def _handle_chats(context: CommandContext, args: str) -> bool:
    from vmpc.commands.chats import run_chats_command

    run_chats_command(context, args)
    return True


def _handle_context(context: CommandContext, args: str) -> bool:
    from vmpc.commands.context import run_context_command

    run_context_command(context, args)
    return True


def _handle_title(context: CommandContext, args: str) -> bool:
    from vmpc.commands.chats import run_title_command

    run_title_command(context, args)
    return True


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

#: DO NOT ALPHA-SORT. This order is the popup order. ``desc_key`` values are
#: looked up as ``cmd.<key>`` in :mod:`vmpc.strings`.
COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "api",
        "api",
        supports_inline_args=True,
        handler=_handle_api,
    ),
    SlashCommand(
        "model",
        "model",
        supports_inline_args=True,
        handler=_handle_model,
    ),
    SlashCommand(
        "context",
        "context",
        supports_inline_args=True,
        aliases=("ctx", "files"),
        handler=_handle_context,
    ),
    SlashCommand(
        "mode",
        "mode",
        supports_inline_args=True,
        handler=_handle_mode,
    ),
    SlashCommand(
        "lang",
        "lang",
        supports_inline_args=True,
        aliases=("language", "язык"),
        handler=_handle_lang,
    ),
    SlashCommand(
        "reasoning",
        "reasoning",
        supports_inline_args=True,
        handler=_handle_reasoning,
    ),
    SlashCommand("new", "new", handler=_handle_new),
    SlashCommand(
        "chats",
        "chats",
        supports_inline_args=True,
        aliases=("resume",),
        handler=_handle_chats,
    ),
    SlashCommand(
        "title",
        "title",
        supports_inline_args=True,
        handler=_handle_title,
    ),
    SlashCommand(
        "status",
        "status",
        available_during_task=True,
        handler=_handle_status,
    ),
    SlashCommand("clear", "clear", handler=_handle_clear),
    SlashCommand(
        "help",
        "help",
        available_during_task=True,
        handler=_handle_help,
    ),
    SlashCommand(
        "quit",
        "quit",
        aliases=("exit",),
        available_during_task=True,
        handler=_handle_quit,
    ),
)


def _dev(name: str, desc_key: str, handler_name: str, **kwargs: Any) -> SlashCommand:
    """Build one developer command.

    The handler is resolved lazily by name so importing this module does not
    drag in httpx, the renderer and the theme for a user who never types
    ``/dev``.
    """

    def dispatch(context: CommandContext, args: str) -> bool:
        from vmpc.commands import dev as dev_module

        getattr(dev_module, handler_name)(context, args)
        return True

    return SlashCommand(
        name, desc_key, handler=dispatch, group=DEV_GROUP, **kwargs
    )


#: Revealed by any mode unlocking :data:`~vmpc.modes.DEV_GROUP`. These are the
#: tools for working on vmpc itself rather than for talking to a model.
DEV_COMMANDS: tuple[SlashCommand, ...] = (
    _dev("raw", "dev.raw", "show_raw", supports_inline_args=True),
    _dev("render", "dev.render", "render_sample", supports_inline_args=True),
    _dev("wire", "dev.wire", "show_wire", supports_inline_args=True),
    _dev("ping", "dev.ping", "ping_endpoint"),
    _dev("palette", "dev.palette", "show_palette"),
    _dev("prompt", "dev.prompt", "show_prompt"),
)

#: Every command that can exist, whatever the mode. Used for name collision
#: checks, so a custom mode cannot be given a name that would shadow a command
#: the user has not unlocked yet.
ALL_COMMANDS: tuple[SlashCommand, ...] = COMMANDS + DEV_COMMANDS


@dataclass
class CommandRegistry:
    """The command table as it stands for the active mode.

    ``config`` is the live object, not a copy: :meth:`visible` is called on every
    keystroke that opens the popup, and it must reflect a mode added seconds ago.
    A registry with no config still resolves the base commands, which is what
    makes a bare :class:`~vmpc.ui.composer.Composer` usable in isolation.
    """

    config: Any = None

    # -- modes -------------------------------------------------------------

    def custom_modes(self) -> tuple[Mode, ...]:
        return tuple(getattr(self.config, "modes", ()) or ())

    def modes(self) -> tuple[Mode, ...]:
        return all_modes(self.custom_modes())

    def active_mode(self) -> Optional[Mode]:
        name = str(getattr(self.config, "mode", "") or "")
        if not name:
            return None
        return find_mode(name, self.custom_modes())

    def groups(self) -> frozenset[str]:
        return unlocked_groups(self.active_mode())

    # -- commands ----------------------------------------------------------

    def mode_commands(self) -> tuple[SlashCommand, ...]:
        """One slash command per mode, so ``/dev`` is typed like any other."""
        active = self.active_mode()
        out = []
        for mode in self.modes():
            current = active is not None and active.name == mode.name
            # mode.description is already resolved text (translated for a
            # built-in mode, the user's own words for a custom one) — a
            # literal, not a key, so it is passed straight through rather
            # than looked up a second time as ``cmd.<name>``.
            description = mode.description
            if current:
                # The toggle only exists for the active mode, so it is only
                # advertised there.
                description = f"{description}  {t('cmd.mode_toggle_suffix')}"
            out.append(
                SlashCommand(
                    mode.name,
                    "",
                    literal_description=description,
                    handler=partial(_handle_mode_switch, mode.name),
                    group="",
                )
            )
        return tuple(out)

    def visible(self) -> tuple[SlashCommand, ...]:
        """Commands the user can type right now, in popup order.

        Unlocked group commands come directly after the base ones rather than
        last: they are the whole visible effect of entering a mode, and burying
        them under the mode list would hide the thing that just happened.
        """
        groups = self.groups()
        unlocked = tuple(cmd for cmd in DEV_COMMANDS if cmd.group in groups)
        return COMMANDS + unlocked + self.mode_commands()

    def find(self, name: str) -> Optional[SlashCommand]:
        """Resolve a command by canonical name or alias, honoring the mode."""
        lowered = name.lower().lstrip("/")
        for command in self.visible():
            if command.name == lowered or lowered in command.aliases:
                return command
        return None

    def locked(self, name: str) -> Optional[SlashCommand]:
        """A real command that the active mode has not unlocked, if any.

        Separate from :meth:`find` so the caller can say "``/wire`` needs
        ``/dev``" instead of "unknown command", which is the difference between
        a discoverable feature and a typo.
        """
        lowered = name.lower().lstrip("/")
        groups = self.groups()
        for command in ALL_COMMANDS:
            if command.group and command.group not in groups:
                if command.name == lowered or lowered in command.aliases:
                    return command
        return None

    def parse(self, line: str) -> Optional[tuple[SlashCommand, str]]:
        """Split a ``/command args`` line, or return None if it is not one.

        Returning None for an unknown slash word is deliberate: it lets the
        caller decide between "unknown command" and "the user is talking about
        a path".
        """
        if not line.startswith("/"):
            return None
        head, _, rest = line[1:].partition(" ")
        command = self.find(head)
        if command is None:
            return None
        return command, rest.strip()

    def reserved_names(self) -> frozenset[str]:
        """Every word already claimed, for validating a new mode's name.

        Built from :data:`ALL_COMMANDS` rather than the visible set: a mode named
        ``wire`` would look fine until the day the user typed ``/dev``, and then
        one of the two would silently win.
        """
        names: set[str] = set()
        for command in ALL_COMMANDS:
            names.add(command.name)
            names.update(command.aliases)
        return frozenset(names)
