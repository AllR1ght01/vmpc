"""The input composer.

A :class:`prompt_toolkit.PromptSession` configured to behave the way the Codex
composer does, without reimplementing a text editor:

- Enter sends, Alt+Enter (and Ctrl+J) inserts a newline. This is the right way
  round for a chat prompt — the common action gets the unmodified key.
- Ctrl+C clears a non-empty buffer, and quits only when pressed twice on an
  empty one, so a stray interrupt never loses a half-written message.
- A ``/`` at the start of the line opens the command popup, driven by the same
  table that drives ``/help``.
- History persists across runs, with Up/Down searching on the typed prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from vmpc.commands import CommandRegistry
from vmpc.config import default_config_path
from vmpc.style import ptk_style
from vmpc.ui.clipboard import paste_bindings


class SlashCompleter(Completer):
    """Completes ``/commands`` at the start of the buffer only.

    Asks the registry on every keystroke rather than caching a list: the set of
    commands depends on the active mode, and ``/dev`` has to change the popup
    without rebuilding the composer.
    """

    def __init__(self, registry: "CommandRegistry") -> None:
        self._registry = registry

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # Only while typing the command word itself; after a space the user is
        # writing arguments and a popup would be in the way.
        if " " in text:
            return
        prefix = text[1:].lower()
        for command in self._registry.visible():
            names = (command.name,) + command.aliases
            match = next((n for n in names if n.startswith(prefix)), None)
            if match is None:
                continue
            yield Completion(
                match,
                start_position=-len(prefix),
                display=f"/{match}",
                display_meta=command.description,
            )


class Composer:
    """Owns the prompt session and its key bindings."""

    def __init__(
        self, vi_mode: bool = False, registry: "Optional[CommandRegistry]" = None
    ) -> None:
        self._interrupt_armed = False
        self.registry = registry if registry is not None else CommandRegistry()
        self.session: PromptSession = PromptSession(
            history=_history(),
            completer=SlashCompleter(self.registry),
            auto_suggest=AutoSuggestFromHistory(),
            complete_while_typing=True,
            key_bindings=self._bindings(),
            style=ptk_style(),
            editing_mode=EditingMode.VI if vi_mode else EditingMode.EMACS,
            multiline=True,
            prompt_continuation=self._continuation,
            enable_history_search=True,
            reserve_space_for_menu=6,
        )

    # -- prompt ------------------------------------------------------------

    def prompt(self, placeholder: str = "") -> Optional[str]:
        """Read one message. Returns None when the user wants to quit."""
        placeholder_text = (
            FormattedText([("class:placeholder", placeholder)]) if placeholder else None
        )
        try:
            text = self.session.prompt(
                FormattedText([("class:prompt", "❯ ")]),
                placeholder=placeholder_text,
            )
        except KeyboardInterrupt:
            # Reached when the buffer was already empty; the binding below
            # handles the non-empty case without raising.
            if self._interrupt_armed:
                return None
            self._interrupt_armed = True
            return ""
        except EOFError:
            return None
        self._interrupt_armed = False
        return text

    @staticmethod
    def _continuation(width: int, line_number: int, is_soft_wrap: bool):
        # Align continuation lines under the prompt glyph so a multiline message
        # reads as one block. A soft wrap is the terminal's doing, not the
        # user's, so it gets blank lead-in; a real newline gets a mark.
        return FormattedText([("class:continuation", "  " if is_soft_wrap else "┆ ")])

    # -- keys --------------------------------------------------------------

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @Condition
        def buffer_has_text() -> bool:
            from prompt_toolkit.application.current import get_app

            return bool(get_app().current_buffer.text.strip())

        @bindings.add("enter")
        def _submit(event) -> None:  # noqa: ANN001
            buffer = event.current_buffer
            if buffer.complete_state and buffer.complete_state.current_completion:
                buffer.apply_completion(buffer.complete_state.current_completion)
                return
            buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def _newline(event) -> None:  # noqa: ANN001
            event.current_buffer.insert_text("\n")

        @bindings.add("c-c", filter=buffer_has_text)
        def _clear_buffer(event) -> None:  # noqa: ANN001
            # A first Ctrl+C on a written message clears it rather than quitting.
            event.current_buffer.reset()
            self._interrupt_armed = False

        @bindings.add("c-d", filter=~buffer_has_text)
        def _eof(event) -> None:  # noqa: ANN001
            event.app.exit(exception=EOFError, style="class:exiting")

        # Multiline, so a pasted block keeps its newlines: the composer is where
        # a stack trace or a diff gets pasted, and flattening it would be worse
        # than not pasting at all.
        paste_bindings(single_line=False, into=bindings)

        return bindings


def _history():
    """Persist history next to the config, falling back to memory."""
    if os.environ.get("VMPC_NO_HISTORY"):
        return InMemoryHistory()
    path = default_config_path().parent / "history"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileHistory(str(path))
    except OSError:
        return InMemoryHistory()


def history_path() -> Path:
    return default_config_path().parent / "history"
