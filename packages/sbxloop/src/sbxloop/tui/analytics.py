"""What the console's Overview reports: a window of runs folded into the
few numbers that answer "is this performing well".

Three things this module insists on, because each of them was a wrong
answer first:

**Active is not elapsed.** A run's ``updated_at - created_at`` is
dominated by the time it sat waiting for a human — on this project's own
host, active time is about a sixth of elapsed. Reporting elapsed as
"duration" says the loop is slow when the loop is fast and the human is
slow, so both are carried and named separately: ``active`` is what the
loop did, ``parked`` is what it waited on you for.

**Turns, not tokens, are the cost.** Every turn re-sends the session
context, so a run's spend tracks turns far more closely than jobs; tokens
and cache reads are carried too, but turns lead.

**A cancelled run is not a failure.** It is a decision, so the success
rate is taken over the runs that finished on their own — cancelled ones
are counted and reported, never folded into the denominator.

Everything here is a pure fold over rows from
:meth:`~sbxloop.engine.store.StateStore.runs_between` and
:meth:`~sbxloop.engine.store.StateStore.phases_between`, so it is tested
without a screen.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import RowMapping

#: The window the Overview reports on, and the number of buckets its
#: trends are drawn with — a day per bucket over a week.
WINDOW_S = 7 * 86400.0
BUCKETS = 7

#: Run states that finished the way they were meant to, and the one that
#: is a human's decision rather than an outcome.
LANDED = ("merged", "completed")
CANCELLED = "cancelled"

#: How long a computed window is served before it is recomputed. The
#: console ticks several times a second; nothing in a week-long window
#: moves that fast, and the store only grows.
CACHE_TTL_S = 10.0


class WindowStore(Protocol):
    """The slice of the engine store this module reads."""

    def runs_between(self, since: float, until: float) -> list[RowMapping]: ...

    def phases_between(self, since: float, until: float) -> list[RowMapping]: ...

    def task_totals_between(self, since: float, until: float) -> RowMapping: ...


@dataclass(frozen=True)
class Lane:
    """One run kind's totals. Code and workload runs differ by an order of
    magnitude in turns and duration, so blending them describes neither."""

    kind: str
    runs: int = 0
    landed: int = 0
    failed: int = 0
    cancelled: int = 0
    turns: int = 0
    tokens: int = 0
    cache: int = 0
    active: float = 0.0
    elapsed: float = 0.0

    @property
    def parked(self) -> float:
        """Elapsed the loop did not spend working — waiting on a human."""
        return max(self.elapsed - self.active, 0.0)

    @property
    def judged(self) -> int:
        """Runs whose outcome the loop owns: cancelled ones are excluded."""
        return self.landed + self.failed

    @property
    def ok_rate(self) -> float | None:
        """Share of judged runs that landed; ``None`` when none were."""
        return self.landed / self.judged if self.judged else None

    @property
    def turns_per_run(self) -> float:
        return self.turns / self.runs if self.runs else 0.0

    @property
    def tokens_per_turn(self) -> float:
        """The fixed context each turn re-sends — the number that moves
        when a prompt grows."""
        return self.tokens / self.turns if self.turns else 0.0

    @property
    def active_per_run(self) -> float:
        return self.active / self.runs if self.runs else 0.0

    @property
    def parked_per_run(self) -> float:
        return self.parked / self.runs if self.runs else 0.0

    @property
    def parked_share(self) -> float:
        return self.parked / self.elapsed if self.elapsed else 0.0


@dataclass(frozen=True)
class PhaseSlice:
    """One phase's share of the window: its time, its cost and its rework."""

    phase: str
    seconds: float
    attempts: int
    turns: int
    tokens: int = 0
    cache: int = 0
    retries: int = 0

    @property
    def cache_ratio(self) -> float:
        """Cache reads per fresh token. This is a *per-phase* fact — a
        phase that re-sends a large fixed context reads far more than it
        writes, and the phases differ by an order of magnitude."""
        return self.cache / self.tokens if self.tokens else 0.0

    @property
    def seconds_per_attempt(self) -> float:
        return self.seconds / self.attempts if self.attempts else 0.0


@dataclass(frozen=True)
class Rework:
    """What the window's tasks cost in going round again."""

    tasks: int = 0
    revisions: int = 0
    replans: int = 0
    suspect: int = 0

    @property
    def retried_share(self) -> float:
        return self.revisions / self.tasks if self.tasks else 0.0


@dataclass(frozen=True)
class Day:
    """One bucket of the trend, by outcome."""

    landed: int = 0
    failed: int = 0
    cancelled: int = 0

    @property
    def runs(self) -> int:
        return self.landed + self.failed + self.cancelled


@dataclass(frozen=True)
class RunRow:
    """One run, as the outlier lists rank it."""

    run_id: str
    kind: str
    state: str
    turns: int
    tokens: int
    active: float
    parked: float


