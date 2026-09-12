"""Slash command registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Optional

from vmpc.modes import DEV_GROUP, Mode, all_modes, find_mode, unlocked_groups

Handler = Callable[["CommandContext", str], bool]

@dataclass
class SlashCommand:
    name: str
    description: str
    supports_inline_args: bool = False
    available_during_task: bool = False
    aliases: tuple[str, ...] = ()
    handler: Optional[Handler] = None
    group: str = ""
    @property
    def display(self) -> str:
        return f"/{self.name}"

@dataclass
class CommandContext:
    console: object
    config: object
    session: object
    registry: Optional["CommandRegistry"] = None
    should_exit: bool = False
    config_changed: bool = False
    extras: dict = field(default_factory=dict)


def _simple(module: str, function: str):
    def handler(context: CommandContext, args: str) -> bool:
        from importlib import import_module
        getattr(import_module(module), function)(context, args)
        return True
    return handler


def _handle_api(c, a):
    from vmpc.commands.api import run_api_command
    c.config_changed = run_api_command(c.console, c.config, a) or c.config_changed
    return True

def _handle_model(c, a):
    from vmpc.commands.model import run_model_command
    c.config_changed = run_model_command(c.console, c.config, a) or c.config_changed
    return True

def _handle_help(c, a):
    from vmpc.commands.builtins import show_help
    show_help(c); return True

def _handle_clear(c, a):
    from vmpc.commands.builtins import clear_screen
    clear_screen(c); return True

def _handle_status(c, a):
    from vmpc.commands.builtins import show_status
    show_status(c); return True

def _handle_new(c, a):
    from vmpc.commands.builtins import reset_conversation
    reset_conversation(c); return True

def _handle_context(c, a):
    from vmpc.commands.context import run_context_command
    run_context_command(c, a); return True

def _handle_files(c, a):
    from vmpc.commands.files import run_files_command
    run_files_command(c, a); return True

def _handle_chats(c, a):
    from vmpc.commands.chats import run_chats_command
    run_chats_command(c, a); return True

def _handle_title(c, a):
    from vmpc.commands.chats import run_title_command
    run_title_command(c, a); return True

def _handle_reasoning(c, a):
    from vmpc.commands.builtins import toggle_reasoning
    c.config_changed = toggle_reasoning(c, a) or c.config_changed
    return True

def _handle_mode(c, a):
    from vmpc.commands.mode import run_mode_command
    c.config_changed = run_mode_command(c, a) or c.config_changed
    return True

def _handle_quit(c, a):
    c.should_exit = True
    return False

def _apply(c, a):
    from vmpc.commands.apply import run_apply_command
    run_apply_command(c, a); return True

COMMANDS = (
    SlashCommand("apply", "apply a code block from the last response", True, handler=_apply),
    SlashCommand("api", "add, switch or edit API endpoints", True, handler=_handle_api),
    SlashCommand("model", "choose the model for the active endpoint", True, handler=_handle_model),
    SlashCommand("context", "attach a folder or file for the model to read", True, aliases=("ctx",), handler=_handle_context),
    SlashCommand("files", "read, write, find or search local files", True, aliases=("file",), handler=_handle_files),
    SlashCommand("mode", "switch prompt mode, or write your own", True, handler=_handle_mode),
    SlashCommand("reasoning", "toggle streaming of the model's reasoning channel", True, handler=_handle_reasoning),
    SlashCommand("new", "start a fresh conversation", handler=_handle_new),
    SlashCommand("chats", "reopen a saved conversation", True, aliases=("resume",), handler=_handle_chats),
    SlashCommand("title", "rename this chat", True, handler=_handle_title),
    SlashCommand("status", "show the active endpoint and token usage", True, available_during_task=True, handler=_handle_status),
    SlashCommand("clear", "clear the screen and start fresh", handler=_handle_clear),
    SlashCommand("help", "list commands", available_during_task=True, handler=_handle_help),
    SlashCommand("quit", "exit vmpc", aliases=("exit",), available_during_task=True, handler=_handle_quit),
)


def _dev(name, description, handler_name, **kwargs):
    def dispatch(c, a):
        from vmpc.commands import dev
        getattr(dev, handler_name)(c, a); return True
    return SlashCommand(name, description, handler=dispatch, group=DEV_GROUP, **kwargs)

DEV_COMMANDS = (
    _dev("raw", "the last reply's exact source, before rendering", "show_raw", supports_inline_args=True),
    _dev("render", "run markdown through the renderer and show the result", "render_sample", supports_inline_args=True),
    _dev("wire", "the exact HTTP request a turn would send, key masked", "show_wire", supports_inline_args=True),
    _dev("ping", "measure latency to first token and throughput", "ping_endpoint"),
    _dev("palette", "every theme style, and what this terminal supports", "show_palette"),
    _dev("prompt", "the full system prompt currently in effect", "show_prompt"),
)
ALL_COMMANDS = COMMANDS + DEV_COMMANDS

@dataclass
class CommandRegistry:
    config: Any = None
    def custom_modes(self): return tuple(getattr(self.config, "modes", ()) or ())
    def modes(self): return all_modes(self.custom_modes())
    def active_mode(self): return find_mode(str(getattr(self.config, "mode", "") or ""), self.custom_modes()) if getattr(self.config, "mode", "") else None
    def groups(self): return unlocked_groups(self.active_mode())
    def mode_commands(self):
        active = self.active_mode(); out = []
        for mode in self.modes():
            out.append(SlashCommand(mode.name, mode.description, handler=partial(_handle_mode_switch, mode.name)))
        return tuple(out)
    def visible(self): return COMMANDS + tuple(c for c in DEV_COMMANDS if c.group in self.groups()) + self.mode_commands()
    def find(self, name):
        lowered = name.lower().lstrip("/")
        return next((c for c in self.visible() if c.name == lowered or lowered in c.aliases), None)
    def locked(self, name):
        lowered = name.lower().lstrip("/")
        return next((c for c in ALL_COMMANDS if c.group and c.group not in self.groups() and (c.name == lowered or lowered in c.aliases)), None)
    def parse(self, line):
        if not line.startswith("/"): return None
        head, _, rest = line[1:].partition(" ")
        command = self.find(head)
        return (command, rest.strip()) if command else None
    def reserved_names(self):
        names = set()
        for command in ALL_COMMANDS:
            names.add(command.name); names.update(command.aliases)
        return frozenset(names)


def _handle_mode_switch(name, context, args):
    from vmpc.commands.mode import activate_mode
    context.config_changed = activate_mode(context, name) or context.config_changed
    return True
