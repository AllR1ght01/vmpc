"""Dual-stream controller: reasoning and answer, paced onto one transcript.

Modern endpoints emit two logical channels in a single response — the model's
reasoning and its actual answer. Both stream concurrently, both are markdown,
and both must land in one scrollback in a readable order.

The controller keeps a separate :class:`StreamState` per channel so their
markdown never interleaves mid-block, and shares one
:class:`AdaptiveChunkingPolicy` so pacing reflects total display pressure rather
than letting a chatty reasoning channel starve the answer.

Ordering rule: reasoning is drained until the first answer delta arrives. At
that point the reasoning stream is closed out in full and the channel switches
for the rest of the turn. Models do occasionally emit a late reasoning delta
after answer text has started; that is appended to the answer's tail rather than
reopening a closed section, because reopening would mean rewriting scrollback
that has already been committed.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Optional

from rich.text import Text

from vmpc.streaming.chunking import (
    AdaptiveChunkingPolicy,
    ChunkingMode,
    DrainKind,
    QueueSnapshot,
)
from vmpc.streaming.state import StreamState


class StreamKind(Enum):
    REASONING = "reasoning"
    ANSWER = "answer"


#: Emitted once above a reasoning section.
REASONING_HEADER = "Thinking"


class StreamController:
    """Owns both channels of one turn and decides what to commit each tick.

    ``emit`` is called with finalized lines; it is the only way text leaves the
    controller, so a caller can send them to the terminal, a log, or a test
    buffer without the controller knowing which.
    """

    def __init__(
        self,
        emit: Callable[[list[Text], StreamKind], None],
        width: Optional[int] = None,
        show_reasoning: bool = True,
    ) -> None:
        self._emit = emit
        self._show_reasoning = show_reasoning
        self._reasoning = StreamState(width=width)
        self._answer = StreamState(width=width)
        self._policy = AdaptiveChunkingPolicy()
        self._active = StreamKind.REASONING
        self._reasoning_header_shown = False
        self._answer_started = False
        self._finished = False

    # -- lifecycle ---------------------------------------------------------

    def begin_turn(self) -> None:
        self._reasoning.clear()
        self._answer.clear()
        self._policy.reset()
        self._active = StreamKind.REASONING
        self._reasoning_header_shown = False
        self._answer_started = False
        self._finished = False

    def set_width(self, width: Optional[int]) -> None:
        self._reasoning.set_width(width)
        self._answer.set_width(width)

    # -- input -------------------------------------------------------------

    def push_reasoning(self, delta: str) -> None:
        if not self._show_reasoning:
            return
        if self._answer_started:
            # Late reasoning after the answer opened: fold it into the answer
            # tail rather than reopening a committed section.
            self._answer.push_delta(delta)
            return
        if not self._reasoning_header_shown and delta.strip():
            self._reasoning_header_shown = True
            self._emit([Text(REASONING_HEADER, style="reasoning.header")], StreamKind.REASONING)
        self._reasoning.push_delta(delta)

    def push_answer(self, delta: str) -> None:
        if not self._answer_started and delta:
            self._answer_started = True
            self._close_reasoning()
        self._answer.push_delta(delta)

    # -- pacing ------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> bool:
        """Commit one tick's worth of lines. Returns True if anything was emitted."""
        reference = time.monotonic() if now is None else now
        state = self._current_state()
        snapshot = self._combined_snapshot(reference)
        decision = self._policy.decide(snapshot, reference)

        if decision.drain_plan.kind is DrainKind.SINGLE:
            lines = state.step()
        else:
            lines = state.drain_n(decision.drain_plan.count)

        if not lines and state is self._reasoning and self._answer_started:
            # Reasoning is exhausted; move on so the answer is not held up.
            self._active = StreamKind.ANSWER
            lines = self._answer.step()

        if not lines:
            return False
        self._emit(lines, self._active)
        return True

    def drain_until_idle(self) -> None:
        """Commit everything queued, ignoring pacing. Used on interrupt and exit."""
        for kind, state in (
            (StreamKind.REASONING, self._reasoning),
            (StreamKind.ANSWER, self._answer),
        ):
            lines = state.drain_all()
            if lines:
                self._emit(lines, kind)

    def finish_turn(self) -> None:
        """Flush both collectors and everything still queued."""
        if self._finished:
            return
        self._finished = True
        self._reasoning.finalize_collector()
        self._answer.finalize_collector()
        self.drain_until_idle()

    # -- inspection --------------------------------------------------------

    @property
    def mode(self) -> ChunkingMode:
        return self._policy.mode

    @property
    def active_kind(self) -> StreamKind:
        return self._active

    def is_idle(self) -> bool:
        return self._reasoning.is_idle() and self._answer.is_idle()

    def queued_len(self) -> int:
        return self._reasoning.queued_len() + self._answer.queued_len()

    def has_answer_text(self) -> bool:
        return self._answer.has_seen_delta

    # -- internals ---------------------------------------------------------

    def _current_state(self) -> StreamState:
        if self._active is StreamKind.REASONING and not self._reasoning.is_idle():
            return self._reasoning
        if self._answer_started:
            self._active = StreamKind.ANSWER
            return self._answer
        return self._reasoning

    def _combined_snapshot(self, now: float) -> QueueSnapshot:
        """Pressure across both channels, so pacing sees the real backlog."""
        depth = self._reasoning.queued_len() + self._answer.queued_len()
        ages = [
            age
            for age in (
                self._reasoning.oldest_queued_age(now),
                self._answer.oldest_queued_age(now),
            )
            if age is not None
        ]
        return QueueSnapshot(queued_lines=depth, oldest_age=max(ages) if ages else None)

    def _close_reasoning(self) -> None:
        """Flush the reasoning channel completely before the answer begins."""
        if not self._reasoning.has_seen_delta:
            return
        self._reasoning.finalize_collector()
        lines = self._reasoning.drain_all()
        if lines:
            self._emit(lines, StreamKind.REASONING)
        self._emit([Text("")], StreamKind.REASONING)
        self._active = StreamKind.ANSWER
