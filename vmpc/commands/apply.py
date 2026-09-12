"""Apply a fenced code block from the last assistant response."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from rich.text import Text
from vmpc.commands import CommandContext
from vmpc.file_tools import FileToolError, write_file

_BLOCK_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


def run_apply_command(context: CommandContext, args: str) -> None:
    console = context.console
    messages = getattr(context.session, "messages", [])
    response = next(
        (str(item.get("content", "")) for item in reversed(messages)
         if item.get("role") == "assistant"),
        "",
    )
    if not response:
        console.print(Text("  no assistant response to apply", style="error"))
        return
    try:
        tokens = shlex.split(args, posix=False)
    except ValueError as exc:
        console.print(Text(f"  ✖ invalid arguments: {exc}", style="error"))
        return
    force = "--force" in tokens
    paths = [x.strip("\"'`") for x in tokens if x != "--force"]
    blocks = [(m.group(1).strip(), m.group(2)) for m in _BLOCK_RE.finditer(response)]
    if not blocks:
        console.print(Text("  last response contains no fenced code block", style="error"))
        return

    selected: list[tuple[str, str]] = []
    if paths:
        selected = [(str(Path(paths[0])), blocks[0][1])]
    else:
        for info, code in blocks:
            parts = info.split()
            if len(parts) >= 2:
                selected.append((parts[-1], code))
        if not selected:
            console.print(Text("  usage: /apply PATH [--force]", style="hint"))
            return

    for path, code in selected:
        try:
            result = write_file(path, code, overwrite=force, create_parents=True)
        except FileToolError as exc:
            console.print(Text(f"  ✖ {exc}", style="error"))
            if not force:
                console.print(Text("  use /apply PATH --force to replace an existing file", style="hint"))
            continue
        console.print(Text(f"  applied {result['bytes']} bytes to {result['path']}", style="success"))
