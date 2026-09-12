"""The dual-thread turn runner.

One turn runs on two threads:

- a **network thread** that owns the HTTP stream and pushes
  :class:`~vmpc.api.events.StreamEvent` into a queue, blocking on the socket
  without touching the terminal;
- the **display thread** (the caller's, normally the main thread) that drains
  that queue, feeds the dual-stream controller, and paces committed lines onto
  the screen at a fixed tick.

Splitting them is what decouples display pacing from network jitter. A burst of
twenty lines arriving in one TCP segment still types out smoothly, and a stalled
socket cannot freeze the spinner. It also gives interrupt a clean meaning: set
the cancel event, and the network thread stops between frames while the display
thread flushes what was already committed.

Finalized lines are printed with :meth:`rich.live.Live.console.print`, which
writes *above* the live region — so the transcript accumulates in the terminal's
own scrollback and stays selectable, copyable and scrollable with the mouse.
Only the status row is ever redrawn.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from rich.console import Console
from rich.live import Live
from rich.text import Text

from vmpc.api.events import ApiError, EventKind, StreamEvent, Usage
from vmpc.render.status import StatusIndicator
from vmpc.streaming.controller import StreamController, StreamKind

#: Baseline commit tick. In smooth mode one line lands per tick, which is the
#: perceived typing speed; catch-up mode drains the backlog regardless.
COMMIT_TICK = 0.040

#: How often the status row is redrawn. Independent of the commit tick so the
#: spinner stays alive even when no text is arriving.
FRAME_RATE = 20


@dataclass
class TurnResult:
    """What a completed turn produced."""

    text: str = ""
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    interrupted: bool = False
    error: Optional[ApiError] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.interrupted


class _Producer(threading.Thread):
    """Runs the HTTP stream and posts events onto a queue."""

    def __init__(
        self,
        source: Callable[[threading.Event], Iterator[StreamEvent]],
        sink: "queue.Queue[object]",
        cancel: threading.Event,
    ) -> None:
        super().__init__(name="vmpc-net", daemon=True)
        self._source = source
        self._sink = sink
        self._cancel = cancel

    def run(self) -> None:
        try:
            for event in self._source(self._cancel):
                self._sink.put(event)
                if self._cancel.is_set():
                    break
        except ApiError as exc:
            self._sink.put(exc)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, not swallowed
            self._sink.put(ApiError(f"unexpected transport failure: {exc}"))
        finally:
            self._sink.put(_SENTINEL)


class _Sentinel:
    """Marks the end of the producer's output."""


_SENTINEL = _Sentinel()


def run_turn(
    console: Console,
    source: Callable[[threading.Event], Iterator[StreamEvent]],
    *,
    header: Optional[str] = None,
    show_reasoning: bool = True,
    cancel: Optional[threading.Event] = None,
) -> TurnResult:
    """Run one streamed turn and render it. Returns once the stream is done.

    ``source`` is a callable taking the cancel event and returning an iterator of
    stream events — normally a ``partial`` around
    :func:`vmpc.api.client.stream_chat`. Keeping it a callable rather than an
    iterator means the request itself is issued on the network thread, so even
    connection setup does not block the first frame.

    ``header=None`` (the default) defers to :class:`StatusIndicator`'s own
    default, resolved at construction time rather than baked into this
    function's signature — same reasoning as the dataclass field itself.
    """
    cancel = cancel or threading.Event()
    events: "queue.Queue[object]" = queue.Queue()
    result = TurnResult()

    answer_parts: list[str] = []
    reasoning_parts: list[str] = []

    width = max(console.size.width - 2, 20)
    status = StatusIndicator(
        header=header,
        truecolor=console.color_system == "truecolor",
    )

    pending: list[tuple[list[Text], StreamKind]] = []

    def emit(lines: list[Text], kind: StreamKind) -> None:
        pending.append((lines, kind))

    controller = StreamController(emit, width=width, show_reasoning=show_reasoning)
    controller.begin_turn()

    producer = _Producer(source, events, cancel)
    producer.start()

    def flush() -> None:
        """Write committed lines above the live region, into scrollback."""
        while pending:
            lines, _kind = pending.pop(0)
            for line in lines:
                console.print(line, markup=False, highlight=False, soft_wrap=False)

    finished = False
    next_tick = time.monotonic()

    try:
        with Live(
            status,
            console=console,
            refresh_per_second=FRAME_RATE,
            transient=True,
        ) as live:
            while True:
                # 1. Drain whatever the network thread has posted. Non-blocking
                #    so the tick below keeps its cadence.
                while True:
                    try:
                        item = events.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(item, _Sentinel):
                        finished = True
                        continue
                    if isinstance(item, ApiError):
                        result.error = item
                        finished = True
                        continue
                    if isinstance(item, StreamEvent):
                        if item.kind is EventKind.ANSWER:
                            answer_parts.append(item.text)
                            controller.push_answer(item.text)
                        elif item.kind is EventKind.REASONING:
                            reasoning_parts.append(item.text)
                            controller.push_reasoning(item.text)
                        elif item.kind is EventKind.USAGE and item.usage:
                            result.usage = _merge_usage(result.usage, item.usage)
                            status.tokens = result.usage.total
                        elif item.kind is EventKind.DONE:
                            result.stop_reason = item.stop_reason or result.stop_reason

                # 2. Commit at most one tick's worth of lines.
                now = time.monotonic()
                if now >= next_tick:
                    controller.tick(now)
                    next_tick = now + COMMIT_TICK

                if pending:
                    flush()
                    live.refresh()

                if cancel.is_set():
                    result.interrupted = True
                    break

                if finished and controller.is_idle():
                    break

                # 3. Idle politely. Short enough that the queue never sits, long
                #    enough that the loop is not a spin.
                if controller.is_idle() and events.empty() and not finished:
                    time.sleep(0.008)
                else:
                    time.sleep(0.002)

            controller.finish_turn()
            flush()
    finally:
        cancel.set()
        producer.join(timeout=2.0)

    result.text = "".join(answer_parts)
    result.reasoning = "".join(reasoning_parts)
    return result


