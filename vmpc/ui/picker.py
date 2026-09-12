"""Inline selection lists and text fields.

These deliberately do not use prompt_toolkit's dialog helpers. Dialogs take over
the screen with a bordered box and clear it on exit, which loses the choice from
scrollback. The Codex TUI instead draws a compact list in place, leaves the
resolved answer in the transcript, and never enters the alternate screen — so a
wizard reads afterwards like a record of what you picked.

Everything here is synchronous and returns plain values. ``None`` means the user
cancelled with Esc or Ctrl+C, and callers are expected to treat that as "abort
the whole flow", not "use a default".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import PromptSession

from vmpc.strings import t
from vmpc.style import ptk_style
from vmpc.ui.clipboard import paste_bindings


@dataclass
class Choice:
    """One row in a picker."""

    value: str
    label: str
    description: str = ""
    #: Shown right-aligned in the row, e.g. "current" or "default".
    badge: str = ""


def select(
    title: str,
    choices: Sequence[Choice],
    initial: int = 0,
    hint: Optional[str] = None,
) -> Optional[str]:
    """Show an inline list and return the chosen value, or None if cancelled."""
    if not choices:
        return None
    if hint is None:
        # Resolved here, not as the default value above: a default is bound at
        # import time, before anything could have called set_ui_language, and
        # would freeze the wizard's hints in whatever language happened to be
        # active first.
        hint = t("picker.select_hint")

    index = max(0, min(initial, len(choices) - 1))
    label_width = max(len(choice.label) for choice in choices)

    def render() -> FormattedText:
        fragments: list[tuple[str, str]] = []
        if title:
            fragments.append(("class:popup.title", f"{title}\n"))
        for position, choice in enumerate(choices):
            selected = position == index
            cursor = "❯ " if selected else "  "
            row_style = "class:popup.row.selected" if selected else "class:popup.row"
            desc_style = (
                "class:popup.desc.selected" if selected else "class:popup.desc"
            )
            fragments.append(("class:popup.cursor" if selected else "", cursor))
            fragments.append((row_style, choice.label.ljust(label_width)))
            if choice.badge:
                fragments.append(("class:popup.badge", f"  {choice.badge}"))
            if choice.description:
                fragments.append((desc_style, f"   {choice.description}"))
            fragments.append(("", "\n"))
        if hint:
            fragments.append(("class:popup.hint", hint))
        return FormattedText(fragments)

    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("c-p")
    @bindings.add("k")
    def _up(event) -> None:  # noqa: ANN001
        nonlocal index
        index = (index - 1) % len(choices)
        event.app.invalidate()

    @bindings.add("down")
    @bindings.add("c-n")
    @bindings.add("j")
    def _down(event) -> None:  # noqa: ANN001
        nonlocal index
        index = (index + 1) % len(choices)
        event.app.invalidate()

    @bindings.add("enter")
    def _accept(event) -> None:  # noqa: ANN001
        event.app.exit(result=choices[index].value)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    def _cancel(event) -> None:  # noqa: ANN001
        event.app.exit(result=None)

    # Number keys jump straight to a row, which is faster than arrowing for
    # short lists and costs nothing for long ones.
    for position in range(min(len(choices), 9)):

        @bindings.add(str(position + 1))
        def _jump(event, target: int = position) -> None:  # noqa: ANN001
            event.app.exit(result=choices[target].value)

    height = len(choices) + (1 if title else 0) + (1 if hint else 0)
    application: Application = Application(
        layout=Layout(
            HSplit([Window(FormattedTextControl(render), height=height, wrap_lines=False)])
        ),
        key_bindings=bindings,
        style=ptk_style(),
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    try:
        return application.run()
    except (KeyboardInterrupt, EOFError):
        return None


def prompt_text(
    label: str,
    default: str = "",
    placeholder: str = "",
    password: bool = False,
    validate: Optional[Callable[[str], Optional[str]]] = None,
    allow_empty: bool = False,
) -> Optional[str]:
    """Prompt for one line of text.

    ``placeholder`` is shown greyed out while the field is empty and is *not* a
    default — it disappears on the first keystroke and an empty submission does
    not adopt it. ``default`` is real prefilled text the user can edit.

    ``validate`` returns an error string to reject and re-prompt, or None to
    accept.
    """
    session: PromptSession = PromptSession(
        style=ptk_style(),
        # Without this, Ctrl+V is swallowed by prompt_toolkit's basic bindings —
        # which is worst exactly here, in the masked api-key field, where a paste
        # that did nothing and a paste that worked look the same.
        key_bindings=paste_bindings(single_line=True),
    )
    placeholder_text = (
        FormattedText([("class:placeholder", placeholder)]) if placeholder else None
    )

    while True:
        try:
            value = session.prompt(
                FormattedText([("class:prompt", f"{label} ")]),
                default=default,
                placeholder=placeholder_text,
                is_password=password,
            )
        except (KeyboardInterrupt, EOFError):
            return None

        value = value.strip()
        if not value and not allow_empty:
            # Empty submission with nothing prefilled reads as "cancel".
            if not default:
                return None
            value = default
        if validate is not None:
            error = validate(value)
            if error:
                print_error(error)
                default = value
                continue
        return value


def prompt_multiline(
    label: str,
    default: str = "",
    placeholder: str = "",
    hint: Optional[str] = None,
) -> Optional[str]:
    """Prompt for text that is expected to span lines, e.g. a system prompt.

    Enter submits and Alt+Enter inserts a newline — the same way round as the
    main composer. prompt_toolkit's own multiline default is the opposite, and
    having the two disagree would mean the key that sends a message somewhere
    else silently writes a newline here.
    """
    if hint is None:
        hint = t("picker.multiline_hint")
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event) -> None:  # noqa: ANN001
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def _newline(event) -> None:  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    paste_bindings(single_line=False, into=bindings)

    session: PromptSession = PromptSession(
        style=ptk_style(),
        multiline=True,
        key_bindings=bindings,
        prompt_continuation=lambda width, line_number, is_soft_wrap: FormattedText(
            [("class:continuation", "  " if is_soft_wrap else "┆ ")]
        ),
    )
    placeholder_text = (
        FormattedText([("class:placeholder", placeholder)]) if placeholder else None
    )
    if hint:
        print_hint(hint)
    try:
        value = session.prompt(
            FormattedText([("class:prompt", f"{label} ")]),
            default=default,
            placeholder=placeholder_text,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    value = value.strip()
    return value or (default.strip() or None)


def confirm(question: str, default: bool = False) -> Optional[bool]:
    """Yes/no as a two-row picker, so it looks like every other prompt."""
    choice = select(
        question,
        [
            Choice("yes", t("picker.yes")),
            Choice("no", t("picker.no")),
        ],
        initial=0 if default else 1,
        hint=t("picker.confirm_hint"),
    )
    if choice is None:
        return None
    return choice == "yes"


def print_error(message: str) -> None:
    """Report a validation failure between prompts.

    Uses prompt_toolkit's own printer rather than rich, so it cannot collide
    with an active prompt's redraw.
    """
    from prompt_toolkit import print_formatted_text

    print_formatted_text(
        FormattedText([("class:popup.error", f"  {message}")]),
        style=ptk_style(),
    )


def print_hint(message: str) -> None:
    """Say how a field works, just above it. Same printer as :func:`print_error`."""
    from prompt_toolkit import print_formatted_text

    print_formatted_text(
        FormattedText([("class:popup.hint", f"  {message}")]),
        style=ptk_style(),
    )
