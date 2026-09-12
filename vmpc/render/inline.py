"""Inline markdown to :class:`rich.text.Text`.

Written by hand rather than delegated to ``rich.markdown`` because that renderer
needs a whole document: it re-parses and re-emits everything it is given, which
makes it unusable for a stream where a line must be rendered once, final, and
never touched again. This one converts a single line in isolation.

Supported: ``**bold**``, ``__bold__``, ``*italic*``, ``_italic_``,
``~~strike~~``, `` `code` ``, ``[text](url)`` and bare URLs. Code spans win over
everything inside them, which is what stops a snippet like ``a * b * c`` from
turning into accidental italics.
"""

from __future__ import annotations

import re

from rich.text import Text

# One alternation so the leftmost match always wins, which gives code spans
# priority in the common cases and keeps the scan single-pass.
_INLINE = re.compile(
    r"""
    (?P<code>`+)(?P<code_body>.+?)(?P=code)
  | \[(?P<link_text>[^\]]*)\]\((?P<link_url>[^)\s]+)\)
  | (?P<strong>\*\*|__)(?P<strong_body>\S.*?\S|\S)(?P=strong)
  | (?P<strike>~~)(?P<strike_body>\S.*?\S|\S)~~
  | (?P<em>[*_])(?P<em_body>\S.*?\S|\S)(?P=em)
  | (?P<url>https?://[^\s<>()\[\]]+)
    """,
    re.VERBOSE | re.DOTALL,
)


def render_inline(source: str, base_style: str = "") -> Text:
    """Render one line of inline markdown.

    ``base_style`` is applied underneath the inline styles, so a header can pass
    ``"md.h1"`` and still get bold code spans inside it.
    """
    text = Text(style=base_style)
    position = 0
    for match in _INLINE.finditer(source):
        if match.start() > position:
            text.append(source[position : match.start()])
        _append_match(text, match)
        position = match.end()
    if position < len(source):
        text.append(source[position:])
    return text


def _append_link(text: Text, label: str, url: str) -> None:
    """Append ``label`` styled as a link and carrying an OSC 8 target.

    The two have to be applied in separate passes. A rich style string is a list
    of attributes and colors, so ``"md.link link <url>"`` is not "the theme's
    md.link, plus a link" — it is an attempt to parse ``md.link`` as a color,
    which fails and leaves the label with no style and no hyperlink at all.
    Naming the theme style on append and layering the link as a span keeps both.
    """
    from rich.style import Style

    # Rich resolves named theme styles only when rendering a span. A second
    # span containing just ``link=...`` replaces the first span's color on
    # terminals that support OSC 8, leaving the link underlined but uncolored.
    # Keep the link attributes together so both survive that resolution step.
    from vmpc.style import LILAC

    start = len(text)
    text.append(label, style="md.link")
    # Keep the named theme style above so the link role remains discoverable by
    # the theme and style checks, then add the URL and explicit color together.
    text.stylize(
        Style(color=LILAC, underline=True, link=url), start, len(text)
    )


def _append_match(text: Text, match: "re.Match[str]") -> None:
    group = match.lastgroup
    if match.group("code") is not None:
        # Strip one leading/trailing space, per CommonMark, so `` ` `` renders.
        body = match.group("code_body")
        if len(body) > 1 and body.startswith(" ") and body.endswith(" "):
            body = body[1:-1]
        text.append(body, style="md.code")
        return
    if match.group("link_url") is not None:
        # OSC 8 hyperlink where the terminal supports it; the visible label
        # stays short either way.
        _append_link(
            text, match.group("link_text") or match.group("link_url"),
            match.group("link_url"),
        )
        return
    if match.group("strong") is not None:
        text.append_text(render_inline(match.group("strong_body"), "md.bold"))
        return
    if match.group("strike") is not None:
        text.append_text(render_inline(match.group("strike_body"), "md.strike"))
        return
    if match.group("em") is not None:
        text.append_text(render_inline(match.group("em_body"), "md.italic"))
        return
    if match.group("url") is not None:
        url = match.group("url")
        # Trailing punctuation is the sentence's, not the address's: a bare
        # URL at the end of a line would otherwise swallow the full stop.
        trimmed = url.rstrip(".,;:!?'\"")
        _append_link(text, trimmed, trimmed)
        if len(trimmed) < len(url):
            text.append(url[len(trimmed):])
        return
    # Unreachable while the pattern only contains the groups above; append the
    # raw match rather than dropping text if that ever changes.
    text.append(match.group(0))
    del group


def plain_width(text: Text) -> int:
    """Cell width of rendered text, for table layout."""
    return text.cell_len
