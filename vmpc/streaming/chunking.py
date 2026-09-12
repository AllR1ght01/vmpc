"""Adaptive stream chunking policy for commit ticks.

Ported from the Codex TUI (``codex-rs/tui/src/streaming/chunking.rs``), whose
tuning is worth keeping: it is the difference between text that appears to type
itself and text that stutters or lags seconds behind the network.

Two gears:

- :attr:`ChunkingMode.SMOOTH` — drain one queued line per commit tick, so
  output is paced evenly regardless of how bursty the network was.
- :attr:`ChunkingMode.CATCH_UP` — drain the whole backlog each tick, so display
  lag converges as fast as possible.

The transitions use hysteresis: enter catch-up on high-pressure thresholds, exit
only after pressure has stayed low for :data:`EXIT_HOLD`, then suppress
re-entry for :data:`REENTER_CATCH_UP_HOLD` unless the backlog is severe. Without
the holds the policy flaps between gears at the threshold boundary, which looks
worse than either gear alone.

The policy reads only queue depth and queue age. It knows nothing about who
produced the text, which is what lets the reasoning and answer streams share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

#: Queue depth that alone is enough to leave smooth mode.
ENTER_QUEUE_DEPTH_LINES = 8

#: Oldest-line age that alone is enough to leave smooth mode.
ENTER_OLDEST_AGE = 0.120

#: Depth must be at or below this before exit hold timing can start.
EXIT_QUEUE_DEPTH_LINES = 2

#: Age must be at or below this before exit hold timing can start.
EXIT_OLDEST_AGE = 0.040

#: How long pressure must stay below the exit thresholds to leave catch-up.
EXIT_HOLD = 0.250

#: Cooldown after a catch-up exit that suppresses immediate re-entry.
REENTER_CATCH_UP_HOLD = 0.250

#: Depth past which backlog counts as severe and bypasses the re-entry hold.
SEVERE_QUEUE_DEPTH_LINES = 64

#: Age past which backlog counts as severe.
SEVERE_OLDEST_AGE = 0.300


class ChunkingMode(Enum):
    SMOOTH = "smooth"
    CATCH_UP = "catch-up"


class DrainKind(Enum):
    SINGLE = "single"
    BATCH = "batch"


@dataclass(frozen=True)
class DrainPlan:
    """How many queued lines this tick should emit."""

    kind: DrainKind
    count: int = 1

    @classmethod
    def single(cls) -> "DrainPlan":
        return cls(DrainKind.SINGLE, 1)

    @classmethod
    def batch(cls, count: int) -> "DrainPlan":
        return cls(DrainKind.BATCH, max(count, 1))


@dataclass(frozen=True)
class QueueSnapshot:
    """Queue pressure inputs for one decision."""

    queued_lines: int = 0
    #: Age in seconds of the oldest queued line, or ``None`` when empty.
    oldest_age: Optional[float] = None


@dataclass(frozen=True)
class ChunkingDecision:
    mode: ChunkingMode
    #: True only on the tick that transitioned into catch-up, for one-shot
    #: observability.
    entered_catch_up: bool
    drain_plan: DrainPlan


def _should_enter_catch_up(snapshot: QueueSnapshot) -> bool:
    """Either depth or age pressure is enough to trigger catch-up."""
    if snapshot.queued_lines >= ENTER_QUEUE_DEPTH_LINES:
        return True
    return snapshot.oldest_age is not None and snapshot.oldest_age >= ENTER_OLDEST_AGE


def _should_exit_catch_up(snapshot: QueueSnapshot) -> bool:
    """Both depth and age must be low, so one loaded signal cannot cause flap."""
    if snapshot.queued_lines > EXIT_QUEUE_DEPTH_LINES:
        return False
    return snapshot.oldest_age is not None and snapshot.oldest_age <= EXIT_OLDEST_AGE


def _is_severe_backlog(snapshot: QueueSnapshot) -> bool:
    if snapshot.queued_lines >= SEVERE_QUEUE_DEPTH_LINES:
        return True
    return snapshot.oldest_age is not None and snapshot.oldest_age >= SEVERE_OLDEST_AGE


class AdaptiveChunkingPolicy:
    """Tracks gear and hysteresis state across commit ticks."""

    def __init__(self) -> None:
        self._mode = ChunkingMode.SMOOTH
        self._below_exit_threshold_since: Optional[float] = None
        self._last_catch_up_exit_at: Optional[float] = None

    @property
    def mode(self) -> ChunkingMode:
        return self._mode

    def reset(self) -> None:
        self._mode = ChunkingMode.SMOOTH
        self._below_exit_threshold_since = None
        self._last_catch_up_exit_at = None

    def decide(self, snapshot: QueueSnapshot, now: float) -> ChunkingDecision:
        """Compute the drain decision for the current queue snapshot.

        Deterministic for a given ``(mode, snapshot, now)``. Callers should pass
        real snapshots — a synthetic one with stale age can cause a premature
        catch-up exit.
        """
        if snapshot.queued_lines == 0:
            if self._mode is ChunkingMode.CATCH_UP:
                self._last_catch_up_exit_at = now
            self._mode = ChunkingMode.SMOOTH
            self._below_exit_threshold_since = None
            return ChunkingDecision(self._mode, False, DrainPlan.single())

        entered_catch_up = False
        if self._mode is ChunkingMode.SMOOTH:
            entered_catch_up = self._maybe_enter_catch_up(snapshot, now)
        else:
            self._maybe_exit_catch_up(snapshot, now)

        if self._mode is ChunkingMode.SMOOTH:
            plan = DrainPlan.single()
        else:
            plan = DrainPlan.batch(snapshot.queued_lines)
        return ChunkingDecision(self._mode, entered_catch_up, plan)

    def _maybe_enter_catch_up(self, snapshot: QueueSnapshot, now: float) -> bool:
        if not _should_enter_catch_up(snapshot):
            return False
        if self._reentry_hold_active(now) and not _is_severe_backlog(snapshot):
            return False
        self._mode = ChunkingMode.CATCH_UP
        self._below_exit_threshold_since = None
        self._last_catch_up_exit_at = None
        return True

    def _maybe_exit_catch_up(self, snapshot: QueueSnapshot, now: float) -> None:
        if not _should_exit_catch_up(snapshot):
            self._below_exit_threshold_since = None
            return
        since = self._below_exit_threshold_since
        if since is None:
            self._below_exit_threshold_since = now
        elif now - since >= EXIT_HOLD:
            self._mode = ChunkingMode.SMOOTH
            self._below_exit_threshold_since = None
            self._last_catch_up_exit_at = now

    def _reentry_hold_active(self, now: float) -> bool:
        exit_at = self._last_catch_up_exit_at
        return exit_at is not None and now - exit_at < REENTER_CATCH_UP_HOLD
