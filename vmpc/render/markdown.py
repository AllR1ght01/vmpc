"""Block-level markdown for a stream.

Two entry points:

- :func:`render_markdown` renders a complete document, for text that is already
  in hand.
- :class:`MarkdownStreamCollector` renders a document that is still arriving.

The collector is the reason this module exists instead of a call to
``rich.markdown``. It is *newline-gated*: deltas accumulate, and a line is only
rendered once its terminating newline has arrived. Rendered lines are final and
are never revisited, which is what lets the transcript live in the terminal's
own scrollback instead of in a redrawn buffer.

Two constructs cannot be rendered a line at a time and are held back until the
block closes:

- **tables**, because column widths depend on every row, so they are held until
  a non-row line closes the block;
- **paragraph lines that ``===``/``---`` on the next line would promote into a
  heading**, held only for the rest of the current delta batch. Whichever way
  that hold is resolved costs something: holding across batches would put every
  paragraph line one delta behind the stream, so a setext heading split across
  two deltas renders as a paragraph plus its underline instead.

Fenced code streams out line by line without syntax highlighting. Highlighting
would need the whole block, and holding a long code block back until its closing
fence is worse than losing color on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from rich.text import Text

from vmpc.render.inline import render_inline

_ATX_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<body>.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(?P<indent>\s*)(?P<fence>```+|~~~+)\s*(?P<info>.*)$")
_BULLET = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+])\s+(?P<body>.*)$")
_ORDERED = re.compile(r"^(?P<indent>\s*)(?P<number>\d{1,9})[.)]\s+(?P<body>.*)$")
_QUOTE = re.compile(r"^\s*>\s?(?P<body>.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_SETEXT = re.compile(r"^\s*(?P<char>=+|-+)\s*$")

#: Bullet glyphs by nesting depth, cycling after the third level.
_BULLETS = ("•", "◦", "▪")

#: Left gutter for fenced code, so a block is visible without a box.
_CODE_GUTTER = "  "


@dataclass
class _TableBuffer:
    """Rows held back until the table ends and widths can be computed."""

    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    has_divider: bool = False

    def is_empty(self) -> bool:
        return not self.header and not self.rows


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


class MarkdownStreamCollector:
    """Accumulates markdown deltas and emits finalized rendered lines.

    Usage is a loop of :meth:`push` then :meth:`commit_ready`, and a single
    :meth:`finalize` when the stream ends to flush the trailing partial line and
    any held-back block.
    """

    def __init__(self, width: Optional[int] = None) -> None:
        self._width = width
        self._pending = ""
        self._in_fence = False
        self._fence_marker = ""
        self._table: _TableBuffer = _TableBuffer()
        #: Held so a ``===``/``---`` on the next line can retroactively turn it
        #: into a heading.
        self._held_paragraph: Optional[str] = None

    # -- input -------------------------------------------------------------

    def push(self, delta: str) -> None:
        self._pending += delta

    def clear(self) -> None:
        self._pending = ""
        self._in_fence = False
        self._fence_marker = ""
        self._table = _TableBuffer()
        self._held_paragraph = None

    def set_width(self, width: Optional[int]) -> None:
        self._width = width

    # -- output ------------------------------------------------------------

    def commit_ready(self) -> list[Text]:
        """Render every line whose newline has arrived and whose block is closed."""
        if "\n" not in self._pending:
            return []
        head, _, self._pending = self._pending.rpartition("\n")
        out: list[Text] = []
        for line in head.split("\n"):
            out.extend(self._consume(line))
        # A paragraph line is held only long enough for the next line *in this
        # batch* to promote it into a setext heading. Holding it across batches
        # would leave every paragraph line one delta behind the stream, which is
        # the common case paying for a rare one. Tables still hold across
        # batches: their widths genuinely cannot be known early.
        out.extend(self._flush_held())
        return out

    def finalize(self) -> list[Text]:
        """Flush the trailing partial line and any held-back block."""
        out: list[Text] = []
        if self._pending:
            trailing, self._pending = self._pending, ""
            out.extend(self._consume(trailing))
        out.extend(self._flush_held())
        out.extend(self._flush_table())
        if self._in_fence:
            # The stream ended mid-fence. Close it visually so the transcript
            # does not look truncated.
            self._in_fence = False
            self._fence_marker = ""
        return out

    # -- line dispatch -----------------------------------------------------

    def _consume(self, line: str) -> list[Text]:
        line = line.rstrip("\r")

        if self._in_fence:
            return self._consume_in_fence(line)

        fence = _FENCE.match(line)
        if fence:
            out = self._flush_held() + self._flush_table()
            self._in_fence = True
            self._fence_marker = fence.group("fence")[:3]
            info = fence.group("info").strip()
            label = f"{self._fence_marker}{info}" if info else self._fence_marker
            out.append(Text(f"{_CODE_GUTTER}{label}", style="md.code_fence"))
            return out

        if _TABLE_ROW.match(line):
            return self._consume_table_row(line)
        if not self._table.is_empty():
            # A non-row ends the table; the current line still needs handling.
            return self._flush_table() + self._consume(line)

        # A setext underline retroactively promotes the held paragraph line.
        setext = _SETEXT.match(line)
        if setext and self._held_paragraph is not None:
            body = self._held_paragraph
            self._held_paragraph = None
            level = "md.h1" if setext.group("char").startswith("=") else "md.h2"
            return [render_inline(body, level)]

        out = self._flush_held()

        if not line.strip():
            out.append(Text(""))
            return out

        if _RULE.match(line):
            out.append(self._rule())
            return out

        heading = _ATX_HEADING.match(line)
        if heading:
            level = min(len(heading.group("hashes")), 3)
            # The '#' marks are kept, per the Codex style guide: they carry the
            # level even where bold does not survive the terminal theme.
            hashes = "#" * len(heading.group("hashes"))
            rendered = Text(f"{hashes} ", style=f"md.h{level}")
            rendered.append_text(render_inline(heading.group("body"), f"md.h{level}"))
            out.append(rendered)
            return out

        quote = _QUOTE.match(line)
        if quote:
            rendered = Text("│ ", style="md.quote")
            rendered.append_text(render_inline(quote.group("body"), "md.quote"))
            out.append(rendered)
            return out

        bullet = _BULLET.match(line)
        if bullet:
            depth = len(bullet.group("indent")) // 2
            glyph = _BULLETS[depth % len(_BULLETS)]
            rendered = Text("  " * depth)
            rendered.append(f"{glyph} ", style="md.bullet")
            rendered.append_text(render_inline(bullet.group("body")))
            out.append(rendered)
            return out

        ordered = _ORDERED.match(line)
        if ordered:
            depth = len(ordered.group("indent")) // 2
            rendered = Text("  " * depth)
            rendered.append(f"{ordered.group('number')}. ", style="md.bullet")
            rendered.append_text(render_inline(ordered.group("body")))
            out.append(rendered)
            return out

        # A plain paragraph line is held for exactly one line, in case the next
        # one is a setext underline.
        self._held_paragraph = line
        return out

    def _consume_in_fence(self, line: str) -> list[Text]:
        fence = _FENCE.match(line)
        if fence and fence.group("fence").startswith(self._fence_marker):
            self._in_fence = False
            self._fence_marker = ""
            return [Text(f"{_CODE_GUTTER}```", style="md.code_fence")]
        return [Text(f"{_CODE_GUTTER}{line}", style="md.code_block")]

    def _consume_table_row(self, line: str) -> list[Text]:
        out = self._flush_held()
        if _TABLE_DIVIDER.match(line):
            self._table.has_divider = True
            return out
        cells = _split_row(line)
        if not self._table.header:
            self._table.header = cells
        else:
            self._table.rows.append(cells)
        return out

    # -- held-back blocks --------------------------------------------------

    def _flush_held(self) -> list[Text]:
        if self._held_paragraph is None:
            return []
        body, self._held_paragraph = self._held_paragraph, None
        return [render_inline(body)]

    def _flush_table(self) -> list[Text]:
        table = self._table
        self._table = _TableBuffer()
        if table.is_empty():
            return []
        if not table.has_divider and not table.rows:
            # A single pipe-ish line that never became a table; emit verbatim.
            return [render_inline(" | ".join(table.header))]
        return _render_table(table, self._width)

    def _rule(self) -> Text:
        width = self._width or 60
        return Text("─" * max(width - 2, 8), style="md.rule")


def _render_table(table: _TableBuffer, width: Optional[int]) -> list[Text]:
    """Lay a held-back table out on measured column widths."""
    columns = max([len(table.header)] + [len(row) for row in table.rows] or [0])
    if columns == 0:
        return []

    def cell(row: list[str], index: int) -> str:
        return row[index] if index < len(row) else ""

    header_texts = [render_inline(cell(table.header, i), "md.table_header") for i in range(columns)]
    body_texts = [
        [render_inline(cell(row, i)) for i in range(columns)] for row in table.rows
    ]

    widths = [header_texts[i].cell_len for i in range(columns)]
    for row in body_texts:
        for i, text in enumerate(row):
            widths[i] = max(widths[i], text.cell_len)

    if width is not None:
        # Shrink the widest column until the table fits, rather than letting the
        # terminal hard-wrap the border into garbage.
        overhead = 3 * (columns - 1) + 4
        budget = max(width - overhead, columns * 3)
        while sum(widths) > budget:
            widest = max(range(columns), key=lambda i: widths[i])
            if widths[widest] <= 3:
                break
            widths[widest] -= 1

    def compose(cells: list[Text]) -> Text:
        line = Text("  ")
        for index, text in enumerate(cells):
            piece = text.copy()
            piece.truncate(widths[index], overflow="ellipsis", pad=True)
            line.append_text(piece)
            if index != columns - 1:
                line.append(" │ ", style="md.table_border")
        return line

    lines = [compose(header_texts)]
    divider = Text("  ")
    for index, column_width in enumerate(widths):
        divider.append("─" * column_width, style="md.table_border")
        if index != columns - 1:
            divider.append("─┼─", style="md.table_border")
    lines.append(divider)
    lines.extend(compose(row) for row in body_texts)
    return lines


def render_markdown(source: str, width: Optional[int] = None) -> list[Text]:
    """Render a complete markdown document to final lines."""
    collector = MarkdownStreamCollector(width=width)
    collector.push(source if source.endswith("\n") else source + "\n")
    lines = collector.commit_ready()
    lines.extend(collector.finalize())
    return lines


def iter_markdown(chunks: Iterable[str], width: Optional[int] = None) -> Iterator[Text]:
    """Stream rendered lines from an iterable of markdown deltas."""
    collector = MarkdownStreamCollector(width=width)
    for chunk in chunks:
        collector.push(chunk)
        yield from collector.commit_ready()
    yield from collector.finalize()