@dataclass(frozen=True)
class Analytics:
    """A window, folded."""

    since: float
    until: float
    lanes: dict[str, Lane] = field(default_factory=dict)
    phases: tuple[PhaseSlice, ...] = ()
    costliest: tuple[RunRow, ...] = ()
    longest_parked: tuple[RunRow, ...] = ()
    #: Every run in the window, for the distributions.
    runs_seen: tuple[RunRow, ...] = ()
    failures: tuple[tuple[str, int], ...] = ()
    daily: dict[str, tuple[int, ...]] = field(default_factory=dict)
    #: One entry per bucket, split by outcome — the trend as a stack
    #: rather than a single line.
    days: tuple[Day, ...] = ()
    rework: Rework = field(default_factory=Rework)
    #: Review, CI and delivery-update rounds the window's runs spent.
    review_rounds: int = 0
    ci_rounds: int = 0
    #: The same window, one window earlier — what "better or worse" is
    #: measured against. ``None`` when nothing preceded it.
    previous: Lane | None = None

    @property
    def empty(self) -> bool:
        return not any(lane.runs for lane in self.lanes.values())

    def lane(self, kind: str) -> Lane:
        return self.lanes.get(kind, Lane(kind))

    @property
    def total(self) -> Lane:
        """Every kind together — for the headline, where the split would
        be noise."""
        merged = Lane("all")
        for lane in self.lanes.values():
            merged = Lane(
                "all",
                merged.runs + lane.runs,
                merged.landed + lane.landed,
                merged.failed + lane.failed,
                merged.cancelled + lane.cancelled,
                merged.turns + lane.turns,
                merged.tokens + lane.tokens,
                merged.cache + lane.cache,
                merged.active + lane.active,
                merged.elapsed + lane.elapsed,
            )
        return merged

    @property
    def active_seconds(self) -> float:
        return sum(slice_.seconds for slice_ in self.phases)

    @property
    def phase_turns(self) -> tuple[PhaseSlice, ...]:
        """Phases by what they cost rather than by how long they took —
        the two orders are not the same, and cost is the one you act on."""
        return tuple(sorted((p for p in self.phases if p.turns), key=lambda p: -p.turns))

    @property
    def retried_phases(self) -> tuple[PhaseSlice, ...]:
        """Where the loop went round again, most first."""
        return tuple(sorted((p for p in self.phases if p.retries), key=lambda p: -p.retries))

    def delta(self, metric: str) -> float | None:
        """This window against the one before it, as a share. ``None``
        when there is no previous window to compare with, or it was
        empty — a change from nothing is not a percentage."""
        if self.previous is None:
            return None
        before = getattr(self.previous, metric, 0.0) or 0.0
        after = getattr(self.total, metric, 0.0) or 0.0
        if not before:
            return None
        return (after - before) / before

    def spread(self, values: Sequence[float]) -> tuple[float, float]:
        """Median and p90 — a mean hides the run that was ten times the
        rest, and that run is the point."""
        ordered = sorted(values)
        if not ordered:
            return 0.0, 0.0
        mid = ordered[len(ordered) // 2]
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        return mid, p90

    @property
    def turns_spread(self) -> tuple[float, float]:
        return self.spread([float(r.turns) for r in self.runs_seen])

    @property
    def landed_runs(self) -> tuple[RunRow, ...]:
        """The runs that finished the way they were meant to."""
        return tuple(r for r in self.runs_seen if r.state in LANDED)

    @property
    def cycle_spread(self) -> tuple[float, float]:
        """How long work took to land, end to end — the number a person
        waiting on a pull request actually feels. Elapsed, not active:
        the wait is part of the cycle even when the loop was idle for it."""
        return self.spread([r.active + r.parked for r in self.landed_runs])

    @property
    def slowest_to_land(self) -> tuple[RunRow, ...]:
        """Landed runs by how long they took, slowest first."""
        return tuple(sorted(self.landed_runs, key=lambda r: -(r.active + r.parked)))

    @property
    def active_spread(self) -> tuple[float, float]:
        return self.spread([r.active for r in self.runs_seen])


def _reason(raw: str | None) -> str:
    """A failure reason short enough to rank by. The tail of a reason is
    usually the particular run's detail; the head is the class."""
    text = (raw or "unknown").strip()
    head = text.split(":", 1)[0].strip()
    return head[:40] or "unknown"


def fold(
    runs: Sequence[RowMapping],
    phases: Sequence[RowMapping],
    *,
    since: float,
    until: float,
    buckets: int = BUCKETS,
    top: int = 8,
    tasks: RowMapping | None = None,
    previous: Sequence[RowMapping] = (),
) -> Analytics:
    """Fold the window's rows into what the screen reports."""
    lanes: dict[str, Lane] = {}
    rows: list[RunRow] = []
    reasons: Counter[str] = Counter()
    width = (until - since) / buckets if buckets and until > since else 0.0
    daily: dict[str, list[int]] = {"runs": [0] * buckets, "turns": [0] * buckets}
    days: list[Day] = [Day() for _ in range(buckets)]
    rounds = [0, 0]

    for row in runs:
        kind = str(row["kind"])
        lane = lanes.get(kind, Lane(kind))
        state = str(row["state"])
        active = float(row["active"])
        elapsed = max(float(row["updated_at"]) - float(row["created_at"]), 0.0)
        lanes[kind] = Lane(
            kind,
            lane.runs + 1,
            lane.landed + (1 if state in LANDED else 0),
            lane.failed + (1 if state == "failed" else 0),
            lane.cancelled + (1 if state == CANCELLED else 0),
            lane.turns + int(row["turns"]),
            lane.tokens + int(row["tokens"]),
            lane.cache + int(row["cache"]),
            lane.active + active,
            lane.elapsed + elapsed,
        )
        rows.append(
            RunRow(
                str(row["run_id"]),
                kind,
                state,
                int(row["turns"]),
                int(row["tokens"]),
                active,
                max(elapsed - active, 0.0),
            )
        )
        if state == "failed":
            reasons[_reason(row["reason"])] += 1
        rounds[0] += int(row["review_rounds"] or 0)
        rounds[1] += int(row["ci_rounds"] or 0)
        if width > 0:
            index = min(buckets - 1, int((float(row["created_at"]) - since) // width))
            if 0 <= index < buckets:
                daily["runs"][index] += 1
                daily["turns"][index] += int(row["turns"])
                day = days[index]
                days[index] = Day(
                    day.landed + (1 if state in LANDED else 0),
                    day.failed + (1 if state == "failed" else 0),
                    day.cancelled + (1 if state == CANCELLED else 0),
                )

    costliest = tuple(sorted(rows, key=lambda r: -r.turns)[:top])
    parked = tuple(sorted(rows, key=lambda r: -r.parked)[:top])
    return Analytics(
        since=since,
        until=until,
        lanes=lanes,
        phases=tuple(
            PhaseSlice(
                str(p["phase"]),
                float(p["seconds"]),
                int(p["attempts"]),
                int(p["turns"]),
                int(p["tokens"]),
                int(p["cache"]),
                int(p["retries"]),
            )
            for p in phases
        ),
        costliest=tuple(r for r in costliest if r.turns),
        longest_parked=tuple(r for r in parked if r.parked > 0),
        runs_seen=tuple(rows),
        failures=tuple(reasons.most_common(top)),
        daily={key: tuple(values) for key, values in daily.items()},
        days=tuple(days),
        rework=(
            Rework(
                int(tasks["tasks"]),
                int(tasks["revisions"]),
                int(tasks["replans"]),
                int(tasks["suspect"]),
            )
            if tasks is not None
            else Rework()
        ),
        review_rounds=rounds[0],
        ci_rounds=rounds[1],
        previous=_lane_of(previous) if previous else None,
    )


def _lane_of(rows: Sequence[RowMapping]) -> Lane:
    """Every kind together, for the window before this one."""
    lane = Lane("previous")
    for row in rows:
        state = str(row["state"])
        active = float(row["active"])
        elapsed = max(float(row["updated_at"]) - float(row["created_at"]), 0.0)
        lane = Lane(
            "previous",
            lane.runs + 1,
            lane.landed + (1 if state in LANDED else 0),
            lane.failed + (1 if state == "failed" else 0),
            lane.cancelled + (1 if state == CANCELLED else 0),
            lane.turns + int(row["turns"]),
            lane.tokens + int(row["tokens"]),
            lane.cache + int(row["cache"]),
            lane.active + active,
            lane.elapsed + elapsed,
        )
    return lane


def compute(
    store: WindowStore,
    *,
    now: float | None = None,
    window_s: float = WINDOW_S,
    buckets: int = BUCKETS,
) -> Analytics:
    """Read the window and fold it."""
    until = time.time() if now is None else now
    since = until - window_s
    return fold(
        store.runs_between(since, until + 1.0),
        store.phases_between(since, until + 1.0),
        since=since,
        until=until,
        buckets=buckets,
        tasks=store.task_totals_between(since, until + 1.0),
        # The window before this one, so every headline can say whether it
        # is better or worse than it was.
        previous=store.runs_between(since - window_s, since),
    )


class Cache:
    """One computed window, served for :data:`CACHE_TTL_S`.

    The Overview's poller runs on the console's tick; recomputing a week
    of runs several times a second would scan the store for a picture that
    cannot have changed."""

    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._value: Analytics | None = None
        self._at = 0.0

    def stale(self, now: float) -> bool:
        return self._value is None or now - self._at >= self.ttl_s

    def get(self) -> Analytics | None:
        return self._value

    def put(self, value: Analytics, now: float) -> None:
        self._value, self._at = value, now

    def clear(self) -> None:
        self._value, self._at = None, 0.0


__all__ = [
    "BUCKETS",
    "CACHE_TTL_S",
    "WINDOW_S",
    "Analytics",
    "Cache",
    "Day",
    "Lane",
    "PhaseSlice",
    "Rework",
    "RunRow",
    "WindowStore",
    "compute",
    "fold",
]
