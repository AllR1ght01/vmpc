"""Color and text style vocabulary for the whole UI.

The structural rules still come from the Codex TUI style guide
(``codex-rs/tui/styles.md``): headers are ``bold``, secondary text is quieter
than primary, body text keeps the terminal's own foreground so it stays readable
on any background, and the status row is the only animated thing on screen.

Where this departs from that guide is hue. Codex sticks to the 16 ANSI colors so
the TUI inherits whatever palette the user already likes. vmpc instead names its
own rose palette in RGB, which trades that inheritance for a consistent look:
the chrome is the same shade of pink everywhere rather than whatever the
terminal calls "magenta".

What that costs, measured rather than assumed. rich and prompt_toolkit both
downsample RGB on their own, so the palette degrades gracefully instead of
breaking:

- truecolor — the shades below, exactly.
- 256 colors — a close match (the brand rose lands on 212, mint on 79, lilac on
  183); the palette still reads as itself.
- 16 colors — hues collapse. The rose survives as bright magenta, errors as
  bright red, but mint, lilac and the muted rose all flatten onto white. Meaning
  never rests on color alone for this reason: errors carry ``✖``, success ``✔``,
  the active endpoint ``●``, so a 16-color terminal loses prettiness, not
  information.
- ``NO_COLOR`` — rich honors it and strips color entirely, leaving bold/italic.

On Windows this only holds once VT processing is on, which is why
:func:`vmpc.cli._enable_windows_vt` runs before the first Console is built —
without it rich reports a legacy console and every shade here collapses to grey.

Body text is deliberately *not* tinted. Pink chrome around terminal-colored
prose reads as a theme; pink prose reads as low contrast.

Everything the UI draws should name a style from here rather than spelling out
a color inline, so a palette change stays a one-file edit.
"""

from __future__ import annotations

from rich.theme import Theme

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

#: Mid-tone hues on purpose. A pastel pink looks lovely on a dark background and
#: vanishes on a white one; these hold contrast both ways.
ROSE = "#ff8ec7"
#: One step up in lightness, for headings and the things that should catch first.
ROSE_BRIGHT = "#ffb3dd"
#: The one saturated accent: cursor, selection, current completion.
ROSE_DEEP = "#ff5aa8"
#: Stands in for ``dim`` — quiet, but still in the family rather than grey.
ROSE_MUTED = "#b9788f"

#: Pulled off the pink so code and links do not read as more chrome.
LILAC = "#c9a3ff"
#: Success. Mint rather than ANSI green: it sits next to pink without clashing.
MINT = "#6fdcae"
#: Errors. Red-orange leaning, so it separates from the magenta-leaning brand
#: pink even for a red-weak eye — and every error also carries a ``✖``.
CORAL = "#ff6b7f"

# --------------------------------------------------------------------------
# rich
# --------------------------------------------------------------------------

#: Named styles handed to :class:`rich.console.Console`.
VMPC_THEME = Theme(
    {
        # Structure.
        "header": f"bold {ROSE_BRIGHT}",
        "secondary": ROSE_MUTED,
        "primary": "none",
        # Roles.
        "agent": f"bold {ROSE}",
        "user": LILAC,
        "success": MINT,
        "error": CORAL,
        "hint": ROSE,
        "status": ROSE,
        # Markdown. Heading levels step down in lightness, which reads as depth
        # without needing three different hues.
        "md.h1": f"bold {ROSE_BRIGHT}",
        "md.h2": f"bold {ROSE}",
        "md.h3": f"bold {ROSE_MUTED}",
        "md.bold": "bold",
        "md.italic": "italic",
        "md.strike": "strike",
        "md.code": LILAC,
        "md.code_block": "none",
        "md.code_fence": ROSE_MUTED,
        "md.link": f"{LILAC} underline",
        "md.quote": f"italic {ROSE_MUTED}",
        "md.bullet": ROSE,
        "md.rule": ROSE_MUTED,
        "md.table_border": ROSE_MUTED,
        "md.table_header": f"bold {ROSE_BRIGHT}",
        # Reasoning is secondary to the answer, so it stays muted and italic.
        "reasoning": f"italic {ROSE_MUTED}",
        "reasoning.header": ROSE_MUTED,
        # Chrome. Only the one rich draws itself — the pickers and the entry
        # fields are prompt_toolkit's, so their styles live in
        # :data:`PTK_STYLE_RULES` below and duplicating them here would define
        # colors nothing ever reads.
        "prompt": f"bold {ROSE}",
    }
)

# --------------------------------------------------------------------------
# prompt_toolkit
# --------------------------------------------------------------------------

#: prompt_toolkit style rules, kept visually in sync with :data:`VMPC_THEME`.
#: prompt_toolkit cannot read a rich Theme, so the overlap is spelled out once.
PTK_STYLE_RULES = {
    "": "",
    "prompt": f"bold {ROSE}",
    "continuation": ROSE_MUTED,
    "placeholder": ROSE_MUTED,
    "bottom-toolbar": f"noreverse {ROSE_MUTED}",
    "bottom-toolbar.text": f"noreverse {ROSE_MUTED}",
    # Selection popups.
    "popup.title": f"bold {ROSE_BRIGHT}",
    "popup.row": "",
    "popup.row.selected": f"bold {ROSE_DEEP}",
    "popup.cursor": f"bold {ROSE_DEEP}",
    "popup.desc": ROSE_MUTED,
    "popup.desc.selected": ROSE,
    "popup.hint": ROSE_MUTED,
    "popup.badge": MINT,
    "popup.error": CORAL,
    # Completion menu for slash commands / file mentions. `noinherit` keeps
    # prompt_toolkit's default reverse-video menu from painting over the theme.
    "completion-menu": "noinherit",
    "completion-menu.completion": f"noinherit {ROSE_MUTED}",
    "completion-menu.completion.current": f"noinherit bold {ROSE_DEEP}",
    "completion-menu.meta.completion": f"noinherit {ROSE_MUTED}",
    "completion-menu.meta.completion.current": f"noinherit {ROSE}",
}


def ptk_style():
    """Build the prompt_toolkit :class:`Style` from :data:`PTK_STYLE_RULES`."""
    from prompt_toolkit.styles import Style

    return Style.from_dict(PTK_STYLE_RULES)
