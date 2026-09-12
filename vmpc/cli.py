"""Entry point.

Hybrid shape: bare ``vmpc`` opens the interactive REPL, a subcommand runs once
and exits. Both share the same config, transport and renderer, so nothing about
the one-shot path is a second implementation.

    vmpc                      interactive
    vmpc -C ./src             interactive, with a folder attached for reading
    vmpc chat "question"      one question, rendered
    vmpc api [add|list|…]     manage endpoints
    vmpc models [list|probe]  list what the endpoint offers, or find out
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from functools import partial
from typing import Optional

from rich.text import Text

from vmpc import __version__
from vmpc.api.client import stream_chat
from vmpc.config import ConfigError, load_config
from vmpc.strings import set_ui_language, t
from vmpc.streaming.pump import run_turn, stream_to_stdout
from vmpc.ui.app import App, make_console


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vmpc",
        description="A terminal agent CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"vmpc {__version__}")
    parser.add_argument(
        "--provider",
        metavar="NAME",
        help="use this endpoint instead of the active one",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_chat",
        nargs="?",
        const="",
        metavar="CHAT",
        help="reopen the most recent chat, or one named by id or by part of its name",
    )
    parser.add_argument(
        "-C",
        "--context",
        dest="context",
        action="append",
        metavar="PATH",
        help="attach a folder or file for the model to read; repeatable",
    )

    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="send one message and print the reply")
    chat.add_argument("message", nargs="*", help="the message; reads stdin if omitted")
    chat.add_argument(
        "--raw",
        action="store_true",
        help="write the model's own bytes to stdout, no markdown rendering",
    )
    chat.add_argument(
        "--reasoning",
        action="store_true",
        help="stream the reasoning channel too",
    )

    api = sub.add_parser("api", help="manage API endpoints")
    api.add_argument(
        "args",
        nargs="*",
        help="add | list | use NAME | edit NAME | remove NAME | test NAME",
    )

    models = sub.add_parser("models", help="list models the active endpoint offers")
    models.add_argument(
        "args",
        nargs="*",
        help="list | probe [NAMES|FILE]",
    )

    chats = sub.add_parser("chats", help="list saved conversations")
    chats.add_argument(
        "args",
        nargs="*",
        help="list | rm ID",
    )

    return parser


def _force_utf8_io() -> None:
    """Make stdout/stderr able to carry the text we actually emit.

    On Windows a redirected stream defaults to the ANSI code page (cp1252 here),
    which cannot encode the glyphs the renderer uses — or an em-dash out of the
    model. ``vmpc chat q > out.md`` would then die with UnicodeEncodeError partway
    through the stream. Reconfiguring to UTF-8 with ``errors="replace"`` means a
    redirect gets correct bytes and an unencodable character degrades to a
    placeholder instead of killing the turn.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "") in ("utf8", "utf8sig"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable TextIOWrapper (captured or wrapped stream).
            # Nothing to do; rich still substitutes rather than raising.
            pass


def _enable_windows_vt() -> None:
    """Turn on VT processing so the rose palette survives on Windows.

    Without this a Windows console reports no VT support, rich falls back to its
    16-color path, and every RGB shade in :mod:`vmpc.style` collapses onto white
    or grey — the theme silently stops existing. Windows 10+ consoles do support
    ANSI, they just do not admit it until asked.

    The current mode is read and OR-ed rather than assigned, so unrelated flags
    the terminal set for itself are preserved. Every failure path is a no-op: a
    redirected stream has no console mode, and non-Windows has no ``windll``.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | enable_vt)
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8_io()
    _enable_windows_vt()
    parser = build_parser()
    options = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"vmpc: {exc}", file=sys.stderr)
        return 1

    # Same interface language the REPL applies from config.ui, applied here too
    # so a one-shot `vmpc chat` after `/lang ru` prints its own messages in
    # Russian instead of quietly falling back to English.
    set_ui_language(str(config.ui.get("language", "") or ""))

    if options.provider:
        if config.get(options.provider) is None:
            print(t("cli.no_endpoint_named", name=options.provider), file=sys.stderr)
            return 1
        config.active = options.provider

    if options.command == "chat":
        return _chat(options, config)
    if options.command == "api":
        return _api(options, config)
    if options.command == "models":
        return _models(options, config)
    if options.command == "chats":
        return _chats(options)

    console = make_console()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # The REPL needs a real terminal: prompt_toolkit owns the bottom of the
        # screen and cannot attach to a pipe. Say so instead of letting it throw
        # a traceback from deep inside the library.
        print(t("cli.no_terminal"), file=sys.stderr)
        return 1

    app = App(config, console)
    extra_context = [path for path in (options.context or []) if path]
    if options.continue_chat is not None:
        record = _find_chat(options.continue_chat)
        if record is None:
            what = options.continue_chat or "anything"
            print(t("cli.no_saved_chat_matching", what=what), file=sys.stderr)
            return 1
        # Folded into the record rather than appended after adopt(): adopt reads
        # the attached folders itself, and adding them afterwards would read the
        # chat's own folders a second time and report them twice.
        record.context = list(record.context) + [
            path for path in extra_context if path not in record.context
        ]
        app.adopt(record)
    elif extra_context:
        app.session.context_paths.extend(extra_context)
        app.attach_context()
    try:
        return app.run()
    except KeyboardInterrupt:
        console.print()
        return 130


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _chat(options: argparse.Namespace, config) -> int:  # noqa: ANN001
    message = " ".join(options.message).strip()
    if not message:
        if sys.stdin.isatty():
            print(t("cli.nothing_to_send"), file=sys.stderr)
            return 2
        message = sys.stdin.read().strip()
    if not message:
        return 2

    provider = config.active_provider()
    if provider is None:
        print(t("cli.no_endpoint_configured"), file=sys.stderr)
        return 1
    problems = provider.validate()
    if problems:
        print(t("cli.provider_problem", name=provider.name, problem=problems[0]), file=sys.stderr)
        return 1

    if options.reasoning:
        provider = provider.copy(reasoning=True)

    from vmpc.session import DEFAULT_SYSTEM_PROMPT

    system = DEFAULT_SYSTEM_PROMPT
    if options.context:
        # Same attachment the REPL builds, so `vmpc -C ./src chat "..."` answers
        # about the same files an interactive session would see. Errors go to
        # stderr and the question still goes out: a mistyped path is not a reason
        # to throw the question away.
        from vmpc import context as context_module

        bundles, problems = context_module.load(options.context)
        for problem in problems:
            print(t("cli.context_problem", problem=problem), file=sys.stderr)
        block = context_module.render(bundles)
        if block:
            system = f"{system}\n\n{block}"

    source = partial(
        _source,
        provider,
        [{"role": "user", "content": message}],
        system,
    )

    # Piped output gets raw bytes; a terminal gets the rendered stream. Deciding
    # by isatty means `vmpc chat ... | jq` behaves and `vmpc chat ...` is pretty,
    # without the user having to remember a flag.
    raw = options.raw or not sys.stdout.isatty()
    if raw:
        result = stream_to_stdout(source, include_reasoning=options.reasoning)
    else:
        console = make_console()
        result = run_turn(
            console,
            source,
            show_reasoning=provider.reasoning,
            cancel=threading.Event(),
        )

    if result.error is not None:
        print(t("cli.error_prefix", error=result.error), file=sys.stderr)
        if result.error.hint:
            print(t("cli.error_hint", hint=result.error.hint), file=sys.stderr)
        return 1
    return 0


#: `api` subcommands that only print and so work fine in a pipe. Everything
#: else opens a picker or a field and needs a terminal.
_NON_INTERACTIVE_API = ("list", "test")


def _api(options: argparse.Namespace, config) -> int:  # noqa: ANN001
    from vmpc.commands.api import run_api_command

    args = " ".join(options.args)
    action = options.args[0] if options.args else ""
    if action not in _NON_INTERACTIVE_API and not (
        sys.stdin.isatty() and sys.stdout.isatty()
    ):
        label = f"'vmpc api {action}'" if action else t("cli.endpoint_menu_label")
        print(t("cli.api_needs_terminal", label=label), file=sys.stderr)
        return 1

    console = make_console()
    run_api_command(console, config, args)
    return 0


def _models(options: argparse.Namespace, config) -> int:  # noqa: ANN001
    from vmpc.commands.model import _list_models, known_models

    console = make_console()
    provider = config.active_provider()
    if provider is None:
        console.print(Text(t("cli.no_endpoint_short"), style="error"))
        return 1

    args = list(getattr(options, "args", []) or [])
    action = args[0].lower() if args else "list"

    if action in ("probe", "brute", "brute-force", "find", "search"):
        return _models_probe(console, config, provider, " ".join(args[1:]))
    if action not in ("list", "ls"):
        print(t("cli.unknown_models_action", action=action), file=sys.stderr)
        return 1

    live = _list_models(provider) or []
    saved = set(getattr(provider, "models", []) or [])
    # Saved names are listed alongside the advertised ones: they are the ones the
    # user confirmed work here, and a gateway that serves no catalogue at all
    # would otherwise report "nothing" about an endpoint that works fine.
    models = known_models(provider, live)
    if not models:
        console.print(
            Text(t("cli.did_not_advertise", name=provider.name), style="secondary")
        )
        console.print(
            Text(t("cli.models_probe_hint"), style="hint").append(
                t("cli.models_probe_hint_suffix"), style="secondary"
            )
        )
        return 1
    for model in models:
        current = model == provider.model
        where = t("model.saved_label") if model in saved else (t("model.advertised_label") if model in live else "")
        console.print(
            Text("● " if current else "  ", style="success" if current else "secondary")
            .append(model, style="primary")
            .append(f"  {where}" if where else "", style="secondary")
        )
    return 0


def _models_probe(console, config, provider, rest: str) -> int:  # noqa: ANN001
    """``vmpc models probe`` — the same sweep ``/model probe`` runs.

    Delegated rather than reimplemented so the one-shot path cannot drift from
    the interactive one; the confirm inside it is why a terminal is required.
    """
    from vmpc.commands.model import _probe

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # It sends a request per name to the user's own endpoint, so it asks
        # first. Without a terminal there is nobody to ask, and running it
        # unattended is not a decision to make on their behalf.
        print(t("cli.probe_needs_terminal"), file=sys.stderr)
        return 1
    _probe(console, config, provider, rest)
    return 0


def _find_chat(target: str):  # noqa: ANN201
    """Resolve ``--continue``'s argument: latest, an id, or part of a name."""
    from vmpc.chats import latest_chat, list_chats

    if not target:
        return latest_chat()
    records = list_chats(limit=0)
    for record in records:
        if record.id == target:
            return record
    lowered = target.lower()
    matches = [record for record in records if lowered in record.label.lower()]
    # An ambiguous fragment resolves to the most recent match rather than
    # failing: the list is ordered by recency, and "the one I meant" is almost
    # always the one touched last.
    return matches[0] if matches else None


def _chats(options: argparse.Namespace) -> int:
    from vmpc.chats import delete_chat, list_chats

    console = make_console()
    args = list(options.args)
    action = args[0].lower() if args else "list"

    if action in ("remove", "rm", "delete"):
        target = " ".join(args[1:]).strip()
        if not target:
            print(t("cli.which_chat"), file=sys.stderr)
            return 1
        record = _find_chat(target)
        if record is None:
            print(t("cli.no_saved_chat", target=target), file=sys.stderr)
            return 1
        if not delete_chat(record.id):
            print(t("cli.could_not_delete", id=record.id), file=sys.stderr)
            return 1
        console.print(Text(t("cli.deleted", label=record.label), style="secondary"))
        return 0

    if action != "list":
        print(t("cli.unknown_chats_action", action=action), file=sys.stderr)
        return 1

    records = list_chats(limit=0)
    if not records:
        console.print(Text(t("cli.no_saved_chats"), style="secondary"))
        return 0
    width = min(max(len(record.label) for record in records), 48)
    for record in records:
        console.print(
            Text(record.label.ljust(width), style="primary")
            .append(f"  {record.id}", style="secondary")
            .append(f"  {record.turns}t", style="secondary")
        )
    return 0


def _source(provider, messages, system, cancel):  # noqa: ANN001
    return stream_chat(provider, messages, system=system, cancel=cancel)


if __name__ == "__main__":
    raise SystemExit(main())
