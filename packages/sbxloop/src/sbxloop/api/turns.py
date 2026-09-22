"""TurnCoordinator: when accepted chat turns run.

Each channel is a FIFO lane: its turns run strictly one after another, in
the order they were submitted, because a turn reads the replies of the one
before it. Different channels share one pool of ``[concierge]
max_concurrent_turns`` workers, so they overlap up to that width and no
further. A lane has at most one turn on the pool at a time; the next one is
handed over when it finishes, so a busy channel never holds more than one
worker.

The coordinator only schedules. What running, cancelling or recovering a
turn means (the store's statuses, the participants, the replies) belongs
to the callables the caller submits (:class:`sbxloop.api.context.ApiContext`).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from sbxloop.log import get_logger

log = get_logger(__name__)


class ChannelTurn(Protocol):
    """What the coordinator reads from a turn: its id and its lane."""

    @property
    def id(self) -> str: ...

    @property
    def channel_id(self) -> str: ...


@dataclass(eq=False)
class _Entry:
    turn_id: str
    channel_id: str
    run: Callable[[], None]
    cancel: Callable[[], bool] | None
    cancelled: bool = False
    future: Future[None] | None = field(default=None, repr=False)


class TurnCoordinator:
    def __init__(self, width: int, *, thread_name_prefix: str = "sbxloop-collaboration") -> None:
        if width < 1:
            raise ValueError("a turn coordinator needs at least one worker")
        self.width = width
        self._executor = ThreadPoolExecutor(
            max_workers=width, thread_name_prefix=thread_name_prefix
        )
        self._changed = threading.Condition()
        #: Turns waiting behind their channel's current one.
        self._lanes: dict[str, deque[_Entry]] = {}
        #: Each channel's current turn: handed to the pool, running or about to.
        self._current: dict[str, _Entry] = {}
        self._closed = False

    def submit(
        self,
        turn: ChannelTurn,
        run: Callable[[], None],
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Queue ``run`` at the back of the turn's channel lane.

        ``cancel`` is called by :meth:`cancel_channel`: for a turn that has
        not started it is the only effect (``run`` is then never called), so
        it must settle the turn whatever state its channel is in; for the
        running turn it is a request the turn is expected to observe. It
        returns whether it stopped the turn (False for one already over).
        """
        entry = _Entry(turn.id, turn.channel_id, run, cancel)
        with self._changed:
            if self._closed:
                raise RuntimeError("the turn coordinator is shut down")
            self._lanes.setdefault(entry.channel_id, deque()).append(entry)
            if entry.channel_id not in self._current:
                self._dispatch(entry.channel_id)

    def cancel_channel(self, channel_id: str, *, keep: str | None = None) -> list[str]:
        """Cancel the channel's queued turns and ask its current one to stop.

        Returns the ids of the turns it stopped, the current turn first, then
        the queued ones in lane order. A turn whose ``cancel`` reports it was
        already over, or raises, is left out, as is a current turn without a
        ``cancel``; a queued turn without one is simply dropped and counted.
        ``keep`` names a turn to leave running: the one asking for the stop,
        when the stop is typed in the channel itself.
        """
        with self._changed:
            entries = list(self._lanes.pop(channel_id, ()))
            current = self._current.get(channel_id)
            if current is not None and not current.cancelled and current.turn_id != keep:
                entries.insert(0, current)
            for entry in entries:
                entry.cancelled = True
            self._changed.notify_all()
        stopped: list[str] = []
        for entry in entries:
            if entry.cancel is None:
                if entry is not current:
                    stopped.append(entry.turn_id)
                continue
            try:
                if entry.cancel():
                    stopped.append(entry.turn_id)
            except Exception:
                log.warning(
                    "collaboration.turn_cancel_failed",
                    turn_id=entry.turn_id,
                    channel_id=channel_id,
                    exc_info=True,
                )
        return stopped

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no turn is running or queued; whether that happened."""
        with self._changed:
            return self._changed.wait_for(
                lambda: not self._current and not self._lanes, timeout=timeout
            )

    def shutdown(self, *, wait: bool = False) -> list[str]:
        """Stop taking turns and drop the queued ones.

        Dropped turns are not cancelled: they stay accepted, so the next
        daemon's recovery runs them. Returns their ids. A running turn
        finishes on its own (it watches the daemon's stopping flag);
        ``wait`` blocks until it has.
        """
        with self._changed:
            self._closed = True
            dropped = [entry.turn_id for lane in self._lanes.values() for entry in lane]
            self._lanes.clear()
            self._changed.notify_all()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        with self._changed:
            for channel_id, entry in list(self._current.items()):
                if entry.future is not None and entry.future.cancelled():
                    del self._current[channel_id]
                    dropped.append(entry.turn_id)
            self._changed.notify_all()
        return dropped

    # -- internals -----------------------------------------------------------

    def _dispatch(self, channel_id: str) -> None:
        """Hand the channel's next turn to the pool. Called with the lock."""
        lane = self._lanes.get(channel_id)
        if not lane or self._closed:
            self._lanes.pop(channel_id, None)
            return
        entry = lane.popleft()
        if not lane:
            del self._lanes[channel_id]
        self._current[channel_id] = entry
        try:
            entry.future = self._executor.submit(self._run, entry)
        except RuntimeError:
            # The pool is shutting down under us: the turn stays accepted.
            del self._current[channel_id]
            self._changed.notify_all()

    def _run(self, entry: _Entry) -> None:
        try:
            with self._changed:
                skip = entry.cancelled
            if not skip:
                entry.run()
        except Exception:
            log.exception(
                "collaboration.turn_crashed",
                turn_id=entry.turn_id,
                channel_id=entry.channel_id,
            )
        finally:
            with self._changed:
                if self._current.get(entry.channel_id) is entry:
                    del self._current[entry.channel_id]
                    self._dispatch(entry.channel_id)
                self._changed.notify_all()
