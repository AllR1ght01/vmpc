<div align="center">

# vmpc

**A terminal chat client for LLM APIs.**
Streaming responses, a live reasoning channel, a markdown renderer built
for a terminal instead of a browser — and an interface that speaks English
or Russian, live, without a restart.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Rich](https://img.shields.io/badge/rendered%20with-rich-ff69b4)](https://github.com/Textualize/rich)
[![Endpoints](https://img.shields.io/badge/endpoints-OpenAI%20%7C%20Anthropic-8A2BE2)](#install)
[![Interface](https://img.shields.io/badge/interface-EN%20%7C%20RU-orange)](#commands)
[![License](https://img.shields.io/badge/license-unset-lightgrey)](#license)

**English** · [Русский](README.ru.md)

</div>

<br>

```
  ╭─ vmpc · claude-sonnet-5 · anthropic
  │  /dev unlocks 6 more commands
  ╰─ /help for commands

  › explain the birthday paradox in one paragraph

  Working  0s4  (Esc to interrupt)
```

Not an IDE, not an agent framework — a fast, keyboard-first way to talk to
a model from a terminal, with the small conveniences (saved chats, prompt
modes, file context) that make that pleasant to do every day.

## Contents

- [Features](#features)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [Configuration](#configuration)
- [Design principles](#design-principles)
- [Development](#development)
- [License](#license)

## Features

|  |  |
|---|---|
|  **Any endpoint** | OpenAI- or Anthropic-shaped: official APIs, Groq, Together, vLLM, LM Studio, OpenRouter, Bedrock gateways, proxies. One `/api add` wizard covers the wire format, auth scheme, and key storage (static, env var, or a shell command — 1Password, `pass`, vault). |
|  **Reasoning channel** | Extended-thinking / reasoning-model output streams in its own section, paced against the answer so neither channel starves the other. |
|  **Terminal-native markdown** | A renderer built for a terminal, not a browser — fenced code, tables, and inline formatting, no headless-browser dependency. |
|  **Prompt modes** | `/mode` switches named system prompts mid-conversation, each optionally unlocking its own commands. Write your own with `/mode add`. |
|  **File context** | `/context` attaches a folder or file, re-read from disk every turn — it can't go stale, and a saved chat can't balloon into a copy of your repository. |
|  **Saved chats** | Every conversation is a JSON file, auto-titled by the model after the first exchange, resumable by id. |
|  **Bilingual interface** | `/lang` switches vmpc's *own* text — not the model's replies — between English and Russian, instantly, no restart. |
|  **One-shot mode** | `vmpc chat "..."`, `vmpc api list`, `vmpc models`, `vmpc chats` all work outside the REPL, sharing config, transport, and renderer with the interactive app. |

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/AllR1ght01/vmpc.git
cd vmpc
pip install -e .
```

This pulls in `rich`, `prompt_toolkit`, and `httpx` — all pure-Python or
pre-built wheels, so no compiler toolchain is needed anywhere, including
[Termux](https://termux.dev/) on Android.

## Quickstart

```bash
vmpc          # opens the interactive REPL — /api add walks you through
              # your first endpoint if none is configured yet
```

or skip the REPL entirely:

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

<details>
<summary><strong>🔧 <code>/mode dev</code> additionally unlocks</strong></summary>
<br>

| Command | Does |
|---|---|
| `/raw` | the last reply's exact source, before rendering |
| `/render` | run markdown through the renderer and show the result |
| `/wire` | the exact HTTP request a turn would send, key masked |
| `/ping` | measure latency to first token and throughput |
| `/palette` | every theme style, and what this terminal supports |
| `/prompt` | the full system prompt currently in effect |

</details>

**Keys:** `enter` sends · `alt+enter` inserts a newline · `esc` interrupts a
running turn · `ctrl+c` clears the input (twice to quit).

## Configuration

Everything lives in `~/.vmpc/config.json` — endpoints, the active mode,
custom modes, and the interface-language preference. There's nothing to
edit by hand; every field is written by a command (`/api`, `/mode`,
`/lang`, `/reasoning`).

## Design principles

A few decisions worth knowing before you dive into the code:

- **A mode is a prompt *and* its commands, paired on purpose.** `/dev`
  isn't just a different system prompt — it's a different toolbox.
  Separating the two would mean remembering to turn on both.
- **Attached context is re-read, never cached.** A folder attached with
  `/context` is walked again on every turn, so it can't drift from disk —
  and a saved chat file can't quietly grow into a snapshot of your repo.
- **Interface text resolves lazily, not at import time.** `/lang` has to
  take effect on the very next line printed, not after a restart — so
  built-in modes, command descriptions, and status labels are all computed
  at the moment they're shown, not baked into a module-level constant.
- **The one-shot CLI and the REPL are one implementation.** `vmpc chat`
  isn't a separate code path that happens to look similar — it shares
  config loading, transport, and rendering with the interactive app, so a
  fix in one is a fix in both.

## Development

```bash
python -m py_compile $(find vmpc -name '*.py')   # sanity-check the tree
python _smoke.py                                  # exercises chat save/load
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full conventions — in
particular, the docstrings throughout the codebase explain *why* a piece of
code is shaped the way it is, not just what it does. Read a few before
changing the ones you're touching; the reasoning is often the point.

## License

No license has been chosen yet. Until one is added, treat this repository
as all-rights-reserved — ask before reusing it beyond personal, local
experimentation. ([choosealicense.com](https://choosealicense.com/) is a
reasonable starting point if you're the one deciding.)

---

<div align="center">
<sub>Built with 🩷 for the terminal.</sub>
</div>

