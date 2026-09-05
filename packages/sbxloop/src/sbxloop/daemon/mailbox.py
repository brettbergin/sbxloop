"""MailboxClient: the ``sbxloop tui`` side of the local chat bridge.

The console is a second process on the daemon host. It must see the
daemon's state and mailbox live, and it must never run the stores' schema
DDL under a running daemon — both stores migrate on open, and after the
per-backend rekey that migration rebuilds tables. So the console opens
both stores **read-only** (``readonly=True``: a ``mode=ro`` URI, no schema
statement, the store's own queries and row shaping) and writes exactly one
kind of row, an inbound mailbox message, through one plain connection of
its own. A store without the mailbox, or at another schema version, is
refused with the fix (start or upgrade the daemon).

Every method takes one lock: the console reads from several worker threads
at once, and a sqlite3 connection tolerates one caller at a time.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import (
    LOCAL_HEARTBEAT_KEY,
    LOCAL_STARTED_KEY,
    OPEN_GATE_STATES,
    ChatThread,
    DaemonStore,
    LocalMessage,
    MergeGate,
    ReviewHold,
)
from sbxloop.engine.model import RunRecord, TaskRecord
from sbxloop.engine.store import StateStore
from sbxloop.log import get_logger
from sbxloop_worker.protocol import Event

log = get_logger(__name__)

#: The bridge stamps the heartbeat every ``LOCAL_HEARTBEAT_S``; this many
#: seconds without one means no daemon is reading the mailbox.
HEARTBEAT_STALE_S = 15.0


class MailboxClient:
    """Read the daemon's state, write inbound mailbox rows; nothing else."""

    def __init__(self, path: Path, *, operator_id: str, operator_name: str | None = None) -> None:
        self.path = path
        self.operator_id = operator_id
        self.operator_name = operator_name or operator_id
        self._lock = threading.RLock()
        self.daemon = DaemonStore(path, readonly=True)
        self.engine = StateStore(path, readonly=True)
        self._rw = sqlite3.connect(path, check_same_thread=False)
        self._rw.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        with self._lock:
            self.daemon.close()
            self.engine.close()
            self._rw.close()

    @contextmanager
    def read_engine(self) -> Iterator[StateStore]:
        """The read-only engine store for a helper that takes a store
        (`classify_sandboxes`, `usage_for_run`), under the client's lock:
        the console's workers share these connections across threads."""
        with self._lock:
            yield self.engine

    # -- liveness ----------------------------------------------------------------

    def _float_value(self, key: str) -> float | None:
        value = self.daemon.get_value(key)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None

    def heartbeat(self) -> float | None:
        """When the local bridge last said it was alive."""
        with self._lock:
            return self._float_value(LOCAL_HEARTBEAT_KEY)

    def daemon_started_at(self) -> float | None:
        with self._lock:
            return self._float_value(LOCAL_STARTED_KEY)

    def daemon_alive(self, *, now: float, stale_after_s: float = HEARTBEAT_STALE_S) -> bool:
        beat = self.heartbeat()
        return beat is not None and now - beat <= stale_after_s

    # -- the transcript ----------------------------------------------------------

    def threads(self) -> list[tuple[str, ChatThread]]:
        """Every run's local thread, newest first."""
        with self._lock:
            return self.daemon.chat_threads("local")

    def thread_for_run(self, run_id: str) -> ChatThread | None:
        with self._lock:
            return self.daemon.chat_thread(run_id, "local")

    def messages(
        self, channel_id: str = TUI_CONTROL_CHANNEL, *, after_id: int = 0, limit: int = 500
    ) -> list[LocalMessage]:
        with self._lock:
            return self.daemon.local_messages(channel_id, after_id=after_id, limit=limit)

    def message(self, message_id: int) -> LocalMessage | None:
        with self._lock:
            return self.daemon.local_message(message_id)

    def changed_since(self, channel_id: str, *, after_id: int, since: float) -> list[LocalMessage]:
        """Rows the console already holds that changed since ``since``."""
        with self._lock:
            return self.daemon.local_changed_since(channel_id, after_id=after_id, since=since)

    def latest_ids(self) -> dict[str, int]:
        with self._lock:
            return self.daemon.local_latest_ids()

    def count_after(self, channel_id: str, after_id: int) -> int:
        with self._lock:
            return self.daemon.local_count_after(channel_id, after_id)

    def gate_open(self, run_id: str) -> bool:
        with self._lock:
            gate = self.daemon.merge_gate_for(run_id)
            return gate is not None and gate.state in OPEN_GATE_STATES

    # -- inbound: the only writes ------------------------------------------------

    def _insert(
        self, channel_id: str, kind: str, text: str, *, reply_to_id: int | None, now: float
    ) -> int:
        with self._lock:
            cur = self._rw.execute(
                "INSERT INTO daemon_local_messages (direction, channel_id, kind, text, "
                "reply_to_id, author_id, author_name, created_at, updated_at) "
                "VALUES ('in', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel_id,
                    kind,
                    text,
                    reply_to_id,
                    self.operator_id,
                    self.operator_name,
                    now,
                    now,
                ),
            )
            self._rw.commit()
            return int(cur.lastrowid or 0)

    def post(
        self, channel_id: str, text: str, *, now: float, reply_to_id: int | None = None
    ) -> int:
        """What the operator typed, verbatim (``@sbx`` and ``!sbx`` included)."""
        return self._insert(channel_id, "message", text, reply_to_id=reply_to_id, now=now)

    def click_choice(self, question_id: int, value: str, *, now: float) -> int:
        question = self.message(question_id)
        channel = question.channel_id if question is not None else TUI_CONTROL_CHANNEL
        return self._insert(channel, "choice", value, reply_to_id=question_id, now=now)

    def approve(self, prompt_id: int, *, now: float) -> int:
        prompt = self.message(prompt_id)
        channel = prompt.channel_id if prompt is not None else TUI_CONTROL_CHANNEL
        return self._insert(channel, "approve", "", reply_to_id=prompt_id, now=now)

    def taken(self, message_ids: Sequence[int]) -> set[int]:
        """Which of the console's own rows the daemon has claimed."""
        with self._lock:
            return self.daemon.local_taken(message_ids)

    # -- the daemon's and the engine's state, read-only --------------------------

    def runs(self, *, limit: int = 200) -> list[RunRecord]:
        with self._lock:
            return self.engine.list_runs()[:limit]

    def run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            try:
                return self.engine.get_run(run_id)
            except Exception:
                return None

    def tasks(self, run_id: str) -> list[TaskRecord]:
        with self._lock:
            return self.engine.get_tasks(run_id)

    def phase_attempts(self, run_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self.engine.phase_attempts(run_id)

    def events(
        self, run_id: str, *, after_seq: int = 0, type_prefix: str | None = None
    ) -> list[tuple[int, Event]]:
        with self._lock:
            return list(self.engine.events(run_id, after_seq=after_seq, type_prefix=type_prefix))

    def last_event_ts(self, run_id: str) -> float | None:
        with self._lock:
            return self.engine.last_event_ts(run_id)

    def last_event_ts_many(self, run_ids: Sequence[str]) -> dict[str, float]:
        with self._lock:
            return self.engine.last_event_ts_many(run_ids)

    def last_event(self, run_id: str, type_prefix: str) -> Event | None:
        with self._lock:
            return self.engine.last_event(run_id, type_prefix)

    def run_items(self) -> dict[str, str]:
        with self._lock:
            return self.daemon.run_items()

    def queued_in_order(self) -> list[WorkItem]:
        """The queue as the daemon will dispatch it."""
        with self._lock:
            return self.daemon.queued_in_order()

    def items(self, states: Sequence[str] | None = None) -> list[WorkItem]:
        with self._lock:
            return self.daemon.items(list(states) if states else None)  # type: ignore[arg-type]

    def item(self, item_id: str) -> WorkItem | None:
        with self._lock:
            return self.daemon.get(item_id)

    def item_for_run(self, run_id: str) -> str | None:
        with self._lock:
            return self.daemon.item_for_run(run_id)

    def gates(self, states: Sequence[str] = OPEN_GATE_STATES) -> list[MergeGate]:
        with self._lock:
            wanted = set(states)
            return [g for g in self.daemon.open_merge_gates() if g.state in wanted]

    def holds(
        self, states: Sequence[str] = ("open", "approving", "fixing", "paused")
    ) -> list[ReviewHold]:
        with self._lock:
            return self.daemon.review_holds(tuple(states))

    def breaker(self) -> tuple[float | None, int]:
        with self._lock:
            return self.daemon.breaker()

    def status_snapshot(self, *, now: float) -> dict[str, Any]:
        """What the store alone can say about the daemon; ``ctl status``
        stays the authority on holds, the claim in flight and the cap."""
        with self._lock:
            queued = self.daemon.queued()
            running = self.daemon.running_items()
            opened_at, failures = self.daemon.breaker()
            gates = len(self.daemon.open_merge_gates())
            holds = len(self.daemon.review_holds(("open", "approving", "fixing", "paused")))
            beat = self._float_value(LOCAL_HEARTBEAT_KEY)
            started = self._float_value(LOCAL_STARTED_KEY)
        current = running[0] if running else None
        return {
            "alive": beat is not None and now - beat <= HEARTBEAT_STALE_S,
            "heartbeat": beat,
            "started_at": started,
            "queued": len(queued),
            "current": (
                {"run_id": current.run_id, "item_id": current.item_id} if current else None
            ),
            "breaker_opened_at": opened_at,
            "consecutive_failures": failures,
            "gates": gates,
            "holds": holds,
        }


__all__ = ["HEARTBEAT_STALE_S", "MailboxClient"]
