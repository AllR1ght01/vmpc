"""The ``/context`` command — point vmpc at a folder and let it read.

    /context C:\\proj        attach a folder (or a single file)
    /context                 show what is attached, re-reading it from disk
    /context reload          same, said out loud
    /context rm C:\\proj     detach one path
    /context clear           detach everything

The attachment is rebuilt from disk on every attach, reload and resume, so a file
edited in another window is what the next turn sees. It rides in the system
prompt, which means it is re-sent every turn — the summary prints an approximate
token count for exactly that reason: the cost is per-question, not per-attach,
and it should not be a surprise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text

from vmpc import context as context_module
from vmpc.commands import CommandContext

#: What a path is stripped of before it is used. Pasting a Windows path from a
#: file manager brings the quotes with it, and ``"C:\\proj"`` is not a directory.
_QUOTES = "\"'` "


def run_context_command(command: CommandContext, args: str) -> None:
    console = command.console
    session = command.session
    if session is None:  # pragma: no cover - the App always passes one
        return

    raw = args.strip()
    head, _, rest = raw.partition(" ")
    action = head.lower()

    if not raw:
        _show(console, session, header="context")
        return
    if action in ("clear", "off", "none", "drop", "detach"):
        _clear(console, session)
        return
    if action in ("reload", "refresh", "reread", "sync"):
        _show(console, session, header="context reloaded")
        return
    if action in ("rm", "remove", "-", "del", "delete"):
        _detach(console, session, rest)
        return
    target = rest if action in ("add", "+", "attach") else raw
    _attach(console, session, target)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def _attach(console: Any, session: Any, target: str) -> None:
    cleaned = target.strip().strip(_QUOTES).strip()
    if not cleaned:
        console.print(Text("  which folder? /context PATH", style="secondary"))
        return
    try:
        bundle = context_module.collect(cleaned, budget=_remaining(session))
    except context_module.ContextError as exc:
        console.print(Text(f"  ✖ {exc}", style="error"))
        return

    # Stored resolved rather than as typed: the chat outlives this working
    # directory, and a relative path reopened from elsewhere would either miss or
    # — worse — quietly attach a different folder with the same name.
    stored = str(bundle.root)
    if stored in session.context_paths:
        console.print(Text(f"  already attached: {stored}", style="secondary"))
    else:
        session.context_paths.append(stored)

    bundles, errors = context_module.apply(session)
    _report(console, session, bundles, errors, header="context")


def _detach(console: Any, session: Any, target: str) -> None:
    cleaned = target.strip().strip(_QUOTES).strip()
    if not cleaned:
        console.print(Text("  which one? /context rm PATH", style="secondary"))
        return
    lowered = cleaned.lower()
    # Matched by tail as well as in full, so a path can be dropped by the folder
    # name that was shown in the summary instead of by retyping all of it.
    keep = [
        path
        for path in session.context_paths
        if path.lower() != lowered and Path(path).name.lower() != lowered
    ]
    if len(keep) == len(session.context_paths):
        console.print(Text(f"  not attached: {cleaned}", style="secondary"))
        return
    session.context_paths[:] = keep
    bundles, errors = context_module.apply(session)
    _report(console, session, bundles, errors, header="context")


def _clear(console: Any, session: Any) -> None:
    if not session.context_paths:
        console.print(Text("  nothing attached", style="secondary"))
        return
    count = len(session.context_paths)
    session.context_paths.clear()
    session.context_text = ""
    console.print(
        Text("  detached ", style="secondary").append(
            f"{count} path{'' if count == 1 else 's'}", style="primary"
        )
    )


def _show(console: Any, session: Any, header: str) -> None:
    if not session.context_paths:
        console.print(Text("  nothing attached", style="secondary"))
        console.print(
            Text("  /context ", style="hint").append(
                "PATH — read a folder or a file", style="secondary"
            )
        )
        return
    bundles, errors = context_module.apply(session)
    _report(console, session, bundles, errors, header=header)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _report(
    console: Any, session: Any, bundles: list, errors: list[str], header: str
) -> None:
    console.print()
    console.print(Text(f"  {header}", style="header"))
    for bundle in bundles:
        row = Text("  │ ", style="secondary")
        row.append(bundle.label, style="primary")
        row.append(
            f"  {len(bundle.files)} file{'' if len(bundle.files) == 1 else 's'}",
            style="secondary",
        )
        row.append(f"  ~{_thousands(bundle.tokens)} tokens", style="hint")
        console.print(row)
        reasons = bundle.reasons()
        if reasons:
            detail = ", ".join(
                f"{count} {reason}" for reason, count in sorted(reasons.items())
            )
            console.print(
                Text("  │   skipped ", style="secondary").append(detail, style="secondary")
            )
        if bundle.budget_hit:
            console.print(
                Text("  │   ", style="secondary").append(
                    "the budget ran out — not all of it is attached", style="error"
                )
            )
    for problem in errors:
        console.print(Text(f"  │ ✖ {problem}", style="error"))

    total = sum(bundle.tokens for bundle in bundles)
    footer = Text("  ╰ ", style="secondary")
    if total:
        # Said plainly because it is charged on every turn, not once: a folder
        # attached and forgotten is the easiest way to triple a session's cost.
        footer.append(f"~{_thousands(total)} tokens", style="hint")
        footer.append(" added to every turn · ", style="secondary")
    footer.append("/context clear", style="hint")
    footer.append(" to detach", style="secondary")
    console.print(footer)
    console.print()


def _remaining(session: Any) -> int:
    """Budget left for a new attachment, given what is already attached."""
    used = len(getattr(session, "context_text", "") or "")
    return max(0, context_module.DEFAULT_BUDGET - used)


def _thousands(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k".replace(".0k", "k")


__all__ = ["run_context_command"]
