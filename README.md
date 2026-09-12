# vmpc

A terminal chat client for LLM APIs — `rich` + `prompt_toolkit`, streaming
responses, a custom markdown renderer, and a bilingual interface. Not an IDE,
not an agent framework: a fast, keyboard-first way to talk to a model from a
terminal, with the small conveniences (saved chats, prompt modes, file
context) that make that pleasant to do every day.

```
  ╭─ vmpc · claude-sonnet-5 · anthropic
  │  /dev unlocks 6 more commands
  ╰─ /help for commands

  › explain the birthday paradox in one paragraph
```

## Features

- **Any OpenAI- or Anthropic-shaped endpoint** — official APIs, Groq,
  Together, vLLM, LM Studio, OpenRouter, Bedrock gateways, proxies. One
  `/api add` wizard covers the wire format, auth scheme, and key storage
  (static, env var, or a shell command — 1Password, `pass`, vault).
- **Streaming, with a separate reasoning channel** — extended-thinking /
  reasoning-model output renders in its own section, paced against the
  answer so neither channel starves the other.
- **A custom markdown renderer** built for a terminal, not a browser —
  fenced code blocks, tables, and inline formatting without a heavyweight
  dependency.
- **Prompt modes** (`/mode`) — named system prompts you can switch between
  mid-conversation, each optionally unlocking its own commands (the built-in
  `dev` mode unlocks `/raw`, `/render`, `/wire`, `/ping`, `/palette`,
  `/prompt`). Write your own with `/mode add`.
- **File context** (`/context`) — attach a folder or file; it's re-read from
  disk on every turn, so it can't go stale and a saved chat can't balloon
  into a copy of your repository.
- **Saved chats** (`/chats`) — every conversation is a JSON file on disk,
  auto-titled by the model after the first exchange, resumable by id.
- **A bilingual interface** (`/lang`) — vmpc's own text (not the model's
  replies) switches between English and Russian instantly, no restart.
- **One-shot mode** — `vmpc chat "..."`, `vmpc api list`, `vmpc models`,
  `vmpc chats` all work outside the REPL, sharing the same config and
  renderer as the interactive app.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/<your-username>/vmpc.git
cd vmpc
pip install -e .
```

This pulls in `rich`, `prompt_toolkit`, and `httpx` — all pure-Python or
pre-built wheels, so no compiler toolchain is needed. It also works in
[Termux](https://termux.dev/) on Android, for the curious.

## Quickstart

```bash
vmpc          # opens the interactive REPL; /api add walks you through
              # your first endpoint if none is configured yet
```

or, without the REPL:

```bash
vmpc api add                       # configure an endpoint once
vmpc chat "explain how DNS works"  # one-shot, no interactive session
```

## Commands

| Command | Does |
|---|---|
| `/api` | add, switch, or edit API endpoints |
| `/model` | choose the model for the active endpoint |
| `/context` (`/ctx`, `/files`) | attach a folder or file for the model to read |
| `/mode` | switch prompt mode, or write your own |
| `/lang` (`/language`) | switch vmpc's own interface between English and Russian |
| `/reasoning` | toggle streaming of the model's reasoning channel |
| `/new` | start a fresh conversation |
| `/chats` (`/resume`) | reopen a saved conversation |
| `/title` | rename this chat, or let the model name it again |
| `/status` | show the active endpoint and token usage |
| `/clear` | clear the screen and start fresh |
| `/help` | list commands |
| `/quit` (`/exit`) | exit vmpc |

`/mode dev` additionally unlocks:

| Command | Does |
|---|---|
| `/raw` | the last reply's exact source, before rendering |
| `/render` | run markdown through the renderer and show the result |
| `/wire` | the exact HTTP request a turn would send, key masked |
| `/ping` | measure latency to first token and throughput |
| `/palette` | every theme style, and what this terminal supports |
| `/prompt` | the full system prompt currently in effect |

Keys: `enter` sends, `alt+enter` inserts a newline, `esc` interrupts a
running turn, `ctrl+c` clears the input (twice to quit).

## Configuration

Everything lives in `~/.vmpc/config.json` — endpoints, the active mode,
custom modes, and the interface-language preference. There's nothing to
configure by hand; every field is written by a command (`/api`, `/mode`,
`/lang`, `/reasoning`).

## Development

```bash
python -m py_compile $(find vmpc -name '*.py')   # sanity-check the tree
python _smoke.py                                  # exercises chat save/load
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the project's conventions —
in particular, the docstrings throughout the codebase explain *why* a piece
of code is shaped the way it is, not just what it does. Read a few before
changing the ones you're touching; the reasoning is often the point.

## License

No license has been chosen yet. Until one is added, treat this repository
as all-rights-reserved — ask before reusing it beyond personal, local
experimentation. ([choosealicense.com](https://choosealicense.com/) is a
reasonable starting point if you're the one deciding.)
