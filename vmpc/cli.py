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
from vmpc.api.client import stream_agent_chat
from vmpc.config import ConfigError, load_config
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
        "--gui",
        action="store_true",
        help="open the desktop interface instead of the terminal interface",
    )
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

    files = sub.add_parser("files", help="list, read, write and search local files")
    file_sub = files.add_subparsers(dest="file_action")
    file_list = file_sub.add_parser("list", help="list a directory")
    file_list.add_argument("path")
    file_list.add_argument("--max-entries", type=int, default=200)
    file_read = file_sub.add_parser("read", help="read a UTF-8 text file")
    file_read.add_argument("path")
    file_read.add_argument("--start-line", type=int, default=1)
    file_read.add_argument("--max-lines", type=int)
    file_write = file_sub.add_parser("write", help="write a UTF-8 text file")
    file_write.add_argument("path")
    file_write.add_argument("--content")
    file_write.add_argument("--stdin", action="store_true", help="read content from stdin")
    file_write.add_argument("--force", action="store_true", help="replace an existing file")
    file_write.add_argument("--mkdir", action="store_true", help="create missing parent directories")
    file_find = file_sub.add_parser("find", help="find files by glob")
    file_find.add_argument("root")
    file_find.add_argument("pattern", nargs="?", default="*")
    file_find.add_argument("--max-results", type=int, default=200)
    file_search = file_sub.add_parser("search", help="search text files")
    file_search.add_argument("root")
    file_search.add_argument("query")
    file_search.add_argument("--pattern", default="*")
    file_search.add_argument("--ignore-case", action="store_true")
    file_search.add_argument("--regex", action="store_true")
    file_search.add_argument("--max-results", type=int, default=200)

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

    # Filesystem utilities are local and must remain usable before an API
    # endpoint is configured (or when its config is temporarily invalid).
    if options.command == "files":
        return _files(options)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"vmpc: {exc}", file=sys.stderr)
        return 1

    if options.provider:
        if config.get(options.provider) is None:
            print(f"vmpc: no endpoint named {options.provider!r}", file=sys.stderr)
            return 1
        config.active = options.provider

    if options.gui:
        from vmpc.gui import run_gui

        return run_gui(config)

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
        print(
            "vmpc: the interactive mode needs a terminal.\n"
            "  run 'vmpc' directly in a terminal, or use a subcommand:\n"
            "    vmpc chat \"your question\"\n"
            "    vmpc api list",
            file=sys.stderr,
        )
        return 1

    app = App(config, console)
    extra_context = [path for path in (options.context or []) if path]
    if options.continue_chat is not None:
        record = _find_chat(options.continue_chat)
        if record is None:
            what = options.continue_chat or "anything"
            print(f"vmpc: no saved chat matching {what!r}", file=sys.stderr)
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
            print("vmpc: nothing to send", file=sys.stderr)
            return 2
        message = sys.stdin.read().strip()
    if not message:
        return 2

    provider = config.active_provider()
    if provider is None:
        print("vmpc: no endpoint configured — run 'vmpc api add'", file=sys.stderr)
        return 1
    problems = provider.validate()
    if problems:
        print(f"vmpc: {provider.name}: {problems[0]}", file=sys.stderr)
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
            print(f"vmpc: context: {problem}", file=sys.stderr)
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
        print(f"vmpc: {result.error}", file=sys.stderr)
        if result.error.hint:
            print(f"  {result.error.hint}", file=sys.stderr)
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
        label = f"'vmpc api {action}'" if action else "the endpoint menu"
        print(
            f"vmpc: {label} asks questions, so it needs a terminal.\n"
            "  these work anywhere:\n"
            "    vmpc api list\n"
            "    vmpc api test NAME",
            file=sys.stderr,
        )
        return 1

    console = make_console()
    run_api_command(console, config, args)
    return 0


