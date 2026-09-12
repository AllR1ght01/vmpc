"""The ``/api`` command: manage custom API endpoints.

The flow follows the menu the user sketched:

    /api
      ├─ OpenAI Compatible / Anthropic Compatible
      ├─ site:   https://example.com/v1   (grey placeholder; no /v1 for Anthropic)
      ├─ api-key
      ├─ api-key-type:  static-api-key | env-var | command
      └─ auth:          bearer | x-api-key | authorization | custom-header | none

Each step is a picker or a field, and each has a sensible default derived from
the previous answer — picking Anthropic preselects ``x-api-key`` and drops the
``/v1`` from the URL placeholder, because that is what the Anthropic shape
actually wants. The user can still override every one of them; the defaults just
mean a stock endpoint is four keystrokes.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.text import Text

from vmpc.config import (
    AUTH_BEARER,
    AUTH_CUSTOM,
    AUTH_DESCRIPTIONS,
    AUTH_LABELS,
    AUTH_NONE,
    AUTH_RAW,
    AUTH_X_API_KEY,
    DEFAULT_AUTH_FOR_WIRE,
    DEFAULT_MODELS,
    KEY_TYPE_COMMAND,
    KEY_TYPE_DESCRIPTIONS,
    KEY_TYPE_ENV,
    KEY_TYPE_LABELS,
    KEY_TYPE_STATIC,
    URL_PLACEHOLDERS,
    WIRE_ANTHROPIC,
    WIRE_DESCRIPTIONS,
    WIRE_LABELS,
    WIRE_OPENAI,
    Config,
    ConfigError,
    Provider,
)
from vmpc.ui.picker import Choice, confirm, print_error, select, prompt_text

# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_api_command(console: Console, config: Config, args: str = "") -> bool:
    """Handle ``/api`` and its inline forms. Returns True if config changed.

    Inline args exist so the common operations stay scriptable:
    ``/api add``, ``/api use <name>``, ``/api edit <name>``,
    ``/api remove <name>``, ``/api list``, ``/api test``.
    """
    parts = args.split(maxsplit=1)
    verb = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb == "add":
        return _add_provider(console, config)
    if verb == "list":
        show_providers(console, config)
        return False
    if verb in ("use", "switch"):
        return _use_provider(console, config, rest)
    if verb == "edit":
        return _edit_provider(console, config, rest)
    if verb in ("remove", "rm", "delete"):
        return _remove_provider(console, config, rest)
    if verb == "test":
        return _test_provider(console, config, rest)
    if verb:
        console.print(
            Text(f"unknown: /api {verb}", style="error"),
        )
        console.print(
            Text("  try: add · list · use · edit · remove · test", style="secondary")
        )
        return False

    return _menu(console, config)


def _menu(console: Console, config: Config) -> bool:
    """The top-level menu shown by a bare ``/api``."""
    show_providers(console, config)

    choices = [Choice("add", "add", "configure a new endpoint")]
    if config.providers:
        choices.extend(
            [
                Choice("use", "use", "switch the active endpoint"),
                Choice("edit", "edit", "change an existing endpoint"),
                Choice("test", "test", "send a one-token request to verify it works"),
                Choice("remove", "remove", "delete an endpoint"),
            ]
        )

    action = select("API endpoints", choices)
    if action is None:
        return False
    if action == "add":
        return _add_provider(console, config)
    if action == "use":
        return _use_provider(console, config, "")
    if action == "edit":
        return _edit_provider(console, config, "")
    if action == "test":
        return _test_provider(console, config, "")
    if action == "remove":
        return _remove_provider(console, config, "")
    return False


# --------------------------------------------------------------------------
# The wizard
# --------------------------------------------------------------------------


def _add_provider(console: Console, config: Config) -> bool:
    provider = _wizard(console, config, existing=None)
    if provider is None:
        console.print(Text("  cancelled", style="secondary"))
        return False
    config.upsert(provider)
    config.active = provider.name
    path = config.save()
    console.print()
    _print_summary(console, provider, title="added")
    console.print(Text(f"  saved to {path}", style="secondary"))
    return True


def _format_headers(headers: dict[str, str]) -> str:
    """Render stored headers back into the form the prompt accepts."""
    return ", ".join(f"{name}: {value}" for name, value in headers.items())


def _parse_headers(text: str) -> dict[str, str]:
    """Parse ``name: value, other: value`` into a dict.

    Commas separate pairs and the first colon splits each one, so a value may
    contain a colon (a URL, say) without escaping.
    """
    headers: dict[str, str] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"expected 'name: value' but got {part!r}")
        name, _, value = part.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            raise ValueError(f"missing a header name in {part!r}")
        if not value:
            raise ValueError(f"missing a value for {name!r}")
        headers[name] = value
    return headers


def _edit_provider(console: Console, config: Config, name: str) -> bool:
    target = _pick_provider(config, name, "Edit which endpoint?")
    if target is None:
        return False
    provider = _wizard(console, config, existing=target)
    if provider is None:
        console.print(Text("  cancelled", style="secondary"))
        return False
    # Captured before remove(): remove() reassigns `active` when it takes the
    # active endpoint away, and the check below would then never fire — so
    # renaming the endpoint in use silently switched to another one.
    was_active = config.active == target.name
    if provider.name != target.name:
        config.remove(target.name)
    config.upsert(provider)
    if was_active:
        config.active = provider.name
    config.save()
    console.print()
    _print_summary(console, provider, title="updated")
    return True


def _wizard(
    console: Console, config: Config, existing: Optional[Provider]
) -> Optional[Provider]:
    """Walk the five steps. Returns None at any point the user cancels."""
    editing = existing is not None
    base = existing.copy() if existing else Provider(name="")

    # -- 1. wire format ----------------------------------------------------
    wire = select(
        "API format",
        [
            Choice(
                WIRE_OPENAI,
                WIRE_LABELS[WIRE_OPENAI],
                WIRE_DESCRIPTIONS[WIRE_OPENAI],
                badge="current" if editing and base.wire == WIRE_OPENAI else "",
            ),
            Choice(
                WIRE_ANTHROPIC,
                WIRE_LABELS[WIRE_ANTHROPIC],
                WIRE_DESCRIPTIONS[WIRE_ANTHROPIC],
                badge="current" if editing and base.wire == WIRE_ANTHROPIC else "",
            ),
        ],
        initial=0 if base.wire == WIRE_OPENAI else 1,
    )
    if wire is None:
        return None

    # -- 2. base URL -------------------------------------------------------
    # The placeholder carries the shape difference: the OpenAI client appends
    # /chat/completions to whatever it is given, so the version segment must be
    # in the base URL; the Anthropic client appends /v1/messages itself.
    url = prompt_text(
        "site:",
        default=base.base_url,
        placeholder=URL_PLACEHOLDERS[wire],
        validate=lambda value: _validate_url(value, wire),
    )
    if url is None:
        return None
    url = _normalize_url(url, wire)

    # -- 3. api key --------------------------------------------------------
    key_type = select(
        "api-key-type",
        [
            Choice(
                KEY_TYPE_STATIC,
                KEY_TYPE_LABELS[KEY_TYPE_STATIC],
                KEY_TYPE_DESCRIPTIONS[KEY_TYPE_STATIC],
            ),
            Choice(
                KEY_TYPE_ENV,
                KEY_TYPE_LABELS[KEY_TYPE_ENV],
                KEY_TYPE_DESCRIPTIONS[KEY_TYPE_ENV],
            ),
            Choice(
                KEY_TYPE_COMMAND,
                KEY_TYPE_LABELS[KEY_TYPE_COMMAND],
                KEY_TYPE_DESCRIPTIONS[KEY_TYPE_COMMAND],
            ),
        ],
        initial=_index_of(
            [KEY_TYPE_STATIC, KEY_TYPE_ENV, KEY_TYPE_COMMAND], base.key_type
        ),
    )
    if key_type is None:
        return None

    if key_type == KEY_TYPE_STATIC:
        # Typed hidden, and never echoed back on edit — an existing key is kept
        # by submitting the field empty.
        key_label = "api-key:"
        # The paste hint is here because the field is masked: a paste that did
        # nothing and a paste that worked look identical, so the one thing worth
        # saying is which key does it.
        key_placeholder = (
            "sk-… (hidden; ctrl+v pastes; enter keeps the current key)"
            if editing
            else "sk-… (hidden; ctrl+v pastes)"
        )
        key_value = prompt_text(
            key_label,
            placeholder=key_placeholder,
            password=True,
            allow_empty=editing,
        )
        if key_value is None and not editing:
            return None
        if not key_value:
            key_value = base.key_value
    elif key_type == KEY_TYPE_ENV:
        key_value = prompt_text(
            "env var:",
            default=base.key_value if base.key_type == KEY_TYPE_ENV else "",
            placeholder="OPENAI_API_KEY",
        )
        if key_value is None:
            return None
    else:
        key_value = prompt_text(
            "command:",
            default=base.key_value if base.key_type == KEY_TYPE_COMMAND else "",
            placeholder="op read op://vault/api/key",
        )
        if key_value is None:
            return None

    # -- 4. auth scheme ----------------------------------------------------
    default_auth = (
        base.auth_scheme if editing else DEFAULT_AUTH_FOR_WIRE.get(wire, AUTH_BEARER)
    )
    scheme_order = [AUTH_BEARER, AUTH_X_API_KEY, AUTH_RAW, AUTH_CUSTOM, AUTH_NONE]
    auth_scheme = select(
        "auth",
        [
            Choice(
                scheme,
                AUTH_LABELS[scheme],
                AUTH_DESCRIPTIONS[scheme],
                badge="default" if scheme == DEFAULT_AUTH_FOR_WIRE.get(wire) else "",
            )
            for scheme in scheme_order
        ],
        initial=_index_of(scheme_order, default_auth),
    )
    if auth_scheme is None:
        return None

    auth_header = base.auth_header
    if auth_scheme == AUTH_CUSTOM:
        header = prompt_text(
            "header name:",
            default=base.auth_header,
            placeholder="x-gateway-token",
        )
        if header is None:
            return None
        auth_header = header

    # -- 5. model and name -------------------------------------------------
    # allow_empty so Enter on a blank field adopts the wire format's default
    # instead of reading as "cancel" and throwing away the whole wizard — the
    # placeholder already advertises the default, so Enter should take it.
    model = prompt_text(
        "model:",
        default=base.model,
        placeholder=DEFAULT_MODELS.get(wire, ""),
        allow_empty=True,
    )
    if model is None:
        return None
    model = model.strip() or DEFAULT_MODELS.get(wire, "")

    suggested = base.name or _suggest_name(url)
    name = prompt_text(
        "name:",
        default=suggested,
        placeholder="how this endpoint appears in /api list",
    )
    if name is None:
        return None
    if not editing or name != base.name:
        name = config.unique_name(name)

    # -- 6. extra headers --------------------------------------------------
    # Gateways commonly document a required header of their own (a project id,
    # a tenant, a beta opt-in). Without a way to set one, such an endpoint is
    # simply unusable, so this asks — pre-filled when editing, skippable when not.
    headers_text = prompt_text(
        "extra headers:",
        default=_format_headers(base.extra_headers),
        placeholder="optional — name: value, comma separated",
        allow_empty=True,
    )
    if headers_text is None:
        return None
    try:
        extra_headers = _parse_headers(headers_text)
    except ValueError as exc:
        print_error(str(exc))
        return None

    provider = base.copy(
        name=name,
        wire=wire,
        base_url=url,
        model=model,
        key_type=key_type,
        key_value=key_value or "",
        auth_scheme=auth_scheme,
        auth_header=auth_header,
        extra_headers=extra_headers,
    )

    problems = provider.validate()
    if problems:
        console.print(Text(f"  incomplete: {problems[0]}", style="error"))
        keep = confirm("Save anyway?", default=False)
        if not keep:
            return None
    return provider


# --------------------------------------------------------------------------
# Other verbs
# --------------------------------------------------------------------------


def _use_provider(console: Console, config: Config, name: str) -> bool:
    target = _pick_provider(config, name, "Use which endpoint?")
    if target is None:
        return False
    config.active = target.name
    config.save()
    console.print(
        Text("  active: ", style="secondary").append(target.name, style="success")
    )
    return True


def _remove_provider(console: Console, config: Config, name: str) -> bool:
    target = _pick_provider(config, name, "Remove which endpoint?")
    if target is None:
        return False
    sure = confirm(f"Remove {target.name}?", default=False)
    if not sure:
        return False
    config.remove(target.name)
    config.save()
    console.print(Text(f"  removed {target.name}", style="secondary"))
    return True


def _test_provider(console: Console, config: Config, name: str) -> bool:
    """Send a minimal request and report what came back."""
    target = _pick_provider(config, name, "Test which endpoint?")
    if target is None:
        return False

    problems = target.validate()
    if problems:
        console.print(Text(f"  not usable: {problems[0]}", style="error"))
        return False

    from vmpc.api.client import stream_chat
    from vmpc.api.events import ApiError

    console.print(Text(f"  → {target.endpoint()}", style="secondary"))
    try:
        # Ask for one token: enough to prove auth, routing and the model name.
        probe = target.copy(max_tokens=16, reasoning=False)
        received = []
        import threading

        for event in stream_chat(
            probe,
            [{"role": "user", "content": "Reply with the single word: ok"}],
            cancel=threading.Event(),
        ):
            if event.text:
                received.append(event.text)
            if len("".join(received)) > 40:
                break
    except ApiError as exc:
        console.print(Text(f"  ✖ {exc}", style="error"))
        if exc.hint:
            console.print(Text(f"    {exc.hint}", style="secondary"))
        return False
    except ConfigError as exc:
        console.print(Text(f"  ✖ {exc}", style="error"))
        return False

    reply = "".join(received).strip() or "(empty response)"
    console.print(Text("  ✔ ", style="success").append(reply[:60], style="secondary"))
    return False


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------


def show_providers(console: Console, config: Config) -> None:
    """Print the configured endpoints, with the active one marked."""
    if not config.providers:
        console.print(Text("  no endpoints configured", style="secondary"))
        console.print(Text("  /api add — configure one", style="hint"))
        return

    active = config.active_provider()
    # Rich's width property falls back to 80 for a non-terminal stream on
    # Windows, even when a caller set ``console.width`` for a test or buffer.
    # The setter stores that requested value in ``_width``; prefer it when it
    # exists so the row is measured against the actual requested viewport.
    console_width = getattr(console, "_width", None) or console.width
    name_width = max(len(p.name) for p in config.providers)
    for provider in config.providers:
        is_active = active is not None and provider.name == active.name
        row = Text("  ")
        row.append("● " if is_active else "  ", style="success" if is_active else "")
        row.append(provider.name.ljust(name_width), style="header" if is_active else "")
        row.append(f"  {WIRE_LABELS.get(provider.wire, provider.wire)}", style="secondary")
        row.append(f"  {provider.base_url}", style="secondary")
        if provider.model:
            row.append(f"  {provider.model}", style="hint")
        # One endpoint is one line. A long base URL would otherwise wrap and the
        # column alignment — the only thing making this list scannable — would
        # be gone for every row after it.
        row.truncate(max(console_width - 1, 20), overflow="ellipsis")
        console.print(row)


def _print_summary(console: Console, provider: Provider, title: str) -> None:
    console.print(Text(f"  {title} ", style="secondary").append(provider.name, style="success"))
    rows = [
        ("format", WIRE_LABELS.get(provider.wire, provider.wire)),
        ("site", provider.base_url),
        ("endpoint", provider.endpoint()),
        ("model", provider.model or "(not set)"),
        ("api-key-type", KEY_TYPE_LABELS.get(provider.key_type, provider.key_type)),
        ("api-key", provider.masked_key()),
        (
            "auth",
            provider.auth_header
            if provider.auth_scheme == AUTH_CUSTOM
            else AUTH_LABELS.get(provider.auth_scheme, provider.auth_scheme),
        ),
    ]
    if provider.extra_headers:
        rows.append(("extra headers", _format_headers(provider.extra_headers)))
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        console.print(
            Text(f"    {label.ljust(width)}  ", style="secondary").append(
                str(value), style="primary"
            )
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _pick_provider(
    config: Config, name: str, title: str
) -> Optional[Provider]:
    """Resolve a provider by name, or ask when the name is missing or wrong."""
    if name:
        found = config.get(name)
        if found is not None:
            return found
    if not config.providers:
        return None
    if len(config.providers) == 1 and not name:
        return config.providers[0]

    active = config.active
    chosen = select(
        title,
        [
            Choice(
                p.name,
                p.name,
                f"{WIRE_LABELS.get(p.wire, p.wire)} · {p.base_url}",
                badge="active" if p.name == active else "",
            )
            for p in config.providers
        ],
        initial=_index_of([p.name for p in config.providers], active),
    )
    return config.get(chosen) if chosen else None


def _validate_url(value: str, wire: str) -> Optional[str]:
    candidate = value if "://" in value else f"https://{value}"
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return "that does not look like a URL"
    if parsed.scheme not in ("http", "https"):
        return "URL must be http or https"
    if wire == WIRE_ANTHROPIC and parsed.path.rstrip("/").endswith("/v1"):
        # Not fatal — _normalize_url strips it — but say so, because a silent
        # fix looks like the field ignored the input.
        return "drop the /v1 for the Anthropic format; it is added automatically"
    return None


def _normalize_url(value: str, wire: str) -> str:
    """Add the scheme if omitted and strip a trailing slash."""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    candidate = candidate.rstrip("/")
    if wire == WIRE_ANTHROPIC and candidate.endswith("/v1"):
        candidate = candidate[: -len("/v1")]
    return candidate


def _suggest_name(url: str) -> str:
    """Derive a short name from the host, e.g. api.openai.com → openai."""
    host = urlparse(url).netloc or url
    host = host.split(":")[0]
    parts = [part for part in host.split(".") if part not in ("www", "api")]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else "provider"


def _index_of(values: list[str], target: str) -> int:
    try:
        return values.index(target)
    except ValueError:
        return 0
