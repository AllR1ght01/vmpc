"""The interactive REPL.

Structure of one iteration:

1. Read a message with the composer (prompt_toolkit owns the bottom of the
   screen while it is active).
2. If it is a slash command, dispatch and loop.
3. Otherwise run a turn through :func:`vmpc.streaming.pump.run_turn`, which
   spawns the network thread and paces committed lines into scrollback.

The screen is never taken over: no alternate screen, no full-screen layout. The
transcript is the terminal's own scrollback, so selection, copy and mouse scroll
keep working, and quitting leaves the conversation on screen. That is the single
most valuable idea in the Codex TUI and it costs nothing to adopt.
"""

from __future__ import annotations

import signal
import threading
from functools import partial
from typing import Optional

from rich.console import Console
from rich.text import Text

from vmpc import __version__
from vmpc.api.client import stream_agent_chat
from vmpc.api.events import ApiError
from vmpc.chats import ChatRecord, new_id, sanitize_title, save_chat, title_for
from vmpc.commands import CommandContext, CommandRegistry
from vmpc.config import Config
from vmpc.session import Session
from vmpc.streaming.pump import run_turn
from vmpc.style import VMPC_THEME
from vmpc.ui.composer import Composer


def make_console(force_terminal: Optional[bool] = None) -> Console:
    """Build the one Console the whole app draws through."""
    return Console(
        theme=VMPC_THEME,
        force_terminal=force_terminal,
        # soft_wrap off by default: rendered markdown lines are already laid out
        # to the measured width, and letting rich re-wrap them would double-wrap.
        soft_wrap=False,
        highlight=False,
    )


