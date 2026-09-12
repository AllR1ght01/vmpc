"""The ``dev`` command group — tools for working on vmpc, not with a model.

Unlocked by any mode that names :data:`~vmpc.modes.DEV_GROUP`, which the built-in
``/dev`` does. They are gated rather than always-on because they are answers to
questions most sessions never ask: what did the model actually send me, what
would we put on the wire, does this terminal really support the palette.

Everything here reads through the same code paths a real turn uses —
:func:`vmpc.api.client._openai_body`, the markdown collector, the theme — rather
than reimplementing them. A diagnostic that models the system instead of
observing it can agree with itself while both are wrong.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from rich.console import Console
from rich.text import Text

from vmpc.commands import CommandContext
from vmpc.strings import current_language, en_plural, ru_plural, t

# --------------------------------------------------------------------------
# /raw — what the model actually sent
# --------------------------------------------------------------------------


def show_raw(context: CommandContext, args: str) -> None:
    """Print a reply's source text, unrendered.

    The renderer is lossy by design — it drops the ``**`` and turns ``|`` rows
    into a laid-out table — so when output looks wrong this is the only way to
    tell a bad reply apart from a bad render.
    """
    console: Console = context.console  # type: ignore[assignment]
    messages = list(getattr(context.session, "messages", []))

    role = "assistant"
    back = 1
    wanted = args.strip().lower()
    if wanted in ("user", "me"):
        role = "user"
    elif wanted.isdigit() and int(wanted) > 0:
        back = int(wanted)

    matching = [m for m in messages if m.get("role") == role]
    if len(matching) < back:
        console.print(Text(t("dev.no_role_message", role=role), style="secondary"))
        return
    body = matching[-back].get("content", "")
    lines = len(body.splitlines())
    line_noun = (
        en_plural(lines, t("dev.line"))
        if current_language() == "en"
        else ru_plural(lines, "строка", "строки", "строк")
    )

    console.print()
    console.print(
        Text(f"  {role}", style="header").append(
            f"  {t('dev.chars_lines', chars=len(body), n=lines, line_noun=line_noun)}",
            style="secondary",
        )
    )
    _print_verbatim(console, body)
    console.print()


def _print_verbatim(console: Console, body: str) -> None:
    """Print text exactly as it is, against a rail.

    ``markup=False`` matters: a reply containing ``[/]`` is ordinary text here,
    and letting rich parse it would corrupt the one view whose whole job is to
    be uncorrupted.
    """
    for line in body.splitlines() or [""]:
        console.print(
            Text("  │ ", style="secondary").append(line, style="primary"),
            markup=False,
            highlight=False,
        )


# --------------------------------------------------------------------------
# /render — put markdown through the renderer
# --------------------------------------------------------------------------

#: Exercises every construct the renderer handles, so one command shows whether
#: any of them regressed. Kept here rather than in a test fixture because the
#: thing being checked is how it *looks*, which no assertion covers.
_SAMPLE = """# Heading one
## Heading two
### Heading three

