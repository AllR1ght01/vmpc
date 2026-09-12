"""The ``/model`` command.

Kept separate from ``/api`` because switching model is frequent and switching
endpoint is not.

    /model                   pick from what is known, or add / probe from there
    /model NAME              use NAME
    /model add [NAME]        save NAME on this endpoint and use it
    /model rm NAME           forget NAME
    /model list              what is saved, and what the endpoint advertises
    /model scan              ask the endpoint for its list and save it
    /model probe [NAMES]     try names against this key and save what answers

Three sources of names, in order of how much they can be trusted: what the user
saved by hand, what a probe proved reachable, and what ``GET /models``
advertises. The last one is a suggestion — plenty of gateways serve a catalogue
that has little to do with what a given key is entitled to — so it is offered but
never persisted on its own.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, Sequence

import httpx
from rich.console import Console
from rich.text import Text

from vmpc.config import WIRE_OPENAI, Config, ConfigError, Provider
from vmpc.strings import t
from vmpc.ui.picker import Choice, confirm, prompt_text, select

#: Picker rows that are actions rather than model names. A NUL cannot occur in a
#: name typed at a prompt or served by a gateway, so these can never collide with
#: a real choice.
_ACTION_ADD = "\x00add"
_ACTION_PROBE = "\x00probe"
_ACTION_SCAN = "\x00scan"


def run_model_command(console: Console, config: Config, args: str = "") -> bool:
    provider = config.active_provider()
    if provider is None:
        console.print(Text(t("app.no_endpoint"), style="error"))
        console.print(Text(t("app.api_add_hint"), style="hint"))
        return False

    raw = args.strip()
    head, _, rest = raw.partition(" ")
    action = head.lower()
    rest = rest.strip()

    if action in ("add", "+", "save"):
        return _add(console, config, provider, rest)
    if action in ("rm", "remove", "-", "del", "delete", "forget"):
        return _remove(console, config, provider, rest)
    if action in ("list", "ls"):
        _print_list(console, provider)
        return False
    if action in ("scan", "fetch", "refresh"):
        return _scan(console, config, provider)
    if action in ("probe", "brute", "brute-force", "find", "search"):
        return _probe(console, config, provider, rest)

    if raw:
        # Anything else is the name itself — the fast path, and the reason the
        # subcommands above are all words no model is called.
        return _use(console, config, provider, raw)

    return _pick(console, config, provider)


# --------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------


def known_models(provider: Provider, live: Optional[Sequence[str]] = None) -> list[str]:
    """Every name worth offering: saved first, then whatever the endpoint said.

    Saved names lead because they are the ones somebody confirmed work here. The
    current model is always in the list even if it is in neither — otherwise
    ``/model`` would offer to replace it with no way to keep it.
    """
    out: list[str] = []
    for name in (
        [provider.model]
        + list(getattr(provider, "models", []) or [])
        + list(live or [])
    ):
        name = (name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _pick(console: Console, config: Config, provider: Provider) -> bool:
    live = _list_models(provider) or []
    saved = set(getattr(provider, "models", []) or [])
    names = known_models(provider, live)

    rows = [
        Choice(
            name,
            name,
            badge="current"
            if name == provider.model
            else ("saved" if name in saved else ""),
        )
        for name in names
    ]
    rows.append(
        Choice(
            _ACTION_ADD,
            t("model.picker_add_label"),
            description=t("model.picker_add_desc"),
        )
    )
    rows.append(
        Choice(
            _ACTION_PROBE,
            t("model.picker_probe_label"),
            description=t("model.picker_probe_desc"),
        )
    )
    if not live:
        rows.append(
            Choice(
                _ACTION_SCAN,
                t("model.picker_scan_label"),
                description=t("model.picker_scan_desc"),
            )
        )

    chosen = select(t("model.picker_title", name=provider.name), rows)
    if chosen is None:
        return False
    if chosen == _ACTION_ADD:
        return _add(console, config, provider, "")
    if chosen == _ACTION_PROBE:
        return _probe(console, config, provider, "")
    if chosen == _ACTION_SCAN:
        return _scan(console, config, provider)
    return _use(console, config, provider, chosen)


# --------------------------------------------------------------------------
# Manual entry
# --------------------------------------------------------------------------


def _use(console: Console, config: Config, provider: Provider, name: str) -> bool:
    provider.model = name.strip()
    config.upsert(provider)
    config.save()
    console.print(
        Text(t("model.set"), style="secondary").append(provider.model, style="success")
    )
    return True


def _add(console: Console, config: Config, provider: Provider, name: str) -> bool:
    """Save a name on this endpoint, and switch to it.

    Switching too, because "add a model" almost always means "I want to use this
    one" — and when it does not, the previous name is still one row up in the
    picker.
    """
    entered = name.strip()
    if not entered:
        entered = prompt_text(
            t("model.field_model"),
            placeholder=t("model.field_model_placeholder"),
        ) or ""
    entered = entered.strip()
    if not entered:
        return False

    saved = list(getattr(provider, "models", []) or [])
    if entered in saved:
        console.print(Text(t("model.already_saved", name=entered), style="secondary"))
    else:
        saved.append(entered)
        provider.models = saved
    provider.model = entered
    config.upsert(provider)
    config.save()
    console.print(
        Text(t("model.set"), style="secondary")
        .append(entered, style="success")
        .append(t("model.saved_on"), style="secondary")
        .append(provider.name, style="hint")
    )
    return True


def _remove(console: Console, config: Config, provider: Provider, name: str) -> bool:
    wanted = name.strip()
    if not wanted:
        console.print(Text(t("model.which_one"), style="secondary"))
        return False
    saved = list(getattr(provider, "models", []) or [])
    if wanted not in saved:
        console.print(Text(t("model.not_saved", name=wanted), style="secondary"))
        return False
    provider.models = [item for item in saved if item != wanted]
    config.upsert(provider)
    config.save()
    console.print(Text(t("model.forgot"), style="secondary").append(wanted, style="primary"))
    if wanted == provider.model:
        # Left in place deliberately: the name still works, it is just no longer
        # on the list. Switching the active model as a side effect of tidying up
        # the list would be a surprise mid-conversation.
        console.print(
            Text(t("model.still_using"), style="secondary").append(
                "/model NAME", style="hint"
            ).append(t("model.to_switch"), style="secondary")
        )
    return True


def _print_list(console: Console, provider: Provider) -> None:
    saved = list(getattr(provider, "models", []) or [])
    live = _list_models(provider) or []
    console.print()
    console.print(
        Text(f"  {provider.name}", style="header").append(
            f"  {provider.endpoint()}", style="secondary"
        )
    )
    if not saved and not live:
        console.print(Text(t("model.nothing_saved"), style="secondary"))
        console.print(
            Text("  /model add NAME", style="hint").append(
                " · ", style="secondary"
            ).append("/model probe", style="hint")
        )
        console.print()
        return
    for name in known_models(provider, live):
        mark = "● " if name == provider.model else "  "
        where = t("model.saved_label") if name in saved else (t("model.advertised_label") if name in live else "")
        console.print(
            Text(f"  {mark}", style="success" if name == provider.model else "secondary")
            .append(name, style="primary")
            .append(f"  {where}", style="secondary")
        )
    console.print()


def _scan(console: Console, config: Config, provider: Provider) -> bool:
    """Fetch ``GET /models`` and keep what it says."""
    live = _list_models(provider)
    if not live:
        console.print(
            Text(t("model.did_not_advertise", name=provider.name), style="secondary")
        )
        console.print(Text(t("model.probe_hint"), style="hint"))
        return False
    saved = list(getattr(provider, "models", []) or [])
    added = [name for name in live if name not in saved]
    provider.models = saved + added
    config.upsert(provider)
    config.save()
    console.print(
        Text("  ", style="secondary")
        .append(str(len(live)), style="primary")
        .append(t("model.advertised_sep"), style="secondary")
        .append(str(len(added)), style="success")
        .append(t("model.new_label"), style="secondary")
    )
    return True


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------


def _probe(console: Console, config: Config, provider: Provider, rest: str) -> bool:
    """Try model names against this endpoint and save the ones that answer.

    Every probe is a real request to the user's own endpoint with their own key,
    so the count is shown and confirmed before any of it is sent, and hits are
    printed as they land — a sweep that gets interrupted has still told you
    something.
    """
    from vmpc import probe as probe_module

    names = _wanted_names(console, rest)
    if names is None:
        return False
    if not names:
        names = probe_module.candidates(provider)

    console.print()
    console.print(
        Text(t("model.probe_header"), style="header")
        .append(str(len(names)), style="primary")
        .append(t("model.probe_against"), style="secondary")
        .append(provider.endpoint(), style="hint")
    )
    console.print(
        Text("  ", style="secondary").append(
            t("model.probe_requests", n=len(names)), style="secondary"
        )
    )
    answer = confirm(t("model.confirm_send"), default=True)
    if not answer:
        console.print(Text(t("model.cancelled"), style="secondary"))
        return False

    cancel = threading.Event()
    found: list[str] = []
    counts: dict[str, int] = {}
    errors = 0
    try:
        for result in probe_module.probe(provider, names, cancel=cancel):
            counts[result.kind] = counts.get(result.kind, 0) + 1
            if result.available:
                found.append(result.model)
                console.print(
                    Text("  ● ", style="success").append(result.model, style="primary")
                )
            elif result.kind == probe_module.KIND_UNCLEAR:
                console.print(
                    Text("  ? ", style="hint")
                    .append(result.model, style="primary")
                    .append(f"  {result.status} {result.detail}"[:96], style="secondary")
                )
            elif result.kind == probe_module.KIND_FATAL:
                console.print(
                    Text("  ✖ ", style="error").append(
                        f"{result.status} {result.detail}"[:120], style="error"
                    )
                )
                console.print(
                    Text(t("model.stopping_key"), style="secondary")
                )
            elif result.kind == probe_module.KIND_ERROR:
                errors += 1
                if errors <= 3:
                    console.print(
                        Text("  ! ", style="error")
                        .append(result.model, style="primary")
                        .append(f"  {result.detail}"[:96], style="secondary")
                    )
                if errors >= 5:
                    # Five failures in a row is the endpoint, not the names.
                    cancel.set()
                    console.print(
                        Text(t("model.too_many_failures"), style="error")
                    )
    except KeyboardInterrupt:
        cancel.set()
        console.print(Text(t("app.interrupted"), style="secondary"))

    _probe_summary(console, counts, len(names))
    if not found:
        console.print()
        return False

    saved = list(getattr(provider, "models", []) or [])
    added = [name for name in found if name not in saved]
    provider.models = saved + added
    if not provider.model or provider.model not in found:
        # A model that was set but does not answer is worse than none: every turn
        # would fail the same way. The first one that does answer is a better
        # default than leaving it broken.
        provider.model = found[0]
    config.upsert(provider)
    config.save()
    console.print(
        Text(t("model.saved_summary"), style="secondary")
        .append(str(len(added)), style="success")
        .append(t("model.using_sep"), style="secondary")
        .append(provider.model, style="success")
    )
    console.print()
    return True


def _probe_summary(console: Console, counts: dict[str, int], asked: int) -> None:
    from vmpc import probe as probe_module

    labels = (
        (probe_module.KIND_AVAILABLE, t("model.label.available")),
        (probe_module.KIND_REJECTED, t("model.label.rejected")),
        (probe_module.KIND_UNCLEAR, t("model.label.unclear")),
        (probe_module.KIND_ERROR, t("model.label.failed")),
        (probe_module.KIND_SKIPPED, t("model.label.not_sent")),
    )
    row = Text("  ", style="secondary")
    row.append(t("model.asked", n=asked), style="secondary")
    for kind, label in labels:
        count = counts.get(kind, 0)
        if not count:
            continue
        row.append(" · ", style="secondary")
        row.append(str(count), style="success" if kind == probe_module.KIND_AVAILABLE else "primary")
        row.append(f" {label}", style="secondary")
    console.print(row)


def _wanted_names(console: Console, rest: str) -> Optional[list[str]]:
    """Parse the probe's argument: nothing, a list, or a file of names.

    Returns None when the argument was meant to be a file and was not readable —
    silently falling back to the built-in list would probe the wrong thing.
    """
    text = rest.strip().strip("\"'")
    if not text:
        return []
    candidate = Path(text).expanduser()
    if candidate.is_file():
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            console.print(Text(t("model.cannot_read", path=candidate, exc=exc), style="error"))
            return None
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    separators = "," if "," in text else None
    parts = text.split(separators) if separators else text.split()
    return [part.strip() for part in parts if part.strip()]


# --------------------------------------------------------------------------
# GET /models
# --------------------------------------------------------------------------


def _list_models(provider: Provider) -> Optional[list[str]]:
    """Ask the endpoint for its models.

    Only the OpenAI shape has a conventional ``/models`` route; Anthropic-shaped
    endpoints get None and fall back to the saved list. Failures are swallowed on
    purpose — this is a convenience, and a gateway that does not implement the
    route should not produce an error for a ``/model`` call.
    """
    if provider.wire != WIRE_OPENAI:
        return None
    url = f"{provider.base_url.rstrip('/')}/models"
    try:
        headers = provider.auth_headers()
    except ConfigError:
        return None
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        if response.status_code >= 400:
            return None
        payload = response.json()
    except (httpx.RequestError, ValueError):
        return None

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    names = [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("id")
    ]
    return sorted(names)[:60] or None