class App:
    def __init__(self, config: Config, console: Optional[Console] = None) -> None:
        self.config = config
        self.console = console or make_console()
        self.session = Session()
        self.registry = CommandRegistry(config)
        # The mode is persisted, so a run starts in whichever one was left on.
        # Applying it here rather than lazily means the very first turn already
        # carries the right prompt.
        self.session.set_mode(self.registry.active_mode())
        # Built on the first prompt rather than here. prompt_toolkit binds to the
        # terminal the moment a PromptSession exists, so constructing one in
        # __init__ would make an App unbuildable anywhere there is no console —
        # including in a test, which is where most of this class gets exercised.
        self.composer: Optional[Composer] = None
        # Assigned on the first save rather than here, so a run where nobody said
        # anything leaves no file behind and the id's timestamp is the time of the
        # first message rather than of launch.
        self.chat_id: str = ""
        self.chat_created: float = 0.0
        self._namer: Optional[_Namer] = None

    def adopt(self, record) -> None:  # noqa: ANN001 - vmpc.chats.ChatRecord
        """Continue a saved chat instead of starting an empty one."""
        record.into_session(self.session)
        self.chat_id = record.id
        self.chat_created = record.created
        self._namer = None
        # The chat stored the folders, not their contents, so they are read again
        # here: resuming yesterday's conversation about a project should see the
        # project as it is today.
        self.attach_context()

    def attach_context(self, announce_errors: bool = True) -> None:
        """Re-read every attached path into the session's context text.

        Never fatal. A folder that has been moved or deleted since is worth one
        line on screen — the conversation is still perfectly usable without it,
        and refusing to open a chat because a path went stale would not be.
        """
        if not getattr(self.session, "context_paths", None):
            return
        from vmpc import context as context_module

        try:
            bundles, errors = context_module.apply(self.session)
        except OSError as exc:  # noqa: PERF203 - one message beats a traceback
            self.console.print(Text(f"  context not read: {exc}", style="error"))
            return
        if announce_errors:
            for problem in errors:
                self.console.print(Text(f"  ✖ context: {problem}", style="error"))
        files = sum(len(bundle.files) for bundle in bundles)
        if files:
            self.console.print(
                Text("  ✿ context ", style="hint")
                .append(f"{files} file{'' if files == 1 else 's'}", style="primary")
                .append(
                    f" from {len(bundles)} path{'' if len(bundles) == 1 else 's'}",
                    style="secondary",
                )
            )

    def forget_chat(self) -> None:
        """Detach from the chat's file, so the next turn starts a new one.

        Called by ``/new`` and ``/clear``. A namer still in flight is dropped
        with it: its answer describes the conversation that was just discarded,
        and applying it would name the new one after the old one.
        """
        self.chat_id = ""
        self.chat_created = 0.0
        self._namer = None

    # -- run ---------------------------------------------------------------

    def run(self) -> int:
        if self.composer is None:
            self.composer = Composer(registry=self.registry)
        self._print_banner()
        while True:
            # Before the prompt, not after the turn: the namer runs while the
            # reply is still being read, and this is the first moment where
            # printing cannot land in the middle of the streamed answer.
            self._collect_title()
            try:
                line = self.composer.prompt(placeholder=self._placeholder())
            except Exception as exc:  # noqa: BLE001 - never die on an input glitch
                self.console.print(Text(f"input error: {exc}", style="error"))
                return 1

            if line is None:
                self.console.print(Text("  bye", style="secondary"))
                return 0
            line = line.strip()
            if not line:
                continue

            if line.startswith("/"):
                if self._dispatch(line):
                    continue
                return 0

            self._turn(line)

    # -- commands ----------------------------------------------------------

    def _dispatch(self, line: str) -> bool:
        """Run a slash command. Returns False when the app should exit."""
        parsed = self.registry.parse(line)
        if parsed is None:
            name = line.split()[0]
            # A command that exists but is not unlocked gets its own message.
            # "unknown command /wire" would be a lie, and would send someone
            # looking for a typo instead of for the mode that reveals it.
            locked = self.registry.locked(name)
            if locked is not None:
                self.console.print(
                    Text(f"  {locked.display} needs ", style="secondary").append(
                        f"/{locked.group}", style="hint"
                    )
                )
                return True
            self.console.print(Text(f"  unknown command {name}", style="error"))
            self.console.print(Text("  /help — list commands", style="secondary"))
            return True

        command, args = parsed
        if command.handler is None:
            self.console.print(
                Text(f"  {command.display} is not implemented yet", style="secondary")
            )
            return True

        context = CommandContext(
            console=self.console,
            config=self.config,
            session=self.session,
            registry=self.registry,
            # /chats and /title need to swap the conversation and rename its
            # file, which is the App's bookkeeping, not the Session's.
            extras={"app": self},
        )
        keep_running = command.handler(context, args)
        return keep_running and not context.should_exit

    # -- one turn ----------------------------------------------------------

    def _turn(self, message: str) -> None:
        provider = self.config.active_provider()
        if provider is None:
            self.console.print(Text("  no endpoint configured", style="error"))
            self.console.print(Text("  /api add — configure one", style="hint"))
            return
        problems = provider.validate()
        if problems:
            self.console.print(Text(f"  {provider.name}: {problems[0]}", style="error"))
            self.console.print(Text("  /api edit — fix it", style="hint"))
            return

        self.session.add_user(message)
        cancel = threading.Event()

        source = partial(
            _stream_source,
            provider,
            list(self.session.messages),
            # full_system(), not system: attached files are part of what the model
            # is told and are deliberately not folded into the stored prompt.
            self.session.full_system(),
        )

        self.console.print()
        with _interrupt_guard(cancel):
            result = run_turn(
                self.console,
                source,
                header="Working",
                show_reasoning=provider.reasoning,
                cancel=cancel,
            )

        if result.error is not None:
            self._report_error(result.error)
            self.session.drop_last_user()
            return

        if result.interrupted:
            self.console.print(Text("  interrupted", style="secondary"))
            if result.text:
                # Keep partial output in context; the user saw it, so pretending
                # it never happened would make the next turn confusing.
                self.session.add_assistant(result.text)
            else:
                self.session.drop_last_user()
            return

        if result.text:
            self.session.add_assistant(result.text)
        else:
            self.console.print(Text("  (empty response)", style="secondary"))
            self.session.drop_last_user()

        if result.usage.total:
            self.session.total_tokens += result.usage.total
        self.console.print()
        self._persist()
        self._start_namer(provider)

    # -- saving and naming -------------------------------------------------

    def _persist(self) -> None:
        """Write the conversation to its file. Never fatal.

        A chat that cannot be saved is worth one warning and not worth ending the
        session over — the transcript the user came for is on screen either way.
        """
        if not self.session.messages:
            return
        provider = self.config.active_provider()
        try:
            if not self.chat_id:
                self.chat_id = new_id()
            record = ChatRecord.from_session(
                self.session,
                self.chat_id,
                provider_name=getattr(provider, "name", ""),
                model=getattr(provider, "model", ""),
                created=self.chat_created,
            )
            self.chat_created = record.created
            save_chat(record)
        except (OSError, ValueError) as exc:
            if not getattr(self, "_save_warned", False):
                self._save_warned = True
                self.console.print(Text(f"  chat not saved: {exc}", style="error"))

    def _start_namer(self, provider) -> None:  # noqa: ANN001
        """Ask the model for a name, off the main thread.

        Off-thread because the name is worth nothing and the prompt is worth
        everything: a namer against a slow endpoint would otherwise add its
        latency to the gap between the answer and the next prompt, which is the
        one place in a chat CLI where a stall is unmissable.
        """
        if self.session.title or self._namer is not None:
            return
        if len(self.session.messages) < 2:
            return
        self._namer = _Namer(provider, list(self.session.messages))
        self._namer.start()

    def _collect_title(self) -> None:
        """Adopt a finished name, if one arrived."""
        namer = self._namer
        if namer is None or namer.is_alive():
            return
        self._namer = None
        title = namer.title
        if not title or self.session.title:
            return
        self.set_title(title)

    def set_title(self, title: str, announce: bool = True) -> None:
        """Name the current chat, on screen and on disk."""
        cleaned = sanitize_title(title)
        if not cleaned:
            return
        self.session.title = cleaned
        self._persist()
        if announce:
            self.console.print(
                Text("  ✿ ", style="hint").append(cleaned, style="header")
            )
            self.console.print()
        # The tab is the one place a name is useful without being asked for: it
        # is how you find this window among six others.
        try:
            self.console.set_window_title(f"vmpc · {cleaned}")
        except Exception:  # noqa: BLE001 - a terminal that refuses is not an error
            pass

    def _report_error(self, error: ApiError) -> None:
        self.console.print(Text(f"  ✖ {error}", style="error"))
        if error.hint:
            self.console.print(Text(f"    {error.hint}", style="secondary"))

    # -- chrome ------------------------------------------------------------

    def _print_banner(self) -> None:
        """Draw the opening block: name, endpoint, and how to get help.

        Laid out against a left rail rather than a full box: a box would have to
        be measured against the terminal width and would break on resize, while
        a rail is width-independent and still groups the three lines. It is
        printed into scrollback like everything else, so it stays at the top of
        the session as a record of which endpoint the conversation ran against.
        """
        provider = self.config.active_provider()

        title = Text("  ╭─ ", style="secondary")
        title.append("✿ ", style="hint")
        title.append("vmpc", style="agent")
        title.append(f" {__version__}", style="secondary")
        self.console.print(title)

        # A resumed chat prints its name here, so a run that continued something
        # says what it continued instead of looking like a fresh session that
        # mysteriously remembers.
        name = getattr(getattr(self, "session", None), "title", "")
        if name:
            row = Text("  │  ", style="secondary")
            row.append(name, style="header")
            self.console.print(row)

        if provider is not None:
            row = Text("  │  ", style="secondary")
            row.append(provider.name, style="header")
            if provider.model:
                row.append("  ·  ", style="secondary")
                row.append(provider.model, style="hint")
            self.console.print(row)
        else:
            row = Text("  │  ", style="secondary")
            row.append("no endpoint yet — run ", style="secondary")
            row.append("/api", style="hint")
            self.console.print(row)

        # A mode rewrites the system prompt, so it gets its own line rather than
        # living only in /status: a prompt in effect that nothing on screen
        # mentions is indistinguishable from the model behaving oddly.
        mode = getattr(self, "registry", None) and self.registry.active_mode()
        if mode is not None:
            row = Text("  │  ", style="secondary")
            row.append("● ", style="success")
            row.append(f"/{mode.name}", style="header")
            row.append(f"  {mode.description}", style="secondary")
            self.console.print(row)

        # Attached files are re-sent on every turn, which is exactly the kind of
        # thing that should not be invisible.
        paths = list(getattr(getattr(self, "session", None), "context_paths", []) or [])
        if paths:
            row = Text("  │  ", style="secondary")
            row.append("◆ ", style="hint")
            row.append(paths[0] if len(paths) == 1 else f"{len(paths)} paths", style="header")
            row.append("  attached", style="secondary")
            row.truncate(max(self.console.width - 1, 20), overflow="ellipsis")
            self.console.print(row)

        footer = Text("  ╰─ ", style="secondary")
        footer.append("/help", style="hint")
        footer.append(" for commands", style="secondary")
        self.console.print(footer)
        self.console.print()

    def _placeholder(self) -> str:
        provider = self.config.active_provider()
        if provider is None:
            return "run /api to configure an endpoint"
        return "ask anything · / for commands"


