"""Interactive wrapper around :mod:`vmpc.file_tools`.

Examples::

    /files read path/to/file.py
    /files find . *.py
    /files search . "TODO" --ignore-case
    /files write path/to/file.txt hello --force
"""

from __future__ import annotations

import shlex
from typing import Any

from rich.text import Text

from vmpc.commands import CommandContext
from vmpc.file_tools import (
    FileToolError,
    find_files,
    list_directory,
    read_file,
    search_files,
    write_file,
)


def run_files_command(command: CommandContext, args: str) -> None:
    console = command.console
    tokens = [token.strip("\"'`") for token in shlex.split(args, posix=False)]
    if not tokens:
        console.print(Text("  /files list|read|write|find|search ...", style="hint"))
        return
    action = tokens.pop(0).lower()
    try:
        if action == "list" and tokens:
            for item in list_directory(tokens[0]):
                size = "-" if item["size"] is None else str(item["size"])
                console.print(Text(
                    f"{item['type']:<9} {size:>10}  {item['path']}",
                    style="primary",
                ))
            return
        if action == "read" and tokens:
            result = read_file(tokens[0])
            console.print(Text(f"  {result['path']}  ({result['total_lines']} lines)", style="secondary"))
            # markup=False: the file's own bytes are data, and a stray "[/]"
            # in it would raise MarkupError rather than print.
            console.print(result["content"], markup=False, highlight=False)
            return
        if action == "find" and tokens:
            pattern = tokens[1] if len(tokens) > 1 else "*"
            for item in find_files(tokens[0], pattern):
                console.print(Text(item["path"], style="primary"))
            return
        if action == "search" and len(tokens) >= 2:
            root, query = tokens[0], tokens[1]
            ignore_case = "--ignore-case" in tokens[2:]
            regex = "--regex" in tokens[2:]
            for item in search_files(root, query, case_sensitive=not ignore_case, regex=regex):
                console.print(Text(f"{item['path']}:{item['line']}: {item['text']}", style="primary"))
            return
        if action == "write" and len(tokens) >= 2:
            force = "--force" in tokens[2:]
            content = " ".join(token for token in tokens[1:] if token != "--force")
            result = write_file(tokens[0], content, overwrite=force)
            console.print(Text(f"  wrote {result['bytes']} bytes to {result['path']}", style="success"))
            return
        console.print(Text("  usage: /files list DIR | read PATH | write PATH TEXT [--force] | find ROOT [GLOB] | search ROOT QUERY", style="hint"))
    except FileToolError as exc:
        console.print(Text(f"  ✖ {exc}", style="error"))


__all__ = ["run_files_command"]
