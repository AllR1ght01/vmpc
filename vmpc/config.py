"""Configuration model and on-disk store.

Config lives in a single JSON file (``~/.vmpc/config.json`` by default,
overridable with ``VMPC_HOME``). JSON rather than TOML because Python 3.10 has
no ``tomllib`` in the stdlib and this project deliberately depends only on
rich, prompt_toolkit and httpx.

The interesting part is :class:`Provider`, which describes how to talk to an
OpenAI- or Anthropic-compatible endpoint: where it lives, how the credential is
obtained, and how that credential is attached to the request. Those three are
kept separate on purpose — "static key in the config" and "send it as a Bearer
token" are independent choices, and plenty of gateways mix them.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from vmpc.modes import Mode

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Wire formats we can speak. The value is the request/response shape, not the
#: vendor — any gateway that mimics the shape works.
WIRE_OPENAI = "openai"
WIRE_ANTHROPIC = "anthropic"

WIRE_LABELS = {
    WIRE_OPENAI: "OpenAI Compatible",
    WIRE_ANTHROPIC: "Anthropic Compatible",
}

WIRE_DESCRIPTIONS = {
    WIRE_OPENAI: "/chat/completions, SSE deltas — OpenAI, Groq, together, vLLM, LM Studio, OpenRouter",
    WIRE_ANTHROPIC: "/v1/messages, typed SSE events — Anthropic, Bedrock gateways, proxies",
}

#: Placeholder shown greyed out in the URL field. The OpenAI shape wants the
#: version segment in the base URL; the Anthropic client appends ``/v1/messages``
#: itself, so its base URL carries no ``/v1``.
URL_PLACEHOLDERS = {
    WIRE_OPENAI: "https://example.com/v1",
    WIRE_ANTHROPIC: "https://example.com",
}

#: Where the credential comes from.
KEY_TYPE_STATIC = "static-api-key"
KEY_TYPE_ENV = "env-var"
KEY_TYPE_COMMAND = "command"

KEY_TYPE_LABELS = {
    KEY_TYPE_STATIC: "static-api-key",
    KEY_TYPE_ENV: "env-var",
    KEY_TYPE_COMMAND: "command",
}

KEY_TYPE_DESCRIPTIONS = {
    KEY_TYPE_STATIC: "the key is stored in the config file",
    KEY_TYPE_ENV: "read from an environment variable at request time",
    KEY_TYPE_COMMAND: "run a shell command and use its stdout (1Password, pass, vault)",
}

#: How the credential is attached to the request.
AUTH_BEARER = "bearer"
AUTH_X_API_KEY = "x-api-key"
AUTH_RAW = "authorization"
AUTH_CUSTOM = "custom-header"
AUTH_NONE = "none"

AUTH_LABELS = {
    AUTH_BEARER: "bearer",
    AUTH_X_API_KEY: "x-api-key",
    AUTH_RAW: "authorization",
    AUTH_CUSTOM: "custom-header",
    AUTH_NONE: "none",
}

AUTH_DESCRIPTIONS = {
    AUTH_BEARER: "Authorization: Bearer <key>",
    AUTH_X_API_KEY: "x-api-key: <key>",
    AUTH_RAW: "Authorization: <key>  (key already carries its own scheme)",
    AUTH_CUSTOM: "<your-header>: <key>",
    AUTH_NONE: "no auth header — local endpoints, or auth via a proxy",
}

#: Auth scheme each wire format defaults to.
DEFAULT_AUTH_FOR_WIRE = {
    WIRE_OPENAI: AUTH_BEARER,
    WIRE_ANTHROPIC: AUTH_X_API_KEY,
}

DEFAULT_MODELS = {
    WIRE_OPENAI: "gpt-4o-mini",
    WIRE_ANTHROPIC: "claude-sonnet-5",
}

#: Sent by Anthropic-shaped endpoints; overridable per provider.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class ConfigError(Exception):
    """Raised when a provider is unusable — bad shape, or a key we can't get."""


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