def _stream_source(provider, messages, system, cancel):  # noqa: ANN001
    """Adapter matching the signature :func:`run_turn` expects."""
    return stream_agent_chat(provider, messages, system=system, cancel=cancel)


class _Namer(threading.Thread):
    """Runs :func:`vmpc.chats.title_for` once and holds the answer.

    A thread with an attribute rather than a queue or a callback: the main loop
    reads :attr:`title` only after :meth:`is_alive` says the thread is done, and
    a completed thread's write is already visible to the reader that joined it,
    so there is nothing to synchronize. Daemon, because a name is never worth
    delaying exit for.
    """

    def __init__(self, provider, messages) -> None:  # noqa: ANN001
        super().__init__(name="vmpc-namer", daemon=True)
        self._provider = provider
        self._messages = messages
        self.title: str = ""

    def run(self) -> None:
        self.title = title_for(self._provider, self._messages)


class _interrupt_guard:
    """Route Ctrl+C to the cancel event while a turn is streaming.

    The composer is not active during a turn, so prompt_toolkit is not reading
    keys and cannot deliver the interrupt. Swapping SIGINT for the duration is
    the portable way to get one: the display loop sleeps in short slices, which
    gives CPython a chance to run the handler promptly even on Windows, where
    signal delivery only happens between bytecodes in the main thread.
    """

    def __init__(self, cancel: threading.Event) -> None:
        self._cancel = cancel
        self._previous = None

    def __enter__(self) -> "_interrupt_guard":
        try:
            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle)
        except ValueError:
            # Not the main thread; the caller keeps default behavior.
            self._previous = None
        return self

    def _handle(self, signum, frame) -> None:  # noqa: ANN001
        self._cancel.set()

    def __exit__(self, *exc_info) -> None:
        if self._previous is not None:
            try:
                signal.signal(signal.SIGINT, self._previous)
            except ValueError:
                pass
