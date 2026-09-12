"""Per-stream markdown collection plus a FIFO of committed render lines.

Ported from ``codex-rs/tui/src/streaming/mod.rs``. The invariant that matters is
queue ordering: every drain pops from the front, and enqueue stamps an arrival
time so the pacing policy can reason about how stale the oldest queued line is
without looking at the text.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from rich.text import Text

from vmpc.render.markdown import MarkdownStreamCollector
from vmpc.streaming.chunking import QueueSnapshot


@dataclass
class _QueuedLine:
    line: Text
    enqueued_at: float


class StreamState:
    """In-flight markdown state and the queue of lines waiting to be shown."""

    def __init__(self, width: Optional[int] = None) -> None:
        self.collector = MarkdownStreamCollector(width=width)
        self._queue: "deque[_QueuedLine]" = deque()
        self.has_seen_delta = False

    # -- lifecycle ---------------------------------------------------------

    def clear(self) -> None:
        self.collector.clear()
        self._queue.clear()
        self.has_seen_delta = False

    def clear_queue(self) -> None:
        """Drop queued lines but keep collector and turn state intact."""
        self._queue.clear()

    def set_width(self, width: Optional[int]) -> None:
        self.collector.set_width(width)

    # -- input -------------------------------------------------------------

    def push_delta(self, delta: str) -> None:
        """Feed a network delta in and queue whatever lines it completed."""
        if delta:
            self.has_seen_delta = True
        self.collector.push(delta)
        self.enqueue(self.collector.commit_ready())

    def finalize_collector(self) -> None:
        """Flush the collector's trailing partial line into the queue."""
        self.enqueue(self.collector.finalize())

    def enqueue(self, lines: list[Text]) -> None:
        if not lines:
            return
        now = time.monotonic()
        self._queue.extend(_QueuedLine(line, now) for line in lines)

    # -- output ------------------------------------------------------------

    def step(self) -> list[Text]:
        """Drain exactly one queued line from the front."""
        if not self._queue:
            return []
        return [self._queue.popleft().line]

    def drain_n(self, max_lines: int) -> list[Text]:
        """Drain up to ``max_lines`` from the front, clamped to what is queued."""
        count = min(max_lines, len(self._queue))
        return [self._queue.popleft().line for _ in range(count)]

    def drain_all(self) -> list[Text]:
        return self.drain_n(len(self._queue))

    # -- inspection --------------------------------------------------------

    def is_idle(self) -> bool:
        return not self._queue

    def queued_len(self) -> int:
        return len(self._queue)

    def oldest_queued_age(self, now: Optional[float] = None) -> Optional[float]:
        if not self._queue:
            return None
        reference = time.monotonic() if now is None else now
        return max(reference - self._queue[0].enqueued_at, 0.0)

    def snapshot(self, now: Optional[float] = None) -> QueueSnapshot:
        return QueueSnapshot(
            queued_lines=len(self._queue),
            oldest_age=self.oldest_queued_age(now),
        )
