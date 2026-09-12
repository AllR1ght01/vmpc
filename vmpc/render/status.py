"""The live status row shown above the composer while a turn is running.

Owns spinner timing, the shimmer sweep, the elapsed clock and the interrupt
hint. Everything sits on one line so the layout does not shift while the agent
works — vertical churn under streaming text is what makes a TUI feel unstable.

The shimmer is a port of ``codex-rs/tui/src/shimmer.rs``: a cosine band sweeping
across the word, blending between two levels of the same color. Codex sweeps
between two greys because its style guide forbids hue; vmpc sweeps between two
levels of its own rose instead, so the glow belongs to the palette in
:mod:`vmpc.style` rather than sitting next to it. Terminals without truecolor
get a dim/normal/bold approximation, and reduced-motion mode gets a static label.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Optional

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from vmpc.strings import t

#: Braille spinner; reads as motion even at a low tick rate.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Seconds for one full shimmer sweep.
SWEEP_SECONDS = 2.0

#: Half-width of the highlight band, in characters.
BAND_HALF_WIDTH = 5.0

#: Blank characters of lead-in and lead-out, so the band enters and leaves
#: cleanly instead of popping at the word boundary.
SWEEP_PADDING = 10

#: The two ends of the sweep: the resting rose from :mod:`vmpc.style` and a
#: near-white pink for the crest of the band. Kept as tuples rather than the hex
#: strings over there because :func:`_blend` interpolates per channel.
_SHIMMER_BASE = (185, 120, 143)  # style.ROSE_MUTED
_SHIMMER_HIGHLIGHT = (255, 219, 238)


def reduced_motion() -> bool:
    """Honor the conventional environment switches for animation."""
    return bool(os.environ.get("VMPC_NO_MOTION") or os.environ.get("NO_MOTION"))


def _blend(
    high: tuple[int, int, int], low: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(  # type: ignore[return-value]
        int(round(low[i] + (high[i] - low[i]) * amount)) for i in range(3)
    )


def shimmer_text(label: str, elapsed: float, truecolor: bool = True) -> Text:
    """Render ``label`` with the highlight band at the position for ``elapsed``."""
    if not label:
        return Text("")
    if reduced_motion():
        return Text(label, style="status")

    chars = list(label)
    period = len(chars) + SWEEP_PADDING * 2
    position = (elapsed % SWEEP_SECONDS) / SWEEP_SECONDS * period

    out = Text()
    for index, char in enumerate(chars):
        distance = abs((index + SWEEP_PADDING) - position)
        if distance <= BAND_HALF_WIDTH:
            intensity = 0.5 * (1.0 + math.cos(math.pi * (distance / BAND_HALF_WIDTH)))
        else:
            intensity = 0.0
        if truecolor:
            red, green, blue = _blend(_SHIMMER_HIGHLIGHT, _SHIMMER_BASE, intensity * 0.9)
            out.append(char, style=f"bold rgb({red},{green},{blue})")
        else:
            out.append(char, style=_level_style(intensity))
    return out


def _level_style(intensity: float) -> str:
    if intensity < 0.2:
        return "dim"
    if intensity < 0.6:
        return ""
    return "bold"


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class StatusIndicator:
    """Renderable status row. Instances are cheap; rebuild per frame is fine."""

    #: None means "use vmpc's own default label", resolved in __post_init__ so
    #: a ``/lang`` switch is reflected on the next turn rather than frozen at
    #: import time. Pass "" explicitly to show no header text.
    header: Optional[str] = None
    #: Short context under the header, e.g. the tool currently running.
    detail: str = ""
    started_at: float = 0.0
    #: Same None-means-default convention as ``header`` above.
    interrupt_hint: Optional[str] = None
    tokens: int = 0
    truecolor: bool = True
    _paused_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()
        if self.header is None:
            self.header = t("app.working_header")
        if self.interrupt_hint is None:
            self.interrupt_hint = t("status.interrupt_hint")

    @property
    def elapsed(self) -> float:
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return end - self.started_at

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        if self._paused_at is not None:
            self.started_at += time.monotonic() - self._paused_at
            self._paused_at = None

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        line = Text()
        if reduced_motion():
            line.append("• ", style="status")
        else:
            frame = SPINNER_FRAMES[int(self.elapsed * 10) % len(SPINNER_FRAMES)]
            line.append(f"{frame} ", style="status")

        line.append_text(shimmer_text(self.header, self.elapsed, self.truecolor))
        line.append(f"  {format_elapsed(self.elapsed)}", style="secondary")

        if self.tokens:
            line.append(f"  {_compact_tokens(self.tokens)}{t('status.tok_suffix')}", style="secondary")
        if self.interrupt_hint:
            line.append(f"  ({self.interrupt_hint})", style="secondary")

        # Truncate rather than wrap: a status row that grows to two lines
        # shifts everything above it on every frame.
        line.truncate(max(options.max_width, 8), overflow="ellipsis")
        yield line

        if self.detail:
            detail = Text("  └ ", style="secondary")
            detail.append(self.detail, style="secondary")
            detail.truncate(max(options.max_width, 8), overflow="ellipsis")
            yield detail


def _compact_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}m".replace(".0m", "m")