@dataclass
class Provider:
    """One configured endpoint."""

    name: str
    wire: str = WIRE_OPENAI
    base_url: str = ""
    model: str = ""
    #: Model names the user added by hand, or a probe found, or a scan saved.
    #: Persisted per endpoint because most gateways either do not implement
    #: ``GET /models`` or answer it with a catalogue that has nothing to do with
    #: what this key can reach — in which case the list someone assembled once is
    #: the only real one there is.
    models: list[str] = field(default_factory=list)

    key_type: str = KEY_TYPE_STATIC
    #: For ``static-api-key`` the literal key; for ``env-var`` the variable
    #: name; for ``command`` the command line.
    key_value: str = ""

    auth_scheme: str = AUTH_BEARER
    #: Header name, only meaningful when ``auth_scheme`` is ``custom-header``.
    auth_header: str = ""

    #: Extra headers merged into every request, e.g. ``HTTP-Referer`` for
    #: OpenRouter or a gateway's tenant id.
    extra_headers: dict[str, str] = field(default_factory=dict)

    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION
    #: Ask the model to stream its reasoning as a second channel where the wire
    #: format supports it. Drives the dual-stream renderer.
    reasoning: bool = False
    max_tokens: int = 4096
    timeout: float = 120.0
    #: Route through the vendor SDK (``anthropic`` / ``openai``) when it is
    #: installed, instead of our own httpx transport. On by default: gateways
    #: that gate on a recognised client answer the hand-rolled path with 401,
    #: and the SDK tracks vendor wire changes for free. Turn it off to use the
    #: httpx path — fewer dependencies, and it is the one under our control.
    use_sdk: bool = True

    # -- validation --------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means usable."""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("name is empty")
        if self.wire not in WIRE_LABELS:
            problems.append(f"unknown wire format {self.wire!r}")
        if not self.base_url.strip():
            problems.append("base URL is empty")
        elif not self.base_url.startswith(("http://", "https://")):
            problems.append("base URL must start with http:// or https://")
        if self.key_type not in KEY_TYPE_LABELS:
            problems.append(f"unknown key type {self.key_type!r}")
        if self.auth_scheme not in AUTH_LABELS:
            problems.append(f"unknown auth scheme {self.auth_scheme!r}")
        if self.auth_scheme == AUTH_CUSTOM and not self.auth_header.strip():
            problems.append("custom-header auth needs a header name")
        if self.auth_scheme != AUTH_NONE and not self.key_value.strip():
            problems.append("no API key configured")
        return problems

    # -- credential --------------------------------------------------------

    def resolve_key(self) -> str:
        """Return the actual credential, following :attr:`key_type`.

        Resolution happens per request rather than at load time so an env var
        or a vault command can change without restarting.
        """
        if self.auth_scheme == AUTH_NONE:
            return ""
        if self.key_type == KEY_TYPE_STATIC:
            return self.key_value
        if self.key_type == KEY_TYPE_ENV:
            value = os.environ.get(self.key_value.strip())
            if not value:
                raise ConfigError(
                    f"environment variable {self.key_value!r} is not set"
                )
            return value
        if self.key_type == KEY_TYPE_COMMAND:
            try:
                completed = subprocess.run(
                    shlex.split(self.key_value, posix=os.name != "nt"),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, ValueError) as exc:
                raise ConfigError(f"key command failed: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or "").strip().splitlines()
                tail = detail[-1] if detail else f"exit code {completed.returncode}"
                raise ConfigError(f"key command failed: {tail}")
            key = completed.stdout.strip()
            if not key:
                raise ConfigError("key command produced no output")
            return key
        raise ConfigError(f"unknown key type {self.key_type!r}")

    def auth_headers(self) -> dict[str, str]:
        """Build the auth headers for one request."""
        if self.auth_scheme == AUTH_NONE:
            return {}
        key = self.resolve_key()
        if self.auth_scheme == AUTH_BEARER:
            return {"Authorization": f"Bearer {key}"}
        if self.auth_scheme == AUTH_X_API_KEY:
            return {"x-api-key": key}
        if self.auth_scheme == AUTH_RAW:
            return {"Authorization": key}
        if self.auth_scheme == AUTH_CUSTOM:
            return {self.auth_header.strip(): key}
        raise ConfigError(f"unknown auth scheme {self.auth_scheme!r}")

    # -- display -----------------------------------------------------------

    def masked_key(self) -> str:
        """Render the credential setting without leaking a static key."""
        value = self.key_value
        if self.auth_scheme == AUTH_NONE:
            return "(none)"
        if not value:
            return "(not set)"
        if self.key_type != KEY_TYPE_STATIC:
            # Env var names and commands are not secrets; show them in full so
            # a typo is visible.
            return value
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}{'*' * 6}{value[-4:]}"

    def endpoint(self) -> str:
        """The URL a completion request goes to."""
        base = self.base_url.rstrip("/")
        if self.wire == WIRE_ANTHROPIC:
            return f"{base}/v1/messages"
        return f"{base}/chat/completions"

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provider":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("name", "unnamed")
        provider = cls(**kwargs)
        if not provider.model:
            provider.model = DEFAULT_MODELS.get(provider.wire, "")
        # The saved model list ends up in a picker and in request bodies, so a
        # hand-edited file that put a number or a null in it is cleaned here
        # rather than surfacing three layers away.
        if isinstance(provider.models, list):
            provider.models = [
                item.strip()
                for item in provider.models
                if isinstance(item, str) and item.strip()
            ]
        else:
            provider.models = []
        # Same reasoning as the model list: a hand-edited file can put
        # anything here, and a non-dict would break every request build.
        if not isinstance(provider.extra_headers, dict):
            provider.extra_headers = {}
        return provider

    def copy(self, **changes: Any) -> "Provider":
        return replace(self, **changes)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Everything persisted between runs."""

    providers: list[Provider] = field(default_factory=list)
    active: str = ""
    #: The user's own prompt modes. Built-ins are not stored — they ship with
    #: the code, so persisting copies would freeze a stale prompt on disk.
    modes: list[Mode] = field(default_factory=list)
    #: Name of the mode in effect, built-in or custom. Persisted because a mode
    #: is a preference, not conversation state: picking ``/dev`` once should
    #: still hold tomorrow. It is shown in the banner so it never acts unseen.
    mode: str = ""
    #: Reserved for UI preferences that other modules will grow into.
    ui: dict[str, Any] = field(default_factory=dict)

    path: Path | None = field(default=None, compare=False, repr=False)

    # -- lookup ------------------------------------------------------------

    def get(self, name: str) -> Provider | None:
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def active_provider(self) -> Provider | None:
        if self.active:
            found = self.get(self.active)
            if found is not None:
                return found
        return self.providers[0] if self.providers else None

    def upsert(self, provider: Provider) -> None:
        """Add the provider, or replace the one with the same name."""
        for index, existing in enumerate(self.providers):
            if existing.name == provider.name:
                self.providers[index] = provider
                break
        else:
            self.providers.append(provider)
        if not self.active:
            self.active = provider.name

    def remove(self, name: str) -> bool:
        before = len(self.providers)
        self.providers = [p for p in self.providers if p.name != name]
        if len(self.providers) == before:
            return False
        if self.active == name:
            self.active = self.providers[0].name if self.providers else ""
        return True

    def unique_name(self, base: str) -> str:
        """Return ``base``, or ``base-2``/``base-3``… if it is taken."""
        base = base.strip() or "provider"
        if self.get(base) is None:
            return base
        suffix = 2
        while self.get(f"{base}-{suffix}") is not None:
            suffix += 1
        return f"{base}-{suffix}"

    # -- persistence -------------------------------------------------------

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "active": self.active,
            "mode": self.mode,
            "ui": self.ui,
            "providers": [p.to_dict() for p in self.providers],
            "modes": [m.to_dict() for m in self.modes],
        }
        # Write through a temp file so an interrupted save cannot truncate a
        # working config.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        _restrict_permissions(target)
        self.path = target
        return target


def default_config_path() -> Path:
    """``$VMPC_HOME/config.json``, else ``~/.vmpc/config.json``."""
    home = os.environ.get("VMPC_HOME")
    root = Path(home) if home else Path.home() / ".vmpc"
    return root / "config.json"


def load_config(path: Path | None = None) -> Config:
    """Load the config, returning an empty one if the file does not exist.

    A corrupt file is reported rather than silently replaced — the user's keys
    live in there.
    """
    target = path or default_config_path()
    if not target.exists():
        return Config(path=target)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read config at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config at {target} is not a JSON object")
    raw_providers = raw.get("providers")
    providers = [
        Provider.from_dict(item)
        for item in raw_providers
        if isinstance(item, dict)
    ] if isinstance(raw_providers, list) else []
    # A mode with no name would claim the slash command "/" and shadow nothing
    # usefully, so it is dropped rather than repaired.
    raw_modes = raw.get("modes")
    modes = [
        mode
        for mode in (
            Mode.from_dict(item)
            for item in raw_modes
            if isinstance(item, dict)
        )
        if mode.name
    ] if isinstance(raw_modes, list) else []

    def _as_str(value: Any) -> str:
        # ``str(None)`` is "None", which would quietly become a provider or
        # mode name that matches nothing.
        return value if isinstance(value, str) else ""

    raw_ui = raw.get("ui")
    return Config(
        providers=providers,
        active=_as_str(raw.get("active")),
        modes=modes,
        mode=_as_str(raw.get("mode")),
        ui=raw_ui if isinstance(raw_ui, dict) else {},
        path=target,
    )


def _restrict_permissions(path: Path) -> None:
    """Best-effort tightening of file permissions — the config holds keys.

    On POSIX this is chmod 600. On Windows the file inherits the profile
    directory's ACL, which is already user-scoped; there is no cheap stdlib way
    to tighten it further, so we leave it and rely on the key never being
    echoed back to the screen.
    """
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass
