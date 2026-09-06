"""`[[schedules]]` (#761): the cadence grammar and arithmetic, and the
loop firing ticks on a fake clock.

The property every test here holds the loop to: a tick is recorded at its
*due* time, so a late daemon does not shift the grid and one that missed
several ticks catches up with one — never one per missed tick.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sbxloop.config import Config
from sbxloop.daemon.control import dispatch
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.schedule import (
    Cadence,
    CronSpec,
    ScheduleRow,
    format_due,
    format_every,
    parse_every,
)
from sbxloop.daemon.sources import ChatSource, CompositeSource, ScheduleSource
from sbxloop.ghids import (
    is_local_id,
    is_schedule_id,
    parse_schedule_id,
    schedule_item_id,
)
from tests.unit.test_daemon_loop import Harness, RecordingFrontend

UTC_TZ = ZoneInfo("UTC")
T0 = 1_000_000.0
HOUR = 3600.0


# -- the grammar -----------------------------------------------------------------


class TestEvery:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("24h", 86400),
            ("90m", 5400),
            ("1h30m", 5400),
            ("1d", 86400),
            ("2w", 1209600),
            ("60s", 60),
        ],
    )
    def test_periods(self, text: str, seconds: int) -> None:
        assert parse_every(text) == seconds

    @pytest.mark.parametrize("bad", ["", "5", "h1", "1h 30x", "1x", "30s"])
    def test_refusals_name_the_grammar(self, bad: str) -> None:
        with pytest.raises(ValueError, match="every must be"):
            parse_every(bad)

    def test_format_round_trips(self) -> None:
        assert format_every(86400) == "1d"
        assert format_every(5400) == "1h30m"
        assert format_every(parse_every("90m")) == "1h30m"


class TestCron:
    def test_five_fields_with_names_ranges_lists_and_steps(self) -> None:
        spec = CronSpec.parse("*/15 9-17 1,15 jan,jul mon-fri")
        assert spec.minutes == frozenset({0, 15, 30, 45})
        assert spec.hours == frozenset(range(9, 18))
        assert spec.days == frozenset({1, 15})
        assert spec.months == frozenset({1, 7})
        assert spec.weekdays == frozenset({1, 2, 3, 4, 5})

    def test_sunday_is_zero_or_seven(self) -> None:
        assert CronSpec.parse("0 0 * * 7").weekdays == frozenset({0})
        assert CronSpec.parse("0 0 * * sun").weekdays == frozenset({0})
        assert CronSpec.parse("0 0 * * 1-7").weekdays == frozenset(range(7))

    @pytest.mark.parametrize(
        ("bad", "why"),
        [
            ("* * * *", "five fields"),
            ("60 * * * *", "outside 0..59"),
            ("0 24 * * *", "outside 0..23"),
            ("0 0 0 * *", "outside 1..31"),
            ("0 0 * 13 *", "outside 1..12"),
            ("0 0 * * 8", "outside 0..7"),
            ("0 0 31 2 *", "never matches"),
            ("5-1 * * * *", "runs backwards"),
            ("*/0 * * * *", "bad step"),
            ("a * * * *", "bad value"),
        ],
    )
    def test_refusals(self, bad: str, why: str) -> None:
        with pytest.raises(ValueError, match=why):
            CronSpec.parse(bad)

    def test_next_and_previous_match(self) -> None:
        spec = CronSpec.parse("0 7 * * mon-fri")
        saturday = datetime(2026, 9, 5, 12, 0, tzinfo=UTC_TZ)
        assert spec.next_after(saturday) == datetime(2026, 9, 7, 7, 0, tzinfo=UTC_TZ)
        assert spec.latest_at_or_before(saturday) == datetime(2026, 9, 4, 7, 0, tzinfo=UTC_TZ)
        # At the match itself: "after" is strict, "at or before" inclusive.
        monday = datetime(2026, 9, 7, 7, 0, tzinfo=UTC_TZ)
        assert spec.next_after(monday) == datetime(2026, 9, 8, 7, 0, tzinfo=UTC_TZ)
        assert spec.latest_at_or_before(monday) == monday

    def test_day_of_month_or_weekday_when_both_restricted(self) -> None:
        # vixie cron: the 1st OR a Monday.
        spec = CronSpec.parse("0 0 1 * mon")
        wed = datetime(2026, 9, 2, 0, 0, tzinfo=UTC_TZ)
        assert spec.next_after(wed) == datetime(2026, 9, 7, 0, 0, tzinfo=UTC_TZ)
        assert spec.next_after(datetime(2026, 9, 29, tzinfo=UTC_TZ)) == datetime(
            2026, 10, 1, tzinfo=UTC_TZ
        )

    def test_reads_the_schedules_timezone(self) -> None:
        spec = CronSpec.parse("0 7 * * *")
        london = ZoneInfo("Europe/London")
        after = datetime(2026, 9, 5, 5, 0, tzinfo=UTC_TZ)
        # 07:00 London in September is 06:00 UTC.
        nxt = spec.next_after(after.astimezone(london))
        assert nxt.astimezone(UTC_TZ) == datetime(2026, 9, 5, 6, 0, tzinfo=UTC_TZ)


class TestCadence:
    def test_every_is_a_grid_from_the_base(self) -> None:
        cadence = Cadence.parse("1h", None)
        assert cadence.next_due(T0, UTC_TZ) == T0 + HOUR
        assert cadence.latest_due(T0, T0 + HOUR - 1, UTC_TZ) is None
        assert cadence.latest_due(T0, T0 + HOUR, UTC_TZ) == T0 + HOUR
        # 2h10 past: the latest grid point, not every one since.
        assert cadence.latest_due(T0, T0 + 2 * HOUR + 600, UTC_TZ) == T0 + 2 * HOUR
        assert cadence.describe() == "every 1h"

    def test_cron_is_the_stated_minutes(self) -> None:
        cadence = Cadence.parse(None, "0 7 * * *")
        base = datetime(2026, 9, 5, 6, 0, tzinfo=UTC_TZ).timestamp()
        seven = datetime(2026, 9, 5, 7, 0, tzinfo=UTC_TZ).timestamp()
        assert cadence.next_due(base, UTC_TZ) == seven
        assert cadence.latest_due(base, seven - 60, UTC_TZ) is None
        assert cadence.latest_due(base, seven, UTC_TZ) == seven
        assert cadence.latest_due(base, seven + 3 * 86400, UTC_TZ) == seven + 3 * 86400
        assert cadence.describe() == "cron 0 7 * * *"

    def test_exactly_one_cadence(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Cadence.parse(None, None)
        with pytest.raises(ValueError, match="exactly one"):
            Cadence.parse("1h", "* * * * *")

    def test_due_is_named_to_the_minute_in_utc(self) -> None:
        ts = datetime(2026, 9, 5, 7, 0, 30, tzinfo=UTC_TZ).timestamp()
        assert format_due(ts) == "2026-09-05T07:00Z"

    def test_row_base(self) -> None:
        assert ScheduleRow(name="x", anchor=5.0).base == 5.0
        assert ScheduleRow(name="x", anchor=5.0, last_due=9.0).base == 9.0


# -- the ids and the sources ------------------------------------------------------


def test_schedule_ids_name_the_schedule_and_its_due() -> None:
    item_id = schedule_item_id("morning-brief", "2026-09-05T07:00Z")
    assert item_id == "sched:morning-brief:2026-09-05T07:00Z"
    assert parse_schedule_id(item_id) == ("morning-brief", "2026-09-05T07:00Z")
    assert is_schedule_id(item_id) and not is_schedule_id("chat:1") and not is_schedule_id("sched:")
    assert is_local_id(item_id) and is_local_id("chat:1") and not is_local_id("gh:issue:1")
    with pytest.raises(ValueError, match="malformed schedule name"):
        schedule_item_id("a:b", "t")
    with pytest.raises(ValueError, match="not a schedule id"):
        parse_schedule_id("gh:issue:1")
    with pytest.raises(ValueError, match="malformed schedule id"):
        parse_schedule_id("sched:noduetime")


def test_composite_routes_schedule_ticks_to_the_schedule_source() -> None:
    chat, sched = ChatSource(), ScheduleSource()
    src = CompositeSource(None, chat, sched)
    assert src.name == "chat+schedule" and src.poll() == []
    tick = WorkItem(item_id="sched:m:2026-09-05T07:00Z", source_key="x", title="t", kind="workload")
    ask = WorkItem(item_id="chat:1", source_key="1", title="t", kind="workload")
    assert src.for_item(tick) is sched and src.for_item(ask) is chat
    assert src.claim(tick) is True and src.settle_claim(tick) is False
    # The GitHub extras are not there without GitHub.
    with pytest.raises(AttributeError):
        src.repo_health  # noqa: B018
    assert CompositeSource(None, None, sched).for_item(ask) is sched
    with pytest.raises(ValueError, match="at least one"):
        CompositeSource(None)


# -- the config ---------------------------------------------------------------------


def config_with(tmp_path: Path, *schedules: dict[str, Any], **daemon: Any) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "daemon": daemon,
            "workloads": [{"name": "brief"}],
            "schedules": list(schedules),
        }
    )


EVERY_HOUR = {"name": "hourly", "profile": "brief", "ask": "Summarise the hour", "every": "1h"}


class TestConfig:
    def test_a_schedule_names_a_profile_that_exists(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"schedules\.hourly\.profile = 'nope' names no"):
            config_with(tmp_path, {**EVERY_HOUR, "profile": "nope"})

    def test_names_are_unique_and_identifier_like(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="declared twice"):
            config_with(tmp_path, EVERY_HOUR, EVERY_HOUR)
        with pytest.raises(ValueError, match=r"schedules\[\]\.name must be"):
            config_with(tmp_path, {**EVERY_HOUR, "name": "no spaces"})

    def test_exactly_one_cadence_and_a_real_timezone(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="set exactly one of every / cron"):
            config_with(tmp_path, {**EVERY_HOUR, "cron": "* * * * *"})
        with pytest.raises(ValueError, match="set exactly one of every / cron"):
            config_with(tmp_path, {k: v for k, v in EVERY_HOUR.items() if k != "every"})
        with pytest.raises(ValueError, match=r"schedules\.hourly: every must be"):
            config_with(tmp_path, {**EVERY_HOUR, "every": "soon"})
        with pytest.raises(ValueError, match=r"schedules\.hourly: cron"):
            config_with(tmp_path, {**EVERY_HOUR, "every": None, "cron": "0 0 31 2 *"})
        with pytest.raises(ValueError, match="valid IANA timezone"):
            config_with(tmp_path, {**EVERY_HOUR, "timezone": "Mars/Olympus"})
        with pytest.raises(ValueError, match="ask must not be blank"):
            config_with(tmp_path, {**EVERY_HOUR, "ask": "  "})
        config = config_with(tmp_path, {**EVERY_HOUR, "timezone": "Europe/London"})
        assert config.schedule("hourly") is config.schedules[0]
        assert config.schedule("nope") is None
        assert config.schedules[0].cadence_text == "every 1h"


# -- the loop ------------------------------------------------------------------------


def harness(tmp_path: Path, *schedules: dict[str, Any], **daemon: Any) -> Harness:
    h = Harness(tmp_path, config_with(tmp_path, *(schedules or (EVERY_HOUR,)), **daemon))
    h.loop.frontend = RecordingFrontend()
    return h


def kinds(h: Harness, kind: str) -> list[str]:
    return [n.text for n in h.loop.frontend.notices if n.kind == kind]  # type: ignore[attr-defined]


def tick_at(h: Harness, t: float) -> Any:
    h.clock.t = t
    return h.loop.tick()


class TestFiring:
    def test_first_sight_anchors_the_grid_and_fires_on_it(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        assert tick_at(h, T0).idle_kind == "no_work"
        (row,) = h.loop.schedules()
        assert row["next_due"] == T0 + HOUR and row["last_fired_at"] is None
        assert tick_at(h, T0 + HOUR - 1).idle_kind == "no_work"
        result = tick_at(h, T0 + HOUR)
        assert result.discovered == 1 and result.outcome == "done"
        (item,) = h.dstore.items()
        assert item.item_id == f"sched:hourly:{format_due(T0 + HOUR)}"
        assert item.kind == "workload" and item.profile == "brief" and item.state == "done"
        assert item.title == "Summarise the hour" and item.body == "Summarise the hour"
        assert kinds(h, "daemon.schedule_fired") == [
            f"⏰ schedule hourly: tick due {format_due(T0 + HOUR)} queued as {item.item_id}"
        ]
        assert len(kinds(h, "run.done")) == 1
        (row,) = h.loop.schedules()
        assert row["last_due"] == T0 + HOUR and row["last_item"] == item.item_id
        assert row["next_due"] == T0 + 2 * HOUR

    def test_a_late_tick_keeps_the_grid(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        # Seen at t+1h10: fires (recorded at t+1h), and the next is t+2h,
        # not t+2h10.
        assert tick_at(h, T0 + HOUR + 600).discovered == 1
        assert tick_at(h, T0 + 2 * HOUR - 1).discovered == 0
        assert tick_at(h, T0 + 2 * HOUR).discovered == 1
        assert [i.item_id for i in h.dstore.items()] == [
            f"sched:hourly:{format_due(T0 + HOUR)}",
            f"sched:hourly:{format_due(T0 + 2 * HOUR)}",
        ]

    def test_a_restart_catches_up_with_one_tick_at_most(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        tick_at(h, T0 + HOUR)
        # Down until t+1h30: nothing owed; next fire t+2h.
        h2 = harness(tmp_path)
        assert tick_at(h2, T0 + HOUR + 1800).discovered == 0
        assert h2.loop.schedules()[0]["next_due"] == T0 + 2 * HOUR
        # Down from t+1h until t+3h10: one catch-up tick (t+3h), then t+4h.
        h3 = harness(tmp_path)
        assert tick_at(h3, T0 + 3 * HOUR + 600).discovered == 1
        assert h3.dstore.items()[-1].item_id == f"sched:hourly:{format_due(T0 + 3 * HOUR)}"
        assert h3.loop.schedules()[0]["next_due"] == T0 + 4 * HOUR
        assert len(h3.dstore.items()) == 2

    def test_a_tick_queued_before_the_crash_is_not_queued_twice(self, tmp_path: Path) -> None:
        # The item lands before the row records the fire; a daemon that
        # died between the two finds its tick already queued and only
        # catches the row up.
        h = harness(tmp_path)
        tick_at(h, T0)
        due = T0 + HOUR
        h.loop.dstore.upsert_new(
            h.loop._schedule_item("hourly", "x", "brief", format_due(due)), due
        )
        assert tick_at(h, due).discovered == 0
        assert len(h.dstore.items()) == 1 and h.dstore.items()[0].state == "done"
        assert h.loop.schedules()[0]["last_due"] == due
        assert kinds(h, "daemon.schedule_fired") == []

    def test_a_tick_still_live_skips_the_next_and_says_so(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        h.outcomes = ["raise"]  # the run fails; the item waits in retry backoff
        tick_at(h, T0 + HOUR)
        (item,) = h.dstore.items()
        assert item.state == "queued" and item.attempts == 1
        h.outcomes = ["raise"]
        result = tick_at(h, T0 + 2 * HOUR)
        assert result.discovered == 0
        assert len(h.dstore.items()) == 1
        (skipped,) = kinds(h, "daemon.schedule_skipped")
        assert skipped.startswith(
            f"⏭ schedule hourly: tick due {format_due(T0 + 2 * HOUR)} skipped — "
            f"{item.item_id} is still queued"
        )
        # The skipped tick is handled: the grid moves on to t+3h.
        assert h.loop.schedules()[0]["next_due"] == T0 + 3 * HOUR

    def test_a_cron_schedule_fires_on_its_minutes_in_its_zone(self, tmp_path: Path) -> None:
        daily = {
            "name": "daily",
            "profile": "brief",
            "ask": "Morning brief",
            "cron": "0 7 * * *",
            "timezone": "Europe/London",
        }
        h = harness(tmp_path, daily)
        seven = datetime(2026, 9, 5, 7, 0, tzinfo=ZoneInfo("Europe/London")).timestamp()
        assert tick_at(h, seven - 3600).discovered == 0
        assert tick_at(h, seven - 60).discovered == 0
        assert tick_at(h, seven + 30).discovered == 1
        (item,) = h.dstore.items()
        assert item.item_id == "sched:daily:2026-09-05T06:00Z"  # 07:00 London = 06:00 UTC
        assert tick_at(h, seven + 1800).discovered == 0
        assert tick_at(h, seven + 86400).discovered == 1
        (row,) = h.loop.schedules()
        assert row["cadence"] == "cron 0 7 * * *" and row["timezone"] == "Europe/London"

    def test_the_default_zone_is_the_run_caps(self, tmp_path: Path) -> None:
        h = harness(
            tmp_path,
            {"name": "d", "profile": "brief", "ask": "x", "cron": "0 7 * * *"},
            run_cap_timezone="America/New_York",
        )
        assert h.loop.schedules()[0]["timezone"] == "America/New_York"

    def test_the_outcome_names_the_schedule(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        tick_at(h, T0 + HOUR)
        (item,) = h.dstore.items()
        text = h.loop.outcome_text(item)
        assert text.startswith("Summarise the hour")
        assert (
            f"This work item came from: the schedule `hourly`, due {format_due(T0 + HOUR)}." in text
        )
        assert h.source.calls[-1][0] == "completed"

    def test_a_paused_daemon_fires_once_on_resume(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        h.loop.pause()
        assert tick_at(h, T0 + 3 * HOUR).idle_kind == "paused"
        h.loop.unpause()
        assert tick_at(h, T0 + 3 * HOUR + 60).discovered == 1
        assert len(h.dstore.items()) == 1


class TestControl:
    def test_schedules_lists_cadence_last_fire_and_next_due(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        text = dispatch(h.loop, "schedules").text
        assert "**hourly** · every 1h (UTC) · profile `brief`" in text
        assert "never fired" in text and "next due in 1.0h" in text
        tick_at(h, T0 + HOUR)
        h.clock.t = T0 + HOUR + 300
        text = dispatch(h.loop, "schedules").text
        assert "last fired 5m ago (sched:hourly:" in text and "next due in 55m" in text

    def test_no_schedules(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert dispatch(h.loop, "schedules").text.startswith("no schedules configured")

    def test_pause_and_resume_one_schedule(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        tick_at(h, T0)
        reply = dispatch(h.loop, "schedules pause hourly", by="brett")
        assert reply.ok and reply.text.startswith("schedule hourly paused")
        assert not h.loop.paused  # the daemon itself is not held
        assert kinds(h, "daemon.schedule_paused") == [
            "⏸ schedule hourly paused by brett; its ticks are skipped until resumed"
        ]
        assert dispatch(h.loop, "schedules pause hourly", by="brett").ok is not False
        assert "already paused" in dispatch(h.loop, "schedules pause hourly").text
        # Two ticks pass unfired; they are not made up on resume — even the
        # one that came due while the daemon was not ticking.
        assert tick_at(h, T0 + 2 * HOUR).discovered == 0
        assert h.dstore.items() == []
        assert "paused by brett" in dispatch(h.loop, "schedules").text
        h.clock.t = T0 + 3 * HOUR + 60
        reply = dispatch(h.loop, "schedules resume hourly", by="brett")
        assert reply.ok and "fires from its next tick" in reply.text
        assert "not paused" in dispatch(h.loop, "schedules resume hourly").text
        assert tick_at(h, T0 + 3 * HOUR + 120).discovered == 0
        assert tick_at(h, T0 + 4 * HOUR).discovered == 1

    def test_unknown_schedule_and_usage(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        reply = dispatch(h.loop, "schedules pause nope")
        assert not reply.ok and "no schedule called 'nope' (configured: hourly)" in reply.text
        assert not dispatch(h.loop, "schedules stop hourly").ok
        assert not dispatch(h.loop, "schedules pause").ok
        assert "schedules [pause <name>|resume <name>]" in dispatch(h.loop, "bogus").text
