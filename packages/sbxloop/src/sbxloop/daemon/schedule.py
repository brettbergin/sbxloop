"""Workloads on a cadence (#761): the timing behind ``[[schedules]]``.

A schedule is a workload ask the daemon queues by itself — ``every =
"24h"`` on a grid from the moment the daemon first saw the schedule, or
``cron = "0 7 * * 1-5"`` on the stated minutes in the schedule's timezone.
This module owns the cadence grammar and the arithmetic; the loop's
``_fire_schedules`` decides what to do with a due tick (queue an item,
skip it because the last one is still live, swallow it because the
schedule is paused) and the store keeps each schedule's row.

The invariant every path here preserves: a fire is recorded at the
*due* time, never the time the daemon got round to it. The grid does not
drift when a tick is late, and a daemon that was down for several ticks
catches up with one — the latest due — not one per missed tick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# The shortest `every`; the loop polls on the order of a minute, so a
# tighter grid would only queue ticks the queue then skips.
EVERY_MIN_S = 60
_EVERY_RE = re.compile(r"(\d+)\s*([smhdw])")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
# The cron search stops here rather than spin on a spec that can never
# match (`0 0 31 2 *`).
_CRON_HORIZON = timedelta(days=366 * 5)
# Item ids carry the due time to the minute, in UTC, so a tick is one item
# whatever timezone the schedule counts in.
DUE_FORMAT = "%Y-%m-%dT%H:%MZ"


def parse_every(text: str) -> int:
    """``"24h"``, ``"90m"``, ``"1h30m"``, ``"1d"``, ``"2w"`` as seconds.
    Units may be combined largest-first; anything else, or a period under
    :data:`EVERY_MIN_S`, is a ``ValueError`` naming what was expected."""
    raw = text.strip().lower()
    parts = _EVERY_RE.findall(raw)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != raw.replace(" ", ""):
        raise ValueError(
            f"every must be a period like '24h', '90m', '1h30m', '1d' or '2w', got {text!r}"
        )
    seconds = sum(int(n) * _UNIT_S[u] for n, u in parts)
    if seconds < EVERY_MIN_S:
        raise ValueError(f"every must be at least {EVERY_MIN_S // 60}m, got {text!r}")
    return seconds


def format_every(seconds: int) -> str:
    """The period as a person would write it: ``86400`` → ``24h``."""
    out = []
    rest = seconds
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, rest = divmod(rest, size)
        if count:
            out.append(f"{count}{unit}")
    return "".join(out) or "0s"


def format_due(ts: float) -> str:
    """The due instant as the item id and the notices carry it."""
    return datetime.fromtimestamp(ts, UTC).strftime(DUE_FORMAT)


def _parse_field(
    text: str, lo: int, hi: int, names: tuple[str, ...] = (), *, accept_hi: int | None = None
) -> frozenset[int]:
    """One cron field: ``*``, ``*/n``, ``a``, ``a-b``, ``a-b/n``, names,
    comma lists. Values must lie in ``lo..hi`` (``accept_hi`` widens the
    check — Sunday as 7 — and the caller folds it)."""
    top = hi if accept_hi is None else accept_hi

    def value(token: str) -> int:
        token = token.lower()
        if names and token in names:
            return names.index(token) + lo
        if not token.isdigit():
            raise ValueError(f"bad value {token!r}")
        return int(token)

    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty list entry")
        span, _, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() else (1 if not step_text else 0)
        if step < 1:
            raise ValueError(f"bad step in {part!r}")
        if span == "*":
            first, last = lo, hi
        elif "-" in span:
            a, _, b = span.partition("-")
            first, last = value(a), value(b)
        else:
            first = value(span)
            # `n/step` in vixie cron means n..hi by step; a bare value
            # is itself.
            last = hi if step_text else first
        if not (lo <= first <= top and lo <= last <= top):
            raise ValueError(f"{part!r} is outside {lo}..{top}")
        if first > last:
            raise ValueError(f"{part!r} runs backwards")
        out.update(range(first, last + 1, step))
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A five-field cron expression: minute, hour, day of month, month,
    day of week (0 or 7 = Sunday; ``mon``..``sun`` and ``jan``..``dec``
    accepted). As in vixie cron, a restricted day-of-month and a
    restricted day-of-week match on *either*."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> CronSpec:
        fields = text.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron must have five fields (minute hour day month weekday), got {text!r}"
            )
        try:
            minutes = _parse_field(fields[0], 0, 59)
            hours = _parse_field(fields[1], 0, 23)
            days = _parse_field(fields[2], 1, 31)
            months = _parse_field(fields[3], 1, 12, _MONTHS)
            weekdays = frozenset(d % 7 for d in _parse_field(fields[4], 0, 6, _DAYS, accept_hi=7))
        except ValueError as exc:
            raise ValueError(f"cron {text!r}: {exc}") from None
        spec = cls(
            text=" ".join(fields),
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=weekdays,
            any_day=fields[2] == "*",
            any_weekday=fields[4] == "*",
        )
        # A spec that never matches is an operator's typo to fix now,
        # not a schedule that silently never fires.
        spec.next_after(datetime(2000, 1, 1, tzinfo=UTC))
        return spec

    def _day_matches(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.weekday() + 1) % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def matches(self, dt: datetime) -> bool:
        return (
            dt.month in self.months
            and self._day_matches(dt)
            and dt.hour in self.hours
            and dt.minute in self.minutes
        )

    def next_after(self, dt: datetime) -> datetime:
        """The first matching minute strictly after ``dt`` (tz-aware)."""
        t = (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
        horizon = t + _CRON_HORIZON
        while t < horizon:
            if t.month not in self.months:
                first = t.replace(day=1, hour=0, minute=0)
                t = (first + timedelta(days=32)).replace(day=1)
            elif not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                return t
        raise ValueError(f"cron {self.text!r} never matches")

    def latest_at_or_before(self, dt: datetime) -> datetime:
        """The last matching minute at or before ``dt`` (tz-aware)."""
        t = dt.replace(second=0, microsecond=0)
        horizon = t - _CRON_HORIZON
        minute = timedelta(minutes=1)
        while t > horizon:
            if t.month not in self.months:
                t = t.replace(day=1, hour=0, minute=0) - minute
            elif not self._day_matches(t):
                t = t.replace(hour=0, minute=0) - minute
            elif t.hour not in self.hours:
                t = t.replace(minute=0) - minute
            elif t.minute not in self.minutes:
                t -= minute
            else:
                return t
        raise ValueError(f"cron {self.text!r} never matches")


@dataclass(frozen=True, slots=True)
class Cadence:
    """When a schedule is due: a fixed period on a grid from its anchor,
    or a cron expression read in ``tz``."""

    every_s: int | None = None
    cron: CronSpec | None = None

    @classmethod
    def parse(cls, every: str | None, cron: str | None) -> Cadence:
        if (every is None) == (cron is None):
            raise ValueError("exactly one of every / cron must be set")
        if every is not None:
            return cls(every_s=parse_every(every))
        assert cron is not None
        return cls(cron=CronSpec.parse(cron))

    def describe(self) -> str:
        if self.every_s is not None:
            return f"every {format_every(self.every_s)}"
        assert self.cron is not None
        return f"cron {self.cron.text}"

    def next_due(self, after: float, tz: ZoneInfo) -> float:
        """The first due instant strictly after ``after`` — for `every`,
        the next grid point (``after`` is always one itself)."""
        if self.every_s is not None:
            return after + self.every_s
        assert self.cron is not None
        return self.cron.next_after(datetime.fromtimestamp(after, tz)).timestamp()

    def latest_due(self, after: float, now: float, tz: ZoneInfo) -> float | None:
        """The latest due instant in ``(after, now]``, None when there is
        none: what a tick fires — several missed ticks collapse into the
        one most recent."""
        if self.every_s is not None:
            steps = int((now - after) // self.every_s)
            return after + steps * self.every_s if steps >= 1 else None
        assert self.cron is not None
        latest = self.cron.latest_at_or_before(datetime.fromtimestamp(now, tz)).timestamp()
        return latest if latest > after else None


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """A schedule's persisted state: when it was first seen (the `every`
    grid's origin), the last due it acted on (fired or skipped), the last
    fire and the item it queued, and who paused it."""

    name: str
    anchor: float
    last_due: float | None = None
    last_fired_at: float | None = None
    last_item: str | None = None
    paused_by: str | None = None
    paused_at: float | None = None

    @property
    def base(self) -> float:
        """Where the next search starts: the last due handled, else the anchor."""
        return self.last_due if self.last_due is not None else self.anchor


__all__ = [
    "DUE_FORMAT",
    "EVERY_MIN_S",
    "Cadence",
    "CronSpec",
    "ScheduleRow",
    "format_due",
    "format_every",
    "parse_every",
]