Plain text with **bold**, *italic*, ~~strike~~ and `inline code`.
A [labelled link](https://example.com/docs) and a bare https://example.com/bare.

- bullet
- another
  - nested
    - deeper
1. ordered
2. second

> a quote
> spanning two lines

| column | meaning        |
| ------ | -------------- |
| left   | first cell     |
| right  | a longer cell  |

```python
def hello(name: str) -> str:
    return f"hi {name}"  # ** not bold ** inside a fence
```

---
"""


def render_sample(context: CommandContext, args: str) -> None:
    """Render markdown through the real collector and show the result."""
    console: Console = context.console  # type: ignore[assignment]
    from vmpc.render.markdown import render_markdown

    source = args.strip()
    label = t("dev.sample_label")
    if source.lower() == "last":
        replies = [
            m for m in getattr(context.session, "messages", []) if m.get("role") == "assistant"
        ]
        if not replies:
            console.print(Text(t("dev.no_reply_rerender"), style="secondary"))
            return
        source = replies[-1].get("content", "")
        label = t("dev.last_reply_label")
    elif not source:
        source = _SAMPLE

    console.print()
    console.print(
        Text(t("dev.render_header"), style="header").append(
            f"{label}{t('dev.width_label', width=max(console.width - 2, 20))}", style="secondary"
        )
    )
    console.print()
    for line in render_markdown(source, width=max(console.width - 2, 20)):
        console.print(line, markup=False, highlight=False, soft_wrap=False)
    console.print()


# --------------------------------------------------------------------------
# /wire — the request a turn would send
# --------------------------------------------------------------------------


def show_wire(context: CommandContext, args: str) -> None:
    """Show the exact HTTP request the next turn would make, key redacted.

    Built by calling the same body and header builders the transport calls, so
    this cannot drift from what actually goes out — which is the entire point of
    a command you reach for when the endpoint answers 401.
    """
    console: Console = context.console  # type: ignore[assignment]
    import json

    from vmpc.api import client
    from vmpc.config import WIRE_ANTHROPIC, ConfigError

    provider = context.config.active_provider()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))
        return

    sample = args.strip() or "ping"
    messages = list(getattr(context.session, "messages", [])) or [
        {"role": "user", "content": sample}
    ]
    system = _system_of(context.session)

    key = ""
    try:
        key = provider.resolve_key()
        headers = client._headers(provider)
        header_error = ""
    except ConfigError as exc:
        # Worth showing rather than aborting: "the vault command failed" is
        # exactly the diagnosis someone runs /wire to get.
        headers = {}
        header_error = str(exc)

    if provider.wire == WIRE_ANTHROPIC:
        body = client._anthropic_body(provider, messages, system)
    else:
        body = client._openai_body(provider, messages, system)

    console.print()
    console.print(
        Text(t("dev.post_label"), style="header").append(provider.endpoint(), style="primary")
    )
    transport = t("dev.transport_sdk") if provider.use_sdk else t("dev.transport_httpx")
    console.print(
        Text(t("dev.via_label"), style="secondary").append(transport, style="hint")
    )
    console.print()

    if header_error:
        console.print(Text(t("dev.credential_error", detail=header_error), style="error"))
    for name, value in sorted(headers.items()):
        console.print(
            Text(f"  {name}: ", style="secondary").append(
                _redact(name, value, key), style="primary"
            )
        )
    console.print()
    for line in json.dumps(body, indent=2, ensure_ascii=False).splitlines():
        console.print(
            Text("  │ ", style="secondary").append(line, style="primary"),
            markup=False,
            highlight=False,
        )
    console.print()


#: Substrings that mark a header as carrying a credential. Matched rather than
#: compared, because the leaky case is the header the user added themselves —
#: ``x-session-token``, ``x-goog-api-key`` — and an exact-name list only ever
#: knows the two or three we thought of. Benign extras (``http-referer``,
#: ``x-title``) miss every substring and still print in full, which is the point:
#: masking those would make the command useless for the thing it exists to show.
_SECRET_HINTS = ("auth", "key", "token", "secret", "cookie", "credential", "password")


def _redact(name: str, value: str, key: str) -> str:
    """Mask anything in a header that could be a secret.

    Two passes, because either alone leaks. Masking the resolved key by
    substring catches it wherever it travels — including an ``extra_headers``
    entry the user routed it through, which a name-based redactor would print in
    full right underneath the one it hid. Masking credential-shaped headers
    catches the second token someone added by hand, which the key-substring pass
    has never seen and would happily print.
    """
    masked = _mask(key) if key and key in value else ""
    if masked:
        return value.replace(key, masked)
    lowered = name.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        scheme, space, rest = value.partition(" ")
        # Keep the scheme word ("Bearer"): it is not a secret, and getting it
        # wrong is a common enough cause of 401 to be worth seeing.
        return f"{scheme}{space}{_mask(rest)}" if space else _mask(value)
    return value


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


# --------------------------------------------------------------------------
# /ping — how fast is this endpoint
# --------------------------------------------------------------------------


def ping_endpoint(context: CommandContext, args: str) -> None:
    """Time a real one-token request: connect, first token, throughput."""
    console: Console = context.console  # type: ignore[assignment]
    from vmpc.api.client import stream_chat
    from vmpc.api.events import ApiError, EventKind

    provider = context.config.active_provider()  # type: ignore[union-attr]
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))
        return

    # Small and reasoning-free: this measures the endpoint, not the model's
    # willingness to think. A large max_tokens would time the answer instead.
    probe = provider.copy(max_tokens=32, reasoning=False)
    cancel = threading.Event()

    console.print()
    console.print(
        Text(t("dev.ping_header"), style="header").append(probe.endpoint(), style="secondary")
    )

    started = time.perf_counter()
    first: Optional[float] = None
    chars = 0
    output_tokens = 0
    try:
        for event in stream_chat(
            probe,
            [{"role": "user", "content": "Reply with the single word: pong."}],
            system="",
            cancel=cancel,
        ):
            if event.kind is EventKind.ANSWER:
                if first is None:
                    first = time.perf_counter()
                chars += len(event.text)
            elif event.kind is EventKind.USAGE and event.usage:
                output_tokens = max(output_tokens, event.usage.output_tokens)
    except ApiError as exc:
        console.print(Text(t("app.error_prefix", error=exc), style="error"))
        if exc.hint:
            console.print(Text(t("app.error_hint", hint=exc.hint), style="secondary"))
        return
    finally:
        cancel.set()

    total = time.perf_counter() - started
    rows = [(t("dev.label.total"), f"{total * 1000:.0f} ms")]
    if first is not None:
        rows.append((t("dev.label.first_token"), f"{(first - started) * 1000:.0f} ms"))
        streaming = total - (first - started)
        if output_tokens and streaming > 0.001:
            rows.append((t("dev.label.throughput"), f"{output_tokens / streaming:.1f} tok/s"))
    rows.append((t("dev.label.output"), t("dev.output_value", tokens=output_tokens or "?", chars=chars)))

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(
            Text(f"  {label.ljust(width)}  ", style="secondary").append(
                value, style="primary"
            )
        )
    if first is None:
        console.print(Text(t("dev.no_content"), style="error"))
    console.print()


# --------------------------------------------------------------------------
# /palette — does this terminal actually have the colors
# --------------------------------------------------------------------------


def show_palette(context: CommandContext, args: str) -> None:
    """Swatch every theme style, and report what the terminal admits to.

    The header is the useful half. The rose palette was invisible on Windows for
    a while because the console reports no VT support until asked, and every RGB
    shade silently collapsed to grey — a failure that looks like a design choice
    unless something prints ``color_system`` next to the swatches.
    """
    console: Console = context.console  # type: ignore[assignment]
    from rich.default_styles import DEFAULT_STYLES

    from vmpc.style import VMPC_THEME

    facts = [
        (t("dev.palette.color_system"), str(console.color_system)),
        (t("dev.palette.no_color"), str(console.no_color)),
        (t("dev.palette.terminal"), str(console.is_terminal)),
        (t("dev.palette.legacy_windows"), str(console.legacy_windows)),
        (t("dev.palette.width"), str(console.width)),
    ]
    console.print()
    width = max(len(label) for label, _ in facts)
    for label, value in facts:
        console.print(
            Text(f"  {label.ljust(width)}  ", style="secondary").append(
                value, style="primary"
            )
        )
    console.print()

    ours = sorted(set(VMPC_THEME.styles) - set(DEFAULT_STYLES))
    name_width = max(len(name) for name in ours)
    for name in ours:
        row = Text("  ")
        row.append("████", style=name)
        row.append(f"  {name.ljust(name_width)}", style=name)
        row.append(f"  {VMPC_THEME.styles[name]}", style="secondary")
        row.truncate(max(console.width - 1, 20), overflow="ellipsis")
        console.print(row)
    console.print()


# --------------------------------------------------------------------------
# /prompt — what the model is actually being told
# --------------------------------------------------------------------------


def show_prompt(context: CommandContext, args: str) -> None:
    """Print the system prompt in effect, base and mode together.

    A mode appends to the base prompt and ``/context`` appends to that, so no
    single field is what the model sees. This shows the string that actually
    ships.
    """
    console: Console = context.console  # type: ignore[assignment]
    session: Any = context.session
    system = _system_of(session)
    mode = str(getattr(session, "mode", "") or "")
    attached = list(getattr(session, "context_paths", []) or [])

    console.print()
    console.print(
        Text(t("dev.prompt_header"), style="header").append(
            t("dev.prompt_chars", n=len(system)), style="secondary"
        )
    )
    console.print(
        Text(t("dev.mode_label"), style="secondary").append(
            f"/{mode}" if mode else t("dev.mode_none"),
            style="hint" if mode else "secondary",
        )
    )
    if attached:
        console.print(
            Text(t("dev.files_label"), style="secondary").append(
                ", ".join(attached), style="hint"
            )
        )
    console.print()
    _print_verbatim(console, system)
    console.print()


def _system_of(session: Any) -> str:
    """The prompt a turn would send, attachments included.

    Goes through ``full_system()`` when the session has it. A diagnostic that
    printed only ``system`` would omit the attached files — and those are exactly
    what someone is looking for when a request is unexpectedly enormous.
    """
    builder = getattr(session, "full_system", None)
    if callable(builder):
        try:
            return str(builder())
        except Exception:  # noqa: BLE001 - a diagnostic never dies on a getter
            pass
    return str(getattr(session, "system", ""))