def _models(options: argparse.Namespace, config) -> int:  # noqa: ANN001
    from vmpc.commands.model import _list_models, known_models

    console = make_console()
    provider = config.active_provider()
    if provider is None:
        console.print(Text("no endpoint configured", style="error"))
        return 1

    args = list(getattr(options, "args", []) or [])
    action = args[0].lower() if args else "list"

    if action in ("probe", "brute", "brute-force", "find", "search"):
        return _models_probe(console, config, provider, " ".join(args[1:]))
    if action not in ("list", "ls"):
        print(f"vmpc: unknown 'models' action {action!r}", file=sys.stderr)
        return 1

    live = _list_models(provider) or []
    saved = set(getattr(provider, "models", []) or [])
    # Saved names are listed alongside the advertised ones: they are the ones the
    # user confirmed work here, and a gateway that serves no catalogue at all
    # would otherwise report "nothing" about an endpoint that works fine.
    models = known_models(provider, live)
    if not models:
        console.print(
            Text(f"{provider.name} did not advertise a model list", style="secondary")
        )
        console.print(
            Text("  vmpc models probe", style="hint").append(
                "  — find out by asking", style="secondary"
            )
        )
        return 1
    for model in models:
        current = model == provider.model
        where = "saved" if model in saved else ("advertised" if model in live else "")
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
        print(
            "vmpc: 'models probe' asks before sending, so it needs a terminal.\n"
            "  run it in a terminal, or use '/model probe' inside vmpc",
            file=sys.stderr,
        )
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
            print("vmpc: which chat? pass an id from 'vmpc chats'", file=sys.stderr)
            return 1
        record = _find_chat(target)
        if record is None:
            print(f"vmpc: no saved chat matching {target!r}", file=sys.stderr)
            return 1
        if not delete_chat(record.id):
            print(f"vmpc: could not delete {record.id}", file=sys.stderr)
            return 1
        console.print(Text(f"deleted {record.label}", style="secondary"))
        return 0

    if action != "list":
        print(f"vmpc: unknown 'chats' action {action!r}", file=sys.stderr)
        return 1

    records = list_chats(limit=0)
    if not records:
        console.print(Text("no saved chats yet", style="secondary"))
        return 0
    width = min(max(len(record.label) for record in records), 48)
    for record in records:
        console.print(
            Text(record.label.ljust(width), style="primary")
            .append(f"  {record.id}", style="secondary")
            .append(f"  {record.turns}t", style="secondary")
        )
    return 0


def _files(options: argparse.Namespace) -> int:
    """Run the dependency-free local filesystem tools without an API config."""
    import json

    from vmpc.file_tools import (
        FileToolError,
        find_files,
        list_directory,
        read_file,
        search_files,
        write_file,
    )

    action = getattr(options, "file_action", None)
    try:
        if action == "list":
            print(json.dumps(
                list_directory(options.path, max_entries=options.max_entries),
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        if action == "read":
            result = read_file(options.path, start_line=options.start_line, max_lines=options.max_lines)
            sys.stdout.write(result["content"])
            return 0
        if action == "write":
            if options.content is not None and options.stdin:
                print("vmpc: choose --content or --stdin, not both", file=sys.stderr)
                return 2
            if options.stdin:
                content = sys.stdin.read()
            elif options.content is not None:
                content = options.content
            else:
                print("vmpc: write needs --content or --stdin", file=sys.stderr)
                return 2
            print(json.dumps(write_file(options.path, content, overwrite=options.force, create_parents=options.mkdir), ensure_ascii=False))
            return 0
        if action == "find":
            print(json.dumps(find_files(options.root, options.pattern, max_results=options.max_results), ensure_ascii=False, indent=2))
            return 0
        if action == "search":
            print(json.dumps(search_files(options.root, options.query, pattern=options.pattern, case_sensitive=not options.ignore_case, regex=options.regex, max_results=options.max_results), ensure_ascii=False, indent=2))
            return 0
        print("vmpc: choose files read, write, find or search", file=sys.stderr)
        return 2
    except FileToolError as exc:
        print(f"vmpc: files: {exc}", file=sys.stderr)
        return 1


def _source(provider, messages, system, cancel):  # noqa: ANN001
    return stream_agent_chat(provider, messages, system=system, cancel=cancel)


if __name__ == "__main__":
    raise SystemExit(main())