def _merge_usage(existing: Usage, incoming: Usage) -> Usage:
    """Combine usage reports; later ones refine rather than replace."""
    return Usage(
        input_tokens=incoming.input_tokens or existing.input_tokens,
        output_tokens=max(incoming.output_tokens, existing.output_tokens),
    )


def collect_turn(
    source: Callable[[threading.Event], Iterator[StreamEvent]],
    cancel: Optional[threading.Event] = None,
) -> TurnResult:
    """Run a turn with no display, for non-interactive use.

    Same event handling as :func:`run_turn`, minus the pacing and the terminal.
    """
    cancel = cancel or threading.Event()
    result = TurnResult()
    answer: list[str] = []
    reasoning: list[str] = []
    try:
        for event in source(cancel):
            if event.kind is EventKind.ANSWER:
                answer.append(event.text)
            elif event.kind is EventKind.REASONING:
                reasoning.append(event.text)
            elif event.kind is EventKind.USAGE and event.usage:
                result.usage = _merge_usage(result.usage, event.usage)
            elif event.kind is EventKind.DONE:
                result.stop_reason = event.stop_reason or result.stop_reason
    except ApiError as exc:
        result.error = exc
    result.text = "".join(answer)
    result.reasoning = "".join(reasoning)
    return result


def stream_to_stdout(
    source: Callable[[threading.Event], Iterator[StreamEvent]],
    cancel: Optional[threading.Event] = None,
    include_reasoning: bool = False,
) -> TurnResult:
    """Write raw answer deltas straight to stdout, for pipes.

    No markdown rendering and no pacing: when output is redirected the consumer
    wants the model's own bytes, not something laid out for a terminal.
    """
    import sys

    cancel = cancel or threading.Event()
    result = TurnResult()
    answer: list[str] = []
    reasoning: list[str] = []
    try:
        for event in source(cancel):
            if event.kind is EventKind.ANSWER:
                answer.append(event.text)
                sys.stdout.write(event.text)
                sys.stdout.flush()
            elif event.kind is EventKind.REASONING:
                reasoning.append(event.text)
                if include_reasoning:
                    sys.stderr.write(event.text)
                    sys.stderr.flush()
            elif event.kind is EventKind.USAGE and event.usage:
                result.usage = _merge_usage(result.usage, event.usage)
            elif event.kind is EventKind.DONE:
                result.stop_reason = event.stop_reason or result.stop_reason
    except ApiError as exc:
        result.error = exc
    if answer and not answer[-1].endswith("\n"):
        sys.stdout.write("\n")
        sys.stdout.flush()
    result.text = "".join(answer)
    result.reasoning = "".join(reasoning)
    return result
