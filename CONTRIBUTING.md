# Contributing to vmpc

Thanks for looking at this. It's a small hobby project, so the bar is "does
it fit the existing shape" more than a formal process — but a few
conventions are worth knowing before you send a change.

## Setting up

```bash
git clone https://github.com/AllR1ght01/vmpc.git
cd vmpc
pip install -e .
python _smoke.py                                 # should print no errors
python -m py_compile $(find vmpc -name '*.py')    # should print nothing
```

There's no formal test suite yet — `_smoke.py` covers chat save/load/resume,
and a clean `py_compile` pass is the rest of the safety net. If you're
adding something non-trivial, extending `_smoke.py` alongside it is
welcome.

## How the codebase is organized

Worth reading before making structural changes:

- **`vmpc/commands/__init__.py`** — the command table. Adding a command
  means adding one `SlashCommand` entry here (or to `DEV_COMMANDS` if it
  should live behind a mode), not wiring dispatch by hand.
- **`vmpc/modes.py`** — prompt modes and the groups they unlock. Built-in
  modes are rebuilt on every call (`BUILTIN_MODES()`) rather than being a
  static tuple, specifically so `/lang` can change their descriptions
  without a restart — keep that pattern if you add one.
- **`vmpc/session.py`** — the whole of a conversation's state. Deliberately
  thin; if you're adding context-window management or summarization, it
  belongs behind this class's interface, not spread through `ui/app.py`.
- **`vmpc/strings.py`** — every piece of text vmpc prints *about itself*
  (not what the model replies) goes through `t("some.key")` here, in both
  the `"en"` and `"ru"` dictionaries. See "Adding interface text" below.
- **`vmpc/ui/app.py`** — the interactive REPL loop.
- **`vmpc/cli.py`** — the one-shot commands (`vmpc chat`, `vmpc api`, ...).
  These share config, transport, and renderer with the REPL; if you're
  fixing something in one, check whether the same bug exists in the other.

## Adding interface text

If it's text vmpc shows about itself — a command description, a status
label, an error message — it does **not** get hardcoded as a string. Add it
to both dictionaries in `vmpc/strings.py`:

```python
"en": {
    "mycommand.done": "  done",
},
"ru": {
    "mycommand.done": "  готово",
},
```

and call it with `t("mycommand.done")`. A couple of things that trip people
up:

- If the default value of a function argument or dataclass field would be
  the text (e.g. `header: str = "Working"`), that default is evaluated once
  at import time — before `/lang` could possibly have run — and would
  freeze in whatever language happened to be active first. Use `None` as
  the sentinel and resolve it lazily (see `StatusIndicator.__post_init__`
  in `vmpc/render/status.py`, or `BUILTIN_MODES()` in `vmpc/modes.py`, for
  the pattern).
- Russian has three plural forms, not two. Use `ru_plural(n, one, few,
  many)` from `vmpc/strings.py` rather than hand-rolling a suffix — `1
  файл`, `2 файла`, `5 файлов`.
- Technical/protocol identifiers (`bearer`, `x-api-key`, `env-var`) are
  left in English on purpose — they're literal header names and CLI-style
  tokens, not prose, and translating them would make them harder to
  recognize, not easier.
- After adding keys, a quick parity check catches typos before they ship:

  ```bash
  python -c "
  from vmpc.strings import _STRINGS
  print(set(_STRINGS['en']) ^ set(_STRINGS['ru']))
  "
  ```

  An empty set means every key exists in both languages.

## Style

The docstrings in this codebase tend to explain *why* something is shaped
the way it is, especially where the obvious approach would have been
different (see `session.reset()`, or the top of `vmpc/context.py`). When
you add something non-obvious, a short "why" comment saves the next person
from "fixing" it back to the obvious-but-wrong version. When you change
something that already has one of these comments, check whether the reason
still applies — if it doesn't, update or remove the comment along with the
code.

## Sending a change

- Keep it focused — one command, one bug, one feature per change is easier
  to review than a grab-bag.
- Run the checks in "Setting up" above before sending.
- Say what you tested it against (which endpoint / wire format), since
  `/api`'s wizard covers enough variations that "works for me" doesn't
  always generalize.
